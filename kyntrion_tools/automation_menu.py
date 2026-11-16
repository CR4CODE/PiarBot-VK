"""Nested, allowlisted Telegram navigation for registered dot commands."""

from __future__ import annotations

from kyntrion_tools.bot_menu import inline_keyboard
from kyntrion_tools.command_catalog import help_keyboard, help_text
from kyntrion_tools.command_plugins.registry import PLUGINS


GROUPS = (
    ("text", "📄 Текст и файлы", frozenset(("text", "txt"))),
    ("effects", "✨ Эффекты и оформление", frozenset(("type", "quote", "love"))),
    ("collect", "📚 Сбор сообщений", frozenset(("collect", "done", "status", "cancel"))),
)
KNOWN_TOPICS = frozenset(topic for _, _, topics in GROUPS for topic in topics)
HELP_TOPIC = "help"
EXAMPLES = {
    "chats": ("chat-automation-chats.jpg",
              "Шаг 1 — выбери @KyntrionBot и чаты, к которым разрешён доступ."),
    "permissions": ("chat-automation-permissions.png",
                    "Шаг 2 — пример настройки прав для сообщений. Включай только нужные права."),
}


def _group_items(key: str) -> tuple:
    if key == "other":
        return tuple(spec for spec in PLUGINS
                     if spec.topic not in KNOWN_TOPICS and spec.topic != HELP_TOPIC)
    return tuple(spec for spec in PLUGINS
                 if any(group == key and spec.topic in topics for group, _, topics in GROUPS))


def _category_for(topic: str) -> str | None:
    if topic == HELP_TOPIC:
        return None
    for key, _, topics in GROUPS:
        if topic in topics:
            return key
    return "other"


def _button(label: str, callback: str) -> dict:
    return {"text": label, "callback_data": callback}


def _keyboard(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def _back_to_categories() -> list[dict]:
    return [_button("⬅️ К категориям", "auto:menu")]


def automation_example(key: str) -> tuple[str, str] | None:
    """Return an image asset and caption only for a trusted, fixed example key."""
    return EXAMPLES.get(key) if isinstance(key, str) else None


def decode_automation_callback(data: object) -> str | None:
    """Decode only existing categories and registered topics, never raw user input."""
    if not isinstance(data, str) or not data.startswith("auto:"):
        return None
    page = data[len("auto:"):]
    if page in ("menu", "help", "back", "setup", "examples", "monitor"):
        return page
    if page.startswith("photo:") and automation_example(page[len("photo:"):]) is not None:
        return page
    if page.startswith("cat:"):
        key = page[len("cat:"):]
        if key in {group for group, _, _ in GROUPS} or (key == "other" and _group_items(key)):
            return page
    if page.startswith("topic:"):
        topic = page[len("topic:"):]
        if any(spec.topic == topic for spec in PLUGINS) and topic != HELP_TOPIC:
            return page
    return None


def automation_page(page: str) -> tuple[str, dict] | None:
    """Return the message text and inline keyboard for a validated menu page."""
    if decode_automation_callback("auto:" + page) != page:
        return None
    if page == "back":
        return "Что сделаем первым?", inline_keyboard("start")
    if page == "menu":
        rows = [[_button(title, "auto:cat:" + key)] for key, title, _ in GROUPS
                if _group_items(key)]
        if _group_items("other"):
            rows.append([_button("📦 Другие команды", "auto:cat:other")])
        rows.extend(([_button("🛡 Контроль сообщений", "auto:monitor")],
                     [_button("📖 Подключить бота", "auto:setup")],
                     [_button("❓ Помощь по автоматизации", "auto:help")],
                     [_button("⬅️ Главное меню", "auto:back")]))
        return ("⚡ KYNTRION — Автоматизация в чатах\n\n"
                "Выбери категорию, затем команду. Бот покажет инструкцию и примеры."), _keyboard(rows)
    if page == "monitor":
        return (
            "🛡 KYNTRION — Контроль сообщений\n\n"
            "Этот раздел показывает удалённый текст и изменения, если сообщение "
            "было получено после подключения и добровольного включения мониторинга. "
            "Доступные фото, видео и голосовые тоже можно сохранять на ограниченный срок.\n\n"
            "После включения модуля здесь появятся персональные настройки.",
            _keyboard([
                [{"text": "🔗 Подключить бота", "url": "tg://settings/edit"}],
                [_button("📖 Инструкция", "auto:setup")],
                _back_to_categories(),
            ]),
        )
    if page == "setup":
        return (
            "🔗 Подключение @KyntrionBot к профилю Telegram\n\n"
            "1. Открой настройки профиля кнопкой ниже. Если раздел не виден, "
            "пройди вручную: Настройки → Автоматизация чатов.\n"
            "2. Выбери подключение бота и укажи @KyntrionBot.\n"
            "3. Определи чаты, доступные боту: «Только выбранные чаты» или "
            "«Все личные чаты, кроме…». При необходимости добавь исключения.\n"
            "4. Включи необходимые права для сообщений. Для команд с удалением нужны "
            "соответствующие разрешения; другие права включать необязательно.\n"
            "5. Нажми галочку ✓ для сохранения.\n\n"
            "Подключение доступно обычным пользователям; платная подписка не требуется. "
            "Доступ к профилю, подаркам и историям для этих команд не нужен.\n\n"
            "Важно: подключение открывает боту доступ к выбранным личным перепискам. "
            "Для групповых команд используются обычные права бота в группе."
        ), _keyboard([
            [{"text": "🔗 Подключить автоматизацию", "url": "tg://settings/edit"}],
            [_button("🖼 Примеры настройки", "auto:examples")],
            _back_to_categories(),
        ])
    if page == "examples":
        return (
            "🖼 Примеры настройки KYNTRION\n\n"
            "Выбери, какой скриншот показать в личном чате:\n"
            "1️⃣ Доступ к чатам и исключения.\n"
            "2️⃣ Разрешения для сообщений.\n\n"
            "Это примеры. Не обязательно включать все разрешения для каждой функции."
        ), _keyboard([
            [_button("1️⃣ Доступ к чатам", "auto:photo:chats")],
            [_button("2️⃣ Права для сообщений", "auto:photo:permissions")],
            [_button("⬅️ К инструкции", "auto:setup")],
        ])
    if page == "help":
        return (
            "❓ Помощь по автоматизации KYNTRION\n\n"
            "• Команды начинаются с точки: .text, .type, .love и другие.\n"
            "• Напиши команду в личке или доступном боту чате. Для обработки выбранного "
            "сообщения используй ответ на него, если это указано в инструкции.\n"
            "• Бот старается удалить сообщение с командой; в группах нужны право удаления "
            "и доступ к сообщениям.\n"
            "• Нажми категорию и выбери команду, чтобы увидеть синтаксис и пример. "
            "Для .type и .love есть кнопка демонстрации.\n"
            "• Команда .help также открывает общий справочник команд."
        ), _keyboard([
            [_button("📖 Как подключить бота", "auto:setup")],
            _back_to_categories(),
        ])
    if page.startswith("cat:"):
        key = page[4:]
        label = next((title for group, title, _ in GROUPS if group == key), "📦 Другие команды")
        specs = _group_items(key)
        labels = [f"• {spec.aliases[0]} — {spec.summary}" for spec in specs]
        choices = [_button(spec.aliases[0], "auto:topic:" + spec.topic) for spec in specs]
        rows = [choices[i:i + 2] for i in range(0, len(choices), 2)]
        rows.append(_back_to_categories())
        return f"{label}\n\n" + "\n".join(labels) + "\n\nНажми команду для инструкции:", _keyboard(rows)
    if page.startswith("topic:"):
        topic = page[6:]
        key = _category_for(topic)
        rows = [row for row in help_keyboard(topic)["inline_keyboard"]
                if any(button["callback_data"].startswith("help:demo:") for button in row)]
        rows.extend(([_button("⬅️ К командам", "auto:cat:" + key)],
                     _back_to_categories()))
        return help_text(topic), _keyboard(rows)
    return None
