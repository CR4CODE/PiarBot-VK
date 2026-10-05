#!/data/data/com.termux/files/usr/bin/bash
# make_release.sh — собирает чистый zip для переноса на ПК

set -e
cd "$(dirname "$0")"

REL_NAME="piar-bot-vk-$(date +%Y%m%d)"
ZIP_NAME="${REL_NAME}.zip"
TMP_DIR="$HOME/tmp/${REL_NAME}"

echo "📦 Собираю релиз..."

mkdir -p "$HOME/tmp"
rm -rf "$TMP_DIR" "$ZIP_NAME"
mkdir -p "$TMP_DIR/handlers"
mkdir -p "$TMP_DIR/data"
mkdir -p "$TMP_DIR/logs"

# Код
cp bot.py           "$TMP_DIR/"
cp config.py        "$TMP_DIR/"
cp db.py            "$TMP_DIR/"
cp utils.py         "$TMP_DIR/"
cp background.py    "$TMP_DIR/"
cp keyboards.py     "$TMP_DIR/"
cp watch.py         "$TMP_DIR/"
cp run.sh           "$TMP_DIR/"

# Обработчики
cp handlers/__init__.py "$TMP_DIR/handlers/"
cp handlers/private.py  "$TMP_DIR/handlers/"
cp handlers/chat.py     "$TMP_DIR/handlers/"
cp handlers/admin.py    "$TMP_DIR/handlers/"

# Шаблоны и инструкции
cp requirements.txt "$TMP_DIR/"
cp .env.example     "$TMP_DIR/"
cp README.md        "$TMP_DIR/"
cp .gitignore        "$TMP_DIR/"

# Пустые папки для данных и логов
touch "$TMP_DIR/data/.gitkeep"
touch "$TMP_DIR/logs/.gitkeep"

# ZIP
cd "$HOME/tmp"
zip -r "$ZIP_NAME" "$REL_NAME" > /dev/null
mv "$ZIP_NAME" "$OLDPWD/"

echo "✅ Готово: $(pwd)/$ZIP_NAME"
echo ""
echo "📊 Содержимое:"
unzip -l "$ZIP_NAME" | tail -30
