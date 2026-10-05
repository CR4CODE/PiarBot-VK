# handlers/chat.py — логика бесед VK
# --- фикс путей ---
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# -----------------

import logging
import time
from datetime import datetime, timedelta

import db
from config import (
    CHAT_ADD_BONUS, CHAT_ADD_DAILY_LIMIT, CHAT_MILESTONES,
    LIST_SIZE_DEFAULT, WARN_DELETE_SECONDS, GREETING_LIFETIME,
    SKIP_ADD_PRICE, PENDING_ADD_TTL,
)
from utils import (
    send, extract_vk_resource, looks_like_any_link,
    to_chat_id, mention, round_reward, utcnow,
)
from background import schedule_delete

log = logging.getLogger(__name__)


# ============================================================
# КЭШ АДМИНОВ
# ============================================================

_admin_cache: dict[int, tuple[float, int | None]] = {}
_ADMIN_TTL = 300


def _fetch_chat_admin(vk, peer_id: int):
    now = time.time()
    cached = _admin_cache.get(peer_id)
    if cached and now - cached[0] < _ADMIN_TTL:
        return cached[1]
    admin_id = None
    try:
        resp = vk.messages.getConversationMembers(peer_id=peer_id)
        items = resp.get("items", []) if isinstance(resp, dict) else []
        for it in items:
            member_id = it.get("member_id")
            if member_id is None or member_id < 0:
                continue
            if it.get("is_admin") or it.get("is_owner"):
                admin_id = member_id
                break
    except Exception as e:
        log.info("getConversationMembers(%s) failed: %s", peer_id, e)
    _admin_cache[peer_id] = (now, admin_id)
    return admin_id


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================

def _resolve_vk_resource(vk, slug: str):
    # нормализация: clubN/publicN → N, vk.com/xxx → xxx
    norm = slug.strip().lstrip("@")
    if norm.lower().startswith(("club", "public")):
        num = norm[4:] if norm.lower().startswith("club") else norm[6:]
        if num.isdigit():
            norm = int(num)
    try:
        resp = vk.groups.getById(group_id=norm)
        g = resp[0] if isinstance(resp, list) else resp.get("groups", [{}])[0]
        return {
            "id": int(g["id"]),
            "type": "group",
            "screen_name": g.get("screen_name", ""),
            "title": g.get("name", ""),
        }
    except Exception as e:
        log.warning("resolve_vk_resource(%s) failed: %s", slug, e)
        return None


def _is_member(vk, user_id: int, group_id: int):
    try:
        resp = vk.groups.isMember(group_id=abs(group_id), user_id=user_id)
        if isinstance(resp, dict):
            return bool(resp.get("member", 0))
        return bool(resp)
    except Exception as e:
        code = getattr(e, "code", None)
        if code == 15:
            return "hidden"
        return None


def _chat_default_title(peer_id: int) -> str:
    return f"Беседа №{to_chat_id(peer_id)}"


def _get_chat(peer_id: int):
    return db.query_one("SELECT * FROM chats WHERE peer_id=?", (peer_id,))


def _is_chat_admin(vk, peer_id: int, user_id: int) -> bool:
    row = _get_chat(peer_id)
    if not row:
        return False
    if row["admin_id"] == user_id:
        return True
    real_admin = _fetch_chat_admin(vk, peer_id)
    if real_admin and real_admin == user_id:
        db.execute("UPDATE chats SET admin_id=? WHERE peer_id=?",
                   (user_id, peer_id))
        log.info("admin переопределён по API: peer=%s user=%s", peer_id, user_id)
        return True
    return False


def _check_chat_milestones(vk, user_id: int) -> None:
    cnt_row = db.query_one(
        "SELECT COUNT(*) AS c FROM chat_add_rewards WHERE user_id=?", (user_id,)
    )
    cnt = cnt_row["c"] if cnt_row else 0
    for milestone, bonus in CHAT_MILESTONES.items():
        if cnt >= milestone:
            exists = db.query_one(
                "SELECT 1 FROM chat_milestones WHERE user_id=? AND milestone=?",
                (user_id, milestone),
            )
            if not exists:
                db.execute(
                    "INSERT INTO chat_milestones(user_id, milestone, bonus) "
                    "VALUES (?, ?, ?)", (user_id, milestone, bonus),
                )
                db.add_balance(user_id, bonus, reason="milestone")
                try:
                    send(vk, user_id,
                         f"🏆 Веха бесед: {milestone}!\nБонус: {bonus} PP 🎉")
                except Exception as e:
                    log.warning("milestone DM failed: %s", e)


# ============================================================
# /setchat
# ============================================================

def cmd_setchat(vk, event, args: str = "") -> bool:
    peer_id = event.peer_id
    user_id = event.user_id
    existing = _get_chat(peer_id)

    if existing:
        real_admin = _fetch_chat_admin(vk, peer_id)
        if real_admin and real_admin != existing["admin_id"]:
            db.execute("UPDATE chats SET admin_id=? WHERE peer_id=?",
                       (real_admin, peer_id))
            existing = _get_chat(peer_id)
        send(vk, peer_id,
             f"ℹ️ Беседа уже зарегистрирована.\n"
             f"Название: {existing['title']}\n"
             f"Админ: {mention(existing['admin_id'])}\n"
             f"Размер списка (N): {existing['list_size']}")
        return True

    real_admin = _fetch_chat_admin(vk, peer_id)
    if real_admin is None:
        send(vk, peer_id,
             "❌ Не могу определить администратора беседы.\n"
             "Дай боту право «Управление участниками» в беседе.")
        return True
    if user_id != real_admin:
        send(vk, peer_id,
             f"⛔ Только админ беседы может её зарегистрировать.\n"
             f"Текущий админ: {mention(real_admin)}")
        return True

    title = args.strip() or _chat_default_title(peer_id)
    chat_id = to_chat_id(peer_id)
    db.execute(
        "INSERT INTO chats(peer_id, chat_id, title, admin_id, pending, "
        "list_size) VALUES (?, ?, ?, ?, 0, ?)",
        (peer_id, chat_id, title, user_id, LIST_SIZE_DEFAULT),
    )
    log.info("Зарегистрирована беседа peer=%s title=%s admin=%s",
             peer_id, title, user_id)

    owner_id = db.get_config("owner_id", "0")
    bonus_line = ""
    if str(user_id) != owner_id:
        day_ago = (utcnow() - timedelta(hours=24)).isoformat(sep=" ")
        cnt_row = db.query_one(
            "SELECT COUNT(*) AS c FROM chat_add_rewards "
            "WHERE user_id=? AND awarded_at >= ?", (user_id, day_ago),
        )
        cnt = cnt_row["c"] if cnt_row else 0
        if cnt < CHAT_ADD_DAILY_LIMIT:
            db.execute(
                "INSERT INTO chat_add_rewards(user_id, peer_id, amount) "
                "VALUES (?, ?, ?)", (user_id, peer_id, CHAT_ADD_BONUS),
            )
            db.add_balance(user_id, CHAT_ADD_BONUS, reason="chat_bonus",
                           peer_id=peer_id)
            bonus_line = (f"\n\n🎁 Тебе начислено {CHAT_ADD_BONUS} PP "
                          f"за подключение беседы!")
            _check_chat_milestones(vk, user_id)
        else:
            bonus_line = (f"\n\n⏳ Лимит бонусов за беседы "
                          f"({CHAT_ADD_DAILY_LIMIT}/сутки) исчерпан.")

    send(vk, peer_id,
         f"✅ Беседа зарегистрирована!\n"
         f"━━━━━━━━━━━━━━━━\n"
         f"Название: {title}\n"
         f"Админ: {mention(user_id)}\n"
         f"Размер активного списка: {LIST_SIZE_DEFAULT}\n\n"
         f"📌 Что дальше:\n"
         f"• /add ссылка — добавить обязательную подписку\n"
         f"• Участники пишут ссылку на свой паблик — она попадает в раунд\n"
         f"• /list — посмотреть текущий раунд"
         + bonus_line)
    return True


# ============================================================
# /add
# ============================================================

def cmd_add(vk, event, args: str) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id

    if not _is_chat_admin(vk, peer_id, user_id):
        send(vk, peer_id, "⛔ Только админ беседы может добавлять подписки.")
        return True

    if not args.strip():
        send(vk, peer_id,
             "📌 Синтаксис: /add ссылка_на_паблик\n"
             "Пример: /add https://vk.com/go_gigi")
        return True

    parsed = extract_vk_resource(args.strip())
    if not parsed:
        send(vk, peer_id, "❌ Не вижу VK-ссылку в этом сообщении.")
        return True

    res = _resolve_vk_resource(vk, parsed["slug"])
    if not res:
        send(vk, peer_id, "❌ Не нашёл такой паблик ВКонтакте.")
        return True

    exists = db.query_one(
        "SELECT 1 FROM mandatory_subs WHERE peer_id=? AND resource_id=?",
        (peer_id, res["id"]),
    )
    if exists:
        send(vk, peer_id, f"ℹ️ «{res['title']}» уже в списке обязательных.")
        return True

    db.execute(
        "INSERT INTO mandatory_subs(peer_id, resource_id, resource_type, "
        "screen_name, title, added_by) VALUES (?, ?, ?, ?, ?, ?)",
        (peer_id, res["id"], res["type"], res["screen_name"], res["title"], user_id),
    )
    send(vk, peer_id,
         f"✅ Добавлено в обязательные:\n"
         f"{res['title']}\n"
         f"https://vk.com/{res['screen_name']}")
    return True


# ============================================================
# /editround
# ============================================================

def cmd_editround(vk, event, args: str) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id

    if not _is_chat_admin(vk, peer_id, user_id):
        send(vk, peer_id, "⛔ Только админ беседы может менять размер раунда.")
        return True

    row = _get_chat(peer_id)
    if not row:
        send(vk, peer_id, "❌ Беседа не зарегистрирована. /setchat")
        return True

    if not args.strip():
        send(vk, peer_id,
             f"📌 /editround N — размер активного списка\n"
             f"Сейчас: {row['list_size']}\n"
             f"Диапазон: 1-20")
        return True

    try:
        n = int(args.strip().split()[0])
    except ValueError:
        send(vk, peer_id, "❌ Нужно число.")
        return True

    if n < 1 or n > 20:
        send(vk, peer_id, "❌ Число от 1 до 20.")
        return True

    old = row["list_size"]
    db.execute("UPDATE chats SET list_size=? WHERE peer_id=?", (n, peer_id))
    log.info("editround: peer=%s %s -> %s by %s", peer_id, old, n, user_id)
    send(vk, peer_id, f"✅ Размер активного списка: {old} → {n}")
    return True


# ============================================================
# /setadmin
# ============================================================

def cmd_setadmin(vk, event, args: str) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id

    if not _is_chat_admin(vk, peer_id, user_id):
        send(vk, peer_id, "⛔ Только текущий админ может передать права.")
        return True

    args = args.strip()
    if not args:
        row = _get_chat(peer_id)
        cur = row["admin_id"] if row else "?"
        send(vk, peer_id,
             "📌 /setadmin USER_ID\n"
             f"Сейчас админ: {mention(cur)}")
        return True

    try:
        target = int(args.split()[0].lstrip("@").replace("[id", "").split("|")[0])
    except ValueError:
        send(vk, peer_id, "❌ Нужен числовой user_id.")
        return True

    if not db.get_user(target):
        send(vk, peer_id, f"❌ Юзер {target} не в БД.")
        return True

    real_admin = _fetch_chat_admin(vk, peer_id)
    if real_admin is None:
        send(vk, peer_id, "❌ Не могу проверить участников беседы.")
        return True

    if target != real_admin and target != user_id:
        is_member = False
        try:
            resp = vk.messages.getConversationMembers(peer_id=peer_id)
            ids = [it.get("member_id") for it in resp.get("items", [])]
            is_member = target in ids
        except Exception as e:
            log.info("setadmin member check failed: %s", e)
        if not is_member:
            send(vk, peer_id, f"❌ Юзер {target} не в беседе.")
            return True

    db.execute("UPDATE chats SET admin_id=? WHERE peer_id=?", (target, peer_id))
    _admin_cache.pop(peer_id, None)
    log.info("setadmin: peer=%s %s -> %s", peer_id, user_id, target)
    send(vk, peer_id, f"✅ Админ беседы: {mention(target)}")
    return True


# ============================================================
# /ban /unban
# ============================================================

def cmd_ban(vk, event, args: str) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id

    if not _is_chat_admin(vk, peer_id, user_id):
        send(vk, peer_id, "⛔ Только админ беседы.")
        return True

    args = args.strip()
    if not args:
        send(vk, peer_id, "📌 /ban USER_ID")
        return True

    try:
        target = int(args.split()[0].lstrip("@").replace("[id", "").split("|")[0])
    except ValueError:
        send(vk, peer_id, "❌ Нужен числовой user_id.")
        return True

    if target == user_id:
        send(vk, peer_id, "❌ Себя нельзя.")
        return True

    if not db.get_user(target):
        send(vk, peer_id, f"❌ Юзер {target} не в БД.")
        return True

    db.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (target,))
    db.execute("DELETE FROM channels WHERE peer_id=? AND user_id=?",
               (peer_id, target))
    log.info("ban: peer=%s target=%s by=%s", peer_id, target, user_id)
    send(vk, peer_id, f"🚫 {mention(target)} забанен.")
    return True


def cmd_unban(vk, event, args: str) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id

    if not _is_chat_admin(vk, peer_id, user_id):
        send(vk, peer_id, "⛔ Только админ беседы.")
        return True

    args = args.strip()
    if not args:
        send(vk, peer_id, "📌 /unban USER_ID")
        return True

    try:
        target = int(args.split()[0].lstrip("@").replace("[id", "").split("|")[0])
    except ValueError:
        send(vk, peer_id, "❌ Нужен числовой user_id.")
        return True

    db.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (target,))
    log.info("unban: target=%s by=%s", target, user_id)
    send(vk, peer_id, f"✅ {mention(target)} разбанен.")
    return True


# ============================================================
# /list
# ============================================================

def cmd_chatstats(vk, event) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id

    if not _is_chat_admin(vk, peer_id, user_id):
        send(vk, peer_id, "⛔ Только админ беседы.")
        return True

    row = _get_chat(peer_id)
    if not row:
        send(vk, peer_id, "❌ Беседа не зарегистрирована. /setchat")
        return True

    active = db.query_one(
        "SELECT COUNT(*) AS c FROM channels WHERE peer_id=? AND status='active'",
        (peer_id,))["c"]
    queue = db.query_one(
        "SELECT COUNT(*) AS c FROM channels WHERE peer_id=? AND status='queue'",
        (peer_id,))["c"]
    mandatory = db.query_one(
        "SELECT COUNT(*) AS c FROM mandatory_subs WHERE peer_id=?",
        (peer_id,))["c"]
    total_rounds = db.query_one(
        "SELECT COUNT(*) AS c FROM rewards WHERE peer_id=? AND reason='round'",
        (peer_id,))["c"]

    top_rounds = db.query(
        "SELECT user_id, COUNT(*) AS c FROM rewards "
        "WHERE peer_id=? AND reason='round' "
        "GROUP BY user_id ORDER BY c DESC LIMIT 5",
        (peer_id,))

    unique = db.query_one(
        "SELECT COUNT(DISTINCT user_id) AS c FROM rewards "
        "WHERE peer_id=? AND reason='round'",
        (peer_id,))["c"]

    lines = [
        f"📊 Статистика беседы «{row['title']}»",
        "━━━━━━━━━━━━━━━━",
        f"👤 Админ: {mention(row['admin_id'])}",
        f"📐 Размер списка: N={row['list_size']}",
        f"",
        f"📌 Обязательных подписок: {mandatory}",
        f"🟢 Активных: {active}",
        f"🟡 В очереди: {queue}",
        f"",
        f"🎯 Раундов пройдено: {total_rounds}",
        f"👥 Уникальных участников: {unique}",
    ]

    if top_rounds:
        lines.append("")
        lines.append("🏆 Топ участников по раундам:")
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(top_rounds):
            mark = medals[i] if i < 3 else f"{i+1}."
            u = db.get_user(r["user_id"])
            name = (u["first_name"] if u else "") or f"id{r['user_id']}"
            lines.append(f"{mark} {name} — {r['c']}")

    send(vk, peer_id, "\n".join(lines))
    return True


def cmd_list(vk, event) -> bool:
    peer_id = event.peer_id
    row = _get_chat(peer_id)
    if not row:
        send(vk, peer_id, "❌ Беседа не зарегистрирована. Напиши /setchat.")
        return True

    active = db.query(
        "SELECT * FROM channels WHERE peer_id=? AND status='active' "
        "ORDER BY position", (peer_id,))
    queue = db.query(
        "SELECT * FROM channels WHERE peer_id=? AND status='queue' "
        "ORDER BY position", (peer_id,))
    mandatory = db.query(
        "SELECT * FROM mandatory_subs WHERE peer_id=?", (peer_id,))

    lines = [f"📋 Раунд беседы «{row['title']}» (N={row['list_size']})",
             "━━━━━━━━━━━━━━━━"]

    if mandatory:
        lines.append(f"\n📌 Обязательные ({len(mandatory)}):")
        for m in mandatory:
            link = f"vk.com/{m['screen_name']}" if m['screen_name'] else f"id{m['resource_id']}"
            lines.append(f"• {m['title']} — {link}")

    if active:
        lines.append(f"\n🟢 Активные ({len(active)}/{row['list_size']}):")
        for i, a in enumerate(active, 1):
            t = a["title"] or a["screen_name"] or f"id{a['resource_id']}"
            link = f"vk.com/{a['screen_name']}" if a['screen_name'] else f"id{a['resource_id']}"
            lines.append(f"{i}. {t} ({link}) — {mention(a['user_id'])}")
    else:
        lines.append("\n🟢 Активных пока нет.")

    if queue:
        lines.append(f"\n🟡 Очередь ({len(queue)}):")
        for i, q in enumerate(queue, 1):
            t = q["title"] or q["screen_name"] or f"id{q['resource_id']}"
            link = f"vk.com/{q['screen_name']}" if q['screen_name'] else f"id{q['resource_id']}"
            lines.append(f"{i}. {t} ({link}) — {mention(q['user_id'])}")
    else:
        lines.append("\n🟡 Очередь пуста.")

    send(vk, peer_id, "\n".join(lines))
    return True


# ============================================================
# ПРИЁМ ССЫЛКИ
# ============================================================

def handle_link(vk, event) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id
    text = (event.text or "").strip()

    row = _get_chat(peer_id)
    if not row:
        return False

    u = db.get_user(user_id)
    if u and u["is_banned"]:
        send(vk, peer_id, f"🚫 {mention(user_id)}, ты в бане.")
        return True

    parsed = extract_vk_resource(text)
    if not parsed:
        return False

    if parsed["type_hint"] == "user":
        send(vk, peer_id, "❌ Личные страницы не участвуют. Только паблики.")
        return True

    res = _resolve_vk_resource(vk, parsed["slug"])
    if not res:
        send(vk, peer_id, "❌ Не нашёл такой паблик.")
        return True

    missing_mandatory, hidden_mandatory = [], []
    for m in db.query("SELECT * FROM mandatory_subs WHERE peer_id=?", (peer_id,)):
        r = _is_member(vk, user_id, m["resource_id"])
        if r is False:
            missing_mandatory.append(m)
        elif r == "hidden":
            hidden_mandatory.append(m)

    missing_active, hidden_active = [], []
    for a in db.query(
        "SELECT * FROM channels WHERE peer_id=? AND status='active' "
        "AND user_id != ?", (peer_id, user_id)):
        r = _is_member(vk, user_id, a["resource_id"])
        if r is False:
            missing_active.append(a)
        elif r == "hidden":
            hidden_active.append(a)

    if hidden_mandatory or hidden_active:
        send(vk, peer_id,
             f"🚫 {mention(user_id)}, не могу проверить твои подписки.\n"
             "━━━━━━━━━━━━━━━━\n"
             "У тебя в настройках приватности VK закрыт список подписок.\n\n"
             "Чтобы участвовать, открой его:\n"
             "VK → Настройки → Приватность → «Кто видит мои подписки» → "
             "«Все» или «Друзья».")
        return True

    if missing_mandatory or missing_active:
        db.execute("INSERT OR REPLACE INTO pending_adds(user_id, peer_id, resource_id, resource_type, screen_name, title, created_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)", (user_id, peer_id, res["id"], res["type"], res["screen_name"], res["title"]))
        lines = [f"⚠️ {mention(user_id)}, ты не подписан на всё, что нужно.", ""]
        if missing_mandatory:
            lines.append("📌 Обязательные:")
            for m in missing_mandatory:
                lines.append(f"• {m['title']} — https://vk.com/{m['screen_name']}")
        if missing_active:
            lines.append("\n🟢 Активные:")
            for a in missing_active:
                t = a["title"] or a["screen_name"]
                lines.append(f"• {t} — https://vk.com/{a['screen_name']}")
        lines.append("\nПодпишись и пришли ссылку ещё раз.")
        lines.append("")
        lines.append(f"💸 Или пропусти проверку за {SKIP_ADD_PRICE} PP: /skip")
        send(vk, peer_id, "\n".join(lines))
        return True

    existing = db.query_one(
        "SELECT * FROM channels WHERE peer_id=? AND user_id=?",
        (peer_id, user_id))
    if existing:
        send(vk, peer_id,
             f"ℹ️ {mention(user_id)}, ты уже в раунде:\n"
             f"«{existing['title'] or existing['screen_name']}» "
             f"(статус: {existing['status']}).")
        return True

    import sqlite3 as _sqlite

    try:
        with db.transaction():
            active_count = db.query_one(
                "SELECT COUNT(*) AS c FROM channels "
                "WHERE peer_id=? AND status='active'",
                (peer_id,))["c"]

            if active_count < row["list_size"]:
                status = "active"
                pos = active_count + 1
            else:
                status = "queue"
                pos = db.query_one(
                    "SELECT COUNT(*) AS c FROM channels "
                    "WHERE peer_id=? AND status='queue'",
                    (peer_id,))["c"] + 1

            db.execute(
                "INSERT INTO channels(peer_id, user_id, resource_id, "
                "resource_type, screen_name, title, status, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (peer_id, user_id, res["id"], res["type"],
                 res["screen_name"], res["title"], status, pos),
            )
    except _sqlite.IntegrityError:
        send(vk, peer_id,
             f"ℹ️ {mention(user_id)}, ты уже в раунде.")
        return True

    reward_line = ""
    if status == "active":
        u = db.get_user(user_id)
        current_rounds = u["rounds"] if u else 0
        reward = round_reward(current_rounds + 1)
        db.add_balance(user_id, reward, reason="round",
                       peer_id=peer_id, round_number=current_rounds + 1)
        db.execute("UPDATE users SET rounds = rounds + 1 WHERE user_id=?",
                   (user_id,))
        reward_line = f"\n\n🎁 Награда за раунд: +{reward} PP"
        log.info("round reward: user=%s reward=%s (round %s)",
                 user_id, reward, current_rounds + 1)

    if status == "active":
        send(vk, peer_id,
             f"✅ {mention(user_id)}, твой ресурс «{res['title']}» "
             f"добавлен в активный список! 🟢\n"
             f"Позиция: {pos}/{row['list_size']}"
             + reward_line)
    else:
        send(vk, peer_id,
             f"🟡 {mention(user_id)}, твой ресурс «{res['title']}» в очереди.\n"
             f"Позиция: {pos}. Попадёт в раунд после ротации.")

    _recount_and_maybe_rotate(vk, peer_id)
    return True


# ============================================================
# РОТАЦИЯ
# ============================================================

def _recount_and_maybe_rotate(vk, peer_id: int) -> None:
    row = _get_chat(peer_id)
    if not row:
        return
    N = row["list_size"]
    active = db.query_one(
        "SELECT COUNT(*) AS c FROM channels WHERE peer_id=? AND status='active'",
        (peer_id,))["c"]
    queue = db.query_one(
        "SELECT COUNT(*) AS c FROM channels WHERE peer_id=? AND status='queue'",
        (peer_id,))["c"]
    db.execute("UPDATE chats SET pending=? WHERE peer_id=?", (active, peer_id))
    if active >= N and queue >= N:
        _do_rotation(vk, peer_id)


def _do_rotation(vk, peer_id: int) -> None:
    """Ротация раунда: active сбрасывается, топ-N из queue становятся active.

    Всем новым активным начисляется награда за новый раунд.
    Оставшаяся очередь перенумеровывается.
    """
    log.info("РОТАЦИЯ peer=%s", peer_id)

    row = _get_chat(peer_id)
    if not row:
        return
    N = row["list_size"]

    with db.transaction():
        # 1. Взять топ-N из очереди по позиции
        promoted = db.query(
            "SELECT user_id, title, screen_name FROM channels "
            "WHERE peer_id=? AND status='queue' "
            "ORDER BY position ASC LIMIT ?",
            (peer_id, N),
        )

        # 2. Удалить только активных
        db.execute(
            "DELETE FROM channels WHERE peer_id=? AND status='active'",
            (peer_id,),
        )

        # 3. Queue -> active с новыми позициями
        promoted_users = []
        for i, p in enumerate(promoted, 1):
            db.execute(
                "UPDATE channels SET status='active', position=? "
                "WHERE peer_id=? AND user_id=?",
                (i, peer_id, p["user_id"]),
            )
            promoted_users.append(dict(p))

        # 4. Оставшуюся очередь перенумеровать
        rest = db.query(
            "SELECT user_id FROM channels "
            "WHERE peer_id=? AND status='queue' "
            "ORDER BY position ASC",
            (peer_id,),
        )
        for i, r in enumerate(rest, 1):
            db.execute(
                "UPDATE channels SET position=? "
                "WHERE peer_id=? AND user_id=?",
                (i, peer_id, r["user_id"]),
            )

        # 5. Обновить pending
        db.execute(
            "UPDATE chats SET pending=? WHERE peer_id=?",
            (len(promoted), peer_id),
        )

    # 6. Награды — вне транзакции, но по порядку (rounds читаем в момент)
    total_reward = 0
    for p in promoted_users:
        uid = p["user_id"]
        u = db.get_user(uid)
        current_rounds = u["rounds"] if u else 0
        reward = round_reward(current_rounds + 1)
        db.add_balance(uid, reward, reason="round",
                       peer_id=peer_id, round_number=current_rounds + 1)
        db.execute("UPDATE users SET rounds = rounds + 1 WHERE user_id=?", (uid,))
        total_reward += reward
        log.info("round reward (rotation): user=%s reward=%s (round %s)",
                 uid, reward, current_rounds + 1)

    # 7. Сообщение о новом раунде
    if promoted_users:
        lines = [
            "🔄 НОВЫЙ РАУНД ЗАПУЩЕН!",
            "━━━━━━━━━━━━━━━━",
            f"Из очереди в игру: {len(promoted_users)} участников.",
            "",
            "🟢 Новый активный список:",
        ]
        for i, p in enumerate(promoted_users, 1):
            title = p.get("title") or p.get("screen_name") or f"id{p['user_id']}"
            lines.append(f"{i}. {title} — https://vk.com/{p.get('screen_name') or ''}")
        lines.append("")
        lines.append("Подпишись на всех из списка — и присылай свою ссылку заново! 🚀")
        send(vk, peer_id, "\n".join(lines))
    else:
        send(vk, peer_id,
             "🔄 РАУНД ЗАВЕРШЁН!\n"
             "━━━━━━━━━━━━━━━━\n"
             "Активный список обнулён.\n"
             "Хочешь остаться — пришли ссылку на свой паблик заново! 🚀")


# ============================================================
# МОДЕРАЦИЯ
# ============================================================

def moderate(vk, event) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id
    text = (event.text or "").strip()

    if not text:
        return False
    if _is_chat_admin(vk, peer_id, user_id):
        return False
    if not _get_chat(peer_id):
        return False
    if not looks_like_any_link(text) and not text.startswith("@"):
        return False

    parsed = extract_vk_resource(text)
    if not parsed:
        _delete_and_warn(vk, event, "🚫 Сторонние ссылки запрещены.")
        return True

    without_link = text
    for token in text.split():
        if extract_vk_resource(token):
            without_link = without_link.replace(token, "", 1)
    if without_link.strip():
        _delete_and_warn(vk, event,
                         "✋ Отправь только ссылку, без лишнего текста.")
        return True
    return False


def _delete_and_warn(vk, event, text: str) -> None:
    peer_id = event.peer_id
    try:
        vk.messages.delete(message_ids=event.message_id, delete_for_all=1)
    except Exception as e:
        log.warning("delete failed: %s", e)
    warn_id = send(vk, peer_id, f"{text}\n\n{mention(event.user_id)}")
    if warn_id:
        schedule_delete(peer_id, warn_id, WARN_DELETE_SECONDS)


# ============================================================
# ПРИВЕТСТВИЕ НОВИЧКОВ
# ============================================================

def greet_newbie(vk, peer_id: int, user_id: int) -> bool:
    cur = db.execute(
        "INSERT OR IGNORE INTO chat_members(peer_id, user_id) VALUES (?, ?)",
        (peer_id, user_id))
    if cur.rowcount == 0:
        return False

    text = (
        f"👋 {mention(user_id)}, добро пожаловать в чат!\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Здесь бот взаимного пиара: подписываешься на участников раунда — "
        f"получаешь подписчиков на свой паблик.\n\n"
        f"📌 Что делать:\n"
        f"• Напиши ссылку на свой паблик (vk.com/xxx) — попадёшь в раунд\n"
        f"• /list — посмотреть текущий раунд\n"
        f"• /help — команды"
    )
    msg_id = send(vk, peer_id, text)
    if msg_id:
        schedule_delete(peer_id, msg_id, GREETING_LIFETIME)
    return True


# ============================================================
# РОУТЕР
# ============================================================

def handle(vk, event) -> bool:
    text = (event.text or "").strip()
    peer_id = event.peer_id
    user_id = event.user_id

    if _get_chat(peer_id) and user_id > 0:
        greet_newbie(vk, peer_id, user_id)

    if text == "/setchat" or text.startswith("/setchat "):
        args = text[len("/setchat"):].strip()
        return cmd_setchat(vk, event, args)

    if text == "/add" or text.startswith("/add "):
        args = text[4:].strip()
        return cmd_add(vk, event, args)

    if text == "/editround" or text.startswith("/editround "):
        args = text[len("/editround"):].strip()
        return cmd_editround(vk, event, args)

    if text == "/setadmin" or text.startswith("/setadmin "):
        args = text[len("/setadmin"):].strip()
        return cmd_setadmin(vk, event, args)

    if text == "/ban" or text.startswith("/ban "):
        args = text[4:].strip()
        return cmd_ban(vk, event, args)

    if text == "/unban" or text.startswith("/unban "):
        args = text[6:].strip()
        return cmd_unban(vk, event, args)

    if text == "/list":
        return cmd_list(vk, event)

    if text in ("/chatstats", "/stats"):
        return cmd_chatstats(vk, event)

    if text in ("/help", "ℹ️ помощь"):
        send(vk, peer_id,
             "ℹ️ КОМАНДЫ В БЕСЕДЕ\n"
             "━━━━━━━━━━━━━━━━\n"
             "📌 Для всех участников:\n"
             "• vk.com/ваш_паблик — попасть в раунд\n"
             "• /list — посмотреть текущий раунд\n"
             "• /help — эта справка\n\n"
             "🔧 Для админа беседы:\n"
             "• /setchat Название — зарегистрировать беседу\n"
             "• /add ссылка — добавить обязательную подписку\n"
             "• /editround N — размер активного списка (1-20)\n"
             "• /setadmin ID — назначить админа\n"
             "• /ban ID — забанить участника\n"
             "• /unban ID — снять бан\n\n"
             "💡 Правила:\n"
             "• Подписка на всех активных — обязательна\n"
             "• Попал в очередь — жди ротации\n"
             "• После ротации присылай ссылку заново\n"
             "• За раунд капают Piar Points (PP)")
        return True

    if text in ("/skip", "💸 Пропустить за 1500 PP", "💸 пропустить за 1500 pp"):
        return cmd_skip(vk, event)

    if moderate(vk, event):
        return True

    if handle_link(vk, event):
        return True

    return False


if __name__ == "__main__":
    print("=== chat.py OK ===")


# ============================================================
# ПЛАТНОЕ ДОБАВЛЕНИЕ В ОЧЕРЕДЬ (SKIP)
# ============================================================

def cmd_skip(vk, event) -> bool:
    peer_id = event.peer_id
    user_id = event.user_id

    row = _get_chat(peer_id)
    if not row:
        return False

    pending = db.query_one("SELECT * FROM pending_adds WHERE user_id=? AND peer_id=?", (user_id, peer_id))
    if not pending:
        send(vk, peer_id, f"🤷 Нет отложенной заявки. Пришли ссылку заново. {mention(user_id)}")
        return True

    try:
        dt = datetime.strptime(pending["created_at"], "%Y-%m-%d %H:%M:%S")
        age = (utcnow() - dt).total_seconds()
    except Exception:
        age = 0

    if age > PENDING_ADD_TTL:
        db.execute("DELETE FROM pending_adds WHERE user_id=? AND peer_id=?", (user_id, peer_id))
        send(vk, peer_id, f"⏰ Заявка устарела. Пришли ссылку заново. {mention(user_id)}")
        return True

    u = db.get_user(user_id)
    balance = u["balance"] if u else 0
    if balance < SKIP_ADD_PRICE:
        send(vk, peer_id, f"❌ Не хватает PP. Нужно {SKIP_ADD_PRICE}, у тебя {balance}. {mention(user_id)}")
        return True

    existing = db.query_one("SELECT * FROM channels WHERE peer_id=? AND user_id=?", (peer_id, user_id))
    if existing:
        db.execute("DELETE FROM pending_adds WHERE user_id=? AND peer_id=?", (user_id, peer_id))
        send(vk, peer_id, f"ℹ️ Ты уже в раунде. {mention(user_id)}")
        return True

    import sqlite3 as _sqlite
    try:
        with db.transaction():
            pos = db.query_one("SELECT COUNT(*) AS c FROM channels WHERE peer_id=? AND status='queue'", (peer_id,))["c"] + 1
            db.execute("INSERT INTO channels(peer_id, user_id, resource_id, resource_type, screen_name, title, status, position) VALUES (?, ?, ?, ?, ?, ?, 'queue', ?)", (peer_id, user_id, pending["resource_id"], pending["resource_type"], pending["screen_name"], pending["title"], pos))
            db.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (SKIP_ADD_PRICE, user_id))
            db.execute("INSERT INTO rewards(user_id, peer_id, amount, reason) VALUES (?, ?, ?, 'skip_add')", (user_id, peer_id, -SKIP_ADD_PRICE))
            db.execute("DELETE FROM pending_adds WHERE user_id=? AND peer_id=?", (user_id, peer_id))
    except _sqlite.IntegrityError:
        send(vk, peer_id, f"ℹ️ Ты уже в раунде. {mention(user_id)}")
        return True

    try:
        vk.messages.delete(message_ids=event.message_id, delete_for_all=1)
    except Exception as e:
        log.warning("delete skip failed: %s", e)

    title = pending["title"] or pending["screen_name"] or "ресурс"
    msg_id = send(vk, peer_id, f"✅ Ты в очереди за {SKIP_ADD_PRICE} PP.\nРесурс: «{title}»\nПозиция: {pos}. {mention(user_id)}")
    if msg_id:
        schedule_delete(peer_id, msg_id, 30)

    _recount_and_maybe_rotate(vk, peer_id)
    return True
