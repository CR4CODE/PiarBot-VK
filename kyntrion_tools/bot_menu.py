"""Telegram reply/inline keyboard layouts and safe label lookup."""

from __future__ import annotations


ROWS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("📄 TXT-файл", "/text"), ("🎙 Голос → текст", "/voice")),
    (("📚 Собрать контекст", "/collect"), ("✅ Получить файл", "/done")),
    (("🚀 Услуги", "/services"), ("🤖 ИИ-консультация", "/consult")),
    (("🎨 Портфолио", "/portfolio"), ("👤 Контакты", "/contacts")),
    (("⚡ Автоматизация в чатах", "/automation"),),
    (("⚙️ Помощь", "/help"),),
)

BUTTON_COMMANDS = {
    **{label: command for row in ROWS for label, command in row},
    # Previously displayed reply keyboards can survive Telegram bot updates.
    "📄 Текст → TXT": "/text",
    "🎙 Расшифровать голос": "/voice",
    "🚀 Наши услуги": "/services",
    "📝 Заказать": "/order",
}

INLINE_LAYOUTS: dict[str, tuple[tuple[tuple[str, str], ...], ...]] = {
    "start": (
        (("📄 TXT", "ui:text"), ("🎙 Голосовые", "ui:voice")),
        (("📚 Начать сбор", "ui:collect"), ("🚀 Услуги", "ui:services")),
        (("🎨 Портфолио", "ui:portfolio"), ("🤖 ИИ-консультация", "ui:consult")),
        (("⚡ Автоматизация в чатах", "ui:automation"), ("⚙️ Помощь", "ui:help")),
    ),
    "collect": (
        (("✅ Готово", "ui:done"), ("📊 Статус", "ui:status")),
        (("❌ Отменить", "ui:cancel"),),
    ),
    "txt": (
        (("📚 Начать сбор", "ui:collect"), ("🚀 Услуги", "ui:services")),
    ),
    "voice": (
        (("📚 Собрать контекст", "ui:collect"), ("🚀 Услуги", "ui:services")),
    ),
}

CALLBACK_COMMANDS = {
    "ui:text": "/text",
    "ui:voice": "/voice",
    "ui:collect": "/collect",
    "ui:done": "/done",
    "ui:status": "/status",
    "ui:cancel": "/cancel",
    "ui:services": "/services",
    "ui:order": "/order",
    "ui:contacts": "/contacts",
    "ui:portfolio": "/portfolio",
    "ui:help": "/help",
    "ui:automation": "/automation",
    "ui:consult": "/consult",
}

# Post Studio builds a fresh owner-bound callback for every control. This list
# defines only labels and action types; no raw draft IDs enter callback_data.
POST_STUDIO_EDITOR_ACTIONS: tuple[tuple[str, str], ...] = (
    ("✏️ Текст", "text"),
    ("🖼 Фото / видео", "media"),
    ("📝 Подпись", "caption"),
    ("🔗 URL-кнопки", "button"),
    ("👁 Предпросмотр", "preview"),
    ("🚀 Опубликовать", "publish"),
    ("📅 Отложить", "schedule"),
    ("📊 Статистика", "stats"),
    ("💾 Сохранить", "save"),
    ("❌ Выйти", "cancel"),
)


def reply_keyboard(*, include_post_studio: bool = False) -> dict:
    rows = [[{"text": "🎨 Студия постов"}]] if include_post_studio else []
    rows.extend([[{"text": title} for title, _ in row] for row in ROWS])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": False,
        "one_time_keyboard": False,
        "input_field_placeholder": "Перешли текст или голосовое…",
    }


def inline_keyboard(section: str) -> dict:
    rows = INLINE_LAYOUTS[section]
    return {
        "inline_keyboard": [
            [{"text": title, "callback_data": data} for title, data in row]
            for row in rows
        ]
    }


def decode_reply_button(label: str) -> str | None:
    return BUTTON_COMMANDS.get(label)


def callback_command(data: str) -> str | None:
    return CALLBACK_COMMANDS.get(data)
