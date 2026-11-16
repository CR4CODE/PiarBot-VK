"""Opt-in AI consultation; customer-approved lead submission only."""

from __future__ import annotations

import re
import time

from kyntrion_tools.kyntrion_ai_client import AIUnavailable
from kyntrion_tools.kyntrion_marketing import MarketingAddon, SERVICES, _buttons, _send

MAX_TURNS = 12
MAX_DAILY_USER = 30
MAX_DAILY_GLOBAL = 300
MAX_MESSAGE = 2000
EXPIRY_SECONDS = 24 * 3600
SECRET_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|\b\d{6,12}:[A-Za-z0-9_-]{20,}\b)")


class ConsultationAddon:
    def __init__(self, marketing: MarketingAddon, ai_client):
        self.marketing = marketing
        self.db = marketing.db
        self.ai_client = ai_client
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS consultations ("
                "user_id INTEGER PRIMARY KEY, category TEXT NOT NULL, "
                "status TEXT NOT NULL, consent INTEGER NOT NULL, "
                "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
                "order_id INTEGER, request_count INTEGER NOT NULL DEFAULT 0)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS consultation_messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL REFERENCES consultations(user_id) ON DELETE CASCADE, "
                "role TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS consultation_ai_usage ("
                "day INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                "requests INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(day,user_id))"
            )

    def forget_user(self, user_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM consultations WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM consultation_ai_usage WHERE user_id=?", (user_id,))

    def _reserve_ai_budget(self, user_id: int) -> bool:
        """Atomic per-day per-user and global budget (user_id=0 is anonymous total)."""
        day = int(time.time() // 86400)
        with self.db:
            count_user = self.db.execute(
                "SELECT requests FROM consultation_ai_usage WHERE day=? AND user_id=?",
                (day, user_id),
            ).fetchone()
            count_global = self.db.execute(
                "SELECT requests FROM consultation_ai_usage WHERE day=? AND user_id=0", (day,),
            ).fetchone()
            if ((count_user is not None and count_user[0] >= MAX_DAILY_USER)
                    or (count_global is not None and count_global[0] >= MAX_DAILY_GLOBAL)):
                return False
            for recipient in (user_id, 0):
                self.db.execute(
                    "INSERT INTO consultation_ai_usage(day,user_id,requests) VALUES(?,?,1) "
                    "ON CONFLICT(day,user_id) DO UPDATE SET requests=requests+1",
                    (day, recipient),
                )
            # Keep only the latest 7 days of anonymous usage totals.
            self.db.execute("DELETE FROM consultation_ai_usage WHERE day < ?", (day - 7,))
        return True

    def _session(self, user_id: int):
        row = self.db.execute("SELECT * FROM consultations WHERE user_id=?", (user_id,)).fetchone()
        if (row is not None and row["status"] == "active"
                and int(time.time()) - row["updated_at"] > EXPIRY_SECONDS):
            self.forget_user(user_id)
            return None
        return row

    def _review(self, user_id: int) -> dict:
        session = self._session(user_id)
        if session is None or session["status"] != "active":
            return _send(user_id, "Нет активной консультации. /consult")
        answers = [r["content"] for r in self.db.execute(
            "SELECT content FROM consultation_messages WHERE user_id=? AND role='user' ORDER BY id",
            (user_id,),
        )]
        if not answers:
            return _send(user_id, "Пока нет описания задачи. Напиши, что требуется.")
        description = "\n".join(f"• {answer[:400]}" for answer in answers)
        return _send(user_id, "📝 Проверь заявку KYNTRION\n\n"
                     f"Услуга: {SERVICES[session['category']]}\n\n{description[:1700]}\n\n"
                     "Отправить заявку владельцу? Это не коммерческое предложение.",
                     reply_markup=_buttons([
                         [("✅ Отправить заявку", "con:confirm")],
                         [("✏️ Дополнить", "con:continue"), ("❌ Отмена", "con:cancel")],
                     ]))

    def _confirmed_lead(self, user_id: int) -> list[dict]:
        session = self._session(user_id)
        if session is None or session["status"] != "active":
            return [_send(user_id, "Заявка уже отправлена или консультация завершена. /services")]
        rows = self.db.execute(
            "SELECT content FROM consultation_messages WHERE user_id=? AND role='user' ORDER BY id",
            (user_id,),
        ).fetchall()
        if not rows:
            return [_send(user_id, "Сначала опиши задачу, затем подтверди заявку.")]
        summary = "\n".join(row["content"][:400] for row in rows)[:1900]
        # The marketing method performs lead insertion and consultation finalization
        # in one SQLite transaction: stale/double taps cannot create two orders.
        return self.marketing.submit_catalog_order(
            user_id, session["category"], summary, consultation_id=user_id)

    def process(self, update: dict) -> tuple[bool, list[dict]]:
        callback = update.get("callback_query")
        if isinstance(callback, dict) and isinstance(callback.get("data"), str) and callback["data"].startswith("con:"):
            query_id = callback.get("id")
            chat = (callback.get("message") or {}).get("chat") or {}
            user_id = (callback.get("from") or {}).get("id")
            if not isinstance(query_id, str):
                return True, []
            ack = {"method": "answerCallbackQuery", "callback_query_id": query_id}
            if (not isinstance(user_id, int) or chat.get("type") != "private"
                    or chat.get("id") != user_id):
                return True, [{**ack, "text": "Кнопка доступна только в личном чате."}]
            data = callback["data"]
            if data.startswith("con:begin:"):
                category = data[10:]
                if category not in SERVICES:
                    return True, [ack, _send(user_id, "Неизвестная категория. /consult")]
                now = int(time.time())
                with self.db:
                    self.db.execute("DELETE FROM consultations WHERE user_id=?", (user_id,))
                    self.db.execute("DELETE FROM pending_orders WHERE user_id=?", (user_id,))
                    self.db.execute(
                        "INSERT INTO consultations(user_id,category,status,consent,created_at,updated_at) "
                        "VALUES(?,?,'active',1,?,?)", (user_id, category, now, now),
                    )
                return True, [ack, _send(user_id, "🤖 ИИ-консультация начата с твоего согласия. "
                                                "Расскажи о задаче, сроках и результате. "
                                                "Не присылай пароли или токены. "
                                                "Просмотр: /review_consult · отмена: /cancel_consult")]
            if data == "con:review":
                return True, [ack, self._review(user_id)]
            if data == "con:confirm":
                return True, [ack, *self._confirmed_lead(user_id)]
            if data == "con:cancel":
                self.forget_user(user_id)
                return True, [ack, _send(user_id, "Консультация отменена.")]
            if data == "con:continue":
                return True, [ack, _send(user_id, "Продолжай описание задачи. /review_consult")]
            return True, [{**ack, "text": "Кнопка устарела."}]

        msg = update.get("message")
        if not isinstance(msg, dict):
            return False, []
        chat = msg.get("chat") or {}
        user_id = (msg.get("from") or {}).get("id")
        body = msg.get("text")
        if (not isinstance(user_id, int) or chat.get("type") != "private"
                or chat.get("id") != user_id or not isinstance(body, str)
                or "forward_date" in msg or "forward_origin" in msg):
            return False, []
        cmd = body.split(maxsplit=1)[0].split("@", 1)[0].lower() if body.startswith("/") else ""
        if cmd == "/consult":
            rows = [[(f"{SERVICES[key]}", f"con:begin:{key}")]
                    for key in ("channel", "post", "video", "automation", "whieda")]
            return True, [_send(user_id, "🤖 Консультация с ИИ KYNTRION\n\n"
                                "ИИ поможет уточнить задачу. Выбери категорию, если согласен "
                                "передать описание задачи ИИ для подготовки заявки. "
                                "Цена и коммерческое предложение — только после согласования с владельцем.",
                                reply_markup=_buttons(rows))]
        if cmd == "/cancel_consult":
            self.forget_user(user_id)
            return True, [_send(user_id, "Консультация отменена. /services")]
        if cmd == "/review_consult":
            return True, [self._review(user_id)]
        if cmd:
            return False, []
        session = self._session(user_id)
        if session is None or session["status"] != "active":
            return False, []
        if (not 1 <= len(body.strip()) <= MAX_MESSAGE or SECRET_RE.search(body)):
            return True, [_send(user_id, "Сообщение не отправлено: максимум 2000 символов, "
                                        "не присылай ключи, пароли или токены.")]
        if session["request_count"] >= MAX_TURNS:
            return True, [self._review(user_id)]
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT INTO consultation_messages(user_id,role,content,created_at) "
                            "VALUES(?,'user',?,?)", (user_id, body.strip(), now))
            self.db.execute("UPDATE consultations SET updated_at=?,request_count=request_count+1 "
                            "WHERE user_id=?", (now, user_id))
        history = [dict(row) for row in self.db.execute(
            "SELECT role,content FROM consultation_messages WHERE user_id=? ORDER BY id DESC LIMIT 12",
            (user_id,),
        )][::-1]
        if self.ai_client is None:
            return True, [_send(user_id, "ИИ временно недоступен. Сохранил описание задачи; "
                                         "можешь дополнить её или проверить: /review_consult")]
        if not self._reserve_ai_budget(user_id):
            return True, [_send(user_id, "На сегодня достигнут лимит ИИ-консультаций. "
                                         "Ответ сохранён: отправь заявку вручную через /review_consult")]
        try:
            reply = self.ai_client.reply(session["category"], history)
            if not isinstance(reply, str) or not 1 <= len(reply.strip()) <= 1800:
                raise AIUnavailable("Bad AI response")
        except Exception:
            return True, [_send(user_id, "ИИ временно недоступен. Твои ответы сохранены. "
                                         "Можно продолжить или нажать /review_consult")]
        with self.db:
            self.db.execute("INSERT INTO consultation_messages(user_id,role,content,created_at) "
                            "VALUES(?,'assistant',?,?)", (user_id, reply.strip(), int(time.time())))
        return True, [_send(user_id, reply.strip(), reply_markup=_buttons([
            [("📋 Проверить заявку", "con:review"), ("❌ Завершить", "con:cancel")],
        ]))]
