import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "limebot_vm_lab", _ROOT / "skills" / "vm-lab" / "vm_lab.py"
)
vm_lab = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(vm_lab)


class TestVmLab(unittest.TestCase):
    def test_detect_reports_missing_qemu_cleanly(self):
        with patch.object(vm_lab.shutil, "which", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                result = vm_lab.cmd_detect(SimpleNamespace())
        self.assertTrue(result["ok"])
        self.assertEqual(result["accel"], "unavailable")
        self.assertIn("qemu", result["blocker"].lower())

    def test_download_rejects_localhost_and_outside_allowlist(self):
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "iso.bin"
            with self.assertRaises(SystemExit) as forbidden:
                vm_lab.cmd_download(
                    SimpleNamespace(
                        url="http://127.0.0.1/secret.iso",
                        dest=str(dest),
                        max_bytes=1024,
                    )
                )
            self.assertIn("localhost", str(forbidden.exception))

            outside = Path("/etc/passwd")
            with patch.dict("os.environ", {"ALLOWED_PATHS": tmp}, clear=False):
                with self.assertRaises(SystemExit) as blocked:
                    vm_lab.cmd_download(
                        SimpleNamespace(
                            url="https://example.com/tiny.iso",
                            dest=str(outside),
                            max_bytes=1024,
                        )
                    )
            self.assertIn("allowlist", str(blocked.exception))

    def test_create_writes_state_under_workspace(self):
        with TemporaryDirectory() as tmp:
            disk = Path(tmp) / "disk.qcow2"
            iso = Path(tmp) / "tiny.iso"
            iso.write_bytes(b"iso")
            args = SimpleNamespace(
                name="alpine-smoke",
                iso=str(iso),
                disk=str(disk),
                disk_size="1G",
                memory_mb=512,
                ssh_port=2222,
                seed=None,
                ssh_pubkey="",
                ssh_pubkey_file="",
                ssh_identity="",
                cloud_user="alpine",
                install=False,
            )
            with patch.dict(
                "os.environ",
                {"ALLOWED_PATHS": tmp, "LIMEBOT_STATE_DIR": tmp},
                clear=False,
            ):
                vm_lab.DEFAULT_WORKSPACE = Path(tmp) / "vm-lab"
                with patch.object(
                    vm_lab,
                    "cmd_detect",
                    return_value={
                        "ok": True,
                        "qemu": "/usr/bin/qemu-system-x86_64",
                        "qemu_img": "/usr/bin/qemu-img",
                        "kvm": False,
                        "accel": "tcg",
                        "workspace": str(vm_lab.DEFAULT_WORKSPACE),
                        "blocker": "",
                    },
                ):
                    with patch.object(
                        vm_lab.subprocess,
                        "run",
                        return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
                    ):
                        result = vm_lab.cmd_create(args)
            self.assertTrue(result["ok"])
            self.assertEqual(result["accel"], "tcg")
            state = json.loads((Path(tmp) / "vm-lab" / "alpine-smoke.json").read_text())
            self.assertEqual(state["ssh_port"], 2222)
            self.assertFalse(state["install"])
            self.assertEqual(state["seed"], "")

    def test_wait_ssh_requires_banner_not_bare_tcp(self):
        with TemporaryDirectory() as tmp:
            vm_lab.DEFAULT_WORKSPACE = Path(tmp)
            state_path = Path(tmp) / "lab.json"
            state_path.write_text(
                json.dumps({"name": "lab", "ssh_port": 2222, "serial": ""}),
                encoding="utf-8",
            )
            with patch.object(vm_lab, "_state_file", return_value=state_path):
                with patch.object(vm_lab, "_ssh_banner", return_value="tcp-open-no-banner"):
                    with self.assertRaises(SystemExit) as blocked:
                        vm_lab.cmd_wait_ssh(SimpleNamespace(name="lab", timeout=1))
            self.assertIn("banner", str(blocked.exception))

            with patch.object(vm_lab, "_state_file", return_value=state_path):
                with patch.object(vm_lab, "_ssh_banner", return_value="SSH-2.0-OpenSSH_9.7"):
                    result = vm_lab.cmd_wait_ssh(SimpleNamespace(name="lab", timeout=1))
            self.assertTrue(result["ok"])
            self.assertTrue(result["banner"].startswith("SSH-"))
