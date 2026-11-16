"""Entry points specific to VK community messages."""

from kyntrion_tools.bot_menu import reply_keyboard
from kyntrion_tools.command_catalog import help_keyboard


def send_vk_menu(api, peer, command, studio, news, panel):
    markup = None
    if command == "/start":
        text = ("KYNTRION · ВКонтакте\n\n"
                "Перешли текст для экспорта в TXT или голосовое до 3 минут для обработки. "
                "Несколько сообщений можно объединить командой /collect.\n"
                "Архивы: .zip и .unzip. Команды и примеры: /help.")
        markup = reply_keyboard(include_post_studio=studio)
        if getattr(api, "text_buttons", False):
            markup["keyboard"].append([{"text": "⬅️ В меню PiarBot"}])
            text += "\nВернуться в PiarBot — /piar."
        if news:
            text += "\nСервисные новости включены после обращения. Отписка — /news_off."
        if panel and peer == panel.owner_id:
            api.send_action({"method": "sendMessage", "chat_id": peer,
                             "text": "Уведомления сообщества",
                             "reply_markup": {"inline_keyboard": [[panel.entry_button()]]}})
    elif command == "/automation":
        text = ("Команды работают в сообщениях сообщества. В беседе ответь командой "
                ".txt, .текст или .цитата на нужное сообщение. Для команд в беседе "
                "боту нужны права удаления, а для результата в личку — открытый диалог "
                "с сообществом. Выбери команду для подробностей.")
        markup = help_keyboard()
    elif command == "/myid":
        text = f"Твой ID ВКонтакте — {peer}."
    elif command == "/voice":
        text = "Пришли голосовое ВК длительностью до трёх минут. При подключённом распознавании получишь текст и TXT."
    elif command == "/privacy":
        text = ("Заявки, настройки и история консультации привязаны к твоему ID ВК. "
                "Экспорт текста и сбор файлов хранятся в памяти на время обработки. "
                "Проверить сервисные уведомления — /news_status, отключить — /news_off. "
                "Очистить свои данные — /forget_me.")
    else:
        return False
    api.send_message(peer, text, reply_markup=markup)
    return True
