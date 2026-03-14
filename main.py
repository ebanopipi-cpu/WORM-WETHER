import asyncio
import logging
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from decimal import Decimal, ROUND_HALF_UP

# ТВОЙ ТОКЕН - ВСТАВЬ СЮДА!
import os
TOKEN = os.getenv("BOT_TOKEN")

# Включаем логирование
logging.basicConfig(level=logging.INFO)


# Состояния для конвертации
class ConvertStates(StatesGroup):
    rub_to_kzt = State()
    kzt_to_rub = State()


# Класс для получения курса валют
class CurrencyAPI:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    async def get_rate(self):
        """Получаем курс рубля к тенге"""
        try:
            session = await self.get_session()

            # Пробуем получить курс с бесплатного API
            async with session.get(
                    "https://api.exchangerate-api.com/v4/latest/RUB",
                    timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'rates' in data and 'KZT' in data['rates']:
                        rate = data['rates']['KZT']
                        return round(rate, 2)

            # Если не сработало, пробуем запасной вариант
            async with session.get(
                    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/rub.json",
                    timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'rub' in data and 'kzt' in data['rub']:
                        rate = data['rub']['kzt']
                        return round(rate, 2)

        except Exception as e:
            logging.error(f"Ошибка получения курса: {e}")

        # Если всё сломалось - возвращаем примерный курс
        return 6.15

    async def close(self):
        if self.session:
            await self.session.close()


# Основной класс бота
class CurrencyBot:
    def __init__(self, token):
        self.bot = Bot(token=token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.currency_api = CurrencyAPI()
        self.setup_handlers()

    def setup_handlers(self):
        @self.dp.message(Command("start"))
        async def start_command(message: types.Message):
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="🇷🇺 RUB → KZT 🇰🇿"), KeyboardButton(text="🇰🇿 KZT → RUB 🇷🇺")],
                    [KeyboardButton(text="📊 Курс"), KeyboardButton(text="❓ Помощь")]
                ],
                resize_keyboard=True
            )
            await message.answer(
                "👋 Привет! Я бот для конвертации RUB ↔ KZT\n\n"
                "Выбери направление конвертации:",
                reply_markup=keyboard
            )

        @self.dp.message(F.text == "🇷🇺 RUB → KZT 🇰🇿")
        async def rub_to_kzt_start(message: types.Message, state: FSMContext):
            await message.answer("💵 Введи сумму в рублях:")
            await state.set_state(ConvertStates.rub_to_kzt)

        @self.dp.message(ConvertStates.rub_to_kzt)
        async def rub_to_kzt_calc(message: types.Message, state: FSMContext):
            try:
                amount = float(message.text.replace(',', '.'))
                rate = await self.currency_api.get_rate()
                result = amount * rate

                await message.answer(
                    f"✅ {amount:,.2f} RUB = {result:,.2f} KZT\n"
                    f"📈 Курс: 1 RUB = {rate} KZT"
                )
                await state.clear()
            except:
                await message.answer("❌ Введи число нормально!")

        @self.dp.message(F.text == "🇰🇿 KZT → RUB 🇷🇺")
        async def kzt_to_rub_start(message: types.Message, state: FSMContext):
            await message.answer("💶 Введи сумму в тенге:")
            await state.set_state(ConvertStates.kzt_to_rub)

        @self.dp.message(ConvertStates.kzt_to_rub)
        async def kzt_to_rub_calc(message: types.Message, state: FSMContext):
            try:
                amount = float(message.text.replace(',', '.'))
                rate = await self.currency_api.get_rate()
                result = amount / rate

                await message.answer(
                    f"✅ {amount:,.2f} KZT = {result:,.2f} RUB\n"
                    f"📈 Курс: 1 RUB = {rate} KZT"
                )
                await state.clear()
            except:
                await message.answer("❌ Введи число нормально!")

        @self.dp.message(F.text == "📊 Курс")
        async def show_rate(message: types.Message):
            rate = await self.currency_api.get_rate()
            await message.answer(f"📊 Текущий курс: 1 RUB = {rate} KZT")

        @self.dp.message(F.text == "❓ Помощь")
        async def help_command(message: types.Message):
            await message.answer(
                "🤖 Как пользоваться:\n\n"
                "1. Выбери направление конвертации\n"
                "2. Введи сумму\n"
                "3. Получи результат!\n\n"
                "Курс обновляется автоматически 📈"
            )

    async def run(self):
        print("🚀 Бот запущен!")
        await self.dp.start_polling(self.bot)


# Запуск
async def main():
    bot = CurrencyBot(TOKEN)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
