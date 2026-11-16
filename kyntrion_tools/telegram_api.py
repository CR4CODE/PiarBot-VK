"""Minimal HTTPS adapter for Telegram's Bot API; uses Python stdlib only."""

from __future__ import annotations

import json
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class TelegramApiError(RuntimeError):
    """API/network failure; never includes the bot token."""


class TelegramFloodWait(TelegramApiError):
    """Telegram explicitly rejected a send with retry_after; no delivery occurred."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"API HTTP 429: retry after {retry_after} seconds")


class TelegramApi:
    def __init__(self, token: str, *, gateway_url: str | None = None,
                 voice_asr_url: str | None = None, opener=urlopen):
        if not token or ":" not in token:
            raise ValueError("Нужен корректный TELEGRAM_BOT_TOKEN")
        self._token = token
        if gateway_url:
            self._require_https_base(gateway_url, "TELEGRAM_GATEWAY_URL")
            self._url = gateway_url.rstrip("/") + "/tg/"
            self._file_url = gateway_url.rstrip("/") + "/file/"
            self._headers = {"X-Kyntrion-Bot-Token": token}
        else:
            self._url = f"https://api.telegram.org/bot{token}/"
            self._file_url = f"https://api.telegram.org/file/bot{token}/"
            self._headers = {}
        if voice_asr_url:
            self._require_https_base(voice_asr_url, "VOICE_ASR_URL")
        self._asr_url = voice_asr_url.rstrip("/") + "/" if voice_asr_url else None
        self._opener = opener

    @staticmethod
    def _require_https_base(value: str, name: str) -> None:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError(f"{name} должен быть HTTPS-адресом без пути")

    def _request(self, method: str, data: bytes, content_type: str, timeout: int = 45,
                 *, base_url: str | None = None):
        request = Request(
            (base_url or self._url) + method,
            data=data,
            headers={"Content-Type": content_type, "User-Agent": "KYNTRION-TextBot/1.0",
                     **(self._headers if base_url is None else {"X-Kyntrion-Bot-Token": self._token})},
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                payload = json.load(response)
        except HTTPError as exc:
            body = None
            try:
                body = json.loads(exc.read())
                detail = body.get("description") or body.get("error") or "HTTP error"
            except (ValueError, OSError, AttributeError):
                detail = "HTTP error"
            if exc.code == 429 and isinstance(body, dict):
                parameters = body.get("parameters")
                retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
                if type(retry) is int and 1 <= retry <= 3600:
                    raise TelegramFloodWait(retry) from None
            raise TelegramApiError(f"API HTTP {exc.code}: {str(detail).replace(self._token, '[скрыто]')}") from None
        except (URLError, TimeoutError, OSError):
            raise TelegramApiError("Ошибка сети при запросе к Telegram API") from None
        except (ValueError, UnicodeError):
            raise TelegramApiError("Telegram API вернул неожиданный ответ") from None
        if not payload.get("ok"):
            detail = payload.get("description") or payload.get("error") or "unknown error"
            raise TelegramApiError(f"API: {str(detail).replace(self._token, '[скрыто]')}")
        return payload["result"]

    def _json(self, method: str, params: dict, *, timeout: int = 45):
        return self._request(
            method,
            json.dumps(params, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            timeout,
        )

    def get_me(self) -> dict:
        return self._json("getMe", {})

    @staticmethod
    def _validate_chat_ref(chat_id: int | str) -> None:
        if type(chat_id) is int and chat_id != 0:
            return
        if isinstance(chat_id, str) and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", chat_id):
            return
        raise ValueError("Нужен идентификатор канала или его @username")

    def get_chat(self, chat_id: int | str) -> dict:
        self._validate_chat_ref(chat_id)
        return self._json("getChat", {"chat_id": chat_id})

    def get_chat_member(self, chat_id: int | str, user_id: int) -> dict:
        self._validate_chat_ref(chat_id)
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("Нужен положительный Telegram user_id")
        return self._json("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def get_file(self, file_id: str) -> dict:
        return self._json("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, *, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        if (not isinstance(file_path, str) or not file_path or file_path.startswith("/") or
                not re.fullmatch(r"[A-Za-z0-9_./-]+", file_path) or
                any(part in ("", ".", "..") for part in file_path.split("/"))):
            raise TelegramApiError("Некорректный путь голосового файла")
        request = Request(
            self._file_url + quote(file_path, safe="/"),
            headers={"User-Agent": "KYNTRION-TextBot/1.0", **self._headers},
            method="GET",
        )
        try:
            with self._opener(request, timeout=60) as response:
                audio = response.read(max_bytes + 1)
        except HTTPError as exc:
            raise TelegramApiError(f"Загрузка аудио: HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise TelegramApiError("Не удалось скачать голосовое сообщение") from None
        if len(audio) > max_bytes:
            raise TelegramApiError("Голосовое сообщение превышает лимит 20 МБ")
        return audio

    def transcribe_audio(self, audio: bytes) -> str:
        if not self._asr_url:
            raise TelegramApiError("Распознавание речи пока не настроено")
        result = self._request("transcribe", audio, "audio/ogg", timeout=100,
                               base_url=self._asr_url)
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise TelegramApiError("Речь в аудио не распознана")
        return text.strip()

    def set_commands(self, *, include_marketing: bool = False) -> None:
        # Only the discovery menu is simplified; typed/owner commands still work.
        commands = [{"command": "start", "description": "Открыть KYNTRION"}]
        self._json(
            "setMyCommands",
            {"commands": commands},
        )

    def get_updates(self, offset: int | None, *, include_voice_commands: bool = False,
                    include_chat_monitor: bool = False,
                    poll_timeout: int = 25) -> list[dict]:
        if type(poll_timeout) is not int or poll_timeout not in (1, 25):
            raise ValueError("Polling timeout must be 1 or 25 seconds")
        params = {"timeout": poll_timeout, "allowed_updates": ["message", "callback_query"]}
        if include_voice_commands or include_chat_monitor:
            params["allowed_updates"].extend(["business_connection", "business_message"])
        if include_chat_monitor:
            params["allowed_updates"].extend(
                ["edited_business_message", "deleted_business_messages"])
        if offset is not None:
            params["offset"] = offset
        return self._json("getUpdates", params, timeout=poll_timeout + 10)

    @staticmethod
    def _validate_connection_id(connection_id: str) -> None:
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise ValueError("Нужен непустой business_connection_id")

    def get_business_connection(self, connection_id: str) -> dict:
        self._validate_connection_id(connection_id)
        return self._json("getBusinessConnection", {"business_connection_id": connection_id})

    def delete_business_messages(self, connection_id: str, message_ids: list[int]) -> bool:
        self._validate_connection_id(connection_id)
        if (not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 100 or
                any(type(message_id) is not int or message_id <= 0 for message_id in message_ids)):
            raise ValueError("Нужны от 1 до 100 положительных message_ids")
        return self._json("deleteBusinessMessages", {
            "business_connection_id": connection_id, "message_ids": message_ids})

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        if type(chat_id) is not int or chat_id == 0:
            raise ValueError("Нужен ненулевой chat_id")
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("Нужен положительный message_id")
        return self._json("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def send_message(self, chat_id: int, text: str, reply_to_message_id: int | None = None,
                     *, reply_markup: dict | None = None) -> None:
        params: dict = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            params["reply_parameters"] = {"message_id": reply_to_message_id}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        self._json("sendMessage", params)

    def send_action(self, action: dict) -> object:
        """Send only explicitly supported Bot API actions, including media by file_id."""
        method = action.get("method")
        if method not in ("sendMessage", "answerCallbackQuery", "editMessageText",
                          "sendPhoto", "sendVideo", "sendMediaGroup",
                          "editMessageCaption", "editMessageMedia",
                          "editMessageReplyMarkup", "deleteMessage"):
            raise ValueError("Unsupported Bot API action")
        if method == "sendMediaGroup":
            media = action.get("media")
            if not isinstance(media, list) or not 2 <= len(media) <= 10:
                raise ValueError("Media group requires 2 to 10 file_id objects")
            for item in media:
                self._validate_studio_media(item)
        elif method == "editMessageMedia":
            self._validate_studio_media(action.get("media"))
        # Return the Bot API result so an editing plugin can use the exact
        # message_id of its own initial send; never guess or resend on doubt.
        return self._json(method, {k: v for k, v in action.items() if k != "method"})

    @staticmethod
    def _validate_studio_media(media: object) -> None:
        if (not isinstance(media, dict) or media.get("type") not in ("photo", "video")
                or not isinstance(media.get("media"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{6,512}", media["media"])):
            raise ValueError("Post Studio accepts only Telegram photo/video file_id")

    def send_photo_bytes(self, chat_id: int, filename: str, contents: bytes,
                         caption: str) -> None:
        """Upload one trusted local screenshot without third-party media URLs."""
        if type(chat_id) is not int or chat_id <= 0:
            raise ValueError("Нужен положительный личный chat_id")
        if (not isinstance(filename, str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.(?:jpg|jpeg|png)", filename)):
            raise ValueError("Некорректное имя изображения")
        if not isinstance(contents, bytes) or not 1 <= len(contents) <= 10 * 1024 * 1024:
            raise ValueError("Недопустимый размер изображения")
        if not isinstance(caption, str) or not 1 <= len(caption) <= 1024:
            raise ValueError("Некорректная подпись")
        boundary = "----Kyntrion" + uuid.uuid4().hex
        fields = {"chat_id": str(chat_id), "caption": caption}
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend((
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
                value.encode("utf-8"), b"\r\n",
            ))
        media_type = "image/png" if filename.endswith(".png") else "image/jpeg"
        chunks.extend((
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{filename}\"\r\n".encode(),
            f"Content-Type: {media_type}\r\n\r\n".encode(),
            contents, f"\r\n--{boundary}--\r\n".encode(),
        ))
        self._request("sendPhoto", b"".join(chunks),
                      f"multipart/form-data; boundary={boundary}")

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
    ) -> None:
        boundary = "----Kyntrion" + uuid.uuid4().hex
        fields: dict[str, object] = {"chat_id": chat_id, "caption": caption}
        if reply_to_message_id is not None:
            fields["reply_parameters"] = json.dumps({"message_id": reply_to_message_id})
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        chunks = []
        for name, value in fields.items():
            chunks.extend([
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ])
        chunks.extend([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n".encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            contents,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        self._request(
            "sendDocument",
            b"".join(chunks),
            f"multipart/form-data; boundary={boundary}",
        )
