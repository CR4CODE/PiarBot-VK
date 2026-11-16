"""Message-to-TXT business logic for KYNTRION Text Bot.

Works with any adapter implementing send_message and send_document.
Text is kept in memory only for the optional collection mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from kyntrion_tools.bot_menu import inline_keyboard, reply_keyboard
from kyntrion_tools.command_catalog import help_keyboard, help_text, parse_dot
from kyntrion_tools.command_plugins.archives import ArchiveSessions
from kyntrion_tools.command_plugins.registry import execute_private
from kyntrion_tools.telegram_api import TelegramApiError


MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_PARTS = 1000
MAX_VOICE_DURATION = 180
MAX_VOICE_BYTES = 20 * 1024 * 1024


class BotApi(Protocol):
    def send_message(self, chat_id: int, text: str, reply_to_message_id: int | None = None,
                     *, reply_markup: dict | None = None) -> None: ...

    def send_document(
        self,
        chat_id: int,
        filename: str,
        contents: bytes,
        caption: str,
        reply_to_message_id: int | None = None,
        *,
        mime_type: str = "text/plain; charset=utf-8",
        reply_markup: dict | None = None,
    ) -> None: ...

    def get_file(self, file_id: str) -> dict: ...

    def download_file(self, file_path: str, *, max_bytes: int) -> bytes: ...

    def transcribe_audio(self, audio: bytes) -> str: ...


def extract_text(message: dict) -> str | None:
    """Return visible plain text, including media captions if present."""
    for field_name in ("text", "caption"):
        value = message.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def media_description(message: dict) -> str | None:
    """Text-only representation; never pretend a TXT contains media bytes."""
    kind = None
    details = ""
    for field, label in (("video", "Видео"), ("photo", "Фото"),
                         ("audio", "Аудиофайл"), ("video_note", "Видеосообщение"),
                         ("document", "Документ")):
        media = message.get(field)
        if isinstance(media, dict) or (field == "photo" and isinstance(media, list) and media):
            kind = label
            if isinstance(media, dict) and isinstance(media.get("duration"), int):
                details = f", {media['duration']} сек."
            break
    if kind is None:
        return None
    caption = extract_text(message)
    return f"[{kind}{details}; оригинал не включён в TXT]" + (f"\n{caption}" if caption else "")


def make_filename(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"context_{now:%Y%m%d_%H%M%S}.txt"


def voice_filename(extension: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"voice_{now:%Y%m%d_%H%M%S}.{extension}"


@dataclass
class TextCollector:
    parts: list[str] = field(default_factory=list)
    size_bytes: int = 0

    def add(self, text: str, max_bytes: int = MAX_TEXT_BYTES) -> bool:
        text_bytes = len(text.encode("utf-8"))
        separator_bytes = 2 if self.parts else 0
        if len(self.parts) >= MAX_PARTS or self.size_bytes + separator_bytes + text_bytes > max_bytes:
            return False
        self.parts.append(text)
        self.size_bytes += separator_bytes + text_bytes
        return True

    def render(self) -> bytes:
        return "\n\n".join(self.parts).encode("utf-8")


class BotController:
    def __init__(self, api: BotApi, *, max_bytes: int = MAX_TEXT_BYTES):
        self.api = api
        self.archive_sessions = ArchiveSessions()
        self.max_bytes = max_bytes
        self.sessions: dict[tuple[int, int], TextCollector] = {}

    def handle(self, message: dict) -> None:
        """Process one Telegram private message without storing its text on disk."""
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if chat.get("type") != "private" or chat.get("id") is None or user.get("id") is None:
            return

        chat_id = chat["id"]
        key = (chat_id, user["id"])
        reply_id = message.get("message_id")
        text = extract_text(message)

        # A forwarded message beginning with a slash is still content.
        is_forwarded = "forward_origin" in message or "forward_from" in message or "forward_date" in message
        command = ""
        if text and text.startswith("/") and not is_forwarded:
            first = text.split(maxsplit=1)[0]
            command = first.partition("@")[0].lower()

        dot = parse_dot(text) if not is_forwarded else None
        if dot is not None:
            action, argument = dot
            execute_private(action, argument, self.api, self, chat_id, message)
            return

        if command == "/help":
            self.api.send_message(chat_id, help_text(),
                                  reply_markup=help_keyboard())
            return

        if command == "/start":
            self.api.send_message(
                chat_id,
                help_text(),
                reply_markup=reply_keyboard(),
            )
            return

        if command == "/collect":
            self.sessions[key] = TextCollector()
            self.api.send_message(chat_id, "📥 Сбор начат. Перешли все части текста по порядку, затем отправь /done.",
                                  reply_markup=inline_keyboard("collect"))
            return

        if command == "/cancel":
            self.sessions.pop(key, None)
            self.api.send_message(chat_id, "🗑 Сбор отменён, текст удалён из памяти.",
                                  reply_markup=inline_keyboard("txt"))
            return

        if command == "/status":
            session = self.sessions.get(key)
            if session is None:
                self.api.send_message(chat_id, "Сбор не запущен. Отправь /collect.")
            else:
                self.api.send_message(chat_id, f"📊 Получено частей: {len(session.parts)}. Размер: {session.size_bytes} байт.",
                                      reply_markup=inline_keyboard("collect"))
            return

        if command == "/done":
            session = self.sessions.get(key)
            if not session or not session.parts:
                self.api.send_message(chat_id, "Пока нет собранного текста. Отправь /collect и перешли сообщения.")
                return
            self._send_txt(chat_id, session.render(), len(session.parts))
            del self.sessions[key]
            return

        voice = message.get("voice")
        if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
            self._process_voice(chat_id, key, reply_id, voice)
            return

        media = media_description(message)
        if media is not None:
            session = self.sessions.get(key)
            if session is not None:
                if session.add(media, self.max_bytes):
                    self.api.send_message(chat_id, f"✅ Медиаописание добавлено как часть №{len(session.parts)}.")
                else:
                    self.api.send_message(chat_id, "⚠️ Лимит сбора. Заверши .txt или /done.")
            else:
                self._send_txt(chat_id, media.encode("utf-8"), 1, reply_id)
            return

        if text is not None:
            session = self.sessions.get(key)
            if session is not None:
                if not session.add(text, self.max_bytes):
                    self.api.send_message(
                        chat_id,
                        "⚠️ Достигнут лимит текста или числа частей. Отправь /done, "
                        "чтобы получить уже собранный TXT, или /cancel для отмены.",
                    )
                    return
                # Acknowledgement prevents confusion; don't send unnecessary files mid-collection.
                self.api.send_message(chat_id, f"✅ Добавлена часть №{len(session.parts)}. Заверши сбор командой /done.",
                                      reply_markup=inline_keyboard("collect"))
            else:
                data = text.encode("utf-8")
                if len(data) > self.max_bytes:
                    self.api.send_message(chat_id, "⚠️ Превышен лимит текста (8 МБ). Раздели его на части.")
                    return
                self._send_txt(chat_id, data, 1, reply_id)
            return

        if message.get("voice") or message.get("audio"):
            self.api.send_message(chat_id, "🎙 Пришли голосовое сообщение Telegram до 3 минут.")
            return

        self.api.send_message(chat_id, "Пришли или перешли текстовое сообщение. Для нескольких частей — /collect.")

    def export_selected_to_private(self, chat_id: int, message: dict) -> None:
        voice = message.get("voice")
        if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
            self.transcribe_voice_to_private(chat_id, voice)
            return
        content = media_description(message) or extract_text(message)
        if content is None:
            self.api.send_message(chat_id, "В выбранном сообщении нет доступного текста. " + help_text("txt"))
            return
        if len(content.encode("utf-8")) > self.max_bytes:
            self.api.send_message(chat_id, "⚠️ Превышен лимит TXT (8 МБ).")
            return
        self._send_txt(chat_id, content.encode("utf-8"), 1)

    def _send_txt(self, chat_id: int, data: bytes, count: int, reply_id: int | None = None) -> None:
        self.api.send_document(
            chat_id,
            make_filename(),
            data,
            f"📄 Готово! Сообщений: {count}. Размер: {len(data):,} байт. Прикрепи TXT к чату ChatGPT.",
            reply_id,
            reply_markup=inline_keyboard("txt"),
        )

    def _download_voice(self, chat_id: int, voice: dict, reply_id: int | None = None,
                        *, announce: bool = True) -> bytes | None:
        if isinstance(voice.get("duration"), (int, float)) and voice["duration"] > MAX_VOICE_DURATION:
            self.api.send_message(chat_id, "⚠️ Голосовое длиннее 3 минут. Пришли короткую запись.", reply_id)
            return
        if isinstance(voice.get("file_size"), int) and voice["file_size"] > MAX_VOICE_BYTES:
            self.api.send_message(chat_id, "⚠️ Голосовое превышает лимит 20 МБ.", reply_id)
            return

        if announce:
            self.api.send_message(chat_id, "🎙 Голосовое получено. Скачиваю аудио и распознаю речь…", reply_id)
        try:
            info = self.api.get_file(voice["file_id"])
            if isinstance(info.get("file_size"), int) and info["file_size"] > MAX_VOICE_BYTES:
                raise TelegramApiError("Аудио превышает лимит 20 МБ")
            file_path = info.get("file_path")
            if not isinstance(file_path, str) or not file_path:
                raise TelegramApiError("Telegram не предоставил путь к аудио")
            audio = self.api.download_file(file_path, max_bytes=MAX_VOICE_BYTES)
            if not audio:
                raise TelegramApiError("Telegram вернул пустое аудио")
        except TelegramApiError:
            self.api.send_message(chat_id, "⚠️ Не удалось скачать голосовое. Попробуй отправить его снова.")
            return
        return audio

    def _transcribe(self, voice: dict, audio: bytes) -> str:
        if getattr(self.api, "platform", None) == "vk" and voice.get("transcript"):
            return voice["transcript"]
        return self.api.transcribe_audio(audio)

    def transcribe_voice_to_private(self, chat_id: int, voice: dict) -> None:
        """Send text/TXT to the caller-verified private chat, never collecting or sending audio."""
        audio = self._download_voice(chat_id, voice)
        if audio is None:
            return
        try:
            transcript = self._transcribe(voice, audio)
            if not transcript.strip():
                raise TelegramApiError("Пустая расшифровка")
        except TelegramApiError:
            self.api.send_message(chat_id, "⚠️ Не удалось распознать речь. Попробуй позже.")
            return
        self._send_transcript_chunks(chat_id, transcript)
        self._send_transcript_txt(chat_id, transcript)

    def _process_voice(self, chat_id: int, key: tuple[int, int], reply_id: int | None,
                       voice: dict) -> None:
        collecting = key in self.sessions
        audio = self._download_voice(chat_id, voice, reply_id, announce=not collecting)
        if audio is None:
            return

        if collecting:
            try:
                transcript = self._transcribe(voice, audio)
                if not transcript.strip():
                    raise TelegramApiError("Пустая расшифровка")
            except TelegramApiError:
                self.api.send_message(chat_id, "⚠️ Не удалось распознать голосовое; в сбор оно не добавлено.")
                return
            session = self.sessions[key]
            if session.add(transcript, self.max_bytes):
                self.api.send_message(chat_id, f"✅ Голосовое добавлено как часть №{len(session.parts)}.")
            else:
                self.api.send_message(chat_id, "⚠️ Лимит сбора. Заверши .txt или /done.")
            return

        self.api.send_document(
            chat_id, voice_filename("ogg"), audio, "🎧 Оригинал голосового сообщения (.ogg)",
            reply_id, mime_type="audio/ogg",
        )
        try:
            transcript = self._transcribe(voice, audio)
        except TelegramApiError:
            self.api.send_message(chat_id, "⚠️ Не удалось распознать речь. Оригинал аудио уже отправлен; попробуй позже.")
            return

        self._send_transcript_chunks(chat_id, transcript)

        self._send_transcript_txt(chat_id, transcript, reply_id)

    def _send_transcript_chunks(self, chat_id: int, transcript: str) -> None:
        prefix = "📝 Расшифровка:\n\n"
        for offset in range(0, len(transcript), 3500):
            piece = transcript[offset:offset + 3500]
            self.api.send_message(chat_id, prefix + piece if offset == 0 else "📝 Продолжение:\n\n" + piece)

    def _send_transcript_txt(self, chat_id: int, transcript: str, reply_id: int | None = None) -> None:
        self.api.send_document(
            chat_id, voice_filename("txt").replace("voice_", "transcript_", 1),
            transcript.encode("utf-8"), "📝 Расшифровка голосового для ChatGPT", reply_id,
            reply_markup=inline_keyboard("voice"),
        )
