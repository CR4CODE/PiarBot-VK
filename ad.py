# ad.py — рекламные боты (мульти-аккаунт)
import logging
import random
import time
from datetime import datetime

import vk_api

import db

log = logging.getLogger("ad")


# ============================================================
# БОТЫ
# ============================================================

def add_bot(token: str, resource_id: int = None,
            screen_name: str = None, name: str = None) -> dict:
    """Валидирует токен и добавляет бота. Возвращает {'ok': bool, ...}"""
    raw = token.strip()
    if not raw:
        return {"ok": False, "error": "пустой токен"}

    # попытка вытащить токен из URL
    token = extract_token(raw)
    if not token:
        return {"ok": False, "error": "не вижу токен в сообщении"}

    try:
        session = vk_api.VkApi(token=token)
        api = session.get_api()
        me = api.users.get()[0]
        user_id = me["id"]
        first = me.get("first_name", "")
        last = me.get("last_name", "")
    except Exception as e:
        return {"ok": False, "error": f"токен нерабочий: {e}"}

    existing = db.query_one("SELECT id FROM ad_bots WHERE user_id=?", (user_id,))
    if existing:
        return {"ok": False, "error": f"бот {user_id} уже добавлен"}

    db.execute(
        "INSERT INTO ad_bots(user_id, name, token, resource_id, screen_name, "
        "status) VALUES (?, ?, ?, ?, ?, 'active')",
        (user_id, name or f"{first} {last}".strip(), token,
         resource_id, screen_name),
    )
    row = db.query_one("SELECT id FROM ad_bots WHERE user_id=?", (user_id,))
    log.info("ad_bot added: id=%s user=%s name=%s",
             row["id"], user_id, name)
    return {"ok": True, "bot_id": row["id"], "user_id": user_id,
            "name": name or f"{first} {last}".strip()}


def remove_bot(bot_id: int) -> bool:
    row = db.query_one("SELECT id FROM ad_bots WHERE id=?", (bot_id,))
    if not row:
        return False
    db.execute("DELETE FROM ad_bots WHERE id=?", (bot_id,))
    db.execute("UPDATE ad_posts SET bot_id=NULL WHERE bot_id=?", (bot_id,))
    log.info("ad_bot removed: id=%s", bot_id)
    return True


def list_bots() -> list:
    return db.query(
        "SELECT id, user_id, name, resource_id, screen_name, status, "
        "posts_ok, posts_fail, last_check, last_post, notes "
        "FROM ad_bots ORDER BY id")


def get_bot(bot_id: int):
    return db.query_one("SELECT * FROM ad_bots WHERE id=?", (bot_id,))


def check_bot(bot_id: int) -> str:
    """Проверяет живость токена. Возвращает новый статус."""
    row = get_bot(bot_id)
    if not row:
        return "not_found"
    try:
        api = vk_api.VkApi(token=row["token"]).get_api()
        api.users.get()
        status = "active"
    except Exception as e:
        log.info("bot %s dead: %s", bot_id, e)
        status = "dead"

    db.execute(
        "UPDATE ad_bots SET status=?, last_check=CURRENT_TIMESTAMP WHERE id=?",
        (status, bot_id))
    return status


def set_bot_status(bot_id: int, status: str, note: str = "") -> None:
    db.execute(
        "UPDATE ad_bots SET status=?, notes=COALESCE(NULLIF(?, ''), notes) "
        "WHERE id=?", (status, note, bot_id))


def set_bot_resource(bot_id: int, resource_id: int, screen_name: str = "") -> bool:
    row = get_bot(bot_id)
    if not row:
        return False
    db.execute(
        "UPDATE ad_bots SET resource_id=?, screen_name=? WHERE id=?",
        (resource_id, screen_name, bot_id))
    return True


def _active_bots() -> list:
    return db.query(
        "SELECT * FROM ad_bots WHERE status='active' "
        "ORDER BY COALESCE(last_post, added_at) ASC")


def pick_bot() -> dict | None:
    """Выбирает бота, который дольше всех не постил."""
    bots = _active_bots()
    return bots[0] if bots else None


# ============================================================
# ЦЕЛИ (ПАБЛИКИ ДЛЯ РЕКЛАМЫ)
# ============================================================

def add_target(owner_id: int, screen_name: str = None,
               title: str = None, method: str = "wall") -> dict:
    owner_id = int(owner_id)
    existing = db.query_one("SELECT id FROM ad_targets WHERE owner_id=?",
                            (owner_id,))
    if existing:
        return {"ok": False, "error": "уже добавлен"}
    db.execute(
        "INSERT INTO ad_targets(owner_id, screen_name, title, method, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (owner_id, screen_name, title, method))
    row = db.query_one("SELECT id FROM ad_targets WHERE owner_id=?", (owner_id,))
    log.info("ad_target added: id=%s owner=%s", row["id"], owner_id)
    return {"ok": True, "target_id": row["id"]}


def remove_target(target_id: int) -> bool:
    row = db.query_one("SELECT id FROM ad_targets WHERE id=?", (target_id,))
    if not row:
        return False
    db.execute("DELETE FROM ad_targets WHERE id=?", (target_id,))
    return True


def list_targets() -> list:
    return db.query(
        "SELECT * FROM ad_targets ORDER BY status, id")


def _active_targets() -> list:
    return db.query(
        "SELECT * FROM ad_targets WHERE status='active' "
        "ORDER BY COALESCE(last_post, added_at) ASC")


# ============================================================
# ПОСТИНГ
# ============================================================

def _log_post(bot_id: int, target_id: int, message: str,
              status: str, error: str = None) -> None:
    db.execute(
        "INSERT INTO ad_posts(bot_id, target_id, message, status, error) "
        "VALUES (?, ?, ?, ?, ?)",
        (bot_id, target_id, message, status, error))


def post_to_target(bot_row, target_row, message: str) -> tuple:
    """
    Публикует пост от имени бота в паблик-цель.
    Возвращает (ok, error_msg).
    """
    owner_id = target_row["owner_id"]
    try:
        api = vk_api.VkApi(token=bot_row["token"]).get_api()
    except Exception as e:
        return False, f"auth: {e}"

    method = target_row["method"] or "wall"

    try:
        if method == "wall":
            api.wall.post(
                owner_id=owner_id,
                message=message,
                from_group=0,
            )
        elif method == "suggest":
            api.wall.post(
                owner_id=owner_id,
                message=message,
                from_group=0,
                publish_date=int(time.time()) + 5,
            )
        elif method == "comment":
            # ищем свежий пост в паблике и комментируем
            wall = api.wall.get(owner_id=owner_id, count=1)
            items = wall.get("items", [])
            if not items:
                return False, "нет постов для коммента"
            post_id = items[0]["id"]
            api.wall.createComment(
                owner_id=owner_id,
                post_id=post_id,
                message=message,
            )
        else:
            return False, f"неизвестный метод: {method}"
    except vk_api.exceptions.ApiError as e:
        code = getattr(e, "code", None)
        if code in (15, 200):
            db.execute(
                "UPDATE ad_targets SET fails=fails+1, "
                "status=CASE WHEN fails+1 >= 3 THEN 'banned' ELSE status END "
                "WHERE id=?", (target_row["id"],))
            return False, f"бан в паблике (code {code})"
        if code in (5, 14):
            set_bot_status(bot_row["id"], "dead", f"token invalid code {code}")
            return False, f"токен мёртв (code {code})"
        return False, f"api error {code}: {e}"
    except Exception as e:
        return False, f"unexpected: {e}"

    # успех
    db.execute(
        "UPDATE ad_bots SET last_post=CURRENT_TIMESTAMP, "
        "posts_ok=posts_ok+1 WHERE id=?", (bot_row["id"],))
    db.execute(
        "UPDATE ad_targets SET last_post=CURRENT_TIMESTAMP, fails=0 "
        "WHERE id=?", (target_row["id"],))
    return True, None


def build_ad_message() -> str:
    """Собирает текст рекламы с плейсхолдерами."""
    from config import BOT_LINK
    invite = db.get_config("main_chat_invite", "")
    tpl = db.get_config("ad_message", "").strip()
    if not tpl:
        tpl = (
            "🚀 Хочешь бесплатных подписчиков?\n"
            "Заходи в наш бот взаимного пиара: {bot}\n"
            "Главная беседа: {invite}"
        )
    return (tpl.replace("{bot}", BOT_LINK)
               .replace("{invite}", invite))


def do_one_post() -> dict:
    """Один цикл: выбрать бота + цель, запостить. Возвращает результат."""
    if db.get_config("ad_enabled", "0") != "1":
        return {"ok": False, "reason": "выключено"}

    bot = pick_bot()
    if not bot:
        return {"ok": False, "reason": "нет активных ботов"}

    target = None
    for t in _active_targets():
        target = t
        break
    if not target:
        return {"ok": False, "reason": "нет активных целей"}

    msg = build_ad_message()
    ok, err = post_to_target(bot, target, msg)

    _log_post(bot["id"], target["id"], msg,
              "ok" if ok else "fail", err)

    if not ok:
        log.warning("post fail: bot=%s target=%s err=%s",
                    bot["id"], target["id"], err)
    else:
        log.info("post ok: bot=%s target=%s", bot["id"], target["id"])
    return {"ok": ok, "bot": bot["id"], "target": target["id"], "error": err}


# ============================================================
# РЕЗОЛВ ЦЕЛИ ПО ССЫЛКЕ
# ============================================================

def resolve_target(vk, text: str) -> dict:
    """Резолвит vk.com/xxx в owner_id и название. vk = сообщество-бот."""
    from utils import extract_vk_resource
    parsed = extract_vk_resource(text.strip())
    if not parsed:
        return {"ok": False, "error": "не вижу VK-ссылку"}

    slug = parsed["slug"]
    try:
        # группы
        resp = vk.groups.getById(group_id=slug)
        g = resp[0] if isinstance(resp, list) else resp.get("groups", [{}])[0]
        owner_id = -int(g["id"])
        return {
            "ok": True,
            "owner_id": owner_id,
            "screen_name": g.get("screen_name", ""),
            "title": g.get("name", ""),
        }
    except Exception as e:
        return {"ok": False, "error": f"не нашёл паблик: {e}"}


# ============================================================
# ПОДПИСКИ БОТОВ НА ЦЕЛИ
# ============================================================

def bot_join_target(bot_row, target_row) -> tuple:
    """Бот подписывается на паблик-цель. Возвращает (ok, error)."""
    try:
        api = vk_api.VkApi(token=bot_row["token"]).get_api()
        group_id = abs(target_row["owner_id"])
        api.groups.join(group_id=group_id)
        db.execute(
            "INSERT INTO imitation_log(bot_id, action, target_id, status) "
            "VALUES (?, 'join_target', ?, 'ok')",
            (bot_row["id"], target_row["id"]))
        return True, None
    except vk_api.exceptions.ApiError as e:
        code = getattr(e, "code", None)
        if code in (15, 200):
            db.execute(
                "UPDATE ad_targets SET status='banned', fails=fails+1 "
                "WHERE id=?", (target_row["id"],))
            db.execute(
                "INSERT INTO imitation_log(bot_id, action, target_id, status, details) "
                "VALUES (?, 'join_target', ?, 'banned', ?)",
                (bot_row["id"], target_row["id"], str(e)))
            return False, f"бан в паблике (code {code})"
        if code in (5, 14):
            set_bot_status(bot_row["id"], "dead", f"token invalid code {code}")
            return False, f"токен мёртв (code {code})"
        return False, f"api error: {e}"
    except Exception as e:
        return False, f"unexpected: {e}"


def do_one_subscribe() -> dict:
    """Один цикл подписки: бот подписывается на цель."""
    if db.get_config("imitation_enabled", "0") != "1":
        return {"ok": False, "reason": "имитация выключена"}

    bots = db.query(
        "SELECT * FROM ad_bots WHERE status='active' AND can_subscribe=1 "
        "ORDER BY COALESCE(last_post, added_at) ASC")
    if not bots:
        return {"ok": False, "reason": "нет активных ботов с подписками"}

    targets = db.query(
        "SELECT * FROM ad_targets WHERE status='active'")
    if not targets:
        return {"ok": False, "reason": "нет активных целей"}

    import random as _r
    bot = bots[0]
    target = _r.choice(targets)

    ok, err = bot_join_target(bot, target)
    if not ok:
        log.warning("subscribe fail: bot=%s target=%s err=%s",
                    bot["id"], target["id"], err)
    else:
        log.info("subscribe ok: bot=%s target=%s",
                 bot["id"], target["id"])
        db.execute(
            "UPDATE ad_bots SET last_post=CURRENT_TIMESTAMP WHERE id=?",
            (bot["id"],))
    return {"ok": ok, "bot": bot["id"], "target": target["id"], "error": err}


# ============================================================
# КОНКУРЕНТЫ: ПОСТИНГ
# ============================================================

def direct_post(bot_row, target_row, message: str) -> tuple:
    """Прямой пост в паблик-цель (wall.post)."""
    try:
        api = vk_api.VkApi(token=bot_row["token"]).get_api()
        api.wall.post(
            owner_id=target_row["owner_id"],
            message=message,
            from_group=0,
        )
        db.execute(
            "UPDATE ad_bots SET last_post=CURRENT_TIMESTAMP, "
            "posts_ok=posts_ok+1 WHERE id=?", (bot_row["id"],))
        db.execute(
            "UPDATE ad_targets SET last_post=CURRENT_TIMESTAMP, fails=0 "
            "WHERE id=?", (target_row["id"],))
        db.execute(
            "INSERT INTO imitation_log(bot_id, action, target_id, status) "
            "VALUES (?, 'post', ?, 'ok')",
            (bot_row["id"], target_row["id"]))
        return True, None
    except vk_api.exceptions.ApiError as e:
        code = getattr(e, "code", None)
        if code in (15, 200):
            db.execute(
                "UPDATE ad_targets SET fails=fails+1, "
                "status=CASE WHEN fails+1 >= 3 THEN 'banned' ELSE status END "
                "WHERE id=?", (target_row["id"],))
            db.execute(
                "INSERT INTO imitation_log(bot_id, action, target_id, status, details) "
                "VALUES (?, 'post', ?, 'banned', ?)",
                (bot_row["id"], target_row["id"], str(e)))
            return False, f"бан в паблике (code {code})"
        if code in (5, 14):
            set_bot_status(bot_row["id"], "dead", f"token invalid code {code}")
            return False, f"токен мёртв (code {code})"
        return False, f"api error {code}: {e}"
    except Exception as e:
        return False, f"unexpected: {e}"


def imitation_stats() -> dict:
    """Метрики имитации."""
    total_subs = db.query_one(
        "SELECT COUNT(*) AS c FROM imitation_log WHERE action='join_target'"
    )["c"]
    total_posts = db.query_one(
        "SELECT COUNT(*) AS c FROM imitation_log WHERE action='post' AND status='ok'"
    )["c"]
    total_fails = db.query_one(
        "SELECT COUNT(*) AS c FROM imitation_log WHERE status IN ('fail','banned')"
    )["c"]
    return {"subs": total_subs, "posts": total_posts, "fails": total_fails}


# ============================================================
# ПАРСЕР ТОКЕНА ИЗ URL
# ============================================================

import re as _re

_TOKEN_RE = _re.compile(r"access_token=([a-zA-Z0-9._\-]+)")


def extract_token(raw: str) -> str:
    """
    Принимает:
      - чистый токен vk1.a.xxx
      - URL с access_token=xxx
      - строку "access_token=vk1.a.xxx&expires_in=0..."
    Возвращает чистый токен или пустую строку.
    """
    raw = raw.strip()
    if not raw:
        return ""

    # уже чистый токен
    if raw.startswith("vk1.") or (len(raw) > 50 and " " not in raw and "=" not in raw):
        return raw

    # вытащить из URL
    m = _TOKEN_RE.search(raw)
    if m:
        return m.group(1)
    return ""


# ============================================================
# САМОПРОВЕРКА
# ============================================================

if __name__ == "__main__":
    import db as _db
    _db.init_db()
    print("ad.py OK")
    print("боты:", len(list_bots()))
    print("цели:", len(list_targets()))
