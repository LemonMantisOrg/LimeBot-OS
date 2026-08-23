#!/usr/bin/env python3
"""Allowlisted QEMU/KVM helper for ISO download, VM create, and SSH."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


DEFAULT_WORKSPACE = Path(
    os.environ.get("LIMEBOT_STATE_DIR") or Path.cwd()
).resolve() / "vm-lab"
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB hard cap
DEFAULT_DOWNLOAD_LIMIT = 2 * 1024 * 1024 * 1024  # 2 GiB unless overridden


def _allowed_roots() -> List[Path]:
    raw = str(os.environ.get("ALLOWED_PATHS") or "").strip()
    roots = [DEFAULT_WORKSPACE]
    if raw:
        for part in raw.split(","):
            item = part.strip()
            if item:
                roots.append(Path(item).expanduser().resolve())
    state = str(os.environ.get("LIMEBOT_STATE_DIR") or "").strip()
    if state:
        roots.append(Path(state).resolve())
    roots.append(Path.cwd().resolve())
    uniq: List[Path] = []
    for root in roots:
        if root not in uniq:
            uniq.append(root)
    return uniq


def _is_allowed(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    for root in _allowed_roots():
        if resolved == root or root in resolved.parents:
            return True
    return False


def _require_allowed(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not _is_allowed(resolved):
        raise SystemExit(f"error: path is outside the VM workspace allowlist: {resolved}")
    return resolved


def _public_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SystemExit("error: only http(s) downloads are allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("error: localhost downloads are blocked")
    return url


def cmd_detect(_: argparse.Namespace) -> Dict[str, Any]:
    qemu = shutil.which("qemu-system-x86_64")
    qemu_img = shutil.which("qemu-img")
    kvm = Path("/dev/kvm").exists()
    return {
        "ok": True,
        "qemu": qemu or "",
        "qemu_img": qemu_img or "",
        "kvm": kvm,
        "accel": "kvm" if kvm and qemu else ("tcg" if qemu else "unavailable"),
        "workspace": str(DEFAULT_WORKSPACE),
        "blocker": (
            ""
            if qemu
            else "qemu-system-x86_64 is not installed. Install qemu-system-x86 on Linux."
        ),
    }


def cmd_download(args: argparse.Namespace) -> Dict[str, Any]:
    url = _public_http_url(str(args.url))
    dest = _require_allowed(Path(args.dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    limit = int(args.max_bytes or DEFAULT_DOWNLOAD_LIMIT)
    limit = max(1, min(limit, MAX_DOWNLOAD_BYTES))
    written = 0
    request = urllib.request.Request(url, headers={"User-Agent": "LimeBot-vm-lab/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp, dest.open("wb") as handle:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    handle.close()
                    dest.unlink(missing_ok=True)
                    raise SystemExit(
                        f"error: download exceeded max-bytes={limit}; "
                        "use a smaller image or raise --max-bytes"
                    )
                handle.write(chunk)
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: download failed: {exc}") from exc
    return {"ok": True, "path": str(dest), "bytes": written, "url": url}


def _state_file(name: str) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")[:40] or "vm"
    path = DEFAULT_WORKSPACE / f"{safe}.json"
    return _require_allowed(path)


def cmd_create(args: argparse.Namespace) -> Dict[str, Any]:
    detect = cmd_detect(args)
    if not detect.get("qemu"):
        raise SystemExit(detect.get("blocker") or "error: qemu missing")
    iso = _require_allowed(Path(args.iso)) if args.iso else None
    disk = _require_allowed(Path(args.disk))
    disk.parent.mkdir(parents=True, exist_ok=True)
    if not disk.exists():
        size = str(args.disk_size or "8G")
        qemu_img = detect["qemu_img"] or "qemu-img"
        completed = subprocess.run(
            [qemu_img, "create", "-f", "qcow2", str(disk), size],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(f"error: qemu-img failed: {completed.stderr.strip()}")
    state = {
        "name": args.name,
        "iso": str(iso) if iso else "",
        "disk": str(disk),
        "memory_mb": int(args.memory_mb or 1024),
        "ssh_port": int(args.ssh_port or 2222),
        "accel": detect["accel"],
        "pid": None,
    }
    path = _state_file(args.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {"ok": True, "state": str(path), **state}


def _load_state(name: str) -> Dict[str, Any]:
    path = _state_file(name)
    if not path.exists():
        raise SystemExit(f"error: unknown VM '{name}'")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_start(args: argparse.Namespace) -> Dict[str, Any]:
    detect = cmd_detect(args)
    qemu = detect.get("qemu")
    if not qemu:
        raise SystemExit(detect.get("blocker") or "error: qemu missing")
    state = _load_state(args.name)
    disk = _require_allowed(Path(state["disk"]))
    command = [
        qemu,
        "-name",
        str(state["name"]),
        "-m",
        str(state.get("memory_mb") or 1024),
        "-drive",
        f"file={disk},if=virtio",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{state['ssh_port']}-:22",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-display",
        "none",
        "-daemonize",
        "-pidfile",
        str(_require_allowed(DEFAULT_WORKSPACE / f"{state['name']}.pid")),
    ]
    if detect.get("kvm"):
        command[1:1] = ["-enable-kvm"]
    iso = str(state.get("iso") or "").strip()
    if iso:
        iso_path = _require_allowed(Path(iso))
        command.extend(["-cdrom", str(iso_path), "-boot", "d"])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(
            f"error: qemu start failed: {(completed.stderr or completed.stdout).strip()}"
        )
    pid_file = DEFAULT_WORKSPACE / f"{state['name']}.pid"
    pid = int(pid_file.read_text().strip()) if pid_file.exists() else None
    state["pid"] = pid
    _state_file(args.name).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {"ok": True, "pid": pid, "ssh_port": state["ssh_port"], "accel": detect["accel"]}


def cmd_wait_ssh(args: argparse.Namespace) -> Dict[str, Any]:
    state = _load_state(args.name)
    port = int(state.get("ssh_port") or 22)
    timeout = max(1, int(args.timeout or 60))
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        try:
            sock.connect(("127.0.0.1", port))
            sock.close()
            return {"ok": True, "port": port, "ready": True}
        except OSError as exc:
            last = str(exc)
            time.sleep(1)
        finally:
            try:
                sock.close()
            except OSError:
                pass
    raise SystemExit(f"error: ssh port {port} did not open: {last}")


def cmd_ssh(args: argparse.Namespace) -> Dict[str, Any]:
    state = _load_state(args.name)
    port = int(state.get("ssh_port") or 22)
    user = str(args.user or "root")
    command = list(args.command or ["uname", "-a"])
    ssh = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=8",
        "-p",
        str(port),
        f"{user}@127.0.0.1",
        *command,
    ]
    completed = subprocess.run(ssh, capture_output=True, text=True, check=False)
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[:4000],
        "stderr": (completed.stderr or "")[:1000],
    }


def cmd_stop(args: argparse.Namespace) -> Dict[str, Any]:
    state = _load_state(args.name)
    pid = state.get("pid")
    if pid:
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass
    state["pid"] = None
    _state_file(args.name).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {"ok": True, "stopped": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LimeBot allowlisted VM helper")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("detect")
    download = sub.add_parser("download")
    download.add_argument("--url", required=True)
    download.add_argument("--dest", required=True)
    download.add_argument("--max-bytes", type=int, default=DEFAULT_DOWNLOAD_LIMIT)
    create = sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--iso")
    create.add_argument("--disk", required=True)
    create.add_argument("--disk-size", default="8G")
    create.add_argument("--memory-mb", type=int, default=1024)
    create.add_argument("--ssh-port", type=int, default=2222)
    for name in ("start", "stop"):
        item = sub.add_parser(name)
        item.add_argument("--name", required=True)
    wait = sub.add_parser("wait-ssh")
    wait.add_argument("--name", required=True)
    wait.add_argument("--timeout", type=int, default=60)
    ssh = sub.add_parser("ssh")
    ssh.add_argument("--name", required=True)
    ssh.add_argument("--user", default="root")
    ssh.add_argument("--command", nargs=argparse.REMAINDER, default=["uname", "-a"])
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    actions = {
        "detect": cmd_detect,
        "download": cmd_download,
        "create": cmd_create,
        "start": cmd_start,
        "wait-ssh": cmd_wait_ssh,
        "ssh": cmd_ssh,
        "stop": cmd_stop,
    }
    try:
        result = actions[args.action](args)
    except SystemExit as exc:
        message = str(exc) if exc.code not in {0, 1, 2} and exc.code is not None else str(exc)
        if isinstance(exc.code, int) and exc.code > 2:
            raise
        if str(exc):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 1
        raise
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
