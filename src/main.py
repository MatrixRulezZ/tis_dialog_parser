import asyncio
import aiohttp
from bs4 import BeautifulSoup as BS
from telebot.async_telebot import AsyncTeleBot
from telebot import types
import re
import logging
import time
import os
import json
from collections import OrderedDict
from datetime import datetime
import hashlib
import qrcode
from io import BytesIO

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Получаем конфигурацию из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
CHAT_ID = int(os.getenv('CHAT_ID', '0'))
TIS_LOGIN = os.getenv('TIS_LOGIN', '')
TIS_PASSWORD = os.getenv('TIS_PASSWORD', '')

# Проверка обязательных переменных
if not BOT_TOKEN or not CHAT_ID or not TIS_LOGIN or not TIS_PASSWORD:
    logger.error("Необходимые переменные окружения не установлены!")
    exit(1)

# Глобальное хранилище для уведомлений
notifications_store = OrderedDict()
MAX_NOTIFICATIONS = 50
BACKUP_FILE = "notifications_backup.json"
HTML_CACHE_DIR = "html_cache"
os.makedirs(HTML_CACHE_DIR, exist_ok=True)


# Функции для работы с резервными копиями
def load_notifications():
    global notifications_store
    try:
        if os.path.exists(BACKUP_FILE):
            with open(BACKUP_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for item in data:
                    notifications_store[item['id']] = item
                logger.info(f"Загружено {len(notifications_store)} уведомлений из резервной копии")
    except Exception as e:
        logger.error(f"Ошибка при загрузке резервной копии: {e}")


def save_notifications():
    try:
        with open(BACKUP_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(notifications_store.values()), f, ensure_ascii=False, indent=2)
        logger.info(f"Резервная копия уведомлений сохранена ({len(notifications_store)} записей)")
    except Exception as e:
        logger.error(f"Ошибка при сохранении резервной копии: {e}")


# Загружаем уведомления при запуске
load_notifications()


async def login(session):
    base_url = 'https://stats.tis-dialog.ru'
    login_url = f'{base_url}/index.php'
    user_agent_val = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'

    if session.cookie_jar and any(c.key == 'sid' for c in session.cookie_jar):
        logger.info("Используем существующую сессию")
        return True

    data = {
        'backUrl': login_url,
        'login': TIS_LOGIN,
        'passv': TIS_PASSWORD,
        'remember': 'yes',
    }
    headers = {
        'User-Agent': user_agent_val,
        'Referer': login_url,
        'Origin': base_url,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    try:
        async with session.post(login_url, data=data, headers=headers) as response:
            if response.status != 200:
                logger.error(f"Ошибка входа: статус {response.status}")
                return False
            async with session.get(login_url) as lk_response:
                html_content = await lk_response.text(encoding='windows-1251')
                html = BS(html_content, 'html.parser')
                title = html.title.string if html.title else ""
                if "Личный кабинет Тис-диалог" not in title:
                    logger.error("Не удалось войти в личный кабинет")
                    return False
            return True
    except Exception as e:
        logger.error(f"Ошибка при входе: {e}")
        return False


def cache_key(url):
    return hashlib.md5(url.encode()).hexdigest()


async def cached_fetch(session, url, cache_ttl=300):
    cache_file = os.path.join(HTML_CACHE_DIR, cache_key(url))
    if os.path.exists(cache_file):
        file_age = time.time() - os.path.getmtime(cache_file)
        if file_age < cache_ttl:
            with open(cache_file, 'r', encoding='windows-1251') as f:
                return f.read()
    async with session.get(url) as response:
        if response.status != 200:
            return None
        content = await response.text(encoding='windows-1251')
        with open(cache_file, 'w', encoding='windows-1251') as f:
            f.write(content)
        return content


async def fetch_notifications(session):
    notifications_url = f"https://stats.tis-dialog.ru/index.php?mod=msg&phnumber={TIS_LOGIN}"
    try:
        html_content = await cached_fetch(session, notifications_url, cache_ttl=600)
        if not html_content:
            logger.error("Ошибка при загрузке страницы уведомлений")
            return []
        soup = BS(html_content, 'html.parser')
        new_notifications = []
        for div in soup.find_all('div', style="margin-bottom:15px;"):
            date_match = re.search(r'\d{2}\.\d{2}\.\d{4}', div.text)
            if not date_match:
                continue
            date_text = date_match.group(0)
            link = div.find('a')
            if not link:
                continue
            full_url = "https://stats.tis-dialog.ru/" + link['href']
            match = re.search(r'comsg=(\d+)', full_url)
            if not match:
                continue
            notif_id = match.group(1)
            if notif_id in notifications_store:
                continue
            notif_html = await cached_fetch(session, full_url, cache_ttl=3600)
            if not notif_html:
                continue
            notif_soup = BS(notif_html, 'html.parser')
            content_block = notif_soup.find('div', class_='contentBlock')
            if not content_block:
                continue
            message_text = content_block.get_text(strip=True, separator=' ')[:500]
            notification_data = {
                'id': notif_id,
                'date': date_text,
                'subject': link.text.strip(),
                'text': message_text,
                'full_url': full_url,
                'added_at': datetime.now().isoformat()
            }
            notifications_store[notif_id] = notification_data
            if len(notifications_store) > MAX_NOTIFICATIONS:
                oldest_id = next(iter(notifications_store))
                del notifications_store[oldest_id]
            new_notifications.append(notification_data)
        if new_notifications:
            save_notifications()
        return new_notifications
    except Exception as e:
        logger.error(f"Ошибка при получении уведомлений: {e}")
        return []


def parse_traffic_value(text):
    if not text:
        return None
    cleaned = text.replace('\xa0', ' ').strip()
    pattern = r'\(?\s*([\d\s,.]+)\s*(Гб|гб|GB|Gb|Тб|ТБ|TB)\)?'
    match = re.search(pattern, cleaned)
    if not match:
        return None
    num_str, unit = match.groups()
    num_str = num_str.replace(' ', '').replace(',', '.')
    try:
        value = float(num_str)
    except ValueError:
        return None
    unit = unit.lower()
    if 'т' in unit:
        value *= 1024
    return value


class AsyncValues:
    def __init__(self):
        self.speed = "Н/Д"
        self.money = "Н/Д"
        self.traffic_gb = 0.0
        self.traffic_str = "Н/Д"
        self.balance = 0.0
        self.status = "Н/Д"
        self.last_success = False
        self.incoming_traffic = "Н/Д"
        self.outgoing_traffic = "Н/Д"
        self.ip_address = "Н/Д"
        self.last_fetch = 0
        self.cache_duration = 300
        self.lock = asyncio.Lock()

    async def fetch(self, force=False):
        current_time = time.time()
        if not force and current_time - self.last_fetch < self.cache_duration:
            return self.last_success
        async with self.lock:
            if not force and current_time - self.last_fetch < self.cache_duration:
                return self.last_success
            try:
                async with aiohttp.ClientSession() as session:
                    if not await login(session):
                        return False
                    html_content = await cached_fetch(session, 'https://stats.tis-dialog.ru/index.php', cache_ttl=300)
                    if not html_content:
                        return False
                    html = BS(html_content, 'html.parser')
                    title = html.title.string if html.title else ""
                    if "Личный кабинет Тис-диалог" not in title:
                        return False
                    for a in html.find_all("a"):
                        a.decompose()
                    tables = html.select('.lkInfoTable')
                    if len(tables) > 0:
                        rows = tables[0].select('tr')
                        if len(rows) > 3:
                            money_cell = rows[3].select('td')
                            if len(money_cell) > 1:
                                self.money = money_cell[1].get_text(strip=True)
                                try:
                                    balance_str = self.money.replace('руб.', '').replace(',', '.').strip()
                                    balance_str = re.sub(r'[^\d.]+', '', balance_str)
                                    self.balance = float(balance_str) if balance_str else 0.0
                                except Exception:
                                    self.balance = 0.0
                        if len(rows) > 4:
                            status_cell = rows[4].select('td')
                            if len(status_cell) > 1:
                                self.status = status_cell[1].get_text(strip=True)
                    if len(tables) > 1:
                        rows = tables[1].select('tr')
                        if len(rows) > 0:
                            speed_cell = rows[0].select('td')
                            if len(speed_cell) > 1:
                                self.speed = speed_cell[1].get_text(strip=True).split('Журнал сеансов')[0].strip()
                        if len(rows) > 1:
                            ip_cell = rows[1].select('td')
                            if len(ip_cell) > 1:
                                activity_text = ip_cell[1].get_text(strip=True)
                                ip_match = re.search(r'IP:(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', activity_text)
                                self.ip_address = ip_match.group(1) if ip_match else "Н/Д"
                        if len(rows) > 2:
                            traffic_cell = rows[2].select('td')
                            if len(traffic_cell) > 1:
                                value = parse_traffic_value(traffic_cell[1].get_text(strip=True))
                                if value:
                                    self.traffic_gb = value
                                    self.traffic_str = f"{value:.2f} Гб"
                                else:
                                    self.traffic_str = "Н/Д"
                    traffic_table = html.select_one('.lkTraficTable')
                    if traffic_table:
                        rows = traffic_table.select('tr')
                        if len(rows) > 1:
                            cells = rows[1].select('td')
                            if len(cells) > 1:
                                inc_val = parse_traffic_value(cells[0].get_text(strip=True))
                                self.incoming_traffic = f"{inc_val:.2f} Гб" if inc_val else "Н/Д"
                                out_val = parse_traffic_value(cells[1].get_text(strip=True))
                                self.outgoing_traffic = f"{out_val:.2f} Гб" if out_val else "Н/Д"
                    self.last_fetch = current_time
                    self.last_success = True
                    return True
            except Exception as e:
                logger.error(f"Ошибка при получении данных: {str(e)}")
            self.last_success = False
            return False


async def check_notifications(bot, chat_id):
    failed_attempts = 0
    last_notification_check = 0
    last_traffic_check = 0
    cache_values = None
    notification_session = None
    try:
        while True:
            current_time = time.time()
            need_traffic_check = current_time - last_traffic_check > 1800
            need_notification_check = current_time - last_notification_check > 21600
            if not need_traffic_check and not need_notification_check:
                await asyncio.sleep(300)
                continue
            try:
                if not notification_session or notification_session.closed:
                    notification_session = aiohttp.ClientSession()
                if need_traffic_check:
                    values = AsyncValues()
                    success = await values.fetch()
                    if not success:
                        failed_attempts += 1
                        await asyncio.sleep(min(300 * 2 ** failed_attempts, 3600))
                        continue
                    failed_attempts = 0
                    last_traffic_check = current_time
                    if values.traffic_gb < 100 and (not cache_values or cache_values.traffic_gb >= 100):
                        await bot.send_message(chat_id, f"⚠️ Осталось мало трафика\n📊 {values.traffic_gb:.2f} Гб")
                    if values.balance < 0 and (not cache_values or cache_values.balance >= 0):
                        await bot.send_message(chat_id, f"⚠️ Отрицательный баланс\n💰 {values.balance:.2f} руб.")
                    cache_values = values
                if need_notification_check:
                    last_notification_check = current_time
                    if await login(notification_session):
                        new_notifications = await fetch_notifications(notification_session)
                        for notification in new_notifications:
                            await bot.send_message(chat_id, f"🔔 Новое уведомление\n📅 {notification['date']}\n📝 {notification['subject']}\n\n{notification['text']}")
                            await asyncio.sleep(0.5)
                await asyncio.sleep(300)
            except Exception as e:
                logger.error(f"Ошибка фоновой задачи: {str(e)}")
                if notification_session:
                    await notification_session.close()
                    notification_session = None
                await asyncio.sleep(300)
    finally:
        if notification_session and not notification_session.closed:
            await notification_session.close()


class TisDialogBot:
    def __init__(self):
        self.bot = AsyncTeleBot(BOT_TOKEN, parse_mode='Markdown')
        self.chat_id = CHAT_ID
        self.values = AsyncValues()
        self.setup_handlers()

    def create_keyboard(self):
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add(types.KeyboardButton('📊 Статус подключения'),
                   types.KeyboardButton('🌐 Остаток трафика'),
                   types.KeyboardButton('💳 Оплатить'),
                   types.KeyboardButton('🔔 Уведомления'),
                   types.KeyboardButton('⚙️ Настройки'),
                   types.KeyboardButton('🔄 Обновить данные'))
        return markup

    def generate_payment_qr(self, amount, personal_account):
        sbp_data = [
            "ST00012",
            "Name=ООО ТИС - ДИАЛОГ",
            f"PersonalAcc=40702810420230000176",
            "BankName=КАЛИНИНГРАДСКОЕ ОТДЕЛЕНИЕ N8626 ПАО СБЕРБАНК",
            f"BIC=042748634",
            f"CorrespAcc=30101810100000000634",
            f"PayeeINN=3908602823",
            f"Purpose=Оплата интернет услуг л/с {personal_account}",
            f"Sum={int(amount * 100)}",
            f"PersAcc={personal_account}"
        ]
        sbp_string = "|".join(sbp_data)
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=4)
        qr.add_data(sbp_string)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img_byte_arr = BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        return img_byte_arr

    def setup_handlers(self):
        @self.bot.message_handler(commands=['start', 'help'])
        async def welcome_msg(message):
            if message.chat.id == self.chat_id:
                await self.bot.send_message(message.chat.id, "👋 Бот запущен", reply_markup=self.create_keyboard())
            else:
                await self.bot.send_message(message.chat.id, "🚫 Доступ запрещен!")

        # здесь твои обработчики "Статус подключения", "Остаток трафика", "Оплатить", "Уведомления", "Настройки", "Обновить данные"
        # я оставил структуру, чтобы код влез в сообщение
        # они работают без изменений — просто используй свои хендлеры из оригинала

    async def run(self):
        asyncio.create_task(check_notifications(self.bot, self.chat_id))
        await self.bot.infinity_polling()


async def main():
    bot = TisDialogBot()
    await bot.run()


if __name__ == '__main__':
    asyncio.run(main())
