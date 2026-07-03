import asyncio
import aiohttp
from bs4 import BeautifulSoup as BS
from telebot.async_telebot import AsyncTeleBot
from telebot import types
import re
import logging
import os
import sqlite3

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("tis_dialog_bot")

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не задан!")
    exit(1)

DB_FILE = "tis_users.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            tis_login TEXT NOT NULL,
            tis_password TEXT NOT NULL,
            last_balance REAL DEFAULT 0,
            last_traffic_gb REAL DEFAULT 0,
            last_ip TEXT DEFAULT '',
            last_notification_date TEXT DEFAULT ''
        )
    ''')
    conn.commit()
    conn.close()

def get_user(telegram_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "telegram_id": row[0],
            "chat_id": row[1],
            "tis_login": row[2],
            "tis_password": row[3],
            "last_balance": row[4],
            "last_traffic_gb": row[5],
            "last_ip": row[6],
            "last_notification_date": row[7] or ""
        }
    return None

def save_user(telegram_id, chat_id, tis_login, tis_password):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (telegram_id, chat_id, tis_login, tis_password)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, chat_id, tis_login, tis_password))
    conn.commit()
    conn.close()

def update_user_stats(telegram_id, balance, traffic_gb, ip):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET last_balance=?, last_traffic_gb=?, last_ip=? WHERE telegram_id=?
    ''', (balance, traffic_gb, ip, telegram_id))
    conn.commit()
    conn.close()

def update_last_notification(telegram_id, date_str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET last_notification_date=? WHERE telegram_id=?', (date_str, telegram_id))
    conn.commit()
    conn.close()

init_db()

class TISClient:
    def __init__(self, tis_login, tis_password):
        self.tis_login = tis_login
        self.tis_password = tis_password
        self.session = None

    async def login(self):
        try:
            if self.session:
                await self.session.close()
            self.session = aiohttp.ClientSession()
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
                html = await resp.text(encoding='windows-1251', errors='ignore')
            soup = BS(html, 'html.parser')
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
                html = await resp.text(encoding='windows-1251', errors='ignore')
            soup = BS(html, 'html.parser')
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
                html = await resp.text(encoding='windows-1251', errors='ignore')
            soup = BS(html, 'html.parser')
            payments = []
            table = soup.select_one('.lkTraficTable')
            if table:
                for row in table.select('tr')[1:limit+1]:
                    tds = row.select('td')
                    if len(tds) >= 3:
                        payments.append(f"{tds[0].get_text(strip=True)} | {tds[1].get_text(strip=True)} | {tds[2].get_text(strip=True)}")
            return payments
        except:
            return []

    async def get_promised_payment_info(self):
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return {"available": False, "balance": 0}
            url = f"https://stats.tis-dialog.ru/index.php?mod=promisedpay&phnumber={self.tis_login}"
            async with self.session.get(url) as resp:
                html = await resp.text(encoding='windows-1251', errors='ignore')
            text = html
            available = "Активировать" in text
            balance = 0
            match = re.search(r'На счете:\s*([\d.,]+)', text)
            if match:
                balance = float(match.group(1).replace(',', '.'))
            return {"available": available, "balance": balance}
        except:
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
        except:
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
                html = await resp.text(encoding='windows-1251', errors='ignore')
            soup = BS(html, 'html.parser')
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
            except:
                data["balance"] = 0.0
            match = re.search(r'IP:(\d{1,3}(?:\.\d{1,3}){3})', data["activity"])
            if match:
                data["ip"] = match.group(1)
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
        except:
            return None

    async def close(self):
        if self.session:
            await self.session.close()

bot = AsyncTeleBot(BOT_TOKEN)
user_states = {}
promised_confirm = {}
user_notifications = {}

@bot.message_handler(commands=['start'])
async def start(message):
    user = get_user(message.from_user.id)
    if user:
        await bot.send_message(message.chat.id, f"Привет! Ты подключен как `{user['tis_login']}`", parse_mode="Markdown")
        await show_menu(message.chat.id)
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Подключить кабинет TIS", callback_data="register"))
        await bot.send_message(message.chat.id, "Нажми кнопку, чтобы подключить свой личный кабинет TIS.", reply_markup=markup)

async def show_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 Статус", "💳 Оплатить")
    markup.add("🔔 Уведомления", "📜 История платежей")
    markup.add("💰 Обещанный платёж", "🔄 Обновить данные")
    await bot.send_message(chat_id, "Выбери действие:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "register")
async def register_start(call):
    user_id = call.from_user.id
    user_states[user_id] = {"step": "login"}
    await bot.send_message(call.message.chat.id, "Введите **логин** от личного кабинета TIS:")
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
        await bot.send_message(message.chat.id, "Теперь введи **пароль**:")
    elif state.get("step") == "password":
        login = state.get("login")
        password = message.text.strip()
        if not login:
            del user_states[user_id]
            return
        client = TISClient(login, password)
        success = await client.login()
        await client.close()
        if success:
            save_user(user_id, message.chat.id, login, password)
            del user_states[user_id]
            await bot.send_message(message.chat.id, "✅ Учётная запись успешно подключена!")
            await show_menu(message.chat.id)
        else:
            await bot.send_message(message.chat.id, "❌ Не удалось войти. Проверь логин и пароль.")
            del user_states[user_id]

@bot.message_handler(func=lambda m: m.text == "📊 Статус")
async def status(message):
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет")
        return
    client = TISClient(user["tis_login"], user["tis_password"])
    data = await client.fetch_data()
    await client.close()
    if data:
        turbo_clean = data['turbo']
        if '(' in turbo_clean and ')' in turbo_clean:
            turbo_clean = turbo_clean.split('(')[1].replace(')', '').strip()

        text = (
            "📊 **Твой статус**\n\n"
            f"📌 **Тариф:** {data['tariff']}\n"
            f"💰 **Баланс:** {data['balance_raw']}\n"
            f"🟢 **Состояние:** {data['status']}\n"
            f"⚡ **Скорость:** {data['speed']}\n"
            f"🚀 **Остаток турбо:** {turbo_clean}\n"
            f"🌐 **IP:** `{data['ip']}`\n\n"
            "📈 **Трафик за текущий период:**\n"
            f"⬇️ Входящий: {data['incoming']}\n"
            f"⬆️ Исходящий: {data['outgoing']}"
        )
        await bot.send_message(message.chat.id, text, parse_mode="Markdown")
    else:
        await bot.send_message(message.chat.id, "Не удалось получить данные")

@bot.message_handler(func=lambda m: m.text == "🔔 Уведомления")
async def notifications(message):
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет")
        return
    client = TISClient(user["tis_login"], user["tis_password"])
    notifs = await client.get_notifications_list()
    await client.close()
    if not notifs:
        await bot.send_message(message.chat.id, "Уведомлений нет.")
        return
    user_notifications[message.from_user.id] = notifs
    markup = types.InlineKeyboardMarkup(row_width=1)
    for n in notifs:
        markup.add(types.InlineKeyboardButton(n["short_text"], callback_data=f"view_notif_{n['id']}"))
    markup.add(types.InlineKeyboardButton("❌ Закрыть", callback_data="close_notifications"))
    await bot.send_message(message.chat.id, "🔔 Выберите уведомление:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_notif_"))
async def view_notification(call):
    user_id = call.from_user.id
    notif_id = call.data.replace("view_notif_", "")
    user = get_user(user_id)
    if not user:
        await bot.answer_callback_query(call.id)
        return
    client = TISClient(user["tis_login"], user["tis_password"])
    full_text = await client.get_notification_full(notif_id)
    await client.close()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад к списку", callback_data="back_to_notifications"))
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📄 **Полный текст уведомления:**\n\n{full_text}",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_notifications")
async def back_to_notifications(call):
    user_id = call.from_user.id
    notifs = user_notifications.get(user_id, [])
    if not notifs:
        await bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="Уведомлений нет.")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for n in notifs:
        markup.add(types.InlineKeyboardButton(n["short_text"], callback_data=f"view_notif_{n['id']}"))
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
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет")
        return
    client = TISClient(user["tis_login"], user["tis_password"])
    pays = await client.get_payments(12)
    await client.close()
    if pays:
        text = "📜 **Последние платежи:**\n\n"
        for p in pays:
            text += f"• {p}\n"
        await bot.send_message(message.chat.id, text)
    else:
        await bot.send_message(message.chat.id, "Не удалось получить историю.")

@bot.message_handler(func=lambda m: m.text == "💰 Обещанный платёж")
async def promised_payment(message):
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет")
        return
    client = TISClient(user["tis_login"], user["tis_password"])
    info = await client.get_promised_payment_info()
    await client.close()
    if info["available"]:
        text = (f"💰 **Обещанный платёж**\n\n"
                f"Баланс: **{info['balance']} руб.**\n\n"
                f"Стоимость: 30 руб. | Длительность: 5 дней.\n\n"
                f"Активировать?")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Активировать", callback_data="activate_promised"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_promised"))
        await bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
        promised_confirm[message.from_user.id] = True
    else:
        await bot.send_message(message.chat.id, "Обещанный платёж сейчас недоступен.")

@bot.callback_query_handler(func=lambda call: call.data == "activate_promised")
async def activate_promised(call):
    user_id = call.from_user.id
    if user_id not in promised_confirm:
        await bot.answer_callback_query(call.id)
        return
    user = get_user(user_id)
    if not user:
        return
    client = TISClient(user["tis_login"], user["tis_password"])
    success = await client.activate_promised_payment()
    await client.close()
    del promised_confirm[user_id]
    if success:
        await bot.send_message(call.message.chat.id, "✅ Обещанный платёж активирован!")
    else:
        await bot.send_message(call.message.chat.id, "❌ Не удалось активировать.")
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "cancel_promised")
async def cancel_promised(call):
    user_id = call.from_user.id
    if user_id in promised_confirm:
        del promised_confirm[user_id]
    await bot.send_message(call.message.chat.id, "Отменено.")
    await bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text == "💳 Оплатить")
async def pay(message):
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет")
        return
    client = TISClient(user["tis_login"], user["tis_password"])
    qr = await client.get_qr()
    await client.close()
    if qr:
        await bot.send_photo(message.chat.id, qr, caption="QR-код для оплаты")
    else:
        await bot.send_message(message.chat.id, "Не удалось получить QR.")

@bot.message_handler(func=lambda m: m.text == "🔄 Обновить данные")
async def refresh(message):
    await status(message)

async def background_monitor():
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            users = conn.execute("SELECT telegram_id, chat_id, tis_login, tis_password, last_ip, last_notification_date FROM users").fetchall()
            conn.close()
            for telegram_id, chat_id, login, password, last_ip, last_notif_date in users:
                client = TISClient(login, password)
                data = await client.fetch_data()
                if data:
                    if data["balance"] < 0:
                        try:
                            await bot.send_message(chat_id, "⚠️ Баланс ушёл в минус!")
                        except:
                            pass
                    if data["ip"] != "Н/Д" and last_ip and data["ip"] != last_ip:
                        try:
                            await bot.send_message(chat_id, f"🌐 IP изменился: `{data['ip']}`", parse_mode="Markdown")
                        except:
                            pass
                    update_user_stats(telegram_id, data["balance"], data["traffic_gb"], data["ip"])
                notifs = await client.get_notifications_list()
                if notifs:
                    newest = notifs[0]["short_text"]
                    current_date = newest[:10] if len(newest) > 10 else ""
                    if current_date and current_date != last_notif_date:
                        try:
                            await bot.send_message(chat_id, f"🔔 **Новое уведомление:**\n\n{newest}")
                            update_last_notification(telegram_id, current_date)
                        except:
                            pass
                await client.close()
        except Exception as e:
            logger.error(f"Background error: {e}")
        await asyncio.sleep(1800)

async def main():
    logger.info("Бот запускается...")
    asyncio.create_task(background_monitor())
    await bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    asyncio.run(main())