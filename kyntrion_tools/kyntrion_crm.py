"""Owner-only sales CRM layered over the existing SQLite order records."""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from kyntrion_tools.kyntrion_marketing import MarketingAddon, SERVICES


PAGE_SIZE = 10
TRANSITIONS = {
    "new": frozenset(("qualifying", "awaiting_quote", "cancelled")),
    "qualifying": frozenset(("awaiting_quote", "cancelled")),
    # Only successful human-approved delivery may mark a quote as sent.
    "awaiting_quote": frozenset(("cancelled",)),
    "quoted": frozenset(("in_progress", "cancelled")),
    "in_progress": frozenset(("completed", "cancelled")),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


def _time_label(timestamp: int) -> str:
    if not timestamp:
        return "—"
    tz = timezone(timedelta(hours=3))
    return datetime.fromtimestamp(timestamp, tz).strftime("%d.%m.%Y %H:%M МСК")


def _message(chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> dict:
    action = {"method": "sendMessage", "chat_id": chat_id, "text": text}
    if buttons:
        action["reply_markup"] = {"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in buttons
        ]}
    return action


class CRMAddon:
    def __init__(self, marketing: MarketingAddon):
        self.marketing = marketing
        self.db = marketing.db
        self.owner = marketing.config.admin_chat_id
        self.editor_drafts: dict[int, dict] = {}
        self._migrate()

    def _migrate(self) -> None:
        # Order rows predate the CRM. Migrate in place; never rewrite old leads.
        self.db.execute("PRAGMA foreign_keys=ON")
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(orders)")}
        additions = {
            "status": "TEXT NOT NULL DEFAULT 'new'",
            "updated_at": "INTEGER NOT NULL DEFAULT 0",
            "origin": "TEXT NOT NULL DEFAULT 'legacy'",
            "version": "INTEGER NOT NULL DEFAULT 0",
            "quote_price_rub": "INTEGER",
            "quote_text": "TEXT",
            "quote_state": "TEXT NOT NULL DEFAULT 'none'",
            "quote_sent_at": "INTEGER",
            "quote_generation": "INTEGER NOT NULL DEFAULT 0",
        }
        with self.db:
            for name, definition in additions.items():
                if name not in cols:
                    self.db.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS crm_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE, "
                "actor TEXT NOT NULL, action TEXT NOT NULL, "
                "previous_status TEXT, next_status TEXT, "
                "created_at INTEGER NOT NULL, event_key TEXT NOT NULL UNIQUE)"
            )

    def _require_owner(self, requester_id: int) -> None:
        if type(requester_id) is not int or requester_id != self.owner:
            raise PermissionError("Only configured owner may manage CRM")

    def list_orders(self, owner_id: int, page: int = 0,
                    status: str | None = None) -> list[dict]:
        self._require_owner(owner_id)
        page = min(max(page, 0), 1000) if type(page) is int else 0
        if status is not None and status not in TRANSITIONS:
            return []
        where = " WHERE status=?" if status else ""
        args = (status, PAGE_SIZE, PAGE_SIZE * page) if status else (PAGE_SIZE, PAGE_SIZE * page)
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM orders" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", args
        ).fetchall()]

    def transition(self, owner_id: int, order_id: int, status: str,
                   expected_version: int) -> bool:
        self._require_owner(owner_id)
        if (type(order_id) is not int or order_id <= 0 or type(expected_version) is not int
                or expected_version < 0 or status not in TRANSITIONS):
            return False
        now = int(time.time())
        with self.db:
            row = self.db.execute(
                "SELECT status,version FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if (row is None or row["version"] != expected_version
                    or status not in TRANSITIONS.get(row["status"], ())):
                return False
            result = self.db.execute(
                "UPDATE orders SET status=?,version=version+1,updated_at=? "
                "WHERE id=? AND version=? AND status=?",
                (status, now, order_id, expected_version, row["status"]),
            )
            if result.rowcount != 1:
                return False
            self.db.execute(
                "INSERT INTO crm_events(order_id,actor,action,previous_status,next_status,"
                "created_at,event_key) VALUES(?,?,?,?,?,?,?)",
                (order_id, f"owner:{owner_id}", "status", row["status"], status, now,
                 f"status:{order_id}:{expected_version}:{status}"),
            )
        return True

    def save_quote(self, owner_id: int, order_id: int, price_rub: int | None,
                   text: str, expected_version: int) -> bool:
        self._require_owner(owner_id)
        if (type(order_id) is not int or order_id <= 0
                or type(expected_version) is not int or expected_version < 0
                or (price_rub is not None and (type(price_rub) is not int or not 0 <= price_rub <= 10**9))
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 1500):
            return False
        now = int(time.time())
        with self.db:
            row = self.db.execute("SELECT status,version,quote_state,quote_generation FROM orders WHERE id=?",
                                  (order_id,)).fetchone()
            if (row is None or row["version"] != expected_version
                    or row["status"] not in ("new", "qualifying", "awaiting_quote")
                    or row["quote_state"] not in ("none", "draft")):
                return False
            changed = self.db.execute(
                "UPDATE orders SET status='awaiting_quote',version=version+1,updated_at=?,"
                "quote_price_rub=?,quote_text=?,quote_state='draft',quote_generation=quote_generation+1 "
                "WHERE id=? AND version=? AND quote_state=?",
                (now, price_rub, text.strip(), order_id, expected_version, row["quote_state"]),
            )
            if changed.rowcount != 1:
                return False
            self.db.execute(
                "INSERT INTO crm_events(order_id,actor,action,previous_status,next_status,"
                "created_at,event_key) VALUES(?,?,?,?,?,?,?)",
                (order_id, f"owner:{owner_id}", "quote_draft", row["status"],
                 "awaiting_quote", now, f"quote_draft:{order_id}:{expected_version}"),
            )
        return True

    def deliver_quote(self, owner_id: int, order_id: int, expected_version: int,
                      send: Callable[[dict], None]) -> str:
        """At-most-one attempt; uncertain network outcome is never automatically replayed."""
        self._require_owner(owner_id)
        if (type(order_id) is not int or order_id <= 0 or type(expected_version) is not int
                or expected_version < 0):
            return "stale"
        now = int(time.time())
        with self.db:
            row = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if (row is None or row["version"] != expected_version
                    or row["status"] != "awaiting_quote" or row["quote_state"] != "draft"
                    or not row["quote_text"]):
                return "stale"
            claimed = self.db.execute(
                "UPDATE orders SET quote_state='sending',version=version+1,updated_at=? "
                "WHERE id=? AND version=? AND quote_state='draft'",
                (now, order_id, expected_version),
            )
            if claimed.rowcount != 1:
                return "stale"
            self.db.execute(
                "INSERT INTO crm_events(order_id,actor,action,previous_status,next_status,"
                "created_at,event_key) VALUES(?,?,?,?,?,?,?)",
                (order_id, f"owner:{owner_id}", "quote_attempt", "awaiting_quote",
                 "awaiting_quote", now, f"quote_attempt:{order_id}:{expected_version}"),
            )
        price = (f"{row['quote_price_rub']:,}".replace(",", " ") + " ₽"
                 if row["quote_price_rub"] is not None else "По запросу")
        action = _message(row["user_id"], "📄 Коммерческое предложение KYNTRION\n\n"
                          f"{row['quote_text']}\n\nСтоимость: {price}\n"
                          "Для уточнений ответь в этом чате или напиши /contacts")
        try:
            send(action)
        except Exception:
            # Telegram may have received the message even if the HTTP response was lost.
            with self.db:
                self.db.execute(
                    "UPDATE orders SET quote_state='uncertain',version=version+1,updated_at=? "
                    "WHERE id=? AND quote_state='sending'", (int(time.time()), order_id),
                )
                self.db.execute(
                    "INSERT INTO crm_events(order_id,actor,action,previous_status,next_status,"
                    "created_at,event_key) VALUES(?,?,?,?,?,?,?)",
                    (order_id, f"owner:{owner_id}", "quote_uncertain", "awaiting_quote",
                     "awaiting_quote", int(time.time()), f"quote_uncertain:{order_id}:{expected_version}"),
                )
            return "uncertain"
        with self.db:
            self.db.execute(
                "UPDATE orders SET quote_state='sent',status='quoted',quote_sent_at=?,"
                "version=version+1,updated_at=? WHERE id=? AND quote_state='sending'",
                (int(time.time()), int(time.time()), order_id),
            )
            self.db.execute(
                "INSERT INTO crm_events(order_id,actor,action,previous_status,next_status,"
                "created_at,event_key) VALUES(?,?,?,?,?,?,?)",
                (order_id, f"owner:{owner_id}", "quote_sent", "awaiting_quote", "quoted",
                 int(time.time()), f"quote_sent:{order_id}:{expected_version}"),
            )
        return "sent"

    def _quote_preview(self, order_id: int) -> dict:
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None or row["quote_state"] != "draft":
            return _message(self.owner, "Черновик предложения не найден. /crm")
        price = (f"{row['quote_price_rub']:,}".replace(",", " ") + " ₽"
                 if row["quote_price_rub"] is not None else "По запросу")
        text = (f"👑 Подтверждение отправки\nКлиент: {row['user_id']}\n"
                f"Стоимость: {price}\n\n{row['quote_text']}\n\n"
                "Отправить клиенту именно этот текст и цену?")
        return _message(self.owner, text, [
            [("✅ Отправить предложение", f"crm:quote_send:{order_id}:{row['version']}")],
            [("⬅️ Назад к заявке", f"crm:card:{order_id}")],
        ])

    def _list_menu(self, page: int = 0) -> dict:
        items = self.list_orders(self.owner, page)
        if not items:
            return _message(self.owner, "📋 CRM: заявок на этой странице нет.",
                            [[("⬅️ К началу", "crm:page:0")]] if page else None)
        statuses = self.db.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()
        stats = " · ".join(f"{row['status']}: {row['n']}" for row in statuses)
        lines = [f"📋 KYNTRION CRM — заявки\n{stats}\n"]
        buttons = []
        for item in items:
            name = SERVICES.get(item["service"], item["service"])
            lines.append(f"#{item['id']} · {name} · {item['status']}\n"
                         f"{item['description'][:130]}")
            buttons.append([(f"📋 #{item['id']}", f"crm:card:{item['id']}")])
        pages = []
        if page:
            pages.append(("⬅️", f"crm:page:{page - 1}"))
        if len(items) == PAGE_SIZE:
            pages.append(("➡️", f"crm:page:{page + 1}"))
        if pages:
            buttons.append(pages)
        return _message(self.owner, "\n\n".join(lines)[:3900], buttons)

    def _card(self, order_id: int) -> dict:
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            return _message(self.owner, "Заявка не найдена. /crm")
        status, version = row["status"], row["version"]
        options = [(f"➡️ {new}", f"crm:status:{order_id}:{version}:{new}")
                   for new in sorted(TRANSITIONS.get(status, ())) if new != "cancelled"]
        if "cancelled" in TRANSITIONS.get(status, ()):
            options.append(("❌ Отменена", f"crm:status:{order_id}:{version}:cancelled"))
        buttons = [[option] for option in options]
        if status in ("new", "qualifying", "awaiting_quote") and row["quote_state"] in ("none", "draft"):
            buttons.append([("💰 Подготовить предложение", f"crm:quote:{order_id}:{version}")])
        if row["quote_state"] == "draft":
            buttons.append([("✅ Проверить предложение", f"crm:preview:{order_id}")])
        buttons.append([("📋 К заявкам", "crm:page:0")])
        events = self.db.execute(
            "SELECT action,previous_status,next_status,created_at FROM crm_events "
            "WHERE order_id=? ORDER BY id DESC LIMIT 5", (order_id,),
        ).fetchall()
        history = "\n".join(f"• {_time_label(e['created_at'])}: {e['action']} "
                            f"({e['previous_status'] or '—'} → {e['next_status'] or '—'})"
                            for e in events) if events else "Пока нет изменений"
        text = (f"📋 Заявка #{order_id}\nУслуга: {SERVICES.get(row['service'], row['service'])}\n"
                f"Статус: {status}\nПредложение: {row['quote_state']}\n"
                f"Клиент: {row['user_id']}\nИсточник: {row['origin']}\n"
                f"Создана: {_time_label(row['created_at'])}\n"
                f"Обновлена: {_time_label(row['updated_at'])}\n\n"
                f"{row['description'][:1800]}\n\nИстория:\n{history}")
        return _message(self.owner, text[:3900], buttons)

    def process(self, update: dict) -> tuple[bool, list[dict]]:
        callback = update.get("callback_query")
        if isinstance(callback, dict) and isinstance(callback.get("data"), str) and callback["data"].startswith("crm:"):
            query_id = callback.get("id")
            user_id = (callback.get("from") or {}).get("id")
            chat = (callback.get("message") or {}).get("chat") or {}
            if not isinstance(query_id, str):
                return True, []
            ack = {"method": "answerCallbackQuery", "callback_query_id": query_id}
            if chat.get("type") != "private" or user_id != self.owner or chat.get("id") != user_id:
                return True, [{**ack, "text": "Доступно только владельцу."}]
            parts = callback["data"].split(":")
            if len(parts) == 3 and parts[1] == "page" and parts[2].isdigit():
                return True, [ack, self._list_menu(min(int(parts[2]), 1000))]
            if len(parts) == 3 and parts[1] == "card" and parts[2].isdigit():
                return True, [ack, self._card(int(parts[2]))]
            if len(parts) == 3 and parts[1] == "preview" and parts[2].isdigit():
                return True, [ack, self._quote_preview(int(parts[2]))]
            if (len(parts) == 4 and parts[1] == "quote" and parts[2].isdigit()
                    and parts[3].isdigit()):
                order_id, expected_version = int(parts[2]), int(parts[3])
                row = self.db.execute("SELECT version,quote_state,status FROM orders WHERE id=?",
                                      (order_id,)).fetchone()
                if (row is None or row["version"] != expected_version or row["quote_state"] not in
                        ("none", "draft") or row["status"] not in ("new", "qualifying", "awaiting_quote")):
                    return True, [ack, _message(user_id, "Кнопка устарела. /crm")]
                self.editor_drafts[user_id] = {"order_id": order_id,
                                               "version": expected_version, "step": "price",
                                               "expires": time.time() + 900}
                return True, [ack, _message(user_id, "💰 Укажи стоимость в рублях (число) "
                                                     "или напиши «по запросу». Отмена: /crm_cancel")]
            if (len(parts) == 4 and parts[1] == "quote_send" and parts[2].isdigit()
                    and parts[3].isdigit()):
                return True, [ack, {"method": "crmDeliverQuote", "order_id": int(parts[2]),
                                    "version": int(parts[3]), "owner_id": user_id}]
            if (len(parts) == 5 and parts[1] == "status" and parts[2].isdigit()
                    and parts[3].isdigit()):
                changed = self.transition(user_id, int(parts[2]), parts[4], int(parts[3]))
                notice = "Статус обновлён." if changed else "Действие устарело или недоступно."
                return True, [ack, _message(user_id, notice), self._card(int(parts[2]))]
            return True, [{**ack, "text": "Кнопка устарела. Открой /crm."}]

        message = update.get("message")
        if not isinstance(message, dict):
            return False, []
        chat = message.get("chat") or {}
        sender = (message.get("from") or {}).get("id")
        body = message.get("text")
        if chat.get("type") != "private" or sender != chat.get("id") or not isinstance(body, str):
            return False, []
        command = body.split(maxsplit=1)[0].split("@", 1)[0].lower() if body.startswith("/") else ""
        if command == "/crm":
            if sender != self.owner:
                return True, [_message(sender, "Эта команда доступна только владельцу.")]
            self.editor_drafts.pop(sender, None)
            return True, [self._list_menu()]
        if command == "/crm_cancel":
            if sender != self.owner:
                return True, [_message(sender, "Эта команда доступна только владельцу.")]
            self.editor_drafts.pop(sender, None)
            return True, [_message(sender, "Редактирование CRM отменено. /crm")]
        draft = self.editor_drafts.get(sender) if sender == self.owner else None
        if draft and not command:
            if time.time() > draft["expires"]:
                self.editor_drafts.pop(sender, None)
                return True, [_message(sender, "Время редактирования истекло. /crm")]
            value = body.strip()
            if draft["step"] == "price":
                if value.casefold() == "по запросу":
                    price = None
                elif re.fullmatch(r"[0-9]{1,10}", value) and int(value) <= 10**9:
                    price = int(value)
                else:
                    return True, [_message(sender, "Укажи целую стоимость в рублях или «по запросу».")]
                draft["price"] = price
                draft["step"] = "text"
                return True, [_message(sender, "✍️ Напиши текст коммерческого предложения "
                                               "(до 1500 символов). Отмена: /crm_cancel")]
            if not 1 <= len(value) <= 1500:
                return True, [_message(sender, "Нужен текст предложения до 1500 символов.")]
            changed = self.save_quote(sender, draft["order_id"], draft["price"],
                                      value, draft["version"])
            self.editor_drafts.pop(sender, None)
            if not changed:
                return True, [_message(sender, "Заявка изменилась. Открой /crm снова.")]
            return True, [self._quote_preview(draft["order_id"])]
        return False, []
