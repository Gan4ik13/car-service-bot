import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from sqlalchemy import select

from .config import config
from .db import init_db, get_session
from .models import Appointment, Admin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = None
dp = Dispatcher()


SERVICES = [
    "Замена масла",
    "Шиномонтаж",
    "Диагностика",
    "Ремонт подвески",
    "Замена колодок",
    "Компьютерная диагностика",
    "Другое",
]


class BookForm(StatesGroup):
    name = State()
    car = State()
    service = State()
    custom_service = State()
    date = State()
    time = State()
    phone = State()
    confirm = State()


def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛠 Записаться на ТО")],
            [KeyboardButton(text="📋 Мои записи")],
            [KeyboardButton(text="📞 Контакты")],
        ],
        resize_keyboard=True,
    )


def services_kb():
    rows = [[KeyboardButton(text=s)] for s in SERVICES[:6]]
    rows.append([KeyboardButton(text="🚗 Другое")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
    )


def yes_no_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, всё верно")],
            [KeyboardButton(text="❌ Нет, заново")],
        ],
        resize_keyboard=True,
    )


async def is_admin(user_id: int) -> bool:
    if user_id in config.admin_ids:
        return True
    async for session in get_session():
        result = await session.execute(
            select(Admin).where(Admin.user_id == user_id, Admin.is_active.is_(True))
        )
        return result.scalar_one_or_none() is not None


def notify_admin_text(app: Appointment) -> str:
    created = app.created_at.strftime("%d.%m.%Y %H:%M") if app.created_at else "—"
    return (
        f"🔔 <b>Новая заявка #{app.id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Имя:</b> {app.full_name}\n"
        f"🚗 <b>Авто:</b> {app.car}\n"
        f"🔧 <b>Услуга:</b> {app.service}\n"
        f"📅 <b>Дата:</b> {app.date}\n"
        f"⏰ <b>Время:</b> {app.time}\n"
        f"📞 <b>Телефон:</b> {app.phone or '—'}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Статус:</b> ⏳ Ожидает\n"
        f"🆔 <b>ID заявки:</b> <code>{app.id}</code>\n"
        f"🕐 <b>Создана:</b> {created}"
    )


async def notify_admins(app: Appointment):
    text = notify_admin_text(app)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{app.id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{app.id}"),
            ]
        ]
    )
    async for session in get_session():
        result = await session.execute(
            select(Admin).where(Admin.is_active.is_(True))
        )
        for admin in result.scalars():
            try:
                await bot.send_message(admin.user_id, text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to notify admin {admin.user_id}: {e}")

    for uid in config.admin_ids:
        try:
            await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Failed to notify admin {uid} via config: {e}")


def status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "confirmed": "✅",
        "cancelled": "❌",
    }.get(status, "❓")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 <b>Добро пожаловать!</b>\n\n"
        f"Я — бот автосервиса. Помогу записаться на ТО, "
        f"отследить статус записи и связаться с сервисом.\n\n"
        f"<b>Доступные команды:</b>\n"
        f"• /book — записаться на ТО\n"
        f"• /status — мои записи\n"
        f"• /contacts — контакты автосервиса\n\n"
        f"Или просто нажмите «Записаться на ТО» ниже 👇",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 <b>Помощь</b>\n\n"
        f"<b>Для клиентов:</b>\n"
        f"• /book или «Записаться на ТО» — оформить заявку\n"
        f"• /status или «Мои записи» — посмотреть статус\n"
        f"• /contacts или «Контакты» — наши контакты\n\n"
        f"<b>Для администраторов:</b>\n"
        f"• /list — все активные заявки\n"
        f"• /confirm &lt;id&gt; — подтвердить заявку\n"
        f"• /cancel &lt;id&gt; — отменить заявку\n"
        f"• /admin_add &lt;id&gt; — добавить администратора",
        parse_mode="HTML",
    )


@dp.message(Command("book"))
@dp.message(F.text.in_({"🛠 Записаться на ТО", "Записаться на ТО", "запиши на ТО", "запись на ТО"}))
async def cmd_book(message: types.Message, state: FSMContext):
    await state.set_state(BookForm.name)
    await message.answer(
        "✏️ <b>Давайте запишем вас!</b>\n\n"
        "Введите ваше <b>имя</b>:",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="HTML",
    )


@dp.message(StateFilter(BookForm.name))
async def process_name(message: types.Message, state: FSMContext):
    if len(message.text.strip()) < 2:
        await message.answer("Пожалуйста, введите имя (минимум 2 символа):")
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(BookForm.car)
    await message.answer("🚗 <b>Марка и модель авто</b> (например: Toyota Camry 2020):", parse_mode="HTML")


@dp.message(StateFilter(BookForm.car))
async def process_car(message: types.Message, state: FSMContext):
    if len(message.text.strip()) < 3:
        await message.answer("Пожалуйста, введите марку и модель (минимум 3 символа):")
        return
    await state.update_data(car=message.text.strip())
    await state.set_state(BookForm.service)
    await message.answer("🔧 <b>Выберите услугу</b>:", reply_markup=services_kb(), parse_mode="HTML")


@dp.message(StateFilter(BookForm.service))
async def process_service(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "🚗 Другое":
        await state.set_state(BookForm.custom_service)
        await message.answer("🔧 Введите вашу услугу вручную:", reply_markup=types.ReplyKeyboardRemove())
        return
    await state.update_data(service=text)
    await state.set_state(BookForm.date)
    await message.answer(
        "📅 <b>На какое число?</b>\n\n"
        "Введите дату в формате <b>ДД.ММ.ГГГГ</b>\n"
        "Например: 25.06.2026",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="HTML",
    )


@dp.message(StateFilter(BookForm.custom_service))
async def process_custom_service(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if len(text) < 3:
        await message.answer("Слишком коротко. Введите название услуги:")
        return
    await state.update_data(service=text)
    await state.set_state(BookForm.date)
    await message.answer(
        "📅 <b>На какое число?</b>\n\n"
        "Введите дату в формате <b>ДД.ММ.ГГГГ</b>\n"
        "Например: 25.06.2026",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="HTML",
    )


@dp.message(StateFilter(BookForm.date))
async def process_date(message: types.Message, state: FSMContext):
    text = message.text.strip()
    import re
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        await message.answer("Неверный формат. Введите дату как <b>ДД.ММ.ГГГГ</b> (например: 25.06.2026):", parse_mode="HTML")
        return
    await state.update_data(date=text)
    await state.set_state(BookForm.time)
    await message.answer(
        "⏰ <b>На какое время?</b>\n\n"
        "Введите время в формате <b>ЧЧ:ММ</b>\n"
        "Например: 14:00",
        parse_mode="HTML",
    )


@dp.message(StateFilter(BookForm.time))
async def process_time(message: types.Message, state: FSMContext):
    import re
    text = message.text.strip()
    if not re.match(r"^\d{2}:\d{2}$", text):
        await message.answer("Неверный формат. Введите время как <b>ЧЧ:ММ</b> (например: 14:00):", parse_mode="HTML")
        return
    hours, mins = map(int, text.split(":"))
    if hours < 0 or hours > 23 or mins < 0 or mins > 59:
        await message.answer("Пожалуйста, введите корректное время (00:00–23:59):")
        return
    await state.update_data(time=text)
    await state.set_state(BookForm.phone)
    await message.answer(
        "📞 <b>Ваш номер телефона</b> (для связи):\n\n"
        "Нажмите кнопку ниже, чтобы отправить номер, или введите вручную:",
        reply_markup=phone_kb(),
        parse_mode="HTML",
    )


@dp.message(StateFilter(BookForm.phone), F.contact)
async def process_phone_contact(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number
    await state.update_data(phone=phone)
    await show_booking_summary(message, state)


@dp.message(StateFilter(BookForm.phone))
async def process_phone_text(message: types.Message, state: FSMContext):
    import re
    text = message.text.strip()
    cleaned = re.sub(r"[\s\-\(\)]", "", text)
    if not re.match(r"^\+?\d{7,15}$", cleaned):
        await message.answer(
            "Пожалуйста, введите корректный номер телефона "
            "(например: +7-999-123-45-67 или 89991234567):"
        )
        return
    await state.update_data(phone=cleaned)
    await show_booking_summary(message, state)


async def show_booking_summary(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = (
        "<b>📋 Проверьте данные:</b>\n\n"
        f"👤 <b>Имя:</b> {data['name']}\n"
        f"🚗 <b>Авто:</b> {data['car']}\n"
        f"🔧 <b>Услуга:</b> {data['service']}\n"
        f"📅 <b>Дата:</b> {data['date']}\n"
        f"⏰ <b>Время:</b> {data['time']}\n"
        f"📞 <b>Телефон:</b> {data['phone']}\n\n"
        "Всё верно?"
    )
    await state.set_state(BookForm.confirm)
    await message.answer(text, reply_markup=yes_no_kb(), parse_mode="HTML")


@dp.message(StateFilter(BookForm.confirm), F.text == "✅ Да, всё верно")
async def process_confirm_yes(message: types.Message, state: FSMContext):
    data = await state.get_data()
    username = message.from_user.username or None
    app = Appointment(
        user_id=message.from_user.id,
        username=username,
        full_name=data["name"],
        car=data["car"],
        service=data["service"],
        date=data["date"],
        time=data["time"],
        phone=data.get("phone", ""),
        status="pending",
    )

    async for session in get_session():
        session.add(app)
        await session.commit()
        await session.refresh(app)

    await state.clear()

    user_text = (
        f"✅ <b>Заявка #{app.id} создана!</b>\n\n"
        f"👤 {data['name']}\n"
        f"🚗 {data['car']}\n"
        f"🔧 {data['service']}\n"
        f"📅 {data['date']} в {data['time']}\n\n"
        f"Я уведомил автосервис. С вами свяжутся для подтверждения.\n"
        f"Статус можно отслеживать через /status"
    )
    await message.answer(user_text, reply_markup=main_kb(), parse_mode="HTML")

    await notify_admins(app)


@dp.message(StateFilter(BookForm.confirm), F.text == "❌ Нет, заново")
async def process_confirm_no(message: types.Message, state: FSMContext):
    await state.clear()
    await cmd_book(message, state)


@dp.message(StateFilter(BookForm.confirm))
async def process_confirm_unknown(message: types.Message, state: FSMContext):
    await message.answer("Пожалуйста, выберите «Да, всё верно» или «Нет, заново»:", reply_markup=yes_no_kb())


@dp.message(Command("status"))
@dp.message(F.text == "📋 Мои записи")
async def cmd_status(message: types.Message):
    async for session in get_session():
        result = await session.execute(
            select(Appointment)
            .where(Appointment.user_id == message.from_user.id)
            .order_by(Appointment.created_at.desc())
        )
        apps = result.scalars().all()

    if not apps:
        await message.answer("У вас пока нет записей. /book — записаться на ТО.", reply_markup=main_kb())
        return

    lines = []
    for i, a in enumerate(apps[:10], 1):
        lines.append(
            f"{i}. {status_emoji(a.status)} <b>{a.service}</b>\n"
            f"   📅 {a.date} в {a.time}\n"
            f"   🚗 {a.car}\n"
            f"   📌 <b>{status_text(a.status)}</b>"
        )

    await message.answer(
        f"📋 <b>Ваши записи</b> (последние {min(len(apps), 10)}):\n\n" + "\n\n".join(lines),
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


def status_text(status: str) -> str:
    return {"pending": "Ожидает подтверждения", "confirmed": "Подтверждено ✅", "cancelled": "Отменено ❌"}.get(status, status)


@dp.message(Command("contacts"))
@dp.message(F.text == "📞 Контакты")
async def cmd_contacts(message: types.Message):
    await message.answer(
        "📞 <b>Контакты автосервиса</b>\n\n"
        "📍 <b>Адрес:</b> ул. Примерная, д. 123\n"
        "🕐 <b>Режим работы:</b> Пн–Сб, 09:00–20:00\n"
        "📞 <b>Телефон:</b> +7-999-123-45-67\n"
        "📱 <b>Telegram:</b> @autoservice\n\n"
        "Для записи используйте /book или меню ниже 👇",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@dp.message(Command("list"))
async def admin_list(message: types.Message):
    if not await is_admin(message.from_user.id):
        return

    async for session in get_session():
        result = await session.execute(
            select(Appointment)
            .where(Appointment.status != "cancelled")
            .order_by(Appointment.created_at.desc())
        )
        apps = result.scalars().all()

    if not apps:
        await message.answer("Нет активных заявок.")
        return

    lines = []
    for a in apps:
        lines.append(
            f"#{a.id} {status_emoji(a.status)} "
            f"{a.full_name} — {a.service}\n"
            f"📅 {a.date} в {a.time} | 🚗 {a.car}"
        )

    for chunk in _chunks(lines, 15):
        await message.answer("📋 <b>Все заявки:</b>\n\n" + "\n\n".join(chunk), parse_mode="HTML")


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


@dp.message(Command("confirm"))
async def admin_confirm(message: types.Message, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /confirm <номер_заявки>")
        return

    app_id = int(command.args.strip())
    async for session in get_session():
        result = await session.execute(select(Appointment).where(Appointment.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            await message.answer(f"Заявка #{app_id} не найдена.")
            return
        app.status = "confirmed"
        await session.commit()

    await message.answer(f"✅ Заявка #{app_id} подтверждена!")

    try:
        await bot.send_message(
            app.user_id,
            f"✅ <b>Заявка #{app.id} подтверждена!</b>\n\n"
            f"🔧 {app.service}\n📅 {app.date} в {app.time}\n\n"
            f"Ждём вас!",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {app.user_id}: {e}")


@dp.message(Command("cancel"))
async def admin_cancel(message: types.Message, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /cancel <номер_заявки>")
        return

    app_id = int(command.args.strip())
    async for session in get_session():
        result = await session.execute(select(Appointment).where(Appointment.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            await message.answer(f"Заявка #{app_id} не найдена.")
            return
        app.status = "cancelled"
        await session.commit()

    await message.answer(f"❌ Заявка #{app_id} отменена.")

    try:
        await bot.send_message(
            app.user_id,
            f"❌ <b>Заявка #{app.id} отменена.</b>\n\n"
            f"Свяжитесь с автосервисом для уточнения: +7-999-123-45-67",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {app.user_id}: {e}")


@dp.message(Command("admin_add"))
async def admin_add(message: types.Message, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /admin_add <telegram_id>")
        return

    uid = int(command.args.strip())
    async for session in get_session():
        existing = await session.execute(select(Admin).where(Admin.user_id == uid))
        if existing.scalar_one_or_none():
            await message.answer(f"Пользователь {uid} уже администратор.")
            return
        admin = Admin(user_id=uid, username=None, is_active=True)
        session.add(admin)
        await session.commit()

    await message.answer(f"✅ Пользователь {uid} добавлен как администратор!")

    try:
        await bot.send_message(
            uid,
            "👑 Вас назначили администратором бота автосервиса!\n"
            "Используйте /help для просмотра команд.",
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("confirm_"))
async def callback_confirm(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    app_id = int(callback.data.split("_")[1])
    async for session in get_session():
        result = await session.execute(select(Appointment).where(Appointment.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        app.status = "confirmed"
        await session.commit()

    await callback.message.edit_text(
        callback.message.text.html_text,
        parse_mode="HTML",
    )
    await callback.message.answer(f"✅ Заявка #{app_id} подтверждена!")
    await callback.answer("Подтверждено", show_alert=False)

    try:
        await bot.send_message(
            app.user_id,
            f"✅ <b>Заявка #{app.id} подтверждена!</b>\n\n"
            f"🔧 {app.service}\n📅 {app.date} в {app.time}\n\n"
            f"Ждём вас!",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {app.user_id}: {e}")


@dp.callback_query(F.data.startswith("cancel_"))
async def callback_cancel(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    app_id = int(callback.data.split("_")[1])
    async for session in get_session():
        result = await session.execute(select(Appointment).where(Appointment.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        app.status = "cancelled"
        await session.commit()

    await callback.message.edit_text(
        callback.message.text.html_text,
        parse_mode="HTML",
    )
    await callback.message.answer(f"❌ Заявка #{app_id} отменена!")
    await callback.answer("Отменено", show_alert=False)

    try:
        await bot.send_message(
            app.user_id,
            f"❌ <b>Заявка #{app.id} отменена.</b>\n\n"
            f"Свяжитесь с автосервисом: +7-999-123-45-67",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {app.user_id}: {e}")


@dp.message()
async def fallback(message: types.Message, state: FSMContext):
    text = message.text.lower().strip()
    booking_keywords = [
        "запиши", "запись", "то", "записаться", "хочу", "запишите",
        "booking", "book", "записать", "на то", "на те"
    ]
    if any(kw in text for kw in booking_keywords):
        await state.clear()
        await cmd_book(message, state)
    else:
        await message.answer(
            "Я вас не понял. Используйте /book для записи или /help для справки.",
            reply_markup=main_kb(),
        )


async def on_startup(bot_instance: Bot) -> None:
    domain = os.getenv("RENDER_EXTERNAL_URL", "").strip("/")
    if domain:
        webhook_url = f"{domain}/webhook"
        await bot_instance.set_webhook(webhook_url)
        logger.info(f"Webhook set to {webhook_url}")


async def on_shutdown(bot_instance: Bot) -> None:
    await bot_instance.delete_webhook()


async def main():
    global bot
    await init_db(config.db_url)
    logger.info("Database initialized")

    bot = Bot(token=config.bot_token)

    PORT = int(os.getenv("PORT", "8080"))
    DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "").strip("/")
    WEBHOOK_PATH = "/webhook"

    if DOMAIN:
        app = aiohttp.web.Application()
        app["bot"] = bot

        async def health(_):
            return aiohttp.web.Response(text="OK")

        app.router.add_get("/health", health)
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot, on_startup=on_startup, on_shutdown=on_shutdown)

        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, host="0.0.0.0", port=PORT)
        await site.start()
        logger.info(f"Webhook server started on port {PORT}")
        logger.info(f"Webhook URL: {DOMAIN}{WEBHOOK_PATH}")

        await asyncio.Event().wait()
    else:
        logger.info("No RENDER_EXTERNAL_URL — using polling mode")
        logger.info(f"Bot started. Admins: {config.admin_ids}")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
