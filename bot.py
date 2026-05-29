import os
import json
import logging
import base64
import time
import io
import requests
from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

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

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash-lite:generateContent?key={key}"
)


def compress_image(raw_bytes: bytes, max_size: int = 1024) -> bytes:
    """Сжимает изображение до max_size пикселей по длинной стороне."""
    img = Image.open(io.BytesIO(raw_bytes))
    img = img.convert("RGB")
    w, h = img.size
    if w > max_size or h > max_size:
        ratio = min(max_size / w, max_size / h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    compressed = buf.getvalue()
    logger.info(f"Фото сжато: {len(raw_bytes)//1024}КБ → {len(compressed)//1024}КБ")
    return compressed


def call_gemini(prompt: str, image_bytes: bytes) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": image_b64
                        }
                    }
                ]
            }
        ]
    }
    url = GEMINI_URL.format(key=GEMINI_API_KEY)

    for attempt in range(3):
        resp = requests.post(url, json=body, timeout=60)
        if resp.status_code == 429:
            wait = 20 * (attempt + 1)
            logger.warning(f"429 лимит, жду {wait} сек (попытка {attempt+1}/3)...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        result = resp.json()
        return result["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError(
        "Gemini API временно недоступен (превышен лимит запросов). "
        "Подожди минуту и отправь фото снова."
    )


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
        caption = update.message.caption or ""

        # Сжимаем фото перед отправкой в Gemini
        compressed = compress_image(bytes(raw))

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

        response_text = call_gemini(prompt, compressed)
        response_text = response_text.strip()

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
    except RuntimeError as e:
        await msg.edit_text(f"⏳ {e}")
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
