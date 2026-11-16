"""Seven-frame heart animation via the Telegram Bot API.

The same message is edited throughout. No user account or MTProto is needed.
The surrounding router deletes scoped commands and enforces permissions.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Iterator

from kyntrion_tools.telegram_api import TelegramApiError

from .contracts import CommandContext, CommandSpec


PAUSE_SECONDS = 0.8
MAX_CONCURRENT = 2
_lock = threading.Lock()
_active_chats: set[int] = set()

# Keep the animation narrow enough for a compact message bubble (7 × 6).
# Every cell uses explicit emoji presentation: a bare ❤ is text presentation
# on some clients and has a different width from 🤍 and 🖤.
_OUTLINE = (
    "🖤🤍🤍🖤🤍🤍🖤\n"
    "🤍🤍🤍🤍🤍🤍🤍\n"
    "🤍🤍🤍🤍🤍🤍🤍\n"
    "🖤🤍🤍🤍🤍🤍🖤\n"
    "🖤🖤🤍🤍🤍🖤🖤\n"
    "🖤🖤🖤🤍🖤🖤🖤"
)
_LOW_RED = (
    "🖤🤍🤍🖤🤍🤍🖤\n"
    "🤍🤍🤍🤍🤍🤍🤍\n"
    "🤍🤍🤍🤍🤍🤍🤍\n"
    "🖤🤍❤❤❤🤍🖤\n"
    "🖤🖤🤍❤🤍🖤🖤\n"
    "🖤🖤🖤🤍🖤🖤🖤"
)
_CENTER_RED = (
    "🖤🤍🤍🖤🤍🤍🖤\n"
    "🤍🤍🤍🤍🤍🤍🤍\n"
    "🤍🤍❤❤❤🤍🤍\n"
    "🖤🤍❤❤❤🤍🖤\n"
    "🖤🖤🤍❤🤍🖤🖤\n"
    "🖤🖤🖤🤍🖤🖤🖤"
)
_UPPER_RED = (
    "🖤🤍🤍🖤🤍🤍🖤\n"
    "🤍❤❤❤❤❤🤍\n"
    "🤍❤❤❤❤❤🤍\n"
    "🖤🤍❤❤❤🤍🖤\n"
    "🖤🖤🤍❤🤍🖤🖤\n"
    "🖤🖤🖤🤍🖤🖤🖤"
)
_FULL_RED = (
    "🖤❤❤🖤❤❤🖤\n"
    "❤❤❤❤❤❤❤\n"
    "❤❤❤❤❤❤❤\n"
    "🖤❤❤❤❤❤🖤\n"
    "🖤🖤❤❤❤🖤🖤\n"
    "🖤🖤🖤❤🖤🖤🖤"
)

HEART_FRAMES: tuple[str, ...] = tuple(
    frame.replace("❤", "❤️")
    for frame in (_OUTLINE, _LOW_RED, _CENTER_RED, _UPPER_RED, _FULL_RED,
                  _OUTLINE, _FULL_RED)
)


def distinct_frames(frames: Iterable[str]) -> Iterator[str]:
    previous: str | None = None
    for frame in frames:
        if frame != previous:
            yield frame
            previous = frame


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
        # Unknown send outcome must not trigger another message.
        pass


def animate(ctx: CommandContext, *, frames: Iterable[str] = HEART_FRAMES,
            sleeper=time.sleep, delay: float = PAUSE_SECONDS) -> bool:
    """Send the first frame once, edit the same message, stop on any edit error."""
    iterator = iter(distinct_frames(frames))
    try:
        first = next(iterator)
    except StopIteration:
        return False

    chat_id = _target(ctx)
    scope = _scope(ctx)
    try:
        sent = ctx.api.send_action({"method": "sendMessage", "chat_id": chat_id,
                                    "text": first, **scope})
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if type(message_id) is not int or message_id <= 0:
            return False
    except TelegramApiError:
        return False

    for frame in iterator:
        sleeper(delay)
        try:
            ctx.api.send_action({"method": "editMessageText", "chat_id": chat_id,
                                 "message_id": message_id, "text": frame, **scope})
        except TelegramApiError:
            _notice(ctx, "⚠️ Анимация сердца остановлена: не удалось обновить сообщение.")
            return False
    return True


def schedule(ctx: CommandContext) -> bool:
    """Bound the worker count and return immediately to the update polling loop."""
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
            success = animate(ctx)
        except Exception:
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
        threading.Thread(target=work, name="kyntrion-love", daemon=True).start()
    except Exception:
        with _lock:
            _active_chats.discard(chat_id)
        _notice(ctx, "⚠️ Не удалось запустить анимацию сердца.")
        return False
    return True


def handle(ctx: CommandContext) -> bool | None:
    if not ctx.scoped:
        message = ctx.message
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        message_id = message.get("message_id")
        if (chat.get("type") != "private" or chat.get("id") != sender.get("id")
                or type(message_id) is not int or message_id <= 0):
            return None
        if getattr(ctx.api, "platform", None) == "vk":
            return schedule(ctx)
        try:
            deleted = ctx.api.delete_message(ctx.chat_id, message_id)
        except TelegramApiError:
            deleted = False
        if deleted is not True:
            _notice(ctx, "Не удалось удалить команду. Анимация не запускалась.")
            return None
    return schedule(ctx)


GUIDE = (
    "❤️ Сердце (.love / .любовь)\n"
    "1. Напиши .love или .любовь в личном или доступном чате.\n"
    "2. Бот удалит команду и отправит контур сердца из 🤍 на фоне 🖤.\n"
    "3. Красное ❤ постепенно заполнит сердце, затем оно мигнёт и вспыхнет снова.\n"
    "4. Кадры меняются в одном сообщении с паузой около 0,8 секунды.\n"
    "В группе бот должен видеть команду и иметь разрешение на её удаление.\n"
    "Пример: .любовь → 🤍 → ❤ → 🤍 → ❤."
)

SPEC = CommandSpec(
    topic="love",
    aliases=(".love", ".любовь"),
    category="▫️ Работа с сообщениями",
    summary="анимированное сердце ❤️",
    guide=GUIDE,
    handler=handle,
)
