# utils.py — VK-специфика: ссылки, peer_id, подписки, отправка
import re
import logging
from typing import Optional
from urllib.parse import urlparse, unquote

log = logging.getLogger(__name__)

def is_owner(user_id: int) -> bool:
    """Проверка: является ли пользователь владельцем бота."""
    import db
    return str(user_id) == db.get_config("owner_id", "0")


def utcnow():
    """Наивный UTC — замена deprecated datetime.utcnow()."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


CHAT_PREFIX = 2_000_000_000


def to_chat_id(peer_id: int) -> int:
    return peer_id - CHAT_PREFIX if peer_id >= CHAT_PREFIX else peer_id


def to_peer_id(chat_id: int) -> int:
    return chat_id + CHAT_PREFIX


def is_chat(peer_id: int) -> bool:
    return peer_id >= CHAT_PREFIX


VK_HOSTS = ("vk.com", "vkontakte.ru", "vk.ru", "m.vk.com")
BARE_HOST_RE = re.compile(
    r"^(?:m\.)?(vk\.com|vkontakte\.ru|vk\.ru)/", re.IGNORECASE
)


def _parse_slug(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        host = urlparse(raw).netloc.lower().removeprefix("www.")
        if host not in VK_HOSTS:
            return None
        path = urlparse(raw).path.lstrip("/").split("?")[0].split("#")[0]
        return unquote(path).strip("/") or None
    if BARE_HOST_RE.match(raw):
        path = re.sub(BARE_HOST_RE, "", raw).split("?")[0].split("#")[0]
        return unquote(path).strip("/") or None
    return unquote(raw.lstrip("@")).strip("/") or None


VK_MENTION_RE = re.compile(r"^\[(club|public|id)(\d+)\|[^\]]+\]$")


RESERVED_WORDS = {
    "start", "help", "list", "setchat", "add", "editround", "setadmin",
    "ban", "unban", "daily", "promo", "stats", "wallpost", "news",
    "broadcast", "export", "give", "setowner", "cancel", "settings",
    "setchannel", "setmainchat",
}


def extract_vk_resource(text: str) -> Optional[dict]:
    if not text or not text.strip():
        return None
    # ЗАПРЕТ: слэш-команды не считаем ссылками
    if text.strip().startswith("/"):
        return None
    # ЗАПРЕТ: зарезервированные слова не считаем пабликами
    if text.strip().lower() in RESERVED_WORDS:
        return None
    # VK-упоминание: [club123|Name], [public123|Name], [id123|Name]
    m = VK_MENTION_RE.match(text.strip())
    if m:
        kind, num = m.group(1).lower(), m.group(2)
        if kind == "id":
            return {"slug": f"id{num}", "type_hint": "user"}
        return {"slug": f"{kind}{num}", "type_hint": "group"}
    slug = _parse_slug(text)
    if not slug:
        return None
    low = slug.lower()
    if low.startswith(("club", "public")):
        return {"slug": slug, "type_hint": "group"}
    if low.startswith("id") and slug[2:].isdigit():
        return {"slug": slug, "type_hint": "user"}
    if re.fullmatch(r"[A-Za-z0-9_.]{4,}", slug):
        return {"slug": slug, "type_hint": None}
    return None


def is_vk_host(text: str) -> bool:
    if not text.startswith(("http://", "https://")):
        return False
    host = urlparse(text).netloc.lower().removeprefix("www.")
    return host in VK_HOSTS


def looks_like_any_link(text: str) -> bool:
    return bool(re.search(r"https?://|www\.", text, re.IGNORECASE))


def group_url(screen_name: str) -> str:
    return f"https://vk.com/{screen_name.lstrip('@')}"


def user_url(user_id: int) -> str:
    return f"https://vk.com/id{user_id}"


def mention(user_id: int, name: str = "") -> str:
    """VK-упоминание. Если name не задан — подтягивает из БД."""
    if not name:
        try:
            import db
            u = db.get_user(user_id)
            if u:
                name = (u["first_name"] or "").strip()
        except Exception:
            pass
    return f"[id{user_id}|{name or ('id' + str(user_id))}]"


def round_reward(round_number: int) -> int:
    from config import ROUND_BASE_REWARD, ROUND_GROWTH
    if round_number <= 1:
        return ROUND_BASE_REWARD
    return int(ROUND_BASE_REWARD * (1 + ROUND_GROWTH) ** (round_number - 1))


_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def clean(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def send(vk, peer_id: int, text: str, keyboard: Optional[str] = None,
         attachment: Optional[str] = None) -> Optional[int]:
    from vk_api.utils import get_random_id
    text = clean(text)
    params = {
        "peer_id": peer_id,
        "message": text[:4096],
        "random_id": get_random_id(),
    }
    if keyboard:
        params["keyboard"] = keyboard
    if attachment:
        params["attachment"] = attachment
    try:
        result = vk.messages.send(**params)
        if isinstance(result, dict):
            return result.get("message_id")
        return result
    except Exception as e:
        log.warning("send failed peer=%s: %s", peer_id, e)
        return None


# ---------- отправка документа ----------

def send_doc(vk, peer_id: int, filepath, title: str = "") -> bool:
    """Загружает файл и отправляет как документ VK."""
    import os
    from vk_api.utils import get_random_id
    try:
        upload = vk.docs.getMessagesUploadServer(
            peer_id=peer_id, type="doc")
        url = upload["upload_url"]

        import requests
        with open(filepath, "rb") as f:
            resp = requests.post(url, files={"file": f}).json()

        saved = vk.docs.save(
            file=resp["file"],
            title=title or os.path.basename(str(filepath)),
        )
        doc = saved.get("doc", saved)
        attachment = f"doc{doc['owner_id']}_{doc['id']}"
        vk.messages.send(
            peer_id=peer_id,
            message="📎 Файл готов.",
            attachment=attachment,
            random_id=get_random_id(),
        )
        return True
    except Exception as e:
        log.warning("send_doc failed: %s", e)
        return False


if __name__ == "__main__":
    for t in ["/start", "/list", "vk.com/go_gigi", "go_gigi", "@go_gigi", "start"]:
        print(f"{t!r:25} -> {extract_vk_resource(t)}")
