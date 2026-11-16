"""Bounded local archive of Bot-API-accessible media, never ephemeral bypasses."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import uuid


LOG = logging.getLogger("kyntrion_chat_monitor_media")
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
DEFAULT_OWNER_QUOTA = 200 * 1024 * 1024
EXTENSIONS = {"photo": ".jpg", "voice": ".ogg", "video": ".mp4"}
MIME_TYPES = {"voice": "audio/ogg", "video": "video/mp4"}


def _candidate(message: dict) -> tuple[str, str, int | None] | None:
    if message.get("has_protected_content"):
        return None
    photo = message.get("photo")
    if isinstance(photo, list):
        options = [item for item in photo if isinstance(item, dict)
                   and isinstance(item.get("file_id"), str) and item["file_id"]
                   and (item.get("file_size") is None
                        or (type(item["file_size"]) is int
                            and 0 < item["file_size"] <= MAX_PHOTO_BYTES))]
        if options:
            chosen = options[-1]
            return "photo", chosen["file_id"], chosen.get("file_size")
    for kind in ("voice", "video", "video_note"):
        item = message.get(kind)
        if isinstance(item, dict) and isinstance(item.get("file_id"), str) and item["file_id"]:
            return "video" if kind == "video_note" else kind, item["file_id"], item.get("file_size")
    document = message.get("document")
    if (isinstance(document, dict) and document.get("mime_type") == "video/mp4"
            and isinstance(document.get("file_id"), str) and document["file_id"]):
        return "video", document["file_id"], document.get("file_size")
    return None


def _owner_usage(directory: Path) -> int:
    if not directory.exists():
        return 0
    total = 0
    for entry in directory.iterdir():
        if not entry.is_symlink() and entry.is_file():
            total += entry.stat().st_size
    return total


def capture(api, owner_id: int, message: dict, media_root: str | Path,
            *, quota_bytes: int = DEFAULT_OWNER_QUOTA) -> tuple[str, str] | None:
    """Download only an exposed Bot API file into owner-only, short-lived storage."""
    if (type(owner_id) is not int or owner_id <= 0 or not isinstance(message, dict)
            or type(quota_bytes) is not int or quota_bytes <= 0):
        return None
    item = _candidate(message)
    if item is None:
        return None
    kind, file_id, declared_size = item
    maximum = min(MAX_PHOTO_BYTES if kind == "photo" else MAX_FILE_BYTES, quota_bytes)
    if declared_size is not None and (type(declared_size) is not int
                                      or declared_size <= 0 or declared_size > maximum):
        return None
    try:
        root = Path(media_root)
        if root.is_symlink():
            return None
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner_root = root / str(owner_id)
        if owner_root.is_symlink():
            return None
        owner_root.mkdir(mode=0o700, exist_ok=True)
        remaining = quota_bytes - _owner_usage(owner_root)
        if remaining <= 0 or (declared_size is not None and declared_size > remaining):
            return None
        limit = min(maximum, remaining)
        descriptor = api.get_file(file_id)
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("file_path"), str):
            return None
        data = api.download_file(descriptor["file_path"], max_bytes=limit)
        if not isinstance(data, bytes) or not 0 < len(data) <= limit:
            return None
        dest = owner_root / (uuid.uuid4().hex + EXTENSIONS[kind])
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(dest, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        return kind, str(dest)
    except (OSError, RuntimeError, ValueError, TypeError):
        # Unknown or expired Telegram file: no retry, no logs of content/ids.
        LOG.warning("Available media could not be archived")
        return None


def deliver(api, owner_chat_id: int, kind: str, media_path: str,
            caption: str) -> None:
    """One delivery attempt; the caller claims its event before invoking this."""
    if type(owner_chat_id) is not int or owner_chat_id <= 0:
        raise ValueError("Owner chat ID must be positive")
    if kind not in EXTENSIONS:
        raise ValueError("Unsupported media kind")
    path = Path(media_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Archive media no longer exists")
    if path.suffix != EXTENSIONS[kind]:
        raise ValueError("Wrong media extension")
    maximum = MAX_PHOTO_BYTES if kind == "photo" else MAX_FILE_BYTES
    if not 0 < path.stat().st_size <= maximum:
        raise ValueError("Media exceeds Telegram upload limit")
    contents = path.read_bytes()
    if kind == "photo":
        api.send_photo_bytes(owner_chat_id, path.name, contents, caption[:1024])
    else:
        api.send_document(owner_chat_id, path.name, contents, caption[:1024],
                          mime_type=MIME_TYPES[kind])
