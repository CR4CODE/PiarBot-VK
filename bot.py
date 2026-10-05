# bot.py — точка входа PiarBot VK
import logging
import sys
import time
import requests
from pathlib import Path

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType

import db
from config import VK_GROUP_TOKEN, VK_GROUP_ID, BOT_LINK
from utils import send, is_chat, to_chat_id
from utils import chat_log
import background
import db_migrations
from handlers import private as h_private
from handlers import chat as h_chat

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_handlers = [logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")]
# StreamHandler только если запущены в TTY (foreground), иначе будут дубли в логе
if sys.stdout.isatty():
    _handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_handlers,
)

_chat_handler = logging.FileHandler(LOG_DIR / "chat.log", encoding="utf-8")
_chat_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_chat_log = logging.getLogger("chat")
_chat_log.setLevel(logging.DEBUG)
_chat_log.addHandler(_chat_handler)
_chat_log.propagate = False
logging.getLogger("vk_api").setLevel(logging.WARNING)
log = logging.getLogger("bot")
log.setLevel(logging.DEBUG)

_USER_CACHE: dict[int, tuple[float, dict]] = {}
_USER_TTL = 600


def get_user_info(vk, user_id: int) -> dict:
    now = time.time()
    cached = _USER_CACHE.get(user_id)
    if cached and now - cached[0] < _USER_TTL:
        return cached[1]
    try:
        resp = vk.users.get(user_ids=user_id, fields="")
        u = resp[0] if resp else {}
        info = {"first_name": u.get("first_name", ""),
                "last_name": u.get("last_name", "")}
    except Exception as e:
        log.warning("users.get failed for %s: %s", user_id, e)
        info = {"first_name": "", "last_name": ""}
    _USER_CACHE[user_id] = (now, info)
    return info


def safe_listen(longpoll, log):
    """LongPoll с авто-переподключением."""
    while True:
        try:
            for event in longpoll.listen():
                yield event
            return
        except requests.exceptions.ReadTimeout:
            log.debug("LongPoll timeout, reconnect")
            time.sleep(1)
            continue
        except requests.exceptions.ConnectionError as e:
            log.warning("Network down: %s, wait 5s", e)
            time.sleep(5)
            continue
        except KeyboardInterrupt:
            return
        except Exception as e:
            log.exception("LongPoll error: %s, wait 3s", e)
            time.sleep(3)
            continue


def main() -> None:
    log.info("🚀 PiarBot VK starting...")
    db.init_db()
    db_migrations.apply()
    log.info("✅ БД готова")

    session = vk_api.VkApi(token=VK_GROUP_TOKEN)
    vk = session.get_api()

    try:
        me = vk.groups.getById(group_id=VK_GROUP_ID)[0]
        log.info("👤 Бот: %s (id=%s)", me["name"], me["id"])
    except Exception as e:
        log.error("groups.getById failed: %s", e)

    background.start(vk)
    log.info("🔁 фоновые задачи запущены")

    longpoll = VkLongPoll(session, group_id=VK_GROUP_ID)
    log.info("👂 LongPoll запущен. ЛС: %s", BOT_LINK)

    for event in safe_listen(longpoll, log):
        try:
            if event.type != VkEventType.MESSAGE_NEW:
                continue
            user_id = event.user_id
            peer_id = event.peer_id
            text = (event.text or "").strip()

            # пропускаем сообщения от сообществ/ботов (id < 0)
            if user_id < 0:
                log.debug("skip non-user sender: %s", user_id)
                continue

            # --- личка ---
            if not is_chat(peer_id):
                if not event.to_me:
                    continue
                chat_log.debug("IN-LS  user=%s text=%s", user_id, text[:500])
                info = get_user_info(vk, user_id)
                h_private.handle(vk, event, info)
                continue

            # --- беседа ---
            # ВАЖНО: для бесед to_me=False для обычных сообщений,
            # поэтому обрабатываем все входящие
            chat_id = to_chat_id(peer_id)
            chat_log.debug("IN-CHAT chat=%s user=%s text=%s", chat_id, user_id, text[:500])
            log.debug("💬 chat=%s user=%s to_me=%s text=%r",
                      chat_id, user_id, event.to_me, text[:80])

            # обновляем имя юзера в БД (для mention)
            info = get_user_info(vk, user_id)
            if info.get("first_name"):
                try:
                    db.execute(
                        "UPDATE users SET first_name=?, last_name=?, "
                        "last_seen=CURRENT_TIMESTAMP WHERE user_id=?",
                        (info["first_name"], info.get("last_name", ""), user_id))
                    # если юзера нет — создаём
                    if not db.get_user(user_id):
                        db.execute(
                            "INSERT INTO users(user_id, first_name, last_name) "
                            "VALUES (?, ?, ?)",
                            (user_id, info["first_name"],
                             info.get("last_name", "")))
                except Exception as e:
                    log.debug("user update in chat failed: %s", e)

            h_chat.handle(vk, event)

        except Exception as e:
            log.exception("Ошибка обработки события: %s", e)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("🛑 Остановлен вручную")
    except Exception as e:
        log.exception("💥 Фатальная ошибка: %s", e)
        sys.exit(1)
