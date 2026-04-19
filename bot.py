import os
import json
import base64
import re
import logging
from datetime import datetime
from pathlib import Path

import anthropic
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
TELEGRAM_TOKEN    = os.environ['TELEGRAM_TOKEN']

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

RULES_FILE = '/data/rules.json'
Path('/data').mkdir(exist_ok=True)

EXPENSE_CATEGORIES = [
    'Без категории',
    'Продукты', 'Еда вне дома',
    'GO OUT', 'Развлечения на выходных', 'Подписки/моб', 'Одежда',
    'Покупки разные', 'Благотворительность', 'Подарки', 'комиссии',
    'Юристы/налоги/тп', 'Обучение', 'Психотерапевт личный', 'Психотерапевт семейный',
    'Поездки в РФ и обратно', 'Отдых',
    'Аренда', 'Коммуналка Кипр', 'Улучшение жилья', 'Коты', 'Коммуналка Быково', 'Уборка',
    'Родителям', 'БИЗНЕС', 'WB', 'Алексей Ч',
    'Одежда Алиса', 'Кружки', 'Покупки Алисе', 'Няня', 'Подарки на др', 'Здоровье', 'Школа',
    'Аптека, iHerb', 'Врачи', 'Спорт', 'Relax',
    'Топливо', 'Штрафы', 'Обслуживание авто',
]

INCOME_SOURCES = [
    'АЧ на меня', 'АЧ на Алису', 'Консалтинг', 'Кураторство',
    'Аренда', 'Продажа чего-то', 'Дивиденды impact', 'ХЗ',
]

ALL_CATS = EXPENSE_CATEGORIES + INCOME_SOURCES

ACCOUNTS = [
    'Revolut', 'Revolut отдельный', 'Мой кошелёк', 'СберБанк', 'Т-Банк',
    'Альфа дебет', 'Сейф домашний', 'СберБанк Под Бизнес',
    'Revolut Физ Лица impact', 'Альфа кредитка', 'Сбербанк тайный',
]

sessions = {}

def load_rules():
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            return json.load(f)
    return {}

def save_rules(rules):
    with open(RULES_FILE, 'w') as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)

def norm_key(merchant):
    return re.sub(r'[^a-zA-Za-яёА-ЯЁ0-9]', '', merchant.lower())[:30]

def get_session(user_id):
    if user_id not in sessions:
        sessions[user_id] = {'transactions': [], 'images': [], 'rules': load_rules()}
    return sessions[user_id]

async def parse_images(images, rules):
    cats_str = ', '.join(ALL_CATS)
    prompt = (
        'Ты парсер банковских выписок.\n'
        'На скриншотах транзакции из банковских приложений (Revolut, Т-Банк, Сбер, Альфа).\n'
        'Извлеки ВСЕ видимые транзакции.\n\n'
        'Для каждой верни JSON объект:\n'
        '- id: порядковый номер\n'
        '- date: YYYY-MM-DD HH:MM:00\n'
        '- merchant: название мерчанта или контрагента\n'
        '- amount: число (отрицательное=расход, положительное=приход)\n'
        '- currency: EUR, USD или руб\n'
        '- account: название счёта если видно, иначе пустая строка\n'
        '- type: expense, income, или transfer\n'
        '- suspect_transfer: true если похоже на перевод между своими счетами\n'
        '- suggested_category: ТОЛЬКО из списка: ' + cats_str + '\n'
        '  McDonald/KFC/кафе -> Еда вне дома\n'
        '  Metro/супермаркет/Lidl -> Продукты\n'
        '  Apple/Google/Netflix -> Подписки/моб\n'
        '  АЗС/бензин -> Топливо\n'
        '  переводы между кошельками Revolut -> type=transfer\n'
        '  если не уверен -> Без категории\n\n'
        'Верни ТОЛЬКО валидный JSON массив. Без markdown.'
    )

    content = [{'type': 'text', 'text': prompt}]
    for img in images:
        content.append({
            'type': 'image',
            'source': {'type': 'base64', 'media_type': img['mime'], 'data': img['data']}
        })

    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=4000,
        messages=[{'role': 'user', 'content': content}]
    )

    raw = resp.content[0].text.replace('```json', '').replace('```', '').strip()
    parsed = json.loads(raw)

    transactions = []
    for tx in parsed:
        key = norm_key(tx.get('merchant', ''))
        saved = rules.get(key)
        tx['category'] = saved or tx.get('suggested_category', 'Без категории')
        tx['auto'] = bool(saved)
        tx['skipped'] = False
        tx['is_transfer'] = tx.get('type') == 'transfer'
        tx['is_exchange'] = False
        tx['exchange_pair'] = -1
        tx['transfer_to'] = ''
        transactions.append(tx)

    return transactions

async def handle_voice_command(text, session):
    transactions = session['transactions']
    rules = session['rules']

    tx_json = json.dumps(transactions, ensure_ascii=False, indent=1)
    cats_exp = ', '.join(EXPENSE_CATEGORIES)
    cats_inc = ', '.join(INCOME_SOURCES)
    accounts_str = ', '.join(ACCOUNTS)

    prompt = (
        'Ты помощник для управления списком банковских транзакций.\n\n'
        'Текущие транзакции (JSON):\n' + tx_json + '\n\n'
        'Категории расходов: ' + cats_exp + '\n'
        'Источники доходов: ' + cats_inc + '\n'
        'Счета: ' + accounts_str + '\n\n'
        'Пользователь говорит: ' + text + '\n\n'
        'Пойми что нужно сделать и верни JSON:\n'
        '{"actions": [...], "reply": "ответ по-русски"}\n\n'
        'Типы actions:\n'
        '{"type": "set_category", "id": N, "category": "Название"}\n'
        '{"type": "set_transfer", "id": N, "to_account": "Счёт"}\n'
        '{"type": "set_exchange", "id_out": N, "id_in": M}\n'
        '{"type": "skip", "id": N}\n'
        '{"type": "unskip", "id": N}\n\n'
        'set_exchange - это обмен валюты между своими счетами (две транзакции: расход и приход).\n'
        'Категория должна быть точно из списка.\n'
        'Верни ТОЛЬКО валидный JSON.'
    )

    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=1000,
        messages=[{'role': 'user', 'content': prompt}]
    )

    raw = resp.content[0].text.replace('```json', '').replace('```', '').strip()
    result = json.loads(raw)

    for action in result.get('actions', []):
        t = action.get('type')
        idx = action.get('id', 0) - 1

        if t == 'set_category' and 0 <= idx < len(transactions):
            cat = action.get('category')
            if cat in ALL_CATS:
                transactions[idx]['category'] = cat
                transactions[idx]['skipped'] = False
                transactions[idx]['is_transfer'] = False
                key = norm_key(transactions[idx]['merchant'])
                rules[key] = cat

        elif t == 'set_transfer' and 0 <= idx < len(transactions):
            transactions[idx]['is_transfer'] = True
            transactions[idx]['transfer_to'] = action.get('to_account', '')
            transactions[idx]['skipped'] = False

        elif t == 'set_exchange':
            id_out = action.get('id_out', 0) - 1
            id_in = action.get('id_in', 0) - 1
            if 0 <= id_out < len(transactions):
                transactions[id_out]['is_exchange'] = True
                transactions[id_out]['exchange_pair'] = id_in
                transactions[id_out]['skipped'] = False
            if 0 <= id_in < len(transactions):
                transactions[id_in]['is_exchange'] = True
                transactions[id_in]['exchange_pair'] = id_out
                transactions[id_in]['skipped'] = False

        elif t == 'skip' and 0 <= idx < len(transactions):
            transactions[idx]['skipped'] = True

        elif t == 'unskip' and 0 <= idx < len(transactions):
            transactions[idx]['skipped'] = False

    save_rules(rules)
    return result.get('reply', 'Готово')

def generate_csv(transactions):
    lines = []
    done = set()

    for i, tx in enumerate(transactions):
        if tx['skipped'] or i in done:
            continue

        amt = tx.get('amount', 0)
        cur = tx.get('currency', 'EUR')
        date = tx.get('date', '')
        merchant = str(tx.get('merchant', '')).replace(';', ',').replace('"', "'")
        account = tx.get('account', '') or 'Revolut'

        if tx.get('is_exchange'):
            pair = tx.get('exchange_pair', -1)
            if 0 <= pair < len(transactions):
                tx2 = transactions[pair]
                done.add(i)
                done.add(pair)
                out = tx if tx['amount'] < 0 else tx2
                inc = tx2 if tx['amount'] < 0 else tx
                acc = out.get('account', '') or 'Revolut'
                lines.append(str(abs(out['amount'])) + ';' + out['currency'] + ';' + acc + ';' + acc + ';' + date + ';' + merchant)
                lines.append(str(abs(inc['amount'])) + ';' + inc['currency'] + ';' + acc + ';' + acc + ';' + date + ';' + merchant)
            continue

        if tx['is_transfer'] and tx.get('transfer_to'):
            to = tx['transfer_to']
            lines.append('-' + str(abs(amt)) + ';' + cur + ';' + to + ';' + account + ';' + date + ';' + merchant)
            lines.append(str(abs(amt)) + ';' + cur + ';' + account + ';' + to + ';' + date + ';' + merchant)
        else:
            lines.append(str(amt) + ';' + cur + ';' + tx['category'] + ';' + account + ';' + date + ';' + merchant)

    return '\n'.join(lines)

def format_list(transactions):
    lines = ['Транзакции:\n']
    for i, tx in enumerate(transactions, 1):
        if tx['skipped']:
            st = 'ПРОПУЩЕНО'
        elif tx.get('is_exchange'):
            st = 'ОБМЕН'
        elif tx['is_transfer']:
            st = 'ПЕРЕВОД -> ' + (tx.get('transfer_to') or '?')
        else:
            cat = tx['category']
            mark = 'OK' if cat != 'Без категории' else '?'
            st = mark + ' ' + cat
        flag = '!! ' if tx.get('suspect_transfer') and not tx['is_transfer'] and not tx['skipped'] else ''
        amt = str(tx.get('amount', ''))
        cur = tx.get('currency', '')
        merch = str(tx.get('merchant', ''))[:22]
        d = str(tx.get('date', ''))[:10]
        lines.append(flag + str(i) + '. ' + merch + ' | ' + amt + ' ' + cur + ' | ' + d + '\n   -> ' + st)

    pending = sum(1 for t in transactions if not t['skipped'] and t['category'] == 'Без категории' and not t['is_transfer'])
    if pending:
        lines.append('\nБез категории: ' + str(pending) + ' шт.')
    else:
        lines.append('\nВсе категоризованы!')
    return '\n'.join(lines)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Привет! Я помогу загрузить выписки в Дребеденьги.\n\n'
        'Как пользоваться:\n'
        '1. Отправь скриншоты банковских выписок\n'
        '2. Напиши /parse\n'
        '3. Голосом или текстом объясни что исправить\n'
        '4. Напиши /csv чтобы скачать файл\n\n'
        'Голосовые сообщения принимаю!'
    )

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    photo = update.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    data = await f.download_as_bytearray()
    b64 = base64.standard_b64encode(data).decode()
    session['images'].append({'mime': 'image/jpeg', 'data': b64})
    count = len(session['images'])
    await update.message.reply_text('Скриншот ' + str(count) + ' получен. Отправь ещё или напиши /parse')

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    doc = update.message.document
    if not doc.mime_type.startswith('image/'):
        await update.message.reply_text('Отправляй только изображения.')
        return
    f = await ctx.bot.get_file(doc.file_id)
    data = await f.download_as_bytearray()
    b64 = base64.standard_b64encode(data).decode()
    session['images'].append({'mime': doc.mime_type, 'data': b64})
    count = len(session['images'])
    await update.message.reply_text('Скриншот ' + str(count) + ' получен. Отправь ещё или напиши /parse')

async def parse_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    if not session['images']:
        await update.message.reply_text('Сначала отправь скриншоты.')
        return
    await update.message.reply_text('Разбираю ' + str(len(session['images'])) + ' скриншот(ов)... Подожди 20-30 секунд.')
    try:
        txs = await parse_images(session['images'], session['rules'])
        session['transactions'] = txs
        session['images'] = []
        await update.message.reply_text(format_list(txs))
        await update.message.reply_text(
            'Теперь голосом или текстом объясни что исправить.\n'
            'Пример: транзакция 3 - это обмен, 25000 рублей равно 275 евро\n\n'
            '/csv - скачать файл\n'
            '/reset - начать заново'
        )
    except Exception as e:
        await update.message.reply_text('Ошибка: ' + str(e))

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    if not session['transactions']:
        await update.message.reply_text('Сначала отправь скриншоты и напиши /parse')
        return
    await update.message.reply_text('Слушаю...')
    voice = update.message.voice
    f = await ctx.bot.get_file(voice.file_id)
    voice_data = await f.download_as_bytearray()
    b64 = base64.standard_b64encode(voice_data).decode()
    try:
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=500,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'Транскрибируй голосовое сообщение на русском. Только текст без пояснений.'},
                    {'type': 'document', 'source': {'type': 'base64', 'media_type': 'audio/ogg', 'data': b64}}
                ]
            }]
        )
        text = resp.content[0].text.strip()
        await update.message.reply_text('Распознала: ' + text)
    except Exception:
        await update.message.reply_text('Не смогла распознать голос. Напиши текстом.')
        return
    await process_cmd(update, session, text)

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    text = update.message.text
    if text.startswith('/'):
        return
    if not session['transactions']:
        await update.message.reply_text('Сначала отправь скриншоты и напиши /parse')
        return
    await process_cmd(update, session, text)

async def process_cmd(update, session, text):
    await update.message.reply_text('Обрабатываю...')
    try:
        reply = await handle_voice_command(text, session)
        await update.message.reply_text(reply)
        await update.message.reply_text(format_list(session['transactions']))
    except Exception as e:
        await update.message.reply_text('Ошибка: ' + str(e))

async def csv_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    if not session['transactions']:
        await update.message.reply_text('Нет транзакций.')
        return
    csv_content = generate_csv(session['transactions'])
    active = [t for t in session['transactions'] if not t['skipped']]
    no_cat = sum(1 for t in active if t['category'] == 'Без категории')
    fname = '/tmp/drebedengi_' + datetime.now().strftime('%Y-%m-%d') + '.csv'
    with open(fname, 'w', encoding='utf-8') as f:
        f.write(csv_content)
    msg = 'Транзакций: ' + str(len(active)) + '\nБез категории: ' + str(no_cat)
    await update.message.reply_text(msg)
    await ctx.bot.send_document(
        chat_id=update.effective_chat.id,
        document=open(fname, 'rb'),
        filename='drebedengi_' + datetime.now().strftime('%Y-%m-%d') + '.csv',
        caption='Загрузи в Дребеденьги -> Меню -> Импорт данных'
    )

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions[user_id] = {'transactions': [], 'images': [], 'rules': load_rules()}
    await update.message.reply_text('Начинаем заново. Отправь скриншоты.')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('parse', parse_cmd))
    app.add_handler(CommandHandler('csv', csv_cmd))
    app.add_handler(CommandHandler('reset', reset_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info('Bot started')
    app.run_polling()

if __name__ == '__main__':
    main()
