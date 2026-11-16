"""ZIP tools for Telegram attachments. Payloads never touch the filesystem."""
from io import BytesIO
from pathlib import PurePosixPath
import re
import stat
import time
import zipfile
import zlib

from .contracts import CommandSpec

LIMIT = 20 * 1024 * 1024
COUNT = 32
ZIP_GUIDE = "Пример: ответь на файл командой .zip или введи .zip start и пришли файлы в личку. Заверши командой .zip done или отмени через .zip cancel."
UNZIP_GUIDE = "Пример: ответь на ZIP командой .unzip. До 32 файлов и 20 МиБ суммарно."


def attachment(message):
    for key, ext in (("document", "bin"), ("photo", "jpg"), ("audio", "mp3"),
                     ("video", "mp4"), ("voice", "ogg"), ("video_note", "mp4"),
                     ("animation", "gif"), ("sticker", "webp")):
        media = message.get(key)
        if key == "photo" and isinstance(media, list) and media:
            media = media[-1]
        if isinstance(media, dict) and media.get("file_id"):
            name = media.get("file_name") or f"{key}_{message.get('message_id', 0)}.{ext}"
            return media["file_id"], name, media.get("file_size", 0)


def safe_name(path):
    parts = path.replace("\\", "/").split("/")
    if any(not p or p in (".", "..") or re.search(r'[\x00-\x1f\x7f:"<>|?*]', p) for p in parts):
        raise ValueError("Недопустимое имя файла.")
    return parts[-1]


def unique_name(name, used):
    stem, suffix = PurePosixPath(name).stem, PurePosixPath(name).suffix
    candidate = name
    index = 1
    while candidate.casefold() in used:
        index += 1
        candidate = f"{stem} ({index}){suffix}"
    used.add(candidate.casefold())
    return candidate


def download(api, item, remaining):
    fid, _, size = item
    if size > remaining:
        raise ValueError("Превышен лимит 20 МиБ.")
    info = api.get_file(fid)
    if info.get("file_size", 0) > remaining:
        raise ValueError("Превышен лимит 20 МиБ.")
    return api.download_file(info["file_path"], max_bytes=remaining)


def pack(api, chat, items):
    output = BytesIO()
    remaining = LIMIT
    used = set()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for item in items:
            name = unique_name(safe_name(item[1]), used)
            data = download(api, item, remaining)
            remaining -= len(data)
            archive.writestr(name, data)
    api.send_document(chat, "archive.zip", output.getvalue(), "Готово", mime_type="application/zip")


def unpack(api, chat, item):
    data = download(api, item, LIMIT)
    # Bound central directory parsing before ZipFile allocates one object per entry.
    end = zipfile._EndRecData(BytesIO(data))
    if not end or end[4] > COUNT or end[5] > 64 * 1024:
        raise ValueError("Некорректный ZIP или слишком много записей.")
    files = []
    remaining = LIMIT
    used = set()
    with zipfile.ZipFile(BytesIO(data)) as archive:
        for entry in archive.infolist():
            name = safe_name(entry.orig_filename.rstrip("/") if entry.is_dir() else entry.orig_filename)
            if entry.is_dir():
                continue
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (mode not in (0, stat.S_IFREG) or entry.flag_bits & 1 or
                    entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or
                    entry.file_size > remaining or len(files) >= COUNT):
                raise ValueError("Архив превышает лимиты или содержит неподдерживаемые записи.")
            with archive.open(entry) as stream:
                body = stream.read(remaining + 1)
            if len(body) != entry.file_size:
                raise ValueError("Повреждённый ZIP.")
            remaining -= len(body)
            files.append((unique_name(name, used), body))
    if not files:
        raise ValueError("В архиве нет файлов.")
    for name, body in files:
        api.send_document(chat, name, body, "", mime_type="application/octet-stream")


class ArchiveSessions:
    def __init__(self):
        self.active = {}

    def prune(self):
        now = time.monotonic()
        for chat, (deadline, _) in list(self.active.items()):
            if deadline <= now:
                del self.active[chat]

    def collect(self, api, message):
        self.prune()
        chat = message.get("chat") or {}
        uid = chat.get("id")
        if chat.get("type") != "private" or uid != (message.get("from") or {}).get("id") or uid not in self.active:
            return False
        item = attachment(message)
        if not item:
            return False
        entries = self.active[uid][1]
        mid = message.get("message_id")
        if "_vk_part" in message:
            mid = (mid, message["_vk_part"])
        if mid in entries:
            return True
        if len(entries) >= COUNT or sum(x[2] for x in entries.values()) + item[2] > LIMIT:
            api.send_message(uid, "Лимит сбора — 32 файла и 20 МиБ.")
        else:
            entries[mid] = item
            api.send_message(uid, f"В сборе файлов: {len(entries)}.")
        return True


def handle(ctx, unpacking=False):
    sessions = ctx.controller.archive_sessions
    sessions.prune()
    chat = ctx.chat_id
    arg = ctx.argument.strip().lower()
    try:
        if unpacking or not arg:
            item = attachment(ctx.selected_message() or {})
            if not item:
                raise ValueError(UNZIP_GUIDE if unpacking else ZIP_GUIDE)
            if unpacking:
                unpack(ctx.api, chat, item)
            else:
                pack(ctx.api, chat, [item])
        elif arg == "start":
            if chat in sessions.active:
                raise ValueError("Сбор уже идёт. Используй .zip done или .zip cancel.")
            if len(sessions.active) >= 256:
                raise ValueError("Все места для сбора заняты. Попробуй позже.")
            sessions.active[chat] = (time.monotonic() + 1800, {})
            ctx.api.send_message(chat, "Пришли файлы сюда в течение 30 минут, затем введи .zip done.")
        elif arg == "cancel":
            sessions.active.pop(chat, None)
            ctx.api.send_message(chat, "Сбор закрыт.")
        elif arg == "done":
            session = sessions.active.get(chat)
            if not session or not session[1]:
                raise ValueError("Сбор пуст. Введи .zip start и пришли файлы.")
            pack(ctx.api, chat, session[1].values())
            del sessions.active[chat]
        else:
            raise ValueError(ZIP_GUIDE)
    except ValueError as exc:
        ctx.api.send_message(chat, str(exc))
    except (zipfile.BadZipFile, EOFError, zlib.error, NotImplementedError):
        ctx.api.send_message(chat, "Не удалось прочитать ZIP.")


ZIP = CommandSpec("zip", (".zip",), "Архивы", "Упаковать файлы", ZIP_GUIDE, handle,
                  takes_argument=True, validate=lambda a: None if a.strip().lower() in ("", "start", "done", "cancel") else ZIP_GUIDE)
UNZIP = CommandSpec("unzip", (".unzip",), "Архивы", "Распаковать ZIP", UNZIP_GUIDE,
                    lambda ctx: handle(ctx, True))
