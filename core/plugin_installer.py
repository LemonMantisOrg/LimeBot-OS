"""Install Cursor plugin packages into LimeBot skill and MCP registries.

Accepted sources:
- A local folder with ``.cursor-plugin/plugin.json`` or marketplace.json
- GitHub shorthand such as ``cursor/plugins/github`` or ``cursor/plugins/create-plugin``
- A full GitHub URL, optionally pointing at a subdirectory

This installer does not vendor the official cursor/plugins tree. It copies one
resolved plugin (or marketplace entries) into ``$LIMEBOT_STATE_DIR/plugins``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from loguru import logger

from core.plugin_manifest import (
    PluginManifest,
    PluginManifestError,
    discover_plugins,
)
from core.runtime_paths import get_config_file, get_plugins_dir, get_state_dir


GITHUB_TREE_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
    r"(?:/tree/(?P<ref>[^/]+)(?:/(?P<subpath>.*))?)?/?$"
)
OWNER_REPO_RE = re.compile(
    r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?:/(?P<subpath>.+))?$"
)

# Official repo uses third_party/github, not a top-level github/ folder.
_OFFICIAL_PLUGIN_ALIASES = {
    ("cursor", "plugins", "github"): "third_party/github",
    ("cursor", "plugins", "playwright"): "third_party/playwright",
}


class PluginInstaller:
    def __init__(self, plugins_dir: Optional[Path] = None, config_file: Optional[Path] = None):
        self.plugins_dir = Path(plugins_dir or get_plugins_dir())
        self.config_file = Path(config_file or get_config_file())
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> dict:
        if self.config_file.exists():
            try:
                return json.loads(self.config_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to load %s: %s", self.config_file, exc)
        return {"skills": {"enabled": [], "installed": {}}, "plugins": {"installed": {}}}

    def _save_config(self, config: dict) -> None:
        config.setdefault("skills", {})
        config["skills"].setdefault("enabled", [])
        config["skills"].setdefault("installed", {})
        config.setdefault("plugins", {})
        config["plugins"].setdefault("installed", {})
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def parse_source(source: str) -> Dict[str, Any]:
        raw = str(source or "").strip()
        if not raw:
            raise PluginManifestError("Empty plugin source")
        local = Path(raw).expanduser()
        if local.exists():
            return {"kind": "local", "path": local.resolve()}

        tree = GITHUB_TREE_RE.match(raw)
        if tree:
            owner = tree.group("owner")
            repo = tree.group("repo")
            ref = tree.group("ref") or "main"
            subpath = (tree.group("subpath") or "").strip("/")
            subpath = _OFFICIAL_PLUGIN_ALIASES.get((owner, repo, subpath), subpath)
            return {
                "kind": "github",
                "owner": owner,
                "repo": repo,
                "ref": ref,
                "subpath": subpath,
                "url": f"https://github.com/{owner}/{repo}",
            }

        shorthand = OWNER_REPO_RE.match(raw)
        if shorthand:
            owner = shorthand.group("owner")
            repo = shorthand.group("repo")
            subpath = (shorthand.group("subpath") or "").strip("/")
            subpath = _OFFICIAL_PLUGIN_ALIASES.get((owner, repo, subpath), subpath)
            return {
                "kind": "github",
                "owner": owner,
                "repo": repo,
                "ref": "main",
                "subpath": subpath,
                "url": f"https://github.com/{owner}/{repo}",
            }

        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"}:
            raise PluginManifestError(f"Unsupported remote plugin URL: {raw}")
        raise PluginManifestError(f"Plugin source not found: {raw}")

    def _clone_github(self, spec: Dict[str, Any], dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        url = spec["url"]
        ref = spec.get("ref") or "main"
        subpath = spec.get("subpath") or ""
        cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            ref,
            url,
            str(dest),
        ]
        if subpath:
            cmd = [
                "git",
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--sparse",
                "--branch",
                ref,
                url,
                str(dest),
            ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # Some repos reject --branch for the default HEAD; retry without it.
            fallback = [
                "git",
                "clone",
                "--depth",
                "1",
                url,
                str(dest),
            ]
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(fallback, capture_output=True, text=True)
            if result.returncode != 0:
                raise PluginManifestError(
                    f"git clone failed for {url}: {result.stderr or result.stdout}"
                )
        if subpath:
            sparse = subprocess.run(
                ["git", "-C", str(dest), "sparse-checkout", "set", subpath],
                capture_output=True,
                text=True,
            )
            if sparse.returncode != 0:
                # Non-sparse clone still has the subdirectory on disk.
                logger.warning("sparse-checkout skipped: %s", sparse.stderr.strip())
            plugin_root = dest / subpath
            if not plugin_root.exists():
                raise PluginManifestError(
                    f"Repository {url} has no subdirectory '{subpath}'"
                )
            return plugin_root
        return dest

    def _copy_plugin(self, manifest: PluginManifest, dest_root: Path) -> Path:
        target = dest_root / manifest.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            manifest.root,
            target,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"),
        )
        return target

    def _merge_mcp(self, plugin_name: str, servers: Dict[str, Any]) -> List[str]:
        if not servers:
            return []
        from core.mcp_client import CONFIG_PATH, validate_mcp_config

        config_path = Path(CONFIG_PATH)
        if config_path.exists():
            try:
                current = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                current = {"mcpServers": {}}
        else:
            current = {"mcpServers": {}}
        current.setdefault("mcpServers", {})
        installed: List[str] = []
        for name, cfg in servers.items():
            key = name if name.startswith(f"{plugin_name}_") else f"{plugin_name}_{name}"
            entry = dict(cfg)
            entry.setdefault("plugin", plugin_name)
            current["mcpServers"][key] = entry
            installed.append(key)
        ok, err = validate_mcp_config(current)
        if not ok:
            raise PluginManifestError(f"Merged MCP config is invalid: {err}")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return installed

    def _enable_skills(self, config: dict, skill_names: List[str]) -> None:
        enabled = config.setdefault("skills", {}).setdefault("enabled", [])
        for name in skill_names:
            if name not in enabled:
                enabled.append(name)

    def install(self, source: str, *, name: Optional[str] = None) -> Dict[str, Any]:
        spec = self.parse_source(source)
        temp_dir: Optional[Path] = None
        try:
            if spec["kind"] == "local":
                package_root = spec["path"]
            else:
                temp_dir = Path(tempfile.mkdtemp(prefix="limebot-plugin-"))
                package_root = self._clone_github(spec, temp_dir)

            manifests = discover_plugins(package_root)
            if name:
                manifests = [item for item in manifests if item.name == name]
                if not manifests:
                    raise PluginManifestError(f"No plugin named '{name}' in {source}")

            config = self._load_config()
            installed: List[Dict[str, Any]] = []
            for manifest in manifests:
                dest = self._copy_plugin(manifest, self.plugins_dir)
                installed_manifest = discover_plugins(dest)[0]
                skill_names = [path.name for path in installed_manifest.skills]
                mcp_names = self._merge_mcp(
                    installed_manifest.name, installed_manifest.mcp_servers
                )
                self._enable_skills(config, skill_names)
                config.setdefault("plugins", {}).setdefault("installed", {})[
                    installed_manifest.name
                ] = {
                    "source": source,
                    "version": installed_manifest.version,
                    "path": str(dest),
                    "skills": skill_names,
                    "mcp_servers": mcp_names,
                    "rules": [str(path.name) for path in installed_manifest.rules],
                }
                installed.append(
                    {
                        "name": installed_manifest.name,
                        "path": str(dest),
                        "skills": skill_names,
                        "mcp_servers": mcp_names,
                        "rules": [str(path.name) for path in installed_manifest.rules],
                    }
                )
            self._save_config(config)
            return {"ok": True, "installed": installed}
        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def list_plugins(self) -> Dict[str, Any]:
        config = self._load_config()
        return {"plugins": config.get("plugins", {}).get("installed", {})}

    def uninstall(self, plugin_name: str) -> Dict[str, Any]:
        config = self._load_config()
        installed = config.get("plugins", {}).get("installed", {})
        record = installed.pop(plugin_name, None)
        target = self.plugins_dir / plugin_name
        if target.exists():
            shutil.rmtree(target)
        if record:
            for skill_name in record.get("skills") or []:
                enabled = config.get("skills", {}).get("enabled", [])
                if skill_name in enabled:
                    enabled.remove(skill_name)
            from core.mcp_client import CONFIG_PATH

            mcp_path = Path(CONFIG_PATH)
            if mcp_path.exists() and record.get("mcp_servers"):
                try:
                    mcp_cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
                    for key in record["mcp_servers"]:
                        mcp_cfg.get("mcpServers", {}).pop(key, None)
                    mcp_path.write_text(json.dumps(mcp_cfg, indent=2), encoding="utf-8")
                except Exception as exc:
                    logger.warning("Failed to prune MCP servers for %s: %s", plugin_name, exc)
        self._save_config(config)
        return {"ok": True, "removed": plugin_name, "found": record is not None}


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python -m core.plugin_installer <command> [args]\n\n"
            "Commands:\n"
            "  install <source> [--name plugin]\n"
            "  uninstall <plugin_name>\n"
            "  list"
        )
        sys.exit(1)

    installer = PluginInstaller()
    action = sys.argv[1].lower()
    if action == "install":
        if len(sys.argv) < 3:
            print("Error: provide a local path or GitHub source")
            sys.exit(1)
        source = sys.argv[2]
        name = None
        args = sys.argv[3:]
        for i, arg in enumerate(args):
            if arg == "--name" and i + 1 < len(args):
                name = args[i + 1]
        result = installer.install(source, name=name)
    elif action == "uninstall":
        if len(sys.argv) < 3:
            print("Error: provide a plugin name")
            sys.exit(1)
        result = installer.uninstall(sys.argv[2])
    elif action == "list":
        result = installer.list_plugins()
    else:
        print(f"Unknown command: {action}")
        sys.exit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
