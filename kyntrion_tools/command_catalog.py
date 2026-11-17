"""User-visible help and aliases generated from tested command plugins."""

from __future__ import annotations

from kyntrion_tools.command_plugins.registry import PLUGINS, BY_ALIAS, BY_TOPIC


ALIASES = {alias: spec.topic for alias, spec in BY_ALIAS.items()}

SLASH_TOPICS = {
    "/start": "start", "/help": "help", "/text": "txt", "/voice": "text",
    "/collect": "collect", "/done": "done", "/cancel": "cancel",
    "/status": "status", "/services": "services", "/portfolio": "portfolio",
    "/consult": "consult", "/crm": "crm", "/myid": "myid",
    "/contacts": "contacts", "/order": "order", "/crm_cancel": "crm_cancel",
    "/catalog_manage": "catalog_manage", "/portfolio_manage": "portfolio_manage",
    "/portfolio_stats": "portfolio_stats", "/cancel_order": "cancel_order",
    "/cancel_consult": "cancel_consult", "/review_consult": "review_consult",
    "/catalog_cancel": "catalog_cancel", "/cancel_portfolio": "cancel_portfolio",
    "/leads": "leads", "/campaign_stats": "campaign_stats", "/privacy": "privacy",
    "/forget_me": "forget_me", "/marketing_on": "marketing_on",
    "/marketing_off": "marketing_off",
}


def parse_dot(text: object) -> tuple[str, str] | None:
    if not isinstance(text, str):
        return None
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    spec = BY_ALIAS.get(parts[0].casefold())
    if spec is None:
        return None
    if not spec.takes_argument and len(parts) == 2:
        return None
    return spec.topic, parts[1].strip() if len(parts) == 2 else ""


def canonical_topic(text: str) -> str | None:
    word = text.strip().split(maxsplit=1)[0].casefold() if text.strip() else ""
    if word in ALIASES:
        return ALIASES[word]
    if "." + word in ALIASES:
        return ALIASES["." + word]
    return SLASH_TOPICS.get(word, word if word in GUIDES else None)


# Help sections are derived from registered, implemented commands only.
_group_names = dict.fromkeys(spec.category for spec in PLUGINS)
HELP_GROUPS = tuple((category, tuple(
    (spec.aliases[0], spec.topic, spec.summary)
    for spec in PLUGINS if spec.category == category
)) for category in _group_names)
HELP_TOPICS = frozenset(BY_TOPIC)


def navigation_text() -> str:
    lines = ["🧭 KYNTRION — Навигация по командам", ""]
    for title, commands in HELP_GROUPS:
        lines.append(title)
        lines.extend(f"  • {command} — {description}" for command, _, description in commands)
        lines.append("")
    lines.extend((
        "Нажми на кнопку с названием команды ниже, чтобы узнать, как она работает.",
        "Русские варианты (.текст, .тхт, .тайп, .цитата, .любовь, .сбор, .готово, .статус, .отмена, .хелп) работают так же.",
        "В остальных чатах удаление команды возможно только при наличии прав бота.",
    ))
    return "\n".join(lines)


def help_keyboard(topic: str = "") -> dict:
    """Create an inline keyboard, with demos for the two animated commands."""
    if topic:
        rows = []
        if topic in ("type", "love"):
            rows.append([{"text": "▶️ Посмотреть пример", "callback_data": f"help:demo:{topic}"}])
        rows.append([{"text": "⬅️ К списку команд", "callback_data": "help:menu"}])
        return {"inline_keyboard": rows}
    buttons = [
        {"text": command, "callback_data": f"help:{name}"}
        for _, entries in HELP_GROUPS for command, name, _ in entries
    ]
    return {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}


def decode_help_callback(data: object) -> str | None:
    if not isinstance(data, str) or not data.startswith("help:"):
        return None
    topic = data[len("help:"):]
    if topic in ("demo:type", "demo:love"):
        return topic
    return topic if topic == "menu" or topic in HELP_TOPICS else None


GUIDES = {
    **{spec.topic: spec.guide for spec in PLUGINS},
    "start": "🏠 /start — открыть главное меню KYNTRION и кнопки бота.",
    "services": "🚀 /services — каталог услуг KYNTRION и переход к заказу.",
    "portfolio": "🎨 /portfolio — кейсы и примеры работ KYNTRION.",
    "consult": "🤖 /consult — ИИ-консультация с отдельным подтверждением заявки.",
    "crm": "🧩 /crm — управление заявками и предложениями (только владелец).",
    "myid": "🆔 /myid — показать свой Telegram ID для настройки доступа.",
    "contacts": "👤 /contacts — контакты разработчика и ресурсы KYNTRION.",
    "order": "🛍 /order — перейти к оформлению заявки, если приём заявок настроен.",
    "crm_cancel": "🛑 /crm_cancel — отменить редактирование заявки владельцем.",
    "catalog_manage": "⚙️ /catalog_manage — редактирование услуг и пакетов (только владелец).",
    "portfolio_manage": "🎨 /portfolio_manage — добавление и редактирование кейсов (только владелец).",
    "portfolio_stats": "📈 /portfolio_stats — статистика портфолио владельца.",
    "cancel_order": "🔕 /cancel_order — отменить оформление заказа, пока заявка не отправлена.",
    "cancel_consult": "🛑 /cancel_consult — отменить текущую консультацию.",
    "review_consult": "✅ /review_consult — вернуться к итогам консультации для проверки перед отправкой заявки.",
    "catalog_cancel": "🛑 /catalog_cancel — отменить редактирование каталога услуг (владелец).",
    "cancel_portfolio": "🛑 /cancel_portfolio — отменить создание или редактирование кейса (владелец).",
    "leads": "📊 /leads — посмотреть полученные заявки (только владелец).",
    "campaign_stats": "📈 /campaign_stats — обезличенные счётчики переходов по партнёрским ссылкам (владелец).",
    "privacy": "🔐 /privacy — условия хранения данных и доступные настройки приватности.",
    "forget_me": "🗑 /forget_me — удалить свои персональные заявки и настройки из базы бота.",
    "marketing_on": "📣 /marketing_on — согласиться на будущие предложения от KYNTRION.",
    "marketing_off": "🔕 /marketing_off — отказаться от будущих предложений.",
}


def help_text(topic: str = "", *, api=None) -> str:
    if not topic:
        return navigation_text()
    normalized = canonical_topic(topic)
    if getattr(api, "platform", None) == "vk":
        from kyntrion_tools.vk_menu import VK_GUIDES
        if normalized in VK_GUIDES:
            return VK_GUIDES[normalized]
    if normalized in GUIDES:
        return GUIDES[normalized]
    return "Не нашёл такую команду. Отправь .help или .хелп для общего списка."
