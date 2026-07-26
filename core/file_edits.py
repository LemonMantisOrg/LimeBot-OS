"""Pure helpers for safe, preflighted text edits.

The agent should change source files through a small, explicit patch rather
than regenerating an entire file.  This module deliberately has no filesystem
or agent dependencies so the same validation is used by the tool and its
approval preview.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple


MAX_EDIT_OPERATIONS = 64
MAX_REPLACEMENTS = 256


class EditValidationError(ValueError):
    """Raised when an edit cannot be applied without guessing."""


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    new_text: str
    edit_index: int


def _coerce_edits(edits: Any) -> List[Dict[str, Any]]:
    if not isinstance(edits, list):
        raise EditValidationError("'edits' must be an array of edit objects.")
    if not edits:
        raise EditValidationError("At least one edit is required.")
    if len(edits) > MAX_EDIT_OPERATIONS:
        raise EditValidationError(
            f"Too many edit operations; maximum is {MAX_EDIT_OPERATIONS}."
        )

    normalized: List[Dict[str, Any]] = []
    for index, raw in enumerate(edits, start=1):
        if not isinstance(raw, Mapping):
            raise EditValidationError(f"Edit #{index} must be an object.")

        old_text = raw.get("old_text")
        new_text = raw.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise EditValidationError(
                f"Edit #{index} requires a non-empty string 'old_text'."
            )
        if not isinstance(new_text, str):
            raise EditValidationError(
                f"Edit #{index} requires a string 'new_text'."
            )

        occurrence = raw.get("occurrence", 1)
        if isinstance(occurrence, bool):
            raise EditValidationError(
                f"Edit #{index} 'occurrence' must be a positive integer."
            )
        try:
            occurrence = int(occurrence)
        except (TypeError, ValueError):
            raise EditValidationError(
                f"Edit #{index} 'occurrence' must be a positive integer."
            ) from None
        if occurrence < 1:
            raise EditValidationError(
                f"Edit #{index} 'occurrence' must be a positive integer."
            )

        replace_all = raw.get("replace_all", False)
        if not isinstance(replace_all, bool):
            raise EditValidationError(
                f"Edit #{index} 'replace_all' must be a boolean."
            )

        if old_text == new_text:
            raise EditValidationError(f"Edit #{index} is a no-op.")

        normalized.append(
            {
                "old_text": old_text,
                "new_text": new_text,
                "occurrence": occurrence,
                "replace_all": replace_all,
            }
        )
    return normalized


def apply_text_edits(original: str, edits: Any) -> Tuple[str, int]:
    """Apply exact, non-overlapping edits to *original* after full preflight.

    Every match is located against the same original snapshot.  That prevents
    one replacement from changing the search surface for a later replacement
    and makes the operation deterministic.  The returned count is the number
    of actual replacements, not the number of edit objects.
    """

    if not isinstance(original, str):
        raise EditValidationError("The target file must be decoded text.")

    normalized = _coerce_edits(edits)
    replacements: List[_Replacement] = []

    for edit_index, edit in enumerate(normalized, start=1):
        old_text = edit["old_text"]
        positions: List[int] = []
        cursor = 0
        while True:
            position = original.find(old_text, cursor)
            if position < 0:
                break
            positions.append(position)
            cursor = position + len(old_text)

        if not positions:
            raise EditValidationError(
                f"Edit #{edit_index} could not find its exact 'old_text'; "
                "re-read the file and retry."
            )

        if edit["replace_all"]:
            selected_positions = positions
        else:
            occurrence = edit["occurrence"]
            if occurrence > len(positions):
                raise EditValidationError(
                    f"Edit #{edit_index} requested occurrence {occurrence}, "
                    f"but only {len(positions)} match(es) exist."
                )
            selected_positions = [positions[occurrence - 1]]

        for position in selected_positions:
            replacements.append(
                _Replacement(
                    start=position,
                    end=position + len(old_text),
                    new_text=edit["new_text"],
                    edit_index=edit_index,
                )
            )

    if len(replacements) > MAX_REPLACEMENTS:
        raise EditValidationError(
            f"Too many replacements; maximum is {MAX_REPLACEMENTS}."
        )

    replacements.sort(key=lambda item: (item.start, item.end))
    for previous, current in zip(replacements, replacements[1:]):
        if current.start < previous.end:
            raise EditValidationError(
                f"Edit #{current.edit_index} overlaps another requested edit; "
                "combine the change into one non-overlapping edit."
            )

    updated = original
    for replacement in reversed(replacements):
        updated = (
            updated[: replacement.start]
            + replacement.new_text
            + updated[replacement.end :]
        )
    if updated == original:
        raise EditValidationError("The requested edits produce no file change.")
    return updated, len(replacements)


def unified_text_diff(
    before: str,
    after: str,
    *,
    fromfile: str,
    tofile: str,
    max_chars: int = 6_000,
) -> str:
    """Return a bounded unified diff suitable for approvals and tool output."""

    import difflib

    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
            n=3,
        )
    )
    diff = "\n".join(lines)
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + "\n... [diff truncated] ..."

