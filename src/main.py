import asyncio
import aiohttp
from bs4 import BeautifulSoup as BS
from telebot.async_telebot import AsyncTeleBot
from telebot import types
import re
import logging
import os
import sqlite3
from datetime import datetime

# =========================
# ЛОГИРОВАНИЕ
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("tis_dialog_bot")

# =========================
# КОНФИГ
# =========================
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не задан в переменных окружения!")
    exit(1)

DB_FILE = "tis_users.db"
HTML_CACHE_DIR = "html_cache"
os.makedirs(HTML_CACHE_DIR, exist_ok=True)


# =========================
# БАЗА ДАННЫХ
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS users
                   (
                       telegram_id
                       INTEGER
                       PRIMARY
                       KEY,
                       chat_id
                       INTEGER,
                       tis_login
                       TEXT
                       NOT
                       NULL,
                       tis_password
                       TEXT
                       NOT
                       NULL,
                       last_balance
                       REAL
                       DEFAULT
                       0,
                       last_traffic_gb
                       REAL
                       DEFAULT
                       0,
                       last_ip
                       TEXT
                       DEFAULT
                       '',
                       registered_at
                       TIMESTAMP
                       DEFAULT
                       CURRENT_TIMESTAMP
                   )
                   ''')
    conn.commit()
    conn.close()


def get_user(telegram_id: int):
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
            "last_ip": row[6]
        }
    return None


def save_user(telegram_id: int, chat_id: int, tis_login: str, tis_password: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (telegram_id, chat_id, tis_login, tis_password)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, chat_id, tis_login, tis_password))
    conn.commit()
    conn.close()


def update_user_stats(telegram_id: int, balance: float, traffic_gb: float, ip: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
                   UPDATE users
                   SET last_balance=?,
                       last_traffic_gb=?,
                       last_ip=?
                   WHERE telegram_id = ?
                   ''', (balance, traffic_gb, ip, telegram_id))
    conn.commit()
    conn.close()


init_db()


# =========================
# TIS CLIENT
# =========================
class TISClient:
    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        self.session = None

    async def login(self) -> bool:
        try:
            if self.session:
                await self.session.close()
            self.session = aiohttp.ClientSession()
            data = {"login": self.login, "passv": self.password, "remember": "1"}
            async with self.session.post("https://stats.tis-dialog.ru/index.php", data=data) as resp:
                pass
            async with self.session.get("https://stats.tis-dialog.ru/index.php") as resp:
                text = await resp.text(encoding='windows-1251', errors='ignore')
                return "Выйти" in text or "Выход" in text
        except Exception as e:
            logger.error(f"Login error: {e}")
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
            if not self.session:
                if not await self.login():
                    return None

            async with self.session.get("https://stats.tis-dialog.ru/index.php") as resp:
                html = await resp.text(encoding='windows-1251', errors='ignore')

            soup = BS(html, 'html.parser')
            for a in soup.find_all("a"):
                a.decompose()

            data = {
                "raw_balance": self._get_value(soup, "Баланс"),
                "status": self._get_value(soup, "Состояние"),
                "speed": self._get_value(soup, "Скорость"),
                "ip": "Н/Д",
                "traffic_gb": 0.0,
            }

            # Баланс
            try:
                num = re.sub(r'[^\d\-.]+', '', data["raw_balance"].replace(',', '.'))
                data["balance"] = float(num) if num else 0.0
            except:
                data["balance"] = 0.0

            # IP
            activity = self._get_value(soup, "Журнал сеансов")
            match = re.search(r'IP:\s*(\d{1,3}(?:\.\d{1,3}){3})', activity)
            if match:
                data["ip"] = match.group(1)

            # Турбо-трафик
            turbo = self._get_value(soup, "Остаток турбо")
            m = re.search(r'([\d.,]+)\s*(Гб|Тб)', turbo, re.I)
            if m:
                val = float(m.group(1).replace(',', '.'))
                data["traffic_gb"] = val * 1024 if 'т' in m.group(2).lower() else val

            return data
        except Exception as e:
            logger.error(f"fetch_data error: {e}")
            return None

    async def get_qr(self):
        try:
            if not self.session:
                if not await self.login():
                    return None
            url = f"https://stats.tis-dialog.ru/qrpay.php?phnumber={self.login}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
            return None
        except Exception as e:
            logger.error(f"QR error: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()


# =========================
# TELEGRAM BOT
# =========================
bot = AsyncTeleBot(BOT_TOKEN)
user_states = {}  # {user_id: {"step": "login" или "password", "login": "..."} }


@bot.message_handler(commands=['start'])
async def start_cmd(message):
    user = get_user(message.from_user.id)
    if user:
        await bot.send_message(message.chat.id, f"Привет! Ты подключен как `{user['tis_login']}`",
                               parse_mode="Markdown")
        await show_menu(message.chat.id)
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Подключить кабинет TIS", callback_data="start_register"))
        await bot.send_message(message.chat.id,
                               "Добро пожаловать!\nНажми кнопку ниже, чтобы подключить свой личный кабинет.",
                               reply_markup=markup)


async def show_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 Статус", "📉 Трафик")
    markup.add("💳 Оплатить", "🔄 Обновить")
    await bot.send_message(chat_id, "Выбери действие:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "start_register")
async def start_register(call):
    user_id = call.from_user.id
    user_states[user_id] = {"step": "login"}
    await bot.send_message(call.message.chat.id, "Введите **логин** от личного кабинета TIS:")
    await bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user.id in user_states)
async def registration_handler(message):
    user_id = message.from_user.id
    state = user_states[user_id]

    if state["step"] == "login":
        state["login"] = message.text.strip()
        state["step"] = "password"
        await bot.send_message(message.chat.id, "Теперь введи **пароль**:")

    elif state["step"] == "password":
        login = state["login"]
        password = message.text.strip()

        client = TISClient(login, password)
        if await client.login():
            save_user(user_id, message.chat.id, login, password)
            del user_states[user_id]
            await bot.send_message(message.chat.id, "✅ Учётная запись успешно подключена!")
            await show_menu(message.chat.id)
        else:
            await bot.send_message(message.chat.id, "❌ Не удалось войти. Проверь логин и пароль.")
            del user_states[user_id]
        await client.close()


@bot.message_handler(func=lambda m: m.text == "📊 Статус")
async def status_cmd(message):
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет через /start")
        return

    client = TISClient(user["tis_login"], user["tis_password"])
    data = await client.fetch_data()
    await client.close()

    if data:
        text = (f"📊 **Статус**\n\n"
                f"Баланс: **{data['raw_balance']}**\n"
                f"Статус: {data['status']}\n"
                f"IP: `{data['ip']}`")
        await bot.send_message(message.chat.id, text, parse_mode="Markdown")
    else:
        await bot.send_message(message.chat.id, "Не удалось получить данные")


@bot.message_handler(func=lambda m: m.text == "💳 Оплатить")
async def pay_cmd(message):
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет")
        return

    client = TISClient(user["tis_login"], user["tis_password"])
    qr = await client.get_qr()
    await client.close()

    if qr:
        await bot.send_photo(message.chat.id, qr, caption="QR-код для оплаты (СБП)")
    else:
        await bot.send_message(message.chat.id, "Не удалось получить QR-код")


@bot.message_handler(func=lambda m: m.text == "🔄 Обновить")
async def refresh_cmd(message):
    await status_cmd(message)


# =========================
# ФОНОВАЯ ПРОВЕРКА
# =========================
async def background_check():
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            users = conn.execute("SELECT telegram_id, chat_id, tis_login, tis_password, last_ip FROM users").fetchall()
            conn.close()

            for telegram_id, chat_id, login, password, last_ip in users:
                client = TISClient(login, password)
                data = await client.fetch_data()
                await client.close()

                if not data:
                    continue

                notifications = []
                if data["balance"] < 0:
                    notifications.append("⚠️ Баланс ушёл в минус!")
                if data["ip"] != "Н/Д" and last_ip and data["ip"] != last_ip:
                    notifications.append(f"🌐 IP изменился: `{data['ip']}`")

                for text in notifications:
                    try:
                        await bot.send_message(chat_id, text, parse_mode="Markdown")
                    except:
                        pass

                update_user_stats(telegram_id, data["balance"], data["traffic_gb"], data["ip"])

        except Exception as e:
            logger.error(f"Background error: {e}")

        await asyncio.sleep(1800)  # 30 минут


# =========================
# ЗАПУСК
# =========================
async def main():
    logger.info("Бот запускается...")
    asyncio.create_task(background_check())
    await bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    asyncio.run(main())