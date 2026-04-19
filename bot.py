import os
import json
import base64
import re
import logging
from datetime import datetime
from pathlib import Path

import anthropic
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
Application, CommandHandler, MessageHandler,
filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(**name**)

# ── CONFIG ──────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ[“ANTHROPIC_API_KEY”]
TELEGRAM_TOKEN    = os.environ[“TELEGRAM_TOKEN”]
ALLOWED_USER      = os.environ.get(“ALLOWED_USER”, “”)  # твой @username без @

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

RULES_FILE = “/data/rules.json”
Path(”/data”).mkdir(exist_ok=True)

# ── СПРАВОЧНИКИ ──────────────────────────────────────────────

EXPENSE_CATEGORIES = [
“Без категории”,
“Продукты”, “Еда вне дома”,
“GO OUT”, “Развлечения на выходных”, “Подписки/моб”, “Одежда”,
“Покупки разные”, “Благотворительность”, “Подарки”, “комиссии”,
“Юристы/налоги/тп”, “Обучение”, “Психотерапевт личный”, “Психотерапевт семейный”,
“Поездки в РФ и обратно”, “Отдых”,
“Аренда”, “Коммуналка Кипр”, “Улучшение жилья”, “Коты”, “Коммуналка Быково”, “Уборка”,
“Родителям”, “БИЗНЕС”, “WB”, “Алексей Ч”,
“Одежда Алиса”, “Кружки”, “Покупки Алисе”, “Няня”, “Подарки на др”, “Здоровье”, “Школа”,
“Аптека, iHerb”, “Врачи”, “Спорт”, “Relax”,
“Топливо”, “Штрафы”, “Обслуживание авто”,
]

INCOME_SOURCES = [
“АЧ на меня”, “АЧ на Алису”, “Консалтинг”, “Кураторство”,
“Аренда”, “Продажа чего-то”, “Дивиденды impact”, “ХЗ”,
]

ALL_CATS = EXPENSE_CATEGORIES + INCOME_SOURCES

ACCOUNTS = [
“Revolut”, “Revolut отдельный”, “Мой кошелёк”, “СберБанк”, “Т-Банк”,
“Альфа дебет”, “Сейф домашний”, “СберБанк Под Бизнес”,
“Revolut Физ Лица impact”, “Альфа кредитка”, “Сбербанк тайный”,
]

# ── STATE PER USER ────────────────────────────────────────────

sessions = {}  # user_id -> {“transactions”: [], “images”: [], “pending_idx”: None}

def load_rules():
if os.path.exists(RULES_FILE):
with open(RULES_FILE) as f:
return json.load(f)
return {}

def save_rules(rules):
with open(RULES_FILE, “w”) as f:
json.dump(rules, f, ensure_ascii=False, indent=2)

def norm_key(merchant):
return re.sub(r”[^a-zA-Zа-яёА-ЯЁ0-9]”, “”, merchant.lower())[:30]

def get_session(user_id):
if user_id not in sessions:
sessions[user_id] = {“transactions”: [], “images”: [], “rules”: load_rules()}
return sessions[user_id]

# ── PARSE SCREENSHOTS ─────────────────────────────────────────

async def parse_images(images, rules):
cats_str = “, “.join(ALL_CATS)
prompt = f””“Ты парсер банковских выписок.
На скриншотах транзакции из банковских приложений (Revolut, Т-Банк, Сбер, Альфа).
Извлеки ВСЕ видимые транзакции.

Для каждой верни JSON объект:

- id: порядковый номер
- date: “YYYY-MM-DD HH:MM:00”
- merchant: название мерчанта/контрагента
- amount: число (отрицательное=расход, положительное=приход)
- currency: EUR, USD или руб
- account: название счёта если видно, иначе пустая строка
- type: expense, income, или transfer
- suspect_transfer: true если похоже на перевод между своими счетами
- suggested_category: ТОЛЬКО из: {cats_str}
  McDonald/KFC->Еда вне дома, Metro/супермаркет->Продукты,
  Apple/Google/Netflix->Подписки/моб, АЗС->Топливо,
  переводы между кошельками Revolut->type=transfer,
  если не уверен->Без категории

Верни ТОЛЬКО валидный JSON массив. Без markdown.”””

```
content = [{"type": "text", "text": prompt}]
for img in images:
    content.append({
        "type": "image",
        "source": {"type": "base64", "media_type": img["mime"], "data": img["data"]}
    })

resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4000,
    messages=[{"role": "user", "content": content}]
)

raw = resp.content[0].text.replace("```json", "").replace("```", "").strip()
parsed = json.loads(raw)

transactions = []
for tx in parsed:
    key = norm_key(tx.get("merchant", ""))
    saved = rules.get(key)
    tx["category"] = saved or tx.get("suggested_category", "Без категории")
    tx["auto"] = bool(saved)
    tx["skipped"] = False
    tx["is_transfer"] = tx.get("type") == "transfer"
    tx["transfer_to"] = ""
    transactions.append(tx)

return transactions
```

# ── VOICE COMMAND HANDLER ─────────────────────────────────────

async def handle_voice_command(text, session):
transactions = session[“transactions”]
rules = session[“rules”]

```
prompt = f"""Ты помощник для управления списком банковских транзакций.
```

Текущие транзакции (JSON):
{json.dumps(transactions, ensure_ascii=False, indent=1)}

Доступные категории расходов: {”, “.join(EXPENSE_CATEGORIES)}
Доступные источники доходов: {”, “.join(INCOME_SOURCES)}
Доступные счета: {”, “.join(ACCOUNTS)}

Пользователь говорит: “{text}”

Пойми что пользователь хочет сделать и верни JSON с командами:
{{
“actions”: [
{{“type”: “set_category”, “id”: N, “category”: “Название”}},
{{“type”: “set_transfer”, “id”: N, “from_account”: “Счёт”, “to_account”: “Счёт”}},
{{“type”: “set_exchange”, “id_out”: N, “id_in”: M, “comment”: “текст”}},
{{“type”: “skip”, “id”: N}},
{{“type”: “unskip”, “id”: N}}
],
“reply”: “Короткий ответ пользователю что сделано (по-русски)”
}}

Важно:

- set_exchange — это обмен валюты между своими счетами (две транзакции: расход и приход)
- Если пользователь говорит что N рублей = M евро и это его счета — это set_exchange
- Категория должна быть ТОЧНО из списка или не указывай её
- Верни ТОЛЬКО валидный JSON”””
  
  resp = client.messages.create(
  model=“claude-sonnet-4-6”,
  max_tokens=1000,
  messages=[{“role”: “user”, “content”: prompt}]
  )
  
  raw = resp.content[0].text.replace(”`json", "").replace("`”, “”).strip()
  result = json.loads(raw)
  
  # Apply actions
  
  for action in result.get(“actions”, []):
  t = action.get(“type”)
  idx = action.get(“id”, -1) - 1  # convert to 0-based
  
  ```
    if t == "set_category" and 0 <= idx < len(transactions):
        cat = action.get("category")
        if cat in ALL_CATS:
            transactions[idx]["category"] = cat
            transactions[idx]["skipped"] = False
            transactions[idx]["is_transfer"] = False
            key = norm_key(transactions[idx]["merchant"])
            rules[key] = cat
  
    elif t == "set_transfer" and 0 <= idx < len(transactions):
        transactions[idx]["is_transfer"] = True
        transactions[idx]["transfer_to"] = action.get("to_account", "")
        transactions[idx]["skipped"] = False
  
    elif t == "set_exchange":
        id_out = action.get("id_out", -1) - 1
        id_in = action.get("id_in", -1) - 1
        if 0 <= id_out < len(transactions):
            transactions[id_out]["is_transfer"] = True
            transactions[id_out]["is_exchange"] = True
            transactions[id_out]["exchange_pair"] = id_in
            transactions[id_out]["skipped"] = False
        if 0 <= id_in < len(transactions):
            transactions[id_in]["is_transfer"] = True
            transactions[id_in]["is_exchange"] = True
            transactions[id_in]["exchange_pair"] = id_out
            transactions[id_in]["skipped"] = False
  
    elif t == "skip" and 0 <= idx < len(transactions):
        transactions[idx]["skipped"] = True
  
    elif t == "unskip" and 0 <= idx < len(transactions):
        transactions[idx]["skipped"] = False
  ```
  
  save_rules(rules)
  return result.get(“reply”, “Готово”)

# ── GENERATE CSV ──────────────────────────────────────────────

def generate_csv(transactions):
lines = []
processed_exchanges = set()

```
for i, tx in enumerate(transactions):
    if tx["skipped"]:
        continue

    amt = tx.get("amount", 0)
    cur = tx.get("currency", "EUR")
    date = tx.get("date", "")
    merchant = str(tx.get("merchant", "")).replace(";", ",").replace('"', "'")
    account = tx.get("account", "") or "Revolut"

    if tx.get("is_exchange") and i not in processed_exchanges:
        pair_idx = tx.get("exchange_pair", -1)
        if pair_idx >= 0 and pair_idx < len(transactions):
            pair = transactions[pair_idx]
            processed_exchanges.add(i)
            processed_exchanges.add(pair_idx)
            # outgoing
            abs_out = abs(tx["amount"]) if tx["amount"] < 0 else abs(pair["amount"])
            cur_out = tx["currency"] if tx["amount"] < 0 else pair["currency"]
            acc_out = tx.get("account") or "Revolut"
            # incoming
            abs_in = abs(pair["amount"]) if pair["amount"] > 0 else abs(tx["amount"])
            cur_in = pair["currency"] if pair["amount"] > 0 else tx["currency"]
            acc_in = pair.get("account") or acc_out
            lines.append(f"-{abs_out};{cur_out};{acc_out};{acc_out};{date};{merchant}")
            lines.append(f"{abs_in};{cur_in};{acc_in};{acc_in};{date};{merchant}")
        continue

    if i in processed_exchanges:
        continue

    if tx["is_transfer"] and tx.get("transfer_to"):
        abs_amt = abs(amt)
        to = tx["transfer_to"]
        lines.append(f"-{abs_amt};{cur};{to};{account};{date};{merchant}")
        lines.append(f"{abs_amt};{cur};{account};{to};{date};{merchant}")
    else:
        lines.append(f"{amt};{cur};{tx['category']};{account};{date};{merchant}")

return "\n".join(lines)
```

def format_tx_list(transactions):
lines = [“📋 *Транзакции:*\n”]
for i, tx in enumerate(transactions, 1):
if tx[“skipped”]:
status = “⏭ пропущено”
elif tx.get(“is_exchange”):
status = “💱 обмен”
elif tx[“is_transfer”]:
status = f”⇄ → {tx.get(‘transfer_to’, ‘?’)}”
else:
cat = tx[“category”]
status = f”{‘✅’ if cat != ‘Без категории’ else ‘❓’} {cat}”

```
    flag = "⚠️" if tx.get("suspect_transfer") and not tx["is_transfer"] and not tx["skipped"] else ""
    amt = tx.get("amount", 0)
    cur = tx.get("currency", "")
    merchant = tx.get("merchant", "")[:25]
    date = str(tx.get("date", ""))[:10]
    lines.append(f"{flag}*{i}.* {merchant} | {amt} {cur} | {date}\n   → {status}")

pending = sum(1 for t in transactions if not t["skipped"] and t["category"] == "Без категории" and not t["is_transfer"])
if pending:
    lines.append(f"\n❓ Без категории: *{pending}* шт.")
else:
    lines.append("\n✅ Все категоризованы!")

return "\n".join(lines)
```

# ── TELEGRAM HANDLERS ─────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
“👋 Привет! Я помогу загрузить выписки в Дребеденьги.\n\n”
“📸 Отправь скриншоты банковских выписок (можно несколько)\n”
“Когда отправишь все — напиши /parse\n\n”
“Голосовые тоже принимаю 🎤”
)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
session = get_session(user_id)

```
photo = update.message.photo[-1]
file = await ctx.bot.get_file(photo.file_id)
data = await file.download_as_bytearray()
b64 = base64.standard_b64encode(data).decode()
session["images"].append({"mime": "image/jpeg", "data": b64})

count = len(session["images"])
await update.message.reply_text(f"📸 Получила скриншот #{count}. Отправь ещё или напиши /parse для разбора.")
```

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
session = get_session(user_id)

```
doc = update.message.document
if not doc.mime_type.startswith("image/"):
    await update.message.reply_text("Пожалуйста, отправляй только изображения.")
    return

file = await ctx.bot.get_file(doc.file_id)
data = await file.download_as_bytearray()
b64 = base64.standard_b64encode(data).decode()
session["images"].append({"mime": doc.mime_type, "data": b64})

count = len(session["images"])
await update.message.reply_text(f"📸 Получила скриншот #{count}. Отправь ещё или напиши /parse для разбора.")
```

async def parse_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
session = get_session(user_id)

```
if not session["images"]:
    await update.message.reply_text("Сначала отправь скриншоты 📸")
    return

await update.message.reply_text(f"🤖 Разбираю {len(session['images'])} скриншот(ов)... Подожди 20-30 секунд.")

try:
    transactions = await parse_images(session["images"], session["rules"])
    session["transactions"] = transactions
    session["images"] = []

    text = format_tx_list(transactions)
    await update.message.reply_text(text, parse_mode="Markdown")
    await update.message.reply_text(
        "Теперь можешь:\n"
        "🎤 Голосом объяснить что исправить\n"
        "✍️ Написать текстом\n"
        "/csv — скачать файл\n"
        "/reset — начать заново\n\n"
        "Пример: _«транзакция 3 — это обмен, 25000 рублей равно 275 евро от Юлии»_",
        parse_mode="Markdown"
    )
except Exception as e:
    await update.message.reply_text(f"❌ Ошибка: {str(e)}")
```

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
session = get_session(user_id)

```
if not session["transactions"]:
    await update.message.reply_text("Сначала отправь скриншоты и напиши /parse")
    return

await update.message.reply_text("🎤 Слушаю...")

voice = update.message.voice
file = await ctx.bot.get_file(voice.file_id)
voice_data = await file.download_as_bytearray()
b64 = base64.standard_b64encode(voice_data).decode()

# Transcribe with Claude
try:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "audio/ogg", "data": b64}
                },
                {"type": "text", "text": "Транскрибируй это голосовое сообщение на русском языке. Верни только текст без пояснений."}
            ]
        }]
    )
    text = resp.content[0].text.strip()
    await update.message.reply_text(f"📝 Распознала: _{text}_", parse_mode="Markdown")
except Exception:
    await update.message.reply_text("Не смогла распознать голос. Напиши текстом.")
    return

await process_command(update, session, text)
```

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
session = get_session(user_id)
text = update.message.text

```
if text.startswith("/"):
    return

if not session["transactions"]:
    await update.message.reply_text("Сначала отправь скриншоты и напиши /parse 📸")
    return

await process_command(update, session, text)
```

async def process_command(update, session, text):
await update.message.reply_text(“⚙️ Обрабатываю…”)
try:
reply = await handle_voice_command(text, session)
await update.message.reply_text(f”✅ {reply}”)

```
    # Show updated list
    tx_text = format_tx_list(session["transactions"])
    await update.message.reply_text(tx_text, parse_mode="Markdown")
except Exception as e:
    await update.message.reply_text(f"❌ Ошибка: {str(e)}")
```

async def csv_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
session = get_session(user_id)

```
if not session["transactions"]:
    await update.message.reply_text("Нет транзакций. Сначала отправь скриншоты.")
    return

csv_content = generate_csv(session["transactions"])
active = [t for t in session["transactions"] if not t["skipped"]]
no_cat = sum(1 for t in active if t["category"] == "Без категории")

fname = f"drebedengi_{datetime.now().strftime('%Y-%m-%d')}.csv"
with open(f"/tmp/{fname}", "w", encoding="utf-8") as f:
    f.write(csv_content)

summary = f"📊 Транзакций: {len(active)}\n❓ Без категории: {no_cat}"
if no_cat > 0:
    summary += "\n\n⚠️ Есть транзакции без категории — они попадут в «Без категории» в Дребеденьгах."

await update.message.reply_text(summary)
await ctx.bot.send_document(
    chat_id=update.effective_chat.id,
    document=open(f"/tmp/{fname}", "rb"),
    filename=fname,
    caption="Загрузи этот файл в Дребеденьги → Меню → Импорт данных"
)
```

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
sessions[user_id] = {“transactions”: [], “images”: [], “rules”: load_rules()}
await update.message.reply_text(“🔄 Начинаем заново. Отправь скриншоты 📸”)

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
session = get_session(user_id)
rules = load_rules()

```
await update.message.reply_text(
    f"📊 Статус:\n"
    f"Скриншотов в очереди: {len(session['images'])}\n"
    f"Транзакций: {len(session['transactions'])}\n"
    f"Правил категоризации: {len(rules)}"
)
```

def main():
app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler(“start”, start))
app.add_handler(CommandHandler(“parse”, parse_cmd))
app.add_handler(CommandHandler(“csv”, csv_cmd))
app.add_handler(CommandHandler(“reset”, reset_cmd))
app.add_handler(CommandHandler(“status”, status_cmd))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

```
logger.info("Bot started")
app.run_polling()
```

if **name** == “**main**”:
main()
