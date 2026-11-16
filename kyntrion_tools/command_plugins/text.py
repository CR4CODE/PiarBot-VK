"""KYNTRION .text command plugin."""

from .contracts import CommandContext, CommandSpec


def handle(ctx: CommandContext) -> None:
    from kyntrion_tools.command_catalog import help_text
    selected = ctx.selected_message()
    voice = selected.get("voice") if selected is not None else None
    if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
        ctx.controller.transcribe_voice_to_private(ctx.chat_id, voice)
    else:
        ctx.api.send_message(ctx.chat_id, help_text("text"))


GUIDE = (
        "🎙 Голос → текст (.text / .текст)\n"
        "1. В доступном чате ответь командой на голосовое.\n"
        "2. После подтверждённого удаления команды бот расшифрует голосовое.\n"
        "3. Текст и TXT придут только тебе в личку.\n"
        "Или просто перешли голосовое боту. Лимит: 3 минуты, 20 МБ.\n"
        "Пример: ответь на голосовое сообщением .текст; результат придёт в диалог с @KyntrionBot."
    )

SPEC = CommandSpec(
    topic="text",
    aliases=(".text", ".текст",),
    category="▫️ Работа с сообщениями",
    summary="голосовое → текст в личку",
    guide=GUIDE,
    handler=handle,
)
