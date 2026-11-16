"""Reply-based .quote / .цитата; uses only the Telegram Bot API."""

from __future__ import annotations

from kyntrion_tools.telegram_api import TelegramApiError

from .contracts import CommandContext, CommandSpec


MAX_UNITS = 4096
NO_REPLY = "Ответь командой .quote или .цитата на сообщение, которое хочешь процитировать."
NO_TEXT = "Выделенное сообщение не содержит текста."
TOO_LONG = "Цитата слишком длинная для Telegram (максимум 4096 символов с подписью)."


def _target(ctx: CommandContext) -> int:
    return ctx.source_chat_id if ctx.scoped and ctx.source_chat_id is not None else ctx.chat_id


def _scope(ctx: CommandContext) -> dict:
    return ({"business_connection_id": ctx.business_connection_id}
            if ctx.business_connection_id else {})


def _send(ctx: CommandContext, text: str) -> bool:
    """One send attempt, including on a possibly ambiguous network error."""
    try:
        ctx.api.send_action({
            "method": "sendMessage", "chat_id": _target(ctx),
            "text": text, **_scope(ctx),
        })
    except TelegramApiError:
        if ctx.scoped:
            # Router marks the claimed command uncertain; never retry it.
            raise
        return False
    return True


def _author(message: dict) -> str:
    sender_chat = message.get("sender_chat")
    if isinstance(sender_chat, dict):
        title = sender_chat.get("title")
        if isinstance(title, str) and title.strip():
            return " ".join(title.split())
    sender = message.get("from")
    if isinstance(sender, dict):
        names = [sender.get("first_name"), sender.get("last_name")]
        full_name = " ".join(" ".join(value.split()) for value in names
                             if isinstance(value, str) and value.strip())
        if full_name:
            return full_name
        username = sender.get("username")
        if isinstance(username, str) and username.strip():
            return "@" + username.strip()
    return "Неизвестный автор"


def format_quote(selected: dict) -> str | None:
    original = selected.get("text")
    if not isinstance(original, str) or not original.strip():
        return None
    return f"✨ Цитата:\n\n{original}\n\n— От {_author(selected)}"


def handle(ctx: CommandContext) -> bool | None:
    if not ctx.scoped:
        chat = ctx.message.get("chat") or {}
        sender = ctx.message.get("from") or {}
        message_id = ctx.message.get("message_id")
        if (not isinstance(chat, dict) or not isinstance(sender, dict)
                or chat.get("type") != "private" or chat.get("id") != sender.get("id")
                or type(message_id) is not int or message_id <= 0):
            return None
        try:
            if (getattr(ctx.api, "platform", None) != "vk"
                    and ctx.api.delete_message(ctx.chat_id, message_id) is not True):
                return None
        except TelegramApiError:
            return None
    selected = ctx.selected_message()
    if selected is None:
        _send(ctx, NO_REPLY)
        return None
    text = format_quote(selected)
    if text is None:
        _send(ctx, NO_TEXT)
        return None
    if len(text.encode("utf-16-le")) // 2 > MAX_UNITS:
        _send(ctx, TOO_LONG)
        return None
    delivered = _send(ctx, text)
    # Scoped router treats True as a deferred background task. A quote is
    # synchronous, so None means "finished" and closes its durable claim.
    return None if delivered else False


GUIDE = (
    "✨ Цитата (.quote / .цитата)\n"
    "1. Ответь .quote на текстовое сообщение в доступном чате.\n"
    "2. Бот удалит команду и одним сообщением опубликует текст с именем автора.\n"
    "3. Для медиа без текста придёт подсказка. Лимит Telegram — 4096 символов с подписью.\n"
    "В группе бот должен видеть команду и иметь право её удалить.\n"
    "Пример: ответь .цитата на «Привет» → ✨ Цитата: Привет — От Имя."
)

SPEC = CommandSpec(
    topic="quote",
    aliases=(".quote", ".цитата"),
    category="▫️ Работа с сообщениями",
    summary="оформить сообщение как цитату",
    guide=GUIDE,
    handler=handle,
)
