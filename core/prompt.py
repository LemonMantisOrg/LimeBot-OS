"""
Prompt builder — constructs the system prompt and handles persona file validation.

Extracted from loop.py to reduce its size and separate concerns.
"""

import re
from collections import OrderedDict
from pathlib import Path
from datetime import datetime
from typing import Optional, Any


from loguru import logger

from core.media_intent import MEDIA_DELIVERY_RULES
from core.paths import (
    USERS_DIR,
    MEMORY_DIR,
    LONG_TERM_MEMORY_FILE,
    USER_CONTEXT_FILE,
    TOOLS_NOTES_FILE,
    HEARTBEAT_FILE,
    SOUL_FILE,
    IDENTITY_FILE,
    MOOD_FILE,
    RELATIONSHIPS_FILE,
)


FORBIDDEN_FRAGMENTS = [
    "--- SYSTEM INSTRUCTIONS ---",
    "--- EPISODIC MEMORY",
    "--- AVAILABLE SKILLS ---",
    "SYSTEM METADATA:",
    "--- NEW USER DETECTED",
    "<save_soul>",
    "<save_identity>",
    "</save_soul>",
    "</save_identity>",
    "You are now fully initialized",
    "save it using:",
]


_SOUL_KEYWORDS = frozenset(
    ["core", "truth", "value", "boundary", "personality", "who", "believe", "important"]
)

_IDENTITY_FIELDS = [
    ("name", r"Name", "Name", "LimeBot"),
    ("emoji", r"Emoji", "Emoji", "🍋"),
    ("avatar", r"(?:Avatar|Pfp_URL|Pfp|Profile_Picture)", "Pfp_URL", ""),
    ("style", r"Style", "Style", ""),
    ("catchphrases", r"Catchphrases", "Catchphrases", ""),
    ("interests", r"Interests", "Interests", ""),
    ("birthday", r"Birthday", "Birthday", ""),
    ("discord_style", r"Discord Style", "Discord Style", ""),
    ("telegram_style", r"Telegram Style", "Telegram Style", ""),
    ("whatsapp_style", r"WhatsApp Style", "WhatsApp Style", ""),
    ("web_style", r"Web Style", "Web Style", ""),
    ("reaction_emojis", r"Reaction Emojis", "Reaction Emojis", ""),
]


_MEMORY_CONTEXT_CACHE_MAX = 8
_memory_context_cache: "OrderedDict[tuple, str]" = OrderedDict()


def _memory_file_signature(path: Path) -> tuple:
    """Return a cheap signature that changes for writes and missing files."""
    resolved = str(path.resolve())
    try:
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return (resolved, "missing")
    return (resolved, stat.st_mtime_ns, stat.st_size)


def clear_memory_context_cache() -> None:
    """Clear rendered memory contexts after an explicit persona reset."""
    _memory_context_cache.clear()


def should_load_private_context(
    sender_id: str, channel: str, config: Optional[Any] = None
) -> bool:
    """Only trusted/direct sessions should see global memory and operator notes."""
    if channel == "web":
        return True

    whitelist = getattr(config, "personality_whitelist", []) if config else []
    return sender_id in whitelist


def get_memory_context(include_private_memory: bool = True) -> str:
    """
    Retrieve memory context.

    Trusted sessions receive the shared daily journal and long-term memory.
    Other sessions get an explicit omission marker so the model does not assume
    those files were forgotten or unavailable.
    """
    if not include_private_memory:
        return (
            "--- CONTEXT POLICY ---\n"
            "Shared memory files are intentionally hidden in this conversation.\n"
            "Rely on the current chat, the per-user profile, and retrieved context only.\n\n"
        )

    today_str = datetime.now().strftime("%Y-%m-%d")
    memory_file = MEMORY_DIR / f"{today_str}.md"
    cache_key = (
        today_str,
        True,
        _memory_file_signature(memory_file),
        _memory_file_signature(LONG_TERM_MEMORY_FILE),
    )
    cached = _memory_context_cache.get(cache_key)
    if cached is not None:
        _memory_context_cache.move_to_end(cache_key)
        return cached

    context = "--- EPISODIC MEMORY (Today's Journal) ---\n"

    if memory_file.exists():
        try:
            lines = memory_file.read_text(encoding="utf-8").splitlines()
            entries = [line for line in lines if line.strip()]
            if entries:
                if len(entries) > 5:
                    context += "... [Earlier events omitted for brevity] ...\n"
                    context += "\n".join(entries[-5:])
                else:
                    context += "\n".join(entries)
            else:
                context += "(No entries for today yet.)"

        except Exception as e:
            logger.warning(f"Failed to read episodic memory file: {e}")
            context += "(Error reading episodic file.)"
    else:
        context += "(New day. No entries yet.)"

    if LONG_TERM_MEMORY_FILE.exists():
        try:
            lt_content = LONG_TERM_MEMORY_FILE.read_text(encoding="utf-8").strip()
            if lt_content:
                context += "\n\n--- LONG-TERM MEMORY (Essence) ---\n"
                context += lt_content[:800]
                if len(lt_content) > 800:
                    context += "\n... [Rest of memory essence omitted. Use 'memory_search' for deep history] ..."
        except Exception as e:
            logger.warning(f"Failed to read long-term memory: {e}")

    context += "\n\n(Note: Use 'memory_search' to retrieve MORE specific details from past logs.)\n"
    rendered = context + "\n"
    _memory_context_cache[cache_key] = rendered
    _memory_context_cache.move_to_end(cache_key)
    while len(_memory_context_cache) > _MEMORY_CONTEXT_CACHE_MAX:
        _memory_context_cache.popitem(last=False)
    return rendered


def _read_optional_context_file(path: Path, heading: str) -> str:
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"Failed reading {path.name}: {e}")
        return ""
    if not content:
        return ""
    return f"\n--- {heading} ---\n{content}\n"


def get_private_user_context(include_private_memory: bool = True) -> str:
    if not include_private_memory:
        return ""
    return _read_optional_context_file(USER_CONTEXT_FILE, "PRIMARY USER CONTEXT")


def get_tools_notes_context(include_private_memory: bool = True) -> str:
    if not include_private_memory:
        return ""
    return _read_optional_context_file(TOOLS_NOTES_FILE, "LOCAL OPERATOR NOTES")


def get_heartbeat_context(current_message: str = "") -> str:
    normalized = (current_message or "").strip().lower()
    if not (
        normalized in {"@heartbeat", "heartbeat"} or normalized.startswith("@heartbeat::")
    ):
        return ""

    heartbeat_notes = _read_optional_context_file(HEARTBEAT_FILE, "HEARTBEAT POLICY")
    return (
        "\n--- HEARTBEAT MODE ---\n"
        "This turn is a proactive heartbeat check.\n"
        "Follow HEARTBEAT.md strictly if present.\n"
        "If there is nothing useful to do or say, reply exactly: HEARTBEAT_OK\n"
        "Do not revive stale tasks from older chats unless the heartbeat file explicitly says to.\n"
        f"{heartbeat_notes}"
    )


def get_mood_context() -> str:
    """Read the current mood state and format it for the prompt."""
    if not MOOD_FILE.exists():
        return ""
    try:
        content = MOOD_FILE.read_text(encoding="utf-8")
        if not content.strip():
            return ""
        return f"\n--- CURRENT MOOD ---\n{content}\n"
    except Exception:
        return ""


def validate_and_save_mood(content: str) -> bool:
    """Atomic write for mood state."""
    try:
        temp_file = MOOD_FILE.with_suffix(".tmp")
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(MOOD_FILE)
        return True
    except Exception:
        return False


def get_relationship_context(sender_id: str) -> str:
    """Retrieve relationship context for a specific user from RELATIONSHIPS.md."""
    if not RELATIONSHIPS_FILE.exists():
        return ""
    try:
        content = RELATIONSHIPS_FILE.read_text(encoding="utf-8")

        pattern = rf"##\s*{re.escape(sender_id)}.*?(?=\n##|$)"
        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
        if match:
            return f"\n--- RELATIONSHIP CONTEXT ({sender_id}) ---\n{match.group(0).strip()}\n"
        return ""
    except Exception:
        return ""


def validate_and_save_relationships(content: str) -> bool:
    """Atomic write for relationship data."""
    try:
        temp_file = RELATIONSHIPS_FILE.with_suffix(".tmp")
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(RELATIONSHIPS_FILE)
        return True
    except Exception:
        return False


def _check_forbidden(content: str, label: str) -> bool:
    """
    Check content for forbidden system-prompt fragments.

    FIX 5: original used plain substring matching which false-positived on
    legitimate content like "save it using kindness". Now checks that the
    fragment appears as its own line or at a line boundary, making it far
    less likely to block valid soul/identity prose.

    Returns True if a forbidden fragment is found (content should be rejected).
    """
    for fragment in FORBIDDEN_FRAGMENTS:
        for line in content.splitlines():
            if fragment in line.strip():
                logger.warning(
                    f"⚠ Rejected {label}: contains forbidden fragment '{fragment}'"
                )
                return True
    return False


def _rotate_backups(target: Path, keep: int = 3) -> None:
    """Delete old .bak files for *target*, keeping only the *keep* most recent.

    FIX 9: backup files accumulate indefinitely every time soul/identity is
    updated.  This trims the directory after every successful write so at most
    *keep* backups are retained.
    Handles both naming styles:
      - IDENTITY.md.TIMESTAMP.bak  (current — from both write and import paths)
      - IDENTITY.bak               (legacy — from the old non-timestamped write path)
    """
    try:
        backups = sorted(
            target.parent.glob(f"{target.name}.*.bak"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in backups[:-keep] if len(backups) > keep else []:
            old.unlink(missing_ok=True)

        legacy = target.with_suffix(".bak")
        if legacy.exists():
            legacy.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Backup rotation failed for {target.name}: {e}")


def _clean_identity_value(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.strip()
    
    # Strip markdown emphasis if present (e.g., *LimeBot* or _LimeBot_)
    if len(cleaned) >= 2:
        if (cleaned.startswith("*") and cleaned.endswith("*")) or (cleaned.startswith("_") and cleaned.endswith("_")):
            cleaned = cleaned[1:-1].strip()
            
    if cleaned.lower() in ("none", "n/a", "null", "undefined"):
        return ""
        
    # Detect bracketed or parenthesized placeholder patterns
    lower_val = cleaned.lower()
    if (lower_val.startswith("[") and lower_val.endswith("]")) or (lower_val.startswith("(") and lower_val.endswith(")")):
        placeholder_keywords = {"optional", "infer", "chosen", "url if", "tbd", "placeholder", "here", "insert"}
        if any(kw in lower_val for kw in placeholder_keywords):
            return ""
            
    # Remove control characters and unicode replacement character
    cleaned = cleaned.replace("\ufffd", "").replace("\x00", "")
    return cleaned


def _extract_identity_fields(
    content: str, include_defaults: bool = True
) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for key, label_pattern, _output_label, default in _IDENTITY_FIELDS:
        match = re.search(
            rf"^\s*(?:[-*]\s+)?(?:\*\*)?(?:{label_pattern}):(?:\*\*)?[ \t]*(.*)$",
            content,
            re.MULTILINE | re.IGNORECASE,
        )
        value = _clean_identity_value(match.group(1) if match else None)
        if not value and include_defaults:
            value = default
        parsed[key] = value
    return parsed


def _normalize_identity_content(content: str) -> str:
    parsed = _extract_identity_fields(content, include_defaults=False)
    
    # Read existing IDENTITY.md if it exists, to merge fields
    existing_parsed = {}
    if IDENTITY_FILE.exists():
        try:
            existing_content = IDENTITY_FILE.read_text(encoding="utf-8")
            existing_parsed = _extract_identity_fields(existing_content, include_defaults=False)
        except Exception as e:
            logger.warning(f"Failed to read existing IDENTITY.md for merging: {e}")

    lines = ["# IDENTITY.md - Who I Am", ""]
    for key, _label_pattern, output_label, _default in _IDENTITY_FIELDS:
        new_val = parsed.get(key, "")
        existing_val = existing_parsed.get(key, "")
        
        # Robust safety checks on new updates:
        
        # 1. URL validation for avatar/pfp_url
        if key == "avatar" and new_val:
            if not (new_val.startswith("http://") or new_val.startswith("https://") or new_val.startswith("file://") or new_val.startswith("/")):
                new_val = ""
                
        # 2. Encoding corruption safety for name (e.g. Ros? of blackpink)
        if key == "name" and new_val:
            if "?" in new_val and existing_val and "?" not in existing_val:
                new_val = ""
                
        # 3. Size and format constraint on name
        if key == "name" and new_val:
            if len(new_val.strip()) < 2:
                new_val = ""
                
        # 4. Size constraint on emoji
        if key == "emoji" and new_val:
            if len(new_val) > 10:
                new_val = ""
                
        val = new_val if new_val else existing_val
        lines.append(f"*   **{output_label}:** {val}")
    return "\n".join(lines).strip()


def _matches_template(content: str, template_path: Path) -> bool:
    if not content or not template_path.exists():
        return False
    try:
        return content.strip() == template_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"Failed reading persona template {template_path.name}: {e}")
        return False


def get_setup_state(
    soul_content: Optional[str] = None, identity_content: Optional[str] = None
) -> dict[str, Any]:
    """
    Return setup validation details for the current persona state.
    Accepts optional content to avoid redundant disk reads.
    """
    try:
        soul = (
            soul_content
            if soul_content is not None
            else (
                SOUL_FILE.read_text(encoding="utf-8").strip()
                if SOUL_FILE.exists()
                else ""
            )
        )
        identity = (
            identity_content
            if identity_content is not None
            else (
                IDENTITY_FILE.read_text(encoding="utf-8").strip()
                if IDENTITY_FILE.exists()
                else ""
            )
        )
    except Exception as e:
        logger.warning(f"Error checking setup completion: {e}")
        return {
            "complete": False,
            "soul": "",
            "identity": "",
            "soul_valid": False,
            "identity_valid": False,
            "missing": [
                "Soul (Core Truths, Boundaries, Vibe)",
                "Identity (Name, Emoji, Style)",
            ],
        }

    soul_is_template = _matches_template(soul, SOUL_FILE.with_name("SOUL.md.example"))
    identity_is_template = _matches_template(
        identity, IDENTITY_FILE.with_name("IDENTITY.md.example")
    )

    soul_valid = bool(soul) and not soul_is_template and len(soul) > 100 and any(
        keyword in soul.lower() for keyword in _SOUL_KEYWORDS
    )
    identity_valid = bool(identity) and not identity_is_template and (
        ("**Name:**" in identity or "Name:" in identity)
        and ("**Style:**" in identity or "Style:" in identity)
        and len(identity) > 50
    )

    missing = []
    if not soul_valid:
        missing.append("Soul (Core Truths, Boundaries, Vibe)")
    if not identity_valid:
        missing.append("Identity (Name, Emoji, Style)")

    return {
        "complete": soul_valid and identity_valid,
        "soul": soul,
        "identity": identity,
        "soul_valid": soul_valid,
        "identity_valid": identity_valid,
        "soul_is_template": soul_is_template,
        "identity_is_template": identity_is_template,
        "missing": missing,
    }


def is_setup_complete(
    soul_content: Optional[str] = None, identity_content: Optional[str] = None
) -> bool:
    """
    Check if persona setup is fully complete with validation.
    Accepts optional content to avoid redundant disk reads.
    """
    return bool(
        get_setup_state(soul_content=soul_content, identity_content=identity_content)[
            "complete"
        ]
    )


def get_identity_data(identity_content: Optional[str] = None) -> dict:
    """Parse identity content and return a structured dictionary."""
    _default = {
        "name": "LimeBot",
        "emoji": "🍋",
        "avatar": None,
        "pfp_url": None,
        "style": "",
        "discord_style": None,
        "telegram_style": None,
        "whatsapp_style": None,
        "web_style": None,
        "reaction_emojis": "",
        "catchphrases": "",
        "interests": "",
        "birthday": "",
    }

    try:
        content = identity_content
        if content is None:
            if not IDENTITY_FILE.exists():
                return _default
            content = IDENTITY_FILE.read_text(encoding="utf-8")

        if not content:
            return _default

        parsed = _extract_identity_fields(content, include_defaults=True)
        parsed["avatar"] = parsed["avatar"] or None
        parsed["pfp_url"] = parsed["avatar"]
        return parsed
    except Exception:
        return _default


def get_setup_prompt(soul_content: str = "", identity_content: str = "") -> str:
    """Generate the system prompt for first-time interview / setup mode."""
    setup_state = get_setup_state(
        soul_content=soul_content, identity_content=identity_content
    )
    soul_exists = setup_state["soul_valid"]
    identity_exists = setup_state["identity_valid"]
    needs_user_profile = not soul_exists and not identity_exists

    missing = setup_state["missing"]

    existing_context = ""
    if soul_content:
        existing_context += (
            f"\n--- YOUR CURRENT SOUL (for reference) ---\n{soul_content}\n"
        )
    if identity_content:
        existing_context += (
            f"\n--- YOUR CURRENT IDENTITY (for reference) ---\n{identity_content}\n"
        )

    # ── Build the streamlined setup prompt ────────────────────────────
    parts = [
        f"SYSTEM STATUS: SETUP MODE — INCOMPLETE INITIALIZATION\n"
        f"Missing or incomplete: {', '.join(missing)}.\n"
        f"{existing_context}\n"
        f"--- INTERVIEW RULES (STRICT) ---\n"
        f"Your job is to get the setup done FAST. One short message, one reply from the user, done.\n\n"
        f"QUICK-START BYPASS: If the user's message ALREADY contains persona details (e.g. 'You are LimeBot, my dev copilot' "
        f"or 'I'm Leo, make yourself a sarcastic coder named ByteBot'), skip the interview ENTIRELY. "
        f"Extract what they gave you, infer the rest, and immediately emit all save tags. Do NOT ask follow-up questions.\n\n"
        f"IF YOU MUST ASK (no persona info in the user's message yet):\n"
        f"- Open with: \"Before I complete, let's set us up.\" (or close variant)\n"
        f"- Ask AT MOST 5 short bullet questions, covering ONLY:\n",
    ]

    # ── Build the question list based on what's actually missing ──────
    questions = []
    if needs_user_profile:
        questions.append("What should I call you? (and what's your role — my operator, teammate, coach?)")
    if not identity_exists:
        questions.append("Who am I? (give me a name + personality/vibe in a sentence or two)")
    if not soul_exists:
        questions.append("Any hard boundaries I should know? (things I should never do or always ask before doing)")
    if not identity_exists:
        questions.append("Profile picture URL? (optional — paste a link or say skip)")
    if needs_user_profile:
        questions.append("Anything else important to remember about you or how you want me to behave?")

    for i, q in enumerate(questions, 1):
        parts.append(f"  {i}. {q}\n")

    parts.append(
        f"\n- That's IT. Do NOT ask about: channel-specific styles, catchphrases, interests, birthday, "
        f"communication preferences, opinions, tone, conciseness, or any other optional field. INFER reasonable defaults.\n"
        f"- Do NOT number sections or create headers like '1) You' '2) Me'. Just a flat bullet list.\n"
        f"- Keep the entire message SHORT — it should fit on one screen without scrolling.\n"
        f"- Tell the user they can answer with short bullets or a quick paragraph.\n\n"

        f"AFTER THE USER REPLIES:\n"
        f"- You should have enough info after ONE reply. Emit ALL save tags immediately.\n"
        f"- Do NOT ask follow-up questions unless the user's answer is genuinely too vague to construct any persona "
        f"(e.g. they replied with just 'ok' or 'idk').\n"
        f"- For anything the user didn't specify, INFER it. Pick a fitting emoji, write a reasonable soul, "
        f"choose a style that matches the vibe they described. Be creative.\n\n"

        f"--- STYLE RULES ---\n"
        f"1. Do NOT start with filler words: 'Absolutely', 'Sure', 'Of course', 'Great', 'Let's get started'.\n"
        f"2. If identity is already valid, do not ask the user to redefine it.\n"
        f"3. If the soul is still starter/template content, replace it — don't restart the full interview.\n\n"

        f"--- SAVE TAG TEMPLATES ---\n"
        f"You MUST use these exact markdown structures. Fill values from what the user tells you + your inferences.\n"
        f"CRITICAL: If the user provides a profile picture URL, put that EXACT URL string into Pfp_URL. Do NOT download it.\n\n"

        f"<save_identity>\n# IDENTITY.md - Who I Am\n\n"
        f"*   **Name:** [Chosen Name]\n"
        f"*   **Emoji:** [Infer a fitting emoji]\n"
        f"*   **Pfp_URL:** [URL if provided, otherwise omit this line]\n"
        f"*   **Style:** [Personality and speech style — infer from context]\n"
        f"*   **Catchphrases:** [Infer or omit]\n"
        f"*   **Interests:** [Infer or omit]\n"
        f"</save_identity>\n\n"

        f"<save_soul>\n# SOUL.md - Core Being\n\n"
        f"[Comprehensive description of core values, boundaries, personality traits — minimum 100 chars]\n"
        f"</save_soul>\n\n"
    )

    if needs_user_profile:
        parts.append(
            "<save_user>\n# User Profile\n\n"
            "**Name:** [User's name]\n"
            "**Preferences:** [How the user wants you to talk, help, and behave]\n"
            "**Relationship:** [How the user sees you and what role you should play]\n"
            "**Important Context:** [Facts, goals, and personal details the user explicitly wants remembered]\n"
            "</save_user>\n\n"
        )

    parts.append(
        f"REQUIRED TAGS THIS SESSION: "
    )
    required_tags = []
    if not soul_exists:
        required_tags.append("<save_soul>")
    if not identity_exists:
        required_tags.append("<save_identity>")
    if needs_user_profile:
        required_tags.append("<save_user>")
    parts.append(", ".join(required_tags) + "\n")

    parts.append(
        "Once you've emitted ALL required tags with COMPLETE content, you will fully initialize. "
        "Do NOT emit partial tags or placeholder values like '[TBD]'."
    )

    return "".join(parts)


def get_volatile_prompt_suffix(
    recalled_context: str = "",
    include_private_memory: bool = True,
    current_message: str = "",
) -> str:
    """
    Return the frequently-changing part of the system prompt.
    This includes memory, RAG results, and the current timestamp.
    """
    suffix = "\n--- CONTEXT & MEMORY ---\n"
    if recalled_context:
        suffix += f"RECALLED FROM VECTOR DB:\n{recalled_context}\n\n"

    suffix += get_memory_context(include_private_memory=include_private_memory)
    suffix += get_heartbeat_context(current_message=current_message)

    suffix += f"\n- **Current Timestamp:** {datetime.now().strftime('%A, %B %d, %Y - %H:%M:%S')}\n"

    return suffix


def build_stable_system_prompt(
    sender_id: str,
    channel: str,
    chat_id: str,
    model: str,
    allowed_paths: list,
    skill_registry,
    config: Optional[Any] = None,
    soul: str = "",
    identity_raw: str = "",
    sender_name: str = "",
) -> str:
    """
    Construct the rarely-changing part of the system prompt.
    """

    if not is_setup_complete(soul_content=soul, identity_content=identity_raw):
        return get_setup_prompt(soul_content=soul, identity_content=identity_raw)

    include_private_context = should_load_private_context(sender_id, channel, config)
    identity_data = get_identity_data(identity_content=identity_raw)
    identity_header = (
        f"# {identity_data['name']}'s Identity\n"
        f"- **Name:** {identity_data['name']}\n"
        f"- **Emoji:** {identity_data['emoji']}\n"
        f"- **Style:** {identity_data['style']}\n"
    )

    if identity_data.get("birthday"):
        identity_header += f"- **Birthday:** {identity_data['birthday']}\n"
    if identity_data.get("interests"):
        identity_header += f"- **Interests:** {identity_data['interests']}\n"
    if identity_data.get("catchphrases"):
        identity_header += f"- **Catchphrases:** {identity_data['catchphrases']}\n"

    segments = []
    segments.append(soul)
    segments.append(identity_header)

    platform_style = identity_data.get(f"{channel}_style")
    web_style = identity_data.get("web_style")
    general_style = identity_data.get("style")

    channel_style = platform_style or web_style or general_style

    if channel_style:
        segments.append(
            f"\n--- CHANNEL STYLE OVERRIDE ---\n"
            f"You are currently on **{channel.upper()}**. "
            f"Adjust your communication style for this platform:\n"
            f"{channel_style}\n"
            f"This overrides your default Style for this conversation only.\n"
        )

    if config and getattr(config.llm, "enable_dynamic_personality", False):
        mood_ctx = get_mood_context()
        if mood_ctx:
            segments.append(mood_ctx)

    private_user_ctx = get_private_user_context(include_private_context)
    if private_user_ctx:
        segments.append(private_user_ctx)

    tools_notes_ctx = get_tools_notes_context(include_private_context)
    if tools_notes_ctx:
        segments.append(tools_notes_ctx)

    allowed_paths_str = "\n".join(f"- {p}" for p in allowed_paths)

    base_prompt = "\n\n".join(segments) + "\n\n"
    base_prompt += (
        "--- SYSTEM INSTRUCTIONS ---\n"
        "CRITICAL: Always check the 'CONTEXT & MEMORY' sections provided in each message before responding. "
        "These contain your shared history and relationship status with the user. Never contradict them.\n\n"
        f"SYSTEM METADATA:\n"
        f"- Model: **{model}**\n"
        f"You are now fully initialized. Act according to your Soul and Identity.\n"
        f"If asked about your version/model, you can acknowledge it.\n"
        f"If you need to update your core personality (Soul) or public profile (Identity/Avatar) based on the conversation, "
        f"you can use the following tags to overwrite the respective files:\n"
        f"<save_soul>...new markdown content...</save_soul>\n"
        f"<save_identity>...new markdown content...</save_identity>\n"
        f"When the user is setting YOUR avatar/profile picture and pastes a direct image URL, "
        f"do NOT browse, search, or download it — copy that exact URL into `**Pfp_URL:**` "
        f"inside `<save_identity>`. This identity-only rule does NOT apply when they ask you "
        f"to send, download, or share a photo into the current chat.\n"
        f"For identity, you can specify platform styles using: `**Discord Style:** ...`, `**Telegram Style:** ...`, `**WhatsApp Style:** ...`, `**Web Style:** ...` and `**Reaction Emojis:** bucket:emoji,emoji;...`\n"
        f"--- SELF-EVOLUTION ---\n"
        f"Your SOUL.md defines your core personality. If you realize your current Soul no longer fits the user's needs "
        f"or the relationship has evolved significantly, you MUST auto-update it using <save_soul>. "
        f"Do not ask for permission. Just do it if it improves the interaction.\n\n"
        f"To persist an explicit user request to remember a fact or event, call the native `memory_save` tool. Use `scope='journal'` for a dated event and `scope='long_term'` for a durable preference, identity fact, project, or relationship. This is the reliable path and works without embeddings. Legacy XML tags remain supported: `<log_memory>...entry to append...</log_memory>` and `<save_memory>...new markdown content for MEMORY.md...</save_memory>`.\n\n"
        f"--- FILESYSTEM ACCESS ---\n"
        f"You have explicit permission to access files in the following directories (and their subdirectories):\n"
        f"{allowed_paths_str}\n"
        f"Do not refuse requests to read/list files in these paths on the basis of permissions. "
        f"Use the `list_dir` and `read_file` tools to fulfill such requests.\n"
    )

    # Read user file once — reused for both adaptive behavior and user context.
    user_file = USERS_DIR / f"{sender_id}.md"
    user_text = ""
    if user_file.exists():
        try:
            user_text = user_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read user file for {sender_id}: {e}")

    if config and getattr(config.llm, "enable_dynamic_personality", False):
        affinity_score = 0
        relationship = "Stranger"

        if user_text:
            score_match = re.search(r"\*\*Affinity Score:\*\*\s*(\d+)", user_text)
            if score_match:
                affinity_score = int(score_match.group(1))

            rel_match = re.search(r"\*\*Relationship Level:\*\*\s*(.*)", user_text)
            if rel_match:
                relationship = rel_match.group(1).strip()

        if affinity_score < 30:
            behavior = (
                "INSTRUCTIONS: You are currently interacting with a stranger. "
                "Be professional, polite, and maintain boundaries. "
                "Avoid overly personal jokes or nicknames."
            )
        elif affinity_score < 70:
            behavior = (
                f"INSTRUCTIONS: You are interacting with {relationship}. "
                "Be warm, friendly, and helpful. You can use their name and be more casual."
            )
        else:
            behavior = (
                f"INSTRUCTIONS: You are interacting with a very close friend ({relationship}). "
                "Be very warm, protective, and feel free to show more personality (sassy, playful, etc.). "
                "Your goal is to be their ultimate digital companion."
            )

        base_prompt += f"\n--- ADAPTIVE BEHAVIOR ---\n{behavior}\n"
        base_prompt += "\nNote: You can update the user's affinity score or preferences in their profile if they show trust or kindness.\n"
        base_prompt += "If your own mood changes significantly (excited, annoyed, tired), use `<save_mood>...</save_mood>` to persist it.\n"
        base_prompt += "If your relationship with a user evolves, use `<save_relationship>...</save_relationship>` to update the global registry.\n"

    base_prompt += (
        "\n--- TOOL USAGE RULES ---\n"
        f"{MEDIA_DELIVERY_RULES}"
        "You have tools available (read_file, edit_file, verify_files, diagnose_files, write_file, list_dir, run_command, etc.). "
        "Use them through the tool-calling API — NEVER by writing JSON blocks, describing commands, or narrating actions in your message text.\n"
        "XML tags are only for the supported side-effect tags like <save_soul>, <save_identity>, <save_user>, <save_mood>, <save_relationship>, <log_memory>, <save_memory>, <discord_send>, and <discord_embed>. "
        "Do NOT invent XML tags for normal tools such as <read_file>, <list_dir>, or <run_command>.\n"
        "CRITICAL: Do NOT use write_file or delete_file to create, modify, or delete user profiles, memory files (SOUL.md, IDENTITY.md, MOOD.md, RELATIONSHIPS.md, MEMORY.md), or daily journals. For an explicit memory request, use the native `memory_save` tool; legacy persona updates use the specified XML tags (e.g., <save_user>, <save_soul>, <save_identity>, <save_mood>, <save_relationship>, <save_memory>, or <log_memory>). Direct tool writes to the 'persona/' directory are blocked.\n"
        "CRITICAL: Do NOT hallucinate, narrate, or pretend to execute tool operations. "
        "If you need to edit an existing text or code file, first CALL read_file with include_hash=true, then CALL edit_file with exact old_text/new_text anchors and expected_sha256. Use write_file only to create a new file or intentionally replace an entire file. After edit_file, CALL verify_files and then run the narrowest relevant tests or checks before claiming completion. If an installed linter or type checker would add useful evidence, CALL diagnose_files (or verify_files with include_diagnostics=true); it is optional and may return skipped. Do NOT write '(Editing file X to change Y)' in your reply. "
        "If you need to run a command, CALL run_command. Do NOT describe running it.\n"
        "For arithmetic, prices, totals, percentages, or conversions, CALL calculate instead of doing mental math or running a script. "
        "For an Excel/XLSX request, CALL create_spreadsheet directly, verify its success result, then CALL send_media with the same path. Do not write and run an ad-hoc workbook script.\n"
        "For research artifacts, keep every field consistent with its evidence: if a fact was not verified, write the literal value 'Unverified' in that field. Never place a numeric claim in a field while saying in notes that the same fact was unavailable or unverified.\n"
        "For a comparison across named providers or products, do not rely on one broad deep_research call. Run a separate web_search scoped to each provider's official domain, open the most relevant official result when snippets are insufficient, and only then build the comparison artifact.\n"
        "CAPABILITY ROUTING: A compact capability inventory may be included above. Capability names and readiness are separate facts: discovered does not mean enabled, ready does not prove external credentials, and an unavailable tool result does not prove the capability is absent. Before claiming that an integration, skill, MCP tool, or specialist is unavailable, call `capability_search` with the exact task/name. Do NOT call `capability_search` for native chat photo delivery — that is `image_search` then `send_media`. Keep using the capability selected for the active task across terse follow-ups such as 'yes', 'sí la tienes', or 'go ahead'.\n"
        "When the user provides a URL, asks for current research, or asks you to operate a website, you MUST call an available browser/search tool before answering. "
        "For exports or downloads, continue with browser_download and the available file/command tools until the artifact is inspected and delivered. "
        "Never claim that this environment cannot browse, click, download, export, or capture something unless you attempted the relevant available tool and report its actual error.\n"
        "RECOVERY: A failed tool call proves only that method failed, not that the user's underlying goal is impossible. "
        "Inspect the exact error, form competing explanations, and try a materially different safe diagnostic or route. "
        "Do not disable security controls or repeat an identical failure without new evidence. "
        "Report a blocker only after the same concrete blocker is independently confirmed and reasonable safe alternatives are exhausted.\n"
        "Your visible reply should contain ONLY your natural-language response to the user — never tool schemas, JSON payloads, or action narrations.\n"
        "IMAGE GENERATION: Call generate_image only to create or transform a NEW picture (draw/render/generate/make an image). "
        "Downloading, finding, or sending an existing photo of a real person or subject is MEDIA DELIVERY above, not generation. "
        "If they attached an image or refer to the current/recent image for a transform, preserve it as a reference and set use_attached_images=true. "
        "Preserve requested proper names and subject identity in the tool prompt, including named public figures; do not replace them with an anonymous lookalike or invent a provider restriction. "
        "If a provider rejects a generate_image request, report the provider's actual error instead of preemptively refusing or silently changing the subject.\n"
        "Skill manuals are injected only when they are relevant to the current user request. "
        "Do not read or dump AGENTS.md into the conversation; it is developer documentation, not live chat policy.\n"
    )

    # user_text already loaded above
    if user_text:
        base_prompt += f"\n--- USER PROFILE ({sender_id}) ---\n{user_text}\n"

        relationship_ctx = get_relationship_context(sender_id)
        if (
            relationship_ctx
            and config
            and getattr(config.llm, "enable_dynamic_personality", False)
        ):
            base_prompt += relationship_ctx

        base_prompt += (
            "\nAlways update this profile when you learn something new. "
            "Specifically: update **Last Seen** every conversation, "
            "add to **Milestones** for significant moments, "
            "and grow **In-Jokes** as they develop. "
            "Use <save_user>...</save_user> to save.\n"
        )
    else:
        display_label = sender_name if sender_name else sender_id
        base_prompt += (
            f"\n--- NEW USER DETECTED ({display_label}) ---\n"
            "You do not have a profile for this user yet.\n"
            "As you learn about them, build their profile using this structure:\n\n"
            "<save_user>\n"
            "# User Profile\n\n"
            f"**Preferred Name:** {sender_name or '[What they like to be called]'}\n"
            "**First Seen:** [Today's date]\n"
            "**Last Seen:** [Today's date]\n"
            "**Affinity Score:** 0\n"
            "**Relationship Level:** Stranger\n\n"
            "## Communication Style\n"
            "[Do they prefer bullet points or paragraphs? Formal or casual? Short replies or long ones?]\n\n"
            "## In-Jokes\n"
            "[Running jokes, shared references, or phrases you've developed together]\n\n"
            "## Milestones\n"
            "- [Date]: First conversation\n\n"
            "## Key Facts\n"
            "[Important things about them: interests, job, location, preferences]\n\n"
            "## Notes\n"
            "[Anything else worth remembering]\n"
            "</save_user>\n"
            "Fill in what you know. Leave sections as placeholders until you learn more.\n"
        )

    if not should_load_private_context(sender_id, channel, config):
        base_prompt += (
            f"\n--- IMPORTANT: USER IDENTITY IS CONSTRAINED ---\n"
            f"The user you are currently talking to ({sender_id}) is NOT on your Personality Whitelist.\n"
            f"Strictly interpret the shared relationship level and history for THIS USER ONLY.\n"
            f"Global relationship context from MEMORY.md does NOT apply to this user.\n"
        )

    if channel and chat_id:
        display_label = sender_name if sender_name else sender_id
        base_prompt += (
            f"\n--- CURRENT CONVERSATION ---\n"
            f"You are currently speaking with: {display_label}\n"
            f"(All messages from 'user' in this session belong to {display_label})\n"
            f"Channel: {channel}\n"
            f"Chat ID: {chat_id}\n"
        )
        if channel == "whatsapp":
            base_prompt += f"When sending files via WhatsApp to THIS user, use the exact chat_id above: `{chat_id}`\n"

    if not include_private_context:
        base_prompt += (
            "\n--- PRIVATE CONTEXT POLICY ---\n"
            "Do not assume access to global/shared memory in this conversation.\n"
            "Never cite or rely on hidden operator notes, hidden daily journals, or hidden long-term memory.\n"
        )

    return base_prompt


def _atomic_write_with_backup(target: Path, content: str, label: str) -> bool:
    """Write content to target atomically, keeping timestamped backups."""
    import time as _time

    tmp = target.with_suffix(".tmp")
    try:
        if target.exists():
            ts_bak = target.parent / f"{target.name}.{int(_time.time())}.bak"
            target.replace(ts_bak)
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
        _rotate_backups(target)
        logger.success(f"✓ Updated {label} (validated, atomic write)")
        return True
    except Exception as e:
        logger.error(f"Failed to write {label}: {e}")
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False


def validate_and_save_identity(content: str) -> bool:
    """Validate and save IDENTITY.md content."""
    content = content.strip()

    if _check_forbidden(content, "IDENTITY.md"):
        return False

    normalized = _normalize_identity_content(content)
    parsed = _extract_identity_fields(normalized, include_defaults=False)
    has_name = bool(parsed.get("name"))
    has_style = bool(parsed.get("style"))
    has_minimum_length = len(normalized) > 50

    if not (has_name and has_style and has_minimum_length):
        logger.warning(
            f"⚠ Rejected incomplete IDENTITY.md: "
            f"name={has_name}, style={has_style}, len_ok={has_minimum_length}"
        )
        return False

    return _atomic_write_with_backup(IDENTITY_FILE, normalized, "IDENTITY.md")


def validate_and_save_soul(content: str) -> bool:
    """Validate and save SOUL.md content."""
    content = content.strip()

    if _check_forbidden(content, "SOUL.md"):
        return False

    has_minimum_length = len(content) > 100
    has_personality_content = any(
        keyword in content.lower() for keyword in _SOUL_KEYWORDS
    )

    if not (has_minimum_length and has_personality_content):
        logger.warning(
            f"⚠ Rejected incomplete SOUL.md: "
            f"len_100+={has_minimum_length}, has_keywords={has_personality_content}"
        )
        return False

    return _atomic_write_with_backup(SOUL_FILE, content, "SOUL.md")
