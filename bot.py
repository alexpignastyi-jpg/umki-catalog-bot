import os
import json
import logging
import io
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from PIL import Image

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_TOKEN   = os.environ.get("GOOGLE_TOKEN", "")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

genai.configure(api_key=GEMINI_API_KEY)


def get_sheets_service():
    if not GOOGLE_TOKEN:
        raise RuntimeError("GOOGLE_TOKEN не задан!")
    token_data = json.loads(GOOGLE_TOKEN)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("sheets", "v4", credentials=creds)


def append_row(row):
    service = get_sheets_service()
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Sheet1!A:F",
        valueInputOption="RAW",
        body={"values": [row]}
    ).execute()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👜 Привет! Я бот для каталога сумок.\n\n"
        "Отправь фото сумки — заполню таблицу автоматически.\n\n"
        "Можешь добавить подпись:\n"
        "Артикул: XXX\nЦена: $XX\nЦвета: чёрный, белый"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Анализирую фото...")
    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        raw = await tg_file.download_as_bytearray()
        img = Image.open(io.BytesIO(bytes(raw)))
        caption = update.message.caption or ""

        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Ты эксперт по сумкам. Проанализируй фото и подпись пользователя.\n"
            f"Подпись: {caption if caption else 'отсутствует'}\n\n"
            "Верни ТОЛЬКО валидный JSON без markdown:\n"
            '{\n'
            '  "artikul": "артикул или не указан",\n'
            '  "model": "название сумки",\n'
            '  "razmer": "размер или не указан",\n'
            '  "cena": "цена числом или не указана",\n'
            '  "cveta": "цвета через запятую",\n'
            '  "opisanie": "продающее описание на русском 2-3 предложения"\n'
            '}'
        )

        response = model.generate_content([prompt, img])
        response_text = response.text.strip()

        if "```" in response_text:
            parts = response_text.split("```")
            response_text = parts[1].lstrip("json").strip()

        data = json.loads(response_text)

        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{tg_file.file_path}"

        row = [
            "",
            photo_url,
            data.get("model", ""),
            data.get("artikul", ""),
            data.get("cena", ""),
            data.get("opisanie", ""),
        ]
        append_row(row)

        reply = (
            f"✅ Добавлено в таблицу!\n\n"
            f"📦 Артикул: {data.get('artikul', 'не указан')}\n"
            f"👜 Модель: {data.get('model', '')}\n"
            f"📐 Размер: {data.get('razmer', 'не указан')}\n"
            f"💰 Цена: {data.get('cena', 'не указана')} $\n"
            f"🎨 Цвета: {data.get('cveta', '')}\n\n"
            f"📝 {data.get('opisanie', '')}"
        )
        await msg.edit_text(reply)

    except json.JSONDecodeError:
        await msg.edit_text("⚠️ ИИ не смог разобрать фото. Попробуй ещё раз.")
    except Exception as e:
        logger.exception("Ошибка")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Отправь фото сумки.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
