=== КОНТЕКСТ ДЛЯ ПРОДОЛЖЕНИЯ ===

**ПРОЕКТ:** PiarBot VK
- Назначение: бот для сообществ взаимного пиара ВКонтакте
- GitHub: github.com/CR4CODE/PiarBot-VK
- Группа: vk.com/go_gigi (id=157735873), экран go_gigi
- Пользователь: CR4CODE, Termux на Realme RMX3630 (Android 14, ColorOS 14)

**СТЕК:**
- Python 3.10+
- vk_api==11.10.1, requests==2.34.2
- SQLite (data/piar.db, WAL-режим)
- LongPoll (VkLongPoll)
- НЕ Telegram, НЕ VK Callback API. Всё на LongPoll

**ПУТИ:**
- Рабочая папка: ~/projects/piar-bot/
- БД: data/piar.db (+ -shm, -wal)
- Бэкапы БД: data/backups/piar-YYYYMMDD-HHMM.db (авто, фон-цикл)
- Логи: logs/bot.log, logs/watch.log, logs/boot.log
- Конфиг: .env (chmod 600, не в git)
- PID-файлы: data/bot.pid, data/watch.pid

**УПРАВЛЕНИЕ (run.sh):**
- ./run.sh start — запуск в фоне (nohup + termux-wake-lock)
- ./run.sh stop / restart / status
- ./run.sh log — tail -f лога
- ./run.sh foreground — запуск в текущей сессии (для отладки)
- ./run.sh watch — запустить сторожа (watch.py, авто-перезапуск bot.py)
- ./run.sh unwatch — остановить сторожа

**АРХИТЕКТУРА:**
- bot.py — точка входа, LongPoll loop, safe_listen (авто-reconnect), кэш users.get
- config.py — парсит .env, все константы (награды, лимиты, таймауты, API_VERSION=5.199)
- db.py — SQLite схема (~20 таблиц: users, chats, channels, done_users, referrals, referral_milestones, chat_add_rewards, chat_milestones, config, transfers, rewards, payments, promo_log, chat_members, news_log, reminders_log, ad_bots, ad_targets, ad_posts, imitation_log, pending_deletes)
- db_migrations.py — простые миграции (v2 актуальна)
- background.py — фон-цикл: reminders, backup, imitation, промо (27KB)
- ad.py — автопромо в беседах (17KB)
- keyboards.py — клавиатуры VK (callback-кнопки)
- utils.py — send(), is_chat(), to_chat_id(), хелперы
- watch.py — сторож (проверяет PID bot.py, поднимает при падении)
- handlers/private.py — ЛС-хендлеры: регистрация, баланс, кабинет, ресурсы, топ, рефералы
- handlers/chat.py — беседы: /list, пиар-раунды, промо, приветствие новичков
- handlers/admin.py — админ-панель владельца (53KB, самый большой)

**ЭКОНОМИКА (из config.py):**
- Piar Points (PP) — внутренняя валюта
- LIST_SIZE_DEFAULT=5, ROUND_BASE_REWARD=100 (+60% за раунд), REFERRAL_BONUS=1000, CHAT_ADD_BONUS=10000, SIGNUP_BONUS=500, DAILY_BONUS=100
- Milestones: рефералы {5:5000, 10:15000, 25:50000, 50:150000}, беседы {1:10000, 3:50000, 5:100000, 10:300000}

**ПРАВИЛА РАБОТЫ:**
- Отвечать по-русски
- ОДНА команда за раз, пользователь кидает вывод
- Патчи через Python: python3 -c "..." одной строкой для Free-режима, или cat > /tmp/patch.py <<'PYEOF' в Termux напрямую для многострочных
- Перед заменой: assert s.count(old) == 1
- Бэкапы: .pre-patchN рядом с оригиналом, в .gitignore
- При перезапуске — ./run.sh restart, проверять status
- Бот на LongPoll, отвечает в ЛС и беседах; свой же паблик в беседах игнорируется (skip non-user sender: -157735873)

**ГРАБЛИ:**
- ColorOS/Android 14 агрессивно убивает фоновые процессы Termux. termux-wake-lock НЕ спасает. Сторож (watch.py) умирает раз в несколько часов. Бот падает без присмотра
- Решение (TODO): termux-job-scheduler, Battery optimization off для Termux, Termux:Boot + foreground service
- VK LongPoll переподключается через safe_listen() при Network down (5s wait)
- .env содержит токены — НИКОГДА не коммитить
- БД (data/piar.db) — 250KB+, растёт. Не коммитить, но бэкапить (data/backups/)
- В handlers/ много .pre-patchN файлов — мусор от итераций, тоже в .gitignore
- vk_api==11.10.1 — старая версия, но работает. Обновление может сломать

**ЧТО ИГНОРИТЬ В GIT (.gitignore):**
.env
data/*.db
data/*.db-shm
data/*.db-wal
data/*.pid
data/backups/
logs/
__pycache__/
*.pyc
*.pre-patch*
*.bak*
bot.py.pre-listen-patch

**ТЕКУЩЕЕ СОСТОЯНИЕ (2026-10-05):**
- Бот работает, PID 25253, сторож жив
- БД: piar.db (250KB), миграции актуальны (v2)
- Последний релиз: zip ~/backups/piar-bot/releases/piar-bot-vk-YYYYMMDD-HHMM.zip (много версий от 27-29.09)
- bot.py.pre-listen-patch — от 30.09, не применён

=== КОНЕЦ ===
