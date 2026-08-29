import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_run_command_rejects_the_limebot_service_entrypoint():
    from core.bus import MessageBus
    from core.tools import Toolbox

    toolbox = Toolbox(
        allowed_paths=[str(Path.cwd())],
        bus=MessageBus(),
        config=SimpleNamespace(command_timeout=0, run_command_max_seconds=1),
    )
    result = await toolbox.run_command(
        f'"{sys.executable}" "{Path.cwd() / "main.py"}" user-info'
    )

    assert "long-running backend" in result
    assert "skill entrypoint" in result


@pytest.mark.asyncio
async def test_run_command_explains_blocked_windows_chaining():
    from core.bus import MessageBus
    from core.tools import Toolbox

    toolbox = Toolbox(
        allowed_paths=[str(Path.cwd())],
        bus=MessageBus(),
        config=SimpleNamespace(command_timeout=0),
    )
    result = await toolbox.run_command("cd /d D:\\Code\\LimeBot-OS && python main.py")

    assert "Chained shell commands are blocked" in result
    assert "intended command" in result


@pytest.mark.asyncio
async def test_run_command_blocks_and_and_env_assignment_with_exact_policy():
    from core.bus import MessageBus
    from core.tools import Toolbox

    toolbox = Toolbox(
        allowed_paths=[str(Path.cwd())],
        bus=MessageBus(),
        config=SimpleNamespace(command_timeout=0),
    )
    blocked = await toolbox.run_command("python -m unittest && python -m py_compile clamp.py")
    assert "forbidden character/sequence '&&'" in blocked
    assert "run_steps" in blocked

    env_blocked = toolbox.validate_command("PYTHONPATH=. python -m unittest")
    assert "PYTHONPATH=" in env_blocked
    assert "command policy" in env_blocked
    assert "environment blocked" in env_blocked.lower()


@pytest.mark.asyncio
async def test_isolated_workspace_allows_shell_chaining_but_not_escapes():
    from core.bus import MessageBus
    from core.tools import Toolbox
    from core.workspace_isolation import IsolatedWorkspace

    source = Path("temp") / "run_command_isolated_guard"
    source.mkdir(parents=True, exist_ok=True)
    (source / "ok.py").write_text("x = 1\n", encoding="utf-8")
    workspace = await IsolatedWorkspace.create(source, label="cmd-guard")
    try:
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())],
            bus=MessageBus(),
            config=SimpleNamespace(command_timeout=0, skills=SimpleNamespace(enabled=[])),
        )
        token = workspace.activate()
        try:
            assert toolbox.validate_command("python -m unittest && python -m py_compile ok.py") is None
            assert toolbox.validate_command("true || false") is None
            assert toolbox.validate_command("echo hi | grep hi") is None
            assert toolbox.validate_command("echo hi > out.txt") is None
            assert "sudo" in (toolbox.validate_command("sudo python ok.py") or "").lower()
            assert "PYTHONPATH=" in (toolbox.validate_command("PYTHONPATH=. python ok.py") or "")
            assert "$(" in (toolbox.validate_command("echo $(whoami)") or "")
            assert "cannot escape" in (toolbox.validate_command("cat ../secret") or "")
            chained = await toolbox.run_command(
                f'"{sys.executable}" -c "print(1)" && "{sys.executable}" -c "print(2)"'
            )
            assert "Exit Code: 0" in chained
            assert "forbidden" not in chained.lower()
        finally:
            workspace.deactivate(token)
    finally:
        await workspace.cleanup()


@pytest.mark.asyncio
async def test_run_steps_runs_unittest_then_py_compile_without_and():
    from core.bus import MessageBus
    from core.tools import Toolbox

    toolbox = Toolbox(
        allowed_paths=[str(Path.cwd())],
        bus=MessageBus(),
        config=SimpleNamespace(command_timeout=0, skills=SimpleNamespace(enabled=[])),
    )
    result = await toolbox.run_steps(
        [
            f'"{sys.executable}" -c "print(\'step-one\')"',
            f'"{sys.executable}" -m py_compile "{Path(__file__).resolve()}"',
        ]
    )
    assert "step-one" in result
    assert "Exit Code: 0" in result
    assert not result.startswith("Error:")


@pytest.mark.asyncio
async def test_run_command_has_a_hard_cap_when_command_timeout_is_zero():
    from core.bus import MessageBus
    from core.tools import Toolbox

    toolbox = Toolbox(
        allowed_paths=[str(Path.cwd())],
        bus=MessageBus(),
        config=SimpleNamespace(command_timeout=0, run_command_max_seconds=0.2),
    )
    result = await toolbox.run_command(
        f'"{sys.executable}" -c "import time; time.sleep(1)"'
    )

    assert "[TIMEOUT]" in result
