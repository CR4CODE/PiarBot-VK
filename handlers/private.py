# handlers/private.py — личный кабинет (ЛС бота)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
import random
import string
from datetime import datetime, timedelta

import db
import keyboards as kb
from config import (
    SIGNUP_BONUS, DAILY_BONUS, REFERRAL_BONUS,
    REFERRAL_MILESTONES, CHAT_MILESTONES, BOT_LINK,
)
from utils import send, group_url, user_url, mention, utcnow, is_owner
from handlers import admin as h_admin
import release

log = logging.getLogger(__name__)


# ---------- реф-код ----------

def gen_ref_code(user_id: int) -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{user_id % 100000:05d}{suffix}"


def get_or_create_user(user_id: int, first_name: str = "",
                       last_name: str = "", ref_code: str = "") -> dict:
    row = db.get_user(user_id)
    if row:
        db.execute(
            "UPDATE users SET first_name=?, last_name=?, "
            "last_seen=CURRENT_TIMESTAMP WHERE user_id=?",
            (first_name, last_name, user_id),
        )
        return dict(db.get_user(user_id))

    referrer_id = None
    if ref_code:
        ref_row = db.query_one(
            "SELECT user_id FROM users WHERE referral_code=?", (ref_code,))
        if ref_row and ref_row["user_id"] != user_id:
            referrer_id = ref_row["user_id"]

    import sqlite3 as _sqlite

    last_err = None
    for _attempt in range(5):
        my_code = gen_ref_code(user_id)
        try:
            with db.transaction():
                db.execute(
                    "INSERT INTO users(user_id, first_name, last_name, "
                    "referral_code, referred_by, balance) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, first_name, last_name, my_code, referrer_id, 0),
                )
                db.add_balance(user_id, SIGNUP_BONUS, reason="signup")

                if referrer_id:
                    db.execute(
                        "INSERT OR IGNORE INTO referrals(referrer_id, referred_id) "
                        "VALUES (?, ?)", (referrer_id, user_id))
                    db.add_balance(referrer_id, REFERRAL_BONUS, reason="referral")
            last_err = None
            break
        except _sqlite.IntegrityError as e:
            last_err = e
            # если юзер уже создан (гонка) — просто вернём его
            existing = db.get_user(user_id)
            if existing:
                return dict(existing)
            # иначе — коллизия referral_code, пробуем ещё раз
            continue

    if last_err:
        raise last_err

    if referrer_id:
        check_referral_milestones(referrer_id)

    return dict(db.get_user(user_id))


def check_referral_milestones(referrer_id: int) -> list:
    awarded = []
    with db.transaction():
        cnt_row = db.query_one(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?",
            (referrer_id,))
        cnt = cnt_row["c"] if cnt_row else 0
        for milestone, bonus in REFERRAL_MILESTONES.items():
            if cnt >= milestone:
                cur = db.execute(
                    "INSERT OR IGNORE INTO referral_milestones"
                    "(referrer_id, milestone, bonus) VALUES (?, ?, ?)",
                    (referrer_id, milestone, bonus),
                )
                if cur.rowcount > 0:
                    db.add_balance(referrer_id, bonus, reason="milestone")
                    awarded.append(milestone)
    return awarded


# ---------- daily bonus ----------

def try_daily_bonus(user_id: int) -> bool:
    """Выдать ежедневный бонус. Транзакция защищает от двойного нажатия."""
    with db.transaction():
        last = db.query_one(
            "SELECT created_at FROM rewards WHERE user_id=? AND reason='daily' "
            "ORDER BY id DESC LIMIT 1", (user_id,))
        if last:
            try:
                last_dt = datetime.fromisoformat(last["created_at"])
                if utcnow() - last_dt < timedelta(hours=24):
                    return False
            except Exception:
                pass
        db.add_balance(user_id, DAILY_BONUS, reason="daily")
    return True


# ---------- секции ЛК ----------

def _fmt_profile(u: dict) -> str:
    from utils import round_reward
    rounds = u.get("rounds", 0)
    next_reward = round_reward(rounds + 1)
    refs_row = db.query_one(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?", (u["user_id"],))
    refs = refs_row["c"] if refs_row else 0
    chats_row = db.query_one(
        "SELECT COUNT(*) AS c FROM chats WHERE admin_id=? AND is_active=1",
        (u["user_id"],))
    chats = chats_row["c"] if chats_row else 0
    name = ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip()
    return (
        f"👤 Профиль\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🎭 {name or ('id'+str(u['user_id']))}\n"
        f"🆔 {u['user_id']}\n\n"
        f"💰 Баланс: {u['balance']} PP\n"
        f"🎯 Раундов пройдено: {rounds}\n"
        f"🎁 Награда за следующий: {next_reward} PP\n\n"
        f"👥 Рефералов: {refs}\n"
        f"🏠 Бесед подключено: {chats}\n"
    )


def show_profile(vk, peer_id: int, user_id: int) -> None:
    u = get_or_create_user(user_id)
    send(vk, peer_id, _fmt_profile(u), keyboard=kb.profile_kb())


def show_resources(vk, peer_id: int, user_id: int) -> None:
    rows = db.query(
        "SELECT c.*, ch.title AS chat_title FROM channels c "
        "LEFT JOIN chats ch ON ch.peer_id = c.peer_id "
        "WHERE c.user_id=? ORDER BY c.added_at DESC", (user_id,))
    if not rows:
        send(vk, peer_id,
             "📢 Мои ресурсы\n\n"
             "Ты пока нигде не участвуешь.\n"
             "Зайди в беседу с ботом и напиши ссылку на свой паблик — "
             "она попадёт в активный список или очередь.",
             keyboard=kb.resources_kb())
        return
    lines = ["📢 Мои ресурсы", "━━━━━━━━━━━━━━━━"]
    status_map = {"active": "🟢 активен", "queue": "🟡 в очереди", "pending": "⚪ ждёт"}
    for r in rows:
        title = r["title"] or r["screen_name"] or f"id{r['resource_id']}"
        chat = r["chat_title"] or f"peer{r['peer_id']}"
        lines.append(f"• {title}\n  {status_map.get(r['status'], r['status'])} | {chat}")
    send(vk, peer_id, "\n".join(lines), keyboard=kb.resources_kb())


def show_add_resource(vk, peer_id: int, user_id: int) -> None:
    text = (
        "➕ Добавить свой ресурс в раунд\n"
        "━━━━━━━━━━━━━━━━\n"
        "1. Открой беседу, где уже есть бот PiarBot.\n"
        "2. Напиши в беседе ссылку на свой паблик ВКонтакте,\n"
        "   например: vk.com/go_gigi\n"
        "3. Бот проверит подписки и добавит тебя в активный список\n"
        "   или в очередь раунда.\n\n"
        "Если беседы пока нет — нажми «➕ Добавить в беседу»."
    )
    send(vk, peer_id, text, keyboard=kb.resources_kb())


def show_my_code(vk, peer_id: int, user_id: int) -> None:
    u = get_or_create_user(user_id)
    code = u["referral_code"]
    text = (
        "📝 Твоя реферальная команда\n"
        "━━━━━━━━━━━━━━━━\n"
        "Отправь эту команду другу, чтобы он её скопировал "
        "и отправил боту:\n\n"
        f"/start {code}\n\n"
        "👇 Просто перешли ему это сообщение или скопируй команду выше."
    )
    send(vk, peer_id, text, keyboard=kb.back_to_menu())


def show_referrals(vk, peer_id: int, user_id: int) -> None:
    u = get_or_create_user(user_id)
    code = u["referral_code"]
    refs_row = db.query_one(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?", (user_id,))
    refs = refs_row["c"] if refs_row else 0
    link = f"{BOT_LINK}?ref={code}"
    milestones_lines = []
    for m, bonus in REFERRAL_MILESTONES.items():
        done = db.query_one(
            "SELECT 1 FROM referral_milestones WHERE referrer_id=? AND milestone=?",
            (user_id, m))
        mark = "✅" if done else ("🔸" if refs >= m else "⚪")
        milestones_lines.append(f"{mark} {m} друзей → {bonus} PP")
    text = (
        f"👥 Рефералы\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Приглашено: {refs}\n"
        f"За каждого друга: {REFERRAL_BONUS} PP\n\n"
        f"🔗 Твоя ссылка:\n{link}\n\n"
        f"Другу нужно написать боту:\n/start {code}\n\n"
        f"🏆 Вехи:\n" + "\n".join(milestones_lines)
    )
    send(vk, peer_id, text, keyboard=kb.referrals_kb(link))


def show_buy(vk, peer_id: int, user_id: int) -> None:
    send(vk, peer_id,
         "💰 Купить PP\n\n"
         "🚧 Магазин в разработке.\n"
         "Пока PP можно заработать:\n"
         "• 🎯 проходить раунды\n"
         "• 👥 приглашать друзей\n"
         "• ➕ подключать беседы",
         keyboard=kb.buy_kb())


def show_chats(vk, peer_id: int, user_id: int, only_mine: bool = False) -> None:
    if only_mine:
        rows = db.query(
            "SELECT peer_id, chat_id, title, invite_link, list_size, admin_id "
            "FROM chats WHERE is_active=1 AND admin_id=? "
            "ORDER BY registered_at DESC", (user_id,))
        header = "📍 Беседы, где ты админ"
    else:
        rows = db.query(
            "SELECT peer_id, chat_id, title, invite_link, list_size, admin_id "
            "FROM chats WHERE is_active=1 ORDER BY registered_at DESC")
        header = "🌐 Все беседы бота"

    if not rows:
        send(vk, peer_id, f"{header}\n\nНичего не найдено.",
             keyboard=kb.chats_kb())
        return

    lines = [header, "━━━━━━━━━━━━━━━━"]
    for r in rows:
        title = r["title"] or f"chat{r['chat_id']}"
        active = db.query_one(
            "SELECT COUNT(*) AS c FROM channels WHERE peer_id=? AND status='active'",
            (r["peer_id"],))["c"]
        queue = db.query_one(
            "SELECT COUNT(*) AS c FROM channels WHERE peer_id=? AND status='queue'",
            (r["peer_id"],))["c"]
        admin_txt = mention(r["admin_id"]) if r["admin_id"] else "—"
        lines.append(
            f"• {title}\n"
            f"  N={r['list_size']} | 🟢{active} | 🟡{queue} | админ: {admin_txt}")
    send(vk, peer_id, "\n".join(lines), keyboard=kb.chats_kb())


def show_top(vk, peer_id: int, user_id: int) -> None:
    rows = db.query(
        "SELECT user_id, first_name, last_name, balance, rounds "
        "FROM users WHERE is_banned=0 ORDER BY balance DESC LIMIT 10")
    lines = ["📊 Топ-10 участников", "━━━━━━━━━━━━━━━━"]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i+1}."
        name = ((r["first_name"] or "") + " " + (r["last_name"] or "")).strip()
        name = name or f"id{r['user_id']}"
        lines.append(f"{mark} {name} — {r['balance']} PP ({r['rounds']} раундов)")
    send(vk, peer_id, "\n".join(lines), keyboard=kb.top_kb())


def show_share(vk, peer_id: int, user_id: int) -> None:
    u = get_or_create_user(user_id)
    link = f"{BOT_LINK}?ref={u['referral_code']}"
    send(vk, peer_id,
         f"📢 Поделиться ботом\n\n"
         f"Ссылка на бота:\n{BOT_LINK}\n\n"
         f"Твоя реферальная:\n{link}",
         keyboard=kb.share_kb(link))


def show_help(vk, peer_id: int, user_id: int) -> None:
    text = (
        "ℹ️ КАК ПОЛЬЗОВАТЬСЯ PiarBot\n"
        "━━━━━━━━━━━━━━━━\n"
        "🎯 ЧТО ЭТО\n"
        "Бот взаимного пиара: подписываешься на участников раунда — "
        "получаешь подписчиков на свой паблик.\n\n"
        "🚀 КАК НАЧАТЬ\n"
        "1. Добавь бота в свою беседу VK\n"
        "2. Админ беседы пишет /setchat\n"
        "3. Все пишут ссылку на свой паблик\n"
        "4. Первые N участников → активный список\n"
        "5. Остальные → очередь (ждут ротации)\n\n"
        "💰 PIAR POINTS (PP)\n"
        "• +100 за 1-й раунд, дальше +60% каждый\n"
        "• +1000 за друга (реферал)\n"
        "• +10000 за подключение беседы\n"
        "• +100 ежедневный бонус /daily\n\n"
        "📱 ЛИЧНЫЙ КАБИНЕТ\n"
        "• /start — главное меню\n"
        "• /daily — ежедневный бонус\n"
        "• 👤 Профиль — баланс и статистика\n"
        "• 📢 Мои ресурсы — где участвуешь\n"
        "• 👥 Рефералы — приглашай друзей\n"
        "• 📊 Топ участников — лидеры по PP\n\n"
        "💬 КОМАНДЫ В БЕСЕДЕ\n"
        "• vk.com/ваш_паблик — в раунд\n"
        "• /list — текущий раунд\n"
        "• /help — справка\n\n"
        "🔧 ДЛЯ АДМИНОВ БЕСЕД\n"
        "• /setchat Название\n"
        "• /add ссылка — обязательная подписка\n"
        "• /editround N — размер списка\n"
        "• /setadmin ID — сменить админа\n"
        "• /ban ID, /unban ID\n\n"
        "❓ Вопросы: пиши в главную беседу бота."
    )
    send(vk, peer_id, text, keyboard=kb.help_kb(BOT_LINK))


def show_add_chat(vk, peer_id: int, user_id: int) -> None:
    text = (
        "➕ Добавить бота в свою беседу\n"
        "━━━━━━━━━━━━━━━━\n"
        "1. Открой свою беседу VK.\n"
        "2. Управление → Участники → Пригласить → найди "
        f"{BOT_LINK.rsplit('/', 1)[-1]}.\n"
        "3. Или добавь сообщество по ссылке:\n"
        f"{BOT_LINK}\n\n"
        "4. После добавления напиши в беседе /setchat — "
        "получишь 10000 PP (до 3 бесед в день)."
    )
    send(vk, peer_id, text, keyboard=kb.add_chat_kb())


# ---------- роутер ЛК ----------

MENU_ROUTES = {
    "👤 Профиль":         show_profile,
    "📢 Мои ресурсы":     show_resources,
    "👥 Рефералы":        show_referrals,
    "💰 Купить PP":       show_buy,
    "📋 Все беседы":      show_chats,
    "🌐 Все беседы":      show_chats,
    "📍 Где я админ":     lambda v, p, u: show_chats(v, p, u, only_mine=True),
    "📊 Топ участников":  show_top,
    "📢 Поделиться":      show_share,
    "ℹ️ Помощь":          show_help,
    "➕ Добавить в беседу": show_add_chat,
    "📋 Моя ссылка":      show_referrals,
    "📝 Моя команда":     show_my_code,
    "➕ Добавить ресурс":  show_add_resource,
}




def handle(vk, event, user_info: dict) -> bool:
    user_id = event.user_id
    peer_id = event.peer_id
    text = (event.text or "").strip()

    # --- админка (проверяем ДО всего остального меню) ---
    if h_admin.handle(vk, event, user_info):
        return True

    # --- /release ---
    if text == "/release" or text.startswith("/release "):
        args = text[len("/release"):].strip()
        return release.handle_release(vk, event, args)

    # --- /start ---
    if text == "/start" or text.startswith("/start "):
        parts = text.split(maxsplit=1)
        ref_code = parts[1].strip() if len(parts) > 1 else ""
        new = db.get_user(user_id) is None
        u = get_or_create_user(
            user_id, user_info.get("first_name", ""),
            user_info.get("last_name", ""), ref_code)

        if new:
            bonus_row = db.query_one(
                "SELECT amount FROM rewards WHERE user_id=? AND reason='signup' "
                "ORDER BY id DESC LIMIT 1", (user_id,))
            bonus = bonus_row["amount"] if bonus_row else 0
            greet = (
                f"👋 Привет, {u['first_name'] or 'друг'}!\n\n"
                f"Добро пожаловать в PiarBot — бот взаимного пиара для бесед VK.\n\n"
                f"🤝 Как это работает:\n"
                f"В беседах с ботом собираются раунды из участников.\n"
                f"Все подписываются друг на друга — и получают подписчиков.\n\n"
                f"🎁 Тебе начислено {bonus} PP за регистрацию!\n"
            )
            greet += (
                "\n📌 Что делать:\n"
                "1. Добавь бота в свою беседу или зайди в существующую\n"
                "2. Напиши там ссылку на свой паблик\n"
                "3. Подпишись на участников раунда — попадёшь в список\n"
                "4. За каждый раунд получай Piar Points (PP)"
            )
            if ref_code and u.get("referred_by"):
                greet += "👥 Ты пришёл по приглашению — твой реферер получил бонус."
            greet += "\n\n🔽 Используй кнопки меню ниже."
        else:
            greet = (
                f"👋 С возвращением, {u['first_name'] or 'друг'}!\n"
                f"💰 Баланс и статистика — в меню.\n"
                f"🔽 Что делаем?"
            )

        send(vk, peer_id, greet, keyboard=kb.main_menu(is_owner(user_id)))
        return True

    # --- ежедневный бонус ---
    if text.lower() in ("/daily", "🎁 бонус дня", "бонус дня"):
        if try_daily_bonus(user_id):
            send(vk, peer_id,
                 f"🎁 Держи {DAILY_BONUS} PP! Приходи завтра снова.",
                 keyboard=kb.main_menu(is_owner(user_id)))
        else:
            send(vk, peer_id, "⏳ Бонус дня уже получен. Возвращайся через 24 часа.",
                 keyboard=kb.main_menu(is_owner(user_id)))
        return True

    if text == "⬅️ В меню":
        send(vk, peer_id, "🏠 Главное меню:",
             keyboard=kb.main_menu(is_owner(user_id)))
        return True

    # --- кнопки меню ---
    if text in MENU_ROUTES:
        fn = MENU_ROUTES[text]
        if fn:
            fn(vk, peer_id, user_id)
            return True

    # Юзер прислал ссылку в ЛС — направляем в беседу
    from utils import extract_vk_resource, looks_like_any_link
    if extract_vk_resource(text) or looks_like_any_link(text):
        invite = db.get_config("main_chat_invite", "")
        text_reply = (
            "⚠️ Ссылку нужно отправлять в БЕСЕДУ, где есть бот.\n"
            "━━━━━━━━━━━━━━━━\n"
            "Бот принимает ссылки только в чатах — так работает "
            "взаимная подписка: все участники видят раунд.\n\n"
            "📌 Что делать:\n"
            "1. Зайди в любую беседу с ботом\n"
            "2. Напиши там ссылку на свой паблик\n"
            "3. Попадёшь в раунд и получишь Piar Points\n"
        )
        if invite:
            text_reply += f"\n👇 Наша главная беседа:\n{invite}"
        send(vk, peer_id, text_reply,
             keyboard=kb.help_kb(BOT_LINK))
        return True

    send(vk, peer_id,
         "🤖 Не понял команду. Используй кнопки меню ниже.",
         keyboard=kb.main_menu(is_owner(user_id)))
    return True


if __name__ == "__main__":
    print("=== private.py OK ===")
