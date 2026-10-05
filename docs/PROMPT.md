=== КОНТЕКСТ ДЛЯ ПРОДОЛЖЕНИЯ ===

**ПРОЕКТ:** PiarBot VK
- Назначение: бот для сообществ взаимного пиара ВКонтакте
- GitHub: github.com/CR4CODE/PiarBot-VK
- Группа: vk.com/go_gigi (id=157735873), экран go_gigi
- Пользователь: CR4CODE (Олег, user_id=156002808), Termux на Realme RMX3630 (Android 14, ColorOS 14)

**СТЕК:**
- Python 3.10+, vk_api==11.10.1, requests==2.34.2, python-dotenv
- SQLite (data/piar.db, WAL-режим), миграции актуальны v4
- LongPoll (VkLongPoll, user LongPoll — НЕ Bots, НЕ Callback)
- НЕ Telegram

**ПУТИ:**
- Рабочая папка: ~/projects/piar-bot/
- БД: data/piar.db (+ -shm, -wal)
- Бэкапы БД: data/backups/piar-YYYYMMDD-HHMM.db (авто)
- Релизы: ~/backups/piar-bot/releases/piar-bot-vk-YYYYMMDD-HHMM.zip
- Логи: logs/bot.log, logs/chat.log (новый), logs/watch.log, logs/boot.log
- Конфиг: .env (chmod 600, не в git)
- PID: data/bot.pid, data/watch.pid

**ПРАВИЛА РАБОТЫ:**
- Отвечать по-русски
- ОДНА команда за раз, пользователь кидает вывод
- Патчи через python3 -c одной строкой (Free ломает многострочные)
- Перед заменой: assert s.count(old) == 1
- Бэкапы: .pre-patchN / .pre-<name> рядом с оригиналом, в .gitignore
- Рестарт: ./run.sh restart, проверять status
- Всегда проверять результат через ./run.sh chatlog N
- При коммите — релизный zip через git archive

**АРХИТЕКТУРА:**
- bot.py — точка входа, LongPoll loop, safe_listen (авто-reconnect), кэш users.get, отдельный логгер chat → logs/chat.log
- config.py — парсит .env, все константы (API_VERSION=5.199)
- db.py — SQLite схема (~21 таблица: users, chats, channels, done_users, referrals, referral_milestones, chat_add_rewards, chat_milestones, config, transfers, rewards, payments, promo_log, chat_members, news_log, reminders_log, ad_bots, ad_targets, ad_posts, imitation_log, pending_deletes, pending_adds, release_notes)
- db_migrations.py — миграции v1-v4
- background.py — фон-цикл: reminders, backup, imitation, промо, wall-post, send_news
- ad.py — автопромо в беседах
- keyboards.py — VK-клавиатуры (только текст, callback-ов нет)
- utils.py — send() (логирует OUT в chat.log), is_chat(), to_chat_id(), mention(), utcnow(), is_owner(), chat_log
- release.py — НОВЫЙ: система релиз-постов (/release add/list/show/del/wall/chats/all)
- watch.py — сторож (проверяет PID bot.py)
- handlers/private.py — ЛС: регистрация, /start, баланс, кабинет, /release роутинг
- handlers/chat.py — беседы: /list, /skip, пиар-раунды, промо, приветствие новичков
- handlers/admin.py — админ-панель владельца

**ЭКОНОМИКА (config.py):**
- PP — внутренняя валюта
- LIST_SIZE_DEFAULT=5, ROUND_BASE_REWARD=100 (+60% за раунд), REFERRAL_BONUS=1000, CHAT_ADD_BONUS=10000, SIGNUP_BONUS=500, DAILY_BONUS=100
- SKIP_ADD_PRICE=1500 (пропуск проверки подписок)
- PENDING_ADD_TTL=600 (сек, срок pending-заявки)
- Milestones: рефералы {5,10,25,50}, беседы {1,3,5,10}

**ЧТО СДЕЛАНО (коммиты в origin/main):**
1. 30cbce5 (2026-10-05) — feat: платное добавление в очередь (/skip, 1500 PP) + chat.log
   - миграция v3: таблица pending_adds (user_id, peer_id, resource_id, resource_type, screen_name, title, created_at)
   - handle_link(): при провале подписок сохраняет pending, подсказка «💸 Или пропусти проверку за 1500 PP: /skip»
   - cmd_skip(): TTL → баланс → INSERT в queue → −1500 PP (reason=skip_add) → удаление msg юзера → подтверждение 30 сек
   - utils.send() логирует OUT в chat.log; bot.py создаёт логгер chat → logs/chat.log (IN-LS/IN-CHAT)
   - run.sh: chatlog|cl [N] — tail chat.log
2. c6ee2d2 (2026-10-05) — feat: система постов обновлений (/release)
   - миграция v4: release_notes (id, title, body, created_at, wall_post_id, wall_at, chats_ok, chats_fail, chats_at)
   - release.py: _make_post_text, CRUD, publish_wall (vk.wall.post), publish_chats (chats WHERE is_active=1)
   - Команды владельца: /release add Заголовок | Текст, list, show N, del N, wall N, chats N, all N
   - Роутинг в handlers/private.py, упоминание в /news справке admin.py
   - Протестировано: post_id=85490, chats ok=1
3. 9274654 (2026-10-05) — feat: улучшены приветствия в ЛС и беседе
   - ЛС /start: новое приветствие для новых и вернувшихся (баланс + меню)
   - chat.greet_newbie: адаптивное приветствие с планом (учитывает active_cnt)
4. Релизы: piar-bot-vk-20261005-0732.zip, -0752.zip

**ЧТО ОСТАЛОСЬ (не срочно):**
- Флаг paid в channels: чтобы skip-юзеры не получали round-reward (сейчас получают, Олег сказал «ок»)
- cmd_skip лежит ниже if __name__ == "__main__" в chat.py — косметика
- bot.py.pre-listen-patch от 30.09 — не применён, прибрать/применить?
- Termux убивается ColorOS: TODO termux-job-scheduler, Battery optimization off, Termux:Boot + foreground service
- Канал VK — API не поддерживает публикацию (ошибка 901 can_write.allowed=False), заблокировано VK
- Мусор: много .pre-patch* файлов в handlers/ (в .gitignore)

**ТЕКУЩАЯ ЗАДАЧА:**
Сессия завершена, всё закоммичено и запушено, релиз собран. Пользователь доволен. Следующая задача не определена — ждём запрос.

**ГРАБЛИ:**
- Free-исполнитель: только ОДНОстрочные python3 -c, многострочные -c и heredoc ломаются
- Escape \U0001f... в python -c ломается — использовать литеральные эмодзи
- Вложенные двойные кавычки в f-string: использовать \x27 или разные кавычки
- ColorOS/Android 14 агрессивно убивает Termux, termux-wake-lock НЕ спасает
- VK user LongPoll НЕ видит message_event (callback-кнопки) — только Bots LongPoll; в беседах reply-кнопки не отображаются, inline требуют Bots
- VK LongPoll переподключается через safe_listen() при Network down (5s wait)
- .env содержит токены — НИКОГДА не коммитить
- БД data/piar.db ~250KB+, растёт, бэкапить (data/backups/)
- vk_api==11.10.1 — старая, но работает, обновление может сломать
- Канал сообщества: peer_id канала = отрицательный ID, но бот не может писать без прав (901)
- Пользователь иногда копирует текст бота в терминал (bash: command not found) — не ошибка кода

=== КОНЕЦ ===
