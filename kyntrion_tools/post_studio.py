"""Private-chat controller for the multi-user KYNTRION Post Studio.

Never trust a button's channel/owner claim. Every action derives the Telegram
actor from the update and re-checks current channel administrator privileges.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from html import escape

from kyntrion_tools.bot_menu import POST_STUDIO_EDITOR_ACTIONS, reply_keyboard
from kyntrion_tools.post_studio_render import (_keyboard, _length, _media, render_post,
                                segments_from_telegram)
from kyntrion_tools.post_studio_scheduler import local_to_utc
from kyntrion_tools.post_studio_store import PostStudioStore


_RIGHT = {
    "post": "can_post_messages",
    "edit": "can_edit_messages",
    "delete": "can_delete_messages",
}

_PICKER_ADMIN_RIGHTS = {
    "is_anonymous": False,
    "can_manage_chat": True,
    "can_delete_messages": False,
    "can_manage_video_chats": False,
    "can_restrict_members": False,
    "can_promote_members": False,
    "can_change_info": False,
    "can_invite_users": False,
    "can_post_messages": True,
    "can_edit_messages": False,
}

_TIMEZONE_CHOICES = ("Europe/Moscow", "UTC", "Europe/Berlin",
                     "Europe/Helsinki", "Asia/Almaty")


class PostStudioService:
    render = staticmethod(render_post)
    platform = "Telegram"

    def send_public_action(self, action: dict):
        return self.api.send_action(action)

    @staticmethod
    def is_api_rejection(exc: Exception) -> bool:
        return type(exc).__name__ == "TelegramApiError" and str(exc).startswith(
            ("API HTTP 400", "API HTTP 403", "API: Bad Request", "API: Forbidden"))

    @staticmethod
    def management_receipt(actions: list[dict]) -> dict:
        return {"action_count": len(actions)}

    def __init__(self, api, store: PostStudioStore, bot_id: int, clock=None):
        if type(bot_id) is not int or bot_id <= 0:
            raise ValueError("Post Studio needs the verified bot's Telegram ID")
        self.api = api
        self.store = store
        self.bot_id = bot_id
        self.clock = clock or time.time
        # Opaque UI navigation tokens are ephemeral. A restart invalidates them
        # harmlessly; drafts and scheduler state are persisted separately.
        self._selections: dict[str, tuple[int, int, float]] = {}
        self._controls: dict[str, tuple[int, int, int, str, float]] = {}
        self._chat_requests: dict[int, tuple[int, float]] = {}

    def _request_channel_picker(self, owner_id: int) -> None:
        """Ask the Telegram client to display channels this owner can manage."""
        now = float(self.clock())
        self._chat_requests = {
            owner: value for owner, value in self._chat_requests.items()
            if value[1] > now}
        # A single request per owner, capped globally to limit stale UI state.
        if len(self._chat_requests) >= 512 and owner_id not in self._chat_requests:
            oldest = min(self._chat_requests, key=lambda key: self._chat_requests[key][1])
            self._chat_requests.pop(oldest, None)
        request_id = secrets.randbelow(2**31 - 1) + 1
        self._chat_requests[owner_id] = (request_id, now + 900)
        self._private_message(
            owner_id,
            "🟢 KYNTRION Post Studio\nНажмите «📂 Выбрать канал» и отметьте свой канал в Telegram.",
            reply_markup={
                "keyboard": [[{
                    "text": "📂 Выбрать канал",
                    "request_chat": {
                        "request_id": request_id,
                        "chat_is_channel": True,
                        "user_administrator_rights": dict(_PICKER_ADMIN_RIGHTS),
                        "bot_administrator_rights": dict(_PICKER_ADMIN_RIGHTS),
                        "request_title": True,
                        "request_username": True,
                    },
                }]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
                "input_field_placeholder": "Выберите свой Telegram-канал",
            })

    def _handle_shared_channel(self, owner_id: int, shared: dict) -> bool:
        """Consume an owner-bound picker response and recheck both admin roles."""
        pending = self._chat_requests.get(owner_id)
        request_id = shared.get("request_id")
        channel_id = shared.get("chat_id")
        if (not pending or type(request_id) is not int
                or pending[0] != request_id or pending[1] <= float(self.clock())
                or type(channel_id) is not int or channel_id >= 0):
            self._private_message(owner_id, "Выбор канала устарел. Откройте студию ещё раз.")
            return True
        self._chat_requests.pop(owner_id, None)
        if not self.connect_channel(owner_id, channel_id):
            self._private_message(
                owner_id,
                "Не удалось подключить канал. Проверьте права публикации у себя и бота, "
                "затем выберите канал снова.",
                reply_markup=reply_keyboard(include_post_studio=True))
            return True
        try:
            draft_id = self.store.create_draft(owner_id, channel_id)
            draft = self.store.get_draft(owner_id, draft_id)
        except (LookupError, ValueError):
            self._private_message(
                owner_id, "Канал подключён, но лимит черновиков достигнут. "
                "Откройте студию и продолжите работу с существующим черновиком.",
                reply_markup=reply_keyboard(include_post_studio=True))
            return True
        self._private_message(
            owner_id, "✅ Канал подключён. Открываю редактор.",
            reply_markup=reply_keyboard(include_post_studio=True))
        self._editor(owner_id, draft)
        return True

    def check_channel(self, owner_id: int, chat_id: int, action: str) -> bool:
        """Fail closed unless user and bot currently have this channel right."""
        if (type(owner_id) is not int or owner_id <= 0
                or type(chat_id) is not int or chat_id >= 0 or action not in _RIGHT):
            return False
        try:
            chat = self.api.get_chat(chat_id)
            if (not isinstance(chat, dict) or chat.get("type") != "channel"
                    or chat.get("id") != chat_id):
                return False
            user = self.api.get_chat_member(chat_id, owner_id)
            bot = self.api.get_chat_member(chat_id, self.bot_id)
        except Exception:
            # Telegram permission/network errors must never mean "allow".
            return False
        permission = _RIGHT[action]
        for member in (user, bot):
            if not isinstance(member, dict):
                return False
            status = member.get("status")
            if status == "creator":
                continue
            if status != "administrator" or member.get(permission) is not True:
                return False
        return True

    def connect_channel(self, owner_id: int, channel_ref: int | str) -> bool:
        if type(owner_id) is not int or owner_id <= 0:
            return False
        if isinstance(channel_ref, str):
            channel_ref = channel_ref.strip()
            if channel_ref.startswith("-") and channel_ref[1:].isdigit():
                try:
                    channel_ref = int(channel_ref)
                except ValueError:
                    return False
            elif not channel_ref.startswith("@"):
                return False
        try:
            chat = self.api.get_chat(channel_ref)
            if not isinstance(chat, dict) or chat.get("type") != "channel":
                return False
            channel_id = chat.get("id")
            if (type(channel_id) is not int or channel_id >= 0
                    or not self.check_channel(owner_id, channel_id, "post")):
                return False
            self.store.connect_channel(owner_id, channel_id,
                                       chat.get("title") or "Канал")
            return True
        except (ValueError, LookupError, KeyError, TypeError, OSError, RuntimeError):
            return False

    def _private_message(self, owner_id: int, text: str, **kwargs) -> None:
        self.api.send_action({"method": "sendMessage", "chat_id": owner_id,
                              "text": text, **kwargs})

    def _menu(self, owner_id: int) -> None:
        keyboard = []
        channels = self.store.list_channels(owner_id)
        if not channels:
            self._request_channel_picker(owner_id)
            return
        for channel in channels:
            token = secrets.token_urlsafe(8)
            self._selections[token] = (
                owner_id, channel["channel_id"], float(self.clock()) + 900)
            keyboard.append([{"text": "📣 " + channel["title"][:44],
                              "callback_data": "post:select:" + token}])
        for draft in self.store.list_drafts(owner_id)[:8]:
            if draft["status"] != "draft":
                continue
            token = self._control(owner_id, draft, "resume")
            keyboard.append([{"text": "📄 Черновик #" + str(draft["id"]),
                              "callback_data": "post:resume:" + token}])
        keyboard.append([{"text": "📂 Другой канал",
                          "callback_data": "post:connect"}])
        keyboard.append([{"text": "📅 Очередь", "callback_data": "post:jobs"},
                         {"text": "📊 Статистика", "callback_data": "post:stats"}])
        keyboard.append([{"text": "📨 Мои публикации", "callback_data": "post:posts"}])
        keyboard.append([{"text": "🌍 Часовой пояс", "callback_data": "post:timezone"}])
        self._private_message(
            owner_id, "🟢 KYNTRION Post Studio\nВыберите канал для создания публикации.",
            reply_markup={"inline_keyboard": keyboard})

    def _control(self, owner_id: int, draft: dict, kind: str) -> str:
        token = secrets.token_urlsafe(8)
        self._controls[token] = (
            owner_id, draft["id"], draft["version"], kind, float(self.clock()) + 900)
        return token

    def _editor(self, owner_id: int, draft: dict) -> None:
        rows = []
        actions = POST_STUDIO_EDITOR_ACTIONS
        for title, kind in actions:
            token = self._control(owner_id, draft, kind)
            rows.append([{"text": title, "callback_data": f"post:{kind}:{token}"}])
        self.store.set_session(owner_id, draft["id"], "editing")
        self._private_message(
            owner_id, f"📝 Черновик #{draft['id']} · версия {draft['version']}\n"
                      f"Канал: {draft['channel_id']}\n"
                      "Выберите действие ниже: добавьте текст, фото или видео, "
                      "затем откройте предпросмотр.",
            reply_markup={"inline_keyboard": rows})

    def _current_draft(self, owner_id: int) -> dict | None:
        session = self.store.get_session(owner_id)
        if session is None:
            return None
        draft = self.store.get_draft(owner_id, session["draft_id"])
        if (draft is None or draft["status"] != "draft"
                or not self.check_channel(owner_id, draft["channel_id"], "post")):
            return None
        return draft

    def _update_draft(self, owner_id: int, patch: dict) -> None:
        draft = self._current_draft(owner_id)
        if draft is None:
            self._private_message(owner_id, "Нет доступного черновика. Откройте «Студия постов».")
            return
        try:
            changed = self.store.patch_draft(
                owner_id, draft["id"], patch, draft["version"])
        except (ValueError, LookupError):
            self._private_message(owner_id,
                                  "Черновик изменился. Откройте «Студия постов» заново.")
            return
        self._editor(owner_id, changed)

    def _add_button(self, owner_id: int, value: str) -> None:
        draft = self._current_draft(owner_id)
        if draft is None:
            self._private_message(owner_id, "Нет доступного черновика. Откройте «Студия постов».")
            return
        title, sep, url = value.partition("|")
        if not sep:
            self._private_message(owner_id,
                                  "Формат кнопки: Название | https://адрес")
            return
        rows = [list(row) for row in draft["content"].get("buttons", [])]
        rows.append([{"text": title.strip(), "url": url.strip()}])
        try:
            _keyboard({"buttons": rows})
        except ValueError:
            self._private_message(owner_id,
                                  "Некорректная URL-кнопка. Нужен HTTPS-адрес.")
            return
        self._update_draft(owner_id, {"buttons": rows})

    def _change_buttons(self, owner_id: int, command: str) -> None:
        draft = self._current_draft(owner_id)
        if draft is None:
            self._private_message(owner_id, "Нет доступного черновика. Откройте «Студия постов».")
            return
        rows = [list(row) for row in draft["content"].get("buttons", [])]
        parts = command.split()
        try:
            if len(parts) == 2 and parts[0] == "remove":
                index = int(parts[1]) - 1
                if not 0 <= index < len(rows):
                    raise ValueError
                rows.pop(index)
            elif len(parts) == 3 and parts[0] == "move":
                old, new = int(parts[1]) - 1, int(parts[2]) - 1
                if not 0 <= old < len(rows) or not 0 <= new < len(rows):
                    raise ValueError
                rows.insert(new, rows.pop(old))
            else:
                raise ValueError
        except ValueError:
            self._private_message(
                owner_id, "Чтобы удалить кнопку, отправьте «remove 1». "
                "Чтобы поменять порядок — «move 2 1».")
            return
        self._update_draft(owner_id, {"buttons": rows})

    def _capture_media(self, owner_id: int, message: dict) -> bool:
        draft = self._current_draft(owner_id)
        if draft is None:
            return False
        photos = message.get("photo")
        video = message.get("video")
        item = None
        if isinstance(photos, list) and photos:
            last = photos[-1]
            if isinstance(last, dict):
                item = {"type": "photo", "file_id": last.get("file_id")}
        elif isinstance(video, dict):
            item = {"type": "video", "file_id": video.get("file_id")}
        if item is None:
            return False
        rows = list(draft["content"].get("media", []))
        rows.append(item)
        patch = {"media": rows}
        if isinstance(message.get("caption"), str) and message["caption"]:
            patch["caption"] = message["caption"]
        try:
            _ = self.render({"content": {**draft["content"], **patch}})
        except ValueError:
            self._private_message(owner_id, "Недопустимый файл или превышен лимит медиа.")
            return True
        self._update_draft(owner_id, patch)
        return True

    def request_publish(self, owner_id: int, draft_id: int) -> str:
        """Show the exact private preview and final confirmation; never send to channel."""
        draft = self.store.get_draft(owner_id, draft_id)
        if (draft is None or draft["status"] != "draft"
                or not self.check_channel(owner_id, draft["channel_id"], "post")):
            raise LookupError("Draft or channel posting rights not available")
        actions = self.render(draft, preview=True)
        token = self.store.create_confirmation(owner_id, draft_id, draft["version"])
        for action in actions:
            self.api.send_action({**action, "chat_id": owner_id})
        content = draft["content"]
        snippet = content.get("text", "") or "Текст форматирован или отсутствует"
        snippet = snippet[:160]
        self._private_message(
            owner_id,
            f"⚠️ Подтвердите публикацию\n"
            f"Канал: {draft['channel_id']}\n"
            f"Черновик #{draft_id}, версия {draft['version']}\n"
            f"Текст: {snippet}\n"
            f"Медиа: {len(content.get('media', []))}; "
            f"строк кнопок: {len(content.get('buttons', []))}\n"
            "Отправка произойдёт только после вашего подтверждения.",
            reply_markup={"inline_keyboard": [
                [{"text": "✅ Да, опубликовать",
                  "callback_data": "post:confirm:" + token}],
                [{"text": "↩️ Не публиковать",
                  "callback_data": "post:reject:" + token}],
            ]})
        return token

    @staticmethod
    def _message_ids(result: object) -> list[int] | None:
        records = result if isinstance(result, list) else [result]
        if not records or not all(
                isinstance(record, dict) and type(record.get("message_id")) is int
                and record["message_id"] > 0 for record in records):
            return None
        return [record["message_id"] for record in records]

    def _notify_publish(self, owner_id: int, state: str) -> None:
        phrases = {
            "published": "✅ Пост опубликован. Квитанция сохранена.",
            "failed": f"❌ {self.platform} отклонил отправку. Проверьте настройки канала.",
            "uncertain": ("⚠️ Результат отправки неизвестен. Проверьте канал вручную. "
                          "Повторной автоматической отправки НЕ будет."),
            "denied": "Нет текущих прав публикации. Пост не отправлен.",
        }
        try:
            self._private_message(owner_id, phrases[state])
        except Exception:
            # A private status message failure never triggers channel re-send.
            pass

    def schedule_draft(self, owner_id: int, draft_id: int, local_time: str,
                       zone_name: str | None = None) -> int:
        """Schedule the current immutable draft revision using a selected IANA zone."""
        draft = self.store.get_draft(owner_id, draft_id)
        if (draft is None or draft["status"] != "draft"
                or not self.check_channel(owner_id, draft["channel_id"], "post")):
            raise LookupError("Draft or channel rights unavailable")
        self.render(draft)  # reject invalid posts before committing a schedule
        zone = zone_name or self.store.get_timezone(owner_id)
        due = local_to_utc(local_time, zone)
        if datetime.fromisoformat(due).timestamp() <= float(self.clock()):
            raise ValueError("Scheduled time must be in the future")
        return self.store.create_job(owner_id, draft_id, due)

    def cancel_scheduled(self, owner_id: int, job_id: int) -> bool:
        return self.store.cancel_job(owner_id, job_id)

    def request_schedule(self, owner_id: int, draft_id: int, local_time: str,
                         zone_name: str | None = None) -> str:
        """Preview and ask for an owner-bound confirmation before enqueuing."""
        draft = self.store.get_draft(owner_id, draft_id)
        if (draft is None or draft["status"] != "draft"
                or not self.check_channel(owner_id, draft["channel_id"], "post")):
            raise LookupError("Draft or posting rights unavailable")
        zone = zone_name or self.store.get_timezone(owner_id)
        due = local_to_utc(local_time, zone)
        if datetime.fromisoformat(due).timestamp() <= float(self.clock()):
            raise ValueError("Schedule time must be in the future")
        preview = self.render(draft, preview=True)
        token = self.store.create_schedule_confirmation(
            owner_id, draft_id, draft["version"], due)
        for action in preview:
            self.api.send_action({**action, "chat_id": owner_id})
        self._private_message(owner_id, f"📅 Подтвердите отложенную публикацию\n"
                              f"Канал: {draft['channel_id']}\n"
                              f"Время: {local_time} ({zone})\n"
                              f"Версия черновика: {draft['version']}\n"
                              "До подтверждения в очередь ничего не добавлено.",
                              reply_markup={"inline_keyboard": [
                                  [{"text": "✅ Запланировать",
                                    "callback_data": "post:schedule-confirm:" + token}],
                                  [{"text": "↩️ Не планировать",
                                    "callback_data": "post:schedule-reject:" + token}]]})
        return token

    def confirm_schedule(self, owner_id: int, token: str) -> int | None:
        info = self.store.consume_schedule_confirmation(owner_id, token)
        if info is None:
            return None
        draft = self.store.get_draft(owner_id, info["draft_id"])
        if (draft is None or draft["status"] != "draft"
                or draft["version"] != info["draft_version"]
                or datetime.fromisoformat(info["due_utc"]).timestamp() <=
                float(self.clock())
                or not self.check_channel(owner_id, draft["channel_id"], "post")):
            return None
        try:
            self.render(draft)
            return self.store.create_job(owner_id, draft["id"], info["due_utc"],
                                         expected_version=info["draft_version"])
        except (ValueError, LookupError):
            return None

    def execute_job(self, job: dict) -> str:
        """Execute only a previously durably claimed scheduled revision."""
        if (not isinstance(job, dict) or job.get("state") != "inflight"
                or type(job.get("owner_id")) is not int):
            return "failed"
        owner_id = job["owner_id"]
        stored_job = self.store.get_job(owner_id, job.get("id"))
        if (stored_job is None or stored_job["state"] != "inflight"
                or stored_job["attempt_id"] != job.get("attempt_id")):
            return "failed"
        draft = self.store.get_draft(owner_id, job["draft_id"])
        if (draft is None or draft["status"] != "draft"
                or draft["version"] != job["draft_version"]
                or not self.check_channel(owner_id, draft["channel_id"], "post")):
            self._notify_publish(owner_id, "denied")
            return "failed"
        try:
            actions = self.render(draft)
        except ValueError:
            return "failed"
        result = self._send_claimed_revision(owner_id, draft, actions)
        return "failed" if result == "rejected" else result

    def _send_claimed_revision(self, owner_id: int, draft: dict,
                               actions: list[dict]) -> str:
        """Journal a unique attempt before making any Telegram send call."""
        publication_id = self.store.begin_publication(
            owner_id, draft["id"], draft["version"])
        if publication_id is None:
            return "rejected"
        recorded = []
        for action in actions:
            try:
                result = self.send_public_action(
                    {**action, "chat_id": draft["channel_id"]})
                message_ids = self._message_ids(result)
                if message_ids is None:
                    raise ValueError("Telegram returned no usable message_id")
                if (action["method"] == "sendMediaGroup"
                        and len(message_ids) != len(action["media"])):
                    raise ValueError("Telegram returned incomplete album receipt")
                self.store.record_sent_messages(owner_id, publication_id, message_ids)
                recorded.extend(message_ids)
            except Exception as exc:
                definite_first_failure = not recorded and self.is_api_rejection(exc)
                state = "failed" if definite_first_failure else "uncertain"
                try:
                    self.store.finish_publication(
                        owner_id, publication_id, state,
                        {"last_error_type": type(exc).__name__})
                except Exception:
                    state = "uncertain"
                self._notify_publish(owner_id, state)
                return state
        try:
            self.store.finish_publication(
                owner_id, publication_id, "published",
                {"message_count": len(recorded)})
        except Exception:
            self._notify_publish(owner_id, "uncertain")
            return "uncertain"
        self._notify_publish(owner_id, "published")
        return "published"

    def confirm_publish(self, owner_id: int, confirmation_token: str) -> str:
        """Consume an owner-bound token, journal once, and send at most once."""
        if type(owner_id) is not int or owner_id <= 0:
            return "rejected"
        confirmed = self.store.consume_confirmation(owner_id, confirmation_token)
        if confirmed is None:
            return "rejected"
        draft = self.store.get_draft(owner_id, confirmed["draft_id"])
        if (draft is None or draft["version"] != confirmed["version"]
                or draft["status"] != "draft"):
            return "rejected"
        if not self.check_channel(owner_id, draft["channel_id"], "post"):
            self._notify_publish(owner_id, "denied")
            return "denied"
        try:
            actions = self.render(draft)
        except ValueError:
            return "rejected"
        return self._send_claimed_revision(owner_id, draft, actions)

    def _manage_actions(self, publication: dict, kind: str,
                        payload: object) -> list[dict]:
        """Build edits only for the stored IDs of this bot's own published post."""
        draft = self.store.get_draft(publication["owner_id"], publication["draft_id"])
        if draft is None or draft["version"] != publication["draft_version"]:
            raise ValueError("Publication revision is no longer available")
        content = draft["content"]
        sent = self.render(draft)
        ids = publication["message_ids"]
        if not ids or any(type(mid) is not int or mid <= 0 for mid in ids):
            raise ValueError("No verified Telegram delivery receipts")
        mappings = []
        index = 0
        for action in sent:
            count = len(action["media"]) if action["method"] == "sendMediaGroup" else 1
            item_ids = ids[index:index + count]
            if len(item_ids) != count:
                raise ValueError("Incomplete original Telegram receipts")
            mappings.append((action, item_ids))
            index += count
        if index != len(ids):
            raise ValueError("Mismatched original Telegram receipts")
        chat_id = publication["channel_id"]
        if kind == "delete":
            if payload is not None:
                raise ValueError("Delete takes no payload")
            return [{"method": "deleteMessage", "chat_id": chat_id, "message_id": mid}
                    for mid in reversed(ids)]
        if kind == "edit_text":
            if (not isinstance(payload, str) or not payload.strip()
                    or _length(payload) > 4096):
                raise ValueError("Text must be 1–4096 characters")
            if not content.get("text") and not content.get("segments"):
                raise ValueError("Original post has no separate text message")
            for original, item_ids in mappings:
                if original["method"] == "sendMessage":
                    action = {"method": "editMessageText", "chat_id": chat_id,
                              "message_id": item_ids[0], "text": escape(payload),
                              "parse_mode": "HTML"}
                    if "reply_markup" in original:
                        action["reply_markup"] = original["reply_markup"]
                    return [action]
            raise ValueError("Text message cannot be edited in this post")
        if kind == "edit_caption":
            if (not isinstance(payload, str) or _length(payload) > 1024):
                raise ValueError("Caption must be 0–1024 characters")
            for original, item_ids in mappings:
                if original["method"] in ("sendPhoto", "sendVideo", "sendMediaGroup"):
                    return [{"method": "editMessageCaption", "chat_id": chat_id,
                             "message_id": item_ids[0], "caption": escape(payload),
                             "parse_mode": "HTML"}]
            raise ValueError("Original post has no media to caption")
        if kind == "edit_buttons":
            keyboard = _keyboard({"buttons": payload})
            for original, item_ids in mappings:
                if "reply_markup" in original:
                    return [{"method": "editMessageReplyMarkup", "chat_id": chat_id,
                             "message_id": item_ids[0], "reply_markup": keyboard or
                             {"inline_keyboard": []}}]
            for original, item_ids in mappings:
                if original["method"] in ("sendMessage", "sendPhoto", "sendVideo"):
                    return [{"method": "editMessageReplyMarkup", "chat_id": chat_id,
                             "message_id": item_ids[0], "reply_markup": keyboard or
                             {"inline_keyboard": []}}]
            raise ValueError("An album without a companion message cannot gain buttons")
        if kind == "edit_media":
            changed = _media({"media": [payload]})
            if len(content.get("media", [])) != 1:
                raise ValueError("Only single-media posts support replacement")
            for original, item_ids in mappings:
                if original["method"] in ("sendPhoto", "sendVideo"):
                    media = changed[0]
                    if original.get("caption"):
                        media["caption"] = original["caption"]
                        media["parse_mode"] = "HTML"
                    return [{"method": "editMessageMedia", "chat_id": chat_id,
                             "message_id": item_ids[0], "media": media}]
            raise ValueError("Original media is not editable")
        raise ValueError("Unsupported management action")

    def request_manage(self, owner_id: int, publication_id: int,
                       kind: str, payload: object = None) -> str:
        publication = self.store.get_publication(owner_id, publication_id)
        if publication is None or publication["state"] != "published":
            raise LookupError("Only your bot-authored published messages can be managed")
        permission = "delete" if kind == "delete" else "edit"
        if not self.check_channel(owner_id, publication["channel_id"], permission):
            raise LookupError("Current channel permissions are insufficient")
        self._manage_actions(publication, kind, payload)
        token = self.store.create_management_confirmation(
            owner_id, publication_id, kind, payload)
        self._private_message(
            owner_id,
            f"⚠️ Подтвердите {kind} для своей публикации #{publication_id}. "
            f"Операцию нельзя автоматически повторить, если {self.platform} не ответит.",
            reply_markup={"inline_keyboard": [[
                {"text": "✅ Подтвердить", "callback_data": "post:manage-confirm:" + token}],
                [{"text": "↩️ Отмена", "callback_data": "post:manage-reject:" + token}]]})
        return token

    def confirm_manage(self, owner_id: int, token: str) -> str:
        confirmed = self.store.consume_management_confirmation(owner_id, token)
        if confirmed is None:
            return "rejected"
        publication = self.store.get_publication(owner_id, confirmed["publication_id"])
        if publication is None or publication["state"] != "published":
            return "rejected"
        permission = "delete" if confirmed["kind"] == "delete" else "edit"
        if not self.check_channel(owner_id, publication["channel_id"], permission):
            return "denied"
        try:
            actions = self._manage_actions(
                publication, confirmed["kind"], confirmed["payload"])
        except ValueError:
            return "unsupported"
        operation_id = self.store.begin_management(
            owner_id, publication["id"], token, confirmed["kind"])
        if operation_id is None:
            return "rejected"
        acknowledged = 0
        for action in actions:
            try:
                result = self.send_public_action(action)
                if action["method"] == "deleteMessage":
                    accepted = result is True or (isinstance(result, dict)
                                                  and result.get("message_id") is not None)
                else:
                    accepted = isinstance(result, dict) and type(result.get("message_id")) is int
                if not accepted:
                    raise ValueError("No usable edit/delete acknowledgment")
                self.store.record_management_ack(
                    owner_id, operation_id, action["message_id"])
                acknowledged += 1
            except Exception as exc:
                definite_first_failure = not acknowledged and self.is_api_rejection(exc)
                state = "failed" if definite_first_failure else "uncertain"
                try:
                    self.store.finish_management(owner_id, operation_id, state,
                                                 {"error_type": type(exc).__name__})
                except Exception:
                    state = "uncertain"
                return "unsupported" if state == "failed" else "uncertain"
        status = "deleted" if confirmed["kind"] == "delete" else "edited"
        try:
            self.store.finish_management(owner_id, operation_id, status,
                                         self.management_receipt(actions))
        except Exception:
            return "uncertain"
        return status

    def _handle_callback(self, update: dict, owner_id: int) -> bool:
        data = update.get("data")
        if not isinstance(data, str) or not data.startswith("post:"):
            return False
        callback_id = update.get("id")
        if not isinstance(callback_id, str):
            return False
        reply = {"method": "answerCallbackQuery", "callback_query_id": callback_id}
        if data.startswith("post:confirm:"):
            self.api.send_action(reply)
            result = self.confirm_publish(owner_id, data.removeprefix("post:confirm:"))
            if result == "rejected":
                self._private_message(
                    owner_id, "Подтверждение устарело. Откройте «Студия постов» и проверьте черновик.")
            return True
        if data.startswith("post:manage-confirm:"):
            self.api.send_action(reply)
            result = self.confirm_manage(owner_id,
                                         data.removeprefix("post:manage-confirm:"))
            self._private_message(owner_id, "Результат управления: " + result)
            return True
        if data.startswith("post:manage-reject:"):
            self.store.consume_management_confirmation(
                owner_id, data.removeprefix("post:manage-reject:"))
            self.api.send_action(reply)
            self._private_message(owner_id, "Изменение отменено")
            return True
        if data.startswith("post:schedule-confirm:"):
            self.api.send_action(reply)
            job_id = self.confirm_schedule(
                owner_id, data.removeprefix("post:schedule-confirm:"))
            self._private_message(owner_id, "📅 Создана задача #" + str(job_id) +
                                  ". Проверьте «Очередь» в студии." if job_id else
                                  "Отложенная публикация не подтверждена или устарела")
            return True
        if data.startswith("post:schedule-reject:"):
            self.store.consume_schedule_confirmation(
                owner_id, data.removeprefix("post:schedule-reject:"))
            self.api.send_action(reply)
            self._private_message(owner_id, "Отложенная публикация отменена")
            return True
        if data.startswith("post:reject:"):
            self.store.consume_confirmation(owner_id, data.removeprefix("post:reject:"))
            self.api.send_action(reply)
            self._private_message(owner_id, "Публикация отменена, черновик сохранён.")
            return True
        if data == "post:connect":
            self.api.send_action(reply)
            self._request_channel_picker(owner_id)
            return True
        if data == "post:home":
            self.api.send_action(reply)
            self._menu(owner_id)
            return True
        if data == "post:timezone":
            self.api.send_action(reply)
            buttons = [[{"text": zone + (" ✓" if zone == self.store.get_timezone(owner_id)
                                          else ""), "callback_data": "post:tz:" + zone}]
                       for zone in _TIMEZONE_CHOICES]
            buttons.append([{"text": "⬅️ К студии", "callback_data": "post:home"}])
            self._private_message(owner_id, "🌍 Выберите часовой пояс для отложенных публикаций.",
                                  reply_markup={"inline_keyboard": buttons})
            return True
        if data.startswith("post:tz:"):
            self.api.send_action(reply)
            zone = data.removeprefix("post:tz:")
            if zone not in _TIMEZONE_CHOICES:
                self._private_message(owner_id, "Этот часовой пояс недоступен")
                return True
            try:
                self.store.set_timezone(owner_id, zone)
            except ValueError:
                self._private_message(owner_id, "Невозможно установить часовой пояс")
            else:
                self._private_message(owner_id, "✅ Часовой пояс: " + zone)
                self._menu(owner_id)
            return True
        if data.startswith("post:unschedule:"):
            self.api.send_action(reply)
            raw_job_id = data.removeprefix("post:unschedule:")
            job_id = int(raw_job_id) if raw_job_id.isascii() and raw_job_id.isdecimal() else 0
            cancelled = self.cancel_scheduled(owner_id, job_id) if job_id > 0 else False
            self._private_message(owner_id, "✅ Публикация отменена" if cancelled else
                                  "Нельзя отменить: задача не ваша, уже выполняется или завершена")
            return True
        if data in ("post:jobs", "post:stats", "post:posts"):
            self.api.send_action(reply)
            self.handle_update({"message": {
                "chat": {"id": owner_id, "type": "private"},
                "from": {"id": owner_id},
                "text": "/post " + data.removeprefix("post:")}})
            return True
        if data.startswith("post:select:"):
            token = data.removeprefix("post:select:")
            session = self._selections.get(token)
            if (session and session[0] == owner_id
                    and session[2] > float(self.clock())
                    and self.check_channel(owner_id, session[1], "post")):
                self._selections.pop(token, None)
                try:
                    draft_id = self.store.create_draft(owner_id, session[1])
                    draft = self.store.get_draft(owner_id, draft_id)
                except (LookupError, ValueError):
                    reply["text"] = "Не удалось создать черновик."
                    self.api.send_action(reply)
                    return True
                self.api.send_action(reply)
                self._editor(owner_id, draft)
                return True
        else:
            parts = data.split(":", 2)
            if len(parts) == 3:
                kind, token = parts[1], parts[2]
                item = self._controls.get(token)
                if (item and item[0] == owner_id and item[3] == kind
                        and item[4] > float(self.clock())):
                    draft = self.store.get_draft(owner_id, item[1])
                    if (draft and draft["status"] == "draft"
                            and draft["version"] == item[2]
                            and self.check_channel(owner_id, draft["channel_id"], "post")):
                        self._controls.pop(token, None)
                        self.api.send_action(reply)
                        if kind == "resume":
                            self._editor(owner_id, draft)
                        elif kind in ("text", "caption", "media", "button"):
                            step = "button_add" if kind == "button" else kind
                            self.store.set_session(owner_id, draft["id"], step)
                            prompt = {
                                "text": "Отправьте текст будущего поста.",
                                "caption": "Отправьте подпись к фото или видео.",
                                "media": "Пришлите фото или видео сюда, в личный чат.",
                                "button": "Отправьте: Название кнопки | https://адрес",
                            }[kind]
                            self._private_message(owner_id, prompt)
                        elif kind == "preview":
                            try:
                                actions = self.render(draft, preview=True)
                            except ValueError:
                                self._private_message(
                                    owner_id,
                                    "Пока нечего показать. Добавьте текст или медиа.")
                            else:
                                for action in actions:
                                    self.api.send_action({**action, "chat_id": owner_id})
                        elif kind == "publish":
                            try:
                                self.request_publish(owner_id, draft["id"])
                            except (ValueError, LookupError):
                                self._private_message(
                                    owner_id,
                                    "Невозможно опубликовать. Проверьте содержимое и права.")
                        elif kind == "schedule":
                            self.store.set_session(owner_id, draft["id"], "schedule")
                            self._private_message(
                                owner_id, "📅 Отправьте дату и время: ГГГГ-ММ-ДД ЧЧ:ММ\n"
                                "Часовой пояс: " + self.store.get_timezone(owner_id))
                        elif kind == "stats":
                            self.handle_update({"message": {
                                "chat": {"id": owner_id, "type": "private"},
                                "from": {"id": owner_id}, "text": "/post stats"}})
                        elif kind in ("save", "cancel"):
                            self.store.clear_session(owner_id)
                            self._private_message(
                                owner_id, "💾 Черновик сохранён. Откройте «Студия постов» для продолжения.")
                        else:
                            reply["text"] = "Действие ещё недоступно."
                        return True
        reply["text"] = "Кнопка устарела или нет доступа. Откройте «Студия постов»."
        self.api.send_action(reply)
        return True

    def handle_update(self, update: dict) -> bool:
        if not isinstance(update, dict):
            return False
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            actor = callback.get("from")
            message = callback.get("message")
            chat = message.get("chat") if isinstance(message, dict) else None
            owner_id = actor.get("id") if isinstance(actor, dict) else None
            if (type(owner_id) is not int or owner_id <= 0
                    or not isinstance(chat, dict) or chat.get("type") != "private"
                    or chat.get("id") != owner_id):
                return False
            return self._handle_callback(callback, owner_id)

        message = update.get("message")
        if not isinstance(message, dict):
            return False
        actor = message.get("from")
        chat = message.get("chat")
        text = message.get("text")
        owner_id = actor.get("id") if isinstance(actor, dict) else None
        if (type(owner_id) is not int or owner_id <= 0
                or not isinstance(chat, dict) or chat.get("type") != "private"
                or chat.get("id") != owner_id):
            return False
        shared = message.get("chat_shared")
        if isinstance(shared, dict):
            return self._handle_shared_channel(owner_id, shared)
        if not isinstance(text, str):
            return self._capture_media(owner_id, message)
        if text in ("/post", "🎨 Студия постов"):
            self._menu(owner_id)
            return True
        if text.startswith("/post connect "):
            candidate = text.removeprefix("/post connect ").strip()
            if self.connect_channel(owner_id, candidate):
                self._private_message(owner_id, "✅ Канал подключён. Откройте «Студия постов».")
            else:
                self._private_message(
                    owner_id,
                    "Не удалось подключить канал. Проверьте права администратора "
                    "у себя и бота, затем повторите.")
            return True
        if text.startswith("/post timezone "):
            zone = text.removeprefix("/post timezone ").strip()
            try:
                self.store.set_timezone(owner_id, zone)
            except ValueError:
                self._private_message(owner_id, "Неверная зона. Пример: Europe/Moscow")
            else:
                self._private_message(owner_id, f"🌍 Ваш часовой пояс: {zone}")
            return True
        if text == "/post jobs":
            jobs = self.store.list_jobs(owner_id)[-20:]
            rows = [f"#{job['id']} · {job['due_utc']} · {job['state']}"
                    for job in jobs]
            self._private_message(owner_id, "📅 Отложенные публикации:\n" +
                                  ("\n".join(rows) if rows else "Список пуст"),
                                  reply_markup={"inline_keyboard": [
                                      [{"text": "❌ Отменить #" + str(job["id"]),
                                        "callback_data": "post:unschedule:" + str(job["id"])}]
                                      for job in jobs if job["state"] == "queued"] +
                                      [[{"text": "⬅️ К студии", "callback_data": "post:home"}]]})
            return True
        if text == "/post posts":
            entries = self.store.list_publications(owner_id)[:20]
            lines = [f"#{entry['id']} · канал {entry['channel_id']} · "
                     f"{entry['state']} · сообщений {len(entry['message_ids'])}"
                     for entry in entries]
            self._private_message(owner_id, "📨 Опубликованные посты:\n" +
                                  ("\n".join(lines) if lines else "Список пуст"))
            return True
        if text == "/post stats":
            metrics = self.store.get_metrics(owner_id)
            self._private_message(
                owner_id, "📊 Моя студия:\n" +
                "\n".join(f"{key}: {value}" for key, value in metrics.items()) +
                "\nПросмотры и клики здесь не собираются.")
            return True
        if text.startswith("/post delete "):
            try:
                publication_id = int(text.removeprefix("/post delete ").strip())
                self.request_manage(owner_id, publication_id, "delete")
            except (ValueError, LookupError) as exc:
                self._private_message(owner_id, "Удаление недоступно: " + str(exc))
            return True
        if text.startswith("/post edit "):
            args = text.removeprefix("/post edit ").split(maxsplit=2)
            try:
                if len(args) != 3:
                    raise ValueError("Формат: /post edit ID text|caption новый текст")
                publication_id = int(args[0])
                if args[1] not in ("text", "caption"):
                    raise ValueError("Изменять можно text или caption")
                self.request_manage(owner_id, publication_id,
                                    "edit_" + args[1], args[2])
            except (ValueError, LookupError) as exc:
                self._private_message(owner_id, "Правка недоступна: " + str(exc))
            return True
        if text.startswith("/post editbuttons "):
            args = text.removeprefix("/post editbuttons ").split(maxsplit=1)
            try:
                if len(args) != 2:
                    raise ValueError("Формат: /post editbuttons ID Название | https://сайт")
                publication_id = int(args[0])
                if args[1].strip().lower() == "clear":
                    buttons = []
                else:
                    label, divider, url = args[1].partition("|")
                    if not divider:
                        raise ValueError("Нужна HTTPS URL-кнопка")
                    buttons = [[{"text": label.strip(), "url": url.strip()}]]
                self.request_manage(owner_id, publication_id, "edit_buttons", buttons)
            except (ValueError, LookupError) as exc:
                self._private_message(owner_id, "Кнопки недоступны: " + str(exc))
            return True
        if text.startswith("/post unschedule "):
            raw = text.removeprefix("/post unschedule ").strip()
            try:
                job_id = int(raw)
                cancelled = self.cancel_scheduled(owner_id, job_id)
            except ValueError:
                cancelled = False
            self._private_message(
                owner_id, "✅ Отложенный пост отменён" if cancelled else
                "Нельзя отменить: публикация уже началась или задача отсутствует")
            return True
        if text.startswith("/post schedule "):
            fields = text.removeprefix("/post schedule ").split(maxsplit=1)
            try:
                if len(fields) != 2:
                    raise ValueError("Missing draft ID and time")
                self.request_schedule(owner_id, int(fields[0]), fields[1])
            except (ValueError, LookupError) as exc:
                self._private_message(owner_id, f"Не удалось запланировать: {exc}")
            return True
        if text in ("/post save", "/post cancel"):
            self.store.clear_session(owner_id)
            self._private_message(owner_id, "💾 Черновик сохранён. Откройте «Студия постов».")
            return True
        if text.startswith("/post text "):
            self._update_draft(owner_id, {"text": text.removeprefix("/post text ")})
            return True
        if text.startswith("/post caption "):
            self._update_draft(owner_id, {"caption": text.removeprefix("/post caption ")})
            return True
        if text.startswith("/post button add "):
            self._add_button(owner_id, text.removeprefix("/post button add "))
            return True
        if text.startswith("/post button "):
            self._change_buttons(owner_id, text.removeprefix("/post button "))
            return True
        session = self.store.get_session(owner_id)
        if session and not text.startswith("/"):
            if session["step"] == "schedule":
                try:
                    self.request_schedule(owner_id, session["draft_id"], text)
                except (ValueError, LookupError) as exc:
                    self._private_message(owner_id, "Неверная дата или нет прав: " + str(exc))
                else:
                    self.store.set_session(owner_id, session["draft_id"], "editing")
                return True
            if session["step"] in ("text", "caption"):
                field = session["step"]
                segments_key = "segments" if field == "text" else "caption_segments"
                entities = message.get("entities", [])
                segments = segments_from_telegram(text, entities)
                if segments:
                    self._update_draft(owner_id, {field: "", segments_key: segments})
                else:
                    self._update_draft(owner_id, {field: text, segments_key: None})
                return True
            if session["step"] == "button_add":
                if text.startswith(("remove ", "move ")):
                    self._change_buttons(owner_id, text)
                else:
                    self._add_button(owner_id, text)
                return True
        return False
