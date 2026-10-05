# keyboards.py — VK-клавиатуры для PiarBot
import json


def _text(label: str, color: str = "secondary"):
    return {"action": {"type": "text", "label": label}, "color": color}


def _link(label: str, url: str):
    return {"action": {"type": "open_link", "label": label, "link": url}}


def _kb(buttons, inline: bool = False, one_time: bool = False) -> str:
    return json.dumps(
        {"one_time": one_time, "inline": inline, "buttons": buttons},
        ensure_ascii=False,
    )


def main_menu(is_owner: bool = False) -> str:
    rows = [
        [_text("👤 Профиль", "primary"),  _text("📢 Мои ресурсы", "primary")],
        [_text("👥 Рефералы", "primary"), _text("💰 Купить PP", "positive")],
        [_text("📋 Все беседы", "secondary"), _text("📊 Топ участников", "secondary")],
        [_text("📢 Поделиться", "secondary"), _text("➕ Добавить в беседу", "secondary")],
        [_text("ℹ️ Помощь", "secondary")],
    ]
    if is_owner:
        rows.append([_text("🔧 Админ-панель", "negative")])
    return _kb(rows)


def back_to_menu() -> str:
    return _kb([[_text("⬅️ В меню", "secondary")]])


def profile_kb() -> str:
    return _kb([[_text("🔄 Обновить", "primary"), _text("⬅️ В меню", "secondary")]])


def resources_kb() -> str:
    return _kb([[_text("➕ Добавить ресурс", "positive")],
                [_text("⬅️ В меню", "secondary")]])


def referrals_kb(bot_link: str) -> str:
    return _kb([
        [_link("📨 Поделиться в VK", "https://vk.com/share.php?url=" + bot_link)],
        [_text("📋 Моя ссылка", "primary"), _text("📝 Моя команда", "primary")],
        [_text("⬅️ В меню", "secondary")],
    ])


def share_kb(bot_link: str) -> str:
    return _kb([
        [_link("📨 Поделиться в VK", "https://vk.com/share.php?url=" + bot_link)],
        [_text("⬅️ В меню", "secondary")],
    ])


def buy_kb() -> str:
    return _kb([[_text("🚧 Скоро", "secondary")],
                [_text("⬅️ В меню", "secondary")]])


def chats_kb() -> str:
    return _kb([
        [_text("📍 Где я админ", "primary"), _text("🌐 Все беседы", "primary")],
        [_text("⬅️ В меню", "secondary")],
    ])


def top_kb() -> str:
    return _kb([[_text("🔄 Обновить", "primary"),
                 _text("⬅️ В меню", "secondary")]])


def help_kb(bot_link: str) -> str:
    return _kb([[_link("💬 Открыть бота в ЛС", bot_link)],
                [_text("⬅️ В меню", "secondary")]])


def add_chat_kb() -> str:
    return _kb([[_text("⬅️ В меню", "secondary")]])


# ---------- админ-панель ----------

def admin_menu() -> str:
    return _kb([
        [_text("🎁 Начислить PP", "primary"), _text("🚀 Автопромо", "primary")],
        [_text("📝 Автопост", "primary"), _text("🎯 Реклама", "primary")],
        [_text("📢 Рассылка", "primary"), _text("📣 В беседы", "primary")],
        [_text("📎 Экспорт", "primary")],
        [_text("⚙️ Настройки", "secondary"), _text("📊 Статистика", "secondary")],
        [_text("👤 Режим пользователя", "secondary")],
        [_text("⬅️ В меню", "secondary")],
    ])


def admin_back() -> str:
    return _kb([[_text("🔧 Админ-панель", "primary")]])


def promo_menu(enabled: bool) -> str:
    toggle_label = "🔴 Выключить промо" if enabled else "🟢 Включить промо"
    toggle_color = "negative" if enabled else "positive"
    return _kb([
        [_text(toggle_label, toggle_color)],
        [_text("✏️ Текст промо", "primary"), _text("⏱ Интервал", "primary")],
        [_text("🔧 Админ-панель", "secondary"), _text("⬅️ В меню", "secondary")],
    ])


def settings_menu() -> str:
    return _kb([
        [_text("📢 Канал бота", "primary"), _text("💬 Главная беседа", "primary")],
        [_text("🔧 Админ-панель", "secondary"), _text("⬅️ В меню", "secondary")],
    ])


def cancel_kb() -> str:
    return _kb([[_text("❌ Отмена", "negative")]])


def wallpost_menu(enabled: bool) -> str:
    toggle_label = "🔴 Выключить автопост" if enabled else "🟢 Включить автопост"
    toggle_color = "negative" if enabled else "positive"
    return _kb([
        [_text(toggle_label, toggle_color)],
        [_text("📤 Опубликовать сейчас", "primary")],
        [_text("✏️ Шаблон поста", "primary"), _text("⏱ Интервал поста", "primary")],
        [_text("🔧 Админ-панель", "secondary"), _text("⬅️ В меню", "secondary")],
    ])


def ad_menu(enabled: bool) -> str:
    toggle_label = "🔴 Выключить рекламу" if enabled else "🟢 Включить рекламу"
    toggle_color = "negative" if enabled else "positive"
    return _kb([
        [_text(toggle_label, toggle_color)],
        [_text("🧪 Тест рекламы", "primary"), _text("📝 Текст рекламы", "primary")],
        [_text("📋 Боты", "primary"), _text("🎯 Цели", "primary")],
        [_text("🤖 Имитация", "primary")],
        [_text("🔧 Админ-панель", "secondary"), _text("⬅️ В меню", "secondary")],
    ])


def ad_targets_kb() -> str:
    return _kb([
        [_text("➕ Добавить цель", "positive")],
        [_text("🔄 Обновить список", "primary"), _text("🗑 Удалить цель", "negative")],
        [_text("🎯 Реклама", "secondary"), _text("⬅️ В меню", "secondary")],
    ])


def ad_bots_kb() -> str:
    return _kb([
        [_text("➕ Добавить бота", "positive")],
        [_text("🔄 Проверить всех", "primary")],
        [_text("🗑 Удалить бота", "negative"), _text("🎯 Реклама", "secondary")],
        [_text("⬅️ В меню", "secondary")],
    ])


def ad_imitation_kb(enabled: bool) -> str:
    toggle = "🔴 Выключить имитацию" if enabled else "🟢 Включить имитацию"
    color = "negative" if enabled else "positive"
    return _kb([
        [_text(toggle, color)],
        [_text("👥 Боты (подписки)", "primary")],
        [_text("📊 Статистика имитации", "primary")],
        [_text("🎯 Реклама", "secondary"), _text("⬅️ В меню", "secondary")],
    ])


def ad_bot_add_kb() -> str:
    return _kb([
        [_text("🔑 Получить токен", "positive")],
        [_text("❌ Отмена", "negative")],
    ])


if __name__ == "__main__":
    print("main_menu:", main_menu(is_owner=True)[:80], "...")
    print("promo_menu(True):", promo_menu(True)[:80], "...")
    print("promo_menu(False):", promo_menu(False)[:80], "...")
    print("settings_menu:", settings_menu()[:80], "...")
