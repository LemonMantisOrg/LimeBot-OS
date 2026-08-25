"""LimeBot Entry Point."""

import asyncio
import signal
import sys
import os
from pathlib import Path
import json
import time
from loguru import logger

from config import load_config
from core.asyncio_compat import configure_asyncio_runtime
from core.bus import MessageBus
from core.loop import AgentLoop
from core.persona_bootstrap import ensure_persona_bootstrap_files
from core.scheduler import CronManager
from core.session_manager import SessionManager
from core.asyncio_windows import install_windows_asyncio_exception_filter
from core.runtime_compat import enforce_supported_python_runtime
from channels.discord import DiscordChannel
from channels.telegram import TelegramChannel
from channels.whatsapp import WhatsAppChannel
from channels.web import WebChannel


logger.remove()
logger.add(
    sys.stderr,
    level="DEBUG",
    filter=lambda r: (
        r["level"].no >= 10
        and (r["level"].no >= 20 or "core.loop" in r["name"] or "⏱" in r["message"])
    ),
)
os.makedirs("logs", exist_ok=True)
logger.add(
    "logs/limebot.log",
    rotation="1 MB",
    retention="10 days",
    level="DEBUG",
    filter=lambda r: (
        r["level"].no >= 20 or "core.loop" in r["name"] or "⏱" in r["message"]
    ),
)

BOOT_PATH = Path("persona") / "BOOT.md"
BOOT_STATE_PATH = Path("data") / "boot_state.json"
BOOT_DEBOUNCE_SECONDS = 120


def _load_boot_state() -> dict:
    if not BOOT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(BOOT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_boot_state(state: dict) -> None:
    try:
        BOOT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BOOT_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"[BOOT] Failed to save boot state: {e}")


def _parse_boot_content(raw: str) -> tuple[str, bool]:
    lines = [line.rstrip() for line in raw.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    once = False
    if lines and lines[0].strip().lower() == "@once":
        once = True
        lines = lines[1:]
    # Strip comment-only lines so the default template is treated as empty
    lines = [line for line in lines if not line.lstrip().startswith("#")]
    content = "\n".join(lines).strip()
    return content, once


def _channel_ready_status(channel) -> bool:
    name = getattr(channel, "name", "")
    if name == "discord" and hasattr(channel, "client"):
        return bool(channel.client.is_ready())
    if name == "whatsapp":
        return bool(getattr(channel, "_connected", False))
    if name == "web":
        return True
    return True


async def _wait_for_channels_ready(
    channels: list, timeout: float = 15.0
) -> dict[str, bool]:
    """Wait briefly for core channels to be ready; returns readiness map."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = {
            getattr(c, "name", "unknown"): _channel_ready_status(c) for c in channels
        }
        if all(status.values()):
            return status
        await asyncio.sleep(0.25)
    return {getattr(c, "name", "unknown"): _channel_ready_status(c) for c in channels}


async def _run_boot_hook(bus: MessageBus, channels: list, model: str) -> None:
    """If persona/BOOT.md exists, enqueue it as a high-priority startup task."""
    if os.environ.get("LIMEBOT_SOFT_RESTART") == "1":
        logger.info("[BOOT] Skipping BOOT.md due to soft restart.")
        return

    config = load_config()
    is_local_llm = (
        bool(config.llm.model)
        and (
            "ollama" in str(config.llm.model).lower()
            or "local" in str(config.llm.model).lower()
        )
    ) or bool(getattr(config.llm, "base_url", None))

    if not config.llm.api_key and not is_local_llm:
        logger.info("[BOOT] Skipping BOOT.md during setup (no LLM API key configured).")
        return

    if not BOOT_PATH.exists():
        return

    try:
        raw = BOOT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"[BOOT] Failed to read {BOOT_PATH}: {e}")
        return

    content, once = _parse_boot_content(raw)
    if not content:
        logger.info("[BOOT] BOOT.md is empty; skipping.")
        return

    state = _load_boot_state()
    last_run = state.get("last_run_ts", 0)
    now = time.time()
    if now - last_run < BOOT_DEBOUNCE_SECONDS:
        logger.info("[BOOT] Debounced BOOT.md (recent run).")
        return

    status = await _wait_for_channels_ready(channels)

    from core.events import InboundMessage

    await bus.publish_inbound(
        InboundMessage(
            channel="web",
            sender_id="boot",
            chat_id="system",
            content=content,
            metadata={"source": "boot_md"},
        )
    )
    state["last_run_ts"] = now
    _save_boot_state(state)

    if once:
        try:
            BOOT_PATH.write_text("", encoding="utf-8")
            logger.info("[BOOT] @once detected — BOOT.md cleared after enqueue.")
        except Exception as e:
            logger.error(f"[BOOT] Failed to clear BOOT.md: {e}")

    ready_parts = ", ".join(
        f"{k}={'ready' if v else 'not-ready'}" for k, v in status.items()
    )
    logger.info(f"[BOOT] Boot complete (model={model}; {ready_parts}).")
    logger.info("[BOOT] BOOT.md queued for processing.")


async def main():
    loop = asyncio.get_running_loop()
    install_windows_asyncio_exception_filter(loop)

    ensure_persona_bootstrap_files()
    config = load_config()
    logger.info("Starting LimeBot...")

    from core.job_queue import get_job_queue
    from core.runtime_paths import get_data_dir

    bus = MessageBus()
    session_manager = SessionManager()
    job_queue = get_job_queue(get_data_dir() / "jobs.sqlite")
    scheduler = CronManager(bus, job_queue=job_queue)

    channels = []

    if config.discord.enabled and config.discord.token:
        discord_channel = DiscordChannel(config.discord, bus)
        channels.append(discord_channel)
        bus.subscribe_outbound(discord_channel.name, discord_channel.send)
        logger.info("Discord channel initialized")

    if config.telegram.enabled:
        telegram_channel = TelegramChannel(config.telegram, bus)
        channels.append(telegram_channel)
        bus.subscribe_outbound(telegram_channel.name, telegram_channel.send)
        logger.info("Telegram channel initialized")
    else:
        logger.info("Telegram channel disabled by config")

    if config.whatsapp.enabled:
        whatsapp_channel = WhatsAppChannel(config.whatsapp, bus)
        channels.append(whatsapp_channel)
        bus.subscribe_outbound(whatsapp_channel.name, whatsapp_channel.send)
        logger.info("WhatsApp channel initialized")
    else:
        logger.info("WhatsApp channel disabled by config")

    web_channel = WebChannel(config, bus, session_manager=session_manager)
    web_channel.set_scheduler(scheduler)
    channels.append(web_channel)
    web_channel.set_channels(channels)
    bus.subscribe_outbound(web_channel.name, web_channel.send)
    logger.info("Web channel initialized")

    agent = AgentLoop(
        bus,
        model=config.llm.model,
        scheduler=scheduler,
        session_manager=session_manager,
    )
    agent.toolbox.set_channels(channels)
    from core import prompt as prompt_module

    if prompt_module.is_setup_complete():
        asyncio.create_task(agent._warm_up_services())

    for c in channels:
        if hasattr(c, "set_agent"):
            c.set_agent(agent)

    async def init_background_services():
        from core.reflection import get_reflection_service

        get_reflection_service(bus, model=config.llm.model)

        existing_jobs = await scheduler.list_jobs()
        if not any(j.get("payload") == "@reflect_and_distill" for j in existing_jobs):
            await scheduler.add_job(
                trigger_time=None,
                message="@reflect_and_distill",
                context={
                    "channel": "system",
                    "chat_id": "global_reflection",
                    "sender_id": "maintenance",
                },
                cron_expr="0 */4 * * *",
            )
            logger.info("Reflective background task scheduled")

        system_channels = []
        if config.discord.enabled:
            discord_chat_id = (
                config.discord.allow_channels[0]
                if config.discord.allow_channels
                else "primary"
            )
            system_channels.append({"channel": "discord", "chat_id": discord_chat_id})

        if config.telegram.enabled:
            telegram_chat_id = (
                config.telegram.allow_chats[0]
                if config.telegram.allow_chats
                else "primary"
            )
            system_channels.append({"channel": "telegram", "chat_id": telegram_chat_id})

        if config.whatsapp.enabled:
            system_channels.append({"channel": "whatsapp", "chat_id": "primary"})

        if system_channels and getattr(config.llm, "enable_dynamic_personality", False):
            await scheduler.register_system_jobs(system_channels)
            logger.info("Proactive system jobs registered")
        elif system_channels:
            logger.info(
                "Dynamic personality disabled; skipping proactive system jobs registration"
            )

    asyncio.create_task(init_background_services())

    tasks = []

    async def _recover_durable_jobs():
        from core.task_runs import get_task_run_store

        recovered_runs = get_task_run_store().recover_interrupted()
        for run in recovered_runs:
            # The task-run checkpoint is authoritative; carry its resume
            # marker into the durable inbound envelope before publishing it.
            job_id = str(
                (run.metadata or {}).get("durable_job_id") or run.run_id
            ).strip()
            job_queue.update_payload_metadata(
                job_id,
                {"task_run_id": run.run_id, "task_run_resume": True},
            )
        recovered = job_queue.recover_interrupted()
        ready = job_queue.list_ready()
        for job in ready:
            await bus.publish_inbound(job.to_message())
        if recovered or ready:
            logger.info(
                "Durable queue recovered %s interrupted job(s), %s task run(s), and re-queued %s ready job(s).",
                len(recovered),
                len(recovered_runs),
                len(ready),
            )

    tasks.append(asyncio.create_task(scheduler.run()))
    tasks.append(asyncio.create_task(bus.dispatch_outbound()))
    tasks.append(asyncio.create_task(_recover_durable_jobs()))
    tasks.append(asyncio.create_task(agent.run()))
    tasks.append(asyncio.create_task(_run_boot_hook(bus, channels, config.llm.model)))

    for channel in channels:
        tasks.append(asyncio.create_task(channel.start()))

    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
    else:

        async def wakeup():
            while not stop_event.is_set():
                await asyncio.sleep(1)

        tasks.append(asyncio.create_task(wakeup()))

    await stop_event.wait()

    logger.info("Shutting down...")

    bus.stop()
    await agent.stop()

    for channel in channels:
        await channel.stop()

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("LimeBot stopped")


if __name__ == "__main__":
    try:
        enforce_supported_python_runtime()
        configure_asyncio_runtime()
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback

        traceback.print_exc()
        logger.exception("Fatal error during startup")
        sys.exit(1)
