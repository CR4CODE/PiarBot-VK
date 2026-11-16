"""Opt-in sales flows for an existing Telegram bot.

Framework-neutral: the caller dispatches only the returned Bot API actions.
Messages not owned by this add-on stay with the existing TXT/voice handlers.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SERVICES = {
    "channel": "Канал под ключ",
    "post": "Тексты и изображения",
    "video": "Видео и озвучка",
    "automation": "Боты и автоматизация",
    "whieda": "Услуги для партнёров WHIEDA",
}
FOOTER_INTERVAL = 24 * 60 * 60
MAX_DESCRIPTION = 2000


@dataclass(frozen=True)
class MarketingConfig:
    db_path: Path
    admin_chat_id: int
    owner_username: str = ""
    platform_name: str = "Telegram"

    def __post_init__(self) -> None:
        username = self.owner_username.lstrip("@").strip()
        if username and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{4,31}", username):
            raise ValueError("owner_username must be a Telegram username or empty")
        object.__setattr__(self, "owner_username", username)
        if self.admin_chat_id <= 0:
            raise ValueError("admin_chat_id must be a positive Telegram ID")


def _send(chat_id: int, text: str, **extra: Any) -> dict:
    return {"method": "sendMessage", "chat_id": chat_id, "text": text, **extra}


def _buttons(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": title, "callback_data": callback} for title, callback in row]
            for row in rows
        ]
    }


class MarketingAddon:
    def __init__(self, config: MarketingConfig):
        self.config = config
        Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(config.db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_orders (
                user_id INTEGER PRIMARY KEY,
                service TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS footer_events (
                user_id INTEGER PRIMARY KEY,
                last_shown INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER PRIMARY KEY,
                marketing_opt_in INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS referral_counts (
                source TEXT PRIMARY KEY,
                starts INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # Existing installations have the original pending_orders table. Keep
        # migrations additive so referrals and already started orders survive.
        pending_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(pending_orders)")
        }
        if "portfolio_title" not in pending_columns:
            self.db.execute("ALTER TABLE pending_orders ADD COLUMN portfolio_title TEXT NOT NULL DEFAULT ''")
        if "portfolio_url" not in pending_columns:
            self.db.execute("ALTER TABLE pending_orders ADD COLUMN portfolio_url TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record_referral(self, user_id: int, source: str) -> bool:
        """Count opt-in bot launches without persisting sender identities."""
        if (not isinstance(user_id, int) or user_id <= 0 or not isinstance(source, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", source)):
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO referral_counts(source, starts) VALUES (?, 1) "
                "ON CONFLICT(source) DO UPDATE SET starts=referral_counts.starts+1",
                (source,),
            )
        return True

    def list_orders(self, requester_id: int, limit: int = 10) -> list[dict]:
        if requester_id != self.config.admin_chat_id:
            raise PermissionError("Only the configured owner can view leads")
        rows = self.db.execute(
            "SELECT id, user_id, service, description, created_at "
            "FROM orders ORDER BY id DESC LIMIT ?",
            (min(max(limit, 1), 20),),
        ).fetchall()
        return [dict(row) for row in rows]

    def begin_portfolio_order(self, user_id: int, item_id: int, service: str) -> None:
        """Attribute a new lead to an actual case without changing legacy lead data."""
        if service not in SERVICES or user_id <= 0 or item_id <= 0:
            raise ValueError("Invalid portfolio lead")
        row = self.db.execute(
            "SELECT title, url FROM portfolio_items WHERE id=? AND category=?",
            (item_id, service),
        ).fetchone()
        if row is None:
            raise ValueError("Portfolio example no longer exists")
        with self.db:
            self.db.execute(
                "INSERT INTO pending_orders(user_id,service,portfolio_title,portfolio_url) "
                "VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "service=excluded.service, portfolio_title=excluded.portfolio_title, "
                "portfolio_url=excluded.portfolio_url",
                (user_id, service, row["title"], row["url"]),
            )

    def submit_catalog_order(self, user_id: int, service_key: str, summary: str,
                             *, consultation_id: int | None = None) -> list[dict]:
        """Persist one explicitly confirmed catalog request and notify its owner."""
        if (not isinstance(user_id, int) or user_id <= 0 or service_key not in SERVICES
                or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= MAX_DESCRIPTION):
            raise ValueError("Invalid confirmed catalog order")
        with self.db:
            if consultation_id is not None:
                if consultation_id != user_id:
                    raise ValueError("Consultation owner mismatch")
                active = self.db.execute(
                    "SELECT category FROM consultations WHERE user_id=? AND status='active' AND consent=1",
                    (consultation_id,),
                ).fetchone()
                if active is None or active["category"] != service_key:
                    return []
            inserted = self.db.execute(
                "INSERT INTO orders(user_id,service,description,created_at) VALUES(?,?,?,?)",
                (user_id, service_key, summary.strip(), int(time.time())),
            )
            order_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(orders)")}
            if "origin" in order_columns:
                self.db.execute("UPDATE orders SET origin=? WHERE id=?",
                                ("consultation" if consultation_id is not None else "catalog", inserted.lastrowid))
            if consultation_id is not None:
                self.db.execute(
                    "UPDATE consultations SET status='submitted',order_id=?,updated_at=? "
                    "WHERE user_id=? AND status='active'",
                    (inserted.lastrowid, int(time.time()), consultation_id),
                )
                self.db.execute("DELETE FROM consultation_messages WHERE user_id=?", (consultation_id,))
            self.db.execute("DELETE FROM pending_orders WHERE user_id=?", (user_id,))
        return [
            _send(user_id, "✅ Заявка отправлена! Я свяжусь с тобой для уточнения условий. /contacts"),
            _send(self.config.admin_chat_id,
                  f"🆕 Заявка из каталога KYNTRION\n{self.config.platform_name} ID: {user_id}\n\n{summary.strip()}"),
        ]

    def maybe_footer(self, user_id: int, *, now: int | None = None) -> str:
        """Call after replying to a user request; never schedules or sends messages."""
        if user_id <= 0:
            return ""
        timestamp = int(time.time() if now is None else now)
        row = self.db.execute(
            "SELECT last_shown FROM footer_events WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is not None and timestamp - int(row["last_shown"]) < FOOTER_INTERVAL:
            return ""
        with self.db:
            self.db.execute(
                "INSERT INTO footer_events(user_id, last_shown) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_shown=excluded.last_shown",
                (user_id, timestamp),
            )
        return "\n\n✨ Нужны тексты, оформление канала или видео? Смотри /services"

    def process(self, update: dict) -> tuple[bool, list[dict]]:
        callback = update.get("callback_query")
        if callback:
            data = callback.get("data", "")
            if not isinstance(data, str) or not data.startswith("mkt:"):
                return False, []
            chat = callback.get("message", {}).get("chat", {})
            user_id = callback.get("from", {}).get("id")
            if chat.get("type") != "private" or not isinstance(user_id, int):
                return False, []
            ack = {"method": "answerCallbackQuery", "callback_query_id": callback["id"]}
            if chat.get("id") != user_id:
                return True, [{**ack, "text": "Эта кнопка недоступна в данном чате."}]
            return True, [ack, *self._handle_callback(user_id, data)]

        msg = update.get("message")
        if not isinstance(msg, dict):
            return False, []
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            return False, []
        user_id = msg.get("from", {}).get("id")
        body = msg.get("text")
        if not isinstance(user_id, int) or not isinstance(body, str):
            return False, []
        command = body.split(maxsplit=1)[0].split("@", 1)[0].lower() if body.strip() else ""
        if command == "/services":
            return True, [self._services(user_id)]
        if command == "/order":
            return True, [self._order_menu(user_id)]
        if command == "/contacts":
            return True, [self._contacts(user_id)]
        if command == "/portfolio":
            return True, [self._portfolio(user_id)]
        if command == "/cancel_order":
            with self.db:
                self.db.execute("DELETE FROM pending_orders WHERE user_id=?", (user_id,))
            return True, [_send(user_id, "Оформление заявки отменено. /services")]
        if command == "/forget_me":
            with self.db:
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='consultations'").fetchone():
                    self.db.execute("DELETE FROM consultations WHERE user_id=?", (user_id,))
                for table in ("pending_orders", "orders", "footer_events", "preferences"):
                    self.db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            return True, [_send(user_id, "Данные о ваших заявках и настройках удалены.")]
        if command == "/privacy":
            return True, [_send(user_id, "В личном чате KYNTRION сохраняем Telegram ID "
                                        "и статус сервисных уведомлений. Они включены "
                                        "по умолчанию после вашего обращения к боту: "
                                        "отписаться /news_off, снова включить /news_on. "
                                        "Идентификаторы из групп и чужих Telegram Business чатов "
                                        "для новостей не собираем. "
                                        "Описание заказа и его дата сохраняются для обработки заявки. "
                                        "ИИ-консультация включается по отдельному согласию; "
                                        "её сообщения локальны до отправки заявки или отмены, "
                                        "а ответы могут формироваться ИИ-провайдером. "
                                        "Источники переходов считаются обезличенно. "
                                        "/forget_me удаляет сохранённые данные в функциях бота. "
                                        "Рекламные сообщения отдельно регулируются /marketing_off.")]
        if command == "/marketing_on":
            self._set_opt_in(user_id, True)
            return True, [_send(user_id, "Вы согласились получать предложения. Отключить: /marketing_off")]
        if command == "/marketing_off":
            self._set_opt_in(user_id, False)
            return True, [_send(user_id, "Рекламные предложения отключены.")]
        if command == "/leads":
            if user_id != self.config.admin_chat_id:
                return True, [_send(user_id, "Команда доступна только владельцу.")]
            orders = self.list_orders(user_id)
            if not orders:
                return True, [_send(user_id, "Заявок пока нет.")]
            lines = [f"#{r['id']} — {SERVICES[r['service']]} — ID {r['user_id']}\n"
                     f"{r['description'][:300]}" for r in orders]
            return True, [_send(user_id, "Последние заявки:\n\n" + "\n\n".join(lines))]
        if command == "/campaign_stats":
            if user_id != self.config.admin_chat_id:
                return True, [_send(user_id, "Команда доступна только владельцу.")]
            rows = self.db.execute(
                "SELECT source, starts FROM referral_counts "
                "ORDER BY starts DESC, source LIMIT 20"
            ).fetchall()
            if not rows:
                return True, [_send(user_id, "Переходов по партнёрским ссылкам пока нет.")]
            lines = [f"• {row['source']}: {row['starts']} запусков"
                     for row in rows]
            return True, [_send(user_id, "📊 Источники переходов в KYNTRION Bot:\n\n" + "\n".join(lines))]
        if body.startswith("/"):
            return False, []

        row = self.db.execute(
            "SELECT service, portfolio_title, portfolio_url FROM pending_orders WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            return False, []
        description = body.strip()
        if not description:
            return True, [_send(user_id, "Опиши задачу, пожалуйста. Или /cancel")]
        if len(description) > MAX_DESCRIPTION:
            return True, [_send(user_id, "Напиши, пожалуйста, короче (до 2000 символов).")]
        service = row["service"]
        reference = (f"\n\nПример: {row['portfolio_title']}\n"
                     f"Ссылка: {row['portfolio_url']}") if row["portfolio_url"] else ""
        saved_description = description + reference
        with self.db:
            inserted = self.db.execute(
                "INSERT INTO orders (user_id, service, description, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, service, saved_description, int(time.time())),
            )
            if any(r["name"] == "origin" for r in self.db.execute("PRAGMA table_info(orders)")):
                self.db.execute("UPDATE orders SET origin=? WHERE id=?",
                                ("portfolio" if row["portfolio_url"] else "direct", inserted.lastrowid))
            self.db.execute("DELETE FROM pending_orders WHERE user_id=?", (user_id,))
        admin_notice = (
            f"🆕 Заявка KYNTRION\nУслуга: {SERVICES[service]}\n"
            f"{self.config.platform_name} ID: {user_id}\n\n{saved_description}"
        )
        return True, [
            _send(user_id, "Заявка принята! Я свяжусь с тобой для уточнения деталей. /contacts"),
            _send(self.config.admin_chat_id, admin_notice),
        ]

    def _set_opt_in(self, user_id: int, value: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO preferences (user_id, marketing_opt_in) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET marketing_opt_in=excluded.marketing_opt_in",
                (user_id, int(value)),
            )

    def _handle_callback(self, user_id: int, data: str) -> list[dict]:
        if data == "mkt:services":
            return [self._services(user_id)]
        if data == "mkt:contacts":
            return [self._contacts(user_id)]
        if data == "mkt:portfolio":
            return [self._portfolio(user_id)]
        if data == "mkt:order":
            return [self._order_menu(user_id)]
        if data.startswith("mkt:order:"):
            service = data.rsplit(":", 1)[-1]
            if service not in SERVICES:
                return [_send(user_id, "Неизвестная услуга. Открой /order")]
            with self.db:
                self.db.execute(
                    "INSERT INTO pending_orders(user_id, service) VALUES(?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET service=excluded.service, "
                    "portfolio_title='', portfolio_url=''",
                    (user_id, service),
                )
            return [_send(user_id, f"Выбрано: {SERVICES[service]}\n\n"
                                  "Опиши задачу, сроки и как с тобой связаться. "
                                  "Не отправляй пароли или токены. Отмена: /cancel_order")]
        return [_send(user_id, "Кнопка устарела. Открой /services")]

    def _services(self, user_id: int) -> dict:
        return _send(
            user_id,
            "🚀 KYNTRION — контент и автоматизация\n\n"
            "• Канал под ключ: оформление и первые публикации\n"
            "• Тексты и изображения для продвижения\n"
            "• Видео, Shorts, Reels и озвучка\n"
            "• Боты, парсеры и автопубликация с учётом правил платформ\n\n"
            "Поможем партнёрам WHIEDA и другим небольшим проектам. "
            "Стоимость обсуждаем после короткого описания задачи.",
            reply_markup=_buttons([
                [("📝 Оставить заявку", "mkt:order")],
                [("🎨 Примеры работ", "mkt:portfolio"), ("👤 Контакт", "mkt:contacts")],
            ]),
        )

    def _order_menu(self, user_id: int) -> dict:
        return _send(
            user_id,
            "Выбери услугу — затем расскажи, что нужно сделать:",
            reply_markup=_buttons([
                [("📢 Канал под ключ", "mkt:order:channel")],
                [("✍️ Тексты и картинки", "mkt:order:post")],
                [("🎬 Видео и озвучка", "mkt:order:video")],
                [("🤖 Боты и автоматизация", "mkt:order:automation")],
            ]),
        )

    def _contacts(self, user_id: int) -> dict:
        contact = f"@{self.config.owner_username}" if self.config.owner_username else "через /order"
        return _send(user_id, f"👤 Разработчик: CR4CODE\nСвязь: {contact}\n"
                              "Можно сразу оставить заявку через /order")

    def _portfolio(self, user_id: int) -> dict:
        return _send(user_id, "🎨 Примеры: оформление WHIEDA, рекламные тексты, "
                              "Telegram-боты и автоматизация. "
                              "Попроси образцы по своей задаче через /order")
