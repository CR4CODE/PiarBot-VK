"""VK community transport for the existing KYNTRION command and addon interfaces."""

from __future__ import annotations

from collections import OrderedDict
from http.client import HTTPException
from html import unescape
import json
import re
import secrets
from threading import Lock
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from kyntrion_tools.telegram_api import TelegramApiError


class VkApiError(TelegramApiError):
    """A sanitized error safe to log without tokens, attachment URLs or message text."""


_DOMAINS = ("vk.com", "vk.ru", "userapi.com", "vkuseraudio.net", "vkuseraudio.com",
            "vkuser.net", "vk-cdn.net", "vkusercdn.ru", "vkvideo.ru")
_MAX_FILE = 20 * 1024 * 1024
_ATTACHMENT = re.compile(r"(?:photo|video|doc|audio|audio_message)-?\d+_\d+(?:_[A-Za-z0-9_-]+)?")


def _trusted_url(url):
    if not isinstance(url, str) or not url.isascii() or re.search(r"[\\\x00-\x20\x7f]", url):
        raise VkApiError("Недопустимый URL ВК")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme == "https" and parts.port in (None, 443) and not parts.netloc.count("@")
                and any(host == d or host.endswith("." + d) for d in _DOMAINS)):
            return url
    except ValueError:
        pass
    raise VkApiError("Недопустимый адрес сервера ВК")


class _VkRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # POSTs carry access tokens or private files. Downloads can redirect to a VK CDN.
        if req.get_method() != "GET" or not getattr(req, "_vk_media_download", False):
            raise VkApiError("Перенаправление запроса ВК отклонено")
        redirect = super().redirect_request(req, fp, code, msg, headers, _trusted_url(newurl))
        if redirect is not None:
            redirect._vk_media_download = True
        return redirect


def _json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _keyboard_pages(markup):
    inline = "inline_keyboard" in markup
    rows = []
    for row in markup.get("inline_keyboard" if inline else "keyboard", []):
        converted = []
        for button in row:
            label = button["text"]
            action = {"type": "text", "label": label[:40]}
            if "callback_data" in button:
                action.update(type="callback", payload=_json({"kyntrion": 1, "cmd": button["callback_data"]}))
            elif "url" in button:
                action.update(type="open_link", link=button["url"])
            else:
                action["payload"] = _json({"kyntrion": 1, "label": label})
            converted.append({"action": action})
        rows.extend(converted[n:n + 5] for n in range(0, len(converted), 5))
    if not inline:
        return [{"one_time": bool(markup.get("one_time_keyboard")), "buttons": rows}]
    if len(rows) <= 6 and sum(map(len, rows)) <= 10:
        return [{"inline": True, "one_time": False, "buttons": rows}]
    # Reserve a row and two controls per page. A page switch edits the same message.
    pages, buttons, count = [], [], 0
    for row in rows:
        if len(buttons) >= 5 or count + len(row) > 8:
            pages.append({"inline": True, "one_time": False, "buttons": buttons})
            buttons, count = [], 0
        buttons.append(row)
        count += len(row)
    pages.append({"inline": True, "one_time": False, "buttons": buttons})
    for index, page in enumerate(pages):
        nav = []
        for target, label in ((index - 1, "Назад"), (index + 1, "Далее")):
            if 0 <= target < len(pages):
                nav.append({"action": {"type": "callback", "label": label,
                                       "payload": _json({"kyntrion": 1, "kyn_page": target})}})
        page["buttons"].append(nav)
    return pages


class VkApi:
    version = "5.199"
    platform = "vk"

    def __init__(self, token: str, group_id: int, *, transcriber=None, opener=None):
        if not token or type(group_id) is not int or group_id <= 0:
            raise ValueError("Нужны ключ сообщества ВК и положительный VK_GROUP_ID")
        self.group_id = group_id
        self._token = token
        self._transcriber = transcriber
        self._open = opener or build_opener(_VkRedirects()).open
        self._server = self._key = self.ts = None
        self._media = OrderedDict()
        self._menus = OrderedDict()
        self.text_buttons = False
        self._button_origins = OrderedDict()
        self._lock = Lock()

    def _read(self, request, *, timeout=45, limit=8 * 1024 * 1024):
        try:
            with self._open(request, timeout=timeout) as response:
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise VkApiError("Ответ ВК превышает лимит размера")
                return body
        except HTTPError as error:
            raise VkApiError(f"VK HTTP {error.code}") from None
        except (OSError, HTTPException, ValueError):
            raise VkApiError("Ошибка соединения с ВК") from None

    def _read_json(self, req, **kwargs):
        try:
            data = json.loads(self._read(req, **kwargs))
        except (ValueError, RecursionError):
            raise VkApiError("Некорректный JSON ВК") from None
        if not isinstance(data, dict):
            raise VkApiError("Некорректный ответ ВК")
        return data

    def call(self, method, **params):
        if not re.fullmatch(r"[a-z]+\.[A-Za-z]+", method):
            raise ValueError("Недопустимый метод ВК")
        body = urlencode({**params, "access_token": self._token, "v": self.version}).encode()
        data = self._read_json(Request("https://api.vk.com/method/" + method, data=body))
        if "response" not in data or "error" in data:
            error = data.get("error")
            code = error.get("error_code") if isinstance(error, dict) else None
            detail = str(code) if type(code) is int else "?"
            if code == 6:
                detail += " (429)"
            raise VkApiError(f"VK {method}: {detail}")
        return data["response"]

    def connect(self, *, resume_ts=None):
        if resume_ts is not None and not str(resume_ts).isdigit():
            raise ValueError("Недопустимая метка Bots Long Poll")
        endpoint = self.call("groups.getLongPollServer", group_id=self.group_id)
        if not isinstance(endpoint, dict) or not endpoint.get("key") or not str(endpoint.get("ts")).isdigit():
            raise VkApiError("Некорректный ответ Bots Long Poll")
        self._server = _trusted_url(endpoint.get("server"))
        self._key = endpoint["key"]
        self.ts = str(endpoint["ts"] if resume_ts is None else resume_ts)

    def poll(self, wait=25):
        if type(wait) is not int or not 0 <= wait <= 25:
            raise ValueError("Недопустимое время ожидания Bots Long Poll")
        if self._server is None:
            self.connect(resume_ts=self.ts)
        query = urlencode({"act": "a_check", "key": self._key, "ts": self.ts, "wait": wait})
        separator = "&" if "?" in self._server else "?"
        data = self._read_json(Request(self._server + separator + query), timeout=35)
        failure = data.get("failed")
        if failure in (2, 3):
            self.connect(resume_ts=self.ts if failure == 2 else None)
            return []
        if not str(data.get("ts")).isdigit() or failure not in (None, 1):
            raise VkApiError("Некорректный ответ Bots Long Poll")
        if failure == 1:
            self.ts = str(data["ts"])
            return []
        if not isinstance(data.get("updates"), list):
            raise VkApiError("Отсутствуют события Bots Long Poll")
        self.ts = str(data["ts"])
        return data["updates"]

    @staticmethod
    def register_callback(event_id, user_id, peer_id):
        return _json([event_id, user_id, peer_id])

    def _answer(self, query_id, text=None):
        event, user, peer = json.loads(query_id)
        if self.text_buttons:
            if text:
                self.send_message(peer, text)
            return True
        params = {"event_id": event, "user_id": user, "peer_id": peer}
        if text:
            params["event_data"] = _json({"type": "show_snackbar", "text": text[:90]})
        return self.call("messages.sendMessageEventAnswer", **params)

    def _remember(self, store, key, value, limit):
        with self._lock:
            store[key] = value
            store.move_to_end(key)
            if len(store) > limit:
                store.popitem(last=False)

    def register_media(self, file_id, url, size=None):
        if url:
            self._remember(self._media, file_id, (_trusted_url(url), size), 4096)

    def get_file(self, file_id):
        item = self._media.get(file_id)
        if item is None:
            raise VkApiError("Ссылка на вложение устарела. Отправьте файл повторно.")
        return {"file_path": item[0], "file_size": item[1] or 0}

    def download_file(self, file_path, *, max_bytes=_MAX_FILE):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("Недопустимый лимит файла")
        request = Request(_trusted_url(file_path))
        request._vk_media_download = True
        return self._read(request, limit=min(max_bytes, _MAX_FILE))

    def transcribe_audio(self, audio):
        if self._transcriber is None:
            raise VkApiError("Распознавание речи пока не настроено")
        return self._transcriber(audio)

    def _send(self, peer, text, *, attachment=None, reply=None, markup=None, edit=None):
        params = {"message": text}
        if attachment:
            refs = attachment.split(",")
            if len(refs) > 10 or not all(_ATTACHMENT.fullmatch(ref) for ref in refs):
                raise VkApiError("Некорректное вложение ВК")
            params["attachment"] = attachment
        if reply:
            params["forward"] = _json({"is_reply": True, "peer_id": peer,
                                       "conversation_message_ids": [reply]})
        pages = _keyboard_pages(markup) if markup is not None else []
        origin = None
        if self.text_buttons and pages:
            origin = secrets.token_urlsafe(12)
            for page in pages:
                for row in page["buttons"]:
                    for button in row:
                        action = button["action"]
                        if action["type"] == "callback":
                            action["type"] = "text"
                            action["payload"] = _json({**json.loads(action["payload"]), "origin": origin})
        if pages:
            params["keyboard"] = _json(pages[0])
        if edit is not None:
            if self.call("messages.edit", peer_id=peer, cmid=edit, **params) != 1:
                raise VkApiError("ВК не подтвердил изменение сообщения")
            cmid = edit
        else:
            # peer_ids (even for one recipient) returns a conversation_message_id.
            result = self.call("messages.send", peer_ids=peer,
                               random_id=1 + secrets.randbelow(2**31 - 1), **params)
            item = result[0] if isinstance(result, list) and len(result) == 1 else None
            if (not isinstance(item, dict) or item.get("peer_id") != peer or item.get("error")
                    or type(item.get("conversation_message_id")) is not int
                    or item["conversation_message_id"] <= 0):
                raise VkApiError("ВК не подтвердил отправку сообщения")
            cmid = item["conversation_message_id"]
        if origin:
            self._remember(self._button_origins, (peer, origin), cmid, 4096)
        if len(pages) > 1:
            self._remember(self._menus, (peer, cmid), (params, pages), 256)
        else:
            with self._lock:
                self._menus.pop((peer, cmid), None)
        return {"message_id": cmid}

    def button_origin(self, peer, token):
        return self._button_origins.get((peer, token)) if isinstance(token, str) else None

    def show_keyboard_page(self, event_id, user_id, peer_id, cmid, page):
        menu = self._menus.get((peer_id, cmid))
        query = self.register_callback(event_id, user_id, peer_id)
        if user_id != peer_id or not menu or type(page) is not int or not 0 <= page < len(menu[1]):
            self._answer(query, "Откройте меню заново.")
            return
        params = {k: v for k, v in menu[0].items() if k != "forward"}
        params["keyboard"] = _json(menu[1][page])
        self._answer(query)
        self.call("messages.edit", peer_id=peer_id, cmid=cmid, keep_forward_messages=1, **params)

    def send_message(self, chat_id, text, reply_to_message_id=None, *, reply_markup=None):
        return self._send(chat_id, text, reply=reply_to_message_id, markup=reply_markup)

    def delete_message(self, chat_id, message_id):
        result = self.call("messages.delete", peer_id=chat_id, cmids=message_id, delete_for_all=1)
        return isinstance(result, list) and any(
            isinstance(item, dict) and item.get("peer_id") == chat_id
            and item.get("conversation_message_id") == message_id and item.get("response") == 1
            for item in result)

    def send_action(self, action):
        method = action["method"]
        if method == "answerCallbackQuery":
            return self._answer(action["callback_query_id"], action.get("text"))
        chat = action["chat_id"]
        markup = action.get("reply_markup")
        reply = (action.get("reply_parameters") or {}).get("message_id")
        if method == "sendMessage":
            text = action["text"]
            if action.get("parse_mode") == "HTML":
                text = unescape(re.sub(r"<[^>]+>", "", text))
            return self._send(chat, text, attachment=action.get("vk_attachment"),
                              reply=reply, markup=markup)
        if method == "editMessageText":
            return self._send(chat, action["text"], markup=markup, edit=action["message_id"])
        if method in ("sendPhoto", "sendVideo"):
            field = "photo" if method == "sendPhoto" else "video"
            return self._send(chat, action.get("caption", ""), attachment=action[field],
                              reply=reply, markup=markup)
        if method == "deleteMessage":
            return self.delete_message(chat, action["message_id"])
        raise VkApiError("Метод доставки не поддерживается в ВК")

    def _upload(self, server, field, filename, contents, mime):
        if (not filename or re.search(r'[\x00-\x1f\x7f"\\/]', filename)
                or re.search(r'[\r\n]', mime)):
            raise VkApiError("Недопустимое имя файла")
        boundary = "KYNTRION" + secrets.token_hex(16)
        header = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                  f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        body = header + contents + f"\r\n--{boundary}--\r\n".encode()
        request = Request(_trusted_url(server), data=body, headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary})
        return self._read_json(request, timeout=100)

    @staticmethod
    def _attachment(kind, saved):
        if (not isinstance(saved, dict) or type(saved.get("owner_id")) is not int
                or type(saved.get("id")) is not int or saved["id"] <= 0):
            raise VkApiError("Некорректный идентификатор загруженного вложения")
        ref = f"{kind}{saved['owner_id']}_{saved['id']}"
        return ref + "_" + saved["access_key"] if saved.get("access_key") else ref

    def send_document(self, chat_id, filename, contents, caption, reply_to_message_id=None,
                      *, mime_type="text/plain; charset=utf-8", reply_markup=None):
        server = self.call("docs.getMessagesUploadServer", peer_id=chat_id, type="doc")
        uploaded = self._upload(server["upload_url"], "file", filename, contents, mime_type)
        if not uploaded.get("file"):
            raise VkApiError("ВК отклонил загрузку файла")
        saved = self.call("docs.save", file=uploaded["file"], title=filename)
        return self._send(chat_id, caption, attachment=self._attachment("doc", saved.get("doc")),
                          reply=reply_to_message_id, markup=reply_markup)

    def send_photo_bytes(self, chat_id, filename, contents, caption):
        server = self.call("photos.getMessagesUploadServer", peer_id=chat_id)
        mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        uploaded = self._upload(server["upload_url"], "photo", filename, contents, mime)
        if not all(k in uploaded for k in ("photo", "server", "hash")):
            raise VkApiError("ВК отклонил загрузку фото")
        saved = self.call("photos.saveMessagesPhoto", **{k: uploaded[k] for k in ("photo", "server", "hash")})
        if not isinstance(saved, list) or not saved:
            raise VkApiError("ВК не сохранил фото")
        return self._send(chat_id, caption, attachment=self._attachment("photo", saved[0]))
