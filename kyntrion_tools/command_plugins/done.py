"""KYNTRION .done command plugin."""

from .contracts import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> None:
    ctx.delegate_private("/done")


GUIDE = (
        "✅ Завершение сбора (.done / .готово, также .txt / .тхт или /done)\n"
        "1. Начни сбор командой .сбор, затем перешли нужные сообщения.\n"
        "2. Отправь .готово — бот соберёт содержимое в один TXT по порядку.\n"
        "3. После успешной выдачи текущий сбор очистится. Если частей ещё нет, бот скажет об этом.\n"
        "Пример: .сбор → текст → текст → .готово."
    )

SPEC = CommandSpec(
    topic="done",
    aliases=(".done", ".готово",),
    category="▫️ Управление сбором",
    summary="завершить сбор и получить TXT",
    guide=GUIDE,
    handler=handle,
)
