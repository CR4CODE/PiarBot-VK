"""Run KYNTRION Text Bot using Telegram long polling (no webhook required)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from kyntrion_tools.bot import BotController
from kyntrion_tools.bot_menu import callback_command, decode_reply_button, inline_keyboard, reply_keyboard
from kyntrion_tools.kyntrion_ai_client import AIClient
from kyntrion_tools.kyntrion_catalog import CatalogAddon
from kyntrion_tools.kyntrion_consult import ConsultationAddon
from kyntrion_tools.kyntrion_crm import CRMAddon
from kyntrion_tools.kyntrion_marketing import MarketingAddon, MarketingConfig
from kyntrion_tools.kyntrion_portfolio import PortfolioAddon
from kyntrion_tools.telegram_api import TelegramApi, TelegramApiError
from kyntrion_tools.voice_command_journal import VoiceCommandJournal
from kyntrion_tools.voice_text_commands import VoiceTextRouter
from kyntrion_tools.command_catalog import decode_help_callback, help_keyboard, help_text, parse_dot
from kyntrion_tools.automation_menu import automation_example, automation_page, decode_automation_callback
from kyntrion_tools.chat_monitor import ChatMonitor
from kyntrion_tools.monitor_store import MonitorStore
from kyntrion_tools.post_studio import PostStudioService
from kyntrion_tools.post_studio_scheduler import run_due
from kyntrion_tools.post_studio_store import PostStudioStore
from kyntrion_tools.broadcast_notifications import NotificationHandler, NotificationStore
from kyntrion_tools.notification_panel import OwnerNotificationPanel


LOG = logging.getLogger("kyntrion_text_bot")


def _send_menu(api: TelegramApi, chat_id: int, command: str,
               post_studio_enabled: bool = False,
               notifications_enabled: bool = False,
               notifications_panel: OwnerNotificationPanel | None = None) -> bool:
    """Show a top-level menu page; return True only for owned commands."""
    if getattr(api, "platform", None) == "vk":
        from kyntrion_tools.vk_menu import send_vk_menu
        if send_vk_menu(api, chat_id, command, notifications_enabled, notifications_panel):
            return True
    if command == "/automation":
        page = automation_page("menu")
        if page is not None:
            text, markup = page
            api.send_action({"method": "sendMessage", "chat_id": chat_id,
                             "text": text, "reply_markup": markup})
        return True
    if command == "/help":
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": help_text(), "reply_markup": help_keyboard(),
        })
        return True
    if command == "/start":
        markup = inline_keyboard("start")
        if post_studio_enabled:
            markup = {**markup, "inline_keyboard": [
                [{"text": "🎨 Студия постов", "callback_data": "post:home"}],
                *markup.get("inline_keyboard", [])]}
        if notifications_panel is not None and chat_id == notifications_panel.owner_id:
            markup = {**markup, "inline_keyboard": [
                [notifications_panel.entry_button()],
                *markup.get("inline_keyboard", [])]}
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": (
                "👋 KYNTRION — инструменты для текста, голосовых и автоматизации.\n"
                "Интеллект, который действует.\n\n"
                "📄 Перешли сообщение — получишь TXT без сокращений.\n"
                "🎙 Перешли голосовое до 3 минут — получишь OGG и расшифровку в TXT.\n"
                "📚 Для нескольких сообщений включи сбор и нажми «Получить файл».\n\n"
                "🚀 В разделе услуг — разработка ботов, контент и оформление.\n"
                "Нажми кнопку под полем ввода или выбери действие ниже."
                + ("\n\n📣 Сервисные новости включены после обращения в личный чат. "
                   "Отписка: /news_off · Статус: /news_status."
                   if notifications_enabled else "")
            ),
            "reply_markup": reply_keyboard(include_post_studio=post_studio_enabled),
        })
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": "Что сделаем первым?",
            "reply_markup": markup,
        })
        return True
    if command == "/text":
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": "📄 Перешли сюда сообщение или вставь свой текст — я верну его полным файлом .txt.",
            "reply_markup": inline_keyboard("txt"),
        })
        return True
    if command == "/voice":
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": "🎙 Перешли голосовое Telegram до 3 минут. Пришлю исходный .ogg, распознанный текст и .txt-файл.",
            "reply_markup": inline_keyboard("voice"),
        })
        return True
    if command == "/myid":
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": f"🆔 Твой Telegram ID: {chat_id}\nДля владельца: укажи его как KYN_OWNER_CHAT_ID в защищённом конфиге VPS.",
        })
        return True
    return False


def _public_services(api: TelegramApi, chat_id: int, command: str) -> bool:
    """Informational catalogue while private lead delivery is not configured."""
    if command == "/services":
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": (
                "🚀 KYNTRION — цифровые услуги CR4CODE\n\n"
                "🤖 Боты, интеграции и автоматизация\n"
                "📢 Telegram-каналы под ключ и автопубликация\n"
                "🎨 Тексты, изображения и оформление\n"
                "🎬 Видео, Shorts, Reels и озвучка\n\n"
                "WHIEDA — один из кейсов. Направление услуг — разработка и цифровое продвижение."
            ),
            "reply_markup": {"inline_keyboard": [
                [{"text": "🎨 Портфолио", "callback_data": "ui:portfolio"},
                 {"text": "👤 Контакты", "callback_data": "ui:contacts"}],
                [{"text": "📝 Заказать", "callback_data": "ui:order"}],
            ]},
        })
        return True
    if command == "/portfolio":
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": "🎨 Кейсы: Telegram-бот KYNTRION (TXT и расшифровка речи), оформление и материалы WHIEDA, автоматизация проектов.",
            "reply_markup": {"inline_keyboard": [[{"text": "🚀 Наши услуги", "callback_data": "ui:services"}]]},
        })
        return True
    if command == "/contacts":
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": "👤 Разработчик: CR4CODE\n🌐 https://termux-assistant-ai.ru\n💻 https://github.com/CR4CODE\n"
                    "Форма заявки в боте станет доступна после настройки владельца.",
        })
        return True
    if command == "/order":
        api.send_action({
            "method": "sendMessage", "chat_id": chat_id,
            "text": "🛠 Приём заявок в боте пока требует настройки. Сейчас можно написать разработчику через контакты.\n"
                    "Для включения уведомлений владельцу нужно настроить его Telegram ID (команда /myid).",
            "reply_markup": {"inline_keyboard": [[{"text": "👤 Контакты", "callback_data": "ui:contacts"}]]},
        })
        return True
    return False


def dispatch_update(api: TelegramApi, controller: BotController,
                    marketing: MarketingAddon | None, update: dict,
                    portfolio: PortfolioAddon | None = None,
                    catalog: CatalogAddon | None = None,
                    crm: CRMAddon | None = None,
                    consultation: ConsultationAddon | None = None,
                    voice_router: VoiceTextRouter | None = None,
                    chat_monitor: ChatMonitor | None = None,
                    post_studio: PostStudioService | None = None,
                    notifications: NotificationHandler | None = None,
                     notifications_panel: OwnerNotificationPanel | None = None) -> None:
    """Route features while recording genuine private bot contacts first."""
    if notifications is not None:
        notifications.observe_update(update)
    if post_studio is not None:
        private_message = update.get("message")
        if isinstance(private_message, dict):
            actor = private_message.get("from") or {}
            chat = private_message.get("chat") or {}
            body = private_message.get("text")
            forwarded = any(field in private_message for field in (
                "forward_origin", "forward_from", "forward_date"))
            if (not forwarded and chat.get("type") == "private"
                    and type(chat.get("id")) is int and chat["id"] > 0
                    and chat["id"] == actor.get("id")
                    and isinstance(body, str) and bool(body.strip())
                    and body.split(maxsplit=1)[0].split("@", 1)[0] == "/forget_me"):
                post_studio.store.forget_user(chat["id"])
    if chat_monitor is not None and chat_monitor.handle_update(update):
        return
    if chat_monitor is not None:
        incoming = update.get("message")
        if isinstance(incoming, dict):
            sender = incoming.get("from") or {}
            chat = incoming.get("chat") or {}
            message_text = incoming.get("text")
            if (chat.get("type") == "private" and type(chat.get("id")) is int
                    and chat.get("id") == sender.get("id")
                    and isinstance(message_text, str) and bool(message_text.strip())
                    and message_text.split(maxsplit=1)[0].split("@", 1)[0] == "/forget_me"):
                chat_monitor.forget_owner(chat["id"])
    if voice_router is not None:
        message = update.get("message")
        scoped_command = (isinstance(message, dict)
                          and parse_dot(message.get("text")) is not None
                          and (message.get("chat") or {}).get("type") != "private")
        if ("business_connection" in update or "business_message" in update or scoped_command):
            if voice_router.handle_update(update):
                return
    # Preserve the previously deployed ZIP session routing.
    private_file = update.get("message")
    archives = getattr(controller, "archive_sessions", None)
    if archives is not None and isinstance(private_file, dict) and archives.collect(api, private_file):
        return
    if notifications is not None and notifications.handle_update(update):
        return
    if notifications_panel is not None and notifications_panel.handle_update(update):
        return
    if post_studio is not None:
        message = update.get("message")
        forwarded = isinstance(message, dict) and any(
            key in message for key in ("forward_origin", "forward_from", "forward_date"))
        if not forwarded and post_studio.handle_update(update):
            return
    private = update.get("message")
    if isinstance(private, dict) and parse_dot(private.get("text")) is not None:
        chat = private.get("chat") or {}
        sender = private.get("from") or {}
        forwarded = any(key in private for key in ("forward_origin", "forward_from", "forward_date"))
        if (chat.get("type") == "private" and chat.get("id") == sender.get("id")
                and not forwarded):
            controller.handle(private)
            return
    callback = update.get("callback_query")
    if (isinstance(callback, dict) and isinstance(callback.get("data"), str)
            and callback["data"].startswith("auto:")):
        query_id = callback.get("id")
        if not isinstance(query_id, str) or not query_id:
            return
        call_message = callback.get("message") or {}
        chat = call_message.get("chat") or {}
        sender = callback.get("from") or {}
        chat_id, message_id = chat.get("id"), call_message.get("message_id")
        page_name = decode_automation_callback(callback["data"])
        valid = (chat.get("type") == "private" and type(chat_id) is int and chat_id > 0
                 and type(sender.get("id")) is int and sender["id"] == chat_id
                 and type(message_id) is int and message_id > 0 and page_name is not None)
        example = (automation_example(page_name[6:])
                   if valid and page_name.startswith("photo:") else None)
        photo_data: bytes | None = None
        if example is not None:
            try:
                photo_data = (Path(__file__).resolve().parent / "assets" / example[0]).read_bytes()
            except OSError:
                LOG.warning("Automation example asset is unavailable")
        api.send_action({
            "method": "answerCallbackQuery", "callback_query_id": query_id,
            **({"text": "Кнопка недоступна. Открой автоматизацию в личке."}
               if not valid else
               {"text": "Пример пока недоступен."} if example and photo_data is None else {}),
        })
        if example is not None:
            if photo_data is not None:
                try:
                    api.send_photo_bytes(chat_id, example[0], photo_data, example[1])
                except TelegramApiError:
                    # Telegram might have accepted the photo despite a timeout.
                    # Never replay a potentially completed media operation.
                    LOG.warning("Automation example delivery status is uncertain; no retry")
            return
        if valid:
            page = (chat_monitor.page(chat_id) if page_name == "monitor"
                    and chat_monitor is not None else automation_page(page_name))
            if page is not None:
                text, markup = page
                try:
                    api.send_action({"method": "editMessageText", "chat_id": chat_id,
                                     "message_id": message_id, "text": text,
                                     "reply_markup": markup})
                except TelegramApiError:
                    # Stale or duplicate button: do not send another menu message.
                    pass
        return
    if (isinstance(callback, dict) and isinstance(callback.get("data"), str)
            and callback["data"].startswith("help:")):
        query_id = callback.get("id")
        if not isinstance(query_id, str) or not query_id:
            return
        call_message = callback.get("message") or {}
        chat = call_message.get("chat") or {}
        user = callback.get("from") or {}
        chat_id, message_id = chat.get("id"), call_message.get("message_id")
        topic = decode_help_callback(callback["data"])
        valid = (chat.get("type") == "private" and type(chat_id) is int and chat_id > 0
                 and type(user.get("id")) is int and user["id"] == chat_id
                 and type(message_id) is int and message_id > 0 and topic is not None)
        api.send_action({
            "method": "answerCallbackQuery", "callback_query_id": query_id,
            **({} if valid else {"text": "Кнопка недоступна. Открой .help в личке."}),
        })
        if valid:
            if topic in ("demo:type", "demo:love"):
                from kyntrion_tools.command_plugins.contracts import CommandContext
                demo_ctx = CommandContext(api, controller, chat_id, call_message)
                if topic == "demo:type":
                    from kyntrion_tools.command_plugins import type as typing_plugin
                    typing_plugin.schedule(demo_ctx, "Пример работы функции печати текста")
                else:
                    from kyntrion_tools.command_plugins import love as love_plugin
                    love_plugin.schedule(demo_ctx)
                return
            try:
                api.send_action({
                    "method": "editMessageText", "chat_id": chat_id,
                    "message_id": message_id,
                    "text": help_text("" if topic == "menu" else topic, api=api),
                    "reply_markup": help_keyboard("" if topic == "menu" else topic),
                })
            except TelegramApiError:
                # A duplicate callback, deleted menu or stale message is not
                # replayed; the callback acknowledgement already went out.
                pass
        return
    if (isinstance(callback, dict) and isinstance(callback.get("data"), str)
            and callback["data"].startswith(("cat:", "con:", "crm:"))
            and (callback.get("message") or {}).get("chat", {}).get("type") == "private"):
        sender_id = (callback.get("from") or {}).get("id")
        chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
        if (isinstance(sender_id, int) and sender_id == chat_id
                and (chat_id, sender_id) in controller.sessions
                and isinstance(callback.get("id"), str)):
            api.send_action({"method": "answerCallbackQuery", "callback_query_id": callback["id"],
                             "text": "Сначала заверши сбор через /done или /cancel."})
            return
    if isinstance(callback, dict) and isinstance(callback.get("data"), str) and callback["data"].startswith("ui:"):
        query_id = callback.get("id")
        if not isinstance(query_id, str) or not query_id:
            return
        chat = (callback.get("message") or {}).get("chat") or {}
        user = callback.get("from") or {}
        chat_id = chat.get("id")
        command = callback_command(callback["data"])
        valid = (chat.get("type") == "private" and isinstance(chat_id, int)
                 and chat_id == user.get("id") and command is not None)
        api.send_action({"method": "answerCallbackQuery", "callback_query_id": query_id,
                         **({} if valid else {"text": "Кнопка недоступна. Открой /start."})})
        if valid:
            dispatch_update(api, controller, marketing, {
                "message": {"chat": {"id": chat_id, "type": "private"},
                            "from": {"id": chat_id}, "text": command},
                "_menu_action": True,
            }, portfolio, catalog, crm, consultation, voice_router, chat_monitor,
                            post_studio, notifications, notifications_panel)
        return

    message = update.get("message")
    menu_action = bool(update.get("_menu_action"))
    if message:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        key = (chat.get("id"), user.get("id"))
        collecting = key in controller.sessions
        forwarded = any(k in message for k in ("forward_origin", "forward_from", "forward_date"))
        body = message.get("text")
        if not forwarded and isinstance(body, str):
            mapped = decode_reply_button(body)
            if mapped is not None:
                message = {**message, "text": mapped}
                menu_action = True
                body = mapped
        command = body.split(maxsplit=1)[0].split("@", 1)[0].lower() if isinstance(body, str) and body.startswith("/") else ""
        if (not forwarded and chat.get("type") == "private" and chat.get("id") == user.get("id")
                and isinstance(user.get("id"), int)):
            if portfolio is not None and command in ("/services", "/order", "/catalog_manage", "/crm", "/consult"):
                portfolio.drafts.pop(user["id"], None)
            if catalog is not None and command in ("/portfolio", "/portfolio_manage", "/crm", "/consult"):
                catalog.admin_drafts.pop(user["id"], None)
                catalog.order_drafts.pop(user["id"], None)
            if crm is not None and command in ("/catalog_manage", "/portfolio_manage", "/services", "/order", "/consult"):
                crm.editor_drafts.pop(user["id"], None)
            if consultation is not None and command == "/forget_me":
                consultation.forget_user(user["id"])
        if (marketing is not None and command == "/start" and not forwarded
                and chat.get("type") == "private" and chat.get("id") == user.get("id")
                and isinstance(body, str)):
            parts = body.split(maxsplit=1)
            if len(parts) == 2:
                marketing.record_referral(chat["id"], parts[1].strip())
        if (not forwarded and chat.get("type") == "private" and chat.get("id") == user.get("id")
                and (_send_menu(api, chat["id"], command, post_studio is not None,
                            notifications is not None, notifications_panel)
                     or (marketing is None and _public_services(api, chat["id"], command)))):
            return
    else:
        collecting = forwarded = False

    actual_update = {**update, "message": message} if message else update
    if crm is not None and (not collecting or menu_action) and not forwarded:
        handled, actions = crm.process(actual_update)
        if handled:
            for action in actions:
                if action.get("method") == "crmDeliverQuote":
                    state = crm.deliver_quote(action["owner_id"], action["order_id"],
                                              action["version"], api.send_action)
                    if state == "sent":
                        notice = "✅ Предложение отправлено клиенту и записано в CRM."
                    elif state == "uncertain":
                        notice = ("⚠️ Результат доставки неизвестен. Автоповтора нет: "
                                  "проверь чат клиента вручную. /crm")
                    else:
                        notice = "Это действие уже выполнено или устарело. /crm"
                    api.send_action({"method": "sendMessage", "chat_id": crm.owner, "text": notice})
                else:
                    api.send_action(action)
            return

    if catalog is not None and (not collecting or menu_action) and not forwarded:
        handled, actions = catalog.process(actual_update)
        if handled:
            for action in actions:
                api.send_action(action)
            return

    if portfolio is not None and (not collecting or menu_action) and not forwarded:
        handled, actions = portfolio.process(actual_update)
        if handled:
            for action in actions:
                api.send_action(action)
            return

    if consultation is not None and (not collecting or menu_action) and not forwarded:
        handled, actions = consultation.process(actual_update)
        if handled:
            for action in actions:
                api.send_action(action)
            return

    if marketing is not None and (not collecting or menu_action) and not forwarded:
        handled, actions = marketing.process(actual_update)
        if handled:
            for action in actions:
                api.send_action(action)
            return

    if message:
        controller.handle(message)
        body = message.get("text")
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if (marketing is not None and not collecting and chat.get("type") == "private"
                and isinstance(user.get("id"), int) and
                (forwarded or (isinstance(body, str) and not body.startswith("/"))
                 or isinstance(message.get("voice"), dict))):
            footer = marketing.maybe_footer(user["id"])
            if footer:
                api.send_message(chat["id"], footer)


def build_marketing() -> MarketingAddon | None:
    owner_id = os.environ.get("KYN_OWNER_CHAT_ID", "").strip()
    if not owner_id:
        LOG.info("Маркетинговый модуль выключен: KYN_OWNER_CHAT_ID не задан")
        return None
    return MarketingAddon(MarketingConfig(
        db_path=Path(os.environ.get("KYN_MARKETING_DB", "/var/lib/kyntrion-text-bot/marketing.sqlite3")),
        admin_chat_id=int(owner_id),
        owner_username=os.environ.get("KYN_OWNER_USERNAME", ""),
    ))


def build_post_studio(api: TelegramApi, profile: dict) -> PostStudioService | None:
    """Available to every bot user only when explicitly enabled by operations."""
    if os.environ.get("KYN_POST_STUDIO_ENABLED", "").strip().lower() != "true":
        return None
    bot_id = profile.get("id") if isinstance(profile, dict) else None
    if type(bot_id) is not int or bot_id <= 0:
        raise ValueError("Post Studio needs a verified getMe bot ID")
    db_path = os.environ.get(
        "KYN_POST_STUDIO_DB", "/var/lib/kyntrion-text-bot/post-studio.sqlite3")
    store = PostStudioStore(db_path)
    try:
        return PostStudioService(api, store, bot_id)
    except Exception:
        store.close()
        raise


def build_notifications(api: TelegramApi) -> NotificationHandler | None:
    """Off by default; record genuine private messages but never scrape group members."""
    if os.environ.get("KYN_NOTIFICATIONS_ENABLED", "").strip().lower() != "true":
        return None
    db_path = os.environ.get(
        "KYN_NOTIFICATIONS_DB", "/var/lib/kyntrion-text-bot/notifications.sqlite3")
    store = NotificationStore(db_path)
    return NotificationHandler(api, store)


def build_notifications_panel(
    api: TelegramApi, notifications: NotificationHandler | None
) -> OwnerNotificationPanel | None:
    """Owner ID comes only from protected runtime configuration, not Telegram callbacks."""
    if (notifications is None or
            os.environ.get("KYN_NOTIFICATIONS_PANEL_ENABLED", "").strip().lower() != "true"):
        return None
    owner = os.environ.get("KYN_OWNER_CHAT_ID", "").strip()
    if not owner.isascii() or not owner.isdecimal() or int(owner) <= 0:
        LOG.warning("Центр уведомлений: нужен положительный KYN_OWNER_CHAT_ID")
        return None
    return OwnerNotificationPanel(
        api, notifications.store, int(owner),
        delivery_enabled=(os.environ.get("KYN_NOTIFICATIONS_DELIVERY_ENABLED", "")
                          .strip().lower() == "true"))


def run(api: TelegramApi, marketing: MarketingAddon | None = None) -> None:
    profile = api.get_me()
    LOG.info("Запущен бот @%s", profile.get("username", "unknown"))
    try:
        api.set_commands(include_marketing=marketing is not None)
    except TelegramApiError as exc:
        LOG.warning("Не удалось настроить меню команд: %s", exc)

    controller = BotController(api)
    journal = None
    voice_router = None
    monitor_store = None
    chat_monitor = None
    post_studio = None
    notifications = None
    notifications_panel = None
    try:
        notifications = build_notifications(api)
        notifications_panel = build_notifications_panel(api, notifications)
        post_studio = build_post_studio(api, profile)
        if os.environ.get("KYN_CHAT_MONITOR_ENABLED", "").strip().lower() == "true":
            monitor_db = os.environ.get(
                "KYN_CHAT_MONITOR_DB", "/var/lib/kyntrion-text-bot/chat-monitor.sqlite3")
            media_root = os.environ.get(
                "KYN_CHAT_MONITOR_MEDIA", "/var/lib/kyntrion-text-bot/chat-monitor-media")
            media_target = Path(media_root)
            if (not media_root.strip() or media_root.strip().casefold().startswith("file:")
                    or not media_target.is_absolute() or media_target.is_symlink()
                    or media_target.resolve() in (Path("/"), Path.home().resolve())):
                raise ValueError("KYN_CHAT_MONITOR_MEDIA must be a persistent private directory")
            monitor_store = MonitorStore(monitor_db, media_root=media_root)
            chat_monitor = ChatMonitor(api, monitor_store, media_root)
        if os.environ.get("KYN_VOICE_COMMANDS_ENABLED", "").strip().lower() == "true":
            owner = os.environ.get("KYN_OWNER_CHAT_ID", "").strip()
            if owner.isascii() and owner.isdecimal() and int(owner) > 0:
                db_path = os.environ.get(
                    "KYN_VOICE_COMMANDS_DB", "/var/lib/kyntrion-text-bot/voice-commands.sqlite3")
                if (not db_path.strip() or db_path.strip().casefold() == ":memory:"
                        or db_path.strip().casefold().startswith("file:")):
                    raise ValueError("KYN_VOICE_COMMANDS_DB must be a persistent filesystem path, not a SQLite memory/URI path")
                journal = VoiceCommandJournal(db_path)
                voice_router = VoiceTextRouter(api, controller, int(owner), journal)
            else:
                LOG.warning("Голосовые команды выключены: требуется положительный KYN_OWNER_CHAT_ID")
        portfolio = PortfolioAddon(marketing) if marketing is not None else None
        catalog = CatalogAddon(marketing) if marketing is not None else None
        crm = CRMAddon(marketing) if marketing is not None else None
        ai_client = None
        if marketing is not None and os.environ.get("KYN_AI_URL", "").strip():
            try:
                ai_client = AIClient(os.environ["KYN_AI_URL"].strip(), os.environ["TELEGRAM_BOT_TOKEN"])
            except (ValueError, KeyError):
                LOG.warning("AI консультации работают в ручном режиме: конфигурация неверна")
        consultation = ConsultationAddon(marketing, ai_client) if marketing is not None else None
        _poll(api, controller, marketing, portfolio, catalog, crm, consultation,
              voice_router, chat_monitor, post_studio, notifications,
              notifications_panel)
    finally:
        if journal is not None:
            journal.close()
        if monitor_store is not None:
            monitor_store.close()
        if post_studio is not None:
            post_studio.store.close()
        if notifications is not None:
            notifications.store.close()


def _poll(api, controller, marketing, portfolio, catalog, crm, consultation, voice_router,
          chat_monitor=None, post_studio=None, notifications=None,
          notifications_panel=None):
    offset = None
    errors = 0
    next_schedule_check = 0.0
    while True:
        try:
            poll_args = {}
            if notifications_panel is not None and notifications_panel.wants_short_poll():
                poll_args["poll_timeout"] = 1
            if chat_monitor is not None:
                updates = api.get_updates(
                    offset, include_voice_commands=voice_router is not None,
                    include_chat_monitor=True, **poll_args)
            else:
                updates = (api.get_updates(offset, include_voice_commands=True,
                                            **poll_args)
                           if voice_router is not None else api.get_updates(offset, **poll_args))
            for update in updates:
                dispatch_update(api, controller, marketing, update, portfolio, catalog, crm,
                                consultation, voice_router, chat_monitor, post_studio,
                                notifications, notifications_panel)
                offset = update["update_id"] + 1
            if post_studio is not None and time.monotonic() >= next_schedule_check:
                # A claimed/uncertain job is never replayed after a timeout or restart.
                results = run_due(post_studio, post_studio.store,
                                  time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
                for job_id, state in results:
                    LOG.info("Post Studio scheduled job %s -> %s", job_id, state)
                next_schedule_check = time.monotonic() + 5
            if notifications_panel is not None:
                notifications_panel.tick()
            if voice_router is not None:
                voice_router.flush_completed()
            if chat_monitor is not None:
                chat_monitor.store.expire()
            errors = 0
        except TelegramApiError as exc:
            errors += 1
            LOG.warning("Запрос не выполнен: %s", exc)
            time.sleep(min(2 ** min(errors, 5), 30))
        except (KeyError, TypeError, ValueError) as exc:
            # Unexpected input should not stop the service or expose message bodies.
            errors += 1
            LOG.warning("Некорректные данные Telegram: %s", type(exc).__name__)
            time.sleep(min(2 ** min(errors, 5), 30))


def main() -> int:
    parser = argparse.ArgumentParser(description="KYNTRION Telegram message-to-TXT bot")
    parser.add_argument("--check", action="store_true", help="Проверить токен и соединение без запуска")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Не задан TELEGRAM_BOT_TOKEN. Смотри README.md.", file=sys.stderr)
        return 2
    api = TelegramApi(token, gateway_url=os.environ.get("TELEGRAM_GATEWAY_URL"),
                      voice_asr_url=os.environ.get("VOICE_ASR_URL"))
    try:
        if args.check:
            profile = api.get_me()
            print(f"Соединение установлено: @{profile.get('username', 'unknown')}")
        else:
            marketing = build_marketing()
            try:
                run(api, marketing)
            finally:
                if marketing is not None:
                    marketing.close()
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except TelegramApiError as exc:
        LOG.error("Ошибка Telegram: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
