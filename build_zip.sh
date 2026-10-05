#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"

NAME="piar-bot-vk"
STAMP=$(date +%Y%m%d-%H%M)
OUT="$HOME/${NAME}-${STAMP}.zip"
TMP=$(mktemp -d)

echo "📦 Собираю архив..."
mkdir -p "$TMP/$NAME/handlers" "$TMP/$NAME/data" "$TMP/$NAME/logs"

for f in bot.py config.py db.py utils.py keyboards.py background.py \
         ad.py watch.py run.sh db_migrations.py requirements.txt README.md .env.example .gitignore; do
    [ -f "$f" ] && cp "$f" "$TMP/$NAME/" && echo "  + $f"
done

for f in handlers/__init__.py handlers/private.py handlers/chat.py handlers/admin.py; do
    [ -f "$f" ] && cp "$f" "$TMP/$NAME/handlers/" && echo "  + $f"
done

echo "# placeholder" > "$TMP/$NAME/data/.keep"
echo "# placeholder" > "$TMP/$NAME/logs/.keep"

cd "$TMP"
zip -r "$OUT" "$NAME" > /dev/null
cd - > /dev/null
rm -rf "$TMP"

echo "✅ Готово: $OUT"
ls -lh "$OUT"
