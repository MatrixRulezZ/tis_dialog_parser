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
    """Функция для входа в личный кабинет"""
    base_url = 'https://stats.tis-dialog.ru'
    login_url = f'{base_url}/index.php'
    user_agent_val = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'

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


async def fetch_notifications(session):
    """Функция для получения и парсинга уведомлений"""
    notifications_url = f"https://stats.tis-dialog.ru/index.php?mod=msg&phnumber={TIS_LOGIN}"
    try:
        async with session.get(notifications_url) as response:
            if response.status != 200:
                logger.error(f"Ошибка при загрузке страницы уведомлений: {response.status}")
                return []

            html_content = await response.text(encoding='windows-1251')
            soup = BS(html_content, 'html.parser')

            # Находим все блоки с уведомлениями
            notification_divs = soup.select('div[style="margin-bottom:15px;"]')
            new_notifications = []

            for div in notification_divs:
                # Извлекаем дату и ссылку
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

                # Если уведомление уже есть в хранилище, пропускаем
                if notif_id in notifications_store:
                    continue

                # Получаем текст уведомления
                async with session.get(full_url) as notif_response:
                    if notif_response.status != 200:
                        logger.error(f"Ошибка при загрузке уведомления {notif_id}")
                        continue

                    notif_html = await notif_response.text(encoding='windows-1251')
                    notif_soup = BS(notif_html, 'html.parser')

                    # Извлекаем текст уведомления
                    content_block = notif_soup.find('div', class_='contentBlock')
                    if not content_block:
                        continue

                    # Ищем основной текст после <br>
                    br_tag = content_block.find('br')
                    message_text = ""
                    if br_tag:
                        next_div = br_tag.find_next_sibling('div')
                        if next_div:
                            message_text = next_div.get_text(strip=True)

                    # Форматируем сообщение
                    notification_data = {
                        'id': notif_id,
                        'date': date_text,
                        'subject': link.text.strip(),
                        'text': message_text,
                        'full_url': full_url,
                        'added_at': datetime.now().isoformat()  # Время добавления в бот
                    }

                    # Сохраняем в хранилище
                    notifications_store[notif_id] = notification_data
                    # Удаляем старые уведомления, если превышен лимит
                    if len(notifications_store) > MAX_NOTIFICATIONS:
                        oldest_id = next(iter(notifications_store))
                        del notifications_store[oldest_id]

                    new_notifications.append(notification_data)

            # Сохраняем новые уведомления в резервную копию
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
        self.ip_address = "Н/Д"  # Новое поле для IP-адреса

    async def fetch(self):
        """Основная функция получения данных"""
        try:
            async with aiohttp.ClientSession() as session:
                # Используем единую функцию входа
                if not await login(session):
                    return False

                login_url = 'https://stats.tis-dialog.ru/index.php'
                async with session.get(login_url) as lk_response:
                    html_content = await lk_response.text(encoding='windows-1251')
                    html = BS(html_content, 'html.parser')

                    # Проверка заголовка
                    title = html.title.string if html.title else ""
                    if "Личный кабинет Тис-диалог" not in title:
                        return False

                    # Удаляем все ссылки
                    for a in html.find_all("a"):
                        a.decompose()

                    # Парсим скорость
                    data_speed = html.select(".lkInfoTable:nth-of-type(2) tr:nth-child(1) td:nth-child(2)")
                    if data_speed:
                        self.speed = data_speed[0].get_text(strip=True)

                    # Парсим баланс
                    money_data = html.select(".lkInfoTable:nth-of-type(1) tr:nth-child(4) td:nth-child(2)")
                    if money_data:
                        self.money = money_data[0].get_text(strip=True)
                        try:
                            balance_str = self.money.replace('руб.', '').replace(',', '.').strip()
                            balance_str = re.sub(r'[^\d.]+', '', balance_str)
                            self.balance = float(balance_str) if balance_str else 0.0
                        except (ValueError, TypeError):
                            self.balance = 0.0

                    # Парсим статус подключения
                    status_data = html.select(".lkInfoTable:nth-of-type(1) tr:nth-child(5) td:nth-child(2)")
                    if status_data:
                        self.status = status_data[0].get_text(strip=True)

                    # Парсим остаток трафика
                    traffic_data = html.select(".lkInfoTable:nth-of-type(2) tr:nth-child(3) td:nth-child(2)")
                    if traffic_data:
                        traffic_text = traffic_data[0].get_text(strip=True)
                        match = re.search(r'\(([\d,]+)\s*Тб\)', traffic_text)
                        if match:
                            traffic_tb = float(match.group(1).replace(',', '.'))
                            self.traffic_gb = traffic_tb * 1024
                            self.traffic_str = f"{self.traffic_gb:.2f} Гб"

                    # Парсим входящий и исходящий трафик (только в Гб)
                    traffic_table = html.select_one('.lkTraficTable')
                    if traffic_table:
                        rows = traffic_table.find_all('tr')
                        if len(rows) > 1:  # Первая строка - заголовки
                            cells = rows[1].find_all('td')
                            if len(cells) >= 2:
                                # Извлекаем только гигабайты
                                inc_text = cells[0].get_text(strip=True)
                                inc_match = re.search(r'\(([\d,.]+)\s*Гб\)', inc_text)
                                if inc_match:
                                    self.incoming_traffic = f"{inc_match.group(1)} Гб"
                                else:
                                    self.incoming_traffic = "Н/Д"

                                out_text = cells[1].get_text(strip=True)
                                out_match = re.search(r'\(([\d,.]+)\s*Гб\)', out_text)
                                if out_match:
                                    self.outgoing_traffic = f"{out_match.group(1)} Гб"
                                else:
                                    self.outgoing_traffic = "Н/Д"

                    # Парсим IP-адрес
                    activity_data = html.select(".lkInfoTable:nth-of-type(2) tr:nth-child(2) td:nth-child(2)")
                    if activity_data:
                        activity_text = activity_data[0].get_text(strip=True)
                        # Ищем IP-адрес в формате XXX.XXX.XXX.XXX
                        ip_match = re.search(r'IP:(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', activity_text)
                        if ip_match:
                            self.ip_address = ip_match.group(1)
                        else:
                            self.ip_address = "Н/Д"

                    self.last_success = True
                    return True
        except aiohttp.ClientError as e:
            logger.error(f"Ошибка сети: {e}")
        except Exception as e:
            logger.error(f"Ошибка при получении данных: {str(e)[:100]}")
        self.last_success = False
        return False


async def check_notifications(bot, chat_id):
    """Проверяет условия для уведомлений и отправляет их"""
    await asyncio.sleep(10)
    failed_attempts = 0
    last_notification_check = 0  # Время последней проверки уведомлений

    while True:
        try:
            values = AsyncValues()
            success = await values.fetch()

            if not success:
                failed_attempts += 1
                wait_time = min(300 * 2 ** failed_attempts, 3600)
                logger.warning(f"Не удалось получить данные, повторная попытка через {wait_time} сек.")
                await asyncio.sleep(wait_time)
                continue

            # Сброс счетчика при успехе
            failed_attempts = 0

            # Проверяем остаток трафика
            if values.traffic_gb < 100:
                await bot.send_message(
                    chat_id,
                    f"⚠️ *Внимание! Осталось мало трафика*\n\n"
                    f"📊 Остаток: {values.traffic_gb:.2f} Гб\n"
                    f"Рекомендуем пополнить баланс или выбрать новый тариф",
                    parse_mode='Markdown'
                )

            # Проверяем баланс
            if values.balance < 0:
                await bot.send_message(
                    chat_id,
                    f"⚠️ *Внимание! Отрицательный баланс*\n\n"
                    f"💰 Текущий баланс: {values.balance:.2f} руб.\n"
                    f"Для восстановления подключения необходимо пополнить счет",
                    parse_mode='Markdown'
                )

            # Проверяем уведомления (раз в 6 часов)
            current_time = time.time()
            if current_time - last_notification_check > 21600:  # 6 часов
                logger.info("Проверка новых уведомлений...")
                async with aiohttp.ClientSession() as session:
                    if await login(session):
                        try:
                            new_notifications = await fetch_notifications(session)
                            for notification in new_notifications:
                                await bot.send_message(
                                    chat_id,
                                    f"🔔 *Новое уведомление!*\n"
                                    f"📅 *Дата:* {notification['date']}\n"
                                    f"📝 *Тема:* {notification['subject']}\n\n"
                                    f"ℹ️ *Текст:*\n{notification['text']}",
                                    parse_mode='Markdown'
                                )
                                await asyncio.sleep(1)  # Небольшая задержка между отправкой
                        except Exception as e:
                            logger.error(f"Ошибка при получении уведомлений: {e}")
                    else:
                        logger.error("Не удалось войти для проверки уведомлений")

                last_notification_check = current_time

            # Ожидание до следующей проверки (30 минут)
            await asyncio.sleep(1800)

        except Exception as e:
            logger.error(f"Критическая ошибка в мониторинге: {str(e)[:200]}")
            await asyncio.sleep(300)


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
                success = await self.values.fetch()

                if success:
                    # Форматируем информацию о трафике
                    traffic_info = ""
                    if self.values.incoming_traffic != "Н/Д" and self.values.outgoing_traffic != "Н/Д":
                        traffic_info = (
                            f"⬇️ *Входящий трафик:* {self.values.incoming_traffic}\n"
                            f"⬆️ *Исходящий трафик:* {self.values.outgoing_traffic}\n"
                        )

                    # Определяем смайлик для статуса подключения
                    status_icon = "✅" if "подключен" in self.values.status.lower() else "❌"

                    # Формируем сообщение со смайликами
                    status_message = (
                        f"🌐 *Статус подключения*\n\n"
                        f"{status_icon} *Состояние:* {self.values.status}\n"
                        f"🌐 *IP-адрес:* `{self.values.ip_address}`\n"
                        f"💰 *Баланс:* {self.values.money}\n"
                        f"🚀 *Скорость:* {self.values.speed}\n"
                        f"📊 *Остаток трафика:* {self.values.traffic_str}\n"
                        f"{traffic_info}"
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

        @self.bot.message_handler(func=lambda msg: msg.text == '🌐 Остаток трафика')
        async def send_traffic_status(message):
            if message.chat.id == self.chat_id:
                await self.bot.send_chat_action(message.chat.id, 'typing')
                success = await self.values.fetch()

                if success:
                    # Форматируем сообщение с иконкой
                    await self.bot.send_message(
                        message.chat.id,
                        f"🌐 *Остаток трафика:*\n📊 {self.values.traffic_str}",
                        parse_mode='Markdown',
                        reply_markup=self.create_keyboard()
                    )
                else:
                    await self.bot.send_message(
                        message.chat.id,
                        "❌ Не удалось получить данные о трафике. Попробуйте позже или используйте кнопку '🔄 Обновить данные'.",
                        reply_markup=self.create_keyboard()
                    )
            else:
                await self.bot.send_message(
                    message.chat.id,
                    '🚫 Доступ запрещен!'
                )

        @self.bot.message_handler(func=lambda msg: msg.text == '🔔 Уведомления')
        async def show_notifications(message):
            if message.chat.id != self.chat_id:
                await self.bot.send_message(message.chat.id, '🚫 Доступ запрещен!')
                return

            if not notifications_store:
                await self.bot.send_message(
                    message.chat.id,
                    "ℹ️ *Нет доступных уведомлений*\n\n"
                    "Новые уведомления будут появляться здесь автоматически.",
                    parse_mode='Markdown',
                    reply_markup=self.create_keyboard()
                )
                return

            # Сортируем уведомления по дате (новые сверху)
            sorted_notifications = sorted(
                notifications_store.values(),
                key=lambda x: time.strptime(x['date'], '%d.%m.%Y'),
                reverse=True
            )

            # Формируем список уведомлений
            response = "🔔 *Последние уведомления:*\n\n"
            for i, notif in enumerate(sorted_notifications[:5], 1):  # Показываем последние 5
                response += f"{i}. 📅 *{notif['date']}* - {notif['subject']}\n"

            # Создаем инлайн-клавиатуру для выбора уведомлений
            markup = types.InlineKeyboardMarkup(row_width=2)
            buttons = []
            for i, notif in enumerate(sorted_notifications[:5], 1):
                # Обрезаем длинные темы для кнопок
                btn_text = notif['subject']
                if len(btn_text) > 30:
                    btn_text = btn_text[:27] + '...'
                buttons.append(
                    types.InlineKeyboardButton(
                        f"{i}. {btn_text}",
                        callback_data=f"notif_{notif['id']}"
                    )
                )

            # Разбиваем кнопки на строки по 2
            for i in range(0, len(buttons), 2):
                if i + 1 < len(buttons):
                    markup.add(buttons[i], buttons[i + 1])
                else:
                    markup.add(buttons[i])

            # Кнопка для просмотра всех уведомлений
            markup.add(types.InlineKeyboardButton("📋 Показать все уведомления", callback_data="show_all_notifs"))

            await self.bot.send_message(
                message.chat.id,
                response,
                parse_mode='Markdown',
                reply_markup=markup
            )

        @self.bot.callback_query_handler(func=lambda call: call.data.startswith('notif_'))
        async def show_notification_detail(call):
            notif_id = call.data.split('_')[1]
            notification = notifications_store.get(notif_id)

            if not notification:
                await self.bot.answer_callback_query(call.id, "Уведомление не найдено или устарело")
                return

            # Форматируем текст уведомления
            response = (
                f"🔔 *Уведомление*\n\n"
                f"📅 *Дата:* {notification['date']}\n"
                f"📝 *Тема:* {notification['subject']}\n\n"
                f"ℹ️ *Текст:*\n{notification['text']}"
            )

            # Создаем кнопку "Назад"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Назад к списку", callback_data="back_to_notifs"))

            await self.bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=response,
                parse_mode='Markdown',
                reply_markup=markup
            )

        @self.bot.callback_query_handler(func=lambda call: call.data == "show_all_notifs")
        async def show_all_notifications(call):
            if not notifications_store:
                await self.bot.answer_callback_query(call.id, "Нет доступных уведомлений")
                return

            # Сортируем уведомления по дате (новые сверху)
            sorted_notifications = sorted(
                notifications_store.values(),
                key=lambda x: time.strptime(x['date'], '%d.%m.%Y'),
                reverse=True
            )

            # Формируем полный список уведомлений
            response = "🔔 *Все уведомления:*\n\n"
            for notif in sorted_notifications:
                response += f"▫️ 📅 *{notif['date']}* - {notif['subject']}\n"

            # Создаем кнопку "Назад"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_notifs"))

            await self.bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=response,
                parse_mode='Markdown',
                reply_markup=markup
            )

        @self.bot.callback_query_handler(func=lambda call: call.data == "back_to_notifs")
        async def back_to_notifications(call):
            # Просто повторно вызываем обработчик кнопки уведомлений
            await show_notifications(call.message)

        @self.bot.message_handler(func=lambda msg: msg.text == '⚙️ Настройки')
        async def send_settings(message):
            if message.chat.id == self.chat_id:
                await self.bot.send_message(
                    message.chat.id,
                    "⚙️ *Настройки мониторинга*\n\n"
                    "• ⏱️ *Проверка трафика:* Каждые 30 минут\n"
                    "• 🔔 *Уведомления при:*\n"
                    "  - Остатке трафика < 100 Гб\n"
                    "  - Отрицательном балансе\n"
                    "  - Новых сообщениях в ЛК (проверка раз в 6 часов)\n"
                    f"• 💾 *Резервное копирование:* {len(notifications_store)} уведомлений сохранено\n\n"
                    "Для изменения параметров обратитесь к администратору.",
                    parse_mode='Markdown',
                    reply_markup=self.create_keyboard()
                )
            else:
                await self.bot.send_message(
                    message.chat.id,
                    '🚫 Доступ запрещен!'
                )

        @self.bot.message_handler(func=lambda msg: msg.text == '🔄 Обновить данные')
        async def refresh_data(message):
            if message.chat.id == self.chat_id:
                await self.bot.send_chat_action(message.chat.id, 'typing')
                await self.bot.send_message(
                    message.chat.id,
                    "🔄 *Обновление данных...*",
                    parse_mode='Markdown',
                    reply_markup=self.create_keyboard()
                )

                success = await self.values.fetch()
                if success:
                    await self.bot.send_message(
                        message.chat.id,
                        "✅ *Данные успешно обновлены!*",
                        parse_mode='Markdown',
                        reply_markup=self.create_keyboard()
                    )
                else:
                    await self.bot.send_message(
                        message.chat.id,
                        "❌ *Не удалось обновить данные. Попробуйте позже.*",
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