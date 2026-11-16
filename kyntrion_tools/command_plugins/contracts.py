"""Contracts for allowlisted KYNTRION command plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class CommandContext:
    api: object
    controller: object
    chat_id: int
    message: dict
    argument: str = ""
    scoped: bool = False
    reply: dict | None = None
    source_chat_id: int | None = None
    business_connection_id: str | None = None
    on_complete: Callable[[bool], None] | None = None

    def selected_message(self) -> dict | None:
        if self.scoped:
            return self.reply if isinstance(self.reply, dict) else None
        selected = self.message.get("reply_to_message")
        return selected if isinstance(selected, dict) else None

    def delegate_private(self, slash_command: str) -> None:
        if self.scoped:
            message = {
                "chat": {"id": self.chat_id, "type": "private"},
                "from": {"id": self.chat_id},
                "text": slash_command,
            }
        else:
            message = {**self.message, "text": slash_command}
        self.controller.handle(message)


@dataclass(frozen=True)
class CommandSpec:
    topic: str
    aliases: tuple[str, ...]
    category: str
    summary: str
    guide: str
    handler: Callable[[CommandContext], bool | None]
    takes_argument: bool = False
    validate: Callable[[str], str | None] | None = None
