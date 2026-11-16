"""Animated .type text with a block cursor; no Telegram user session needed.

Group/business authorization and command deletion happen in VoiceTextRouter.
Private command deletion happens here before any animation is scheduled.
Only two simultaneous animations run; the polling loop remains responsive.
"""

from __future__ import annotations

import random
import threading
import time

from kyntrion_tools.telegram_api import TelegramApiError

from .contracts import CommandContext, CommandSpec


CURSOR = "█"
MAX_CHARS = 4096
MAX_CONCURRENT = 2
_lock = threading.Lock()
_active_chats: set[int] = set()


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def validate_argument(argument: str) -> str | None:
    if not isinstance(argument, str) or not argument.strip():
        return "Укажи текст: .type ваш_текст"
    text = argument.strip()
    if len(text) > MAX_CHARS or _utf16_units(text) > MAX_CHARS:
        return "Текст слишком длинный: максимум 4096 символов Telegram."
    return None


def frames(text: str):
    """Show each typed character, then remove the cursor exactly once."""
    yield CURSOR
    prefix = ""
    units = 0
    last = CURSOR
    for char in text:
        prefix += char
        units += _utf16_units(char)
        frame = prefix + CURSOR if units < MAX_CHARS else prefix
        yield frame
        last = frame
    if last != prefix:
        yield prefix


def _target(ctx: CommandContext) -> int:
    return ctx.source_chat_id if ctx.scoped and ctx.source_chat_id is not None else ctx.chat_id


def _scope(ctx: CommandContext) -> dict:
    return ({"business_connection_id": ctx.business_connection_id}
            if ctx.business_connection_id else {})


def _notice(ctx: CommandContext, message: str) -> None:
    try:
        ctx.api.send_action({"method": "sendMessage", "chat_id": _target(ctx),
                             "text": message, **_scope(ctx)})
    except TelegramApiError:
        # An ambiguous send outcome must never cause an automatic retry.
        pass


def animate(ctx: CommandContext, text: str, *, sleeper=time.sleep,
            pause=lambda: random.uniform(0.1, 0.3)) -> bool:
    """Send one cursor message and edit only that message. No resends on ambiguity."""
    iterator = iter(frames(text))
    initial = next(iterator)
    chat_id = _target(ctx)
    scope = _scope(ctx)
    try:
        sent = ctx.api.send_action({"method": "sendMessage", "chat_id": chat_id,
                                    "text": initial, **scope})
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if type(message_id) is not int or message_id <= 0:
            return False  # Unknown initial message ID: do not send another.
    except TelegramApiError:
        return False

    last_confirmed = initial
    consecutive_limits = 0
    for frame in iterator:
        sleeper(pause())
        try:
            ctx.api.send_action({"method": "editMessageText", "chat_id": chat_id,
                                 "message_id": message_id, "text": frame, **scope})
            last_confirmed = frame
            consecutive_limits = 0
        except TelegramApiError as error:
            if "429" in str(error):
                # Explicit rate-limit rejection: skip this intermediate frame.
                # A later frame includes the whole prefix, without replay.
                sleeper(1.5)
                consecutive_limits += 1
                if consecutive_limits >= 3:
                    # Stop hammering Telegram; attempt one final full-text edit.
                    break
                continue
            return False
    if last_confirmed != text:
        sleeper(1.5)
        try:
            ctx.api.send_action({"method": "editMessageText", "chat_id": chat_id,
                                 "message_id": message_id, "text": text, **scope})
        except TelegramApiError:
            return False
    return True


def schedule(ctx: CommandContext, text: str) -> bool:
    """Bound in-flight typing animations so one long text cannot block polling."""
    chat_id = _target(ctx)
    with _lock:
        busy = chat_id in _active_chats or len(_active_chats) >= MAX_CONCURRENT
        if not busy:
            _active_chats.add(chat_id)
    if busy:
        _notice(ctx, "⏳ Уже идёт анимация. Дождись её завершения.")
        return False

    def work() -> None:
        success = False
        try:
            success = animate(ctx, text)
        except Exception:
            # Do not leak message contents or tokens into shared bot logs.
            success = False
        finally:
            with _lock:
                _active_chats.discard(chat_id)
            if ctx.on_complete is not None:
                try:
                    ctx.on_complete(success)
                except Exception:
                    pass

    try:
        threading.Thread(target=work, name="kyntrion-type", daemon=True).start()
    except Exception:
        with _lock:
            _active_chats.discard(chat_id)
        _notice(ctx, "⚠️ Не удалось запустить анимацию.")
        return False
    return True


def handle(ctx: CommandContext) -> bool | None:
    warning = validate_argument(ctx.argument)
    if warning:
        _notice(ctx, warning)
        return None
    text = ctx.argument.strip()
    if not ctx.scoped:
        source = ctx.message
        chat = source.get("chat") or {}
        sender = source.get("from") or {}
        message_id = source.get("message_id")
        if (chat.get("type") != "private" or chat.get("id") != sender.get("id")
                or type(message_id) is not int or message_id <= 0):
            return None
        if getattr(ctx.api, "platform", None) == "vk":
            return schedule(ctx, text)
        try:
            deleted = ctx.api.delete_message(ctx.chat_id, message_id)
        except TelegramApiError:
            deleted = False
        if deleted is not True:
            _notice(ctx, "Не удалось удалить команду. Анимация не запускалась.")
            return None
    return schedule(ctx, text)


GUIDE = (
    "⌨️ Печать текста (.type / .тайп)\n"
    "1. Напиши .type Привет в личке или доступном чате.\n"
    "2. Бот удалит команду, создаст █ и постепенно напечатает текст.\n"
    "3. В конце курсор исчезнет. До 4096 символов.\n"
    "В группе бот должен видеть команду и иметь право её удалить.\n"
    "Пример: .тайп Привет → █ → П█ → Пр█ → ... → Привет."
)

SPEC = CommandSpec(
    topic="type",
    aliases=(".type", ".тайп"),
    category="▫️ Работа с сообщениями",
    summary="печать текста с курсором █",
    guide=GUIDE,
    handler=handle,
    takes_argument=True,
    validate=validate_argument,
)
