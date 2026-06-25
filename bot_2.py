"""
Emoji Mosaic Bot
=================
Бот режет присланную картинку на равные квадратные плитки,
создаёт из них набор кастомных эмодзи (custom emoji sticker set)
в Telegram и сразу отвечает сообщением, где эмодзи выставлены
в правильном порядке — при отправке они "собираются" в исходную картинку.

Требования:
- python-telegram-bot >= 21.0  (использует Bot API 7.x с custom emoji)
- Pillow

Установка:
    pip install python-telegram-bot==21.* Pillow --break-system-packages

Запуск:
    BOT_TOKEN=xxxx python3 bot.py

ВАЖНО про ограничения Telegram (см. README.md рядом с этим файлом):
- custom emoji видны полноценно только у пользователей с Telegram Premium.
  У остальных они отображаются как обычная (серая/плейсхолдер) картинка.
- createNewStickerSet с sticker_type="custom_emoji" создаёт набор,
  привязанный к боту, но "owner" (user_id) — это пользователь, который
  его создал; именно его user_id передаётся в user_id параметре.
- Имя сета должно быть уникальным и заканчиваться на "_by_<bot_username>".
- Каждый эмодзи-файл: PNG, ровно 100x100px, поддерживает прозрачность.
- Максимум 200 эмодзи в одном наборе.
"""

import asyncio
import io
import logging
import os
import re
import uuid
from typing import Dict, List, Optional, Tuple

from PIL import Image

from telegram import Update, InputSticker
from telegram.constants import StickerFormat
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("emoji_mosaic_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]  # обязательно задать перед запуском

EMOJI_TILE_SIZE = 100  # Telegram требует ровно 100x100 для custom emoji
DEFAULT_GRID = 5  # сетка NxN по умолчанию, если юзер не указал размер
MAX_GRID = 12  # 12x12=144 < лимита 200 эмодзи на набор, с запасом
MIN_GRID = 2

# Временное хранилище: ждём ли от юзера картинку или ждём размер сетки
# user_id -> photo_bytes
pending_images: Dict[int, bytes] = {}


def parse_grid_size(text: str) -> Optional[int]:
    """Парсит '5x5', '5', '5 5' -> 5. Возвращает None если не похоже на размер."""
    text = text.strip().lower().replace("х", "x")  # русская "х" -> латинская
    m = re.match(r"^(\d+)\s*x\s*(\d+)$", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a == b:
            return a
        return None  # пока поддерживаем только квадратные сетки NxN
    m = re.match(r"^(\d+)$", text)
    if m:
        return int(m.group(1))
    return None


def slice_image_to_tiles(image_bytes: bytes, grid: int) -> List[Image.Image]:
    """
    Приводит картинку к квадрату (с полями, без обрезки контента),
    режет на grid x grid равных квадратных тайлов размером
    EMOJI_TILE_SIZE x EMOJI_TILE_SIZE каждый.

    Возвращает список тайлов в порядке слева-справа, сверху-вниз
    (т.е. построчно — именно так их и нужно расставлять в тексте,
    чтобы они "собрались" в картинку).
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    # 1) Приводим изображение к квадрату, добавляя прозрачные поля
    #    (а не обрезая) — так весь исходный рисунок остаётся видимым.
    side = max(img.width, img.height)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    paste_x = (side - img.width) // 2
    paste_y = (side - img.height) // 2
    square.paste(img, (paste_x, paste_y), img)

    # 2) Масштабируем квадрат до размера, кратного размеру тайла,
    #    чтобы резка была идеально равной без округлений.
    full_size = grid * EMOJI_TILE_SIZE
    square = square.resize((full_size, full_size), Image.LANCZOS)

    # 3) Режем на grid x grid одинаковых квадратов
    tiles: List[Image.Image] = []
    for row in range(grid):
        for col in range(grid):
            left = col * EMOJI_TILE_SIZE
            top = row * EMOJI_TILE_SIZE
            tile = square.crop(
                (left, top, left + EMOJI_TILE_SIZE, top + EMOJI_TILE_SIZE)
            )
            tiles.append(tile)
    return tiles


def tile_to_png_bytes(tile: Image.Image) -> bytes:
    buf = io.BytesIO()
    tile.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


# Список эмодзи-"тегов", которые Telegram требует указать для каждого
# custom emoji (это не то, что видно пользователю — видно саму картинку,
# тег используется только для поиска/категоризации внутри Telegram).
# Берём с запасом, по кругу, не важно какой именно символ.
PLACEHOLDER_EMOJIS = ["🟦", "🟩", "🟥", "🟨", "🟪", "🟧", "⬜", "⬛"]


async def create_emoji_set_from_tiles(
    bot,
    user_id: int,
    tiles: List[Image.Image],
) -> Tuple[str, List[str]]:
    """
    Создаёт новый custom emoji sticker set из переданных тайлов.
    Возвращает (short_name набора, [custom_emoji_id, ...]) в том же
    порядке, что и tiles (построчно слева-справа/сверху-вниз).
    """
    bot_user = await bot.get_me()
    # short_name должен быть уникальным и заканчиваться на _by_<bot_username>
    unique = uuid.uuid4().hex[:10]
    set_name = f"mosaic_{unique}_by_{bot_user.username}"
    set_title = "Photo Mosaic"

    input_stickers = []
    for i, tile in enumerate(tiles):
        png_bytes = tile_to_png_bytes(tile)
        emoji_char = PLACEHOLDER_EMOJIS[i % len(PLACEHOLDER_EMOJIS)]
        input_stickers.append(
            InputSticker(
                sticker=png_bytes,
                emoji_list=[emoji_char],
                format=StickerFormat.STATIC,
            )
        )

    # Telegram Bot API ограничивает createNewStickerSet 50 стикерами за раз
    # для некоторых клиентов; чтобы быть безопасными, создаём набор первой
    # пачкой, а остальное добавляем через addStickerToSet.
    first_batch, rest = input_stickers[:50], input_stickers[50:]

    await bot.create_new_sticker_set(
        user_id=user_id,
        name=set_name,
        title=set_title,
        stickers=first_batch,
        sticker_type="custom_emoji",
    )

    for sticker in rest:
        await bot.add_sticker_to_set(
            user_id=user_id,
            name=set_name,
            sticker=sticker,
        )
        # небольшая пауза, чтобы не упереться в rate limit Telegram
        await asyncio.sleep(0.3)

    # Получаем итоговый набор, чтобы вытащить custom_emoji_id каждого стикера
    sticker_set = await bot.get_sticker_set(set_name)
    custom_emoji_ids = [s.custom_emoji_id for s in sticker_set.stickers]
    return set_name, custom_emoji_ids


async def send_mosaic_message(
    bot, chat_id: int, custom_emoji_ids: List[str], grid: int
):
    """
    Отправляет сообщение, где каждый плейсхолдер-символ подменён
    на custom emoji entity, указывающий на нужный custom_emoji_id.
    Результат — картинка, собранная из кусочков, прямо в чате.
    """
    from telegram import MessageEntity

    text_chars = []
    entities = []
    offset = 0

    for row in range(grid):
        row_ids = custom_emoji_ids[row * grid : (row + 1) * grid]
        for emoji_id in row_ids:
            ch = "🟦"  # placeholder char; client рендерит custom emoji по entity
            text_chars.append(ch)
            entities.append(
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=offset,
                    length=len(ch),
                    custom_emoji_id=emoji_id,
                )
            )
            offset += len(ch)
        text_chars.append("\n")
        offset += 1

    text = "".join(text_chars).rstrip("\n")
    await bot.send_message(chat_id=chat_id, text=text, entities=entities)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли мне картинку — я нарежу её на квадратики, "
        "сделаю из них набор кастомных эмодзи и сразу пришлю собранную "
        "мозаику обратно прямо в чат.\n\n"
        "После картинки можешь написать размер сетки, например 5x5 "
        f"(от {MIN_GRID}x{MIN_GRID} до {MAX_GRID}x{MAX_GRID}). "
        f"Если не укажешь — возьму {DEFAULT_GRID}x{DEFAULT_GRID}.\n\n"
        "⚠️ Учти: красиво кастомные эмодзи выглядят только у пользователей "
        "с Telegram Premium. У остальных вместо мозаики будет серый плейсхолдер."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]  # наибольшее доступное разрешение
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    pending_images[user_id] = bytes(photo_bytes)

    await update.message.reply_text(
        f"Картинка получена. Напиши размер сетки (например 5x5, "
        f"от {MIN_GRID} до {MAX_GRID}), либо просто слово 'старт' "
        f"для сетки по умолчанию {DEFAULT_GRID}x{DEFAULT_GRID}."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id not in pending_images:
        await update.message.reply_text("Сначала пришли картинку 🙂")
        return

    if text.lower() in ("старт", "start", "go"):
        grid = DEFAULT_GRID
    else:
        grid = parse_grid_size(text)
        if grid is None:
            await update.message.reply_text(
                "Не понял размер сетки. Напиши, например, 5x5 или просто 5."
            )
            return
        if not (MIN_GRID <= grid <= MAX_GRID):
            await update.message.reply_text(
                f"Размер сетки должен быть от {MIN_GRID} до {MAX_GRID}."
            )
            return

    photo_bytes = pending_images.pop(user_id)
    status_msg = await update.message.reply_text(
        f"Режу картинку на {grid}x{grid} и создаю набор эмодзи, это займёт немного времени…"
    )

    try:
        tiles = slice_image_to_tiles(photo_bytes, grid)
        set_name, custom_emoji_ids = await create_emoji_set_from_tiles(
            context.bot, user_id, tiles
        )
        await send_mosaic_message(context.bot, update.effective_chat.id, custom_emoji_ids, grid)
        await status_msg.edit_text(
            f"Готово! Набор эмодзи создан: t.me/addemoji/{set_name}\n"
            f"Мозаика выше — если не видна красиво, нужен Telegram Premium "
            f"у того, кто смотрит чат."
        )
    except Exception as e:
        log.exception("Ошибка при создании мозаики")
        await status_msg.edit_text(
            "Что-то пошло не так при создании набора эмодзи: "
            f"{e}\n\nПопробуй картинку поменьше или сетку поменьше."
        )


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
