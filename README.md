# PiarBot VK

Бот для сообществ взаимного пиара ВКонтакте.

## 🚀 Что умеет

- Автопроверка подписки перед добавлением своего паблика в пиар
- Невозможность пиариться не взаимно — халявщиков нет
- Внутренняя валюта **Piar Points (PP)**
- Награды за раунды, рефералы, вехи
- Бонусы за подключение бесед
- Личный кабинет с балансом, ресурсами, топом
- Админ-панель для владельца
- Автопромо в беседах
- Работает в нескольких беседах одновременно

## 📦 Установка

### 1. Требования

- Python 3.10+
- pip
- Linux / macOS / Windows / Termux

### 2. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 3. Настройка .env

Скопируй пример и заполни:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

| Переменная | Описание |
|---|---|
| VK_GROUP_TOKEN | Токен сообщества (Управление → Работа с API → Создать ключ) |
| VK_GROUP_ID | ID сообщества (положительное число) |
| VK_GROUP_SCREEN | Короткое имя для ссылки vk.me/screen |

## 🚀 Запуск

### Обычный запуск (в фоне)

```bash
./run.sh start
```

### Запуск со сторожем (рекомендуется)

Сторож watch.py проверяет PID bot.py раз в несколько секунд и поднимает при падении:

```bash
./run.sh start
./run.sh watch
```

### Запуск в текущей сессии (для отладки)

```bash
./run.sh foreground
```

Ctrl+C — стоп.

## 🎛 Управление (run.sh)

| Команда | Действие |
|---|---|
| `./run.sh start` | Запустить в фоне |
| `./run.sh stop` | Остановить |
| `./run.sh restart` | Перезапустить |
| `./run.sh status` | Проверить, работает ли |
| `./run.sh log` | Смотреть лог (Ctrl+C — выйти) |
| `./run.sh foreground` | Запуск в текущей сессии |
| `./run.sh watch` | Запустить сторожа |
| `./run.sh unwatch` | Остановить сторожа |
| `./run.sh help` | Справка |

## 💾 Данные и бэкапы

- БД: `data/piar.db` (SQLite, WAL)
- Бэкапы: `data/backups/piar-YYYYMMDD-HHMM.db` — авто, фон-цикл
- Логи: `logs/bot.log`, `logs/watch.log`, `logs/boot.log`

Восстановление из бэкапа:

```bash
./run.sh stop
cp data/backups/piar-YYYYMMDD-HHMM.db data/piar.db
./run.sh start
```

## ⚙️ Экономика (config.py)

Все награды и лимиты — в config.py:

- LIST_SIZE_DEFAULT = 5 — размер активного списка
- ROUND_BASE_REWARD = 100 — PP за 1-й раунд
- ROUND_GROWTH = 0.60 — +60% за каждый следующий
- REFERRAL_BONUS = 1000 — PP за приглашённого
- CHAT_ADD_BONUS = 10000 — PP за подключение беседы
- SIGNUP_BONUS = 500 — PP за регистрацию
- DAILY_BONUS = 100 — PP за ежедневный вход

## 🐛 Troubleshooting

### Бот не запускается

Смотри `logs/bot.log`:

```bash
tail -30 logs/bot.log
```

Частые причины:

- `.env` не создан или токен неверный
- Нет доступа к API — проверь права токена (управление сообществом, сообщения)
- Занят порт / другой экземпляр — `./run.sh stop` перед `start`

### Бот падает каждые несколько часов (Android/Termux)

На Android 14 (ColorOS, MIUI, OneUI) система агрессивно убивает фоновые процессы Termux, даже с termux-wake-lock. Решения:

1. Отключить battery optimization для Termux — Настройки → Приложения → Termux → Батарея → Без ограничений
2. Разрешить автозапуск — в настройках ColorOS/MIUI добавить Termux в автозапуск
3. Termux:Boot (F-Droid) — автозапуск при загрузке
4. termux-job-scheduler — периодические проверки через Android JobScheduler (TODO)

Простейший обход — периодически проверять:

```bash
./run.sh status
```

Если 🔴 Не работает:

```bash
./run.sh start
./run.sh watch
```

### Network down в логе

LongPoll автоматически переподключается (safe_listen). Если видишь Network down: ... wait 5s — это нормально, ждёт восстановления сети.

## 📁 Структура проекта

```
piar-bot/
├── bot.py              # точка входа, LongPoll
├── config.py           # парсинг .env + константы
├── db.py               # SQLite схема
├── db_migrations.py    # миграции
├── background.py       # фон-цикл (напоминания, бэкапы, промо)
├── ad.py               # автопромо в беседах
├── keyboards.py        # клавиатуры VK
├── utils.py            # хелперы
├── watch.py            # сторож (авто-перезапуск bot.py)
├── run.sh              # управление
├── handlers/
│   ├── private.py      # ЛС: регистрация, баланс, кабинет
│   ├── chat.py         # беседы: /list, раунды, промо
│   └── admin.py        # админ-панель
├── docs/
│   └── PROMPT.md       # контекст для ИИ-ассистента
├── data/               # БД и бэкапы (в .gitignore)
└── logs/               # логи (в .gitignore)
```

## 🤖 Для ИИ-ассистента

Если продолжаешь работу над проектом с ИИ (ChatGPT, DeepSeek и т.п.), передай ему содержимое `docs/PROMPT.md` — там весь контекст: стек, архитектура, экономика, грабли.

## 📜 Лицензия

MIT — см. [LICENSE](LICENSE).
