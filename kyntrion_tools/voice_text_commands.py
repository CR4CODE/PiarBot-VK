"""Fail-closed, claim-once routing for per-user group and business commands."""

from queue import Empty, SimpleQueue

from kyntrion_tools.voice_command_journal import JobKey
from kyntrion_tools.command_catalog import parse_dot
from kyntrion_tools.command_plugins.registry import execute_scoped, validate_argument


def _mapping(value):
    return value if isinstance(value, dict) else {}


class VoiceTextRouter:
    def __init__(self, api, controller, owner_id: int, journal):
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError('owner_id must be a positive Telegram user ID')
        self.api = api
        self.controller = controller
        self.owner_id = owner_id
        self.journal = journal

        # Worker threads never write SQLite directly. The polling thread owns
        # this queue drain and performs durable journal updates.
        self._completed = SimpleQueue()

    def flush_completed(self) -> None:
        while True:
            try:
                key, success = self._completed.get_nowait()
            except Empty:
                return
            self.journal.set_state(key, 'sent' if success else 'uncertain')

    def handle_update(self, update: dict) -> bool:
        update = _mapping(update)
        if 'business_connection' in update:
            # No cached rights: every new command performs an authoritative lookup.
            return True
        business = 'business_message' in update
        message = _mapping(update.get('business_message' if business else 'message'))
        parsed = parse_dot(message.get('text'))
        if parsed is None:
            return False
        action, arg = parsed
        chat = _mapping(message.get('chat'))
        if not business and chat.get('type') == 'private':
            return False
        # Consume rejected commands so legacy routing cannot leak a source reply.
        # Ordinary group users may run public commands, but a business command
        # must originate from the owner of that *specific* live connection.
        sender = _mapping(message.get('from'))
        actor_id = sender.get('id')
        if (type(actor_id) is not int or actor_id <= 0
                or message.get('sender_chat') is not None):
            return True
        chat_id, message_id = chat.get('id'), message.get('message_id')
        if (type(chat_id) is not int or chat_id == 0
                or type(message_id) is not int or message_id <= 0):
            return True
        connection_id = message.get('business_connection_id')
        if business:
            if (chat.get('type') != 'private' or not isinstance(connection_id, str)
                    or not connection_id.strip()):
                return True
        elif chat.get('type') not in ('group', 'supergroup') or connection_id:
            return True
        replyless = 'reply_to_message' not in message
        reply = _mapping(message.get('reply_to_message'))
        voice = _mapping(reply.get('voice'))
        if (action == 'text' and not replyless
                and (not isinstance(voice.get('file_id'), str)
                     or not voice['file_id'].strip())):
            return True
        if not replyless:
            if (('chat' in reply and _mapping(reply['chat']).get('id') != chat_id)
                    or ('business_connection_id' in reply
                        and reply['business_connection_id'] != connection_id)):
                return True
        scope = f'{connection_id}:{chat_id}' if business else str(chat_id)
        key = JobKey('business' if business else 'group', scope, message_id, actor_id)
        if business and self.journal.is_claimed(key):
            return True
        connection = {}
        if business:
            try:
                connection = _mapping(self.api.get_business_connection(connection_id))
            except Exception:
                # Unable to authenticate this connection: no sends or deletion.
                return True
            user = _mapping(connection.get('user'))
            if (connection.get('id') != connection_id
                    or type(user.get('id')) is not int or user['id'] != actor_id):
                return True
        warning = validate_argument(action, arg)
        if warning:
            # Validation precedes source-chat deletion. An invalid command
            # does not consume a journal claim or start background work.
            try:
                if business:
                    self.api.send_action({
                        'method': 'sendMessage', 'chat_id': chat_id,
                        'business_connection_id': connection_id, 'text': warning,
                    })
                else:
                    self.api.send_message(chat_id, warning)
            except Exception:
                pass
            return True
        # Persist before deletion or delivery; no existing claim is ever replayed.
        if not self.journal.claim(key):
            return True
        deleted = False
        try:
            if business:
                if (connection.get('is_enabled') is True
                        and _mapping(connection.get('rights')).get('can_delete_all_messages') is True):
                    deleted = self.api.delete_business_messages(connection_id, [message_id])
            else:
                deleted = self.api.delete_message(chat_id, message_id)
        except Exception:
            # The command's deletion was not acknowledged; never fetch media.
            deleted = False
        if deleted is not True:
            self._notify_failed_gate(key)
            return True
        try:
            self.journal.set_state(key, 'deleted')
            # Controller may send error notices during media handling as well as output.
            self.journal.set_state(key, 'sending')
            deferred = execute_scoped(
                action, arg, self.api, self.controller, actor_id,
                None if replyless else reply, source_chat_id=chat_id,
                business_connection_id=connection_id if business else None,
                on_complete=lambda success: self._completed.put((key, success)),
            )
            if not deferred:
                self.journal.set_state(key, 'sent')
        except Exception:
            # Unknown delivery must never trigger a compensating message or retry.
            self.journal.set_state(key, 'uncertain')
        return True

    def _notify_failed_gate(self, key):
        # Authentication and durable claim happened before the gate. Mark before
        # this one possible delivery; a crash or timeout must never replay it.
        self.journal.set_state(key, 'sending')
        try:
            self.api.send_message(
                key.actor_id,
                'Не удалось подтвердить удаление команды. Действие не запускалось. '
                'Проверь исходный чат или отправь команду в личку боту.')
        except Exception:
            pass
        finally:
            # The original command remains unresolved even if this notice arrived.
            self.journal.set_state(key, 'uncertain')
