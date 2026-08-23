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
