"""KYNTRION .cancel command plugin."""

from .contracts import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> None:
    ctx.delegate_private("/cancel")


GUIDE = (
        "❌ Отмена сборки (.cancel / .отмена или /cancel)\n"
        "1. Если начал собирать сообщения по ошибке, отправь .отмена.\n"
        "2. Бот удалит текущие части из оперативной памяти, TXT не выдаётся.\n"
        "3. Чтобы начать заново, используй .сбор. Отменённую сборку восстановить нельзя.\n"
        "Пример: .сбор → лишний текст → .отмена."
    )

SPEC = CommandSpec(
    topic="cancel",
    aliases=(".cancel", ".отмена",),
    category="▫️ Управление сбором",
    summary="отменить сбор",
    guide=GUIDE,
    handler=handle,
)
