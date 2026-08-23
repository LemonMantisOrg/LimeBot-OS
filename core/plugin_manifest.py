"""Cursor plugin package format: parse plugin.json and marketplace.json.

Official schemas live at https://github.com/cursor/plugins/tree/main/schemas
and are vendored under schemas/cursor-plugin/.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


PLUGIN_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

COMPONENT_DEFAULTS = {
    "skills": "skills",
    "rules": "rules",
    "agents": "agents",
    "commands": "commands",
}


class PluginManifestError(ValueError):
    """Raised when a Cursor plugin package is missing or invalid."""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginManifestError(f"Missing manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PluginManifestError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginManifestError(f"{path} must contain a JSON object")
    return data


def _as_path_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _safe_join(root: Path, rel: str) -> Path:
    cleaned = str(rel or "").strip()
    if not cleaned:
        raise PluginManifestError("Empty component path")
    if Path(cleaned).is_absolute() or cleaned.startswith("~"):
        raise PluginManifestError(f"Absolute paths are not allowed: {rel}")
    target = (root / cleaned).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise PluginManifestError(f"Path escapes plugin root: {rel}") from exc
    return target


def _discover_skill_dirs(base: Path) -> List[Path]:
    if not base.exists():
        return []
    if (base / "SKILL.md").is_file():
        return [base]
    found: List[Path] = []
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file():
                found.append(child)
    return found


def _discover_files(base: Path, suffixes: tuple[str, ...]) -> List[Path]:
    if not base.exists():
        return []
    if base.is_file():
        return [base] if base.suffix.lower() in suffixes else []
    files: List[Path] = []
    for child in sorted(base.rglob("*")):
        if child.is_file() and child.suffix.lower() in suffixes:
            files.append(child)
    return files


@dataclass
class PluginManifest:
    name: str
    root: Path
    raw: Dict[str, Any]
    display_name: str = ""
    description: str = ""
    version: str = ""
    author: Dict[str, Any] = field(default_factory=dict)
    skills: List[Path] = field(default_factory=list)
    rules: List[Path] = field(default_factory=list)
    agents: List[Path] = field(default_factory=list)
    commands: List[Path] = field(default_factory=list)
    mcp_servers: Dict[str, Any] = field(default_factory=dict)
    mcp_config_paths: List[Path] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "version": self.version,
            "root": str(self.root),
            "skills": [str(path) for path in self.skills],
            "rules": [str(path) for path in self.rules],
            "agents": [str(path) for path in self.agents],
            "commands": [str(path) for path in self.commands],
            "mcp_servers": self.mcp_servers,
            "mcp_config_paths": [str(path) for path in self.mcp_config_paths],
        }


def _load_mcp_servers(root: Path, spec: Any) -> tuple[Dict[str, Any], List[Path]]:
    servers: Dict[str, Any] = {}
    paths: List[Path] = []

    def _ingest(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            path = _safe_join(root, item)
            if not path.is_file():
                raise PluginManifestError(f"MCP config not found: {item}")
            paths.append(path)
            data = _read_json(path)
            block = data.get("mcpServers", data)
            if isinstance(block, dict):
                servers.update(block)
            return
        if isinstance(item, dict):
            block = item.get("mcpServers", item)
            if isinstance(block, dict):
                # A single server object vs a map of servers.
                if "command" in block or "url" in block:
                    name = str(block.get("name") or "plugin")
                    servers[name] = block
                else:
                    servers.update(block)
            return
        if isinstance(item, list):
            for entry in item:
                _ingest(entry)

    _ingest(spec)
    return servers, paths


def parse_plugin_dir(plugin_dir: Path | str) -> PluginManifest:
    root = Path(plugin_dir).expanduser().resolve()
    if not root.is_dir():
        raise PluginManifestError(f"Plugin directory does not exist: {root}")

    manifest_path = root / ".cursor-plugin" / "plugin.json"
    if not manifest_path.is_file():
        # Agent Plugins open standard: plugin.json at the package root.
        alt = root / "plugin.json"
        if alt.is_file():
            manifest_path = alt
        else:
            raise PluginManifestError(
                f"No .cursor-plugin/plugin.json (or root plugin.json) in {root}"
            )

    raw = _read_json(manifest_path)
    name = str(raw.get("name") or "").strip()
    if not name or not PLUGIN_NAME_RE.match(name):
        raise PluginManifestError(
            "plugin.json 'name' must be lowercase kebab-case "
            "(alphanumeric, hyphens, periods)"
        )

    skills: List[Path] = []
    if "skills" in raw:
        for rel in _as_path_list(raw.get("skills")):
            skills.extend(_discover_skill_dirs(_safe_join(root, rel)))
    else:
        skills.extend(_discover_skill_dirs(root / "skills"))
        if not skills and (root / "SKILL.md").is_file():
            skills.append(root)

    rules: List[Path] = []
    if "rules" in raw:
        for rel in _as_path_list(raw.get("rules")):
            rules.extend(_discover_files(_safe_join(root, rel), (".md", ".mdc", ".markdown")))
    else:
        rules.extend(_discover_files(root / "rules", (".md", ".mdc", ".markdown")))

    agents: List[Path] = []
    if "agents" in raw:
        for rel in _as_path_list(raw.get("agents")):
            agents.extend(_discover_files(_safe_join(root, rel), (".md", ".mdc", ".markdown")))
    else:
        agents.extend(_discover_files(root / "agents", (".md", ".mdc", ".markdown")))

    commands: List[Path] = []
    if "commands" in raw:
        for rel in _as_path_list(raw.get("commands")):
            commands.extend(
                _discover_files(_safe_join(root, rel), (".md", ".mdc", ".markdown", ".txt"))
            )
    else:
        commands.extend(
            _discover_files(root / "commands", (".md", ".mdc", ".markdown", ".txt"))
        )

    mcp_spec: Any = raw.get("mcpServers")
    if mcp_spec is None and (root / "mcp.json").is_file():
        mcp_spec = "mcp.json"
    mcp_servers, mcp_paths = _load_mcp_servers(root, mcp_spec)

    return PluginManifest(
        name=name,
        root=root,
        raw=raw,
        display_name=str(raw.get("displayName") or name),
        description=str(raw.get("description") or ""),
        version=str(raw.get("version") or ""),
        author=dict(raw.get("author") or {}),
        skills=skills,
        rules=rules,
        agents=agents,
        commands=commands,
        mcp_servers=mcp_servers,
        mcp_config_paths=mcp_paths,
        variables=dict(raw.get("variables") or {}),
    )


@dataclass
class MarketplaceManifest:
    name: str
    root: Path
    plugins: List[Dict[str, Any]]
    raw: Dict[str, Any]

    def resolve_plugin_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        for entry in self.plugins:
            source = str(entry.get("source") or "").strip()
            if not source:
                continue
            dirs.append(_safe_join(self.root, source))
        return dirs


def parse_marketplace(root: Path | str) -> Optional[MarketplaceManifest]:
    package = Path(root).expanduser().resolve()
    path = package / ".cursor-plugin" / "marketplace.json"
    if not path.is_file():
        return None
    raw = _read_json(path)
    name = str(raw.get("name") or "").strip()
    plugins = raw.get("plugins")
    if not name or not isinstance(plugins, list):
        raise PluginManifestError(
            "marketplace.json requires 'name' and a 'plugins' array"
        )
    return MarketplaceManifest(name=name, root=package, plugins=plugins, raw=raw)


def discover_plugins(source_dir: Path | str) -> List[PluginManifest]:
    """Parse a local folder as one plugin or a marketplace of plugins."""
    root = Path(source_dir).expanduser().resolve()
    marketplace = parse_marketplace(root)
    if marketplace:
        return [parse_plugin_dir(path) for path in marketplace.resolve_plugin_dirs()]
    return [parse_plugin_dir(root)]
