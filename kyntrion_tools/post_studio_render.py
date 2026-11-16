"""Validate and render deterministic, safe Telegram Bot API post actions."""

from __future__ import annotations

from html import escape
import re
from urllib.parse import urlsplit


_MEDIA_ID = re.compile(r"[A-Za-z0-9_-]{6,512}")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
_TAGS = {
    "bold": "b", "italic": "i", "underline": "u",
    "strikethrough": "s", "code": "code",
}


def _content(draft: dict) -> dict:
    if not isinstance(draft, dict):
        raise ValueError("Draft must be a dictionary")
    data = draft.get("content", draft)
    if not isinstance(data, dict):
        raise ValueError("Draft content must be a dictionary")
    return data


def _length(value: str) -> int:
    # Telegram entity positions use UTF-16 code units; counting them is conservative.
    return len(value.encode("utf-16-le")) // 2


def _safe_url(value: object) -> str:
    if (not isinstance(value, str) or len(value) > 2048
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError("Only HTTPS links without control characters are supported")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("Malformed URL") from None
    if (url.scheme != "https" or not url.hostname or not _HOST.fullmatch(url.hostname)
            or url.username is not None or url.password is not None
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Only public HTTPS URLs without credentials are supported")
    return value


def _segments_text(segments: object) -> tuple[str, str]:
    if not isinstance(segments, list) or not segments or len(segments) > 200:
        raise ValueError("Invalid rich text segments")
    pieces: list[str] = []
    plain: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise ValueError("Invalid rich text segment")
        value = segment["text"]
        style = segment.get("style", "")
        escaped = escape(value)
        if style in ("", None):
            pieces.append(escaped)
        elif style == "link":
            url = _safe_url(segment.get("url"))
            pieces.append(f'<a href="{escape(url, quote=True)}">{escaped}</a>')
        elif style in _TAGS:
            tag = _TAGS[style]
            pieces.append(f"<{tag}>{escaped}</{tag}>")
        else:
            raise ValueError("Unsupported text style")
        plain.append(value)
    return "".join(pieces), "".join(plain)


def _utf16_offset_to_index(value: str, offset: int) -> int | None:
    if type(offset) is not int or offset < 0:
        return None
    units = 0
    for index, char in enumerate(value):
        if units == offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > offset:
            return None
    return len(value) if units == offset else None


def segments_from_telegram(value: str, entities: object) -> list[dict] | None:
    """Preserve non-overlapping Telegram-native styles with UTF-16 offsets.

    Unsupported or overlapping entities fall back to literal text, never to
    user-controlled raw HTML. This keeps the editor mobile-friendly and safe.
    """
    if not isinstance(value, str) or not isinstance(entities, list) or not entities:
        return None
    result: list[dict] = []
    index = 0
    supported = {**{name: name for name in _TAGS}, "text_link": "link"}
    for entity in sorted(entities, key=lambda x: x.get("offset", 0)
                         if isinstance(x, dict) else -1):
        if not isinstance(entity, dict) or entity.get("type") not in supported:
            return None
        if type(entity.get("offset")) is not int or type(entity.get("length")) is not int:
            return None
        start = _utf16_offset_to_index(value, entity.get("offset"))
        end_offset = entity.get("offset", -1) + entity.get("length", -1)
        end = _utf16_offset_to_index(value, end_offset)
        if start is None or end is None or start < index or end <= start:
            return None
        if start > index:
            result.append({"text": value[index:start]})
        style = supported[entity["type"]]
        segment = {"text": value[start:end], "style": style}
        if style == "link":
            try:
                segment["url"] = _safe_url(entity.get("url"))
            except ValueError:
                return None
        result.append(segment)
        index = end
    if index < len(value):
        result.append({"text": value[index:]})
    return result


def _text(content: dict, key: str, segments_key: str) -> tuple[str, str]:
    raw = content.get(key, "")
    segments = content.get(segments_key)
    if segments is not None:
        if raw not in ("", None):
            raise ValueError("Choose plain text or formatted segments, not both")
        return _segments_text(segments)
    if not isinstance(raw, str):
        raise ValueError("Text must be a string")
    return escape(raw), raw


def _keyboard(content: dict) -> dict | None:
    rows = content.get("buttons", [])
    if rows is None or rows == []:
        return None
    if not isinstance(rows, list) or len(rows) > 8:
        raise ValueError("URL buttons need at most 8 rows")
    result = []
    count = 0
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 4:
            raise ValueError("URL button row must have 1 to 4 buttons")
        out = []
        for button in row:
            if not isinstance(button, dict):
                raise ValueError("Invalid URL button")
            title = button.get("text")
            if (not isinstance(title, str) or not title.strip()
                    or _length(title) > 64 or any(ord(c) < 32 for c in title)):
                raise ValueError("Invalid button label")
            out.append({"text": title, "url": _safe_url(button.get("url"))})
            count += 1
        result.append(out)
    if count > 32:
        raise ValueError("Too many URL buttons")
    return {"inline_keyboard": result}


def _media(content: dict) -> list[dict]:
    items = content.get("media", [])
    if items in (None, []):
        return []
    if not isinstance(items, list) or len(items) > 10:
        raise ValueError("A post can contain up to 10 media items")
    result = []
    for item in items:
        if (not isinstance(item, dict) or item.get("type") not in ("photo", "video")
                or not isinstance(item.get("file_id"), str)
                or not _MEDIA_ID.fullmatch(item["file_id"])):
            raise ValueError("Only Telegram photo/video file_id references are accepted")
        result.append({"type": item["type"], "media": item["file_id"]})
    return result


def validate_post(draft: dict) -> None:
    content = _content(draft)
    _, raw_text = _text(content, "text", "segments")
    _, raw_caption = _text(content, "caption", "caption_segments")
    media = _media(content)
    _keyboard(content)
    if not raw_text.strip() and not media:
        raise ValueError("Post requires text or media")
    if _length(raw_text) > 4096:
        raise ValueError("Post text exceeds Telegram's 4096-character limit")
    if _length(raw_caption) > 1024:
        raise ValueError("Media caption exceeds Telegram's 1024-character limit")
    if raw_caption and not media:
        raise ValueError("A caption requires media")
    if type(content.get("silent", False)) is not bool:
        raise ValueError("Silent flag must be boolean")


def render_post(draft: dict, *, preview: bool = False) -> list[dict]:
    """Return ordered Bot API send actions without a destination chat_id."""
    validate_post(draft)
    content = _content(draft)
    text_html, text_plain = _text(content, "text", "segments")
    caption_html, caption_plain = _text(content, "caption", "caption_segments")
    media = _media(content)
    keyboard = _keyboard(content)
    silent = content.get("silent", False)
    actions = []

    def add(action: dict):
        if silent:
            action["disable_notification"] = True
        actions.append(action)

    if text_plain.strip():
        message = {"method": "sendMessage", "text": text_html, "parse_mode": "HTML"}
        if keyboard:
            message["reply_markup"] = keyboard
        add(message)

    if len(media) == 1:
        item = media[0]
        kind = "sendPhoto" if item["type"] == "photo" else "sendVideo"
        message = {"method": kind, item["type"]: item["media"]}
        if caption_plain:
            message.update(caption=caption_html, parse_mode="HTML")
        if keyboard and not text_plain.strip():
            message["reply_markup"] = keyboard
        add(message)
    elif len(media) > 1:
        if caption_plain:
            media[0].update(caption=caption_html, parse_mode="HTML")
        add({"method": "sendMediaGroup", "media": media})
        if keyboard and not text_plain.strip():
            add({"method": "sendMessage", "text": "🔗 Ссылки к публикации",
                 "reply_markup": keyboard})

    if preview:
        return [{"method": "sendMessage",
                 "text": "🔎 ПРЕДПРОСМОТР — не опубликовано"}] + actions
    return actions
