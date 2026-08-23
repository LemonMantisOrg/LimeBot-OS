"""Base channel interface for chat platforms."""

from abc import ABC, abstractmethod
from typing import Any, List

from core.bus import MessageBus
from core.events import OutboundMessage, InboundMessage


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.
    """

    name: str = "base"

    def __init__(self, config: Any, bus: MessageBus):
        self.config = config
        self.bus = bus
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """Start the channel."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through this channel."""
        pass

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: List[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Handle an incoming message."""
        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=metadata or {},
        )
        from core.job_queue import persist_user_inbound

        persist_user_inbound(msg, kind="chat")
        await self.bus.publish_inbound(msg)

    def is_allowed(self, sender_id: str) -> bool:
        """
        Check if a sender is allowed to use this bot.
        """

        allow_list = getattr(self.config, "allow_from", [])

        if not allow_list:
            return True

        return str(sender_id) in allow_list
