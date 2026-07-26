"""Optional, direct-process diagnostics for coding work.

Diagnostics are deliberately best-effort: LimeBot can use an installed
formatter/linter/type checker when present, but syntax verification and the
core edit workflow never depend on one.  Commands are passed as argument
arrays to ``subprocess.run``; no shell is involved.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_MAX_OUTPUT_CHARS = 12_000
_DEFAULT_TIMEOUT_SECONDS = 45

_PROVIDERS = {
    "ruff": {"language": "python", "binary": "ruff"},
    "pyright": {"language": "python", "binary": "pyright"},
    "eslint": {"language": "javascript", "binary": "eslint"},
    "tsc": {"language": "typescript", "binary": "tsc"},
}

_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
_JAVASCRIPT_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs"})
_TYPESCRIPT_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts"})


def _language_for(path: Path) -> Optional[str]:
    suffix = path.suffix.lower()
    if suffix in _PYTHON_SUFFIXES:
        return "python"
    if suffix in _TYPESCRIPT_SUFFIXES:
        return "typescript"
    if suffix in _JAVASCRIPT_SUFFIXES:
        return "javascript"
    return None


def _module_command(module_name: str) -> Optional[List[str]]:
    try:
        if importlib.util.find_spec(module_name) is not None:
            return [sys.executable, "-m", module_name]
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    return None


def _find_executable(provider: str) -> Optional[List[str]]:
    spec = _PROVIDERS[provider]
    binary = spec["binary"]
    direct = shutil.which(binary)
    if direct:
        return [direct]

    # A running LimeBot often lives inside .venv even when the shell PATH was
    # not activated.  Check beside that interpreter without importing or
    # executing anything from the project.
    executable_dir = Path(sys.executable).resolve().parent
    candidates = [executable_dir / binary]
    if os.name == "nt":
        candidates.append(executable_dir / f"{binary}.exe")
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]

    if provider == "ruff":
        return _module_command("ruff")
    return None


def _command_for(provider: str, executable: List[str], paths: List[Path]) -> List[str]:
    rendered = [str(path) for path in paths]
    if provider == "ruff":
        return executable + ["check", "--output-format", "concise", "--"] + rendered
    if provider == "pyright":
        return executable + rendered
    if provider == "eslint":
        return executable + ["--format", "stylish", "--"] + rendered
    if provider == "tsc":
        return executable + ["--noEmit", "--pretty", "false"] + rendered
    raise ValueError(f"Unknown diagnostics provider: {provider}")


def _run_provider_sync(
    provider: str,
    paths: List[Path],
    root: Path,
    env: Optional[Dict[str, str]],
    timeout: float,
) -> Dict[str, Any]:
    executable = _find_executable(provider)
    if not executable:
        return {
            "provider": provider,
            "language": _PROVIDERS[provider]["language"],
            "status": "skipped",
            "detail": f"{provider} is not installed or not on PATH.",
        }

    command = _command_for(provider, executable, paths)
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout)),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or exc.stderr or "").strip()
        return {
            "provider": provider,
            "language": _PROVIDERS[provider]["language"],
            "status": "failed",
            "detail": f"Timed out after {timeout:g}s.",
            "output": output[:_MAX_OUTPUT_CHARS],
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "provider": provider,
            "language": _PROVIDERS[provider]["language"],
            "status": "skipped",
            "detail": f"Could not start {provider}: {exc}",
        }

    output = (completed.stdout or completed.stderr or "").strip()
    result = {
        "provider": provider,
        "language": _PROVIDERS[provider]["language"],
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
    }
    if output:
        result["output"] = output[:_MAX_OUTPUT_CHARS]
    return result


def _providers_for_language(language: str) -> Iterable[str]:
    if language == "python":
        return ("ruff", "pyright")
    if language == "typescript":
        return ("tsc", "eslint")
    if language == "javascript":
        return ("eslint", "tsc")
    return ()


async def run_optional_diagnostics(
    paths: List[Path],
    root: Path,
    provider: str = "auto",
    env: Optional[Dict[str, str]] = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Run available diagnostics for known source-language files."""
    requested = str(provider or "auto").strip().lower()
    if requested == "none":
        return {"status": "skipped", "provider": "none", "results": []}
    if requested not in {"auto", *_PROVIDERS}:
        return {
            "status": "failed",
            "provider": requested,
            "results": [],
            "detail": f"Unknown diagnostics provider '{requested}'.",
        }

    grouped: Dict[str, List[Path]] = {}
    for path in paths:
        language = _language_for(path)
        if language:
            grouped.setdefault(language, []).append(path)

    if not grouped:
        return {
            "status": "skipped",
            "provider": requested,
            "results": [],
            "detail": "No supported source-language files were supplied.",
        }

    results: List[Dict[str, Any]] = []
    for language, language_paths in grouped.items():
        if requested != "auto":
            candidates = (requested,)
        else:
            candidates = tuple(_providers_for_language(language))

        selected = next(
            (candidate for candidate in candidates if candidate in _PROVIDERS), None
        )
        if selected is None or _PROVIDERS[selected]["language"] not in {
            language,
            "javascript" if language == "typescript" else language,
            "typescript" if language == "javascript" else language,
        }:
            results.append(
                {
                    "language": language,
                    "status": "skipped",
                    "detail": f"No diagnostics provider is configured for {language}.",
                }
            )
            continue

        executable = await asyncio.to_thread(_find_executable, selected)
        if not executable:
            if requested != "auto":
                results.append(
                    {
                        "provider": selected,
                        "language": language,
                        "status": "skipped",
                        "detail": f"{selected} is not installed or not on PATH.",
                    }
                )
            else:
                fallback = None
                for candidate in candidates[1:]:
                    if await asyncio.to_thread(_find_executable, candidate):
                        fallback = candidate
                        break
                if fallback:
                    selected = fallback
                else:
                    results.append(
                        {
                            "language": language,
                            "status": "skipped",
                            "detail": "No optional diagnostics provider is installed.",
                        }
                    )
                    continue

        result = await asyncio.to_thread(
            _run_provider_sync,
            selected,
            language_paths,
            root,
            env,
            timeout,
        )
        results.append(result)

    statuses = [str(result.get("status")) for result in results]
    if "failed" in statuses:
        overall = "failed"
    elif "passed" in statuses:
        overall = "passed"
    else:
        overall = "skipped"
    return {"status": overall, "provider": requested, "results": results}
