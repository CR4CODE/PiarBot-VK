"""Single explicit plugin registry. Help/aliases/actions stay synchronized.

The command generator writes only inside the two marked allowlist sections.
No Telegram text is ever evaluated or used as a Python import path.
"""
from __future__ import annotations

from . import archives
from . import cancel, collect, done, help, love, quote, status, text, txt
from . import type as typing_plugin
# KYNTRION-GENERATED-IMPORTS-BEGIN
# KYNTRION-GENERATED-IMPORTS-END
from .contracts import CommandContext, CommandSpec


PLUGINS: tuple[CommandSpec, ...] = (
    text.SPEC, txt.SPEC, typing_plugin.SPEC, quote.SPEC, love.SPEC, collect.SPEC, done.SPEC, status.SPEC,
    cancel.SPEC, archives.ZIP, archives.UNZIP, help.SPEC,
    # KYNTRION-GENERATED-SPECS-BEGIN
    # KYNTRION-GENERATED-SPECS-END
)


def build_indexes(specs: tuple[CommandSpec, ...]) -> tuple[dict[str, CommandSpec], dict[str, CommandSpec]]:
    by_alias: dict[str, CommandSpec] = {}
    by_topic: dict[str, CommandSpec] = {}
    for spec in specs:
        if not spec.topic or spec.topic in by_topic:
            raise ValueError("Duplicate or empty command topic")
        if not spec.aliases or spec.aliases[0] != "." + spec.topic:
            raise ValueError("Primary alias must match topic")
        if not callable(spec.handler) or not spec.guide or not spec.summary:
            raise ValueError("Incomplete command plugin")
        by_topic[spec.topic] = spec
        for alias in spec.aliases:
            normalized = alias.casefold()
            if (not normalized.startswith(".") or normalized in by_alias
                    or any(c.isspace() for c in normalized)):
                raise ValueError("Invalid or duplicate command alias")
            by_alias[normalized] = spec
    return by_alias, by_topic


BY_ALIAS, BY_TOPIC = build_indexes(PLUGINS)


def execute_private(topic: str, argument: str, api: object, controller: object,
                    chat_id: int, message: dict) -> bool:
    result = BY_TOPIC[topic].handler(CommandContext(
        api=api, controller=controller, chat_id=chat_id,
        message=message, argument=argument,
    ))
    return result is True


def execute_scoped(topic: str, argument: str, api: object, controller: object,
                   owner_id: int, reply: dict | None, *,
                   source_chat_id: int | None = None,
                   business_connection_id: str | None = None,
                   on_complete=None) -> bool:
    result = BY_TOPIC[topic].handler(CommandContext(
        api=api, controller=controller, chat_id=owner_id,
        message={}, argument=argument, scoped=True, reply=reply,
        source_chat_id=source_chat_id,
        business_connection_id=business_connection_id,
        on_complete=on_complete,
    ))
    return result is True


def validate_argument(topic: str, argument: str) -> str | None:
    """Validate text before deleting a scoped command from its source chat."""
    validator = BY_TOPIC[topic].validate
    return validator(argument) if validator is not None else None
