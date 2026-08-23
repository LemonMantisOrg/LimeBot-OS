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


def _write_cloud_seed(
    name: str,
    *,
    user: str,
    pubkey: str,
    extra_user_data: str = "",
) -> Path:
    """Write nocloud user-data/meta-data and a cidata ISO."""
    workspace = _require_allowed(DEFAULT_WORKSPACE)
    workspace.mkdir(parents=True, exist_ok=True)
    user_data = (
        "#cloud-config\n"
        "package_update: false\n"
        "ssh_pwauth: true\n"
        "disable_root: false\n"
        "users:\n"
        f"  - name: {user}\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    lock_passwd: false\n"
        "    shell: /bin/sh\n"
        "    ssh_authorized_keys:\n"
        f"      - {pubkey.strip()}\n"
        "chpasswd:\n"
        "  expire: false\n"
        f"  list: |\n"
        f"    {user}:labpass\n"
        "runcmd:\n"
        "  - [ sh, -c, 'rc-update add sshd default 2>/dev/null || true' ]\n"
        "  - [ sh, -c, 'rc-service sshd start 2>/dev/null || systemctl start ssh || systemctl start sshd || true' ]\n"
    )
    if extra_user_data:
        user_data += extra_user_data.rstrip() + "\n"
    meta = f"instance-id: limebot-{name}\nlocal-hostname: {name}\n"
    cidata = _require_allowed(workspace / f"{name}-cidata")
    cidata.mkdir(parents=True, exist_ok=True)
    user_path = cidata / "user-data"
    meta_path = cidata / "meta-data"
    user_path.write_text(user_data, encoding="utf-8")
    meta_path.write_text(meta, encoding="utf-8")
    seed = _require_allowed(workspace / f"{name}-seed.iso")
    maker = shutil.which("genisoimage") or shutil.which("mkisofs") or shutil.which("xorriso")
    if not maker:
        raise SystemExit("error: genisoimage/mkisofs is required to build a cloud-init seed")
    # Files must be named user-data/meta-data inside the ISO. 8.3 Joliet
    # names (USER_DAT) are ignored by cloud-init.
    cmd = [
        maker,
        "-output",
        str(seed),
        "-volid",
        "cidata",
        "-rational-rock",
        "-J",
        "-input-charset",
        "utf-8",
        "-graft-points",
        f"user-data={user_path}",
        f"meta-data={meta_path}",
    ]
    if Path(maker).name == "xorriso":
        cmd = [
            maker,
            "-as",
            "mkisofs",
            "-output",
            str(seed),
            "-volid",
            "cidata",
            "-rational-rock",
            "-J",
            "-graft-points",
            f"user-data={user_path}",
            f"meta-data={meta_path}",
        ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(
            f"error: seed iso failed: {(completed.stderr or completed.stdout).strip()}"
        )
    return seed


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
    seed = _require_allowed(Path(args.seed)) if getattr(args, "seed", None) else None
    pubkey = str(getattr(args, "ssh_pubkey", "") or "").strip()
    pubkey_file = str(getattr(args, "ssh_pubkey_file", "") or "").strip()
    if pubkey_file:
        pubkey = _require_allowed(Path(pubkey_file)).read_text(encoding="utf-8").strip()
    if pubkey and seed is None:
        seed = _write_cloud_seed(
            args.name,
            user=str(getattr(args, "cloud_user", None) or "alpine"),
            pubkey=pubkey,
        )
    if seed is not None:
        seed = _require_allowed(seed)
    state = {
        "name": args.name,
        "iso": str(iso) if iso else "",
        "disk": str(disk),
        "seed": str(seed) if seed else "",
        "install": bool(getattr(args, "install", False)),
        "memory_mb": int(args.memory_mb or 1024),
        "ssh_port": int(args.ssh_port or 2222),
        "ssh_user": str(getattr(args, "cloud_user", None) or "alpine"),
        "ssh_identity": str(getattr(args, "ssh_identity", "") or ""),
        "accel": str(getattr(args, "accel", None) or detect["accel"]),
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


def _save_state(name: str, state: Dict[str, Any]) -> None:
    _state_file(name).write_text(json.dumps(state, indent=2), encoding="utf-8")


def cmd_start(args: argparse.Namespace) -> Dict[str, Any]:
    detect = cmd_detect(args)
    qemu = detect.get("qemu")
    if not qemu:
        raise SystemExit(detect.get("blocker") or "error: qemu missing")
    state = _load_state(args.name)
    disk = _require_allowed(Path(state["disk"]))
    serial = _require_allowed(DEFAULT_WORKSPACE / f"{state['name']}-serial.log")
    serial.parent.mkdir(parents=True, exist_ok=True)
    serial.write_text("", encoding="utf-8")
    pid_path = _require_allowed(DEFAULT_WORKSPACE / f"{state['name']}.pid")
    # Do not use -daemonize: the parent can hang forever after fork (seen
    # with -monitor none + pidfile). Background ourself and keep a pid.
    # IDE boot is more reliable than virtio-blk on tiny cloud images.
    command = [
        qemu,
        "-name",
        str(state["name"]),
        "-machine",
        "pc",
        "-accel",
        str(state.get("accel") or ("kvm" if detect.get("kvm") else "tcg")),
        "-cpu",
        "qemu64",
        "-m",
        str(state.get("memory_mb") or 1024),
        "-smp",
        "2",
        "-drive",
        f"file={disk},if=ide,format=qcow2,index=0,media=disk",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{state['ssh_port']}-:22",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-display",
        "none",
        "-serial",
        f"file:{serial}",
        "-monitor",
        "none",
        "-no-reboot",
    ]
    seed = str(state.get("seed") or "").strip()
    if seed:
        seed_path = _require_allowed(Path(seed))
        command.extend(
            [
                "-drive",
                f"file={seed_path},if=ide,format=raw,index=1,media=cdrom,readonly=on",
            ]
        )
    iso = str(state.get("iso") or "").strip()
    install = bool(state.get("install"))
    if iso and install:
        iso_path = _require_allowed(Path(iso))
        command.extend(["-cdrom", str(iso_path), "-boot", "order=d"])
    log_path = _require_allowed(DEFAULT_WORKSPACE / f"{state['name']}-qemu.log")
    log_handle = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log_handle.close()
        raise SystemExit(f"error: qemu start failed: {exc}") from exc
    time.sleep(0.4)
    if proc.poll() is not None:
        log_handle.close()
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
        raise SystemExit(f"error: qemu exited {proc.returncode}: {tail}")
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    # Nested KVM can accept /dev/kvm then park the vCPU at 0% with an
    # empty serial log. Fall back to TCG so wait-ssh can still succeed.
    if (state.get("accel") or "kvm") == "kvm":
        deadline = time.time() + 12
        while time.time() < deadline:
            if serial.exists() and serial.stat().st_size > 0:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        else:
            try:
                proc.kill()
            except OSError:
                pass
            command = [c if c != "kvm" else "tcg" for c in command]
            proc = subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(0.4)
            if proc.poll() is not None:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
                raise SystemExit(f"error: qemu tcg fallback exited {proc.returncode}: {tail}")
            pid_path.write_text(str(proc.pid), encoding="utf-8")
            state["accel"] = "tcg"
    state["pid"] = proc.pid
    state["serial"] = str(serial)
    state["qemu_command"] = command
    _save_state(args.name, state)
    return {
        "ok": True,
        "pid": proc.pid,
        "ssh_port": state["ssh_port"],
        "accel": state.get("accel") or detect["accel"],
        "serial": str(serial),
        "command": command,
    }


def _ssh_banner(port: int) -> str:
    """QEMU user-net accepts TCP even when sshd is down. Require an SSH banner."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.5)
    try:
        sock.connect(("127.0.0.1", port))
        data = sock.recv(256)
    except OSError as exc:
        return f"error:{exc}"
    finally:
        try:
            sock.close()
        except OSError:
            pass
    text = data.decode("ascii", errors="replace") if data else ""
    if text.startswith("SSH-"):
        return text.strip()
    if data:
        return f"not-ssh:{text[:80]!r}"
    return "tcp-open-no-banner"


def cmd_wait_ssh(args: argparse.Namespace) -> Dict[str, Any]:
    state = _load_state(args.name)
    port = int(state.get("ssh_port") or 22)
    timeout = max(1, int(args.timeout or 60))
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = _ssh_banner(port)
        if last.startswith("SSH-"):
            return {"ok": True, "port": port, "ready": True, "banner": last}
        time.sleep(1)
    serial = str(state.get("serial") or "")
    serial_tail = ""
    if serial and Path(serial).exists():
        serial_tail = Path(serial).read_text(encoding="utf-8", errors="replace")[-800:]
    raise SystemExit(
        f"error: ssh banner on port {port} did not appear: {last}; "
        f"serial_tail={serial_tail!r}"
    )


def cmd_ssh(args: argparse.Namespace) -> Dict[str, Any]:
    state = _load_state(args.name)
    port = int(state.get("ssh_port") or 22)
    user = str(args.user or state.get("ssh_user") or "root")
    command = list(args.command or ["uname", "-a"])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = ["uname", "-a"]
    identity = str(
        getattr(args, "identity", "") or state.get("ssh_identity") or ""
    ).strip()
    ssh = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "IdentitiesOnly=yes" if identity else "IdentitiesOnly=no",
        "-o",
        "PreferredAuthentications=publickey,password",
        "-o",
        "ConnectTimeout=12",
        "-p",
        str(port),
        f"{user}@127.0.0.1",
        *command,
    ]
    if identity:
        ident = _require_allowed(Path(identity))
        ssh[1:1] = ["-i", str(ident)]
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
        time.sleep(0.4)
        try:
            os.kill(int(pid), 9)
        except OSError:
            pass
    state["pid"] = None
    _save_state(args.name, state)
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
    create.add_argument("--seed")
    create.add_argument("--ssh-pubkey")
    create.add_argument("--ssh-pubkey-file")
    create.add_argument("--ssh-identity")
    create.add_argument("--cloud-user", default="alpine")
    create.add_argument(
        "--install",
        action="store_true",
        help="Boot the attached ISO as an installer (default: boot the disk).",
    )
    create.add_argument(
        "--accel",
        choices=("kvm", "tcg"),
        help="Force kvm or tcg. Default: kvm when /dev/kvm exists.",
    )
    for name in ("start", "stop"):
        item = sub.add_parser(name)
        item.add_argument("--name", required=True)
    wait = sub.add_parser("wait-ssh")
    wait.add_argument("--name", required=True)
    wait.add_argument("--timeout", type=int, default=180)
    ssh = sub.add_parser("ssh")
    ssh.add_argument("--name", required=True)
    ssh.add_argument("--user", default=None)
    ssh.add_argument("--identity")
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
