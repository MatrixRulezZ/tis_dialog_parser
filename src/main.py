# =========================
# Регистрация пользователей (упрощённая и стабильная версия)
# =========================
user_states = {}  # временное хранилище состояний


@bot.message_handler(commands=['register'])
async def register_command(message):
    user_id = message.from_user.id
    user_states[user_id] = {"step": "login"}
    await bot.send_message(message.chat.id, "Введите логин от личного кабинета TIS:")


@bot.message_handler(func=lambda message: message.from_user.id in user_states)
async def process_registration(message):
    user_id = message.from_user.id
    state = user_states[user_id]

    if state["step"] == "login":
        state["login"] = message.text.strip()
        state["step"] = "password"
        await bot.send_message(message.chat.id, "Теперь введите пароль от личного кабинета TIS:")

    elif state["step"] == "password":
        login = state["login"]
        password = message.text.strip()

        client = TISClient(login, password)
        success = await client.login()
        await client.close()

        if success:
            add_or_update_user(user_id, message.chat.id, login, password)
            del user_states[user_id]
            await bot.send_message(message.chat.id, "✅ Учётная запись успешно подключена!")
            await show_main_menu(message.chat.id)
        else:
            await bot.send_message(message.chat.id, "❌ Не удалось войти. Проверьте логин и пароль.")
            del user_states[user_id]