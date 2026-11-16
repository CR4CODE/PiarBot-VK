"""Owner-only inline console for KYNTRION release announcements.

There is no send-on-deploy, no automatic enrollment and no background poller.
Owner callbacks are private, short-lived, one-use and bound to message digest.
"""
from __future__ import annotations

from html import escape
import secrets
import time

from kyntrion_tools.broadcast_notifications import NotificationStore, _validate_text, send_batch
from kyntrion_tools.post_studio_render import _segments_text, segments_from_telegram


class OwnerNotificationPanel:
    def __init__(self, api, store: NotificationStore, owner_id: int,
                 *, delivery_enabled: bool = False, clock=None, monotonic=None):
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("Panel requires verified positive owner ID")
        self.api, self.store, self.owner_id = api, store, owner_id
        self.delivery_enabled = delivery_enabled is True
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self._controls: dict[str, tuple[str, str, str, float]] = {}
        self._awaiting_text = False
        self._armed: str | None = None
        self._last_send = float("-inf")
        self.store.pause_all()  # Never resume a paused/in-flight run on bot startup.

    def _send(self, text: str, *, markup: dict | None = None, html: bool = False) -> None:
        action = {"method": "sendMessage", "chat_id": self.owner_id, "text": text}
        if markup:
            action["reply_markup"] = markup
        if html:
            action["parse_mode"] = "HTML"
            action["link_preview_options"] = {"is_disabled": True}
        self.api.send_action(action)

    def _token(self, action: str, campaign: str = "") -> str:
        now = float(self.clock())
        if len(self._controls) > 128:
            self._controls = {k: v for k, v in self._controls.items()
                              if v[3] > now}
        if len(self._controls) > 256:
            self._controls.clear()
        digest = self.store.campaign(campaign)["digest"] if campaign else ""
        token = secrets.token_urlsafe(9)
        self._controls[token] = (action, campaign, digest, now + 600)
        return "nfy:" + token

    def _buttons(self, *rows: tuple[tuple[str, str, str], ...]) -> dict:
        return {"inline_keyboard": [
            [{"text": title, "callback_data": self._token(action, campaign)}
             for title, action, campaign in row] for row in rows]}

    def entry_button(self) -> dict:
        return {"text": "📣 Центр уведомлений",
                "callback_data": self._token("home")}

    def home(self) -> None:
        self._awaiting_text = False
        state = "разрешена вручную" if self.delivery_enabled else "выключена настройками"
        running = f"\n⏳ Активная кампания: {self._armed}" if self._armed else ""
        self._send(
            "📣 KYNTRION — центр уведомлений\n\n"
            f"Подписчиков на новости: {self.store.subscribers_count()}\n"
            f"Отправка: {state}{running}\n\n"
            "Здесь можно подготовить анонс, проверить текст и статистику. "
            "Никакой отправки без отдельного подтверждения.",
            markup=self._buttons(
                (("✍️ Новый анонс", "new", ""),),
                (("🗂 Кампании", "list", ""),),
            ))

    @staticmethod
    def _render_text(message: dict) -> str:
        body = message.get("text")
        if not isinstance(body, str):
            raise ValueError("Expected text announcement")
        entities = segments_from_telegram(body, message.get("entities"))
        html = _segments_text(entities)[0] if entities else escape(body)
        return _validate_text(html)

    def _prepare(self, message: dict) -> None:
        body = message.get("text", "")
        if body.startswith("/"):
            return  # Do not capture unrelated /start, /forget_me or /news_off.
        self._awaiting_text = False
        try:
            html = self._render_text(message)
            campaign_id = "news_" + secrets.token_hex(7)
            count = self.store.create(campaign_id, "release", html)
            self._send(
                f"📝 Черновик создан: {campaign_id}\n"
                f"Подписанных адресатов на момент создания: {count}.\n"
                "Пока ничего не отправлено.",
                markup=self._buttons(
                    (("👁 Предпросмотр", "view", campaign_id),),
                    (("🏠 Меню", "home", ""),),
                ))
        except ValueError:
            self._send("Текст не принят: отправьте обычное сообщение до 4096 символов "
                       "или текст с Telegram-оформлением. /notify — начать заново.")

    def _history(self) -> None:
        campaigns = self.store.recent_campaigns()
        if not campaigns:
            self._send("Кампаний ещё нет.", markup=self._buttons(
                (("✍️ Новый анонс", "new", ""),),))
            return
        rows = tuple((f"📰 {c['id']} ({c['state']})", "view", c["id"])
                     for c in campaigns)
        self._send("🗂 Последние кампании:", markup=self._buttons(
            *[(row,) for row in rows], (("🏠 Меню", "home", ""),)))

    def _details(self, campaign_id: str, *, show_preview: bool = True) -> None:
        campaign = self.store.campaign(campaign_id)
        counts = self.store.counts(campaign_id)
        if show_preview:
            self._send("👁 Предпросмотр сообщения (только для владельца):")
            self._send(campaign["body"], html=True)
        digest = campaign["digest"][:12]
        summary = (
            f"📣 {campaign_id}\n"
            f"Состояние: {campaign['state']}\n"
            f"SHA256: {digest}\n"
            f"Ожидают: {counts['pending']} | Отправлено: {counts['sent']}\n"
            f"Ошибки: {counts['failed']} | Пропущено: {counts['skipped']}\n"
            f"Неизвестно: {counts['unknown']} | В обработке: {counts['inflight']}\n"
            f"Запуск: {'активен' if self._armed == campaign_id else 'не запущен'}"
        )
        rows: list[tuple[tuple[str, str, str], ...]] = []
        if campaign["state"] == "draft":
            rows.append((("✅ Подтвердить содержание", "approve", campaign_id),))
        elif self._armed == campaign_id:
            rows.append((("⏸ Остановить рассылку", "pause", campaign_id),))
        elif (counts["pending"] and not (counts["unknown"] or counts["inflight"])
              and not self.store.has_invalid_payload(campaign_id)):
            rows.append((("🚀 К отправке (подтверждение)", "verify_send", campaign_id),))
        rows.append((("🔄 Обновить статистику", "stats", campaign_id),))
        rows.append((("🏠 Меню", "home", ""),))
        self._send(summary, markup=self._buttons(*rows))

    def _confirm_start(self, campaign_id: str) -> None:
        campaign = self.store.campaign(campaign_id)
        counts = self.store.counts(campaign_id)
        if not self.delivery_enabled:
            self._send("🔒 Рассылка выключена настройками сервера. "
                       "Необходим отдельный допуск владельца на production.")
            return
        if campaign["state"] != "approved" or counts["pending"] == 0:
            self._send("Кампания не одобрена или подписанных ожидающих адресатов нет.")
            return
        if counts["unknown"] or counts["inflight"]:
            self._send("⚠️ Есть неопределённые доставки. Сначала ручная сверка; "
                       "автоповтор запрещён.")
            return
        if self.store.has_invalid_payload(campaign_id):
            self._send("⚠️ Telegram отклонил содержимое кампании. "
                       "Исправьте текст в новом черновике, не повторяйте эту рассылку.")
            return
        self._send(
            f"⚠️ ПОСЛЕДНЕЕ ПОДТВЕРЖДЕНИЕ\n\n"
            f"Кампания: {campaign_id}\n"
            f"Отпечаток: {campaign['digest'][:12]}\n"
            f"Ожидают: {counts['pending']}\n\n"
            "Нажатие «Начать» запустит реальные сообщения подписчикам. "
            "Отменить уже отправленные сообщения невозможно.",
            markup=self._buttons(
                (("🚀 Начать отправку", "start", campaign_id),),
                (("↩️ Отмена", "view", campaign_id),),
            ))

    def _process_action(self, action: str, campaign_id: str) -> None:
        if action == "home":
            self.home()
        elif action == "new":
            self._awaiting_text = True
            self._send("✍️ Отправьте следующим сообщением текст анонса.\n"
                       "Можно использовать жирный, курсив и ссылки средствами Telegram. "
                       "При сохранении будет сделан снимок текущих подписчиков.")
        elif action == "list":
            self._history()
        elif action == "view":
            self._details(campaign_id)
        elif action == "stats":
            self._details(campaign_id, show_preview=False)
        elif action == "approve":
            data = self.store.campaign(campaign_id)
            if data["state"] != "draft":
                self._send("Эта кампания уже утверждена. Откройте /notify.")
                return
            self.store.approve(campaign_id, "APPROVE:" + campaign_id + ":" +
                               data["digest"][:12])
            self._send("✅ Текст утверждён. Это ещё не отправка.")
            self._details(campaign_id, show_preview=False)
        elif action == "verify_send":
            self._confirm_start(campaign_id)
        elif action == "start":
            if not self.delivery_enabled:
                self._send("🔒 Отправка отключена в конфигурации.")
                return
            self.store.activate(campaign_id)
            self._armed = campaign_id
            self._last_send = float("-inf")
            self._send("🚀 Отправка запущена. Проверить или приостановить: /notify.")
        elif action == "pause":
            self.store.pause(campaign_id)
            if self._armed == campaign_id:
                self._armed = None
            self._send("⏸ Рассылка остановлена. Уже сделанные попытки не повторяются.")
            self._details(campaign_id, show_preview=False)

    def handle_update(self, update: dict) -> bool:
        callback = update.get("callback_query")
        if isinstance(callback, dict) and isinstance(callback.get("data"), str):
            if not callback["data"].startswith("nfy:"):
                return False
            chat = (callback.get("message") or {}).get("chat") or {}
            actor = callback.get("from") or {}
            allowed = (chat.get("type") == "private"
                       and type(actor.get("id")) is int
                       and actor["id"] == self.owner_id
                       and chat.get("id") == self.owner_id)
            query_id = callback.get("id")
            if isinstance(query_id, str) and query_id:
                self.api.send_action({
                    "method": "answerCallbackQuery", "callback_query_id": query_id,
                    **({} if allowed else {"text": "Только владелец."})})
            if not allowed:
                return True
            token = callback["data"][4:]
            record = self._controls.pop(token, None)
            if record is None or float(self.clock()) >= record[3]:
                self._send("Кнопка устарела. Откройте /notify.")
                return True
            action, campaign_id, digest, _ = record
            try:
                if campaign_id and self.store.campaign(campaign_id)["digest"] != digest:
                    raise PermissionError("Campaign content changed")
                self._process_action(action, campaign_id)
            except (ValueError, LookupError, PermissionError):
                self._send("Действие недоступно. Откройте /notify для обновления.")
            return True

        message = update.get("message")
        if not isinstance(message, dict) or any(k in message for k in
                ("forward_origin", "forward_from", "forward_date")):
            return False
        chat, actor = message.get("chat") or {}, message.get("from") or {}
        if (chat.get("type") != "private" or chat.get("id") != self.owner_id
                or type(actor.get("id")) is not int or actor["id"] != self.owner_id):
            return False
        body = message.get("text")
        if not isinstance(body, str):
            return False
        command = body.split(maxsplit=1)[0].split("@", 1)[0].lower() if body.strip() else ""
        if command in ("/notify", "/notifications"):
            self.home()
            return True
        if self._awaiting_text and not body.startswith("/"):
            self._prepare(message)
            return True
        return False

    def wants_short_poll(self) -> bool:
        return (self.delivery_enabled and self._armed is not None
                and self.store.has_due(self._armed))

    def fast_poll(self) -> bool:
        return (self.wants_short_poll()
                and self.monotonic() - self._last_send >= 1.1)

    def tick(self) -> None:
        if not self.fast_poll():
            return
        campaign_id = self._armed
        if campaign_id is None:
            return
        self._last_send = self.monotonic()
        # One outbound attempt per polling cycle. The loop continues to handle
        # regular Telegram updates; no worker or concurrent poller is introduced.
        counts = send_batch(self.store, self.api, campaign_id, limit=1)
        if counts["unknown"] or counts["inflight"]:
            self.store.pause(campaign_id)
            self._armed = None
            try:
                self._send("⚠️ Кампания остановлена: результат одной из отправок "
                           "неопределён. Автоматического повтора не будет.")
            except Exception:
                pass
        elif self.store.has_invalid_payload(campaign_id):
            self.store.pause(campaign_id)
            self._armed = None
            try:
                self._send("⚠️ Рассылка остановлена: Telegram отклонил формат сообщения. "
                           "Исправьте текст и создайте новую кампанию.")
            except Exception:
                pass
        elif not counts["pending"]:
            self.store.pause(campaign_id)
            self._armed = None
            try:
                self._send(f"✅ Кампания {campaign_id} завершила обработку адресатов. "
                           f"Успешно: {counts['sent']}, ошибок: {counts['failed']}.")
            except Exception:
                pass
