"""SQLite-backed, owner-editable Telegram service catalog for KYNTRION."""

from __future__ import annotations

import re
import time

from kyntrion_tools.kyntrion_marketing import MarketingAddon


CATEGORIES = {
    "automation": "🤖 Боты и автоматизация",
    "channel": "📢 Telegram-каналы",
    "post": "✍️ Тексты и дизайн",
    "video": "🎬 Видео и озвучка",
    "whieda": "🌿 Для партнёров WHIEDA",
}

SECRET_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")

INITIAL_SERVICES = {
    "automation": ("Telegram-боты и автоматизация", "Разработка ботов, интеграций и повторяемых сценариев для бизнеса."),
    "channel": ("Telegram-канал под ключ", "Структура, оформление и публикации для вашего канала."),
    "post": ("Тексты и визуальные материалы", "Подготовка постов, изображений и контента для продвижения."),
    "video": ("Видео, Reels и озвучка", "Монтаж коротких роликов, оформление и озвучка контента."),
    "whieda": ("Цифровое продвижение партнёра WHIEDA", "Помощь с каналом, публикациями и автоматизацией партнёрского проекта."),
}

INITIAL_PACKAGES = {
    "Старт": "Базовое решение: согласуем конкретный объём и подготовим первый результат.",
    "Развитие": "Расширенный набор задач для регулярной работы и продвижения.",
    "Под ключ": "Комплексная реализация: планирование, подготовка и запуск проекта.",
}


def _send(chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> dict:
    action = {"method": "sendMessage", "chat_id": chat_id, "text": text}
    if buttons is not None:
        action["reply_markup"] = {"inline_keyboard": [
            [{"text": label, "callback_data": callback} for label, callback in row]
            for row in buttons
        ]}
    return action


def _id(value: str) -> int | None:
    return int(value) if re.fullmatch(r"[1-9][0-9]{0,8}", value) else None


def price_label(rubles: int | None) -> str:
    return "По запросу" if rubles is None else f"{rubles:,} ₽".replace(",", " ")


def parse_price(text: str) -> tuple[bool, int | None]:
    value = text.strip().lower()
    if value in ("по запросу", "запрос", "none"):
        return True, None
    if re.fullmatch(r"[0-9]{1,8}", value):
        return True, int(value)
    return False, None


class CatalogAddon:
    def __init__(self, marketing: MarketingAddon):
        self.marketing = marketing
        self.db = marketing.db
        self.owner_id = marketing.config.admin_chat_id
        self.admin_drafts: dict[int, dict] = {}
        self.order_drafts: dict[int, dict] = {}
        self.pending_deletes: dict[int, tuple[int, int]] = {}
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL REFERENCES catalog_services(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price_rub INTEGER CHECK(price_rub IS NULL OR price_rub >= 0),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS catalog_services_category ON catalog_services(category, active);
            CREATE INDEX IF NOT EXISTS catalog_packages_service ON catalog_packages(service_id, active);
            """
        )
        with self.db:
            done = self.db.execute("SELECT 1 FROM catalog_meta WHERE key='seed_v1'").fetchone()
            if done is None:
                now = int(time.time())
                for key, (title, description) in INITIAL_SERVICES.items():
                    service_id = self.db.execute(
                        "INSERT INTO catalog_services(category,title,description,created_at) VALUES(?,?,?,?)",
                        (key, title, description, now),
                    ).lastrowid
                    for package_title, package_desc in INITIAL_PACKAGES.items():
                        self.db.execute(
                            "INSERT INTO catalog_packages(service_id,title,description,price_rub,created_at) "
                            "VALUES(?,?,?,NULL,?)",
                            (service_id, package_title, package_desc, now),
                        )
                self.db.execute("INSERT INTO catalog_meta(key,value) VALUES('seed_v1','1')")

    def list_services(self, category: str | None = None, *, public_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM catalog_services WHERE 1=1"
        args: list = []
        if category is not None:
            sql += " AND category=?"
            args.append(category)
        if public_only:
            sql += " AND active=1"
        return [dict(row) for row in self.db.execute(sql + " ORDER BY id", args).fetchall()]

    def list_packages(self, service_id: int, *, public_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM catalog_packages WHERE service_id=?"
        if public_only:
            sql += " AND active=1"
        return [dict(row) for row in self.db.execute(sql + " ORDER BY id", (service_id,)).fetchall()]

    def _service_row(self, service_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM catalog_services WHERE id=?", (service_id,)).fetchone()
        return dict(row) if row else None

    def _package_row(self, package_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM catalog_packages WHERE id=?", (package_id,)).fetchone()
        return dict(row) if row else None

    def process(self, update: dict) -> tuple[bool, list[dict]]:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            data = callback.get("data")
            if not isinstance(data, str) or not (data.startswith("cat:") or data in ("mkt:services", "mkt:order")):
                return False, []
            callback_id = callback.get("id")
            if not isinstance(callback_id, str) or not callback_id:
                return True, []
            ack = {"method": "answerCallbackQuery", "callback_query_id": callback_id}
            sender = (callback.get("from") or {}).get("id")
            chat = (callback.get("message") or {}).get("chat") or {}
            if not isinstance(sender, int) or chat.get("type") != "private" or chat.get("id") != sender:
                return True, [{**ack, "text": "Кнопка недоступна в этом чате."}]
            return True, [ack, *self._callback(sender, data)]

        message = update.get("message")
        if not isinstance(message, dict):
            return False, []
        sender = (message.get("from") or {}).get("id")
        chat = message.get("chat") or {}
        if not isinstance(sender, int) or chat.get("type") != "private" or chat.get("id") != sender:
            return False, []
        if any(key in message for key in ("forward_origin", "forward_from", "forward_date")):
            return False, []
        body = message.get("text")
        if not isinstance(body, str):
            return False, []
        command = body.split(maxsplit=1)[0].split("@", 1)[0].lower() if body.startswith("/") else ""
        if command == "/catalog_cancel":
            self.admin_drafts.pop(sender, None)
            self.order_drafts.pop(sender, None)
            return True, [_send(sender, "Действие отменено. /services")]
        if command == "/cancel_order" and sender in self.order_drafts:
            self.order_drafts.pop(sender, None)
            return True, [_send(sender, "Заявка отменена. /services")]
        if command == "/catalog_manage":
            self.order_drafts.pop(sender, None)
            return True, [self._admin_home(sender) if sender == self.owner_id
                          else _send(sender, "Команда доступна только владельцу.")]
        if command == "/forget_me":
            self.order_drafts.pop(sender, None)
            self.admin_drafts.pop(sender, None)
            return False, []
        if command in ("/services", "/order"):
            self.order_drafts.pop(sender, None)
            self.admin_drafts.pop(sender, None)
            return True, [self._home(sender)]
        if sender == self.owner_id and sender in self.admin_drafts:
            return True, [self._admin_input(sender, body)]
        if sender in self.order_drafts and not body.startswith("/"):
            return True, [self._order_input(sender, body)]
        return False, []

    def _home(self, user_id: int) -> dict:
        rows = [[(label, f"cat:cat:{key}")] for key, label in CATEGORIES.items()]
        rows.append([("🤖 Консультация с ИИ", "ui:consult")])
        if user_id == self.owner_id:
            rows.append([("⚙️ Управление каталогом", "cat:admin")])
        return _send(user_id, "🚀 KYNTRION — цифровые услуги\n\n"
                     "Выбери направление. Для партнёров WHIEDA есть отдельные предложения.\n"
                     "Стоимость и объём подтверждаем перед началом работ.", rows)

    def _category(self, user_id: int, category: str) -> dict:
        if category not in CATEGORIES:
            return _send(user_id, "Категория недоступна. /services")
        services = self.list_services(category, public_only=True)[:15]
        rows = [[(row["title"][:50], f"cat:s:{row['id']}")] for row in services]
        rows.append([("⬅️ Категории", "cat:home")])
        text = CATEGORIES[category] + "\n\n"
        text += "\n".join(f"• {row['title']}: {row['description'][:100]}" for row in services) if services else "Предложений пока нет."
        return _send(user_id, text, rows)

    def _service(self, user_id: int, service_id: int) -> dict:
        service = self._service_row(service_id)
        if service is None or not service["active"]:
            return _send(user_id, "Услуга сейчас недоступна. /services")
        packages = self.list_packages(service_id, public_only=True)[:15]
        rows = [[(f"{p['title']} · {price_label(p['price_rub'])}"[:60], f"cat:p:{p['id']}")]
                for p in packages]
        rows.append([("⬅️ Направление", f"cat:cat:{service['category']}")])
        package_lines = "\n".join(f"• {p['title']} — {price_label(p['price_rub'])}" for p in packages)
        return _send(user_id, f"✨ {service['title']}\n\n{service['description']}\n\n"
                     f"{package_lines or 'Пакетов пока нет.'}\n\nВыбери пакет. Примеры работ: /portfolio", rows)

    def _package(self, user_id: int, package_id: int) -> dict:
        package = self._package_row(package_id)
        service = self._service_row(package["service_id"]) if package else None
        if not package or not service or not package["active"] or not service["active"]:
            return _send(user_id, "Этот пакет сейчас недоступен. /services")
        return _send(user_id, f"📦 {service['title']} — {package['title']}\n\n"
                     f"{package['description']}\n\nСтоимость: {price_label(package['price_rub'])}\n"
                     "Итоговые условия обсудим перед началом работ.", [
                         [("📝 Заказать пакет", f"cat:buy:{package_id}")],
                         [("⬅️ К услуге", f"cat:s:{service['id']}")],
                     ])

    def _callback(self, user_id: int, data: str) -> list[dict]:
        if data.startswith("cat:admin"):
            return self._admin_callback(user_id, data) if user_id == self.owner_id else []
        if data.startswith("cat:buy:"):
            item_id = _id(data[len("cat:buy:"):])
            return [self._start_order(user_id, item_id)] if item_id else [self._home(user_id)]
        if data == "cat:confirm":
            return self._confirm_order(user_id)
        if data == "cat:cancel":
            self.order_drafts.pop(user_id, None)
            return [_send(user_id, "Заявка отменена. /services")]
        if data == "cat:back":
            return [self._back_order(user_id)]
        if data in ("cat:home", "mkt:services", "mkt:order"):
            return [self._home(user_id)]
        if data.startswith("cat:cat:"):
            return [self._category(user_id, data[8:])]
        if data.startswith("cat:s:"):
            item_id = _id(data[6:])
            return [self._service(user_id, item_id)] if item_id else [self._home(user_id)]
        if data.startswith("cat:p:"):
            item_id = _id(data[6:])
            return [self._package(user_id, item_id)] if item_id else [self._home(user_id)]
        return [_send(user_id, "Кнопка недоступна. /services")]

    @staticmethod
    def _order_buttons() -> list[list[tuple[str, str]]]:
        return [[("⬅️ Назад", "cat:back"), ("❌ Отменить", "cat:cancel")]]

    def _start_order(self, user_id: int, package_id: int) -> dict:
        pkg = self._package_row(package_id)
        service = self._service_row(pkg["service_id"]) if pkg else None
        if not pkg or not service or not pkg["active"] or not service["active"]:
            return _send(user_id, "Пакет сейчас недоступен. Вернись в /services")
        self.order_drafts[user_id] = {
            "step": "brief", "updated_at": int(time.time()),
            "service_id": service["id"], "package_id": pkg["id"],
            "service_key": service["category"], "service_title": service["title"],
            "package_title": pkg["title"], "price_rub": pkg["price_rub"],
        }
        return _send(user_id, f"📝 Заказ: {service['title']} — {pkg['title']}\n\n"
                     "Шаг 1/3: опиши задачу (до 900 символов). Не присылай пароли или секреты.",
                     self._order_buttons())

    def _order_input(self, user_id: int, body: str) -> dict:
        draft = self.order_drafts[user_id]
        if int(time.time()) - draft["updated_at"] > 30 * 60:
            self.order_drafts.pop(user_id, None)
            return _send(user_id, "Время оформления истекло. Начни заново через /services")
        if SECRET_PATTERN.search(body):
            return _send(user_id, "Похоже, это секретный токен. Не отправляй секреты — опиши задачу без них.")
        value = body.strip()
        step = draft["step"]
        if step == "brief":
            if not 1 <= len(value) <= 900:
                return _send(user_id, "Описание задачи должно содержать от 1 до 900 символов.")
            draft.update(brief=value, step="deadline", updated_at=int(time.time()))
            return _send(user_id, "📅 Шаг 2/3: к какому сроку нужен результат? (до 80 символов).",
                         self._order_buttons())
        if step == "deadline":
            if not 1 <= len(value) <= 80:
                return _send(user_id, "Напиши желаемый срок короче — до 80 символов.")
            draft.update(deadline=value, step="contact", updated_at=int(time.time()))
            return _send(user_id, "👤 Шаг 3/3: как с тобой связаться? Можно написать «здесь, в Telegram».\n"
                         "Не отправляй пароли (до 160 символов).", self._order_buttons())
        if step == "contact":
            if not 1 <= len(value) <= 160:
                return _send(user_id, "Укажи способ связи в пределах 160 символов.")
            draft.update(contact=value, step="review", updated_at=int(time.time()))
            return self._order_review(user_id)
        return self._order_review(user_id)

    def _order_review(self, user_id: int) -> dict:
        draft = self.order_drafts[user_id]
        return _send(user_id, "✅ Проверь заявку перед отправкой:\n\n" + self._order_summary(draft), [
            [("📩 Подтвердить и отправить", "cat:confirm")],
            [("⬅️ Исправить контакт", "cat:back"), ("❌ Отменить", "cat:cancel")],
        ])

    @staticmethod
    def _order_summary(draft: dict) -> str:
        return (f"Услуга: {draft['service_title']}\nПакет: {draft['package_title']}\n"
                f"Стоимость: {price_label(draft['price_rub'])}\n\n"
                f"Задача: {draft['brief']}\nСрок: {draft['deadline']}\nКонтакт: {draft['contact']}")

    def _back_order(self, user_id: int) -> dict:
        draft = self.order_drafts.get(user_id)
        if not draft:
            return _send(user_id, "Активной заявки нет. /services")
        step = draft["step"]
        draft["updated_at"] = int(time.time())
        if step == "review":
            draft["step"] = "contact"
            return _send(user_id, "✏️ Напиши новый контакт или «здесь, в Telegram».", self._order_buttons())
        if step == "contact":
            draft["step"] = "deadline"
            return _send(user_id, "✏️ Напиши новый желаемый срок.", self._order_buttons())
        if step == "deadline":
            draft["step"] = "brief"
            return _send(user_id, "✏️ Напиши новое описание задачи.", self._order_buttons())
        self.order_drafts.pop(user_id, None)
        return self._package(user_id, draft["package_id"])

    def _confirm_order(self, user_id: int) -> list[dict]:
        draft = self.order_drafts.get(user_id)
        if not draft or draft["step"] != "review":
            return [_send(user_id, "Нет заявки для подтверждения. /services")]
        if int(time.time()) - draft["updated_at"] > 30 * 60:
            self.order_drafts.pop(user_id, None)
            return [_send(user_id, "Время оформления истекло. /services")]
        pkg = self._package_row(draft["package_id"])
        service = self._service_row(draft["service_id"])
        if not pkg or not service or not pkg["active"] or not service["active"]:
            self.order_drafts.pop(user_id, None)
            return [_send(user_id, "Пакет сейчас недоступен. Выбери другое предложение через /services")]
        actions = self.marketing.submit_catalog_order(
            user_id, draft["service_key"], self._order_summary(draft))
        self.order_drafts.pop(user_id, None)
        return actions

    def _admin_home(self, user_id: int) -> dict:
        rows = [[(f"➕ {label}", f"cat:admin:add:{key}")] for key, label in CATEGORIES.items()]
        rows.extend([
            [("✏️ Изменить услуги и пакеты", "cat:admin:list")],
            [("⬅️ Каталог", "cat:home")],
        ])
        return _send(user_id, "⚙️ Управление каталогом KYNTRION\n\n"
                     "Создай услугу в нужном направлении или выбери существующую для изменения.", rows)

    def _admin_list(self, user_id: int) -> dict:
        services = self.list_services()[:50]
        rows = [[(f"{'✅' if row['active'] else '🔒'} {row['title'][:45]}",
                  f"cat:admin:svc:{row['id']}")] for row in services]
        rows.append([("⬅️ Управление", "cat:admin")])
        return _send(user_id, "✏️ Выбери услугу для редактирования:", rows)

    def _admin_service(self, user_id: int, service_id: int) -> dict:
        service = self._service_row(service_id)
        if service is None:
            return _send(user_id, "Услуга не найдена. /catalog_manage")
        return _send(user_id, f"⚙️ {service['title']}\n\n{service['description']}\n\n"
                     f"Статус: {'Опубликована' if service['active'] else 'Скрыта'}", [
                         [("✏️ Название", f"cat:admin:edit:title:{service_id}"),
                          ("📝 Описание", f"cat:admin:edit:description:{service_id}")],
                         [("📦 Пакеты", f"cat:admin:pkglist:{service_id}"),
                          ("➕ Пакет", f"cat:admin:addpkg:{service_id}")],
                         [("👁 Скрыть / показать", f"cat:admin:toggle:s:{service_id}")],
                         [("⬅️ Все услуги", "cat:admin:list")],
                     ])

    def _admin_packages(self, user_id: int, service_id: int) -> dict:
        service = self._service_row(service_id)
        if service is None:
            return _send(user_id, "Услуга не найдена. /catalog_manage")
        packages = self.list_packages(service_id)[:40]
        rows = [[(f"{'✅' if pkg['active'] else '🔒'} {pkg['title'][:35]} · {price_label(pkg['price_rub'])}"[:60],
                  f"cat:admin:pkg:{pkg['id']}")] for pkg in packages]
        rows.append([("➕ Добавить пакет", f"cat:admin:addpkg:{service_id}")])
        rows.append([("⬅️ Услуга", f"cat:admin:svc:{service_id}")])
        return _send(user_id, f"📦 Пакеты услуги «{service['title']}»:", rows)

    def _admin_package(self, user_id: int, package_id: int) -> dict:
        pkg = self._package_row(package_id)
        if pkg is None:
            return _send(user_id, "Пакет не найден. /catalog_manage")
        return _send(user_id, f"📦 {pkg['title']}\n\n{pkg['description']}\n\n"
                     f"Стоимость: {price_label(pkg['price_rub'])}\n"
                     f"Статус: {'Опубликован' if pkg['active'] else 'Скрыт'}", [
                         [("✏️ Название", f"cat:admin:editpkg:title:{package_id}"),
                          ("📝 Состав", f"cat:admin:editpkg:description:{package_id}")],
                         [("💰 Цена", f"cat:admin:editpkg:price:{package_id}"),
                          ("👁 Скрыть / показать", f"cat:admin:toggle:p:{package_id}")],
                         [("🗑 Удалить", f"cat:admin:delask:p:{package_id}")],
                         [("⬅️ Пакеты", f"cat:admin:pkglist:{pkg['service_id']}")],
                     ])

    def _new_admin_draft(self, user_id: int, *, step: str, **fields: object) -> None:
        self.admin_drafts[user_id] = {"step": step, "updated_at": int(time.time()), **fields}

    def _admin_callback(self, user_id: int, data: str) -> list[dict]:
        if data == "cat:admin":
            return [self._admin_home(user_id)]
        if data == "cat:admin:list":
            return [self._admin_list(user_id)]
        if data.startswith("cat:admin:add:"):
            category = data[len("cat:admin:add:"):]
            if category not in CATEGORIES:
                return [_send(user_id, "Категория недоступна.")]
            self._new_admin_draft(user_id, step="add_service_title", category=category)
            return [_send(user_id, "✍️ Название услуги (1–80 символов). Отмена: /catalog_cancel")]
        if data.startswith("cat:admin:svc:"):
            item_id = _id(data[len("cat:admin:svc:"):])
            return [self._admin_service(user_id, item_id)] if item_id else [self._admin_list(user_id)]
        if data.startswith("cat:admin:pkglist:"):
            item_id = _id(data[len("cat:admin:pkglist:"):])
            return [self._admin_packages(user_id, item_id)] if item_id else [self._admin_list(user_id)]
        if data.startswith("cat:admin:pkg:"):
            item_id = _id(data[len("cat:admin:pkg:"):])
            return [self._admin_package(user_id, item_id)] if item_id else [self._admin_list(user_id)]
        if data.startswith("cat:admin:addpkg:"):
            item_id = _id(data[len("cat:admin:addpkg:"):])
            if not item_id or not self._service_row(item_id):
                return [_send(user_id, "Услуга не найдена.")]
            self._new_admin_draft(user_id, step="add_pkg_title", service_id=item_id)
            return [_send(user_id, "✍️ Название пакета (1–80 символов). Отмена: /catalog_cancel")]
        if data.startswith("cat:admin:editpkg:"):
            parts = data.split(":")
            if len(parts) != 5 or parts[3] not in ("title", "description", "price"):
                return [_send(user_id, "Неизвестное поле.")]
            item_id = _id(parts[4])
            if not item_id or not self._package_row(item_id):
                return [_send(user_id, "Пакет не найден.")]
            self._new_admin_draft(user_id, step=f"edit_pkg_{parts[3]}", package_id=item_id)
            return [_send(user_id, "Введи новое значение (цена: целое число рублей или «по запросу»).")]
        if data.startswith("cat:admin:edit:"):
            parts = data.split(":")
            if len(parts) != 5 or parts[3] not in ("title", "description"):
                return [_send(user_id, "Неизвестное поле.")]
            item_id = _id(parts[4])
            if not item_id or not self._service_row(item_id):
                return [_send(user_id, "Услуга не найдена.")]
            self._new_admin_draft(user_id, step=f"edit_service_{parts[3]}", service_id=item_id)
            return [_send(user_id, "Введи новое название (до 80) или описание (до 400).")]
        if data.startswith("cat:admin:toggle:"):
            parts = data.split(":")
            if len(parts) != 5 or parts[3] not in ("s", "p"):
                return [_send(user_id, "Неизвестный объект.")]
            item_id = _id(parts[4])
            table = "catalog_services" if parts[3] == "s" else "catalog_packages"
            if item_id:
                with self.db:
                    self.db.execute(f"UPDATE {table} SET active=1-active WHERE id=?", (item_id,))
            return [self._admin_service(user_id, item_id) if parts[3] == "s"
                    else self._admin_package(user_id, item_id)]
        if data.startswith("cat:admin:delask:p:"):
            item_id = _id(data[len("cat:admin:delask:p:"):])
            pkg = self._package_row(item_id) if item_id else None
            if pkg is None:
                return [_send(user_id, "Пакет не найден.")]
            self.pending_deletes[user_id] = (item_id, int(time.time()))
            return [_send(user_id, f"Удалить пакет «{pkg['title']}»? Существующие заявки сохранятся.", [
                [("🗑 Да, удалить", f"cat:admin:del:p:{item_id}")],
                [("Отмена", f"cat:admin:pkg:{item_id}")],
            ])]
        if data.startswith("cat:admin:del:p:"):
            item_id = _id(data[len("cat:admin:del:p:"):])
            pending = self.pending_deletes.pop(user_id, None)
            if not pending or pending[0] != item_id or int(time.time()) - pending[1] > 5 * 60:
                return [_send(user_id, "Сначала подтверди удаление через меню пакета.")]
            pkg = self._package_row(item_id) if item_id else None
            if pkg:
                with self.db:
                    self.db.execute("DELETE FROM catalog_packages WHERE id=?", (item_id,))
                return [self._admin_packages(user_id, pkg["service_id"])]
            return [_send(user_id, "Пакет уже удалён.")]
        return [_send(user_id, "Кнопка устарела. /catalog_manage")]

    def _admin_input(self, user_id: int, body: str) -> dict:
        draft = self.admin_drafts[user_id]
        if int(time.time()) - draft["updated_at"] > 15 * 60:
            self.admin_drafts.pop(user_id, None)
            return _send(user_id, "Срок ввода истек. Открой /catalog_manage заново.")
        step = draft["step"]
        if step in ("add_service_title", "add_pkg_title", "edit_service_title", "edit_pkg_title"):
            if not 1 <= len(body.strip()) <= 80 or body.startswith("/"):
                return _send(user_id, "Название должно содержать от 1 до 80 символов.")
        if step in ("add_service_description", "add_pkg_description", "edit_service_description", "edit_pkg_description"):
            if not 1 <= len(body.strip()) <= 400 or body.startswith("/"):
                return _send(user_id, "Описание должно содержать от 1 до 400 символов.")
        value = body.strip()
        if step in ("add_pkg_price", "edit_pkg_price"):
            valid, price = parse_price(value)
            if not valid:
                return _send(user_id, "Цена: целое число рублей до 99999999 или «по запросу».")
        if step == "add_service_title":
            draft.update(title=value, step="add_service_description")
            return _send(user_id, "📝 Отправь описание услуги (до 400 символов).")
        if step == "add_service_description":
            with self.db:
                item_id = self.db.execute(
                    "INSERT INTO catalog_services(category,title,description,created_at) VALUES(?,?,?,?)",
                    (draft["category"], draft["title"], value, int(time.time())),
                ).lastrowid
            self.admin_drafts.pop(user_id, None)
            return self._admin_service(user_id, item_id)
        if step == "add_pkg_title":
            draft.update(title=value, step="add_pkg_description")
            return _send(user_id, "📝 Отправь состав пакета (до 400 символов).")
        if step == "add_pkg_description":
            draft.update(description=value, step="add_pkg_price")
            return _send(user_id, "💰 Цена в рублях или «по запросу».")
        if step == "add_pkg_price":
            with self.db:
                package_id = self.db.execute(
                    "INSERT INTO catalog_packages(service_id,title,description,price_rub,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (draft["service_id"], draft["title"], draft["description"], price, int(time.time())),
                ).lastrowid
            self.admin_drafts.pop(user_id, None)
            return self._admin_package(user_id, package_id)
        if step.startswith("edit_service_"):
            field = step[len("edit_service_"):]
            if field not in ("title", "description"):
                raise ValueError("Unknown service field")
            with self.db:
                self.db.execute(f"UPDATE catalog_services SET {field}=? WHERE id=?",
                                (value, draft["service_id"]))
            self.admin_drafts.pop(user_id, None)
            return self._admin_service(user_id, draft["service_id"])
        if step.startswith("edit_pkg_"):
            field = step[len("edit_pkg_"):]
            if field not in ("title", "description", "price"):
                raise ValueError("Unknown package field")
            col = "price_rub" if field == "price" else field
            stored = price if field == "price" else value
            with self.db:
                self.db.execute(f"UPDATE catalog_packages SET {col}=? WHERE id=?",
                                (stored, draft["package_id"]))
            self.admin_drafts.pop(user_id, None)
            return self._admin_package(user_id, draft["package_id"])
        self.admin_drafts.pop(user_id, None)
        return _send(user_id, "Черновик сброшен. /catalog_manage")
