"""Translate trusted VK Bots Long Poll envelopes into the bot's shared input format."""

import json
import re
from typing import Protocol


class EventApi(Protocol):
    def register_callback(self, event_id: str, user_id: int, peer_id: int) -> str: ...
    def register_media(self, file_id: str, url: str, size: int | None = None) -> None: ...
    def show_keyboard_page(self, event_id: str, user_id: int, peer_id: int,
                           cmid: int, page: object) -> None: ...


def _positive(value):
    return type(value) is int and value > 0


def _payload(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    return value if isinstance(value, dict) else {}


def _chat(peer, actor):
    if not _positive(peer) or not _positive(actor):
        return None
    if peer > 2_000_000_000:
        return {"id": peer, "type": "group"}
    if peer == actor and peer < 2_000_000_000:
        return {"id": peer, "type": "private"}
    return None


def _sender(actor):
    # VK's message object contains an ID, not a profile name.
    name = f"id{actor}" if actor > 0 else f"club{-actor}"
    return {"id": actor, "first_name": name}


def _attachment(attachment, api):
    if not isinstance(attachment, dict):
        return None
    kind = attachment.get("type")
    if kind not in ("doc", "audio_message", "photo", "video", "audio"):
        return None
    media = attachment.get(kind)
    if not isinstance(media, dict):
        return None
    owner, number = media.get("owner_id"), media.get("id")
    if type(owner) is not int or owner == 0 or not _positive(number):
        return None
    fid = f"{kind}{owner}_{number}"
    access = media.get("access_key")
    if isinstance(access, str) and re.fullmatch(r"[A-Za-z0-9_-]+", access):
        fid += "_" + access
    info = {"file_id": fid}
    size = media.get("size")
    if type(size) is int and size >= 0:
        info["file_size"] = size
    duration = media.get("duration")
    if type(duration) is int and duration >= 0:
        info["duration"] = duration
    url = media.get("url")
    field = kind
    if kind == "doc":
        field = "document"
        title = media.get("title")
        if isinstance(title, str) and title:
            info["file_name"] = title
    elif kind == "audio_message":
        field = "voice"
        url = media.get("link_ogg")
        transcript = media.get("transcript")
        if isinstance(transcript, str) and transcript.strip() and len(transcript) <= 20000:
            info["transcript"] = transcript.strip()
    elif kind == "photo":
        sizes = media.get("sizes")
        choices = [s for s in sizes if isinstance(s, dict)
                   and isinstance(s.get("url"), str) and _positive(s.get("width"))
                   and _positive(s.get("height"))] if isinstance(sizes, list) else []
        if choices:
            large = max(choices, key=lambda s: s["width"] * s["height"])
            url = large["url"]
            info.update(width=large["width"], height=large["height"])
    if isinstance(url, str) and url:
        api.register_media(fid, url, info.get("file_size"))
    return {field: [info] if field == "photo" else info}


def _parts(raw, api):
    attachments = raw.get("attachments")
    pieces = []
    if isinstance(attachments, list):
        for attachment in attachments:
            piece = _attachment(attachment, api)
            if piece:
                pieces.append(piece)
    if not pieces:
        pieces.append({})
    body = raw.get("text")
    if isinstance(body, str) and body:
        pieces[0]["text"] = body
    return pieces


def _callback(raw, api):
    user, peer = raw.get("user_id"), raw.get("peer_id")
    chat = _chat(peer, user)
    cmid, event_id = raw.get("conversation_message_id"), raw.get("event_id")
    if not chat or not _positive(cmid) or not isinstance(event_id, str) or not event_id:
        return []
    payload = _payload(raw.get("payload"))
    if "kyn_page" in payload:
        api.show_keyboard_page(event_id, user, peer, cmid, payload["kyn_page"])
        return []
    data = payload.get("cmd")
    if not isinstance(data, str) or not data:
        return []
    return [{"callback_query": {
        "id": api.register_callback(event_id, user, peer), "from": _sender(user),
        "message": {"chat": chat, "message_id": cmid}, "data": data,
    }}]


def normalize_events(event: dict, group_id: int, api: EventApi) -> list[dict]:
    """CMIDs are local to a peer. Never substitute a global ID or a forwarded actor."""
    if (not _positive(group_id) or not isinstance(event, dict)
            or type(event.get("group_id")) is not int or event["group_id"] != group_id):
        return []
    obj = event.get("object")
    if not isinstance(obj, dict):
        return []
    if event.get("type") == "message_event":
        return _callback(obj, api)
    raw = obj.get("message")
    if (event.get("type") != "message_new" or not isinstance(raw, dict)
            or raw.get("out") or raw.get("action")):
        return []
    actor, peer, cmid = raw.get("from_id"), raw.get("peer_id"), raw.get("conversation_message_id")
    chat = _chat(peer, actor)
    if not chat or not _positive(cmid):
        return []
    base = {"chat": chat, "from": _sender(actor), "message_id": cmid}
    if type(raw.get("date")) is int:
        base["date"] = raw["date"]
    reply = raw.get("reply_message")
    if isinstance(reply, dict):
        if reply.get("peer_id", peer) != peer:
            return []
        selected = {"chat": chat, **_parts(reply, api)[0]}
        if type(reply.get("from_id")) is int:
            selected["from"] = _sender(reply["from_id"])
        if _positive(reply.get("conversation_message_id")):
            selected["message_id"] = reply["conversation_message_id"]
        base["reply_to_message"] = selected

    parts = _parts(raw, api)
    if chat["type"] == "group" and "text" in parts[0]:
        parts[0]["text"] = re.sub(
            rf"^\[club{group_id}\|[^\]\r\n]+\][,:]?\s*", "", parts[0]["text"],
        )
    forwards = raw.get("fwd_messages")
    if not isinstance(forwards, list):
        forwards = []
    if not forwards and _payload(raw.get("payload")).get("command") == "start":
        ref = raw.get("ref")
        parts[0]["text"] = "/start " + ref if (
            isinstance(ref, str) and re.fullmatch(r"[\w-]{1,64}", ref, re.ASCII)
        ) else "/start"

    # The sender's comment is a separate message from the forwarded content.
    messages = [{**base, **part} for part in parts if part or not forwards]
    # Group forwards have no executable commands. The scoped router expects
    # only the actual speaker's command, optionally referring to a reply.
    stack = list(reversed(forwards)) if chat["type"] == "private" else []
    while stack:
        forwarded = stack.pop()
        if not isinstance(forwarded, dict):
            continue
        for piece in _parts(forwarded, api):
            if piece:
                messages.append({**base, **piece, "forward_origin": {"type": "hidden_user"}})
        children = forwarded.get("fwd_messages")
        if isinstance(children, list):
            stack.extend(reversed(children))
    if len(messages) > 1:
        for index, message in enumerate(messages):
            message["_vk_part"] = index
    return [{"message": message} for message in messages]
