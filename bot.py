import os
import json
import base64
import logging
from dotenv import load_dotenv

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Temporarily store parsed data while awaiting confirmation
# key = telegram user_id, value = dict with parsed invoice data
pending_invoices: dict = {}


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file("google_credentials.json", scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)


def ensure_sheets(spreadsheet):
    """Create required sheets with headers if they don't exist."""
    existing = [ws.title for ws in spreadsheet.worksheets()]

    if "📋 Расходы" not in existing:
        ws = spreadsheet.add_worksheet("📋 Расходы", rows=1000, cols=10)
        ws.append_row([
            "№", "Дата", "Поставщик", "Товар", "Артикул",
            "Кол-во", "Цена", "Сумма", "Накладная №", "Примечание"
        ])
        ws.format("A1:J1", {"textFormat": {"bold": True}})

    if "💵 Снятия" not in existing:
        ws = spreadsheet.add_worksheet("💵 Снятия", rows=500, cols=5)
        ws.append_row(["№", "Дата", "Откуда снято", "Сумма", "Примечание"])
        ws.format("A1:E1", {"textFormat": {"bold": True}})


def write_invoice_to_sheet(data: dict):
    """Write each line item as a separate row in 📋 Расходы."""
    spreadsheet = get_sheet()
    ensure_sheets(spreadsheet)
    ws = spreadsheet.worksheet("📋 Расходы")

    existing_rows = len(ws.get_all_values())
    rows_to_add = []

    items = data.get("items", [])
    if not items:
        items = [{"name": "—", "article": "—", "qty": "—", "price": "—", "total": data.get("total", "—")}]

    for i, item in enumerate(items):
        row_num = existing_rows + i
        rows_to_add.append([
            row_num,
            data.get("date", ""),
            data.get("supplier", ""),
            item.get("name", ""),
            item.get("article", ""),
            item.get("qty", ""),
            item.get("price", ""),
            item.get("total", ""),
            data.get("invoice_number", ""),
            "",
        ])

    ws.append_rows(rows_to_add)


# ── Gemini API ────────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """Ты помощник для распознавания товарных накладных и чеков.

Извлеки из изображения следующие данные и верни ТОЛЬКО валидный JSON без каких-либо пояснений, без markdown, без ```json.

Формат ответа:
{
  "supplier": "название поставщика или ИП",
  "date": "дата в формате ДД.ММ.ГГГГ",
  "invoice_number": "номер накладной или чека",
  "total": "итоговая сумма числом без пробелов, например 4150.00",
  "items": [
    {
      "name": "название товара",
      "article": "артикул если есть",
      "qty": "количество числом",
      "price": "цена за единицу числом",
      "total": "сумма по позиции числом"
    }
  ]
}

Если какое-то поле не читается — поставь пустую строку "".
Дату всегда пиши в формате ДД.ММ.ГГГГ.
Суммы пиши только цифрами без символов валюты и пробелов."""


async def parse_invoice_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Send image to Gemini Flash and extract structured invoice data."""
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": image_b64,
                        }
                    },
                    {"text": EXTRACT_PROMPT},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 2048,
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()

    result = response.json()
    raw_text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Strip markdown fences if model added them anyway
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1]
        raw_text = raw_text.rsplit("```", 1)[0]

    return json.loads(raw_text)


# ── Formatting helpers ────────────────────────────────────────────────────────

def format_invoice_message(data: dict) -> str:
    lines = [
        "📄 <b>Распознанная накладная</b>",
        "",
        f"🏪 <b>Поставщик:</b> {data.get('supplier') or '—'}",
        f"📅 <b>Дата:</b> {data.get('date') or '—'}",
        f"🔢 <b>Накладная №:</b> {data.get('invoice_number') or '—'}",
        "",
        "<b>Товары:</b>",
    ]

    for i, item in enumerate(data.get("items", []), 1):
        name    = item.get("name", "—")
        article = item.get("article", "")
        qty     = item.get("qty", "")
        price   = item.get("price", "")
        total   = item.get("total", "")

        article_str = f" [{article}]" if article else ""
        detail = f"{qty} шт × {price} = {total}" if qty and price else total
        lines.append(f"  {i}. {name}{article_str} — {detail}")

    lines += [
        "",
        f"💰 <b>Итого: {data.get('total') or '—'} руб.</b>",
        "",
        "Всё верно? Записать в таблицу?",
    ]
    return "\n".join(lines)


def confirmation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Записать", callback_data=f"confirm:{user_id}"),
            InlineKeyboardButton("✏️ Исправить итог", callback_data=f"edit_total:{user_id}"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{user_id}")],
    ])


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для учёта закупок.\n\n"
        "📸 Скинь фото накладной или чека — я распознаю данные и запишу в Google Sheets.\n\n"
        "Команды:\n"
        "/start — это сообщение\n"
        "/help — помощь"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 <b>Как пользоваться:</b>\n\n"
        "1. Сфотографируй накладную или экран с чеком\n"
        "2. Отправь фото в этот чат\n"
        "3. Я извлеку: поставщик, дата, товары, суммы\n"
        "4. Проверь и нажми ✅ Записать\n"
        "5. Данные появятся в Google Sheets\n\n"
        "💡 Совет: весь документ должен быть в кадре, текст в фокусе.",
        parse_mode="HTML"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = await update.message.reply_text("🔍 Распознаю накладную, подождите...")

    try:
        # Get highest resolution photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(file.file_path)
            image_bytes = resp.content

        data = await parse_invoice_image(image_bytes)
        pending_invoices[user_id] = data

        text = format_invoice_message(data)
        keyboard = confirmation_keyboard(user_id)
        await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except json.JSONDecodeError:
        await msg.edit_text(
            "⚠️ Не удалось разобрать ответ. "
            "Попробуй сделать фото чётче и прислать снова."
        )
    except Exception as e:
        logger.exception("Error processing photo")
        await msg.edit_text(f"❌ Ошибка: {e}\n\nПопробуй ещё раз.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, uid_str = query.data.split(":", 1)
    uid = int(uid_str)

    if action == "confirm":
        data = pending_invoices.pop(uid, None)
        if not data:
            await query.edit_message_text("⚠️ Данные не найдены. Пришли фото ещё раз.")
            return
        try:
            write_invoice_to_sheet(data)
            await query.edit_message_text(
                f"✅ <b>Записано в таблицу!</b>\n\n"
                f"🏪 {data.get('supplier')} — {data.get('date')}\n"
                f"💰 Итого: {data.get('total')} руб.\n"
                f"📦 Позиций: {len(data.get('items', []))}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.exception("Error writing to sheet")
            await query.edit_message_text(f"❌ Ошибка записи в таблицу: {e}")

    elif action == "edit_total":
        context.user_data["awaiting_total_fix"] = uid
        await query.edit_message_text(
            "✏️ Введи правильную итоговую сумму (только цифры, например: 4150.00)"
        )

    elif action == "cancel":
        pending_invoices.pop(uid, None)
        await query.edit_message_text("❌ Отменено. Можешь прислать новое фото.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.user_data.get("awaiting_total_fix") == user_id:
        new_total = update.message.text.strip().replace(",", ".")
        try:
            float(new_total)
        except ValueError:
            await update.message.reply_text("⚠️ Введи число, например: 4150.00")
            return

        if user_id in pending_invoices:
            pending_invoices[user_id]["total"] = new_total
            context.user_data.pop("awaiting_total_fix", None)
            data = pending_invoices[user_id]
            text = format_invoice_message(data)
            keyboard = confirmation_keyboard(user_id)
            await update.message.reply_text(
                f"✅ Итог обновлён: {new_total} руб.\n\n" + text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text("⚠️ Данные не найдены. Пришли фото заново.")
        return

    await update.message.reply_text("📸 Пришли фото накладной или чека.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан в .env")
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не задан в .env")
    if not GOOGLE_SHEET_ID:
        raise ValueError("GOOGLE_SHEET_ID не задан в .env")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
