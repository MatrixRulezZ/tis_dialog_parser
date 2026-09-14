import asyncio
import aiohttp
from bs4 import BeautifulSoup as BS
from telebot.async_telebot import AsyncTeleBot
from telebot import types
import re
import logging
import os
import sqlite3
import html
from cryptography.fernet import Fernet, InvalidToken

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("tis_dialog_bot")

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не задан!")
    exit(1)

DB_KEY = os.getenv('TIS_DB_KEY')
if not DB_KEY:
    logger.error(
        "TIS_DB_KEY не задан! Сгенерируйте ключ командой:\n"
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
    exit(1)
try:
    fernet = Fernet(DB_KEY)
except Exception as e:
    logger.error(f"Некорректный TIS_DB_KEY: {e}")
    exit(1)

DB_FILE = os.getenv("DB_FILE", "tis_users.db")

def encrypt_password(password):
    return fernet.encrypt(password.encode()).decode()

def decrypt_password(token):
    try:
        return fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        # не шифротекст (старая запись открытым текстом) — возвращаем как есть
        return token

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            account_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            tis_login TEXT NOT NULL,
            tis_password TEXT NOT NULL,
            last_balance REAL DEFAULT 0,
            last_traffic_gb REAL DEFAULT 0,
            last_ip TEXT DEFAULT '',
            last_notification_date TEXT DEFAULT ''
        )
    ''')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_user_login ON accounts(telegram_id, tis_login)')
    conn.commit()
    conn.close()

def migrate_users_to_accounts():
    # одноразовая миграция со старой схемы "один пользователь = один кабинет"
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    has_users = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if has_users:
        rows = cursor.execute('''
            SELECT telegram_id, chat_id, tis_login, tis_password,
                   last_balance, last_traffic_gb, last_ip, last_notification_date
            FROM users
        ''').fetchall()
        for r in rows:
            cursor.execute('''
                INSERT OR IGNORE INTO accounts
                    (telegram_id, chat_id, tis_login, tis_password,
                     last_balance, last_traffic_gb, last_ip, last_notification_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', r)
        cursor.execute("DROP TABLE users")
        conn.commit()
        logger.info(f"Миграция: перенесено кабинетов — {len(rows)}")
    conn.close()

def get_accounts(telegram_id):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute('''
        SELECT account_id, tis_login, tis_password, chat_id
        FROM accounts WHERE telegram_id = ? ORDER BY account_id
    ''', (telegram_id,)).fetchall()
    conn.close()
    return [
        {
            "account_id": r[0],
            "tis_login": r[1],
            "tis_password": decrypt_password(r[2]),
            "chat_id": r[3],
        }
        for r in rows
    ]

def add_account(telegram_id, chat_id, tis_login, tis_password):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO accounts (telegram_id, chat_id, tis_login, tis_password)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, chat_id, tis_login, encrypt_password(tis_password)))
    conn.commit()
    conn.close()

def delete_account(telegram_id, account_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM accounts WHERE telegram_id = ? AND account_id = ?',
                   (telegram_id, account_id))
    conn.commit()
    conn.close()

def update_account_stats(account_id, balance, traffic_gb, ip):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE accounts SET last_balance=?, last_traffic_gb=?, last_ip=? WHERE account_id=?
    ''', (balance, traffic_gb, ip, account_id))
    conn.commit()
    conn.close()

def update_last_notification(account_id, date_str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE accounts SET last_notification_date=? WHERE account_id=?', (date_str, account_id))
    conn.commit()
    conn.close()

def migrate_plaintext_passwords():
    # шифротекст Fernet всегда начинается с "gAAAAA"; всё остальное — старые пароли открытым текстом
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    rows = cursor.execute("SELECT account_id, tis_password FROM accounts").fetchall()
    changed = 0
    for account_id, pw in rows:
        if not (isinstance(pw, str) and pw.startswith("gAAAAA")):
            cursor.execute(
                "UPDATE accounts SET tis_password=? WHERE account_id=?",
                (encrypt_password(pw or ""), account_id)
            )
            changed += 1
    if changed:
        conn.commit()
        logger.info(f"Миграция: зашифровано паролей — {changed}")
    conn.close()

init_db()
migrate_users_to_accounts()
migrate_plaintext_passwords()

class TISClient:
    def __init__(self, tis_login, tis_password):
        self.tis_login = tis_login
        self.tis_password = tis_password
        self.session = None

    async def login(self):
        try:
            if self.session:
                await self.session.close()
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            data = {"login": self.tis_login, "passv": self.tis_password, "remember": "1"}
            async with self.session.post("https://stats.tis-dialog.ru/index.php", data=data):
                pass
            async with self.session.get("https://stats.tis-dialog.ru/index.php") as resp:
                text = await resp.text(encoding='windows-1251', errors='ignore')
            return "Выйти" in text or "Выход" in text or "Баланс" in text or "lkInfoTable" in text
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    async def get_notifications_list(self):
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return []
            url = f"https://stats.tis-dialog.ru/index.php?mod=msg&phnumber={self.tis_login}"
            async with self.session.get(url) as resp:
                html_ = await resp.text(encoding='windows-1251', errors='ignore')
            soup = BS(html_, 'html.parser')
            notifications = []
            for div in soup.select('.contentBlock > div[style*="margin-bottom"]'):
                a_tag = div.find('a')
                if a_tag and 'comsg=' in str(a_tag.get('href', '')):
                    href = a_tag.get('href', '')
                    match = re.search(r'comsg=(\d+)', href)
                    if match:
                        notifications.append({
                            "id": match.group(1),
                            "short_text": div.get_text(" ", strip=True)[:110]
                        })
            return notifications[:6]
        except Exception as e:
            logger.error(f"get_notifications_list error: {e}")
            return []

    async def get_notification_full(self, notif_id):
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return "Ошибка загрузки."
            url = f"https://stats.tis-dialog.ru/index.php?mod=msg&comsg={notif_id}&phnumber={self.tis_login}"
            async with self.session.get(url) as resp:
                html_ = await resp.text(encoding='windows-1251', errors='ignore')
            soup = BS(html_, 'html.parser')
            content = soup.select_one('.contentBlock')
            return content.get_text("\n", strip=True) if content else "Текст не найден."
        except Exception as e:
            logger.error(f"get_notification_full error: {e}")
            return "Ошибка при загрузке уведомления."

    async def get_payments(self, limit=12):
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return []
            url = f"https://stats.tis-dialog.ru/index.php?mod=payments&phnumber={self.tis_login}"
            async with self.session.get(url) as resp:
                html_ = await resp.text(encoding='windows-1251', errors='ignore')
            soup = BS(html_, 'html.parser')
            payments = []
            table = soup.select_one('.lkTraficTable')
            if table:
                for row in table.select('tr')[1:limit+1]:
                    tds = row.select('td')
                    if len(tds) >= 3:
                        payments.append([
                            self._cell_text(tds[0]),
                            self._cell_text(tds[1]),
                            self._cell_text(tds[2]),
                        ])
            return payments
        except Exception as e:
            logger.error(f"get_payments error: {e}")
            return []

    @staticmethod
    def _cell_text(td):
        # выкидываем скрытые/служебные элементы (поля inline-редактирования и т.п.)
        for el in td.find_all(["input", "script", "style"]) + td.find_all(attrs={"contenteditable": True}):
            el.decompose()
        for el in td.find_all(style=True):
            style = el.get("style", "").replace(" ", "").lower()
            if "display:none" in style or "visibility:hidden" in style:
                el.decompose()
        return td.get_text(strip=True)

    async def get_promised_payment_info(self):
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return {"available": False, "balance": 0}
            url = f"https://stats.tis-dialog.ru/index.php?mod=promisedpay&phnumber={self.tis_login}"
            async with self.session.get(url) as resp:
                html_ = await resp.text(encoding='windows-1251', errors='ignore')
            text = html_
            available = "Активировать" in text
            balance = 0
            match = re.search(r'На счете:\s*([\d.,]+)', text)
            if match:
                balance = float(match.group(1).replace(',', '.'))
            return {"available": available, "balance": balance}
        except Exception as e:
            logger.error(f"get_promised_payment_info error: {e}")
            return {"available": False, "balance": 0}

    async def activate_promised_payment(self):
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return False
            post_data = {"mod": "promisedpay", "modcmd": "promisedpay", "chk_agree": "agree"}
            async with self.session.post("https://stats.tis-dialog.ru/index.php", data=post_data) as resp:
                result = await resp.text(encoding='windows-1251', errors='ignore')
                return "успешно" in result.lower() or "активирована" in result.lower()
        except Exception as e:
            logger.error(f"activate_promised_payment error: {e}")
            return False

    def _get_value(self, soup, label):
        for table in soup.select('.lkInfoTable'):
            for row in table.select('tr'):
                tds = row.select('td')
                if len(tds) >= 2 and label.lower() in tds[0].get_text(strip=True).lower():
                    return tds[1].get_text(strip=True)
        return "Н/Д"

    async def fetch_data(self):
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return None
            async with self.session.get("https://stats.tis-dialog.ru/index.php") as resp:
                html_ = await resp.text(encoding='windows-1251', errors='ignore')
            soup = BS(html_, 'html.parser')
            for a in soup.find_all("a"):
                a.decompose()
            data = {
                "tariff": self._get_value(soup, "Тарифный план"),
                "balance_raw": self._get_value(soup, "Баланс"),
                "status": self._get_value(soup, "Состояние"),
                "speed": self._get_value(soup, "Скорость по тарифу"),
                "turbo": self._get_value(soup, "Остаток турбо-трафика"),
                "activity": self._get_value(soup, "Активность"),
                "ip": "Н/Д",
                "incoming": "Н/Д",
                "outgoing": "Н/Д",
            }
            try:
                num = re.sub(r'[^\d\-.]+', '', data["balance_raw"].replace(',', '.'))
                data["balance"] = float(num) if num else 0.0
            except ValueError:
                data["balance"] = 0.0
            match = re.search(r'IP:(\d{1,3}(?:\.\d{1,3}){3})', data["activity"])
            if match:
                data["ip"] = match.group(1)
            traffic_gb = 0.0
            m = re.search(r'([\d.,]+)', data["incoming"])
            if m:
                try:
                    traffic_gb = float(m.group(1).replace(',', '.'))
                except ValueError:
                    traffic_gb = 0.0
            data["traffic_gb"] = traffic_gb
            traffic_table = soup.select_one('.lkTraficTable')
            if traffic_table:
                tds = traffic_table.select('td')
                if len(tds) >= 2:
                    inc_match = re.search(r'\(([^)]+)\)', tds[0].get_text())
                    out_match = re.search(r'\(([^)]+)\)', tds[1].get_text())
                    data["incoming"] = inc_match.group(1) if inc_match else tds[0].get_text(strip=True)
                    data["outgoing"] = out_match.group(1) if out_match else tds[1].get_text(strip=True)
            return data
        except Exception as e:
            logger.error(f"fetch_data error: {e}")
            return None

    async def get_qr(self):
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return None
            url = f"https://stats.tis-dialog.ru/qrpay.php?phnumber={self.tis_login}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
            return None
        except Exception as e:
            logger.error(f"get_qr error: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

bot = AsyncTeleBot(BOT_TOKEN)
user_states = {}
promised_confirm = {}
user_notifications = {}

def format_payment_row(row):
    date, amount, op = (html.escape(str(c)) for c in row)
    amt = amount.replace(" ", "")
    if amt.startswith("-"):
        return f"📉 <code>{date}</code>  <b>{amt} ₽</b> — {op}"
    return f"💳 <code>{date}</code>  <b>+{amt} ₽</b> — {op}"

@bot.message_handler(commands=['start'])
async def start(message):
    accounts = get_accounts(message.from_user.id)
    if accounts:
        logins = ", ".join(f"<b>{html.escape(a['tis_login'])}</b>" for a in accounts)
        await bot.send_message(message.chat.id, f"Привет! Твои кабинеты: {logins}", parse_mode="HTML")
        await show_menu(message.chat.id)
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Подключить кабинет TIS", callback_data="register"))
        await bot.send_message(message.chat.id, "Нажми кнопку, чтобы подключить свой личный кабинет TIS.", reply_markup=markup)

@bot.message_handler(commands=['cancel'])
async def cancel(message):
    user_id = message.from_user.id
    removed = user_states.pop(user_id, None) is not None
    promised_confirm.pop(user_id, None)
    if removed:
        await bot.send_message(message.chat.id, "Регистрация отменена.")
    else:
        await bot.send_message(message.chat.id, "Нечего отменять.")

async def show_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 Статус", "💳 Оплатить")
    markup.add("🔔 Уведомления", "📜 История платежей")
    markup.add("💰 Обещанный платёж", "🔀 Кабинеты")
    await bot.send_message(chat_id, "Выбери действие:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "register")
async def register_start(call):
    user_id = call.from_user.id
    user_states[user_id] = {"step": "login"}
    await bot.send_message(call.message.chat.id, "Введите <b>логин</b> от личного кабинета TIS:", parse_mode="HTML")
    await bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.from_user.id in user_states)
async def registration_handler(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or not isinstance(state, dict):
        if user_id in user_states:
            del user_states[user_id]
        return
    if state.get("step") == "login":
        state["login"] = message.text.strip()
        state["step"] = "password"
        await bot.send_message(message.chat.id, "Теперь введи <b>пароль</b>:", parse_mode="HTML")
    elif state.get("step") == "password":
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        login = state.get("login")
        password = message.text.strip()
        if not login:
            del user_states[user_id]
            return
        async with TISClient(login, password) as client:
            success = await client.login()
        if success:
            add_account(user_id, message.chat.id, login, password)
            del user_states[user_id]
            await bot.send_message(message.chat.id, f"✅ Кабинет <b>{html.escape(login)}</b> подключён!", parse_mode="HTML")
            await show_menu(message.chat.id)
        else:
            await bot.send_message(message.chat.id, "❌ Не удалось войти. Проверь логин и пароль.")
            del user_states[user_id]

@bot.message_handler(func=lambda m: m.text == "📊 Статус")
async def status(message):
    accounts = get_accounts(message.from_user.id)
    if not accounts:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет (🔀 Кабинеты)")
        return
    for acc in accounts:
        login_esc = html.escape(acc["tis_login"])
        async with TISClient(acc["tis_login"], acc["tis_password"]) as client:
            data = await client.fetch_data()
        if not data:
            await bot.send_message(message.chat.id, f"❌ <b>{login_esc}</b>: не удалось получить данные", parse_mode="HTML")
            continue
        turbo_clean = data['turbo']
        if '(' in turbo_clean and ')' in turbo_clean:
            turbo_clean = turbo_clean.split('(')[1].replace(')', '').strip()

        text = (
            f"📊 <b>{login_esc}</b>\n\n"
            f"📌 <b>Тариф:</b> {html.escape(data['tariff'])}\n"
            f"💰 <b>Баланс:</b> {html.escape(data['balance_raw'])}\n"
            f"🟢 <b>Состояние:</b> {html.escape(data['status'])}\n"
            f"⚡ <b>Скорость:</b> {html.escape(data['speed'])}\n"
            f"🚀 <b>Остаток турбо:</b> {html.escape(turbo_clean)}\n"
            f"🌐 <b>IP:</b> <code>{html.escape(data['ip'])}</code>\n\n"
            f"📈 <b>Трафик за текущий период:</b>\n"
            f"⬇️ Входящий: {html.escape(data['incoming'])}\n"
            f"⬆️ Исходящий: {html.escape(data['outgoing'])}"
        )
        await bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🔔 Уведомления")
async def notifications(message):
    user_id = message.from_user.id
    accounts = get_accounts(user_id)
    if not accounts:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет (🔀 Кабинеты)")
        return
    user_notifications[user_id] = {}
    for acc in accounts:
        login_esc = html.escape(acc["tis_login"])
        async with TISClient(acc["tis_login"], acc["tis_password"]) as client:
            notifs = await client.get_notifications_list()
        if not notifs:
            await bot.send_message(message.chat.id, f"🔔 <b>{login_esc}</b>: уведомлений нет", parse_mode="HTML")
            continue
        user_notifications[user_id][acc["account_id"]] = notifs
        markup = types.InlineKeyboardMarkup(row_width=1)
        for n in notifs:
            markup.add(types.InlineKeyboardButton(n["short_text"], callback_data=f"view_notif_{acc['account_id']}_{n['id']}"))
        markup.add(types.InlineKeyboardButton("❌ Закрыть", callback_data="close_notifications"))
        await bot.send_message(message.chat.id, f"🔔 <b>{login_esc}</b> — выберите уведомление:", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_notif_"))
async def view_notification(call):
    user_id = call.from_user.id
    try:
        _, _, account_id, notif_id = call.data.split("_")
        account_id = int(account_id)
    except ValueError:
        await bot.answer_callback_query(call.id)
        return
    account = next((a for a in get_accounts(user_id) if a["account_id"] == account_id), None)
    if not account:
        await bot.answer_callback_query(call.id)
        return
    async with TISClient(account["tis_login"], account["tis_password"]) as client:
        full_text = await client.get_notification_full(notif_id)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад к списку", callback_data=f"back_to_notifications_{account_id}"))
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📄 <b>{html.escape(account['tis_login'])}</b> — <b>полный текст уведомления:</b>\n\n{html.escape(full_text)}",
        reply_markup=markup,
        parse_mode="HTML"
    )
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("back_to_notifications"))
async def back_to_notifications(call):
    user_id = call.from_user.id
    try:
        account_id = int(call.data.rsplit("_", 1)[-1])
    except ValueError:
        account_id = None
    notifs = user_notifications.get(user_id, {}).get(account_id, [])
    if not notifs:
        await bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="Уведомлений нет.")
        await bot.answer_callback_query(call.id)
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for n in notifs:
        markup.add(types.InlineKeyboardButton(n["short_text"], callback_data=f"view_notif_{account_id}_{n['id']}"))
    markup.add(types.InlineKeyboardButton("❌ Закрыть", callback_data="close_notifications"))
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔔 Выберите уведомление:",
        reply_markup=markup
    )
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "close_notifications")
async def close_notifications(call):
    await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    await bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text == "📜 История платежей")
async def payments(message):
    accounts = get_accounts(message.from_user.id)
    if not accounts:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет (🔀 Кабинеты)")
        return
    for acc in accounts:
        login_esc = html.escape(acc["tis_login"])
        async with TISClient(acc["tis_login"], acc["tis_password"]) as client:
            pays = await client.get_payments(12)
        if pays:
            text = f"📜 <b>{login_esc}</b> — последние платежи:\n\n" + "\n".join(format_payment_row(row) for row in pays)
        else:
            text = f"📜 <b>{login_esc}</b>: не удалось получить историю."
        await bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💰 Обещанный платёж")
async def promised_payment(message):
    user_id = message.from_user.id
    accounts = get_accounts(user_id)
    if not accounts:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет (🔀 Кабинеты)")
        return
    confirmed = promised_confirm.setdefault(user_id, set())
    for acc in accounts:
        login_esc = html.escape(acc["tis_login"])
        async with TISClient(acc["tis_login"], acc["tis_password"]) as client:
            info = await client.get_promised_payment_info()
        if info["available"]:
            confirmed.add(acc["account_id"])
            text = (f"💰 <b>{login_esc}</b>\n\n"
                    f"Баланс: <b>{info['balance']} руб.</b>\n\n"
                    f"Стоимость: 30 руб. | Длительность: 5 дней.\n\n"
                    f"Активировать?")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Активировать", callback_data=f"activate_promised_{acc['account_id']}"))
            markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_promised_{acc['account_id']}"))
            await bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        else:
            await bot.send_message(message.chat.id, f"💰 <b>{login_esc}</b>: обещанный платёж сейчас недоступен.", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("activate_promised_"))
async def activate_promised(call):
    user_id = call.from_user.id
    try:
        account_id = int(call.data.rsplit("_", 1)[-1])
    except ValueError:
        await bot.answer_callback_query(call.id)
        return
    if account_id not in promised_confirm.get(user_id, set()):
        await bot.answer_callback_query(call.id)
        return
    account = next((a for a in get_accounts(user_id) if a["account_id"] == account_id), None)
    if not account:
        return
    async with TISClient(account["tis_login"], account["tis_password"]) as client:
        success = await client.activate_promised_payment()
    promised_confirm[user_id].discard(account_id)
    if success:
        await bot.send_message(call.message.chat.id, f"✅ Обещанный платёж активирован для <b>{html.escape(account['tis_login'])}</b>!", parse_mode="HTML")
    else:
        await bot.send_message(call.message.chat.id, f"❌ <b>{html.escape(account['tis_login'])}</b>: не удалось активировать.", parse_mode="HTML")
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_promised_"))
async def cancel_promised(call):
    user_id = call.from_user.id
    try:
        account_id = int(call.data.rsplit("_", 1)[-1])
    except ValueError:
        await bot.answer_callback_query(call.id)
        return
    if user_id in promised_confirm:
        promised_confirm[user_id].discard(account_id)
    await bot.send_message(call.message.chat.id, "Отменено.")
    await bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text == "💳 Оплатить")
async def pay(message):
    accounts = get_accounts(message.from_user.id)
    if not accounts:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет (🔀 Кабинеты)")
        return
    for acc in accounts:
        login_esc = html.escape(acc["tis_login"])
        async with TISClient(acc["tis_login"], acc["tis_password"]) as client:
            qr = await client.get_qr()
        if qr:
            await bot.send_photo(message.chat.id, qr, caption=f"💳 QR-код для оплаты — <b>{login_esc}</b>", parse_mode="HTML")
        else:
            await bot.send_message(message.chat.id, f"❌ <b>{login_esc}</b>: не удалось получить QR.", parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🔀 Кабинеты")
async def cabinets(message):
    await show_cabinets(message.chat.id, message.from_user.id)

def cabinets_keyboard(accounts):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("➕ Подключить кабинет", callback_data="register"))
    if accounts:
        markup.add(types.InlineKeyboardButton("🚪 Выйти из кабинета", callback_data="leave_select"))
    return markup

def cabinets_text(accounts):
    if accounts:
        return "🔀 <b>Твои кабинеты:</b>\n\n" + "\n".join(
            f"• <b>{html.escape(a['tis_login'])}</b>" for a in accounts
        )
    return "У тебя нет подключённых кабинетов."

async def show_cabinets(chat_id, user_id):
    accounts = get_accounts(user_id)
    await bot.send_message(chat_id, cabinets_text(accounts), reply_markup=cabinets_keyboard(accounts), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "cabinets_back")
async def cabinets_back(call):
    accounts = get_accounts(call.from_user.id)
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=cabinets_text(accounts),
        reply_markup=cabinets_keyboard(accounts),
        parse_mode="HTML"
    )
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "leave_select")
async def leave_select(call):
    accounts = get_accounts(call.from_user.id)
    if not accounts:
        await bot.answer_callback_query(call.id, "Нет кабинетов для выхода")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for a in accounts:
        markup.add(types.InlineKeyboardButton(f"🚪 {a['tis_login']}", callback_data=f"leave_{a['account_id']}"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cabinets_back"))
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Выбери кабинет, из которого хочешь выйти:",
        reply_markup=markup
    )
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: re.fullmatch(r"leave_\d+", call.data or ""))
async def leave_confirm(call):
    user_id = call.from_user.id
    account_id = int(call.data.split("_")[1])
    account = next((a for a in get_accounts(user_id) if a["account_id"] == account_id), None)
    if not account:
        await bot.answer_callback_query(call.id, "Кабинет не найден")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("✅ Да, выйти", callback_data=f"confirm_leave_{account_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cabinets_back"))
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=(f"Точно выйти из кабинета <b>{html.escape(account['tis_login'])}</b>?\n\n"
              f"Информация по нему больше не будет показываться."),
        reply_markup=markup,
        parse_mode="HTML"
    )
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: re.fullmatch(r"confirm_leave_\d+", call.data or ""))
async def leave_done(call):
    user_id = call.from_user.id
    account_id = int(call.data.split("_")[2])
    delete_account(user_id, account_id)
    # чистим кэши этого кабинета
    user_notifications.get(user_id, {}).pop(account_id, None)
    if user_id in promised_confirm:
        promised_confirm[user_id].discard(account_id)
    accounts = get_accounts(user_id)
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="✅ Кабинет отключён.\n\n" + cabinets_text(accounts),
        reply_markup=cabinets_keyboard(accounts),
        parse_mode="HTML"
    )
    await bot.answer_callback_query(call.id)

async def background_monitor():
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            rows = conn.execute('''
                SELECT account_id, chat_id, tis_login, tis_password, last_ip, last_notification_date
                FROM accounts
            ''').fetchall()
            conn.close()
            for account_id, chat_id, login, password, last_ip, last_notif_date in rows:
                try:
                    async with TISClient(login, decrypt_password(password)) as client:
                        data = await client.fetch_data()
                        if data:
                            if data["balance"] < 0:
                                try:
                                    await bot.send_message(chat_id, f"⚠️ <b>{html.escape(login)}</b>: баланс ушёл в минус!", parse_mode="HTML")
                                except Exception:
                                    pass
                            if data["ip"] != "Н/Д" and last_ip and data["ip"] != last_ip:
                                try:
                                    await bot.send_message(chat_id, f"🌐 <b>{html.escape(login)}</b>: IP изменился: <code>{html.escape(data['ip'])}</code>", parse_mode="HTML")
                                except Exception:
                                    pass
                            update_account_stats(account_id, data["balance"], data["traffic_gb"], data["ip"])
                        notifs = await client.get_notifications_list()
                        if notifs:
                            newest = notifs[0]["short_text"]
                            current_date = newest[:10] if len(newest) > 10 else ""
                            if current_date and current_date != last_notif_date:
                                try:
                                    await bot.send_message(chat_id, f"🔔 <b>{html.escape(login)}</b> — <b>новое уведомление:</b>\n\n{html.escape(newest)}", parse_mode="HTML")
                                    update_last_notification(account_id, current_date)
                                except Exception:
                                    pass
                except Exception as e:
                    logger.error(f"Monitor error for account {account_id} ({login}): {e}")
        except Exception as e:
            logger.error(f"Background error: {e}")
        await asyncio.sleep(1800)

async def main():
    logger.info("Бот запускается...")
    asyncio.create_task(background_monitor())
    await bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    asyncio.run(main())
