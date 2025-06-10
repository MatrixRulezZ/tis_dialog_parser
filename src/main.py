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
    """Загружаем уведомления из резервной копии"""
    global notifications_store
    try:
        if os.path.exists(BACKUP_FILE):
            with open(BACKUP_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Восстанавливаем порядок уведомлений
                for item in data:
                    notifications_store[item['id']] = item
                logger.info(f"Загружено {len(notifications_store)} уведомлений из резервной копии")
    except Exception as e:
        logger.error(f"Ошибка при загрузке резервной копии: {e}")


def save_notifications():
    """Сохраняем уведомления в резервную копию"""
    try:
        with open(BACKUP_FILE, 'w', encoding='utf-8') as f:
            # Преобразуем OrderedDict в список для сохранения
            json.dump(list(notifications_store.values()), f, ensure_ascii=False, indent=2)
        logger.info(f"Резервная копия уведомлений сохранена ({len(notifications_store)} записей)")
    except Exception as e:
        logger.error(f"Ошибка при сохранении резервной копии: {e}")


# Загружаем существующие уведомления при запуске
load_notifications()


async def login(session):
    """Функция для входа в личный кабинет с кэшированием сессии"""
    base_url = 'https://stats.tis-dialog.ru'
    login_url = f'{base_url}/index.php'
    user_agent_val = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'

    # Проверяем существующую сессию
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

            # Проверяем успешность входа
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
    """Генерируем ключ кэша для URL"""
    return hashlib.md5(url.encode()).hexdigest()


async def cached_fetch(session, url, cache_ttl=300):
    """Выполняем запрос с кэшированием HTML"""
    cache_file = os.path.join(HTML_CACHE_DIR, cache_key(url))

    # Проверяем актуальность кэша
    if os.path.exists(cache_file):
        file_age = time.time() - os.path.getmtime(cache_file)
        if file_age < cache_ttl:
            with open(cache_file, 'r', encoding='windows-1251') as f:
                return f.read()

    # Если кэш устарел или отсутствует, делаем запрос
    async with session.get(url) as response:
        if response.status != 200:
            return None

        content = await response.text(encoding='windows-1251')

        # Сохраняем в кэш
        with open(cache_file, 'w', encoding='windows-1251') as f:
            f.write(content)

        return content


async def fetch_notifications(session):
    """Оптимизированная функция для получения уведомлений"""
    notifications_url = f"https://stats.tis-dialog.ru/index.php?mod=msg&phnumber={TIS_LOGIN}"
    try:
        html_content = await cached_fetch(session, notifications_url, cache_ttl=600)
        if not html_content:
            logger.error("Ошибка при загрузке страницы уведомлений")
            return []

        soup = BS(html_content, 'html.parser')
        new_notifications = []

        # Эффективный поиск уведомлений
        for div in soup.find_all('div', style="margin-bottom:15px;"):
            date_match = re.search(r'\d{2}\.\d{2}\.\d{4}', div.text)
            if not date_match:
                continue
            date_text = date_match.group(0)

            link = div.find('a')
            if not link:
                continue

            # Полный URL уведомления
            full_url = "https://stats.tis-dialog.ru/" + link['href']

            # Извлекаем ID уведомления
            match = re.search(r'comsg=(\d+)', full_url)
            if not match:
                continue
            notif_id = match.group(1)

            # Проверка наличия в кэше
            if notif_id in notifications_store:
                continue

            # Получаем текст уведомления с кэшированием
            notif_html = await cached_fetch(session, full_url, cache_ttl=3600)
            if not notif_html:
                continue

            notif_soup = BS(notif_html, 'html.parser')
            content_block = notif_soup.find('div', class_='contentBlock')
            if not content_block:
                continue

            # Быстрое извлечение текста
            message_text = ""
            br_tag = content_block.find('br')
            if br_tag:
                next_element = br_tag.next_sibling
                while next_element:
                    if isinstance(next_element, str):
                        message_text += next_element.strip()
                    elif next_element.name == 'div':
                        message_text = next_element.get_text(strip=True)
                        break
                    next_element = next_element.next_sibling

            # Если не нашли текст, используем весь контент
            if not message_text:
                message_text = content_block.get_text(strip=True, separator=' ')[:500]

            # Сохраняем уведомление
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

        # Сохраняем новые уведомления
        if new_notifications:
            save_notifications()

        return new_notifications
    except Exception as e:
        logger.error(f"Ошибка при получении уведомлений: {e}")
        return []


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
        self.cache_duration = 300  # 5 минут кэширования
        self.lock = asyncio.Lock()

    async def fetch(self, force=False):
        """Оптимизированная функция получения данных с кэшированием"""
        current_time = time.time()

        # Проверяем актуальность кэша
        if not force and current_time - self.last_fetch < self.cache_duration:
            return self.last_success

        # Блокировка для предотвращения одновременных запросов
        async with self.lock:
            # Двойная проверка после получения блокировки
            if not force and current_time - self.last_fetch < self.cache_duration:
                return self.last_success

            try:
                async with aiohttp.ClientSession() as session:
                    if not await login(session):
                        return False

                    login_url = 'https://stats.tis-dialog.ru/index.php'
                    html_content = await cached_fetch(session, login_url, cache_ttl=300)
                    if not html_content:
                        return False

                    html = BS(html_content, 'html.parser')
                    title = html.title.string if html.title else ""
                    if "Личный кабинет Тис-диалог" not in title:
                        return False

                    # Удаляем ссылки для упрощения парсинга
                    for a in html.find_all("a"):
                        a.decompose()

                    # Оптимизированный парсинг таблиц
                    tables = html.select('.lkInfoTable')

                    # Первая таблица: основная информация
                    if tables:
                        rows = tables[0].select('tr')
                        if len(rows) >= 5:
                            # Скорость
                            speed_cell = rows[0].select('td')
                            if len(speed_cell) > 1:
                                self.speed = speed_cell[1].get_text(strip=True)

                            # Баланс
                            money_cell = rows[3].select('td')
                            if len(money_cell) > 1:
                                self.money = money_cell[1].get_text(strip=True)
                                try:
                                    balance_str = self.money.replace('руб.', '').replace(',', '.').strip()
                                    balance_str = re.sub(r'[^\d.]+', '', balance_str)
                                    self.balance = float(balance_str) if balance_str else 0.0
                                except (ValueError, TypeError):
                                    self.balance = 0.0

                            # Статус
                            status_cell = rows[4].select('td')
                            if len(status_cell) > 1:
                                self.status = status_cell[1].get_text(strip=True)

                    # Вторая таблица: трафик и активность
                    if len(tables) > 1:
                        rows = tables[1].select('tr')
                        if len(rows) >= 3:
                            # Скорость (если не нашли в первой таблице)
                            if self.speed == "Н/Д" and len(rows) > 0:
                                speed_cell = rows[0].select('td')
                                if len(speed_cell) > 1:
                                    self.speed = speed_cell[1].get_text(strip=True)

                            # IP-адрес
                            if len(rows) > 1:
                                ip_cell = rows[1].select('td')
                                if len(ip_cell) > 1:
                                    activity_text = ip_cell[1].get_text(strip=True)
                                    ip_match = re.search(r'IP:(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', activity_text)
                                    self.ip_address = ip_match.group(1) if ip_match else "Н/Д"

                            # Остаток трафика
                            if len(rows) > 2:
                                traffic_cell = rows[2].select('td')
                                if len(traffic_cell) > 1:
                                    traffic_text = traffic_cell[1].get_text(strip=True)
                                    match = re.search(r'\(([\d,]+)\s*Тб\)', traffic_text)
                                    if match:
                                        traffic_tb = float(match.group(1).replace(',', '.'))
                                        self.traffic_gb = traffic_tb * 1024
                                        self.traffic_str = f"{self.traffic_gb:.2f} Гб"

                    # Трафик за период (отдельная таблица)
                    traffic_table = html.select_one('.lkTraficTable')
                    if traffic_table:
                        rows = traffic_table.select('tr')
                        if len(rows) > 1:
                            cells = rows[1].select('td')
                            if len(cells) > 1:
                                # Входящий трафик
                                inc_text = cells[0].get_text(strip=True)
                                inc_match = re.search(r'\(([\d,.]+)\s*Гб\)', inc_text)
                                self.incoming_traffic = f"{inc_match.group(1)} Гб" if inc_match else "Н/Д"

                                # Исходящий трафик
                                out_text = cells[1].get_text(strip=True)
                                out_match = re.search(r'\(([\d,.]+)\s*Гб\)', out_text)
                                self.outgoing_traffic = f"{out_match.group(1)} Гб" if out_match else "Н/Д"

                    self.last_fetch = current_time
                    self.last_success = True
                    return True
            except aiohttp.ClientError as e:
                logger.error(f"Ошибка сети: {e}")
            except Exception as e:
                logger.error(f"Ошибка при получении данных: {str(e)[:100]}")
            self.last_success = False
            return False


async def check_notifications(bot, chat_id):
    """Оптимизированная фоновая проверка"""
    failed_attempts = 0
    last_notification_check = 0
    last_traffic_check = 0
    cache_values = None
    notification_session = None

    try:
        while True:
            current_time = time.time()
            need_traffic_check = current_time - last_traffic_check > 1800  # 30 минут
            need_notification_check = current_time - last_notification_check > 21600  # 6 часов

            # Если ничего не нужно проверять, ждем
            if not need_traffic_check and not need_notification_check:
                await asyncio.sleep(300)
                continue

            try:
                # Создаем сессию при необходимости
                if not notification_session or notification_session.closed:
                    notification_session = aiohttp.ClientSession()

                # Проверка данных трафика
                if need_traffic_check:
                    values = AsyncValues()
                    success = await values.fetch()

                    if not success:
                        failed_attempts += 1
                        wait_time = min(300 * 2 ** failed_attempts, 3600)
                        logger.warning(f"Ошибка данных, повтор через {wait_time} сек.")
                        await asyncio.sleep(wait_time)
                        continue

                    # Сброс счетчика ошибок
                    failed_attempts = 0
                    last_traffic_check = current_time

                    # Проверка условий для уведомлений
                    if values.traffic_gb < 100:
                        # Отправляем только если состояние изменилось
                        if not cache_values or cache_values.traffic_gb >= 100:
                            await bot.send_message(
                                chat_id,
                                f"⚠️ *Внимание! Осталось мало трафика*\n📊 Остаток: {values.traffic_gb:.2f} Гб",
                                parse_mode='Markdown'
                            )

                    if values.balance < 0:
                        if not cache_values or cache_values.balance >= 0:
                            await bot.send_message(
                                chat_id,
                                f"⚠️ *Внимание! Отрицательный баланс*\n💰 Текущий баланс: {values.balance:.2f} руб.",
                                parse_mode='Markdown'
                            )

                    cache_values = values

                # Проверка уведомлений
                if need_notification_check:
                    last_notification_check = current_time
                    logger.info("Проверка новых уведомлений...")

                    if await login(notification_session):
                        new_notifications = await fetch_notifications(notification_session)
                        for notification in new_notifications:
                            await bot.send_message(
                                chat_id,
                                f"🔔 *Новое уведомление!*\n"
                                f"📅 *Дата:* {notification['date']}\n"
                                f"📝 *Тема:* {notification['subject']}\n\n"
                                f"ℹ️ *Текст:*\n{notification['text']}",
                                parse_mode='Markdown'
                            )
                            await asyncio.sleep(0.5)  # Короткая задержка

                # Стандартная задержка между проверками
                await asyncio.sleep(300)

            except Exception as e:
                logger.error(f"Ошибка в фоновой задаче: {str(e)[:200]}")
                # Закрываем проблемную сессию
                if notification_session:
                    await notification_session.close()
                    notification_session = None
                await asyncio.sleep(300)
    finally:
        # Гарантированное закрытие сессии при выходе
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
        btn_status = types.KeyboardButton('📊 Статус подключения')
        btn_traffic = types.KeyboardButton('🌐 Остаток трафика')
        btn_settings = types.KeyboardButton('⚙️ Настройки')
        btn_refresh = types.KeyboardButton('🔄 Обновить данные')
        btn_notifications = types.KeyboardButton('🔔 Уведомления')
        markup.add(btn_status, btn_traffic, btn_notifications, btn_settings, btn_refresh)
        return markup

    def setup_handlers(self):
        @self.bot.message_handler(commands=['start', 'help'])
        async def welcome_msg(message):
            if message.chat.id == self.chat_id:
                await self.bot.send_message(
                    message.chat.id,
                    "👋 *Бот мониторинга Тис-Диалог запущен!*\n\n"
                    "Используйте кнопки меню для управления:\n\n"
                    "📊 Статус подключения - полная информация\n"
                    "🌐 Остаток трафика - данные о трафике\n"
                    "🔔 Уведомления - просмотр системных сообщений\n"
                    "⚙️ Настройки - информация о мониторинге\n"
                    "🔄 Обновить данные - принудительное обновление",
                    parse_mode='Markdown',
                    reply_markup=self.create_keyboard()
                )
            else:
                await self.bot.send_message(
                    message.chat.id,
                    '🚫 Функционал бота недоступен для вашего аккаунта!'
                )

        @self.bot.message_handler(func=lambda msg: msg.text == '📊 Статус подключения')
        async def send_full_status(message):
            if message.chat.id == self.chat_id:
                await self.bot.send_chat_action(message.chat.id, 'typing')
                # Используем кэшированные данные, если они актуальны
                success = await self.values.fetch()

                if success:
                    status_message = (
                        f"🌐 *Статус подключения*\n\n"
                        f"✅ *Состояние:* {self.values.status}\n"
                        f"🌐 *IP-адрес:* `{self.values.ip_address}`\n"
                        f"💰 *Баланс:* {self.values.money}\n"
                        f"🚀 *Скорость:* {self.values.speed}\n"
                        f"📊 *Остаток трафика:* {self.values.traffic_str}\n"
                        f"⬇️ *Входящий трафик:* {self.values.incoming_traffic}\n"
                        f"⬆️ *Исходящий трафик:* {self.values.outgoing_traffic}\n"
                        f"💳 *Состояние счета:* {'✅ Положительный' if self.values.balance >= 0 else '⚠️ Отрицательный'}"
                    )

                    await self.bot.send_message(
                        message.chat.id,
                        status_message,
                        parse_mode='Markdown',
                        reply_markup=self.create_keyboard()
                    )
                else:
                    await self.bot.send_message(
                        message.chat.id,
                        "❌ Не удалось получить данные. Попробуйте позже или используйте кнопку '🔄 Обновить данные'.",
                        reply_markup=self.create_keyboard()
                    )
            else:
                await self.bot.send_message(
                    message.chat.id,
                    '🚫 Доступ запрещен!'
                )

        # Остальные обработчики остаются без изменений (как в предыдущей версии)
        # Для краткости оставим только измененные части

        @self.bot.message_handler(func=lambda msg: msg.text == '🔄 Обновить данные')
        async def refresh_data(message):
            if message.chat.id == self.chat_id:
                await self.bot.send_chat_action(message.chat.id, 'typing')
                # Принудительное обновление с очисткой кэша
                await self.values.fetch(force=True)
                await self.bot.send_message(
                    message.chat.id,
                    "✅ *Данные успешно обновлены!*",
                    parse_mode='Markdown',
                    reply_markup=self.create_keyboard()
                )
            else:
                await self.bot.send_message(
                    message.chat.id,
                    '🚫 Доступ запрещен!'
                )

    async def run(self):
        # Запускаем фоновую задачу для уведомлений
        asyncio.create_task(check_notifications(self.bot, self.chat_id))
        logger.info("Бот запущен и мониторинг активирован")
        await self.bot.infinity_polling()


async def main():
    bot = TisDialogBot()
    await bot.run()


if __name__ == '__main__':
    asyncio.run(main())