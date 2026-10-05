# handlers/admin.py — админ-панель владельца (stateful UX)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
import re
from datetime import datetime, timedelta

import db
import ad
import background
import keyboards as kb
from utils import send, mention, utcnow, is_owner

log = logging.getLogger(__name__)




# ---------- state helpers ----------

def _state_key(user_id: int) -> str:
    return f"admin_state_{user_id}"


def get_state(user_id: int) -> str:
    return db.get_config(_state_key(user_id), "")


def set_state(user_id: int, v: str) -> None:
    db.set_config(_state_key(user_id), v)


def clear_state(user_id: int) -> None:
    db.set_config(_state_key(user_id), "")


# Маппинг stateful-кнопок: кнопка -> (state, config_key, default, шаблон_подсказки)
# config_key=None означает, что подстановка {current} не делается.
STATEFUL_PROMPTS = {
    "✏️ Шаблон поста": (
        "wallpost_template", "wall_post_template",
        "(по умолчанию — авто-статистика)",
        "✏️ Отправь текст шаблона. Можно {stats} для вставки статистики.\n\n"
        "Текущий:\n{current}"
    ),
    "⏱ Интервал поста": (
        "wallpost_interval", "wall_post_interval", "360",
        "⏱ Отправь интервал в минутах (мин. 15).\nСейчас: {current} мин."
    ),
    "✏️ Текст промо": (
        "promo_text", "promo_text", "(пусто)",
        "✏️ Отправь НОВЫЙ ТЕКСТ промо следующим сообщением.\n\n"
        "Текущий:\n{current}\n\n"
        "Плейсхолдер {bot} заменится на ссылку бота.\n\n"
        "❌ Отмена — кнопка ниже или /cancel."
    ),
    "⏱ Интервал": (
        "promo_interval", "promo_interval", "60",
        "⏱ Отправь новое значение в минутах (минимум 5).\n"
        "Текущий: {current} мин.\n\n"
        "❌ Отмена — кнопка ниже или /cancel."
    ),
    "📢 Канал бота": (
        "channel_url", "bot_channel_url", "(не задан)",
        "📢 Отправь ссылку на канал бота.\n"
        "Текущий: {current}\n\n"
        "❌ Отмена — кнопка ниже или /cancel."
    ),
    "💬 Главная беседа": (
        "mainchat_url", "main_chat_url", "(не задана)",
        "💬 Отправь ссылку на главную беседу.\n"
        "Текущая: {current}\n\n"
        "❌ Отмена — кнопка ниже или /cancel."
    ),
    "🎁 Начислить PP": (
        "give_input", None, None,
        "🎁 Отправь следующим сообщением:\n"
        "USER_ID СУММА [комментарий]\n\n"
        "Пример: 156002808 5000 тест\n\n"
        "❌ Отмена — кнопка ниже или /cancel."
    ),
}



# ============================================================
# ПАНЕЛИ
# ============================================================

def show_admin_menu(vk, event) -> bool:
    if not is_owner(event.user_id):
        return True
    send(vk, event.peer_id,
         "🔧 АДМИН-ПАНЕЛЬ\n"
         "━━━━━━━━━━━━━━━━\n"
         "Выбирай кнопки ниже 👇\n\n"
         "Команды (для быстрого ввода):\n"
         "• /give ID СУММА — начислить PP\n"
         "• /setowner ID — сменить владельца\n"
         "• /stats — статистика",
         keyboard=kb.admin_menu())
    return True


def show_promo(vk, event) -> bool:
    if not is_owner(event.user_id):
        return True
    enabled = db.get_config("promo_enabled", "0") == "1"
    interval = db.get_config("promo_interval", "60")
    text = db.get_config("promo_text", "") or "(не задан)"
    last = db.get_config("last_promo_sent", "") or "никогда"

    send(vk, event.peer_id,
         f"🚀 АВТОПРОМО\n"
         f"━━━━━━━━━━━━━━━━\n"
         f"Статус: {'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}\n"
         f"Интервал: {interval} мин\n"
         f"Последняя отправка: {last}\n\n"
         f"Текст промо:\n{text}",
         keyboard=kb.promo_menu(enabled))
    return True


def show_settings(vk, event) -> bool:
    if not is_owner(event.user_id):
        return True
    send(vk, event.peer_id,
         "⚙️ НАСТРОЙКИ БОТА\n"
         "━━━━━━━━━━━━━━━━\n"
         f"📢 Канал: {db.get_config('bot_channel_url') or '(не задан)'}\n"
         f"💬 Главная беседа: {db.get_config('main_chat_url') or '(не задана)'}\n"
         f"🤖 Имя бота: {db.get_config('bot_name', 'PiarBot')}",
         keyboard=kb.settings_menu())
    return True


def show_stats(vk, event) -> bool:
    if not is_owner(event.user_id):
        return True

    total_users = db.query_one("SELECT COUNT(*) AS c FROM users")["c"]
    day_ago = (utcnow() - timedelta(hours=24)).isoformat(sep=" ")
    active_users = db.query_one(
        "SELECT COUNT(*) AS c FROM users WHERE last_seen >= ?", (day_ago,)
    )["c"]
    total_chats = db.query_one(
        "SELECT COUNT(*) AS c FROM chats WHERE is_active=1"
    )["c"]
    total_pp = db.query_one(
        "SELECT COALESCE(SUM(balance),0) AS s FROM users"
    )["s"]
    total_rounds = db.query_one(
        "SELECT COALESCE(SUM(rounds),0) AS s FROM users"
    )["s"]
    total_refs = db.query_one("SELECT COUNT(*) AS c FROM referrals")["c"]
    paid = db.query_one(
        "SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE status='done'"
    )["s"]

    top = db.query(
        "SELECT user_id, first_name, balance FROM users "
        "ORDER BY balance DESC LIMIT 5"
    )
    top_lines = []
    for i, r in enumerate(top, 1):
        name = r["first_name"] or f"id{r['user_id']}"
        top_lines.append(f"  {i}. {name} — {r['balance']} PP")
    top_block = "🏆 Топ-5:\n" + "\n".join(top_lines) if top_lines else "🏆 Топ пуст."

    send(vk, event.peer_id,
         f"📊 СТАТИСТИКА\n"
         f"━━━━━━━━━━━━━━━━\n"
         f"👥 Юзеров: {total_users}\n"
         f"🟢 Активных за сутки: {active_users}\n"
         f"💬 Бесед: {total_chats}\n"
         f"👥 Рефералов: {total_refs}\n"
         f"🎯 Раундов: {total_rounds}\n"
         f"💰 PP в обороте: {total_pp}\n"
         f"💳 Продано PP: {paid} (демо)\n\n"
         f"{top_block}",
         keyboard=kb.admin_back())
    return True


# ============================================================
# СТАТЕФУЛ-ОБРАБОТКА (пользователь ввёл данные в ответ на запрос)
# ============================================================

def _process_state(vk, event, state: str, text: str, user_info: dict) -> bool:
    user_id = event.user_id
    peer_id = event.peer_id

    if state == "promo_text":
        if not text:
            send(vk, peer_id, "❌ Пустой текст. Попробуй ещё или /cancel.")
            return True
        db.set_config("promo_text", text)
        clear_state(user_id)
        enabled = db.get_config("promo_enabled", "0") == "1"
        send(vk, peer_id, f"✅ Текст промо сохранён:\n{text}",
             keyboard=kb.promo_menu(enabled))
        return True

    if state == "promo_interval":
        try:
            n = int(text.split()[0])
        except (IndexError, ValueError):
            send(vk, peer_id, "❌ Нужно целое число минут. Попробуй ещё или /cancel.")
            return True
        if n < 5:
            send(vk, peer_id, "❌ Минимум 5 минут. Попробуй ещё или /cancel.")
            return True
        db.set_config("promo_interval", str(n))
        clear_state(user_id)
        enabled = db.get_config("promo_enabled", "0") == "1"
        send(vk, peer_id, f"✅ Интервал: {n} мин.",
             keyboard=kb.promo_menu(enabled))
        return True

    if state == "channel_url":
        if not text.startswith(("http://", "https://")):
            send(vk, peer_id, "❌ Нужна ссылка (https://...). Или /cancel.")
            return True
        db.set_config("bot_channel_url", text)
        clear_state(user_id)
        send(vk, peer_id, f"✅ Канал бота: {text}",
             keyboard=kb.settings_menu())
        return True

    if state == "mainchat_url":
        if not text.startswith(("http://", "https://")):
            send(vk, peer_id, "❌ Нужна ссылка (https://...). Или /cancel.")
            return True
        db.set_config("main_chat_url", text)
        clear_state(user_id)
        send(vk, peer_id, f"✅ Главная беседа: {text}",
             keyboard=kb.settings_menu())
        return True

    if state == "give_input":
        clear_state(user_id)
        return cmd_give(vk, event, text)

    if state == "news_text":
        clear_state(user_id)
        return cmd_news(vk, event, text)

    if state == "ad_target_add":
        clear_state(user_id)
        r = ad.resolve_target(vk, text)
        if not r.get("ok"):
            send(vk, peer_id, f"❌ {r.get('error')}", keyboard=kb.ad_targets_kb())
            return True
        rr = ad.add_target(r["owner_id"], r["screen_name"], r["title"])
        if rr.get("ok"):
            send(vk, peer_id,
                 f"✅ Цель добавлена:\n{r['title']}\n"
                 f"owner_id: {r['owner_id']}\nID: {rr['target_id']}",
                 keyboard=kb.ad_targets_kb())
        else:
            send(vk, peer_id, f"❌ {rr.get('error')}", keyboard=kb.ad_targets_kb())
        return True

    if state == "ad_target_del":
        clear_state(user_id)
        if not text.strip().isdigit():
            send(vk, peer_id, "❌ Нужен числовой ID.", keyboard=kb.ad_targets_kb())
            return True
        ok = ad.remove_target(int(text.strip()))
        send(vk, peer_id,
             "✅ Удалена." if ok else "❌ Не найдена.",
             keyboard=kb.ad_targets_kb())
        return True

    if state == "ad_bot_add":
        clear_state(user_id)
        parts = text.strip().split()
        token = parts[0]
        res_id = None
        if len(parts) > 1 and parts[1].isdigit():
            res_id = int(parts[1])
        r = ad.add_bot(token, resource_id=res_id)
        if r.get("ok"):
            send(vk, peer_id,
                 f"✅ Бот добавлен.\nID: {r['bot_id']}\n"
                 f"User: {r['user_id']}\nИмя: {r['name']}",
                 keyboard=kb.ad_bots_kb())
        else:
            send(vk, peer_id, f"❌ {r.get('error')}", keyboard=kb.ad_bots_kb())
        return True

    if state == "ad_bot_del":
        clear_state(user_id)
        if not text.strip().isdigit():
            send(vk, peer_id, "❌ Нужен числовой ID.", keyboard=kb.ad_bots_kb())
            return True
        ok = ad.remove_bot(int(text.strip()))
        send(vk, peer_id,
             "✅ Удалён." if ok else "❌ Не найден.",
             keyboard=kb.ad_bots_kb())
        return True

    if state == "ad_message":
        db.set_config("ad_message", text)
        clear_state(user_id)
        send(vk, peer_id, f"✏️ Текст рекламы сохранён.\n\n{text}",
             keyboard=kb.admin_menu())
        return True

    if state == "broadcast_text":
        clear_state(user_id)
        return cmd_broadcast(vk, event, text)

    if state == "wallpost_template":
        if not text:
            send(vk, peer_id, "❌ Пустой текст. /cancel.")
            return True
        db.set_config("wall_post_template", text)
        clear_state(user_id)
        send(vk, peer_id, f"✏️ Шаблон сохранён:\n{text}",
             keyboard=kb.admin_menu())
        return True

    if state == "wallpost_interval":
        try:
            n = int(text.split()[0])
        except (IndexError, ValueError):
            send(vk, peer_id, "❌ Число минут. /cancel.")
            return True
        if n < 15:
            send(vk, peer_id, "❌ Минимум 15. /cancel.")
            return True
        db.set_config("wall_post_interval", str(n))
        clear_state(user_id)
        send(vk, peer_id, f"⏱ Интервал: {n} мин.", keyboard=kb.admin_menu())
        return True

    clear_state(user_id)
    return True


# ============================================================
# КОМАНДЫ (вводятся текстом)
# ============================================================

def show_wallpost(vk, event) -> bool:
    if not is_owner(event.user_id):
        return True
    enabled = db.get_config("wall_post_enabled", "0") == "1"
    interval = db.get_config("wall_post_interval", "360")
    template = db.get_config("wall_post_template", "") or "(шаблон по умолчанию: авто-статистика)"
    last = db.get_config("last_wall_post", "") or "никогда"

    send(vk, event.peer_id,
         f"📝 АВТОПОСТ В СООБЩЕСТВО\n"
         f"━━━━━━━━━━━━━━━━\n"
         f"Статус: {'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}\n"
         f"Интервал: {interval} мин\n"
         f"Последний пост: {last}\n\n"
         f"Шаблон:\n{template}\n\n"
         f"Команды:\n"
         f"• /wallpost on | off\n"
         f"• /wallpost interval N (мин, мин. 15)\n"
         f"• /wallpost template ТЕКСТ (вставь {{stats}} — авто-статистика)\n"
         f"• /wallpost now — опубликовать сейчас",
         keyboard=kb.wallpost_menu(enabled))
    return True


def cmd_setinvite(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True
    args = args.strip()
    if not args:
        cur = db.get_config("main_chat_invite", "") or "(не задано)"
        send(vk, event.peer_id,
             f"📌 /setinvite URL — ссылка-приглашение в главную беседу\n"
             f"Сейчас: {cur}")
        return True
    if not args.startswith(("http://", "https://")):
        send(vk, event.peer_id, "❌ Нужна ссылка https://vk.me/join/...")
        return True
    db.set_config("main_chat_invite", args)
    send(vk, event.peer_id, f"✅ Приглашение сохранено:\n{args}")
    return True


def cmd_broadcast(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True
    if not args.strip():
        send(vk, event.peer_id,
             "📣 /broadcast ТЕКСТ — рассылка во все беседы\n"
             "Например: /broadcast 🔥 Новый раунд стартовал, пишите ссылки!")
        return True
    text = args.strip()
    send(vk, event.peer_id, "🚀 Отправляю во все беседы...")

    ok, fail = background.send_broadcast(vk, text)
    send(vk, event.peer_id,
         f"✅ Рассылка завершена.\n"
         f"• Бесед: {ok}\n"
         f"• Ошибок: {fail}")
    return True


def cmd_export(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True

    args = (args or "").strip().lower()
    if args not in ("", "users", "chats", "rewards"):
        send(vk, event.peer_id,
             "📌 /export [users|chats|rewards]\n"
             "По умолчанию: users")
        return True

    what = args or "users"

    from pathlib import Path as _P
    from datetime import datetime as _dt
    import csv

    export_dir = _P(__file__).resolve().parent.parent / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.utcnow().strftime("%Y%m%d-%H%M")

    if what == "users":
        rows = db.query(
            "SELECT user_id, first_name, last_name, balance, rounds, "
            "referred_by, created_at, last_seen, is_banned "
            "FROM users ORDER BY balance DESC")
        fname = f"users-{stamp}.csv"
        path = export_dir / fname
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["user_id", "first_name", "last_name", "balance",
                        "rounds", "referred_by", "created_at", "last_seen",
                        "is_banned"])
            for r in rows:
                w.writerow(list(r))

    elif what == "chats":
        rows = db.query(
            "SELECT peer_id, title, admin_id, list_size, pending, "
            "registered_at, is_active FROM chats ORDER BY registered_at DESC")
        fname = f"chats-{stamp}.csv"
        path = export_dir / fname
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["peer_id", "title", "admin_id", "list_size",
                        "pending", "registered_at", "is_active"])
            for r in rows:
                w.writerow(list(r))

    else:  # rewards
        rows = db.query(
            "SELECT id, user_id, amount, reason, peer_id, round_number, "
            "created_at FROM rewards ORDER BY id DESC LIMIT 10000")
        fname = f"rewards-{stamp}.csv"
        path = export_dir / fname
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "user_id", "amount", "reason", "peer_id",
                        "round_number", "created_at"])
            for r in rows:
                w.writerow(list(r))

    size = path.stat().st_size
    send(vk, event.peer_id,
         f"📎 Экспорт готов: {fname}\n"
         f"Строк: {len(rows)}, размер: {size} байт.\n"
         f"Отправляю файл...")

    from utils import send_doc
    ok = send_doc(vk, event.peer_id, path, title=fname)
    if not ok:
        send(vk, event.peer_id, "❌ Не удалось отправить файл. Смотри лог.")
    return True


def show_ad(vk, event) -> bool:
    if not is_owner(event.user_id):
        return True
    bots = ad.list_bots()
    targets = ad.list_targets()

    active_bots = sum(1 for b in bots if b["status"] == "active")
    active_targets = sum(1 for t in targets if t["status"] == "active")
    enabled = db.get_config("ad_enabled", "0") == "1"

    lines = [
        "🎯 РЕКЛАМА (AD BOTS)",
        "━━━━━━━━━━━━━━━━",
        f"Статус: {'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}",
        f"🤖 Ботов: {len(bots)} (активных {active_bots})",
        f"🎯 Целей: {len(targets)} (активных {active_targets})",
        "",
        "Команды:",
        "• /ad on | off",
        "• /ad bot add TOKEN [resource_id]",
        "• /ad bot del ID",
        "• /ad bot list",
        "• /ad bot check — проверить всех",
        "• /ad bot res ID RESOURCE_ID",
        "• /ad target add OWNER_ID [screen_name]",
        "• /ad target del ID",
        "• /ad target list",
        "• /ad msg TEXT ({bot}, {invite})",
        "• /ad test — один пост прямо сейчас",
    ]
    send(vk, event.peer_id, "\n".join(lines), keyboard=kb.ad_menu(enabled))
    return True


def show_ad_targets(vk, event) -> bool:
    if not is_owner(event.user_id):
        return True
    targets = ad.list_targets()
    if not targets:
        send(vk, event.peer_id,
             "🎯 Целей нет.\n\n"
             "Добавь паблик-конкурент:\n"
             "Кнопка «➕ Добавить цель» или /ad target add OWNER_ID [screen]",
             keyboard=kb.ad_targets_kb())
        return True

    lines = [f"🎯 Целей: {len(targets)}", "━━━━━━━━━━━━━━━━"]
    for t in targets:
        st = {"active": "🟢", "banned": "🔴"}.get(t["status"], "⚪")
        name = t["title"] or t["screen_name"] or f"id{t['owner_id']}"
        lines.append(
            f"{st} #{t['id']} {name} ({t['method']})\n"
            f"   owner={t['owner_id']} fails={t['fails']}")
    send(vk, event.peer_id, "\n".join(lines), keyboard=kb.ad_targets_kb())
    return True


def cmd_ad_target(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True

    if not args.strip():
        return show_ad_targets(vk, event)

    # добавить по ссылке
    from utils import extract_vk_resource
    if extract_vk_resource(args):
        r = ad.resolve_target(vk, args)
        if not r.get("ok"):
            send(vk, event.peer_id, f"❌ {r.get('error')}")
            return True
        rr = ad.add_target(r["owner_id"], r["screen_name"], r["title"])
        if rr.get("ok"):
            send(vk, event.peer_id,
                 f"✅ Цель добавлена:\n{r['title']}\n"
                 f"owner_id: {r['owner_id']}\nID: {rr['target_id']}",
                 keyboard=kb.ad_targets_kb())
        else:
            send(vk, event.peer_id, f"❌ {rr.get('error')}")
        return True

    # del N
    parts = args.split()
    if parts[0].lower() == "del" and len(parts) > 1 and parts[1].isdigit():
        ok = ad.remove_target(int(parts[1]))
        send(vk, event.peer_id,
             "✅ Удалена." if ok else "❌ Не найдена.",
             keyboard=kb.ad_targets_kb())
        return True

    send(vk, event.peer_id,
         "📌 Пришли ссылку на паблик или /ad target del ID")
    return True


def show_ad_imitation(vk, event) -> bool:
    if not is_owner(event.user_id):
        return True
    enabled = db.get_config("imitation_enabled", "0") == "1"
    sub_int = db.get_config("imitation_subscribe_interval", "180")

    stats = ad.imitation_stats()
    bots = ad.list_bots()
    bots_active = sum(1 for b in bots if b["status"] == "active")

    lines = [
        "🤖 ИМИТАЦИЯ АКТИВНОСТИ",
        "━━━━━━━━━━━━━━━━",
        f"Статус: {'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}",
        f"Интервал подписок: {sub_int} мин (мин. значение)",
        f"Ботов активных: {bots_active}",
        "",
        "📊 Метрики:",
        f"• Подписок сделано: {stats['subs']}",
        f"• Постов сделано: {stats['posts']}",
        f"• Провалов: {stats['fails']}",
        "",
        "Что делает имитация:",
        "• Боты подписываются на цели-конкуренты",
        "• Боты постят рекламу в паблики-цели",
        "• Все действия с рандомными задержками",
    ]
    send(vk, event.peer_id, "\n".join(lines),
         keyboard=kb.ad_imitation_kb(enabled))
    return True


def show_ad_bots_subscriptions(vk, event) -> bool:
    """Список ботов с их флагами имитации."""
    if not is_owner(event.user_id):
        return True
    bots = ad.list_bots()
    if not bots:
        send(vk, event.peer_id, "🤖 Ботов нет. Добавь: /ad bot add TOKEN",
             keyboard=kb.ad_imitation_kb(db.get_config('imitation_enabled','0')=='1'))
        return True

    lines = ["👥 Боты и имитация:", "━━━━━━━━━━━━━━━━"]
    for b in bots:
        row = ad.get_bot(b["id"])
        sub = "✅" if row["can_subscribe"] else "❌"
        rd = "✅" if row["can_join_round"] else "❌"
        st = {"active": "🟢", "dead": "🔴"}.get(b["status"], "⚪")
        lines.append(
            f"{st} #{b['id']} {b['name']}\n"
            f"   подписки {sub} | раунды {rd}")
    lines.append("")
    lines.append("Переключить: /ad bot flags ID subscribe 0/1")
    lines.append("Или /ad bot flags ID round 0/1")
    send(vk, event.peer_id, "\n".join(lines),
         keyboard=kb.ad_imitation_kb(db.get_config('imitation_enabled','0')=='1'))
    return True


def cmd_ad_imitation(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True
    low = (args or "").strip().lower()

    if low in ("on", "вкл"):
        db.set_config("imitation_enabled", "1")
        send(vk, event.peer_id, "🟢 Имитация включена.",
             keyboard=kb.ad_imitation_kb(True))
        return True
    if low in ("off", "выкл"):
        db.set_config("imitation_enabled", "0")
        send(vk, event.peer_id, "🔴 Имитация выключена.",
             keyboard=kb.ad_imitation_kb(False))
        return True
    if low.startswith("interval"):
        parts = low.split()
        if len(parts) < 2 or not parts[1].isdigit():
            send(vk, event.peer_id, "📌 /ad imitation interval N (мин)")
            return True
        n = int(parts[1])
        if n < 15:
            send(vk, event.peer_id, "❌ Минимум 15 минут.")
            return True
        db.set_config("imitation_subscribe_interval", str(n))
        send(vk, event.peer_id, f"⏱ Интервал подписок: {n} мин.")
        return True
    return show_ad_imitation(vk, event)


def cmd_ad_bot_flags(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True
    parts = args.split()
    if len(parts) < 3 or not parts[0].isdigit():
        send(vk, event.peer_id,
             "📌 /ad bot flags ID subscribe 0/1\n"
             "📌 /ad bot flags ID round 0/1")
        return True
    bid = int(parts[0])
    flag = parts[1].lower()
    val = 1 if parts[2] == "1" else 0

    FLAGS = {
        "subscribe": "can_subscribe",
        "round": "can_join_round",
        "join_round": "can_join_round",
    }
    col = FLAGS.get(flag)
    if not col:
        send(vk, event.peer_id, "❌ Флаг: subscribe | round")
        return True

    if not ad.get_bot(bid):
        send(vk, event.peer_id, f"❌ Бот {bid} не найден.")
        return True
    db.execute(f"UPDATE ad_bots SET {col}=? WHERE id=?", (val, bid))
    send(vk, event.peer_id,
         f"✅ Бот #{bid}: {flag} = {'ON' if val else 'OFF'}")
    return True


def cmd_ad(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True

    parts = args.strip().split()
    if not parts:
        return show_ad(vk, event)

    action = parts[0].lower()

    # /ad on | off
    if action in ("on", "вкл"):
        db.set_config("ad_enabled", "1")
        send(vk, event.peer_id, "🟢 Реклама включена.", keyboard=kb.ad_menu(True))
        return True
    if action in ("off", "выкл"):
        db.set_config("ad_enabled", "0")
        send(vk, event.peer_id, "🔴 Реклама выключена.", keyboard=kb.ad_menu(False))
        return True

    # /ad test
    if action == "test":
        res = ad.do_one_post()
        if res.get("ok"):
            send(vk, event.peer_id,
                 f"✅ Пост опубликован\nбот #{res['bot']} → цель #{res['target']}")
        else:
            send(vk, event.peer_id, f"❌ Не удалось: {res.get('reason') or res.get('error')}")
        return True

    # /ad msg TEXT
    if action == "msg":
        text = args[5:].strip()
        if not text:
            cur = db.get_config("ad_message", "") or "(по умолчанию)"
            send(vk, event.peer_id,
                 f"📝 Текущий текст:\n{cur}\n\n"
                 f"Изменить: /ad msg ТЕКСТ\n"
                 f"Плейсхолдеры: {{bot}}, {{invite}}")
            return True
        db.set_config("ad_message", text)
        send(vk, event.peer_id, f"✏️ Сохранено:\n{text}")
        return True

    # /ad target ...
    if action == "target":
        sub = args[7:].strip()
        return cmd_ad_target(vk, event, sub)

    # /ad imitation ...
    if action == "imitation":
        sub = args[11:].strip()
        return cmd_ad_imitation(vk, event, sub)

    # /ad bot ...
    if action == "bot":
        if len(parts) < 2:
            send(vk, event.peer_id, "📌 /ad bot add TOKEN | del ID | list | check | res ID RES")
            return True
        sub = parts[1].lower()

        if sub == "list":
            bots = ad.list_bots()
            if not bots:
                send(vk, event.peer_id, "🤖 Ботов нет.")
                return True
            lines = ["🤖 Боты:"]
            for b in bots:
                st = {"active": "🟢", "dead": "🔴", "banned_in_chat": "🟡"}.get(b["status"], "⚪")
                res = f" → res {b['resource_id']}" if b["resource_id"] else ""
                lines.append(
                    f"{st} #{b['id']} {b['name']} (uid {b['user_id']}){res}\n"
                    f"    ✅{b['posts_ok']} ❌{b['posts_fail']}")
            send(vk, event.peer_id, "\n".join(lines))
            return True

        if sub == "add":
            if len(parts) < 3:
                send(vk, event.peer_id, "📌 /ad bot add TOKEN [resource_id]")
                return True
            token = parts[2]
            res_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            r = ad.add_bot(token, resource_id=res_id)
            if r.get("ok"):
                send(vk, event.peer_id,
                     f"✅ Бот добавлен.\nID: {r['bot_id']}\n"
                     f"User: {r['user_id']}\nИмя: {r['name']}")
            else:
                send(vk, event.peer_id, f"❌ {r.get('error')}")
            return True

        if sub == "del":
            if len(parts) < 3 or not parts[2].isdigit():
                send(vk, event.peer_id, "📌 /ad bot del ID")
                return True
            ok = ad.remove_bot(int(parts[2]))
            send(vk, event.peer_id, "✅ Удалён." if ok else "❌ Не найден.")
            return True

        if sub == "check":
            bots = ad.list_bots()
            if not bots:
                send(vk, event.peer_id, "🤖 Ботов нет.")
                return True
            send(vk, event.peer_id, f"⏳ Проверяю {len(bots)} ботов...")
            results = []
            for b in bots:
                st = ad.check_bot(b["id"])
                icon = "🟢" if st == "active" else "🔴"
                results.append(f"{icon} #{b['id']} {b['name']} — {st}")
            send(vk, event.peer_id, "\n".join(results))
            return True

        if sub == "flags":
            subargs = " ".join(parts[2:])
            return cmd_ad_bot_flags(vk, event, subargs)

        if sub == "res":
            if len(parts) < 4 or not parts[2].isdigit():
                send(vk, event.peer_id, "📌 /ad bot res ID RESOURCE_ID [screen_name]")
                return True
            bid = int(parts[2])
            rid = int(parts[3])
            sn = parts[4] if len(parts) > 4 else ""
            ok = ad.set_bot_resource(bid, rid, sn)
            send(vk, event.peer_id,
                 "✅ Ресурс привязан." if ok else "❌ Бот не найден.")
            return True

    # /ad target ...
    if action == "target":
        if len(parts) < 2:
            send(vk, event.peer_id, "📌 /ad target add OWNER_ID [screen] | del ID | list")
            return True
        sub = parts[1].lower()

        if sub == "list":
            targets = ad.list_targets()
            if not targets:
                send(vk, event.peer_id, "🎯 Целей нет.")
                return True
            lines = ["🎯 Цели:"]
            for t in targets:
                st = {"active": "🟢", "banned": "🔴"}.get(t["status"], "⚪")
                lines.append(
                    f"{st} #{t['id']} {t['title'] or t['screen_name']} "
                    f"(owner {t['owner_id']}) | fails={t['fails']}")
            send(vk, event.peer_id, "\n".join(lines))
            return True

        if sub == "add":
            if len(parts) < 3:
                send(vk, event.peer_id, "📌 /ad target add OWNER_ID [screen_name]")
                return True
            try:
                oid = int(parts[2])
            except ValueError:
                send(vk, event.peer_id, "❌ OWNER_ID должен быть числом (для групп — с минусом).")
                return True
            sn = parts[3] if len(parts) > 3 else None
            r = ad.add_target(oid, screen_name=sn)
            if r.get("ok"):
                send(vk, event.peer_id, f"✅ Цель добавлена. ID: {r['target_id']}")
            else:
                send(vk, event.peer_id, f"❌ {r.get('error')}")
            return True

        if sub == "del":
            if len(parts) < 3 or not parts[2].isdigit():
                send(vk, event.peer_id, "📌 /ad target del ID")
                return True
            ok = ad.remove_target(int(parts[2]))
            send(vk, event.peer_id, "✅ Удалена." if ok else "❌ Не найдена.")
            return True

    send(vk, event.peer_id, "🤔 /ad on|off|test|msg|bot|target")
    return True


def cmd_news(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True
    if not args.strip():
        send(vk, event.peer_id,
             "📢 /news ТЕКСТ — рассылка всем юзерам\n"
             "Например: /news 🎉 Обновление! Добавлен автопост статистики.\n\n"
             "🆕 /release — система постов обновлений\n"
             "  /release add Заголовок | Текст\n"
             "  /release list / show N / del N\n"
             "  /release wall N / chats N / all N")
        return True
    text = args.strip()
    send(vk, event.peer_id,
         f"🚀 Отправляю {text.count(chr(10)) + 1}-строчное сообщение всем...")

    ok, fail = background.send_news(vk, text)
    send(vk, event.peer_id,
         f"✅ Рассылка завершена.\n"
         f"• Доставлено: {ok}\n"
         f"• Ошибок: {fail}")
    return True


def cmd_wallpost(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        return True

    args = args.strip()
    if not args:
        return show_wallpost(vk, event)

    low = args.lower()
    if low in ("on", "вкл", "1"):
        db.set_config("wall_post_enabled", "1")
        send(vk, event.peer_id, "🟢 Автопост включён.",
             keyboard=kb.wallpost_menu(True))
        return True
    if low in ("off", "выкл", "0"):
        db.set_config("wall_post_enabled", "0")
        send(vk, event.peer_id, "🔴 Автопост выключен.",
             keyboard=kb.wallpost_menu(False))
        return True

    if low == "now":
        ok = background.post_wall_stats(vk)
        send(vk, event.peer_id,
             "✅ Пост опубликован." if ok else "❌ Не удалось опубликовать. Смотри лог.")
        return True

    if low.startswith("interval"):
        try:
            n = int(args.split()[1])
        except (IndexError, ValueError):
            send(vk, event.peer_id, "📌 /wallpost interval N (мин, мин. 15)")
            return True
        if n < 15:
            send(vk, event.peer_id, "❌ Минимум 15 минут.")
            return True
        db.set_config("wall_post_interval", str(n))
        send(vk, event.peer_id, f"⏱ Интервал: {n} мин.")
        return True

    if low.startswith("template"):
        text = args[8:].strip()
        if not text:
            send(vk, event.peer_id, "📌 /wallpost template ТЕКСТ (можно вставить {stats} в шаблон)")
            return True
        db.set_config("wall_post_template", text)
        send(vk, event.peer_id, f"✏️ Шаблон сохранён:\n{text}")
        return True

    send(vk, event.peer_id, "🤔 /wallpost on|off|now|interval N|template TEXT")
    return True


def cmd_give(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        send(vk, event.peer_id, "⛔ Доступ запрещён.")
        return True

    parts = args.split(maxsplit=2)
    if len(parts) < 2:
        send(vk, event.peer_id,
             "📌 Нужно: USER_ID СУММА [комментарий]\n"
             "Пример: 156002808 5000 бонус")
        return True

    try:
        target = int(parts[0])
        amount = int(parts[1])
    except ValueError:
        send(vk, event.peer_id, "❌ ID и сумма должны быть числами.")
        return True

    if amount == 0:
        send(vk, event.peer_id, "❌ Сумма не может быть нулевой.")
        return True

    comment = parts[2] if len(parts) > 2 else ""
    target_user = db.get_user(target)
    if not target_user:
        send(vk, event.peer_id, f"❌ Юзер {target} не найден в БД.")
        return True

    db.add_balance(target, amount, reason="admin")
    updated = db.get_user(target)
    new_balance = updated["balance"] if updated else (target_user["balance"] + amount)
    sign = "+" if amount > 0 else ""
    log.info("admin_give: %s -> %s: %s%s", event.user_id, target, sign, amount)

    send(vk, event.peer_id,
         f"✅ Начислено:\n"
         f"• Юзер: {target_user['first_name'] or target} (id{target})\n"
         f"• Сумма: {sign}{amount} PP\n"
         f"• Новый баланс: {new_balance} PP"
         + (f"\n• Комментарий: {comment}" if comment else ""),
         keyboard=kb.admin_menu())

    try:
        send(vk, target,
             f"🎁 Тебе начислено {sign}{amount} PP от администратора."
             + (f"\nКомментарий: {comment}" if comment else ""))
    except Exception as e:
        log.warning("give notify failed: %s", e)
    return True


def cmd_setowner(vk, event, args: str) -> bool:
    if not is_owner(event.user_id):
        send(vk, event.peer_id, "⛔ Только текущий владелец может передать права.")
        return True

    args = args.strip()
    if not args.isdigit():
        send(vk, event.peer_id, "📌 /setowner USER_ID")
        return True

    new_owner = int(args)
    if new_owner == event.user_id:
        send(vk, event.peer_id, "ℹ️ Ты уже владелец.")
        return True

    if not db.get_user(new_owner):
        send(vk, event.peer_id,
             f"❌ Юзер {new_owner} ещё не писал боту. "
             f"Попроси его сначала отправить /start.")
        return True

    db.set_config("owner_id", str(new_owner))
    log.warning("OWNER CHANGED: %s -> %s", event.user_id, new_owner)
    send(vk, event.peer_id, f"✅ Владелец изменён на id{new_owner}.")
    try:
        send(vk, new_owner,
             "👑 Ты назначен владельцем PiarBot!\n"
             "Пиши /start — увидишь админ-панель.")
    except Exception as e:
        log.warning("owner notify failed: %s", e)
    return True


# ============================================================
# ГЛАВНЫЙ РОУТЕР
# ============================================================

KNOWN_BUTTONS = {
    "🔧 Админ-панель", "🚀 Автопромо", "📝 Автопост", "🎯 Реклама",
    "📢 Рассылка", "📣 В беседы", "📎 Экспорт", "⚙️ Настройки",
    "📊 Статистика", "👤 Режим пользователя", "⬅️ В меню",
    "🎁 Начислить PP", "🟢 Включить промо", "🔴 Выключить промо",
    "✏️ Текст промо", "⏱ Интервал", "📢 Канал бота", "💬 Главная беседа",
    "📋 Боты", "🎯 Цели", "🤖 Имитация", "➕ Добавить бота",
    "➕ Добавить цель", "🗑 Удалить бота", "🗑 Удалить цель",
    "🔄 Проверить всех", "🔄 Обновить список",
    "🟢 Включить рекламу", "🔴 Выключить рекламу", "🧪 Тест рекламы",
    "📝 Текст рекламы", "👥 Боты (подписки)", "📊 Статистика имитации",
    "🟢 Включить имитацию", "🔴 Выключить имитацию", "🔑 Получить токен",
    "🟢 Включить автопост", "🔴 Выключить автопост",
    "📤 Опубликовать сейчас", "✏️ Шаблон поста", "⏱ Интервал поста",
    "❌ Отмена",
}


def handle(vk, event, user_info: dict) -> bool:
    user_id = event.user_id
    peer_id = event.peer_id
    text = (event.text or "").strip()

    log.debug("admin.handle: user=%s text=%r", user_id, text)

    # Только для владельца
    if not is_owner(user_id):
        return False

    # --- /cancel и кнопка Отмена ---
    if text in ("/cancel", "❌ Отмена"):
        if get_state(user_id):
            clear_state(user_id)
            send(vk, peer_id, "❌ Отменено.", keyboard=kb.admin_menu())
        else:
            send(vk, peer_id, "ℹ️ Нечего отменять.", keyboard=kb.admin_menu())
        return True

    # --- stateful ввод ---
    # кнопки всегда перебивают state (чтобы не зацикливаться)
    if text in KNOWN_BUTTONS:
        # если нажата "Отмена" — сброс
        if text == "❌ Отмена":
            clear_state(user_id)
            send(vk, peer_id, "❌ Отменено.", keyboard=kb.admin_menu())
            return True
        # остальные кнопки — сбросить state и обработать как кнопку
        clear_state(user_id)

    st = get_state(user_id)
    if st:
        return _process_state(vk, event, st, text, user_info)

    # --- команды ---
    if text.startswith("/export"):
        args = text[7:].strip()
        return cmd_export(vk, event, args)

    if text.startswith("/ad"):
        args = text[3:].strip()
        return cmd_ad(vk, event, args)

    if text.startswith("/setinvite"):
        args = text[10:].strip()
        return cmd_setinvite(vk, event, args)

    if text.startswith("/broadcast"):
        args = text[10:].strip()
        return cmd_broadcast(vk, event, args)

    if text.startswith("/news"):
        args = text[5:].strip()
        return cmd_news(vk, event, args)

    if text.startswith("/wallpost"):
        args = text[len("/wallpost"):].strip()
        return cmd_wallpost(vk, event, args)

    if text.startswith("/give"):
        return cmd_give(vk, event, text[5:].strip())
    if text.startswith("/setowner"):
        return cmd_setowner(vk, event, text[9:].strip())
    if text == "/stats":
        return show_stats(vk, event)

    # --- кнопки меню ---
    if text == "🔧 Админ-панель":
        return show_admin_menu(vk, event)
    if text == "🚀 Автопромо":
        return show_promo(vk, event)
    if text == "📝 Автопост":
        return show_wallpost(vk, event)
    if text == "🎯 Реклама":
        return show_ad(vk, event)
    if text == "📢 Рассылка":
        set_state(user_id, "news_text")
        send(vk, event.peer_id,
             "📢 Отправь текст для рассылки всем юзерам бота.\n\n"
             "❌ Отмена — /cancel",
             keyboard=kb.cancel_kb())
        return True
    if text == "📣 В беседы":
        set_state(user_id, "broadcast_text")
        send(vk, event.peer_id,
             "📣 Отправь текст для рассылки во все беседы.\n\n"
             "❌ Отмена — /cancel",
             keyboard=kb.cancel_kb())
        return True

    if text == "📎 Экспорт":
        send(vk, event.peer_id,
             "📎 Что выгрузить?\n"
             "• /export users — юзеры\n"
             "• /export chats — беседы\n"
             "• /export rewards — награды",
             keyboard=kb.admin_menu())
        return True
    if text == "⚙️ Настройки":
        return show_settings(vk, event)
    if text == "📊 Статистика":
        return show_stats(vk, event)
    if text == "👤 Режим пользователя":
        return False  # private.py откроет главное меню

    # --- промо: toggle ---
    if text == "🟢 Включить промо":
        db.set_config("promo_enabled", "1")
        send(vk, peer_id, "🟢 Автопромо включено.", keyboard=kb.promo_menu(True))
        return True
    if text == "🔴 Выключить промо":
        db.set_config("promo_enabled", "0")
        send(vk, peer_id, "🔴 Автопромо выключено.", keyboard=kb.promo_menu(False))
        return True

    if text == "🟢 Включить рекламу":
        db.set_config("ad_enabled", "1")
        send(vk, peer_id, "🟢 Реклама включена.", keyboard=kb.ad_menu(True))
        return True
    if text == "🔴 Выключить рекламу":
        db.set_config("ad_enabled", "0")
        send(vk, peer_id, "🔴 Реклама выключена.", keyboard=kb.ad_menu(False))
        return True
    if text == "🧪 Тест рекламы":
        res = ad.do_one_post()
        if res.get("ok"):
            send(vk, peer_id, f"✅ Пост опубликован: бот #{res['bot']} → цель #{res['target']}")
        else:
            send(vk, peer_id, f"❌ Не удалось: {res.get('reason') or res.get('error')}")
        return True
    if text == "🎯 Цели":
        return show_ad_targets(vk, event)
    if text == "➕ Добавить цель":
        set_state(user_id, "ad_target_add")
        send(vk, peer_id,
             "🎯 Пришли ссылку на паблик-конкурент:\n"
             "Например: vk.com/igrapiargrupp\n\n"
             "❌ Отмена — /cancel",
             keyboard=kb.cancel_kb())
        return True
    if text == "🗑 Удалить цель":
        set_state(user_id, "ad_target_del")
        send(vk, peer_id,
             "🗑 Пришли ID цели для удаления.\n"
             "Список: 🎯 Цели\n\n"
             "❌ Отмена — /cancel",
             keyboard=kb.cancel_kb())
        return True
    if text == "🔄 Обновить список":
        return show_ad_targets(vk, event)

    if text == "🔑 Получить токен":
        send(vk, peer_id,
             "🔑 КАК ПОЛУЧИТЬ ТОКЕН\n"
             "━━━━━━━━━━━━━━━━\n"
             "1. Открой: https://vkhost.github.io/\n"
             "2. Выбери приложение Kate Mobile (или VK Admin)\n"
             "3. Разреши доступ для своего аккаунта\n"
             "4. Скопируй токен из адресной строки (начинается с vk1.a.)\n"
             "5. Пришли сюда следующим сообщением\n\n"
             "💡 Также можно скопировать всю строку URL целиком — "
             "я сам вытащу токен.\n\n"
             "❌ Отмена — /cancel",
             keyboard=kb.cancel_kb())
        return True

    if text == "➕ Добавить бота":
        set_state(user_id, "ad_bot_add")
        send(vk, peer_id,
             "🤖 ДОБАВИТЬ РЕКЛАМНОГО БОТА\n"
             "━━━━━━━━━━━━━━━━\n"
             "У тебя уже есть токен аккаунта?\n\n"
             "• ЕСТЬ → пришли его следующим сообщением\n"
             "• НЕТ → жми 🔑 Получить токен\n\n"
             "Что присылать:\n"
             "• чистый токен: vk1.a.xxx\n"
             "• или URL из адресной строки после входа\n\n"
             "Опционально: ТОКЕН RESOURCE_ID — сразу привязать свой паблик\n\n"
             "❌ Отмена — /cancel",
             keyboard=kb.ad_bot_add_kb())
        return True
    if text == "🗑 Удалить бота":
        set_state(user_id, "ad_bot_del")
        send(vk, peer_id,
             "🗑 Пришли ID бота для удаления.\n\n"
             "❌ Отмена — /cancel",
             keyboard=kb.cancel_kb())
        return True
    if text == "🔄 Проверить всех":
        bots = ad.list_bots()
        if not bots:
            send(vk, peer_id, "🤖 Ботов нет.", keyboard=kb.ad_bots_kb())
            return True
        send(vk, peer_id, f"⏳ Проверяю {len(bots)} ботов...")
        results = []
        for b in bots:
            st = ad.check_bot(b["id"])
            icon = "🟢" if st == "active" else "🔴"
            results.append(f"{icon} #{b['id']} {b['name']} — {st}")
        send(vk, peer_id, "\n".join(results), keyboard=kb.ad_bots_kb())
        return True

    if text == "🤖 Имитация":
        return show_ad_imitation(vk, event)
    if text == "👥 Боты (подписки)":
        return show_ad_bots_subscriptions(vk, event)
    if text == "📊 Статистика имитации":
        return show_ad_imitation(vk, event)
    if text == "🟢 Включить имитацию":
        db.set_config("imitation_enabled", "1")
        send(vk, peer_id, "🟢 Имитация включена.",
             keyboard=kb.ad_imitation_kb(True))
        return True
    if text == "🔴 Выключить имитацию":
        db.set_config("imitation_enabled", "0")
        send(vk, peer_id, "🔴 Имитация выключена.",
             keyboard=kb.ad_imitation_kb(False))
        return True

    if text == "📋 Боты":
        bots = ad.list_bots()
        if not bots:
            send(vk, peer_id, "🤖 Ботов нет. Добавь: ➕ Добавить бота",
                 keyboard=kb.ad_bots_kb())
            return True
        lines = ["🤖 Боты:"]
        for b in bots:
            st = {"active": "🟢", "dead": "🔴"}.get(b["status"], "⚪")
            lines.append(f"{st} #{b['id']} {b['name']} ✅{b['posts_ok']} ❌{b['posts_fail']}")
        send(vk, peer_id, "\n".join(lines), keyboard=kb.ad_bots_kb())
        return True

    if text == "📝 Текст рекламы":
        set_state(user_id, "ad_message")
        cur = db.get_config("ad_message", "") or "(по умолчанию)"
        send(vk, peer_id,
             f"📝 Отправь текст рекламы.\n\n"
             f"Плейсхолдеры: {{bot}} — ссылка на бота, {{invite}} — приглашение в беседу.\n\n"
             f"Текущий:\n{cur}",
             keyboard=kb.cancel_kb())
        return True
    if text == "🟢 Включить автопост":
        db.set_config("wall_post_enabled", "1")
        send(vk, peer_id, "🟢 Автопост включён.", keyboard=kb.wallpost_menu(True))
        return True
    if text == "🔴 Выключить автопост":
        db.set_config("wall_post_enabled", "0")
        send(vk, peer_id, "🔴 Автопост выключен.", keyboard=kb.wallpost_menu(False))
        return True
    if text == "📤 Опубликовать сейчас":
        ok = background.post_wall_stats(vk)
        send(vk, peer_id,
             "✅ Пост опубликован." if ok else "❌ Не удалось. Смотри лог.")
        return True
    # --- stateful-кнопки: единый обработчик ---
    if text in STATEFUL_PROMPTS:
        state, key, default, tpl = STATEFUL_PROMPTS[text]
        set_state(user_id, state)
        if key is None:
            msg = tpl
        else:
            current = db.get_config(key, default) or default
            msg = tpl.replace("{current}", str(current))
        send(vk, peer_id, msg, keyboard=kb.cancel_kb())
        return True

    return False


if __name__ == "__main__":
    print("=== admin.py OK ===")
    print("Экспорт: handle, show_admin_menu, show_promo, show_stats, show_settings")
