"""Ежедневная публикация Instagram Story с Link-стикером.

Скрипт:
1. Подключается к Instagram (восстанавливает сессию или логинится по логину/паролю)
2. Выбирает фото из папки media/ (случайно или по алфавиту)
3. При необходимости — изменяет размер до 1080x1920
4. Публикует фото как story с Link-стикером
5. Перемещает выложенное фото в posted/

Запуск:
    python post_story.py                 # обычный режим
    python post_story.py --dry-run       # без реальной публикации
    python post_story.py --image foo.jpg # конкретное фото
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

try:
    from instagrapi import Client
    from instagrapi.exceptions import (
        BadPassword,
        ChallengeRequired,
        LoginRequired,
        TwoFactorRequired,
    )
    from instagrapi.types import StoryLink, StorySticker
except ImportError as exc:
    print(
        "Не удалось импортировать instagrapi. Установите зависимости:\n"
        "    pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

PROJECT_ROOT = Path(__file__).resolve().parent


def setup_logging(level: str, log_file: str | None) -> logging.Logger:
    logger = logging.getLogger("post_story")
    logger.setLevel(level.upper())
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_file:
        log_path = PROJECT_ROOT / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Не найден файл конфигурации: {config_path}. "
            "Скопируйте config.json из репозитория."
        )
    with config_path.open(encoding="utf-8") as f:
        return json.load(f)


def get_credentials(logger: logging.Logger) -> tuple[str, str, str | None, str | None]:
    """Загружает учётные данные из .env."""
    import os

    load_dotenv(PROJECT_ROOT / ".env")
    username = os.getenv("IG_USERNAME", "").strip()
    password = os.getenv("IG_PASSWORD", "").strip()
    totp_seed = os.getenv("IG_TOTP_SEED", "").strip() or None
    proxy = os.getenv("IG_PROXY", "").strip() or None

    if not username or not password:
        logger.error(
            "В .env не заданы IG_USERNAME / IG_PASSWORD. "
            "Скопируйте .env.example в .env и заполните."
        )
        sys.exit(2)

    return username, password, totp_seed, proxy


def pick_image(media_dir: Path, selection: str, logger: logging.Logger) -> Path:
    candidates = [
        p
        for p in sorted(media_dir.iterdir())
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    if not candidates:
        logger.error("Папка %s пуста — нет фото для публикации.", media_dir)
        sys.exit(3)

    if selection == "random":
        chosen = random.choice(candidates)
    else:  # "alphabetical"
        chosen = candidates[0]

    logger.info("Выбрано фото: %s", chosen.name)
    return chosen


def prepare_image(src: Path, max_w: int, max_h: int, auto_resize: bool, logger: logging.Logger) -> Path:
    """Если нужно — конвертирует в JPEG и подгоняет под формат stories."""
    if not auto_resize:
        return src

    img = Image.open(src)
    img = img.convert("RGB") if img.mode != "RGB" else img

    w, h = img.size
    target_ratio = max_w / max_h
    src_ratio = w / h

    if src_ratio > target_ratio:
        new_w = max_w
        new_h = int(max_w / src_ratio)
    else:
        new_h = max_h
        new_w = int(max_h * src_ratio)

    if new_w != w or new_h != h:
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        logger.info("Изменён размер: %dx%d -> %dx%d", w, h, new_w, new_h)

    # Создаём холст 1080x1920 с чёрным фоном и центрируем изображение
    canvas = Image.new("RGB", (max_w, max_h), (0, 0, 0))
    offset_x = (max_w - new_w) // 2
    offset_y = (max_h - new_h) // 2
    canvas.paste(img, (offset_x, offset_y))

    tmp_dir = PROJECT_ROOT / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    out = tmp_dir / f"prepared_{src.stem}.jpg"
    canvas.save(out, "JPEG", quality=92)
    logger.debug("Подготовленное фото: %s", out)
    return out


def login(
    username: str,
    password: str,
    totp_seed: str | None,
    proxy: str | None,
    session_file: Path,
    logger: logging.Logger,
) -> Client:
    cl = Client()
    if proxy:
        cl.set_proxy(proxy)
        logger.info("Используется прокси: %s", proxy.split("@")[-1])

    session_file.parent.mkdir(parents=True, exist_ok=True)

    # Попытка восстановить сессию
    if session_file.exists():
        try:
            cl.load_settings(session_file)
            cl.login(username, password)
            cl.get_timeline_feed()  # быстрая проверка сессии
            logger.info("Сессия восстановлена из %s", session_file)
            return cl
        except LoginRequired:
            logger.warning("Сессия устарела, перелогиниваемся.")
        except Exception as exc:
            logger.warning("Не удалось восстановить сессию: %s. Перелогиниваемся.", exc)
            cl = Client()
            if proxy:
                cl.set_proxy(proxy)

    # Свежий логин
    try:
        if totp_seed:
            verification_code = cl.totp_generate_code(totp_seed)
            cl.login(username, password, verification_code=verification_code)
        else:
            cl.login(username, password)
    except TwoFactorRequired:
        logger.error(
            "Для аккаунта включена 2FA, но IG_TOTP_SEED не задан. "
            "Получите TOTP-секрет из настроек Instagram и пропишите его в .env."
        )
        sys.exit(4)
    except BadPassword:
        logger.error("Неверный логин или пароль.")
        sys.exit(4)
    except ChallengeRequired:
        logger.error(
            "Instagram требует прохождения challenge (подтверждение по почте/SMS). "
            "Залогиньтесь вручную в браузере с того же IP и попробуйте снова."
        )
        sys.exit(4)

    cl.dump_settings(session_file)
    logger.info("Сессия сохранена в %s", session_file)
    return cl


def upload_story(
    cl: Client,
    image_path: Path,
    caption: str,
    link_url: str | None,
    link_text: str,
    logger: logging.Logger,
) -> object:
    links: list[StoryLink] = []
    stickers: list[StorySticker] = []
    if link_url:
        if link_text:
            # Кастомный link-стикер с произвольным текстом
            # (instagrapi не поддерживает text через StoryLink, поэтому конструируем StorySticker напрямую)
            link_sticker = StorySticker(
                type="story_link",
                x=0.5,
                y=0.5,
                z=0,
                width=0.51,
                height=0.07,
                rotation=0.0,
                extra=dict(
                    link_type="web",
                    url=str(link_url),
                    link_text=link_text,
                    tap_state_str_id="link_sticker_default",
                ),
            )
            stickers.append(link_sticker)
            logger.info("Добавлен Link-стикер: %s (текст: %s)", link_url, link_text)
        else:
            links.append(StoryLink(webUri=link_url))
            logger.info("Добавлен Link-стикер: %s", link_url)

    logger.info("Загружаем story (%s)...", image_path.name)
    media = cl.photo_upload_to_story(
        path=image_path,
        caption=caption or "",
        links=links,
        stickers=stickers,
    )
    logger.info(
        "Story успешно опубликована. id=%s, code=%s",
        media.pk,
        getattr(media, "code", "?"),
    )
    return media


def archive_image(src: Path, posted_dir: Path, logger: logging.Logger) -> None:
    posted_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = posted_dir / f"{timestamp}_{src.name}"
    shutil.move(str(src), str(dest))
    logger.info("Фото перемещено в %s", dest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Публикация Instagram Story с Link-стикером.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только подготовить фото и логин, без реальной публикации.",
    )
    parser.add_argument(
        "--image",
        type=str,
        default=None,
        help="Путь к конкретному фото (вместо случайного выбора).",
    )
    args = parser.parse_args()

    cfg = load_config()
    logger = setup_logging(
        cfg.get("logging", {}).get("level", "INFO"),
        cfg.get("logging", {}).get("file"),
    )
    logger.info("=== Запуск post_story (%s) ===", datetime.now().isoformat(timespec="seconds"))

    media_dir = PROJECT_ROOT / cfg["media_dir"]
    posted_dir = PROJECT_ROOT / cfg["posted_dir"]
    session_file = PROJECT_ROOT / cfg["session_file"]

    media_dir.mkdir(parents=True, exist_ok=True)
    posted_dir.mkdir(parents=True, exist_ok=True)

    # 1. Выбор фото
    if args.image:
        src = Path(args.image)
        if not src.is_absolute():
            src = PROJECT_ROOT / src
        if not src.exists():
            logger.error("Файл не найден: %s", src)
            return 3
    else:
        src = pick_image(media_dir, cfg.get("selection", "random"), logger)

    # 2. Подготовка изображения
    image_cfg = cfg.get("image", {})
    prepared = prepare_image(
        src,
        image_cfg.get("max_width", 1080),
        image_cfg.get("max_height", 1920),
        image_cfg.get("auto_resize", True),
        logger,
    )

    # 3. Логин (включая dry-run — чтобы проверить креды и сессию)
    username, password, totp_seed, proxy = get_credentials(logger)
    cl = login(username, password, totp_seed, proxy, session_file, logger)

    if args.dry_run:
        logger.info(
            "[DRY-RUN] Логин успешен, фото подготовлено: %s. Реальная публикация пропущена.",
            prepared,
        )
        return 0

    # 4. Публикация
    link_cfg = cfg.get("link_sticker", {})
    link_url = link_cfg.get("url") if link_cfg.get("enabled") else None
    link_text = link_cfg.get("sticker_text") or ""

    try:
        upload_story(
            cl,
            prepared,
            cfg.get("caption", ""),
            link_url,
            link_text,
            logger,
        )
    except Exception as exc:
        logger.exception("Ошибка при публикации story: %s", exc)
        return 5

    # 5. Перенос фото в архив
    if cfg.get("move_after_post", True) and not args.image:
        archive_image(src, posted_dir, logger)

    return 0


if __name__ == "__main__":
    sys.exit(main())
