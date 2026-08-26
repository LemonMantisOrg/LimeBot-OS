"""Runtime-owned paths that should not need to live in the Git checkout.

The project root remains the default state directory for backwards-compatible
persona/config paths. Skills are the exception: newly created or installed
skills live under ``.limebot/skills`` unless ``LIMEBOT_STATE_DIR`` is set.
That gives the updater a clean checkout boundary while the loader continues
to discover the legacy ``skills/`` directory.
"""

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent


def get_state_dir() -> Path:
    """Return the configured mutable-state directory.

    The value is resolved at call time so tests and embedders can change the
    environment before loading configuration without re-importing modules.
    """

    raw = str(os.getenv("LIMEBOT_STATE_DIR") or "").strip()
    if not raw:
        return PROJECT_DIR
    return Path(raw).expanduser().resolve()


def get_config_file() -> Path:
    return get_state_dir() / "limebot.json"


def get_env_file() -> Path:
    return get_state_dir() / ".env"


def get_allowed_paths_file() -> Path:
    return get_state_dir() / "allowed_paths.txt"


def get_data_dir() -> Path:
    return get_state_dir() / "data"


def get_skills_dir() -> Path:
    """Return the writable directory for newly created/installed skills."""

    return get_local_skills_dir()


def get_local_skills_dir() -> Path:
    """Return the user-owned skill directory.

    ``LIMEBOT_STATE_DIR`` explicitly opts the whole runtime into an external
    state directory. Without it, keep state-compatible files in the project
    root but put mutable skills in the ignored ``.limebot`` namespace.
    """

    if str(os.getenv("LIMEBOT_STATE_DIR") or "").strip():
        return get_state_dir() / "skills"
    return PROJECT_DIR / ".limebot" / "skills"


def get_plugins_dir() -> Path:
    return get_state_dir() / "plugins"


def get_plugin_skill_dirs() -> list[Path]:
    """Return skills/ directories from installed Cursor plugins."""

    plugins_dir = get_plugins_dir()
    if not plugins_dir.is_dir():
        return []
    found: list[Path] = []
    for plugin in sorted(plugins_dir.iterdir()):
        skills = plugin / "skills"
        if skills.is_dir():
            found.append(skills)
    return found


def get_skill_dirs() -> list[Path]:
    """Return shipped skills, user-owned skills, and installed plugin skills."""

    project_skills = PROJECT_DIR / "skills"
    state_skills = get_local_skills_dir()
    dirs = [project_skills]
    if state_skills != project_skills:
        dirs.append(state_skills)
    dirs.extend(get_plugin_skill_dirs())
    return dirs


def ensure_state_dir() -> Path:
    state_dir = get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir
