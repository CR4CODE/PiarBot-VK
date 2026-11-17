"""KYNTRION .txt command plugin."""

from .contracts import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> None:
    from kyntrion_tools.command_catalog import help_text
    key = (ctx.chat_id, ctx.chat_id if ctx.scoped else
           (ctx.message.get("from") or {}).get("id"))
    if not ctx.scoped and key in ctx.controller.sessions:
        ctx.delegate_private("/done")
        return
    selected = ctx.selected_message()
    if selected is not None:
        ctx.controller.export_selected_to_private(ctx.chat_id, selected)
    elif ctx.scoped:
        # Preserves the legacy group command's no-reply lifecycle behavior.
        ctx.delegate_private("/done")
    else:
        ctx.api.send_message(ctx.chat_id, help_text("txt", api=ctx.api))


GUIDE = (
        "📄 Сообщения → TXT (.txt / .тхт / .файл)\n"
        "Одно: ответь .txt на сообщение в доступном чате или перешли его боту.\n"
        "Несколько: .collect / .сбор → перешли сообщения боту по порядку "
        "→ .txt / .тхт (либо .done). Получишь один файл.\n"
        "Текст сохраняется, ГС расшифровываются; для фото/видео/аудиофайлов "
        "в TXT добавляются подпись и отметка типа, но не сами медиа. "
        "Для оригиналов медиа нужен отдельный ZIP (пока не реализован).\n"
        "Пример: ответь .тхт на сообщение с фото или перешли три сообщения в режиме .сбор и заверши .тхт."
    )

SPEC = CommandSpec(
    topic="txt",
    aliases=(".txt", ".тхт", ".файл",),
    category="▫️ Работа с сообщениями",
    summary="сообщение или сбор → один TXT",
    guide=GUIDE,
    handler=handle,
)
