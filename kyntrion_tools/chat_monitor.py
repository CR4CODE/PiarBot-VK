"""Connected-chat edit/delete alerts for every opted-in KYNTRION bot user."""

from __future__ import annotations

import logging
from pathlib import Path

from kyntrion_tools.monitor_media import capture, deliver
from kyntrion_tools.command_catalog import parse_dot
from kyntrion_tools.monitor_store import MonitorStore, TTL_OPTIONS


LOG = logging.getLogger("kyntrion_chat_monitor")
_PREF_LABELS = {
    "notify_edit": "✏️ Уведомления о правках",
    "notify_delete": "🗑 Уведомления об удалении",
    "capture_media": "🖼 Сохранение доступных медиа",
}


def _id(value: object) -> bool:
    return type(value) is int and value > 0


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _name(user: dict) -> str:
    username = user.get("username")
    if isinstance(username, str) and username and username.replace("_", "").isalnum():
        return "@" + username[:60]
    return "ID " + str(user.get("id"))


def _button(label: str, callback: str) -> dict:
    return {"text": label, "callback_data": callback}


def _kb(*rows) -> dict:
    return {"inline_keyboard": list(rows)}


class ChatMonitor:
    def __init__(self, api, store: MonitorStore, media_root: str | Path):
        self.api = api
        self.store = store
        self.media_root = Path(media_root)

    def forget_owner(self, owner_id: int) -> None:
        self.store.purge_owner(owner_id)

    def page(self, owner_id: int, page: str = "main") -> tuple[str, dict]:
        prefs = self.store.get_prefs(owner_id)
        connected = self.store.get_connections_for_owner(owner_id)
        if page == "help":
            return (
                "📖 Как подключить KYNTRION\n\n"
                "1. Открой настройки Telegram → профиль → «Автоматизация чатов».\n"
                "2. Добавь @KyntrionBot и выбери личные чаты.\n"
                "3. Сохрани настройки — контроль начнётся автоматически.\n\n"
                "Подключая бота к чатам, ты разрешаешь ему временно сохранять входящие "
                "сообщения и доступные медиа из выбранных чатов. Архив хранится на сервере "
                "24 часа по умолчанию (можно выбрать 1 час или 7 дней) и не передаётся ИИ. "
                "Контроль можно выключить, а архив удалить в любой момент в меню бота. "
                "Исходный текст доступен лишь для сообщений, полученных после подключения. "
                "Удаление не сообщает, кто нажал кнопку. Защищённые и "
                "одноразовые самоуничтожающиеся фото/видео не архивируются "
                "через Bot API.",
                _kb([{"text": "🔗 Открыть настройки Telegram", "url": "tg://settings/edit"}],
                    [_button("⬅️ К контролю", "monitor:main")]),
            )
        if page == "erase":
            return ("🧹 Удалить все сохранённые сообщения и медиа этого аккаунта? "
                    "Подключение Telegram сохранится, а контроль выключится.",
                    _kb([_button("🗑 Да, удалить", "monitor:erase_confirm")],
                        [_button("⬅️ Отмена", "monitor:main")]))
        if not connected:
            return (
                "🛡 KYNTRION — Контроль сообщений\n\n"
                "🔴 Бот не подключён к твоему аккаунту.\n"
                "Подключи его через профиль Telegram — мониторинг выбранных чатов "
                "включится автоматически. Входящие сообщения и доступные медиа будут "
                "храниться до 24 часов по умолчанию; контроль можно выключить "
                "и удалить архив. Дополнительная платная подписка не нужна.",
                _kb([{"text": "🔗 Подключить бота", "url": "tg://settings/edit"}],
                    [_button("📖 Инструкция", "monitor:help")],
                    [_button("⬅️ Автоматизация", "auto:menu")]),
            )
        status = "🟢 Включён" if prefs["enabled"] else "🟡 Подключён, контроль выключен"
        desc = (
            "🛡 KYNTRION — Контроль сообщений\n\n"
            f"Статус: {status}\n"
            f"✏️ Правки: {'вкл.' if prefs['notify_edit'] else 'выкл.'}\n"
            f"🗑 Удаления: {'вкл.' if prefs['notify_delete'] else 'выкл.'}\n"
            f"🖼 Медиа: {'вкл.' if prefs['capture_media'] else 'выкл.'}\n"
            f"⏱ Хранение: {prefs['ttl']//3600} ч\n\n"
            "Уведомления только тебе. Исходный текст доступен, если был получен заранее."
        )
        if not prefs["enabled"]:
            rows = [[_button("🔔 Включить контроль", "monitor:activate")]]
        else:
            rows = [[_button("⏸ Выключить контроль", "monitor:disable")]]
        rows.extend([
            [_button(("✅ " if prefs[key] else "☐ ") + label, "monitor:" + key)]
            for key, label in _PREF_LABELS.items()
        ])
        rows.extend([
            [_button("⏱ 1 час", "monitor:ttl:3600"),
             _button("⏱ 24 часа", "monitor:ttl:86400"),
             _button("⏱ 7 дней", "monitor:ttl:604800")],
            [_button("🧹 Очистить историю", "monitor:erase")],
            [_button("📖 Инструкция", "monitor:help")],
            [_button("⬅️ Автоматизация", "auto:menu")],
        ])
        return desc, _kb(*rows)

    def handle_callback(self, callback: dict) -> bool:
        callback = _dict(callback)
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith("monitor:"):
            return False
        sender = _dict(callback.get("from"))
        message = _dict(callback.get("message"))
        chat = _dict(message.get("chat"))
        owner = sender.get("id")
        valid = (
            _id(owner) and chat.get("type") == "private" and chat.get("id") == owner
            and _id(message.get("message_id")) and isinstance(callback.get("id"), str)
            and bool(callback["id"])
        )
        try:
            self.api.send_action({"method": "answerCallbackQuery",
                                  "callback_query_id": callback.get("id", ""),
                                  **({} if valid else {"text": "Открой бота в личном чате."})})
        except (RuntimeError, ValueError):
            pass
        if not valid:
            return True
        key = data[len("monitor:"):]
        page = "main"
        if key == "activate":
            if not self.store.update_pref(owner, "enabled", True):
                page = "help"
        elif key == "disable":
            self.store.update_pref(owner, "enabled", False)
        elif key in _PREF_LABELS:
            prefs = self.store.get_prefs(owner)
            self.store.update_pref(owner, key, not prefs[key])
        elif key.startswith("ttl:"):
            value = key[len("ttl:"):]
            if value.isdecimal() and int(value) in TTL_OPTIONS:
                self.store.update_pref(owner, "ttl", int(value))
        elif key == "erase_confirm":
            self.store.update_pref(owner, "enabled", False)
            self.store.clear_snapshots(owner)
        elif key in ("main", "help", "erase", "consent"):
            page = "help" if key == "consent" else key
        else:
            return True
        text, markup = self.page(owner, page)
        try:
            self.api.send_action({
                "method": "editMessageText", "chat_id": owner,
                "message_id": message["message_id"], "text": text,
                "reply_markup": markup,
            })
        except (RuntimeError, ValueError):
            # Telegram may have accepted an edit before timing out.
            pass
        return True

    def _connection(self, cid: object) -> dict | None:
        if not isinstance(cid, str) or not cid:
            return None
        conn = self.store.get_connection(cid)
        if conn is None:
            try:
                current = self.api.get_business_connection(cid)
            except (RuntimeError, ValueError):
                return None
            if not self.store.record_connection(current):
                return None
            conn = self.store.get_connection(cid)
        return conn if conn is not None and conn["is_enabled"] else None

    def _notify(self, owner: int, connection: dict, key: str, text: str,
                *, record: dict | None = None) -> None:
        if not self.store.claim_event(key, owner):
            return
        text = text[:3900]
        chat_id = connection["user_chat_id"]
        try:
            if (record and record["kind"] and record["media_path"]
                    and Path(record["media_path"]).is_file()):
                deliver(self.api, chat_id, record["kind"], record["media_path"], text[:1000])
            else:
                self.api.send_action({"method": "sendMessage", "chat_id": chat_id, "text": text})
        except (OSError, RuntimeError, ValueError):
            # An unknown delivery outcome is not retried.
            LOG.warning("Monitor notification outcome uncertain; no retry")

    def _incoming(self, data: dict) -> None:
        cid = data.get("business_connection_id")
        conn = self._connection(cid)
        if conn is None:
            return
        owner = conn["owner_id"]
        prefs = self.store.get_prefs(owner)
        chat = _dict(data.get("chat"))
        sender = _dict(data.get("from"))
        mid = data.get("message_id")
        if (not prefs["enabled"] or chat.get("type") != "private"
                or not _id(chat.get("id")) or not _id(mid)
                or not _id(sender.get("id"))
                or data.get("sender_chat") is not None):
            return
        if sender["id"] == owner:
            # The owner's own business dot command is deleted by the command
            # router immediately after this monitor sees the update. Remember
            # only that command so its deletion is not reported as an unknown
            # message; do not archive other outgoing owner messages.
            owner_command = data.get("text")
            if parse_dot(owner_command) is not None:
                self.store.save_snapshot(
                    owner, cid, chat["id"], mid, sender["id"], owner_command,
                    sender_label=_name(sender),
                )
            # An explicit owner reply can expose an ordinary message received
            # before the bot observed it. Never probe Telegram for missing media.
            if not prefs["capture_media"]:
                return
            replied = _dict(data.get("reply_to_message"))
            original_sender = _dict(replied.get("from"))
            original_chat = _dict(replied.get("chat"))
            original_id = replied.get("message_id")
            if (not _id(original_id) or not _id(original_sender.get("id"))
                    or original_sender["id"] == owner
                    or (original_chat and original_chat.get("id") != chat["id"])
                    or self.store.get_snapshot(owner, cid, chat["id"], original_id)):
                return
            archived = capture(self.api, owner, replied, self.media_root)
            if archived is None:
                return
            media_kind, media_path = archived
            original_body = replied.get("text", replied.get("caption", ""))
            if not isinstance(original_body, str):
                original_body = ""
            if not self.store.save_snapshot(
                owner, cid, chat["id"], original_id, original_sender["id"],
                original_body, sender_label=_name(original_sender),
                kind=media_kind, media_path=media_path,
            ):
                try:
                    Path(media_path).unlink(missing_ok=True)
                except OSError:
                    pass
            return
        body = data.get("text", data.get("caption", ""))
        if not isinstance(body, str):
            body = ""
        label = _name(sender)
        kind, media_path = "", ""
        if prefs["capture_media"]:
            archived = capture(self.api, owner, data, self.media_root)
            if archived:
                kind, media_path = archived
        if not self.store.save_snapshot(
            owner, cid, chat["id"], mid, sender["id"], body,
            sender_label=label, kind=kind, media_path=media_path,
        ) and media_path:
            # Duplicate message: do not leak the temporary second archive copy.
            try:
                Path(media_path).unlink(missing_ok=True)
            except OSError:
                pass

    def _edited(self, data: dict, update_id: int) -> None:
        cid = data.get("business_connection_id")
        conn = self._connection(cid)
        if conn is None:
            return
        owner = conn["owner_id"]
        prefs = self.store.get_prefs(owner)
        chat = _dict(data.get("chat"))
        sender = _dict(data.get("from"))
        mid = data.get("message_id")
        if (not prefs["enabled"] or chat.get("type") != "private"
                or not _id(chat.get("id")) or not _id(mid)
                or not _id(sender.get("id")) or sender["id"] == owner):
            return
        new = data.get("text", data.get("caption", ""))
        if not isinstance(new, str):
            new = ""
        record = self.store.get_snapshot(owner, cid, chat["id"], mid)
        old = record["current_body"] if record else None
        if record:
            self.store.update_current(owner, cid, chat["id"], mid, new)
        else:
            # Keep the first version we actually saw, not a fictitious original.
            self.store.save_snapshot(owner, cid, chat["id"], mid, sender["id"], new,
                                     sender_label=_name(sender))
        if not prefs["notify_edit"] or old == new:
            return
        source = record["sender_label"] if record else _name(sender)
        before = old if old is not None else "(исходный текст недоступен)"
        notice = (f"✏️ Сообщение изменено\n👤 {source}\n"
                  f"📝 Было: {before}\n✅ Стало: {new or '(без текста)'}")
        self._notify(owner, conn, f"edit:{update_id}:{cid}:{chat['id']}:{mid}", notice)

    def _deleted(self, data: dict, update_id: int) -> None:
        cid = data.get("business_connection_id")
        conn = self._connection(cid)
        if conn is None:
            return
        owner = conn["owner_id"]
        prefs = self.store.get_prefs(owner)
        chat = _dict(data.get("chat"))
        mids = data.get("message_ids")
        if (not prefs["enabled"] or not prefs["notify_delete"]
                or chat.get("type") != "private" or not _id(chat.get("id"))
                or not isinstance(mids, list)):
            return
        for mid in mids[:100]:
            if not _id(mid):
                continue
            record = self.store.get_snapshot(owner, cid, chat["id"], mid)
            # Bot API deletion events do not identify the actor. Suppress only
            # plain, unedited messages that match a registered dot command;
            # keep normal deletions, unknown commands and media visible.
            if (record and not record["kind"] and not record["media_path"]
                    and record["original_body"] == record["current_body"]
                    and parse_dot(record["current_body"]) is not None):
                continue
            if record:
                last = record["current_body"] or "(медиа без подписи)"
                notice = (f"🗑 Сообщение удалено в подключённом чате\n"
                          f"👤 {record['sender_label'] or ('ID ' + str(record['sender_id']))}\n"
                          f"💬 Перед удалением: {last}")
                original = record["original_body"]
                if original and original != record["current_body"]:
                    notice += f"\n📝 Первый сохранённый вариант: {original}"
            else:
                notice = ("🗑 Сообщение удалено в подключённом чате\n"
                          "💬 Исходный текст недоступен: бот не сохранил сообщение заранее.")
            self._notify(owner, conn, f"del:{update_id}:{cid}:{chat['id']}:{mid}",
                         notice, record=record)

    def handle_update(self, update: dict) -> bool:
        if not isinstance(update, dict):
            return False
        if isinstance(update.get("callback_query"), dict):
            return self.handle_callback(update["callback_query"])
        if "business_connection" in update:
            self.store.record_connection(_dict(update["business_connection"]))
            return True
        data = _dict(update.get("business_message"))
        if "business_message" in update:
            self._incoming(data)
            return False  # Let existing chat command handlers still see messages.
        update_id = update.get("update_id")
        if not _id(update_id):
            return any(key in update for key in ("edited_business_message", "deleted_business_messages"))
        if "edited_business_message" in update:
            self._edited(_dict(update["edited_business_message"]), update_id)
            return True
        if "deleted_business_messages" in update:
            self._deleted(_dict(update["deleted_business_messages"]), update_id)
            return True
        return False
