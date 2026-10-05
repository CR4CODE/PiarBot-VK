# watch.py — сторож: перезапускает bot.py при падении
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
PIDFILE = ROOT / "data" / "bot.pid"
LOGFILE = ROOT / "logs" / "bot.log"
WATCHLOG = ROOT / "logs" / "watch.log"
CHECK_INTERVAL = 10          # сек между проверками
RESTART_DELAY = 5            # пауза перед перезапуском
MAX_RESTARTS_PER_HOUR = 20   # защита от цикла падений

WATCHLOG.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watch] %(message)s",
    handlers=[logging.FileHandler(WATCHLOG, encoding="utf-8")],
)
log = logging.getLogger("watch")


def read_pid() -> int | None:
    try:
        if PIDFILE.exists():
            return int(PIDFILE.read_text().strip())
    except Exception:
        return None
    return None


def is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_bot() -> int | None:
    log.info("🚀 поднимаю bot.py")
    with open(LOGFILE, "ab") as f:
        proc = subprocess.Popen(
            [sys.executable, "bot.py"],
            cwd=str(ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PIDFILE.write_text(str(proc.pid))
    log.info("✅ bot.py запущен, PID %s", proc.pid)
    return proc.pid


def main() -> None:
    log.info("👁  watch.py стартовал")
    restarts: list[float] = []

    while True:
        pid = read_pid()
        if not is_alive(pid):
            now = time.time()
            restarts = [t for t in restarts if now - t < 3600]
            if len(restarts) >= MAX_RESTARTS_PER_HOUR:
                log.error("💥 %s перезапусков за час — стоп",
                          MAX_RESTARTS_PER_HOUR)
                return
            if pid:
                log.warning("⚠️  PID %s умер", pid)
            time.sleep(RESTART_DELAY)
            start_bot()
            restarts.append(time.time())

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("🛑 watch.py остановлен вручную")
