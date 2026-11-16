"""KYNTRION .status command plugin."""

from .contracts import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> None:
    ctx.delegate_private("/status")


GUIDE = (
        "📊 Статус сборки (.status / .статус или /status)\n"
        "1. Во время сбора отправь .статус в личке бота или в поддерживаемом чате.\n"
        "2. Бот покажет число полученных частей и общий размер текста в байтах.\n"
        "3. Проверка ничего не завершает и не удаляет — можно продолжать пересылку.\n"
        "Пример: .сбор → первое сообщение → .статус → второе сообщение → .тхт."
    )

SPEC = CommandSpec(
    topic="status",
    aliases=(".status", ".статус",),
    category="▫️ Управление сбором",
    summary="число и размер собранных частей",
    guide=GUIDE,
    handler=handle,
)
