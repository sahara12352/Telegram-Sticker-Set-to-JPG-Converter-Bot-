import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from PIL import Image
import io
import os
import requests
import zipfile
import tempfile
import logging
import re
from telegram import Update
from typing import Final

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.ERROR)

logger = logging.getLogger(__name__)

# Ваш токен
BOT_TOKEN: Final[str] = 'ВАШ ТОКЕН'
MAX_STICKERS_TO_PROCESS: Final[int] = 100
ZIP_FILENAME: Final[str] = "sticker_set_jpg_archive.zip"

MAX_STICKER_SIZE_BYTES: Final[int] = 5 * 1024 * 1024
UPDATE_FREQUENCY: Final[int] = 5


async def error_handler(update: Update, context: CallbackContext) -> None:
    """Обрабатывает ошибки, возникающие при обработке обновлений."""
    logger.error("Произошла ошибка при обработке обновления %s", update, exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text(
            'Произошла внутренняя ошибка. Попробуйте отправить стикер или ссылку еще раз.'
        )


def sanitize_filename(name: str) -> str:
    """БЕЗОПАСНОСТЬ: Очищает строку для использования в качестве имени файла или папки."""
    name = re.sub(r'[^\w\s-]', '', name).strip()
    name = re.sub(r'[-\s]+', '_', name)
    return name[:50]


def get_sticker_set_name(update: Update) -> str | None:
    """Извлекает имя набора стикеров из сообщения и ОЧИЩАЕТ его."""
    name = None

    if update.message and update.message.sticker and update.message.sticker.set_name:
        name = update.message.sticker.set_name
    elif update.message and update.message.text:
        text = update.message.text.strip()
        if 't.me/addstickers/' in text:
            name = text.split('/')[-1]
        else:
            name = text

    return sanitize_filename(name) if name else None


async def start(update: Update, _context: CallbackContext) -> None:
    """Отправляет приветственное сообщение при команде /start."""
    if update.message:
        await update.message.reply_text(
            'Привет! Отправьте мне **любой стикер** из набора или **ссылку** на набор. Я конвертирую стикеры в JPG и отправлю в ZIP-архиве.',
            parse_mode='Markdown')


async def convert_to_jpg_and_archive(update: Update, context: CallbackContext) -> None:
    """
    Основной обработчик: извлекает стикеры, конвертирует, архивирует и отправляет.
    """
    if not update.message:
        return

    chat_id = update.message.chat_id
    sticker_set_name = get_sticker_set_name(update)

    if not sticker_set_name:
        await update.message.reply_text("Не удалось определить набор стикеров. Отправьте стикер или ссылку.")
        return

    status_message = await update.message.reply_text(
        f'Начинаю обработку набора: <b>{sticker_set_name}</b>. Получение данных...',
        parse_mode='HTML'
    )
    status_message_id = status_message.message_id

    try:
        sticker_set = await context.bot.get_sticker_set(sticker_set_name)
        stickers = sticker_set.stickers[:MAX_STICKERS_TO_PROCESS]
    except telegram.error.BadRequest:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message_id,
            text=f"Набор стикеров '{sticker_set_name}' не найден. Проверьте правильность имени или ссылки."
        )
        return

    total_stickers = len(stickers)
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_message_id,
        text=f"Найдено {total_stickers} стикеров. Начинаю конвертацию..."
    )

    temp_zip = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    zip_path = temp_zip.name
    temp_zip.close()

    processed_count = 0
    skipped_large_count = 0

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:

            for i, sticker in enumerate(stickers):

                if (i + 1) % UPDATE_FREQUENCY == 0 or (i + 1) == total_stickers:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_message_id,
                            text=f"Конвертирую стикеры... Обработано {i + 1} из {total_stickers}."
                        )
                    except Exception as edit_e:
                        logger.warning(f"Ошибка при обновлении статус сообщения: {edit_e}")

                try:

                    if sticker.file_size and sticker.file_size > MAX_STICKER_SIZE_BYTES:
                        skipped_large_count += 1
                        continue

                    telegram_file = await context.bot.get_file(sticker.file_id)
                    file_url = telegram_file.file_path
                    response = requests.get(file_url)
                    sticker_bytes = io.BytesIO(response.content)

                    file_name = f"{sticker_set_name}_{i + 1}.jpg"

                    img = Image.open(sticker_bytes)

                    if img.format == 'GIF':
                        img.seek(0)
                        frame = img.convert('RGBA')
                        img = frame

                    if img.mode in ('RGBA', 'P'):
                        background = Image.new("RGB", img.size, (255, 255, 255))
                        background.paste(img, (0, 0), img)
                        img = background
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')

                    output_jpg = io.BytesIO()
                    img.save(output_jpg, format='JPEG', quality=95)
                    output_jpg.seek(0)

                    # Добавление JPG-файла в ZIP-архив
                    zf.writestr(file_name, output_jpg.read())
                    processed_count += 1

                except Exception as e:
                    logger.error(f"Критическая ошибка при обработке стикера {i + 1}. Пропуск. Ошибка: {e}")
                    continue

        final_caption = f"Архив JPG-изображений ({processed_count} шт.) для набора '{sticker_set_name}'"

        if processed_count == 0:
            message = "Не удалось обработать ни один стикер. "
            if skipped_large_count > 0:
                message += f"Пропущено {skipped_large_count} стикеров из-за большого размера."
            else:
                message += "Возможно, все стикеры в формате TGS (Lottie)."

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=message
            )
        else:
            try:

                with open(zip_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=f"{sticker_set_name}_{ZIP_FILENAME}",
                        caption=final_caption
                    )

                await context.bot.delete_message(chat_id=chat_id, message_id=status_message_id)

            except Exception as e:

                logger.error(f"Ошибка при отправке документа: {e}")
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message_id,
                    text=f"Произошла ошибка при отправке архива: {e}"
                )


    except (telegram.error.TelegramError, OSError, requests.exceptions.RequestException) as general_e:
        logger.error(f"Общая ошибка в обработчике: {general_e}")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=f"Произошла ошибка при обработке: {general_e}"
            )
        except telegram.error.TelegramError:
            await update.message.reply_text(f"Произошла критическая ошибка: {general_e}")

    finally:

        if os.path.exists(zip_path):
            os.unlink(zip_path)


async def post_init(application: Application) -> None:
    """Устанавливает описание команд для отображения в меню Telegram."""
    commands = [

        telegram.BotCommand("start", "чтобы использовать нажми на меня (отправь стикер или ссылку)"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Команды бота успешно установлены.")


def main() -> None:
    """Запускает бота в режиме Polling."""

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.Sticker.ALL | filters.TEXT & ~filters.COMMAND, convert_to_jpg_and_archive))

    application.add_error_handler(error_handler)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()