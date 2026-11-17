"""KYNTRION .help command plugin."""

from .contracts import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> None:
    from kyntrion_tools.command_catalog import help_keyboard, help_text
    ctx.api.send_message(ctx.chat_id, help_text(ctx.argument, api=ctx.api),
                         reply_markup=help_keyboard(ctx.argument))


GUIDE = (
        "❓ Справка (.help / .хелп / .помощь)\n"
        "1. Отправь команду в личке бота или в чате, где ему разрешено её удалить.\n"
        "2. В личке откроется список реально реализованных автоматизаций.\n"
        "3. Нажми кнопку с названием команды, чтобы прочитать подробности.\n"
        "4. Нажми «⬅️ К списку команд», чтобы вернуться назад.\n"
        "Пример: .хелп — открыть меню; .хелп текст — сразу открыть инструкцию голосовых."
    )

SPEC = CommandSpec(
    topic="help",
    aliases=(".help", ".хелп", ".помощь",),
    category="▫️ Справка",
    summary="список команд и инструкции",
    guide=GUIDE,
    handler=handle,
    takes_argument=True,
)
