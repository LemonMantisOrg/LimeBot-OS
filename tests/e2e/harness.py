"""Thin lifecycle harness around LimeBot's durable-queue boot path.

Starts MessageBus + DurableJobQueue + CronManager + AgentLoop, recovers
interrupted jobs, and writes a ready file. The LLM is mocked via
LIMEBOT_TEST_LLM_MODE so this process does not need paid APIs.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path


async def run_harness() -> None:
    state_dir = Path(os.environ["LIMEBOT_STATE_DIR"]).resolve()
    ready_path = Path(os.environ.get("LIMEBOT_E2E_READY_FILE") or (state_dir / "ready"))
    os.chdir(state_dir)

    from core.bus import MessageBus
    from core.job_queue import get_job_queue, reset_job_queue
    from core.loop import AgentLoop
    from core.persona_bootstrap import ensure_persona_bootstrap_files
    from core.runtime_paths import get_data_dir
    from core.scheduler import CronManager
    from core.session_manager import SessionManager

    ensure_persona_bootstrap_files()
    reset_job_queue()
    bus = MessageBus()
    queue = get_job_queue(get_data_dir() / "jobs.sqlite")
    scheduler = CronManager(bus, job_queue=queue)
    agent = AgentLoop(
        bus,
        model=os.getenv("LLM_MODEL", "openai/gpt-test"),
        scheduler=scheduler,
        session_manager=SessionManager(),
    )
    agent.job_queue = queue

    recovered = queue.recover_interrupted()
    ready = queue.list_ready()
    for job in ready:
        await bus.publish_inbound(job.to_message())

    stop = asyncio.Event()

    def _stop(*_args):
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(agent.run()),
        asyncio.create_task(scheduler.run()),
    ]
    ready_path.write_text(
        f"pid={os.getpid()}\nrecovered={len(recovered)}\nready={len(ready)}\n",
        encoding="utf-8",
    )
    await stop.wait()
    bus.stop()
    await agent.stop()
    await scheduler.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    if not os.environ.get("LIMEBOT_STATE_DIR"):
        sys.stderr.write("LIMEBOT_STATE_DIR is required\n")
        sys.exit(2)
    asyncio.run(run_harness())
