"""KYNTRION .collect command plugin."""

from .contracts import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> None:
    ctx.delegate_private("/collect")


GUIDE = (
        "📚 Сбор нескольких сообщений (.collect / .сбор, также /collect)\n"
        "1. Начни сбор в личке бота.\n"
        "2. Выдели несколько сообщений в Telegram и перешли боту.\n"
        "3. Заверши .txt / .тхт, .done / .готово или /done.\n"
        "Один TXT содержит элементы по порядку. Данные сборки находятся "
        "в памяти процесса, а не в архиве истории чатов.\n"
        "Пример: .сбор → перешли два текста и одно ГС → .тхт → один TXT."
    )

SPEC = CommandSpec(
    topic="collect",
    aliases=(".collect", ".сбор",),
    category="▫️ Работа с сообщениями",
    summary="начать сбор сообщений",
    guide=GUIDE,
    handler=handle,
)
