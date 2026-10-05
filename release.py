# release.py — система постов обновлений
import logging
import time

import db
from config import VK_GROUP_ID, BOT_LINK
from utils import send, is_owner

log = logging.getLogger(__name__)


def _make_post_text(note, for_chat=False):
    body = (note["body"] or "").strip()
    title = (note["title"] or "").strip()
    header = "📢 ОБНОВЛЕНИЕ" if for_chat else "📢 Что нового в PiarBot"
    parts = [header, "━━━━━━━━━━━━━━━━", title, "", body, "", f"🚀 {BOT_LINK}"]
    return "\n".join(parts)



# ============================================================
# CRUD ЧЕРНОВИКОВ
# ============================================================

def _parse_note_args(args):
    """title | body — разделитель | или \n\n. Возвращает (title, body)."""
    if "|" in args:
        t, b = args.split("|", 1)
        return t.strip(), b.strip()
    if "\n" in args:
        lines = args.split("\n", 1)
        return lines[0].strip(), lines[1].strip()
    return args.strip(), ""


def create_note(title, body):
    cur = db.execute(
        "INSERT INTO release_notes(title, body) VALUES (?, ?)",
        (title, body))
    return cur.lastrowid


def list_notes(limit=10):
    return db.query(
        "SELECT * FROM release_notes ORDER BY id DESC LIMIT ?",
        (limit,))


def get_note(note_id):
    return db.query_one("SELECT * FROM release_notes WHERE id=?", (note_id,))


def delete_note(note_id):
    db.execute("DELETE FROM release_notes WHERE id=?", (note_id,))


# ============================================================
# ПУБЛИКАЦИЯ
# ============================================================

def publish_wall(vk, note_id):
    note = get_note(note_id)
    if not note:
        return None, "нет черновика"
    if note["wall_post_id"]:
        return None, f"уже опубликовано (post_id={note['wall_post_id']})"
    text = _make_post_text(note, for_chat=False)
    try:
        resp = vk.wall.post(
            owner_id=-int(VK_GROUP_ID),
            message=text,
            from_group=1,
        )
        post_id = resp.get("post_id")
        db.execute(
            "UPDATE release_notes SET wall_post_id=?, wall_at=CURRENT_TIMESTAMP WHERE id=?",
            (post_id, note_id))
        log.info("release #%s -> wall post_id=%s", note_id, post_id)
        return post_id, None
    except Exception as e:
        log.warning("release #%s wall failed: %s", note_id, e)
        return None, str(e)


def publish_chats(vk, note_id):
    note = get_note(note_id)
    if not note:
        return None, "нет черновика"
    text = _make_post_text(note, for_chat=True)
    chats = db.query("SELECT peer_id FROM chats WHERE is_active=1")
    ok, fail = 0, 0
    for c in chats:
        pid = c["peer_id"]
        try:
            mid = send(vk, pid, text)
            if mid:
                ok += 1
            else:
                fail += 1
            time.sleep(0.2)
        except Exception as e:
            log.warning("release #%s chat %s failed: %s", note_id, pid, e)
            fail += 1
    db.execute(
        "UPDATE release_notes SET chats_ok=?, chats_fail=?, chats_at=CURRENT_TIMESTAMP WHERE id=?",
        (ok, fail, note_id))
    log.info("release #%s -> chats ok=%s fail=%s", note_id, ok, fail)
    return (ok, fail), None


# ============================================================
# КОМАНДЫ ДЛЯ ВЛАДЕЛЬЦА
# ============================================================

def _fmt_note_line(n):
    flags = []
    if n["wall_post_id"]:
        flags.append("📌 wall")
    if n["chats_at"]:
        flags.append(f"💬 chats {n["chats_ok"]}/{n["chats_ok"] + (n["chats_fail"] or 0)}")
    flag_s = (" [" + ", ".join(flags) + "]") if flags else ""
    return f"#{n["id"]}: {n["title"]}{flag_s}"


def handle_release(vk, event, args):
    peer_id = event.peer_id
    user_id = event.user_id
    if not is_owner(user_id):
        send(vk, peer_id, "⛔ Только владелец.")
        return True

    args = (args or "").strip()
    if not args:
        send(vk, peer_id,
             "📢 СИСТЕМА ОБНОВЛЕНИЙ\n"
             "━━━━━━━━━━━━━━━━\n"
             "/release add Заголовок | Текст поста\n"
             "/release list \u2014 список черновиков\n"
             "/release show N \u2014 показать #N\n"
             "/release del N \u2014 удалить #N\n"
             "/release wall N \u2014 опубликовать #N на стену\n"
             "/release chats N \u2014 опубликовать #N в беседы\n"
             "/release all N \u2014 и на стену, и в беседы")
        return True

    parts = args.split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == "add":
        if len(parts) < 2:
            send(vk, peer_id, "⚠️ Формат: /release add Заголовок | Текст")
            return True
        rest = " ".join(parts[1:])
        title, body = _parse_note_args(rest)
        if not title or not body:
            send(vk, peer_id, "⚠️ Нужны и заголовок, и текст. Разделитель: |")
            return True
        nid = create_note(title, body)
        send(vk, peer_id, f"✅ Черновик #{nid} создан.\n/release show {nid}")
        return True

    if cmd == "list":
        notes = list_notes(10)
        if not notes:
            send(vk, peer_id, "📭 Черновиков нет.")
            return True
        lines = ["📋 Последние черновики:", "━━━━━━━━━━━━━━━━"]
        for n in notes:
            lines.append(_fmt_note_line(n))
        send(vk, peer_id, "\n".join(lines))
        return True

    if cmd in ("show", "del", "wall", "chats", "all"):
        if len(parts) < 2 or not parts[1].isdigit():
            send(vk, peer_id, f"⚠️ Укажи номер: /release {cmd} N")
            return True
        nid = int(parts[1])
        note = get_note(nid)
        if not note:
            send(vk, peer_id, f"❌ Черновик #{nid} не найден.")
            return True

        if cmd == "show":
            text = _make_post_text(note, for_chat=False)
            send(vk, peer_id, f"📄 Черновик #{nid}:\n\n{text}")
            return True

        if cmd == "del":
            delete_note(nid)
            send(vk, peer_id, f"🗑 Черновик #{nid} удалён.")
            return True

        if cmd == "wall":
            post_id, err = publish_wall(vk, nid)
            if err:
                send(vk, peer_id, f"❌ Не удалось: {err}")
            else:
                send(vk, peer_id, f"✅ Опубликовано на стену (post_id={post_id}).")
            return True

        if cmd == "chats":
            res, err = publish_chats(vk, nid)
            if err:
                send(vk, peer_id, f"❌ Не удалось: {err}")
            else:
                ok, fail = res
                send(vk, peer_id, f"✅ В беседы: ok={ok}, fail={fail}.")
            return True

        if cmd == "all":
            post_id, err1 = publish_wall(vk, nid)
            res, err2 = publish_chats(vk, nid)
            lines = [f"📢 Публикация #{nid}:"]
            if err1:
                lines.append(f"❌ wall: {err1}")
            else:
                lines.append(f"✅ wall: post_id={post_id}")
            if err2:
                lines.append(f"❌ chats: {err2}")
            else:
                ok, fail = res
                lines.append(f"✅ chats: ok={ok}, fail={fail}")
            send(vk, peer_id, "\n".join(lines))
            return True

    send(vk, peer_id, "❓ Неизвестная подкоманда. /release \u2014 справка.")
    return True
