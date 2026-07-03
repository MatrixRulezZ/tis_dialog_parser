import asyncio
import aiohttp
from bs4 import BeautifulSoup as BS
from telebot.async_telebot import AsyncTeleBot
from telebot import types
import re
import logging
import os
import json
import sqlite3
from datetime import datetime
import hashlib
from io import BytesIO

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
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не задан!")
    exit(1)

DB_FILE = "tis_users.db"
HTML_CACHE_DIR = "html_cache"
os.makedirs(HTML_CACHE_DIR, exist_ok=True)

# =========================
# БАЗА ДАННЫХ (SQLite)
# =========================
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
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

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

def add_or_update_user(telegram_id: int, chat_id: int, tis_login: str, tis_password: str):
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
        SET last_balance = ?, last_traffic_gb = ?, last_ip = ?
        WHERE telegram_id = ?
    ''', (balance, traffic_gb, ip, telegram_id))
    conn.commit()
    conn.close()

init_db()

# =========================
# TIS CLIENT (улучшенный парсер + реальный QR)
# =========================
class TISClient:
    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        self.session = None

    async def login(self) -> bool:
        try:
            if self.session and not self.session.closed:
                await self.session.close()
            self.session = aiohttp.ClientSession()

            login_url = "https://stats.tis-dialog.ru/index.php"
            data = {
                "login": self.login,
                "passv": self.password,
                "remember": "1"
            }
            async with self.session.post(login_url, data=data) as resp:
                if resp.status != 200:
                    return False

            # Проверяем успешность логина
            async with self.session.get(login_url) as resp:
                text = await resp.text(encoding='windows-1251', errors='ignore')
                if "Выйти" in text or "Выход" in text:
                    logger.info(f"Успешный логин: {self.login}")
                    return True
            return False
        except Exception as e:
            logger.error(f"Login error for {self.login}: {e}")
            return False

    def _get_value_by_label(self, soup: BS, label: str) -> str:
        for table in soup.select('.lkInfoTable'):
            for row in table.select('tr'):
                tds = row.select('td')
                if len(tds) >= 2 and label.lower() in tds[0].get_text(strip=True).lower():
                    return tds[1].get_text(strip=True)
        return "Н/Д"

    async def fetch_data(self):
        """Получение данных из ЛК"""
        try:
            if not self.session or self.session.closed:
                if not await self.login():
                    return None

            url = "https://stats.tis-dialog.ru/index.php"
            async with self.session.get(url) as resp:
                html_content = await resp.text(encoding='windows-1251', errors='ignore')

            soup = BS(html_content, 'html.parser')
            for a in soup.find_all("a"):
                a.decompose()

            data = {
                "balance": 0.0,
                "status": self._get_value_by_label(soup, "Состояние"),
                "speed": self._get_value_by_label(soup, "Скорость"),
                "ip": "Н/Д",
                "traffic_gb": 0.0,
                "incoming_traffic": "Н/Д",
                "outgoing_traffic": "Н/Д",
                "raw_balance": self._get_value_by_label(soup, "Баланс")
            }

            # Парсинг баланса
            balance_text = data["raw_balance"]
            if balance_text and balance_text != "Н/Д":
                try:
                    num = re.sub(r'[^\d\-.]+', '', balance_text.replace(',', '.'))
                    data["balance"] = float(num) if num else 0.0
                except:
                    pass

            # IP
            activity = self._get_value_by_label(soup, "Журнал сеансов")
            ip_match = re.search(r'IP:\s*(\d{1,3}(?:\.\d{1,3}){3})', activity)
            if ip_match:
                data["ip"] = ip_match.group(1)

            # Турбо-трафик
            turbo_text = self._get_value_by_label(soup, "Остаток турбо")
            if turbo_text and turbo_text != "Н/Д":
                match = re.search(r'([\d.,]+)\s*(Гб|Тб|ГБ|ТБ)', turbo_text)
                if match:
                    val = float(match.group(1).replace(',', '.'))
                    if 'Тб' in match.group(2).lower():
                        val *= 1024
                    data["traffic_gb"] = round(val, 2)

            # Трафик за период
            traffic_table = soup.select_one('.lkTraficTable')
            if traffic_table:
                rows = traffic_table.select('tr')
                if len(rows) > 1:
                    cells = rows[1].select('td')
                    if len(cells) >= 2:
                        data["incoming_traffic"] = cells[0].get_text(strip=True)
                        data["outgoing_traffic"] = cells[1].get_text(strip=True)

            return data

        except Exception as e:
            logger.error(f"fetch_data error: {e}")
            return None

    async def get_real_qr(self):
        """Получает реальный QR-код с сайта"""
        try:
            if not self.session or self.session.closed:
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
        if self.session and not self.session.closed:
            await self.session.close()


# =========================
# TELEGRAM BOT
# =========================
bot = AsyncTeleBot(BOT_TOKEN)

# Временное хранилище состояний регистрации
user_states = {}

@bot.message_handler(commands=['start'])
async def start(message):
    user_id = message.from_user.id
    user = get_user(user_id)

    if user:
        await bot.send_message(
            message.chat.id,
            f"👋 Привет! Ты уже подключен как **{user['tis_login']}**.\n\n"
            "Используй кнопки ниже:",
            parse_mode="Markdown"
        )
        await show_main_menu(message.chat.id)
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Подключить личный кабинет TIS", callback_data="register"))
        await bot.send_message(
            message.chat.id,
            "Привет! Я бот для мониторинга личного кабинета **TIS-Dialog**.\n\n"
            "Чтобы начать, подключи свою учётную запись.",
            reply_markup=markup
        )

async def show_main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 Статус", "📉 Трафик")
    markup.add("💳 Оплатить", "🔔 Уведомления")
    markup.add("🔄 Обновить данные", "⚙️ Настройки")
    await bot.send_message(chat_id, "Выбери действие:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "register")
async def register_start(call):
    user_id = call.from_user.id
    user_states[user_id] = {"step": "login"}
    await bot.send_message(call.message.chat.id, "Введите **логин** от личного кабинета TIS:")
    await bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.from_user.id in user_states)
async def register_process(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {})

    if state.get("step") == "login":
        state["login"] = message.text.strip()
        state["step"] = "password"
        user_states[user_id] = state
        await bot.send_message(message.chat.id, "Теперь введи **пароль** от личного кабинета TIS:")
    elif state.get("step") == "password":
        login = state["login"]
        password = message.text.strip()

        # Проверяем, что данные работают
        client = TISClient(login, password)
        if await client.login():
            add_or_update_user(user_id, message.chat.id, login, password)
            del user_states[user_id]
            await bot.send_message(message.chat.id, "✅ Учётная запись успешно подключена!")
            await show_main_menu(message.chat.id)
        else:
            await bot.send_message(message.chat.id, "❌ Не удалось войти. Проверь логин и пароль.")
            del user_states[user_id]

# =========================
# ОСНОВНЫЕ КОМАНДЫ
# =========================
@bot.message_handler(func=lambda m: m.text == "📊 Статус")
async def cmd_status(message):
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет через /start")
        return

    client = TISClient(user["tis_login"], user["tis_password"])
    data = await client.fetch_data()
    await client.close()

    if data:
        text = (f"📊 **Статус подключения**\n\n"
                f"Баланс: **{data['raw_balance']}**\n"
                f"Статус: {data['status']}\n"
                f"Скорость: {data['speed']}\n"
                f"IP: `{data['ip']}`")
        await bot.send_message(message.chat.id, text, parse_mode="Markdown")
    else:
        await bot.send_message(message.chat.id, "Не удалось получить данные")

@bot.message_handler(func=lambda m: m.text == "💳 Оплатить")
async def cmd_pay(message):
    user = get_user(message.from_user.id)
    if not user:
        await bot.send_message(message.chat.id, "Сначала подключи кабинет")
        return

    client = TISClient(user["tis_login"], user["tis_password"])
    qr_bytes = await client.get_real_qr()
    await client.close()

    if qr_bytes:
        await bot.send_photo(
            message.chat.id,
            qr_bytes,
            caption="Отсканируй QR-код для оплаты через СБП"
        )
    else:
        await bot.send_message(message.chat.id, "Не удалось получить QR-код")

# =========================
# ФОНОВАЯ ПРОВЕРКА (для всех пользователей)
# =========================
async def background_monitor():
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_id, chat_id, tis_login, tis_password, last_balance, last_ip FROM users")
            users = cursor.fetchall()
            conn.close()

            for row in users:
                telegram_id, chat_id, tis_login, tis_password, last_balance, last_ip = row

                client = TISClient(tis_login, tis_password)
                data = await client.fetch_data()
                await client.close()

                if not data:
                    continue

                # Уведомления
                notifications = []

                # Баланс в минус
                if data["balance"] < 0 and last_balance >= 0:
                    notifications.append("⚠️ Баланс ушёл в минус!")

                # Смена IP
                if data["ip"] != "Н/Д" and last_ip and data["ip"] != last_ip:
                    notifications.append(f"🌐 IP-адрес изменился: `{data['ip']}`")

                # Мало трафика
                if data["traffic_gb"] < 100 and last_balance >= 100:  # пример
                    notifications.append(f"📉 Осталось мало трафика: {data['traffic_gb']} Гб")

                if notifications:
                    text = "\n".join(notifications)
                    try:
                        await bot.send_message(chat_id, text, parse_mode="Markdown")
                    except:
                        pass

                # Обновляем статистику
                update_user_stats(telegram_id, data["balance"], data["traffic_gb"], data["ip"])

        except Exception as e:
            logger.error(f"Background monitor error: {e}")

        await asyncio.sleep(1800)  # 30 минут

# =========================
# ЗАПУСК
# =========================
async def main():
    logger.info("Бот запускается...")
    asyncio.create_task(background_monitor())
    await bot.infinity_polling()

if __name__ == "__main__":
    asyncio.run(main())