"""Admin-managed public portfolio for KYNTRION's existing Telegram bot.

All published examples are added by the configured owner. View/interest counts
are aggregate numbers; visitor Telegram IDs are never stored in portfolio rows.
"""

from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

from kyntrion_tools.kyntrion_marketing import MarketingAddon


CATEGORIES = {
    "channel": "📢 Telegram-каналы",
    "post": "📝 Публикации",
    "automation": "🤖 Боты и автоматизация",
    "video": "🎬 Видео и озвучка",
}


def _send(chat_id: int, text: str, *, markup: dict | None = None) -> dict:
    action = {"method": "sendMessage", "chat_id": chat_id, "text": text}
    if markup is not None:
        action["reply_markup"] = markup
    return action


def _keyboard(rows: list[list[tuple[str, str, str]]]) -> dict:
    return {"inline_keyboard": [
        [{"text": title, kind: value} for title, kind, value in row]
        for row in rows
    ]}


def _callback_button(title: str, data: str) -> tuple[str, str, str]:
    return (title, "callback_data", data)


def _url_button(title: str, url: str) -> tuple[str, str, str]:
    return (title, "url", url)


def valid_https_url(url: str) -> bool:
    if not isinstance(url, str) or not 8 <= len(url) <= 500 or any(ch.isspace() for ch in url):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443) or host.lower() == "localhost"
                or host.endswith(".local") or "." not in host):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
        return False
    except ValueError:
        return False


class PortfolioAddon:
    def __init__(self, marketing: MarketingAddon):
        self.marketing = marketing
        self.db = marketing.db
        self.owner_id = marketing.config.admin_chat_id
        self.drafts: dict[int, dict] = {}
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS portfolio_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                url TEXT NOT NULL,
                preview_kind TEXT NOT NULL DEFAULT '',
                preview_file_id TEXT NOT NULL DEFAULT '',
                views INTEGER NOT NULL DEFAULT 0,
                interests INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            """
        )
        self.db.commit()

    def list_items(self, category: str | None = None) -> list[dict]:
        if category is None:
            rows = self.db.execute(
                "SELECT * FROM portfolio_items ORDER BY id DESC LIMIT 100"
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM portfolio_items WHERE category=? ORDER BY id DESC LIMIT 100",
                (category,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _item(self, item_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM portfolio_items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row is not None else None

    def process(self, update: dict) -> tuple[bool, list[dict]]:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            data = callback.get("data")
            if not isinstance(data, str) or not (data.startswith("pf:") or data == "mkt:portfolio"):
                return False, []
            query_id = callback.get("id")
            if not isinstance(query_id, str) or not query_id:
                return True, []
            chat = (callback.get("message") or {}).get("chat") or {}
            sender = (callback.get("from") or {}).get("id")
            ack = {"method": "answerCallbackQuery", "callback_query_id": query_id}
            if chat.get("type") != "private" or not isinstance(sender, int) or chat.get("id") != sender:
                return True, [{**ack, "text": "Кнопка недоступна в этом чате."}]
            if data == "mkt:portfolio":
                return True, [ack, self._home(sender)]
            return True, [ack, *self._callback(sender, data)]

        message = update.get("message")
        if not isinstance(message, dict):
            return False, []
        chat = message.get("chat") or {}
        sender = (message.get("from") or {}).get("id")
        if chat.get("type") != "private" or chat.get("id") != sender or not isinstance(sender, int):
            return False, []
        if any(key in message for key in ("forward_origin", "forward_from", "forward_date")):
            return False, []
        body = message.get("text")
        command = body.split(maxsplit=1)[0].split("@", 1)[0].lower() if isinstance(body, str) and body.startswith("/") else ""
        if command == "/portfolio":
            return True, [self._home(sender)]
        if command == "/portfolio_manage":
            return True, [self._admin_menu(sender) if sender == self.owner_id
                          else _send(sender, "Команда доступна только владельцу.")]
        if command == "/portfolio_stats":
            return True, [self._stats(sender) if sender == self.owner_id
                          else _send(sender, "Команда доступна только владельцу.")]
        if command == "/cancel_portfolio":
            self.drafts.pop(sender, None)
            return True, [_send(sender, "Создание примера отменено. Открой /portfolio_manage.")]
        if sender == self.owner_id and sender in self.drafts:
            return True, [self._wizard_message(sender, message)]
        return False, []

    def _home(self, user_id: int) -> dict:
        counts = {category: len(self.list_items(category)) for category in CATEGORIES}
        rows = [[_callback_button(f"{label} · {counts[key]}", f"pf:cat:{key}")]
                for key, label in CATEGORIES.items()]
        if user_id == self.owner_id:
            rows.append([_callback_button("⚙️ Управление портфолио", "pf:admin")])
        return _send(user_id, "🎨 Портфолио KYNTRION\n\nОткрой категорию, чтобы увидеть проекты и перейти к оригиналам.\n"
                            "Новые примеры будем добавлять по мере публикации.",
                     markup=_keyboard(rows))

    def _category(self, user_id: int, category: str) -> dict:
        if category not in CATEGORIES:
            return _send(user_id, "Категория недоступна. Открой /portfolio.")
        items = self.list_items(category)[:10]
        rows = [[_callback_button(item["title"][:45], f"pf:item:{item['id']}")]
                for item in items]
        rows.append([_callback_button("⬅️ Все категории", "pf:home")])
        text = CATEGORIES[category] + "\n\n"
        if items:
            text += "\n".join(f"• {item['title']} — {item['description'][:80]}" for item in items)
        else:
            text += "Реальные примеры здесь пока готовятся. Скоро добавим опубликованные работы."
        return _send(user_id, text, markup=_keyboard(rows))

    def _show_item(self, user_id: int, item_id: int) -> dict:
        item = self._item(item_id)
        if item is None:
            return _send(user_id, "Этот пример больше недоступен. Открой /portfolio.")
        with self.db:
            self.db.execute("UPDATE portfolio_items SET views=views+1 WHERE id=?", (item_id,))
        buttons = _keyboard([
            [_url_button("🔗 Открыть работу", item["url"])],
            [_callback_button("💬 Заказать подобное", f"pf:order:{item_id}")],
            [_callback_button("⬅️ К категории", f"pf:cat:{item['category']}")],
        ])
        content = f"🎨 {item['title']}\n\n{item['description']}\n\n🔗 {item['url']}"
        if item["preview_kind"] == "photo" and item["preview_file_id"]:
            return {"method": "sendPhoto", "chat_id": user_id,
                    "photo": item["preview_file_id"], "caption": content[:950], "reply_markup": buttons}
        if item["preview_kind"] == "video" and item["preview_file_id"]:
            return {"method": "sendVideo", "chat_id": user_id,
                    "video": item["preview_file_id"], "caption": content[:950], "reply_markup": buttons}
        return _send(user_id, content, markup=buttons)

    def _stats(self, user_id: int) -> dict:
        items = self.db.execute(
            "SELECT title, views, interests FROM portfolio_items "
            "ORDER BY views DESC, interests DESC LIMIT 20"
        ).fetchall()
        if not items:
            return _send(user_id, "Портфолио пока пусто. Добавь первую работу через /portfolio_manage.")
        lines = [f"• {row['title']}: {row['views']} просмотров карточки, "
                 f"{row['interests']} интересов к заказу" for row in items]
        return _send(user_id, "📊 Интерес к кейсам (обезличенные события):\n\n" + "\n".join(lines))

    def _admin_menu(self, user_id: int) -> dict:
        rows = [[_callback_button(f"➕ {name}", f"pf:add:{key}")]
                for key, name in CATEGORIES.items()]
        rows.append([_callback_button("🗑 Удалить пример", "pf:manage_list")])
        rows.append([_callback_button("📊 Статистика", "pf:stats")])
        return _send(user_id, "⚙️ Управление портфолио\n\nДобавить пример: выбери категорию. "
                            "Я по очереди попрошу название, описание, HTTPS-ссылку и превью.",
                     markup=_keyboard(rows))

    def _manage_list(self, user_id: int) -> dict:
        rows = [[_callback_button(f"🗑 {item['title'][:45]}", f"pf:remove:{item['id']}")]
                for item in self.list_items()[:20]]
        rows.append([_callback_button("⬅️ Назад", "pf:admin")])
        return _send(user_id, "Выбери пример для удаления (понадобится подтверждение):",
                     markup=_keyboard(rows))

    def _callback(self, user_id: int, data: str) -> list[dict]:
        if data == "pf:home":
            return [self._home(user_id)]
        if data.startswith("pf:cat:"):
            return [self._category(user_id, data[7:])]
        if data.startswith("pf:item:"):
            item_id = self._parse_id(data[8:])
            return [self._show_item(user_id, item_id)] if item_id else [self._home(user_id)]
        if data.startswith("pf:order:"):
            item_id = self._parse_id(data[9:])
            item = self._item(item_id) if item_id else None
            if item is None:
                return [_send(user_id, "Этот пример больше недоступен. Открой /portfolio.")]
            self.marketing.begin_portfolio_order(user_id, item_id, item["category"])
            with self.db:
                self.db.execute("UPDATE portfolio_items SET interests=interests+1 WHERE id=?", (item_id,))
            return [_send(user_id, f"💬 Хочу подобное: {item['title']}\n\n"
                                  "Опиши свою задачу, сроки и удобный способ связи. "
                                  "Не отправляй пароли или токены. Отмена: /cancel_order")]
        admin_commands = ("pf:admin", "pf:stats", "pf:manage_list")
        admin_action = data in admin_commands or data.startswith(("pf:add:", "pf:remove:", "pf:confirm_remove:"))
        if admin_action and user_id != self.owner_id:
            return []
        if data == "pf:admin":
            return [self._admin_menu(user_id)]
        if data == "pf:stats":
            return [self._stats(user_id)]
        if data == "pf:manage_list":
            return [self._manage_list(user_id)]
        if data.startswith("pf:add:"):
            category = data[7:]
            if category not in CATEGORIES:
                return [_send(user_id, "Неизвестная категория. /portfolio_manage")]
            self.drafts[user_id] = {"category": category, "step": "title"}
            return [_send(user_id, "✍️ Напиши название кейса (до 80 символов). Отмена: /cancel_portfolio")]
        if data.startswith("pf:remove:"):
            item_id = self._parse_id(data[10:])
            item = self._item(item_id) if item_id else None
            if item is None:
                return [_send(user_id, "Пример уже удалён.")]
            return [_send(user_id, f"Удалить «{item['title']}» без возможности восстановления?",
                          markup=_keyboard([
                              [_callback_button("🗑 Да, удалить", f"pf:confirm_remove:{item_id}")],
                              [_callback_button("Отмена", "pf:manage_list")],
                          ]))]
        if data.startswith("pf:confirm_remove:"):
            item_id = self._parse_id(data[18:])
            if item_id:
                with self.db:
                    self.db.execute("DELETE FROM portfolio_items WHERE id=?", (item_id,))
            return [_send(user_id, "Пример удалён. Открой /portfolio_manage для остальных работ.")]
        return [_send(user_id, "Кнопка устарела. Открой /portfolio.")]

    @staticmethod
    def _parse_id(raw: str) -> int | None:
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw):
            return None
        return int(raw)

    def _wizard_message(self, user_id: int, message: dict) -> dict:
        draft = self.drafts[user_id]
        body = message.get("text")
        step = draft["step"]
        if step == "title":
            if not isinstance(body, str) or not 1 <= len(body.strip()) <= 80:
                return _send(user_id, "Напиши название короче — от 1 до 80 символов.")
            draft["title"] = body.strip()
            draft["step"] = "description"
            return _send(user_id, "📝 Теперь описание проекта (до 400 символов).")
        if step == "description":
            if not isinstance(body, str) or not 1 <= len(body.strip()) <= 400:
                return _send(user_id, "Опиши работу короче — до 400 символов.")
            draft["description"] = body.strip()
            draft["step"] = "url"
            return _send(user_id, "🔗 Отправь прямую HTTPS-ссылку на опубликованный кейс или пост.")
        if step == "url":
            if not isinstance(body, str) or not valid_https_url(body.strip()):
                return _send(user_id, "Нужна действительная ссылка, начинающаяся с https:// (без пароля и пробелов).")
            draft["url"] = body.strip()
            draft["step"] = "preview"
            return _send(user_id, "🖼 Пришли фото/видео для предпросмотра или /skip, если пока без превью.")
        if step == "preview":
            preview_kind = preview_file_id = ""
            photos = message.get("photo")
            video = message.get("video")
            if isinstance(photos, list) and photos and isinstance(photos[-1], dict):
                preview_kind = "photo"
                preview_file_id = photos[-1].get("file_id", "")
            elif isinstance(video, dict):
                preview_kind = "video"
                preview_file_id = video.get("file_id", "")
            elif body != "/skip":
                return _send(user_id, "Пришли фото, видео или команду /skip.")
            if preview_kind and (not isinstance(preview_file_id, str)
                                 or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", preview_file_id)):
                return _send(user_id, "Telegram не предоставил корректный ID файла. Пришли превью ещё раз.")
            with self.db:
                cursor = self.db.execute(
                    "INSERT INTO portfolio_items(category,title,description,url,preview_kind,preview_file_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (draft["category"], draft["title"], draft["description"], draft["url"],
                     preview_kind, preview_file_id, int(time.time())),
                )
            self.drafts.pop(user_id, None)
            return _send(user_id, f"✅ Кейс опубликован! ID #{cursor.lastrowid}. Открой /portfolio, чтобы проверить.")
        self.drafts.pop(user_id, None)
        return _send(user_id, "Черновик сброшен. Открой /portfolio_manage.")
