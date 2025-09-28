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

# =========================
# ЛОГИРОВАНИЕ
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("tis_dialog_bot")

# =========================
# КОНФИГ И ОКРУЖЕНИЕ
# =========================
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
CHAT_ID = int(os.getenv('CHAT_ID', '0'))
TIS_LOGIN = os.getenv('TIS_LOGIN', '')
TIS_PASSWORD = os.getenv('TIS_PASSWORD', '')

if not BOT_TOKEN or not CHAT_ID or not TIS_LOGIN or not TIS_PASSWORD:
    logger.error("Необходимые переменные окружения не установлены! "
                 "Нужно задать BOT_TOKEN, CHAT_ID, TIS_LOGIN, TIS_PASSWORD")
    exit(1)

# =========================
# ХРАНИЛИЩЕ УВЕДОМЛЕНИЙ
# =========================
notifications_store = OrderedDict()
MAX_NOTIFICATIONS = 50
BACKUP_FILE = "notifications_backup.json"
HTML_CACHE_DIR = "html_cache"
os.makedirs(HTML_CACHE_DIR, exist_ok=True)


# =========================
# БЭКАП УВЕДОМЛЕНИЙ
# =========================
def load_notifications():
    """Загрузка уведомлений из файла."""
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
    """Сохранение уведомлений в файл."""
    try:
        with open(BACKUP_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(notifications_store.values()), f, ensure_ascii=False, indent=2)
        logger.info(f"Резервная копия уведомлений сохранена ({len(notifications_store)} записей)")
    except Exception as e:
        logger.error(f"Ошибка при сохранении резервной копии: {e}")


load_notifications()


# =========================
# HTTP/СЕССИИ/КЭШ
# =========================
def cache_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


async def cached_fetch(session: aiohttp.ClientSession, url: str, cache_ttl: int = 300):
    """GET с кэшированием html (Windows-1251)."""
    cache_file = os.path.join(HTML_CACHE_DIR, cache_key(url))
    try:
        if os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < cache_ttl:
                with open(cache_file, 'r', encoding='windows-1251', errors='ignore') as f:
                    return f.read()
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"GET {url} -> {resp.status}")
                return None
            content = await resp.text(encoding='windows-1251', errors='ignore')
            with open(cache_file, 'w', encoding='windows-1251', errors='ignore') as f:
                f.write(content)
            return content
    except Exception as e:
        logger.error(f"cached_fetch error for {url}: {e}")
        return None


async def login(session: aiohttp.ClientSession) -> bool:
    """Логин в ЛК TIS-Dialog, проверка заголовка."""
    base_url = 'https://stats.tis-dialog.ru'
    login_url = f'{base_url}/index.php'
    data = {
        'backUrl': login_url,
        'login': TIS_LOGIN,
        'passv': TIS_PASSWORD,
        'remember': 'yes',
    }
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': login_url,
        'Origin': base_url
    }
    try:
        async with session.post(login_url, data=data, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"Ошибка входа: HTTP {resp.status}")
                return False

        async with session.get(login_url) as lk_resp:
            if lk_resp.status != 200:
                logger.error(f"Ошибка проверки входа: HTTP {lk_resp.status}")
                return False
            html_content = await lk_resp.text(encoding='windows-1251', errors='ignore')
            html = BS(html_content, 'html.parser')
            title = html.title.string if html.title else ""
            if "Личный кабинет Тис-диалог" not in title:
                logger.error("Вход не выполнен: заголовок не совпадает")
                return False
        return True
    except Exception as e:
        logger.error(f"login error: {e}")
        return False


# =========================
# ПАРСИНГ ТРАФИКА/ЗНАЧЕНИЙ
# =========================
def parse_traffic_value(text: str):
    """
    Универсальный парсер значений трафика:
    Поддержка: '123,45 Гб', '1 234,5 Гб', '(12.3 Гб)', '0.5 Тб', '120GB', '0,1 TB' и т.п.
    Возвращает значение в ГБ (float) или None.
    """
    if not text:
        return None
    cleaned = text.replace('\xa0', ' ').strip()
    # Ищем число + единицу
    # допускаем пробелы, запятые, точки
    pattern = r'\(?\s*([\d\s.,]+)\s*(Гб|гб|GB|Gb|gb|Тб|ТБ|TB|Tb)\)?'
    m = re.search(pattern, cleaned)
    if not m:
        return None
    num_str, unit = m.groups()
    num_str = num_str.replace(' ', '').replace(',', '.')
    try:
        value = float(num_str)
    except ValueError:
        return None
    unit = unit.lower()
    # Тб -> в Гб
    if unit.startswith('т') or unit == 'tb':
        value *= 1024
    return value


# =========================
# ЗАГРУЗКА УВЕДОМЛЕНИЙ
# =========================
async def fetch_notifications(session: aiohttp.ClientSession):
    """Получение новых уведомлений из ЛК, с кэшем и сохранением в файл."""
    url = f"https://stats.tis-dialog.ru/index.php?mod=msg&phnumber={TIS_LOGIN}"
    try:
        html_content = await cached_fetch(session, url, cache_ttl=600)
        if not html_content:
            logger.error("Не удалось загрузить страницу уведомлений")
            return []

        soup = BS(html_content, 'html.parser')
        new_items = []

        for div in soup.find_all('div', style="margin-bottom:15px;"):
            date_match = re.search(r'\d{2}\.\d{2}\.\d{4}', div.get_text(" ", strip=True))
            if not date_match:
                continue
            date_text = date_match.group(0)

            link = div.find('a')
            if not link or not link.get('href'):
                continue

            full_url = "https://stats.tis-dialog.ru/" + link['href']
            id_match = re.search(r'comsg=(\d+)', full_url)
            if not id_match:
                continue
            notif_id = id_match.group(1)

            if notif_id in notifications_store:
                continue

            notif_html = await cached_fetch(session, full_url, cache_ttl=3600)
            if not notif_html:
                continue

            notif_soup = BS(notif_html, 'html.parser')
            content_block = notif_soup.find('div', class_='contentBlock')
            if not content_block:
                continue

            message_text = content_block.get_text(" ", strip=True)
            # ограничим длину для компактности
            if len(message_text) > 2000:
                message_text = message_text[:2000] + "…"

            item = {
                'id': notif_id,
                'date': date_text,
                'subject': link.get_text(strip=True),
                'text': message_text,
                'full_url': full_url,
                'added_at': datetime.now().isoformat()
            }
            notifications_store[notif_id] = item

            # лимит по количеству в памяти
            if len(notifications_store) > MAX_NOTIFICATIONS:
                oldest = next(iter(notifications_store))
                del notifications_store[oldest]

            new_items.append(item)

        if new_items:
            save_notifications()

        return new_items
    except Exception as e:
        logger.error(f"fetch_notifications error: {e}")
        return []


# =========================
# ОСНОВНЫЕ ЗНАЧЕНИЯ/ПАРСИНГ
# =========================
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
        self.cache_duration = 300  # 5 минут
        self.lock = asyncio.Lock()

    async def fetch(self, force: bool = False):
        """Получение значений из ЛК с кэшированием."""
        current = time.time()
        if not force and current - self.last_fetch < self.cache_duration and self.last_success:
            return True

        async with self.lock:
            # повторная проверка после получения lock
            if not force and current - self.last_fetch < self.cache_duration and self.last_success:
                return True

            try:
                async with aiohttp.ClientSession() as session:
                    if not await login(session):
                        self.last_success = False
                        return False

                    login_url = 'https://stats.tis-dialog.ru/index.php'
                    html_content = await cached_fetch(session, login_url, cache_ttl=300)
                    if not html_content:
                        self.last_success = False
                        return False

                    html = BS(html_content, 'html.parser')

                    # Убираем ссылки для чистоты текста
                    for a in html.find_all("a"):
                        a.decompose()

                    tables = html.select('.lkInfoTable')

                    # Первая таблица: баланс и статус
                    if len(tables) > 0:
                        rows = tables[0].select('tr')
                        # Баланс
                        if len(rows) > 3:
                            tds = rows[3].select('td')
                            if len(tds) > 1:
                                self.money = tds[1].get_text(strip=True)
                                try:
                                    balance_str = self.money.replace('руб.', '').replace(',', '.')
                                    balance_str = re.sub(r'[^\d\-.]+', '', balance_str)
                                    self.balance = float(balance_str) if balance_str else 0.0
                                except Exception:
                                    self.balance = 0.0
                        # Статус подключения
                        if len(rows) > 4:
                            tds = rows[4].select('td')
                            if len(tds) > 1:
                                self.status = tds[1].get_text(strip=True)

                    # Вторая таблица: скорость, активность+IP, остаток трафика
                    if len(tables) > 1:
                        rows = tables[1].select('tr')
                        # Скорость
                        if len(rows) > 0:
                            tds = rows[0].select('td')
                            if len(tds) > 1:
                                self.speed = tds[1].get_text(strip=True)
                                if 'Журнал сеансов' in self.speed:
                                    self.speed = self.speed.split('Журнал сеансов')[0].strip()
                        # IP
                        if len(rows) > 1:
                            tds = rows[1].select('td')
                            if len(tds) > 1:
                                activity_text = tds[1].get_text(strip=True)
                                ip_match = re.search(r'IP:\s*(\d{1,3}(?:\.\d{1,3}){3})', activity_text)
                                self.ip_address = ip_match.group(1) if ip_match else "Н/Д"
                        # Остаток
                        if len(rows) > 2:
                            tds = rows[2].select('td')
                            if len(tds) > 1:
                                val = parse_traffic_value(tds[1].get_text(strip=True))
                                if val is not None:
                                    self.traffic_gb = float(val)
                                    self.traffic_str = f"{self.traffic_gb:.2f} Гб"
                                else:
                                    self.traffic_gb = 0.0
                                    self.traffic_str = "Н/Д"

                    # Таблица трафика за период
                    traffic_table = html.select_one('.lkTraficTable')
                    if traffic_table:
                        rows = traffic_table.select('tr')
                        if len(rows) > 1:
                            cells = rows[1].select('td')
                            if len(cells) > 1:
                                inc_val = parse_traffic_value(cells[0].get_text(strip=True))
                                out_val = parse_traffic_value(cells[1].get_text(strip=True))
                                self.incoming_traffic = f"{inc_val:.2f} Гб" if inc_val is not None else "Н/Д"
                                self.outgoing_traffic = f"{out_val:.2f} Гб" if out_val is not None else "Н/Д"

                    self.last_fetch = time.time()
                    self.last_success = True
                    return True

            except aiohttp.ClientError as e:
                logger.error(f"Сеть: {e}")
            except Exception as e:
                logger.error(f"fetch error: {str(e)[:200]}")
            self.last_success = False
            return False


# =========================
# ФОН: ПРОВЕРКИ/УВЕДОМЛЕНИЯ
# =========================
async def check_notifications(bot: AsyncTeleBot, chat_id: int):
    """Фоновая задача: проверка уведомлений и порогов раз в X времени."""
    failed_attempts = 0
    last_notification_check = 0
    last_traffic_check = 0
    cache_values = None
    notification_session = None

    try:
        while True:
            current_time = time.time()
            need_traffic_check = current_time - last_traffic_check > 1800  # 30 мин
            need_notification_check = current_time - last_notification_check > 21600  # 6 часов

            if not need_traffic_check and not need_notification_check:
                await asyncio.sleep(300)  # 5 минут
                continue

            try:
                if not notification_session or notification_session.closed:
                    notification_session = aiohttp.ClientSession()

                # Проверка трафика/баланса
                if need_traffic_check:
                    values = AsyncValues()
                    success = await values.fetch()

                    if not success:
                        failed_attempts += 1
                        wait_time = min(300 * (2 ** failed_attempts), 3600)
                        logger.warning(f"Ошибка получения данных, повтор через {wait_time} сек.")
                        await asyncio.sleep(wait_time)
                        continue

                    failed_attempts = 0
                    last_traffic_check = current_time

                    # Мало трафика
                    if values.traffic_gb < 100:
                        if not cache_values or cache_values.traffic_gb >= 100:
                            await bot.send_message(
                                chat_id,
                                f"⚠️ *Внимание! Осталось мало трафика*\n📊 Остаток: {values.traffic_gb:.2f} Гб",
                                parse_mode='Markdown'
                            )

                    # Отрицательный баланс
                    if values.balance < 0:
                        if not cache_values or cache_values.balance >= 0:
                            await bot.send_message(
                                chat_id,
                                f"⚠️ *Отрицательный баланс*\n💰 Баланс: {values.balance:.2f} руб.",
                                parse_mode='Markdown'
                            )

                    # Смена IP
                    if cache_values and values.ip_address != "Н/Д" and values.ip_address != cache_values.ip_address:
                        await bot.send_message(
                            chat_id,
                            f"ℹ️ *IP-адрес изменился*\n🌐 Новый IP: `{values.ip_address}`",
                            parse_mode='Markdown'
                        )

                    cache_values = values

                # Проверка новых уведомлений в ЛК
                if need_notification_check:
                    last_notification_check = current_time
                    if await login(notification_session):
                        new_notifications = await fetch_notifications(notification_session)
                        for notification in new_notifications:
                            text = (
                                f"🔔 *Новое уведомление*\n"
                                f"📅 *Дата:* {notification['date']}\n"
                                f"📝 *Тема:* {notification['subject']}\n\n"
                                f"{notification['text']}"
                            )
                            await bot.send_message(chat_id, text, parse_mode='Markdown')
                            await asyncio.sleep(0.5)

                await asyncio.sleep(300)

            except Exception as e:
                logger.error(f"Ошибка фоновой задачи: {str(e)[:200]}")
                if notification_session:
                    await notification_session.close()
                    notification_session = None
                await asyncio.sleep(300)

    finally:
        if notification_session and not notification_session.closed:
            await notification_session.close()


# =========================
# TELEGRAM BOT
# =========================
class TisDialogBot:
    def __init__(self):
        self.bot = AsyncTeleBot(BOT_TOKEN, parse_mode='Markdown')
        self.chat_id = CHAT_ID
        self.values = AsyncValues()
        self.setup_handlers()

    # ------- Кнопки -------
    def create_keyboard(self):
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        btn_status = types.KeyboardButton('📊 Статус подключения')
        btn_traffic = types.KeyboardButton('🌐 Остаток трафика')
        btn_payment = types.KeyboardButton('💳 Оплатить')
        btn_notifications = types.KeyboardButton('🔔 Уведомления')
        btn_settings = types.KeyboardButton('⚙️ Настройки')
        btn_refresh = types.KeyboardButton('🔄 Обновить данные')  # оставим, чтобы не ломать привычку
        markup.add(btn_status, btn_traffic, btn_payment, btn_notifications, btn_settings, btn_refresh)
        return markup

    # ------- QR оплата -------
    def generate_payment_qr(self, amount: float, personal_account: str):
        """Генерация QR для СБП (локально)."""
        sbp_data = [
            "ST00012",
            "Name=ООО ТИС - ДИАЛОГ",
            "PersonalAcc=40702810420230000176",
            "BankName=КАЛИНИНГРАДСКОЕ ОТДЕЛЕНИЕ N8626 ПАО СБЕРБАНК",
            "BIC=042748634",
            "CorrespAcc=30101810100000000634",
            "PayeeINN=3908602823",
            f"Purpose=Оплата интернет услуг л/с {personal_account}",
            f"Sum={int(max(amount, 0) * 100)}",  # копейки
            f"PersAcc={personal_account}"
        ]
        sbp_string = "|".join(sbp_data)

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=4,
        )
        qr.add_data(sbp_string)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        img_byte_arr = BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        return img_byte_arr

    # ------- Обработчики -------
    def setup_handlers(self):
        bot = self.bot

        @bot.message_handler(commands=['start', 'help'])
        async def welcome_msg(message):
            if message.chat.id == self.chat_id:
                await bot.send_message(
                    message.chat.id,
                    "👋 *Бот мониторинга Тис-Диалог запущен!*\n\n"
                    "Используйте кнопки меню:\n\n"
                    "📊 Статус подключения — полная информация\n"
                    "🌐 Остаток трафика — данные о трафике\n"
                    "💳 Оплатить — сгенерировать QR для оплаты\n"
                    "🔔 Уведомления — последние системные сообщения\n"
                    "⚙️ Настройки — параметры мониторинга\n"
                    "🔄 Обновить данные — принудительное обновление",
                    parse_mode='Markdown',
                    reply_markup=self.create_keyboard()
                )
            else:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')

        @bot.message_handler(func=lambda msg: msg.text == '📊 Статус подключения')
        async def send_full_status(message):
            if message.chat.id != self.chat_id:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return

            await bot.send_chat_action(message.chat.id, 'typing')
            success = await self.values.fetch()  # используем кэш (5 мин)
            if success:
                status_icon = "✅" if "подключен" in self.values.status.lower() else "❌"
                text = (
                    f"🌐 *Статус подключения*\n\n"
                    f"{status_icon} *Состояние:* {self.values.status}\n"
                    f"🌐 *IP-адрес:* `{self.values.ip_address}`\n"
                    f"💰 *Баланс:* {self.values.money}\n"
                    f"🚀 *Скорость:* {self.values.speed}\n"
                    f"📊 *Остаток трафика:* {self.values.traffic_str}\n"
                    f"⬇️ *Входящий трафик:* {self.values.incoming_traffic}\n"
                    f"⬆️ *Исходящий трафик:* {self.values.outgoing_traffic}\n"
                    f"💳 *Состояние счета:* {'✅ Положительный' if self.values.balance >= 0 else '⚠️ Отрицательный'}"
                )
                await bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=self.create_keyboard())
            else:
                await bot.send_message(message.chat.id, "❌ Не удалось получить данные. Попробуйте позже.", reply_markup=self.create_keyboard())

        @bot.message_handler(func=lambda msg: msg.text == '🌐 Остаток трафика')
        async def send_traffic_status(message):
            if message.chat.id != self.chat_id:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return

            await bot.send_chat_action(message.chat.id, 'typing')
            success = await self.values.fetch()
            if success:
                await bot.send_message(
                    message.chat.id,
                    f"🌐 *Остаток трафика:*\n📊 {self.values.traffic_str}",
                    parse_mode='Markdown',
                    reply_markup=self.create_keyboard()
                )
            else:
                await bot.send_message(message.chat.id, "❌ Не удалось получить данные о трафике.", reply_markup=self.create_keyboard())

        @bot.message_handler(func=lambda msg: msg.text == '💳 Оплатить')
        async def start_payment(message):
            if message.chat.id != self.chat_id:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return

            await self.values.fetch()
            balance = self.values.balance

            msg_text = "💳 *Оплата услуг*\n\n"
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

            if balance < 0:
                debt = abs(balance)
                msg_text += f"📉 У вас задолженность: *{debt:.2f} руб.*\n"
                msg_text += "Вы можете оплатить:\n"
                msg_text += f"1) Точную сумму долга ({debt:.2f} руб.)\n"
                msg_text += "2) Произвольную сумму\n\n"
                msg_text += "Введите сумму или выберите действие:"
                markup.add(types.KeyboardButton(f"Оплатить {debt:.2f} руб."),
                           types.KeyboardButton("Другая сумма"),
                           types.KeyboardButton("Отмена"))
            else:
                msg_text += "💰 Баланс положительный. Вы можете внести предоплату.\nВведите сумму:"
                markup.add(types.KeyboardButton("100 руб."),
                           types.KeyboardButton("500 руб."),
                           types.KeyboardButton("1000 руб."),
                           types.KeyboardButton("Другая сумма"),
                           types.KeyboardButton("Отмена"))

            await bot.send_message(message.chat.id, msg_text, parse_mode='Markdown', reply_markup=markup)

        @bot.message_handler(func=lambda msg: msg.text in ["Отмена", "Назад"])
        async def cancel_payment(message):
            if message.chat.id != self.chat_id:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return
            await bot.send_message(message.chat.id, "❌ Операция отменена", reply_markup=self.create_keyboard())

        @bot.message_handler(func=lambda msg:
            msg.text.startswith("Оплатить") or
            msg.text.endswith("руб.") or
            msg.text == "Другая сумма" or
            re.match(r'^\d+([.,]\d{1,2})?$', msg.text or ""))
        async def process_payment(message):
            if message.chat.id != self.chat_id:
                return

            text = message.text

            if text == "Другая сумма":
                await bot.send_message(
                    message.chat.id,
                    "💳 Введите сумму для оплаты в рублях (например: 500.50):",
                    reply_markup=types.ForceReply(selective=True)
                )
                return

            if text == "Отмена":
                await cancel_payment(message)
                return

            try:
                # Извлекаем сумму
                if "Оплатить" in text:
                    amount_str = text.split()[1]
                    amount = float(amount_str.replace(',', '.'))
                elif "руб." in text:
                    amount_str = text.split()[0]
                    amount = float(amount_str.replace(',', '.'))
                else:
                    amount = float(text.replace(',', '.'))

                if amount <= 0:
                    raise ValueError("Сумма должна быть положительной")
                if amount < 1:
                    await bot.send_message(message.chat.id, "❌ Минимальная сумма оплаты — 1 рубль", reply_markup=self.create_keyboard())
                    return

                await bot.send_chat_action(message.chat.id, 'upload_photo')
                qr_img = self.generate_payment_qr(amount, TIS_LOGIN)

                instructions = (
                    "📲 *Как оплатить:*\n\n"
                    "1) Откройте приложение банка\n"
                    "2) Выберите 'Платеж по QR'\n"
                    "3) Сканируйте код\n"
                    "4) Проверьте реквизиты и подтвердите\n\n"
                    f"💳 *Сумма:* {amount:.2f} руб.\n"
                    f"📋 *Лицевой счет:* {TIS_LOGIN}\n"
                    f"🏢 *Получатель:* ООО ТИС - ДИАЛОГ"
                )

                await bot.send_photo(message.chat.id, photo=qr_img, caption=instructions, parse_mode='Markdown')

                await bot.send_message(
                    message.chat.id,
                    "✅ QR сформирован. После оплаты баланс обновится в течение 10–15 минут.\n"
                    "Реквизиты получателя:\n"
                    "• ИНН: 3908602823\n"
                    "• Расчетный счет: 40702810420230000176\n"
                    "• БИК: 042748634\n"
                    "• Банк: КАЛИНИНГРАДСКОЕ ОТДЕЛЕНИЕ N8626 ПАО СБЕРБАНК\n"
                    "• Корр. счет: 30101810100000000634",
                    reply_markup=self.create_keyboard()
                )

            except (ValueError, IndexError):
                await bot.send_message(
                    message.chat.id,
                    "❌ Неверный формат суммы. Введите число, например: 500 или 250.50",
                    reply_markup=self.create_keyboard()
                )

        @bot.message_handler(func=lambda msg: msg.text == '🔔 Уведомления')
        async def show_notifications(message):
            if message.chat.id != self.chat_id:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return

            if not notifications_store:
                await bot.send_message(
                    message.chat.id,
                    "ℹ️ *Нет доступных уведомлений*\n\nНовые уведомления будут появляться здесь автоматически.",
                    parse_mode='Markdown',
                    reply_markup=self.create_keyboard()
                )
                return

            # сортируем по дате (новые сверху)
            def parse_date(d):  # 'dd.mm.yyyy'
                try:
                    return datetime.strptime(d, "%d.%m.%Y")
                except Exception:
                    return datetime.min

            sorted_notifications = sorted(
                notifications_store.values(),
                key=lambda x: parse_date(x['date']),
                reverse=True
            )

            response = "🔔 *Последние уведомления:*\n\n"
            for i, notif in enumerate(sorted_notifications[:5], 1):
                response += f"{i}. 📅 *{notif['date']}* — {notif['subject']}\n"

            # инлайн-кнопки
            markup = types.InlineKeyboardMarkup(row_width=2)
            buttons = []
            for i, notif in enumerate(sorted_notifications[:5], 1):
                btn_text = notif['subject'] if len(notif['subject']) <= 30 else notif['subject'][:27] + '...'
                buttons.append(types.InlineKeyboardButton(f"{i}. {btn_text}", callback_data=f"notif_{notif['id']}"))

            for i in range(0, len(buttons), 2):
                if i + 1 < len(buttons):
                    markup.add(buttons[i], buttons[i + 1])
                else:
                    markup.add(buttons[i])

            markup.add(types.InlineKeyboardButton("📋 Показать все", callback_data="show_all_notifs"))

            await bot.send_message(message.chat.id, response, parse_mode='Markdown', reply_markup=markup)

        @bot.callback_query_handler(func=lambda call: call.data.startswith('notif_'))
        async def show_notification_detail(call):
            notif_id = call.data.split('_', 1)[1]
            notification = notifications_store.get(notif_id)
            if not notification:
                await bot.answer_callback_query(call.id, "Уведомление не найдено")
                return

            text = (
                f"🔔 *Уведомление*\n\n"
                f"📅 *Дата:* {notification['date']}\n"
                f"📝 *Тема:* {notification['subject']}\n\n"
                f"{notification['text']}"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_notifs"))

            try:
                await bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=text,
                    parse_mode='Markdown',
                    reply_markup=markup
                )
            except Exception:
                # если редактирование невозможно, отправим новое сообщение
                await bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=markup)

        @bot.callback_query_handler(func=lambda call: call.data == "show_all_notifs")
        async def show_all_notifications(call):
            if not notifications_store:
                await bot.answer_callback_query(call.id, "Нет уведомлений")
                return

            def parse_date(d):
                try:
                    return datetime.strptime(d, "%d.%m.%Y")
                except Exception:
                    return datetime.min

            sorted_notifications = sorted(
                notifications_store.values(),
                key=lambda x: parse_date(x['date']),
                reverse=True
            )

            response = "🔔 *Все уведомления:*\n\n"
            for notif in sorted_notifications:
                response += f"▫️ 📅 *{notif['date']}* — {notif['subject']}\n"

            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_notifs"))

            try:
                await bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=response,
                    parse_mode='Markdown',
                    reply_markup=markup
                )
            except Exception:
                await bot.send_message(call.message.chat.id, response, parse_mode='Markdown', reply_markup=markup)

        @bot.callback_query_handler(func=lambda call: call.data == "back_to_notifs")
        async def back_to_notifications(call):
            # Переотправим список последних уведомлений
            fake_msg = call.message
            fake_msg.text = '🔔 Уведомления'
            await show_notifications(fake_msg)

        @bot.message_handler(func=lambda msg: msg.text == '⚙️ Настройки')
        async def send_settings(message):
            if message.chat.id != self.chat_id:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return

            await bot.send_message(
                message.chat.id,
                "⚙️ *Настройки мониторинга*\n\n"
                "• ⏱️ *Проверка трафика:* каждые 30 минут\n"
                "• 🔔 *Уведомления при:*\n"
                "  - Остатке трафика < 100 Гб\n"
                "  - Отрицательном балансе\n"
                "  - Смене IP-адреса\n"
                "• 📨 *Проверка новых сообщений ЛК:* раз в 6 часов\n"
                f"• 💾 *Сохранено уведомлений:* {len(notifications_store)} шт.\n\n"
                "Изменение порогов/частоты — вручную в коде.",
                parse_mode='Markdown',
                reply_markup=self.create_keyboard()
            )

        @bot.message_handler(func=lambda msg: msg.text == '🔄 Обновить данные')
        async def refresh_data(message):
            if message.chat.id != self.chat_id:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return

            await bot.send_chat_action(message.chat.id, 'typing')
            await self.values.fetch(force=True)  # принудительно
            await bot.send_message(message.chat.id, "✅ *Данные обновлены!*", parse_mode='Markdown', reply_markup=self.create_keyboard())

        # Fallback: если пользователь написал что-то иное — подскажем меню
        @bot.message_handler(func=lambda msg: True)
        async def fallback(message):
            if message.chat.id != self.chat_id:
                await bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return
            await bot.send_message(
                message.chat.id,
                "Выберите действие с помощью кнопок ниже.",
                reply_markup=self.create_keyboard()
            )

    async def run(self):
        # Фоновая задача уведомлений
        asyncio.create_task(check_notifications(self.bot, self.chat_id))
        logger.info("Бот запущен, мониторинг активирован")
        await self.bot.infinity_polling()


# =========================
# ENTRYPOINT
# =========================
async def main():
    bot = TisDialogBot()
    await bot.run()


if __name__ == '__main__':
    asyncio.run(main())
