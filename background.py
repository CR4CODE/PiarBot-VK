# background.py — фоновые задачи: автопромо, автоудаление
import logging
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone


def _utcnow() -> datetime:
    """Наивный UTC. Замена deprecated _utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

import db
from config import (
    BOT_LINK, PROMO_DELETE_SECONDS, WARN_DELETE_SECONDS,
)
from utils import send

log = logging.getLogger("background")


# ============================================================
# ОЧЕРЕДЬ ОТЛОЖЕННЫХ УДАЛЕНИЙ
# ============================================================

# Очередь отложенных удалений хранится в БД (таблица pending_deletes)


def schedule_delete(peer_id: int, message_id: int, delay: float) -> None:
    """Запланировать удаление сообщения через `delay` секунд (через БД)."""
    if not message_id:
        return
    deadline = time.time() + delay
    try:
        db.execute(
            "INSERT INTO pending_deletes(peer_id, message_id, delete_at) "
            "VALUES (?, ?, ?)",
            (peer_id, message_id, deadline),
        )
        log.debug("schedule_delete: peer=%s msg=%s delay=%s",
                  peer_id, message_id, delay)
    except Exception as e:
        log.warning("schedule_delete failed: %s", e)


def _process_delete_queue(vk) -> None:
    """Удаляет просроченные сообщения из БД."""
    now = time.time()
    try:
        rows = db.query(
            "SELECT id, peer_id, message_id FROM pending_deletes "
            "WHERE delete_at <= ? LIMIT 100",
            (now,),
        )
    except Exception as e:
        log.warning("delete queue read failed: %s", e)
        return

    if not rows:
        return

    for row in rows:
        msg_id = row["message_id"]
        peer_id = row["peer_id"]
        try:
            vk.messages.delete(message_ids=msg_id, delete_for_all=1)
            log.debug("deleted msg=%s in peer=%s", msg_id, peer_id)
        except Exception as e:
            log.debug("delete failed msg=%s: %s", msg_id, e)
        try:
            db.execute("DELETE FROM pending_deletes WHERE id=?", (row["id"],))
        except Exception as e:
            log.warning("delete pending row failed: %s", e)


# ============================================================
# АВТОПРОМО
# ============================================================

def _promo_due() -> bool:
    """Пора ли отправлять промо?"""
    if db.get_config("promo_enabled", "0") != "1":
        return False

    text = db.get_config("promo_text", "").strip()
    if not text:
        return False

    try:
        interval_min = int(db.get_config("promo_interval", "60"))
    except ValueError:
        interval_min = 60

    last_str = db.get_config("last_promo_sent", "")
    if not last_str:
        return True

    try:
        last_dt = datetime.fromisoformat(last_str)
    except ValueError:
        return True

    return _utcnow() - last_dt >= timedelta(minutes=interval_min)


def _send_promo(vk) -> None:
    """Рассылает промо во все активные беседы."""
    text_tpl = db.get_config("promo_text", "").strip()
    text = text_tpl.replace("{bot}", BOT_LINK)

    chats = db.query("SELECT peer_id FROM chats WHERE is_active=1")
    if not chats:
        log.info("промо: нет активных бесед")
        db.set_config("last_promo_sent", _utcnow().isoformat())
        return

    sent = 0
    failed = 0
    for row in chats:
        peer_id = row["peer_id"]
        try:
            msg_id = send(vk, peer_id, text)
            if msg_id:
                db.execute(
                    "INSERT INTO promo_log(peer_id, message_id) VALUES (?, ?)",
                    (peer_id, msg_id),
                )
                schedule_delete(peer_id, msg_id, PROMO_DELETE_SECONDS)
                sent += 1
                # VK: ~20 сообщений/сек. Держим темп ниже, чтобы не поймать flood.
                time.sleep(0.06)
            else:
                failed += 1
        except Exception as e:
            log.warning("promo failed peer=%s: %s", peer_id, e)
            failed += 1

    db.set_config("last_promo_sent", _utcnow().isoformat())
    log.info("промо: отправлено %s, ошибок %s, интервал до следующей",
             sent, failed)


# ============================================================
# АВТОПОСТ В СООБЩЕСТВО
# ============================================================

def _wall_post_due() -> bool:
    if db.get_config("wall_post_enabled", "0") != "1":
        return False
    try:
        interval_min = int(db.get_config("wall_post_interval", "360"))
    except ValueError:
        interval_min = 360
    last_str = db.get_config("last_wall_post", "")
    if not last_str:
        return True
    try:
        last_dt = datetime.fromisoformat(last_str)
    except ValueError:
        return True
    return _utcnow() - last_dt >= timedelta(minutes=interval_min)


def build_stats_post() -> str:
    from config import BOT_LINK

    total_users = db.query_one("SELECT COUNT(*) AS c FROM users WHERE is_banned=0")["c"]
    total_chats = db.query_one("SELECT COUNT(*) AS c FROM chats WHERE is_active=1")["c"]
    total_pp = db.query_one("SELECT COALESCE(SUM(balance),0) AS s FROM users")["s"]
    total_rounds = db.query_one("SELECT COALESCE(SUM(rounds),0) AS s FROM users")["s"]

    top_balance = db.query(
        "SELECT user_id, first_name, balance FROM users "
        "WHERE is_banned=0 ORDER BY balance DESC LIMIT 3")
    top_rounds = db.query(
        "SELECT user_id, first_name, rounds FROM users "
        "WHERE is_banned=0 ORDER BY rounds DESC LIMIT 3")

    lines = [
        "📊 СТАТИСТИКА PiarBot",
        "━━━━━━━━━━━━━━━━",
        f"👥 Участников: {total_users}",
        f"💬 Бесед: {total_chats}",
        f"🎯 Раундов пройдено: {total_rounds}",
        f"💰 PP в обороте: {total_pp}",
        "",
    ]
    if top_balance:
        lines.append("🏆 Топ по Piar Points:")
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(top_balance):
            name = r["first_name"] or f"id{r['user_id']}"
            lines.append(f"{medals[i]} {name} — {r['balance']} PP")
        lines.append("")
    if top_rounds:
        lines.append("🎯 Топ по раундам:")
        for i, r in enumerate(top_rounds):
            name = r["first_name"] or f"id{r['user_id']}"
            lines.append(f"{i+1}. {name} — {r['rounds']}")
        lines.append("")
    lines.append("🚀 Хочешь в топ? Заходи:")
    lines.append(BOT_LINK)
    return "\n".join(lines)


def post_wall_stats(vk, custom_text=None) -> bool:
    from config import VK_GROUP_ID
    text = custom_text or db.get_config("wall_post_template", "").strip()
    if not text:
        text = build_stats_post()
    else:
        # шаблон может содержать {stats}
        text = text.replace("{stats}", build_stats_post())
    try:
        resp = vk.wall.post(
            owner_id=-int(VK_GROUP_ID),
            message=text,
            from_group=1,
        )
        post_id = resp.get("post_id")
        db.set_config("last_wall_post", _utcnow().isoformat())
        log.info("wall post OK, id=%s", post_id)
        return True
    except Exception as e:
        log.warning("wall.post failed: %s", e)
        return False


# ============================================================
# РАССЫЛКА НОВОСТЕЙ
# ============================================================

def send_news(vk, text: str) -> tuple[int, int]:
    """Рассылка всем юзерам. Возвращает (успех, провал)."""
    users = db.query("SELECT user_id FROM users WHERE is_banned=0")
    ok, fail = 0, 0
    for u in users:
        uid = u["user_id"]
        try:
            msg_id = send(vk, uid, text)
            if msg_id:
                db.execute(
                    "INSERT INTO news_log(user_id, message_id, status) VALUES (?, ?, ?)",
                    (uid, msg_id, "ok"))
                ok += 1
            else:
                db.execute(
                    "INSERT INTO news_log(user_id, message_id, status) VALUES (?, ?, ?)",
                    (uid, None, "fail"))
                fail += 1
            time.sleep(0.05)
        except Exception as e:
            log.warning("news to %s failed: %s", uid, e)
            db.execute(
                "INSERT INTO news_log(user_id, message_id, status) VALUES (?, ?, ?)",
                (uid, None, "fail"))
            fail += 1
    log.info("news: ok=%s fail=%s", ok, fail)
    return ok, fail


# ============================================================
# НАПОМИНАНИЯ «ДАВНО НЕ БЫЛО»
# ============================================================

_reminder_last_check = 0.0


def _reminder_due() -> bool:
    """Проверять раз в час."""
    global _reminder_last_check
    now = time.time()
    if now - _reminder_last_check < 3600:
        return False
    _reminder_last_check = now
    return db.get_config("reminder_enabled", "1") == "1"


def send_reminders(vk) -> tuple[int, int]:
    """Отправляет напоминания юзерам, которые давно не заходили."""
    from config import BOT_LINK

    try:
        days = int(db.get_config("reminder_days", "7"))
        cooldown = int(db.get_config("reminder_cooldown", "14"))
    except ValueError:
        days, cooldown = 7, 14

    cutoff = (_utcnow() - timedelta(days=days)).isoformat(sep=" ")
    cooldown_cutoff = (_utcnow() - timedelta(days=cooldown)).isoformat(sep=" ")

    users = db.query(
        "SELECT user_id, first_name, last_seen FROM users "
        "WHERE is_banned=0 AND last_seen <= ?",
        (cutoff,)
    )

    ok, fail = 0, 0
    for u in users:
        uid = u["user_id"]
        # был ли напомин недавно?
        recent = db.query_one(
            "SELECT 1 FROM reminders_log WHERE user_id=? AND sent_at >= ?",
            (uid, cooldown_cutoff))
        if recent:
            continue

        name = u["first_name"] or "друг"
        text = (
            f"👋 {name}, ты давно не заходил!\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"В PiarBot за это время много всего:\n"
            f"• новые раунды в беседах\n"
            f"• копи Piar Points за подписки\n"
            f"• топ участников обновился\n\n"
            f"🎁 Возвращайся — твои PP ждут.\n"
            f"{BOT_LINK}"
        )
        try:
            msg_id = send(vk, uid, text)
            db.execute(
                "INSERT INTO reminders_log(user_id, message_id) VALUES (?, ?)",
                (uid, msg_id))
            if msg_id:
                ok += 1
            else:
                fail += 1
            time.sleep(0.1)
        except Exception as e:
            log.warning("reminder to %s failed: %s", uid, e)
            fail += 1

    log.info("reminders: ok=%s fail=%s", ok, fail)
    return ok, fail


# ============================================================
# АВТОБЭКАП БД
# ============================================================

_BACKUP_DIR = Path(__file__).parent / "data" / "backups"
_BACKUP_KEEP = 7
_backup_last_check = 0.0


def _backup_due() -> bool:
    global _backup_last_check
    now = time.time()
    if now - _backup_last_check < 3600:
        return False
    _backup_last_check = now
    return True


def backup_db() -> bool:
    """Копирует piar.db (со всеми WAL) в data/backups/, хранит 7 последних."""
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        from db import DB_PATH
        stamp = _utcnow().strftime("%Y%m%d-%H%M")
        target = _BACKUP_DIR / f"piar-{stamp}.db"

        # checkpoint WAL чтобы данные были в основном файле
        try:
            import db as _db
            _db._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _db._conn().commit()
        except Exception as e:
            log.warning("wal checkpoint failed: %s", e)

        # копируем основной файл
        import shutil
        shutil.copy2(DB_PATH, target)

        # чистим старые — оставляем 7 последних
        backups = sorted(_BACKUP_DIR.glob("piar-*.db"))
        while len(backups) > _BACKUP_KEEP:
            old = backups.pop(0)
            try:
                old.unlink()
                log.info("backup: removed old %s", old.name)
            except Exception as e:
                log.warning("backup: remove failed: %s", e)

        log.info("backup: OK -> %s", target.name)
        return True
    except Exception as e:
        log.warning("backup failed: %s", e)
        return False


# ============================================================
# РОТАЦИЯ ЛОГОВ
# ============================================================

_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 МБ
_LOG_KEEP = 3
_log_last_check = 0.0


def _rotate_due() -> bool:
    global _log_last_check
    now = time.time()
    if now - _log_last_check < 600:
        return False
    _log_last_check = now
    return True


def rotate_logs() -> None:
    """Обрезает bot.log при >5 МБ, хранит 3 бэкапа."""
    try:
        logfile = Path(__file__).parent / "logs" / "bot.log"
        if not logfile.exists() or logfile.stat().st_size < _LOG_MAX_BYTES:
            return

        stamp = _utcnow().strftime("%Y%m%d-%H%M")
        rotated = logfile.with_name(f"bot.{stamp}.log")

        # сдвигаем текущий лог
        logfile.rename(rotated)
        logfile.touch()

        # чистим старые — оставляем 3
        olds = sorted(
            (Path(__file__).parent / "logs").glob("bot.*.log"),
            key=lambda x: x.stat().st_mtime,
        )
        while len(olds) > _LOG_KEEP:
            old = olds.pop(0)
            try:
                old.unlink()
                log.info("rotate: removed old %s", old.name)
            except Exception as e:
                log.warning("rotate: remove failed: %s", e)

        log.info("rotate: OK -> %s", rotated.name)
    except Exception as e:
        log.warning("rotate failed: %s", e)


# ============================================================
# ЕЖЕДНЕВНЫЙ ОТЧЁТ ВЛАДЕЛЬЦУ
# ============================================================

_report_last_check = 0.0
_REPORT_INTERVAL = 24 * 3600


def _report_due() -> bool:
    global _report_last_check
    now = time.time()
    last_str = db.get_config("last_owner_report", "")
    if last_str:
        try:
            last = datetime.fromisoformat(last_str).timestamp()
            if now - last < _REPORT_INTERVAL:
                return False
        except ValueError:
            pass
    if now - _report_last_check < 600:
        return False
    _report_last_check = now
    return True


def send_owner_report(vk) -> bool:
    """Отправляет владельцу статистику за сутки."""
    owner_id = db.get_config("owner_id", "0")
    if not owner_id or owner_id == "0":
        return False
    try:
        owner_id = int(owner_id)
    except ValueError:
        return False

    day_ago = (_utcnow() - timedelta(hours=24)).isoformat(sep=" ")
    users_total = db.query_one("SELECT COUNT(*) AS c FROM users")["c"]
    users_new = db.query_one(
        "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?", (day_ago,))["c"]
    users_active = db.query_one(
        "SELECT COUNT(*) AS c FROM users WHERE last_seen >= ?", (day_ago,))["c"]
    chats_total = db.query_one(
        "SELECT COUNT(*) AS c FROM chats WHERE is_active=1")["c"]
    rounds_24h = db.query_one(
        "SELECT COUNT(*) AS c FROM rewards WHERE reason='round' AND created_at >= ?",
        (day_ago,))["c"]
    pp_24h = db.query_one(
        "SELECT COALESCE(SUM(amount),0) AS s FROM rewards "
        "WHERE amount > 0 AND created_at >= ?", (day_ago,))["s"]
    pp_total = db.query_one(
        "SELECT COALESCE(SUM(balance),0) AS s FROM users")["s"]
    refs_24h = db.query_one(
        "SELECT COUNT(*) AS c FROM referrals WHERE created_at >= ?",
        (day_ago,))["c"]

    text = (
        f"📊 Отчёт за сутки\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👥 Юзеров всего: {users_total}\n"
        f"🆕 Новых: {users_new}\n"
        f"🟢 Активных: {users_active}\n"
        f"👥 Рефералов: {refs_24h}\n"
        f"💬 Бесед: {chats_total}\n"
        f"🎯 Раундов: {rounds_24h}\n"
        f"💰 Начислено PP: {pp_24h}\n"
        f"💰 PP в обороте: {pp_total}"
    )
    msg_id = send(vk, owner_id, text)
    if msg_id:
        db.set_config("last_owner_report", _utcnow().isoformat())
        log.info("owner report sent")
        return True
    return False


# ============================================================
# РАССЫЛКА ПО БЕСЕДАМ
# ============================================================

def send_broadcast(vk, text: str) -> tuple[int, int]:
    """Рассылка во все активные беседы. Возвращает (успех, провал)."""
    chats = db.query("SELECT peer_id FROM chats WHERE is_active=1")
    ok, fail = 0, 0
    for c in chats:
        peer_id = c["peer_id"]
        try:
            msg_id = send(vk, peer_id, text)
            if msg_id:
                ok += 1
            else:
                fail += 1
            time.sleep(0.2)
        except Exception as e:
            log.warning("broadcast to %s failed: %s", peer_id, e)
            fail += 1
    log.info("broadcast: ok=%s fail=%s", ok, fail)
    return ok, fail


# ============================================================
# АВТОПОСТИНГ РЕКЛАМЫ (AD BOTS)
# ============================================================

import random as _random
_ad_next_post_at = 0.0


def _ad_pick_next_delay() -> int:
    try:
        lo = int(db.get_config("ad_interval_min", "45"))
        hi = int(db.get_config("ad_interval_max", "90"))
    except ValueError:
        lo, hi = 45, 90
    if lo < 1:
        lo = 1
    if hi < lo:
        hi = lo
    return _random.randint(lo, hi) * 60


def _ad_due() -> bool:
    global _ad_next_post_at
    if db.get_config("ad_enabled", "0") != "1":
        return False
    now = time.time()
    if _ad_next_post_at == 0.0:
        _ad_next_post_at = now + _ad_pick_next_delay()
        return False
    if now >= _ad_next_post_at:
        _ad_next_post_at = now + _ad_pick_next_delay()
        return True
    return False


def _notify_owner(vk, text: str) -> None:
    owner = db.get_config("owner_id", "0")
    if not owner or owner == "0":
        return
    try:
        send(vk, int(owner), text)
    except Exception as e:
        log.warning("notify owner failed: %s", e)


def _check_ad_bots_health(vk) -> None:
    """Раз в 30 минут — проверка живости ботов и уведомления."""
    global _ad_health_last_check
    now = time.time()
    if now - _ad_health_last_check < 1800:
        return
    _ad_health_last_check = now

    import ad
    for b in ad.list_bots():
        if b["status"] != "active":
            continue
        new_status = ad.check_bot(b["id"])
        if new_status == "dead":
            _notify_owner(
                vk,
                f"⚠️ Рекламный бот #{b['id']} ({b['name']}) умер.\n"
                f"Токен не работает. Замени: /ad bot del {b['id']} + /ad bot add TOKEN")


_ad_health_last_check = 0.0


# ============================================================
# ИМИТАЦИЯ: ПОДПИСКИ И ПОСТЫ В КОНКУРЕНТОВ
# ============================================================

_imitation_sub_next = 0.0
_imitation_post_next = 0.0
_imitation_last_reset = ""


def _imitation_pick_delay(lo_min: int, hi_min: int) -> int:
    import random as _r
    if lo_min < 1:
        lo_min = 1
    if hi_min < lo_min:
        hi_min = lo_min
    return _r.randint(lo_min, hi_min) * 60


def _imitation_sub_due() -> bool:
    global _imitation_sub_next
    if db.get_config("imitation_enabled", "0") != "1":
        return False
    now = time.time()
    if _imitation_sub_next == 0.0:
        try:
            interval = int(db.get_config("imitation_subscribe_interval", "180"))
        except ValueError:
            interval = 180
        _imitation_sub_next = now + _imitation_pick_delay(interval, interval * 2)
        return False
    if now >= _imitation_sub_next:
        try:
            interval = int(db.get_config("imitation_subscribe_interval", "180"))
        except ValueError:
            interval = 180
        _imitation_sub_next = now + _imitation_pick_delay(interval, interval * 2)
        return True
    return False


def _imitation_post_due() -> bool:
    global _imitation_post_next
    if db.get_config("imitation_enabled", "0") != "1":
        return False
    now = time.time()
    if _imitation_post_next == 0.0:
        _imitation_post_next = now + _imitation_pick_delay(20, 40)
        return False
    if now >= _imitation_post_next:
        _imitation_post_next = now + _imitation_pick_delay(20, 40)
        return True
    return False


def _reset_daily_counters() -> None:
    global _imitation_last_reset
    from datetime import date as _d
    today = _d.today().isoformat()
    if _imitation_last_reset == today:
        return
    _imitation_last_reset = today
    try:
        db.execute(
            "UPDATE ad_bots SET actions_today=0, last_action_date=? "
            "WHERE COALESCE(last_action_date,'') != ?", (today, today))
        log.info("imitation: счётчики дня сброшены")
    except Exception as e:
        log.warning("imitation reset failed: %s", e)


def _imitation_post_cycle(vk) -> None:
    import ad
    import random as _r
    if db.get_config('ad_enabled', '0') != '1':
        return
    bots = db.query("SELECT * FROM ad_bots WHERE status='active' ORDER BY COALESCE(last_post, added_at) ASC LIMIT 5")
    if not bots:
        return
    targets = db.query("SELECT * FROM ad_targets WHERE status='active'")
    if not targets:
        return
    bot = bots[0]
    target = _r.choice(targets)
    msg = ad.build_ad_message()
    ok, err = ad.direct_post(bot, target, msg)
    db.execute('INSERT INTO ad_posts(bot_id, target_id, message, status, error) VALUES (?, ?, ?, ?, ?)', (bot['id'], target['id'], msg, 'ok' if ok else 'fail', err))
    if not ok:
        log.warning('direct_post fail: bot=%s target=%s err=%s', bot['id'], target['id'], err)


# ============================================================
# ГЛАВНЫЙ ЦИКЛ
# ============================================================

def _loop(vk) -> None:
    log.info("🔁 фон-цикл запущен")
    tick = 0
    while True:
        try:
            # каждые 5 сек — чистим очередь удалений
            _process_delete_queue(vk)

            # раз в 30 сек — проверяем промо
            if tick % 6 == 0 and _promo_due():
                _send_promo(vk)

            if tick % 6 == 0 and _wall_post_due():
                post_wall_stats(vk)

            # раз в час — напоминания «давно не было»
            if _reminder_due():
                send_reminders(vk)

            # раз в час — бэкап БД
            if _backup_due():
                backup_db()

            # раз в 10 мин — ротация логов
            if _rotate_due():
                rotate_logs()

            # раз в сутки — отчёт владельцу
            if _report_due():
                send_owner_report(vk)

            # автопостинг рекламы (случайный интервал)
            if _ad_due():
                try:
                    import ad
                    res = ad.do_one_post()
                    if not res.get("ok"):
                        log.info("ad post skipped: %s",
                                 res.get("reason") or res.get("error"))
                except Exception as e:
                    log.exception("ad post error: %s", e)

            # раз в 30 минут — проверка здоровья ботов
            _check_ad_bots_health(vk)

            # имитация: подписки ботов на цели
            if _imitation_sub_due():
                try:
                    import ad
                    res = ad.do_one_subscribe()
                    if not res.get("ok"):
                        log.info("subscribe skipped: %s",
                                 res.get("reason") or res.get("error"))
                except Exception as e:
                    log.exception("subscribe error: %s", e)

            # имитация: посты в конкурентов
            if _imitation_post_due():
                try:
                    import ad
                    _imitation_post_cycle(vk)
                except Exception as e:
                    log.exception("imitation post error: %s", e)

            _reset_daily_counters()

            tick += 1
            time.sleep(5)
        except Exception as e:
            log.exception("ошибка в фон-цикле: %s", e)
            time.sleep(10)


def start(vk) -> threading.Thread:
    """Запускает фон-цикл в отдельном потоке-демоне."""
    t = threading.Thread(target=_loop, args=(vk,), daemon=True,
                         name="background")
    t.start()
    return t


# ============================================================
# САМОПРОВЕРКА
# ============================================================

if __name__ == "__main__":
    print("=== background.py OK ===")
    print("экспорт: start(vk), schedule_delete(peer_id, msg_id, delay)")
    # тест очереди
    schedule_delete(1, 100, 0.1)
    cnt = db.query("SELECT COUNT(*) AS c FROM pending_deletes")
    print("в очереди:", cnt[0]["c"] if cnt else 0)
    time.sleep(0.2)
    # эмуляция без vk
    now = time.time()
    ready = db.query(
        "SELECT COUNT(*) AS c FROM pending_deletes WHERE delete_at <= ?",
        (now,),
    )
    print("готово к удалению:", ready[0]["c"] if ready else 0)
