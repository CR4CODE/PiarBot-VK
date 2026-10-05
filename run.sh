#!/data/data/com.termux/files/usr/bin/bash
# run.sh — управление PiarBot VK

set -u
cd "$(dirname "$0")"

PIDFILE="data/bot.pid"
LOGFILE="logs/bot.log"
PYTHON="python"

mkdir -p data logs

# ---------- helpers ----------

is_running() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null)
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

get_pid() {
    cat "$PIDFILE" 2>/dev/null
}

# ---------- commands ----------

cmd_start() {
    if is_running; then
        echo "⚠️  Бот уже запущен (PID $(get_pid))"
        return 1
    fi

    echo "🚀 Запускаю PiarBot VK..."
    termux-wake-lock 2>/dev/null || true

    nohup "$PYTHON" bot.py >> "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    sleep 2

    if kill -0 "$pid" 2>/dev/null; then
        echo "✅ Запущен, PID $pid"
        echo "   Лог: $(pwd)/$LOGFILE"
        echo "   Хвост: ./run.sh log"
    else
        echo "❌ Не поднялся. Смотри лог:"
        tail -20 "$LOGFILE"
        rm -f "$PIDFILE"
        return 1
    fi
}

cmd_stop() {
    if ! is_running; then
        echo "ℹ️  Бот не работает"
        rm -f "$PIDFILE"
        return 0
    fi

    local pid
    pid=$(get_pid)
    echo "🛑 Останавливаю PID $pid..."
    kill "$pid" 2>/dev/null
    for i in 1 2 3 4 5; do
        sleep 1
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "⚠️  Не отвечает — убиваю жёстко"
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    termux-wake-unlock 2>/dev/null || true
    echo "✅ Остановлен"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    if is_running; then
        echo "🟢 Работает, PID $(get_pid)"
        echo "   Лог: $(pwd)/$LOGFILE"
    else
        echo "🔴 Не работает"
        rm -f "$PIDFILE" 2>/dev/null
    fi
}

cmd_log() {
    if [ ! -f "$LOGFILE" ]; then
        echo "ℹ️  Лог пуст: $LOGFILE"
        return
    fi
    tail -f "$LOGFILE"
}

cmd_foreground() {
    echo "▶️  Запуск в текущей сессии (Ctrl+C для стопа)"
    termux-wake-lock 2>/dev/null || true
    "$PYTHON" bot.py
}

cmd_help() {
    cat <<HELP
PiarBot VK — управление

  ./run.sh start       запустить в фоне (nohup + wake-lock)
  ./run.sh stop        остановить
  ./run.sh restart     перезапустить
  ./run.sh status      проверить, работает ли
  ./run.sh log         смотреть лог (Ctrl+C чтобы выйти)
  ./run.sh foreground  запустить в этой сессии (для отладки)
  ./run.sh watch       запустить сторожа (авто-перезапуск bot.py)
  ./run.sh unwatch     остановить сторожа
  ./run.sh help        эта справка
HELP
}

# ---------- watch ----------

cmd_watch() {
    if [ -f "data/watch.pid" ]; then
        wpid=$(cat "data/watch.pid" 2>/dev/null)
        if [ -n "$wpid" ] && kill -0 "$wpid" 2>/dev/null; then
            echo "⚠️  Сторож уже работает (PID $wpid)"
            return 1
        fi
    fi
    echo "👁  Запускаю сторожа..."
    termux-wake-lock 2>/dev/null || true
    nohup "$PYTHON" watch.py >> "logs/watch.out" 2>&1 &
    wpid=$!
    echo "$wpid" > "data/watch.pid"
    sleep 2
    if kill -0 "$wpid" 2>/dev/null; then
        echo "✅ Сторож запущен, PID $wpid"
        echo "   Лог сторожа: logs/watch.log"
    else
        echo "❌ Сторож не поднялся"
        rm -f "data/watch.pid"
        return 1
    fi
}

cmd_unwatch() {
    if [ ! -f "data/watch.pid" ]; then
        echo "ℹ️  Сторож не работает"
        return 0
    fi
    wpid=$(cat "data/watch.pid" 2>/dev/null)
    if [ -n "$wpid" ] && kill -0 "$wpid" 2>/dev/null; then
        echo "🛑 Останавливаю сторожа PID $wpid..."
        kill "$wpid" 2>/dev/null
        sleep 1
        kill -9 "$wpid" 2>/dev/null
    fi
    rm -f "data/watch.pid"
    echo "✅ Сторож остановлен"
}

case "${1:-help}" in
    start)      cmd_start ;;
    stop)       cmd_stop ;;
    restart)    cmd_restart ;;
    status)     cmd_status ;;
    log)        cmd_log ;;
    foreground|fg) cmd_foreground ;;
    watch)      cmd_watch ;;
    unwatch)    cmd_unwatch ;;
    help|-h|--help) cmd_help ;;
    *)
        echo "❓ Неизвестная команда: $1"
        cmd_help
        exit 1
        ;;
esac
