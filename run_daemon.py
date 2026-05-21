"""Опциональный фоновый процесс — публикует story раз в день в заданное время.

Альтернатива cron: запускайте этот скрипт как long-running процесс
(например, через `nohup python run_daemon.py &` или systemd).

Конфигурация через переменные окружения / .env:
    POST_HOUR=10        # час публикации (0-23), по умолчанию 10
    POST_MINUTE=0       # минута, по умолчанию 0
    TIMEZONE=Europe/Moscow  # часовой пояс, по умолчанию системный
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_daemon")


def next_run(hour: int, minute: int, tz: ZoneInfo) -> datetime:
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def main() -> int:
    hour = int(os.getenv("POST_HOUR", "10"))
    minute = int(os.getenv("POST_MINUTE", "0"))
    tz_name = os.getenv("TIMEZONE", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        log.warning("Неизвестный часовой пояс %s — использую UTC.", tz_name)
        tz = ZoneInfo("UTC")

    log.info("Демон запущен. Расписание: каждый день в %02d:%02d (%s)", hour, minute, tz_name)

    while True:
        target = next_run(hour, minute, tz)
        sleep_seconds = (target - datetime.now(tz)).total_seconds()
        log.info(
            "Следующая публикация: %s (через %.0f мин)",
            target.isoformat(timespec="seconds"),
            sleep_seconds / 60,
        )
        time.sleep(max(sleep_seconds, 1))

        log.info("Запуск post_story.py...")
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "post_story.py")],
            cwd=PROJECT_ROOT,
        )
        log.info("post_story.py завершился с кодом %d", result.returncode)
        # На случай дребезга — спим минуту, чтобы не зациклиться на той же минуте
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
