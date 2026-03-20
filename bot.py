import os
import json
import base64
import logging
 
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import gspread
from google.oauth2.service_account import Credentials
 
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
 
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("API_TOKEN")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
 
pending_invoices: dict = {}
 
 
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)
 
 
def ensure_sheets(spreadsheet):
    existing = [ws.title for ws in spreadsheet.worksheets()]
    if "Расходы" not in existing:
        ws = spreadsheet.add_worksheet("Расходы", rows=1000, cols=10)
        ws.append_row([
            "№", "Дата", "Поставщик", "Товар", "Артикул",
            "Кол-во", "Цена", "Сумма", "Накладная №", "Примечание"
        ])
    if "Снятия" not in existing:
        ws = spreadsheet.add_worksheet("Снятия", rows=500, cols=5)
        ws.append_row(["№", "Дата", "Откуда снято", "Сумма", "Примечание"])
 
 
def write_invoice_to_sheet(data: dict):
    spreadsheet = get_sheet()
    ensure_sheets(spreadsheet)
    ws = spreadsheet.worksheet("Расходы")
    existing_rows = len(ws.get_all_values())
    rows_to_add = []
    items = data.get("items", [])
    if not items:
        items = [{"name": "—", "article": "—", "qty": "—", "price": "—", "total": data.get("total", "—")}]
    for i, item in enumerate(items):
        rows_to_add.append([
            existing_rows + i,
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
 
 
EXTRACT_PROMPT = """Извлеки данные из накладной/чека и верни ТОЛЬКО валидный JSON без markdown.
 
{
  "supplier": "поставщик",
  "date": "ДД.ММ.ГГГГ",
  "invoice_number": "номер",
  "total": "сумма числом например 4150.00",
  "items": [
    {"name": "товар", "article": "артикул", "qty": "кол-во", "price": "цена", "total": "сумма"}
  ]
}
 
Пустые поля — пустая строка. Суммы только цифрами."""
 
 
async def parse_invoice_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": mime_type, "data": image_b64}},
            {"text": EXTRACT_PROMPT},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
    result = response.json()
    raw_text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(raw_text)
 
 
def format_invoice_message(data: dict) -> str:
    lines = [
        "📄 <b>Распознанная накладная</b>", "",
        f"🏪 <b>Поставщик:</b> {data.get('supplier') or '—'}",
        f"📅 <b>Дата:</b> {data.get('date') or '—'}",
        f"🔢 <b>Накладная №:</b> {data.get('invoice_number') or '—'}",
        "", "<b>Товары:</b>",
    ]
    for i, item in enumerate(data.get("items", []), 1):
        name = item.get("name", "—")
        article = f" [{item.get('article')}]" if item.get("article") else ""
        qty = item.get("qty", "")
        price = item.get("price", "")
        total = item.get("total", "")
        detail = f"{qty} шт × {price} = {total}" if qty and price else total
        lines.append(f"  {i}. {name}{article} — {detail}")
    lines += ["", f"💰 <b>Итого: {data.get('total') or '—'} руб.</b>", "", "Всё верно? Записать в таблицу?"]
    return "\n".join(lines)
 
 
def confirmation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Записать", callback_data=f"confirm:{user_id}"),
            InlineKeyboardButton("✏️ Исправить итог", callback_data=f"edit_total:{user_id}"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{user_id}")],
    ])
 
 
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для учёта закупок.\n\n"
        "📸 Скинь фото накладной — распознаю и запишу в Google Sheets.\n\n"
        "/help — помощь"
    )
 
 
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 <b>Как пользоваться:</b>\n\n"
        "1. Сфотографируй накладную или экран с чеком\n"
        "2. Отправь фото в этот чат\n"
        "3. Проверь данные и нажми ✅ Записать\n"
        "4. Данные появятся в Google Sheets\n\n"
        "💡 Весь документ должен быть в кадре.",
        parse_mode="HTML"
    )
 
 
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = await update.message.reply_text("🔍 Распознаю накладную, подождите...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(file.file_path)
            image_bytes = resp.content
        data = await parse_invoice_image(image_bytes)
        pending_invoices[user_id] = data
        await msg.edit_text(format_invoice_message(data), parse_mode="HTML", reply_markup=confirmation_keyboard(user_id))
    except json.JSONDecodeError:
        await msg.edit_text("⚠️ Не удалось разобрать ответ. Попробуй сделать фото чётче.")
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
                f"✅ <b>Записано!</b>\n\n🏪 {data.get('supplier')} — {data.get('date')}\n"
                f"💰 Итого: {data.get('total')} руб.\n📦 Позиций: {len(data.get('items', []))}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.exception("Error writing to sheet")
            await query.edit_message_text(f"❌ Ошибка записи: {e}")
    elif action == "edit_total":
        context.user_data["awaiting_total_fix"] = uid
        await query.edit_message_text("✏️ Введи правильную итоговую сумму (например: 4150.00)")
    elif action == "cancel":
        pending_invoices.pop(uid, None)
        await query.edit_message_text("❌ Отменено.")
 
 
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
            await update.message.reply_text(
                f"✅ Итог обновлён: {new_total} руб.\n\n" + format_invoice_message(data),
                parse_mode="HTML", reply_markup=confirmation_keyboard(user_id)
            )
        else:
            await update.message.reply_text("⚠️ Данные не найдены. Пришли фото заново.")
        return
    await update.message.reply_text("📸 Пришли фото накладной или чека.")
 
 
def main():
    token = TELEGRAM_TOKEN
    if not token:
        raise ValueError("TELEGRAM_TOKEN или API_TOKEN не задан")
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не задан")
    if not GOOGLE_SHEET_ID:
        raise ValueError("GOOGLE_SHEET_ID не задан")
    if not GOOGLE_CREDENTIALS:
        raise ValueError("GOOGLE_CREDENTIALS не задан")
 
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
 
    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)
 
 
if __name__ == "__main__":
    main()
