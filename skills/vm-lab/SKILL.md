---
name: vm-lab
description: Download an ISO, create a QEMU/KVM virtual machine in an allowlisted workspace, wait for SSH, and run a command. Use for install-an-OS-in-a-VM tasks (CachyOS, Alpine, cloud images).
dependencies:
  python: []
  node: []
  binaries: [qemu-system-x86_64, qemu-img]
---

# VM Lab

This skill is a strategy guide, not a tool name. Do not call `vm-lab` as a tool.

Use `run_command` with the skill entrypoint only. Do not invent shell pipelines.

```text
python {baseDir}/vm_lab.py detect
python {baseDir}/vm_lab.py download --url URL --dest PATH
python {baseDir}/vm_lab.py create --name NAME --disk PATH --ssh-pubkey-file PATH --cloud-user alpine --ssh-port 2222
python {baseDir}/vm_lab.py start --name NAME
python {baseDir}/vm_lab.py wait-ssh --name NAME --timeout 180
python {baseDir}/vm_lab.py ssh --name NAME --user alpine --identity PATH --command uname -a
python {baseDir}/vm_lab.py stop --name NAME
```

`wait-ssh` waits for an `SSH-` banner. QEMU user-net can accept TCP on the hostfwd port before sshd exists; a bare connect is not proof.

Cloud images boot the disk. Pass `--install` only when you really want the attached ISO installer (`-boot d`). Attach a nocloud seed (`--ssh-pubkey-file` or `--seed`) so cloud-init can start sshd. Serial goes to `{name}-serial.log`.

`--accel tcg` if nested KVM parks the vCPU (empty serial, 0% CPU). `start` now retries TCG automatically when KVM produces no serial. CachyOS unattended install is a follow-up after `ssh` returns real command output.

All `--dest`, `--iso`, `--disk`, and workspace files must stay under `ALLOWED_PATHS` or `$LIMEBOT_STATE_DIR/vm-lab/`. Never point QEMU at the host root disk.

## Strategy for "download an ISO and install it in a VM"

1. `detect` first. If KVM is missing, say so and use QEMU TCG or a tiny Alpine/cloud image instead of pretending CachyOS finished.
2. Open the ISO page with `web_search` or `browser_navigate` if the browser skill is installed.
3. `download` the ISO (or a stand-in when the full image is too large) into the allowlisted dest.
4. `create` then `start` the VM. Prefer cloud-init/unattended images when a full installer cannot finish.
5. `wait-ssh` then `ssh` a real command. The SSH proof is the command output, not a claim.
6. `stop` when done.

If nested virt is unavailable, still finish detect + download + an honest blocker. Do not invent a successful OS install.
