"""VK community-wall publishing using Post Studio's revision and confirmation journal."""

from __future__ import annotations

import re

from kyntrion_tools.bot_menu import POST_STUDIO_EDITOR_ACTIONS
from kyntrion_tools.command_catalog import parse_dot
from kyntrion_tools.post_studio import PostStudioService
from kyntrion_tools.post_studio_render import _keyboard, _length, _media, _text
from kyntrion_tools.vk_api import VkApiError


def render_wall_post(draft, *, preview=False):
    content = draft["content"]
    body = _text(content, "text", "segments")[1]
    caption = _text(content, "caption", "caption_segments")[1]
    media = _media(content)
    refs = []
    for item in media:
        ref = item["media"]
        if not re.fullmatch(item["type"] + r"-?[1-9]\d*_[1-9]\d*(?:_[\w-]+)?", ref, re.ASCII):
            raise ValueError("Нужны фото или видео ВКонтакте")
        refs.append(ref)
    links = []
    keyboard = _keyboard(content)
    if keyboard:
        for row in keyboard["inline_keyboard"]:
            for button in row:
                links.append(f"{button['text']} — {button['url']}")
    message = "\n\n".join(part for part in (body, caption, "\n".join(links)) if part.strip())
    if not message and not refs:
        raise ValueError("Добавьте текст, ссылку или вложение")
    if _length(message) > 16384:
        raise ValueError("Публикация слишком длинная")
    attachment = ",".join(refs)
    if preview:
        chunks = [message[i:i + 2000] for i in range(0, len(message), 2000)] or [""]
        return [{"method": "sendMessage", "text": "Предпросмотр записи ВКонтакте"},
                *({"method": "sendMessage", "text": chunk, "vk_attachment": attachment if i == 0 else ""}
                  for i, chunk in enumerate(chunks))]
    return [{"method": "wall.post", "message": message, "attachments": attachment}]


def _button(text, callback):
    return {"text": text, "callback_data": "post:" + callback}


class VKPostStudio(PostStudioService):
    render = staticmethod(render_wall_post)
    platform = "ВКонтакте"

    def __init__(self, api, store, group_id, owner_id, wall_call=None, clock=None):
        super().__init__(api, store, group_id, clock)
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("Для студии нужен ID владельца ВКонтакте")
        self.owner_id = owner_id
        self.group_id = group_id
        self.wall_call = wall_call
        self.store.connect_channel(owner_id, -group_id, f"ВКонтакте · club{group_id}")

    def check_channel(self, owner_id, chat_id, action):
        return (owner_id == self.owner_id and chat_id == -self.group_id
                and action in ("post", "edit", "delete"))

    def connect_channel(self, owner_id, channel_ref):
        return owner_id == self.owner_id and str(channel_ref) == str(-self.group_id)

    def _menu(self, owner_id, page=0):
        drafts = [draft for draft in self.store.list_drafts(owner_id)
                  if draft["channel_id"] == -self.group_id and draft["status"] == "draft"]
        page_size = 4
        page = max(0, min(page, (len(drafts) - 1) // page_size))
        rows = [[_button("Создать запись", "new")],
                [_button("Публикации", "posts"), _button("Очередь", "jobs")],
                [_button("Статистика", "stats"), _button("Часовой пояс", "timezone")]]
        buttons = [_button(f"Черновик #{draft['id']}", "resume:" + self._control(owner_id, draft, "resume"))
                   for draft in drafts[page * page_size:(page + 1) * page_size]]
        rows.extend(buttons[i:i + 2] for i in range(0, len(buttons), 2))
        nav = []
        if page > 0:
            nav.append(_button("Назад", f"page:{page - 1}"))
        if (page + 1) * page_size < len(drafts):
            nav.append(_button("Дальше", f"page:{page + 1}"))
        if nav:
            rows.append(nav)
        text = f"Студия постов · vk.com/club{self.group_id}"
        if self.wall_call is None:
            text += "\nМожно готовить черновики. Для публикации настройте VK_WALL_TOKEN."
        self._private_message(owner_id, text, reply_markup={"inline_keyboard": rows})

    def _editor(self, owner_id, draft):
        buttons = [_button("🔗 Ссылки" if kind == "button" else title,
                           kind + ":" + self._control(owner_id, draft, kind))
                   for title, kind in POST_STUDIO_EDITOR_ACTIONS]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        self.store.set_session(owner_id, draft["id"], "editing")
        self._private_message(owner_id, f"Черновик #{draft['id']} · версия {draft['version']}\n"
                              f"Стена vk.com/club{self.group_id}. Ссылки войдут в текст записи.",
                              reply_markup={"inline_keyboard": rows})

    def _handle_callback(self, callback, owner_id):
        data = callback.get("data", "")
        if not isinstance(data, str) or not isinstance(callback.get("id"), str):
            return False
        if data in ("post:new", "post:connect") or re.fullmatch(r"post:page:\d{1,3}", data):
            self.api.send_action({"method": "answerCallbackQuery", "callback_query_id": callback["id"]})
            if data == "post:new":
                self.store.connect_channel(owner_id, -self.group_id, f"ВКонтакте · club{self.group_id}")
                try:
                    draft_id = self.store.create_draft(owner_id, -self.group_id)
                except ValueError:
                    self._private_message(owner_id, "Достигнут лимит черновиков. Откройте существующий.")
                else:
                    self._editor(owner_id, self.store.get_draft(owner_id, draft_id))
            else:
                self._menu(owner_id, int(data.split(":")[-1]) if "page:" in data else 0)
            return True
        if self.wall_call is None and data.startswith(("post:publish:", "post:schedule:", "post:confirm:")):
            self.api.send_action({"method": "answerCallbackQuery", "callback_query_id": callback["id"]})
            self._private_message(owner_id, "Для отправки на стену настройте VK_WALL_TOKEN владельца.")
            return True
        return super()._handle_callback(callback, owner_id)

    def handle_update(self, update):
        callback = update.get("callback_query") or {}
        message = callback.get("message") or update.get("message") or {}
        actor = (callback.get("from") or message.get("from") or {}).get("id")
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") != actor or message.get("forward_origin"):
            return False
        if actor != self.owner_id:
            text = message.get("text") or ""
            if ((callback.get("data") or "").startswith("post:") or text == "🎨 Студия постов"
                    or text == "/post" or text.startswith("/post ")):
                if callback.get("id"):
                    self.api.send_action({"method": "answerCallbackQuery", "callback_query_id": callback["id"]})
                self._private_message(actor, "Студия стены сообщества доступна владельцу.")
                return True
            return False
        # A VK attachment carries its caption in the message text. The
        # normalizer preserves album order and puts the text on the first item.
        text = message.get("text")
        if (not callback and (message.get("photo") or message.get("video"))
                and isinstance(text, str) and not text.lstrip().startswith("/")
                and parse_dot(text) is None):
            session = self.store.get_session(actor)
            if session and session["step"] in ("editing", "media"):
                return self._capture_media(actor, {**message, "caption": text})
        return super().handle_update(update)

    def request_publish(self, owner_id, draft_id):
        self._require_wall()
        return super().request_publish(owner_id, draft_id)

    def request_schedule(self, owner_id, draft_id, local_time, zone_name=None):
        self._require_wall()
        return super().request_schedule(owner_id, draft_id, local_time, zone_name)

    def _require_wall(self):
        if self.wall_call is None:
            raise ValueError("Настройте VK_WALL_TOKEN владельца для работы со стеной")

    def send_public_action(self, action):
        self._require_wall()
        if action.get("chat_id") != -self.group_id:
            raise ValueError("Неверная стена сообщества")
        method = action["method"]
        params = {"owner_id": -self.group_id}
        if method == "wall.post":
            params["from_group"] = 1
        elif method in ("wall.edit", "wall.delete"):
            params["post_id"] = action["message_id"]
        else:
            raise ValueError("Неизвестное действие со стеной")
        if method != "wall.delete":
            params.update(message=action["message"], attachments=action["attachments"])
        result = self.wall_call(method, **params)
        # PiarBot uses 5.92, where edit and delete both acknowledge with 1.
        # A 5.199 owner session returns the edited post ID explicitly.
        if method in ("wall.edit", "wall.delete") and type(result) is int and result == 1:
            return {"message_id": params["post_id"]}
        post_id = result.get("post_id") if isinstance(result, dict) else None
        if (method == "wall.delete" or type(post_id) is not int or post_id <= 0
                or (method == "wall.edit" and post_id != params["post_id"])):
            raise ValueError("ВКонтакте не подтвердил ID записи")
        return {"message_id": post_id}

    @staticmethod
    def is_api_rejection(exc):
        return isinstance(exc, VkApiError) and bool(re.fullmatch(
            r"VK wall\.(?:post|edit|delete): (?:5|7|15|27|100|214|222)", str(exc)))

    def _notify_publish(self, owner_id, state):
        if state == "failed":
            self._private_message(owner_id, "ВК отклонил публикацию. Проверьте права на стену. "
                                  "Если нужен ключ владельца с правом wall, на Realme есть setup_wall.py.")
            return
        super()._notify_publish(owner_id, state)

    def _manage_actions(self, publication, kind, payload):
        self._require_wall()
        owner_id = publication["owner_id"]
        if (not self.check_channel(owner_id, publication["channel_id"], "edit")
                or len(publication["message_ids"]) != 1):
            raise ValueError("Нет подтверждённой записи на этой стене")
        target = {"chat_id": publication["channel_id"], "message_id": publication["message_ids"][0]}
        if kind == "delete":
            return [{"method": "wall.delete", **target}]
        draft = self.store.get_draft(owner_id, publication["draft_id"])
        if draft is None or draft["version"] != publication["draft_version"]:
            raise ValueError("Версия записи недоступна")
        latest = self.store.latest_edit_receipt(owner_id, publication["id"]) or {}
        content = (latest.get("content") or draft["content"]).copy()
        if kind == "edit_text":
            content.update(text=payload, segments=None)
        elif kind == "edit_caption":
            content.update(caption=payload, caption_segments=None)
        elif kind == "edit_buttons":
            content["buttons"] = payload
        else:
            raise ValueError("Неизвестное изменение записи")
        action, = self.render({"content": content})
        return [{**action, **target, "method": "wall.edit", "content": content}]

    @staticmethod
    def management_receipt(actions):
        return {"content": actions[0]["content"]} if "content" in actions[0] else {"action_count": 1}
