"""
Skill Installer — install, uninstall, update, and list external skills.

Skills can be installed from Git repos (GitHub, GitLab, etc.) or created locally.
Installed skill metadata is tracked in limebot.json under skills.installed.

Usage (CLI):
    python -m core.skill_installer install <repo_url_or_shorthand> [--ref branch] [--name override]
    python -m core.skill_installer uninstall <skill_name>
    python -m core.skill_installer update <skill_name>
    python -m core.skill_installer list
    python -m core.skill_installer enable <skill_name>
    python -m core.skill_installer disable <skill_name>

Shorthand:
    owner/repo  →  expands to https://github.com/owner/repo
"""

import json
import re
import shutil
import subprocess
import sys
import shutil as _shutil
from importlib import metadata as importlib_metadata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from core.runtime_paths import get_config_file, get_skill_dirs, get_skills_dir

SKILLS_DIR = get_skills_dir()
CONFIG_FILE = get_config_file()
_INITIAL_SKILLS_DIR = Path(SKILLS_DIR)
_INITIAL_CONFIG_FILE = Path(CONFIG_FILE)

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False


_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _is_valid_skill_name(name: str) -> bool:
    return bool(_SKILL_NAME_RE.match(name))


class SkillInstaller:
    """Manages external skill installation, updates, and removal."""

    def __init__(self):
        # Keep the module constants as a compatibility seam for embedders and
        # tests, but capture them per instance so one installer never follows a
        # later environment change halfway through an operation.
        configured_skills_dir = Path(SKILLS_DIR)
        configured_config_file = Path(CONFIG_FILE)
        self.skills_dir = (
            get_skills_dir()
            if configured_skills_dir == _INITIAL_SKILLS_DIR
            else configured_skills_dir
        )
        self.config_file = (
            get_config_file()
            if configured_config_file == _INITIAL_CONFIG_FILE
            else configured_config_file
        )
        self.config = self._load_config()

    def _load_config(self) -> dict:
        if self.config_file.exists():
            try:
                return json.loads(self.config_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Failed to load {self.config_file}, using defaults: {e}")
        return {"skills": {"enabled": [], "installed": {}}}

    def _save_config(self) -> None:
        self.config.setdefault("skills", {})
        self.config["skills"].setdefault("enabled", [])
        self.config["skills"].setdefault("installed", {})
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            self.config_file.write_text(
                json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Failed to save config to {self.config_file}: {e}")

    def _find_discovered_skill_dir(self, skill_name: str) -> Optional[Path]:
        """Find an installed, bundled, or legacy skill without exposing paths."""

        candidates = [self.skills_dir]
        candidates.extend(Path(path) for path in get_skill_dirs())
        seen: set[Path] = set()
        for base in candidates:
            try:
                base = base.resolve()
                target = (base / skill_name).resolve()
                if base in seen or target.parent != base:
                    continue
                seen.add(base)
                if target.is_dir() and (target / "SKILL.md").is_file():
                    return target
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _expand_repo_url(repo_url: str) -> str:
        """Expand 'owner/repo' shorthand to a full GitHub URL."""
        url = repo_url.strip()
        if re.match(r"^[\w.-]+/[\w.-]+$", url):
            return f"https://github.com/{url}"
        return url

    @staticmethod
    def _resolve_name(repo_url: str) -> str:
        """Derive skill name from a repo URL."""
        clean = repo_url.rstrip("/")
        if clean.endswith(".git"):
            clean = clean[:-4]
        basename = clean.split("/")[-1]

        for prefix in ("limebot-skill-", "lime-skill-", "lb-skill-"):
            if basename.startswith(prefix):
                return basename[len(prefix) :]
        return basename

    @staticmethod
    def _read_metadata(skill_md: Path) -> dict:
        """Read metadata (version, description) from SKILL.md frontmatter."""
        meta: dict = {"version": "unknown", "description": "", "dependencies": {}}
        if not skill_md.exists():
            return meta

        try:
            content = skill_md.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 2:
                    frontmatter = parts[1]
                    if HAS_YAML:
                        try:
                            data = yaml.safe_load(frontmatter) or {}
                        except Exception:
                            data = {}
                    else:
                        data = {}
                        for line in frontmatter.splitlines():
                            line = line.strip()
                            if ":" in line and not line.startswith("{"):
                                key, val = line.split(":", 1)
                                key = key.strip()
                                val = val.strip().strip('"').strip("'")
                                data[key] = val

                    if isinstance(data, dict):
                        if "version" in data:
                            meta["version"] = str(data.get("version", "unknown"))
                        if "description" in data:
                            meta["description"] = str(data.get("description", ""))
                        if "dependencies" in data:
                            meta["dependencies"] = data.get("dependencies") or {}
        except Exception as e:
            logger.warning(f"Could not read metadata from {skill_md}: {e}")
        return meta

    @staticmethod
    def _normalize_deps(dep_map: dict) -> dict:
        if not isinstance(dep_map, dict):
            return {"python": [], "node": [], "binaries": []}
        return {
            "python": list(dep_map.get("python") or []),
            "node": list(dep_map.get("node") or []),
            "binaries": list(dep_map.get("binaries") or []),
        }

    @staticmethod
    def _parse_requirements(reqs_path: Path) -> list[str]:
        if not reqs_path.exists():
            return []
        packages: list[str] = []
        try:
            for raw in reqs_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(("-", "--")):
                    continue
                line = line.split(";", 1)[0].strip()
                if not line:
                    continue
                name = re.split(r"(==|>=|<=|~=|!=|>|<)", line, 1)[0].strip()
                if "[" in name:
                    name = name.split("[", 1)[0].strip()
                if name:
                    packages.append(name)
        except Exception:
            return []
        return sorted(set(packages))

    @staticmethod
    def _parse_package_json(pkg_path: Path) -> list[str]:
        if not pkg_path.exists():
            return []
        try:
            data = json.loads(pkg_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        deps = []
        for key in ("dependencies", "optionalDependencies"):
            block = data.get(key, {})
            if isinstance(block, dict):
                deps.extend(list(block.keys()))
        return sorted(set(deps))

    @staticmethod
    def _get_python_exe() -> str:
        """Get the target python executable using venv logic."""
        python_exe = sys.executable
        local_venv = Path(".venv")
        if local_venv.exists() and local_venv.is_dir():
            if sys.platform == "win32":
                python_exe = str(local_venv / "Scripts" / "python.exe")
            else:
                python_exe = str(local_venv / "bin" / "python")
        return python_exe

    @staticmethod
    def _missing_python_deps(packages: list[str]) -> list[str]:
        missing: list[str] = []
        if not packages:
            return missing

        # If we are already running in the correct target python, importlib_metadata is faster
        python_exe = SkillInstaller._get_python_exe()
        if Path(sys.executable).resolve() == Path(python_exe).resolve():
            for pkg in packages:
                try:
                    importlib_metadata.version(pkg)
                except importlib_metadata.PackageNotFoundError:
                    missing.append(pkg)
            return missing

        # Otherwise, ask the target python executable what it has installed
        try:
            import json

            check_script = (
                "import sys, json; "
                "from importlib import metadata; "
                "pkgs = sys.argv[1:]; "
                "missing = [p for p in pkgs if not (lambda x: True if metadata.version(x) else False) "
                "try: pass; except: True]; "
                "print(json.dumps([p for p in pkgs if p not in [m.metadata('Name')['Name'] for d in metadata.distributions()]]))"
            )

            check_script = """
import sys, json
from importlib import metadata
pkgs = sys.argv[1:]
missing = []
for p in pkgs:
    try:
        metadata.version(p)
    except metadata.PackageNotFoundError:
        missing.append(p)
print(json.dumps(missing))
"""
            result = subprocess.run(
                [python_exe, "-c", check_script] + packages,
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout.strip())
        except Exception as e:
            logger.warning(f"Could not verify remote deps, assuming missing: {e}")
            return packages

    @staticmethod
    def _missing_node_deps(skill_dir: Path, packages: list[str]) -> list[str]:
        if not packages:
            return []
        node_modules = skill_dir / "node_modules"
        missing: list[str] = []
        for pkg in packages:
            if pkg.startswith("@") and "/" in pkg:
                scope, name = pkg.split("/", 1)
                pkg_path = node_modules / scope / name
            else:
                pkg_path = node_modules / pkg
            if not pkg_path.exists():
                missing.append(pkg)
        return missing

    @staticmethod
    def _missing_binaries(binaries: list[str]) -> list[str]:
        missing: list[str] = []
        for binary in binaries:
            if not _shutil.which(binary):
                missing.append(binary)
        return missing

    def _evaluate_skill_deps(
        self, skill_dir: Path, meta: dict
    ) -> tuple[bool, dict, dict]:
        dep_map = self._normalize_deps(meta.get("dependencies") or {})

        reqs_file = skill_dir / "requirements.txt"
        pkg_json = skill_dir / "package.json"

        python_deps = sorted(
            set(dep_map["python"]) | set(self._parse_requirements(reqs_file))
        )
        node_deps = sorted(
            set(dep_map["node"]) | set(self._parse_package_json(pkg_json))
        )
        bin_deps = sorted(set(dep_map["binaries"]))

        required = {
            "python": python_deps,
            "node": node_deps,
            "binaries": bin_deps,
        }

        missing = {
            "python": self._missing_python_deps(python_deps),
            "node": self._missing_node_deps(skill_dir, node_deps),
            "binaries": self._missing_binaries(bin_deps),
        }

        deps_ok = not any(missing.values())
        return deps_ok, missing, required

    @staticmethod
    def _install_deps(target_dir: Path) -> list[str]:
        """Install Python and/or Node.js dependencies. Returns log lines."""
        logs: list[str] = []

        reqs_file = target_dir / "requirements.txt"
        if reqs_file.exists():
            logger.info("Installing Python dependencies...")
            python_exe = sys.executable

            # If there's a local .venv, use it
            local_venv = Path(".venv")
            if local_venv.exists() and local_venv.is_dir():
                if sys.platform == "win32":
                    python_exe = str(local_venv / "Scripts" / "python.exe")
                else:
                    python_exe = str(local_venv / "bin" / "python")
            elif sys.platform == "win32":
                # Check if the projected path exceeds MAX_PATH (260)
                projected_max = (
                    len(str(Path.cwd().resolve() / ".venv" / "Lib" / "site-packages"))
                    + 180
                )
                if projected_max >= 259:
                    logger.warning("\n====== WINDOWS PATH LIMIT WARNING ======")
                    logger.warning(
                        f"Your project is located at a very long path ({projected_max}/259 chars max)."
                    )
                    logger.warning(
                        "Installing Python packages may fail with '[Errno 2] No such file or directory'.\n"
                    )
                    logger.info("To fix this, choose ONE of the following:")
                    logger.info(
                        "1. Move your project to a shorter path (e.g. C:\\Bots\\LimeBot-OS)"
                    )
                    logger.info(
                        "2. Enable Long Paths in Windows by running this in an Administrator PowerShell:"
                    )
                    logger.info(
                        '   New-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force'
                    )
                    logger.warning("========================================\n")

            pip_result = subprocess.run(
                [
                    python_exe,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(reqs_file),
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if pip_result.returncode != 0:
                logger.warning(f"pip errors:\n{pip_result.stderr.strip()}")
                logs.append(f"pip: {pip_result.stderr.strip()}")
            else:
                logs.append("pip: dependencies installed")

        pkg_json = target_dir / "package.json"
        if pkg_json.exists():
            logger.info("Installing Node.js dependencies...")
            npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
            npm_result = subprocess.run(
                [npm_cmd, "install"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if npm_result.returncode != 0:
                logger.warning(f"npm errors:\n{npm_result.stderr.strip()}")
                logs.append(f"npm: {npm_result.stderr.strip()}")
            else:
                logs.append("npm: dependencies installed")

        return logs

    def _clone_repo(
        self, repo_url: str, target_dir: Path, ref: str, explicit_ref: bool
    ) -> Optional[str]:
        """Clone a repo. Returns None on success, error message on failure."""
        try:
            cmd = ["git", "clone", "--depth=1"]
            if explicit_ref:
                cmd += ["--branch", ref]
            cmd += [repo_url, str(target_dir)]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                return None

            if not explicit_ref:
                return f"Git clone failed: {result.stderr.strip()}"

            logger.info(f"Branch '{ref}' not found, retrying with default branch...")
            shutil.rmtree(target_dir, ignore_errors=True)
            fallback = subprocess.run(
                ["git", "clone", "--depth=1", repo_url, str(target_dir)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if fallback.returncode == 0:
                return None
            return f"Git clone failed: {fallback.stderr.strip()}"

        except FileNotFoundError:
            return "Git is not installed or not in PATH."
        except subprocess.TimeoutExpired:
            shutil.rmtree(target_dir, ignore_errors=True)
            return "Clone timed out after 60 seconds."

    def install(
        self,
        repo_url: str,
        ref: str = "main",
        name: Optional[str] = None,
        explicit_ref: bool = False,
    ) -> dict:
        """Clone a skill repo and register it."""
        repo_url = self._expand_repo_url(repo_url)
        skill_name = name or self._resolve_name(repo_url)

        if not _is_valid_skill_name(skill_name):
            return {
                "status": "error",
                "message": f"Invalid skill name '{skill_name}'. Only alphanumeric, hyphens, and underscores are allowed.",
            }

        self.skills_dir.mkdir(parents=True, exist_ok=True)
        target_dir = self.skills_dir / skill_name

        try:
            resolved = target_dir.resolve()
            if not str(resolved).startswith(str(self.skills_dir.resolve())):
                return {
                    "status": "error",
                    "message": "Path traversal detected in skill name.",
                }
        except Exception as e:
            return {"status": "error", "message": f"Path resolution error: {e}"}

        if target_dir.exists():
            return {
                "status": "error",
                "message": f"Skill '{skill_name}' is already installed. Use 'update' to upgrade it.",
            }

        logger.info(f"Cloning {repo_url} → skills/{skill_name}...")
        error = self._clone_repo(repo_url, target_dir, ref, explicit_ref)
        if error:
            return {"status": "error", "message": error}

        skill_md = target_dir / "SKILL.md"
        if not skill_md.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
            return {
                "status": "error",
                "message": f"Invalid skill: no SKILL.md found in {repo_url}. Cleaned up.",
            }

        meta = self._read_metadata(skill_md)
        version = meta.get("version", "unknown")
        description = meta.get("description", "")

        dep_logs = self._install_deps(target_dir)

        installed = self.config.setdefault("skills", {}).setdefault("installed", {})
        installed[skill_name] = {
            "repo": repo_url,
            "ref": ref,
            "version": version,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "source": "git",
        }
        enabled: list = self.config["skills"].setdefault("enabled", [])
        if skill_name not in enabled:
            enabled.append(skill_name)
        self._save_config()

        logger.success(f"Skill '{skill_name}' installed successfully!")
        return {
            "status": "success",
            "message": f"Skill '{skill_name}' installed and enabled. Restart LimeBot to activate.",
            "skill_name": skill_name,
            "version": version,
            "description": description,
            "deps": dep_logs,
        }

    def uninstall(self, skill_name: str, force: bool = False) -> dict:
        """Remove an installed skill."""

        if not _is_valid_skill_name(skill_name):
            return {"status": "error", "message": f"Invalid skill name '{skill_name}'."}

        installed = self.config.get("skills", {}).get("installed", {})
        if skill_name not in installed and not force:
            return {
                "status": "error",
                "message": f"Skill '{skill_name}' is not installed.",
            }

        target_dir = self.skills_dir / skill_name
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)

        if skill_name in installed:
            del installed[skill_name]

        enabled: list = self.config["skills"].setdefault("enabled", [])
        if skill_name in enabled:
            enabled.remove(skill_name)

        self._save_config()
        logger.success(f"Skill '{skill_name}' uninstalled.")
        return {"status": "success", "message": f"Skill '{skill_name}' removed."}

    def update(self, skill_name: str) -> dict:
        """Pull latest changes for a skill and re-install deps."""
        installed = self.config.get("skills", {}).get("installed", {})
        if skill_name not in installed:
            return {
                "status": "error",
                "message": f"Skill '{skill_name}' is not installed.",
            }

        target_dir = self.skills_dir / skill_name
        if not target_dir.exists():
            return {
                "status": "error",
                "message": f"Skill directory for '{skill_name}' is missing.",
            }

        logger.info(f"Updating skill '{skill_name}'...")
        try:
            result = subprocess.run(
                ["git", "pull"],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return {
                    "status": "error",
                    "message": f"Git pull failed: {result.stderr.strip()}",
                }
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "Update timed out."}

        dep_logs = self._install_deps(target_dir)

        skill_md = target_dir / "SKILL.md"
        meta = self._read_metadata(skill_md)
        version = meta.get("version", "unknown")
        installed[skill_name]["version"] = version
        installed[skill_name]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_config()

        logger.success(f"Skill '{skill_name}' updated to version {version}.")
        return {
            "status": "success",
            "message": f"Skill updated to {version}. Restart to apply.",
            "version": version,
            "deps": dep_logs,
        }

    def enable(self, skill_name: str) -> dict:
        """Enable an installed skill."""
        skill_dir = self._find_discovered_skill_dir(skill_name)
        if skill_dir is None:
            return {
                "status": "error",
                "message": f"Skill '{skill_name}' not found.",
            }

        meta = self._read_metadata(skill_dir / "SKILL.md")
        deps_ok, missing, required = self._evaluate_skill_deps(skill_dir, meta)
        if not deps_ok:
            return {
                "status": "error",
                "code": "deps_missing",
                "message": (
                    f"Skill '{skill_name}' is missing dependencies. "
                    "Install them before enabling the skill."
                ),
                "missing_deps": missing,
                "required_deps": required,
            }

        enabled: list = self.config["skills"].setdefault("enabled", [])
        if skill_name not in enabled:
            enabled.append(skill_name)
            self._save_config()
        return {"status": "success", "message": f"Skill '{skill_name}' enabled."}

    def disable(self, skill_name: str) -> dict:
        """Disable an installed skill."""
        enabled: list = self.config["skills"].setdefault("enabled", [])
        if skill_name in enabled:
            enabled.remove(skill_name)
            self._save_config()
        return {"status": "success", "message": f"Skill '{skill_name}' disabled."}

    def install_skill_deps(self, skill_name: str = None) -> dict:
        """Install dependencies for a skill or all enabled skills."""
        enabled: list = self.config.get("skills", {}).get("enabled", [])

        if skill_name:
            # Single skill
            skill_dir = self._find_discovered_skill_dir(skill_name)
            if skill_dir is None:
                return {
                    "status": "error",
                    "message": f"Skill '{skill_name}' not found.",
                }

            skill_md = skill_dir / "SKILL.md"
            meta = self._read_metadata(skill_md)
            deps_ok, missing, required = self._evaluate_skill_deps(skill_dir, meta)

            if deps_ok:
                return {
                    "status": "success",
                    "message": f"Skill '{skill_name}' has no missing dependencies.",
                }

            # If only binary deps are missing, we can't auto-install them
            has_installable = bool(missing.get("python") or missing.get("node"))
            missing_bins = missing.get("binaries", [])
            if missing_bins and not has_installable:
                bin_list = ", ".join(f"'{b}'" for b in missing_bins)
                return {
                    "status": "error",
                    "code": "binary_deps_manual",
                    "message": (
                        f"Skill '{skill_name}' requires external binaries that "
                        f"cannot be auto-installed: {bin_list}. "
                        "Install them manually and ensure they are on your PATH."
                    ),
                    "missing_deps": missing,
                }

            logger.info(f"Installing deps for '{skill_name}'...")
            logs = self._install_deps(skill_dir)

            # Re-check
            deps_ok, still_missing, _ = self._evaluate_skill_deps(skill_dir, meta)

            # If installable deps resolved but binaries remain, note it clearly
            if not deps_ok and still_missing.get("binaries") and not (
                still_missing.get("python") or still_missing.get("node")
            ):
                bin_list = ", ".join(f"'{b}'" for b in still_missing["binaries"])
                logs.append(
                    f"Note: external binaries still missing: {bin_list}. "
                    "Install them manually and ensure they are on your PATH."
                )

            return {
                "status": "success" if deps_ok else "partial",
                "skill": skill_name,
                "logs": logs,
                "still_missing": still_missing if not deps_ok else {},
            }
        else:
            # All enabled skills
            results = []
            all_dirs = []

            for name in enabled:
                skill_dir = self.skills_dir / name
                if skill_dir.exists():
                    all_dirs.append((name, skill_dir))

            if not all_dirs:
                return {"status": "success", "message": "No enabled skills found."}

            for name, skill_dir in all_dirs:
                skill_md = skill_dir / "SKILL.md"
                meta = self._read_metadata(skill_md)
                deps_ok, missing, _ = self._evaluate_skill_deps(skill_dir, meta)

                if deps_ok:
                    results.append(
                        {
                            "skill": name,
                            "status": "ok",
                            "message": "All deps satisfied.",
                        }
                    )
                    continue

                print(f"Installing deps for '{name}'...")
                logs = self._install_deps(skill_dir)
                deps_ok, still_missing, _ = self._evaluate_skill_deps(skill_dir, meta)
                results.append(
                    {
                        "skill": name,
                        "status": "success" if deps_ok else "partial",
                        "logs": logs,
                        "still_missing": still_missing if not deps_ok else {},
                    }
                )

            return {"status": "success", "results": results}

    def list_skills(self) -> dict:
        """List all skills with their source and status."""
        enabled: list = self.config.get("skills", {}).get("enabled", [])
        installed: dict = self.config.get("skills", {}).get("installed", {})
        skills = []

        # List LimeBot Skills
        if self.skills_dir.exists():
            for folder in sorted(self.skills_dir.iterdir()):
                if not folder.is_dir() or folder.name.startswith(("__", ".")):
                    continue

                skill_md = folder / "SKILL.md"
                # If SKILL.md is missing, we assume it's a core skill like 'browser' if it has python files
                # or just use folder name as description

                meta = self._read_metadata(skill_md)
                name = folder.name
                entry = installed.get(name, {})
                version = entry.get("version") or meta.get("version") or ""
                repo = entry.get("repo", "")
                deps_ok, missing_deps, required_deps = self._evaluate_skill_deps(
                    folder, meta
                )

                # Core skills might not be in 'installed' map if they come pre-packaged
                source = entry.get("source", "limebot")

                skills.append(
                    {
                        "name": name,
                        "id": name,
                        "type": "limebot",
                        "source": source,
                        "enabled": name in enabled,
                        "active": (name in enabled) and deps_ok,
                        "version": version,
                        "description": meta.get("description", "Core LimeBot Skill"),
                        "repo": repo,
                        "deps_ok": deps_ok,
                        "missing_deps": missing_deps,
                        "required_deps": required_deps,
                    }
                )

        return {"skills": skills}


def main() -> None:
    """CLI entrypoint: python -m core.skill_installer <action> [args]"""
    if len(sys.argv) < 2:
        print(
            "Usage: python -m core.skill_installer <command> [args]\n\n"
            "Commands:\n"
            "  install <repo_url> [--ref branch] [--name override]\n"
            "  uninstall <skill_name> [--force]\n"
            "  update <skill_name>\n"
            "  deps [skill_name]   Install dependencies (all enabled or one skill)\n"
            "  list\n"
            "  enable <skill_name>\n"
            "  disable <skill_name>"
        )
        sys.exit(1)

    installer = SkillInstaller()
    action = sys.argv[1].lower()

    if action == "install":
        if len(sys.argv) < 3:
            print("Error: provide a repo URL (or owner/repo shorthand)")
            sys.exit(1)
        repo_url = sys.argv[2]
        ref = "main"
        name = None
        explicit_ref = False
        args = sys.argv[3:]
        for i, arg in enumerate(args):
            if arg == "--ref" and i + 1 < len(args):
                ref = args[i + 1]
                explicit_ref = True
            elif arg == "--name" and i + 1 < len(args):
                name = args[i + 1]

        result = installer.install(
            repo_url, ref=ref, name=name, explicit_ref=explicit_ref
        )

    elif action == "uninstall":
        if len(sys.argv) < 3:
            print("Error: provide a skill name")
            sys.exit(1)
        force = "--force" in sys.argv
        result = installer.uninstall(sys.argv[2], force=force)

    elif action == "update":
        if len(sys.argv) < 3:
            print("Error: provide a skill name")
            sys.exit(1)
        result = installer.update(sys.argv[2])

    elif action == "list":
        result = installer.list_skills()

    elif action == "enable":
        if len(sys.argv) < 3:
            print("Error: provide a skill name")
            sys.exit(1)
        result = installer.enable(sys.argv[2])

    elif action == "disable":
        if len(sys.argv) < 3:
            print("Error: provide a skill name")
            sys.exit(1)
        result = installer.disable(sys.argv[2])

    elif action == "deps":
        skill_name = sys.argv[2] if len(sys.argv) >= 3 else None
        result = installer.install_skill_deps(skill_name)

    else:
        print(f"Unknown command: {action}")
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
