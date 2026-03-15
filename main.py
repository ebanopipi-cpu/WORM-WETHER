import asyncio
import logging
import os
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties  # ВАЖНО: ДОБАВЛЕНО!

# Загружаем переменные из .env файла
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("Токен не найден! Проверь файл .env")

# Включаем логирование
logging.basicConfig(level=logging.INFO)


# Состояния для конвертации
class ConvertStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_from_currency = State()
    waiting_for_to_currency = State()


# Класс для получения курса валют и крипты
class CurrencyAPI:
    def __init__(self):
        self.session = None
        # Фиатные валюты
        self.fiat_currencies = {
            'RUB': '🇷🇺 RUB',
            'KZT': '🇰🇿 KZT',
            'USD': '🇺🇸 USD',
            'EUR': '🇪🇺 EUR',
            'CNY': '🇨🇳 CNY',
            'GBP': '🇬🇧 GBP',
            'TRY': '🇹🇷 TRY',
            'AED': '🇦🇪 AED'
        }
        # Криптовалюты
        self.crypto_currencies = {
            'BTC': '₿ BTC (Bitcoin)',
            'ETH': '⟠ ETH (Ethereum)',
            'BNB': '⧫ BNB (Binance Coin)',
            'SOL': '◎ SOL (Solana)',
            'XRP': '✕ XRP (Ripple)',
            'ADA': '🅰 ADA (Cardano)',
            'DOGE': 'Ð DOGE (Dogecoin)',
            'TON': '⚡ TON (Toncoin)',
            'TRX': '◈ TRX (Tron)',
            'MATIC': '⬡ MATIC (Polygon)'
        }

        # Маппинг ID для CoinGecko
        self.crypto_ids = {
            'BTC': 'bitcoin',
            'ETH': 'ethereum',
            'BNB': 'binancecoin',
            'SOL': 'solana',
            'XRP': 'ripple',
            'ADA': 'cardano',
            'DOGE': 'dogecoin',
            'TON': 'the-open-network',
            'TRX': 'tron',
            'MATIC': 'matic-network'
        }

    async def get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    async def get_rate(self, from_currency, to_currency):
        """Получаем курс валют/крипты"""
        try:
            session = await self.get_session()

            # Определяем тип валют
            from_is_crypto = from_currency in self.crypto_currencies
            to_is_crypto = to_currency in self.crypto_currencies

            # Для крипты используем CoinGecko
            if from_is_crypto or to_is_crypto:
                return await self.get_crypto_rate(from_currency, to_currency, from_is_crypto, to_is_crypto)
            else:
                # Для фиатных валют используем exchangerate-api
                return await self.get_fiat_rate(from_currency, to_currency)

        except Exception as e:
            logging.error(f"Ошибка получения курса {from_currency}→{to_currency}: {e}")
            return self.get_default_rate(from_currency, to_currency)

    async def get_crypto_rate(self, from_currency, to_currency, from_is_crypto, to_is_crypto):
        """Получение курса с участием криптовалют"""
        session = await self.get_session()

        # Если обе криптовалюты
        if from_is_crypto and to_is_crypto:
            # Получаем обе цены в USD
            from_price = await self.get_crypto_price_in_usd(from_currency)
            to_price = await self.get_crypto_price_in_usd(to_currency)

            if from_price and to_price:
                return round(from_price / to_price, 8)

        # Если из крипты в фиат
        elif from_is_crypto and not to_is_crypto:
            price_in_usd = await self.get_crypto_price_in_usd(from_currency)
            if price_in_usd and to_currency == 'USD':
                return round(price_in_usd, 2)
            elif price_in_usd:
                # Конвертируем USD в целевую фиатную валюту
                usd_rate = await self.get_fiat_rate('USD', to_currency)
                if usd_rate:
                    return round(price_in_usd * usd_rate, 4)

        # Если из фиата в крипту
        elif not from_is_crypto and to_is_crypto:
            if from_currency == 'USD':
                price_in_usd = await self.get_crypto_price_in_usd(to_currency)
                if price_in_usd:
                    return round(1 / price_in_usd, 8)
            else:
                # Конвертируем фиат в USD, потом в крипту
                usd_rate = await self.get_fiat_rate(from_currency, 'USD')
                if usd_rate:
                    price_in_usd = await self.get_crypto_price_in_usd(to_currency)
                    if price_in_usd:
                        return round(usd_rate / price_in_usd, 8)

        return None

    async def get_crypto_price_in_usd(self, crypto):
        """Получение цены криптовалюты в USD"""
        try:
            session = await self.get_session()
            crypto_id = self.crypto_ids.get(crypto)
            if not crypto_id:
                return None

            async with session.get(
                    f"https://api.coingecko.com/api/v3/simple/price?ids={crypto_id}&vs_currencies=usd",
                    timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if crypto_id in data and 'usd' in data[crypto_id]:
                        return data[crypto_id]['usd']
        except Exception as e:
            logging.error(f"Ошибка получения цены {crypto}: {e}")
        return None

    async def get_fiat_rate(self, from_currency, to_currency):
        """Курс фиатных валют через exchangerate-api"""
        try:
            session = await self.get_session()
            async with session.get(
                    f"https://api.exchangerate-api.com/v4/latest/{from_currency}",
                    timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'rates' in data and to_currency in data['rates']:
                        return round(data['rates'][to_currency], 4)
        except:
            pass
        return None

    def get_default_rate(self, from_currency, to_currency):
        """Возвращает примерный курс для популярных пар"""
        default_rates = {
            ('RUB', 'KZT'): 6.15,
            ('KZT', 'RUB'): 0.16,
            ('USD', 'RUB'): 92.50,
            ('RUB', 'USD'): 0.011,
            ('EUR', 'RUB'): 100.50,
            ('RUB', 'EUR'): 0.0099,
            ('USD', 'KZT'): 470.00,
            ('KZT', 'USD'): 0.0021,
            ('BTC', 'USD'): 65000.00,
            ('USD', 'BTC'): 0.000015,
            ('ETH', 'USD'): 3500.00,
            ('USD', 'ETH'): 0.000285,
            ('BTC', 'RUB'): 6000000.00,
            ('RUB', 'BTC'): 0.00000016,
            ('BTC', 'ETH'): 18.50,
            ('ETH', 'BTC'): 0.054,
        }
        return default_rates.get((from_currency, to_currency), None)

    async def get_all_crypto_prices(self, vs_currency='USD'):
        """Получаем цены всех криптовалют"""
        try:
            session = await self.get_session()
            crypto_ids = [self.crypto_ids[c] for c in self.crypto_currencies.keys() if c in self.crypto_ids]
            ids_param = ','.join(crypto_ids)

            async with session.get(
                    f"https://api.coingecko.com/api/v3/simple/price?ids={ids_param}&vs_currencies={vs_currency.lower()}",
                    timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    prices = {}
                    # Создаем обратный маппинг
                    id_to_code = {v: k for k, v in self.crypto_ids.items()}

                    for crypto_id, price_data in data.items():
                        if crypto_id in id_to_code and vs_currency.lower() in price_data:
                            code = id_to_code[crypto_id]
                            prices[code] = price_data[vs_currency.lower()]
                    return prices
        except Exception as e:
            logging.error(f"Ошибка получения цен криптовалют: {e}")
        return None

    async def close(self):
        if self.session:
            await self.session.close()


# ========== ИНИЦИАЛИЗАЦИЯ - ИСПРАВЛЕНО ==========
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
currency_api = CurrencyAPI()


# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    """Главная клавиатура"""
    buttons = [
        [KeyboardButton(text="💱 Конвертация"), KeyboardButton(text="📊 Курсы валют")],
        [KeyboardButton(text="₿ Криптовалюта"), KeyboardButton(text="⭐ Популярные пары")],
        [KeyboardButton(text="❓ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_currency_type_keyboard():
    """Выбор типа валюты"""
    buttons = [
        [KeyboardButton(text="💵 Фиатные валюты")],
        [KeyboardButton(text="₿ Криптовалюта")],
        [KeyboardButton(text="◀ Назад в меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_fiat_keyboard():
    """Клавиатура с фиатными валютами"""
    buttons = [
        [KeyboardButton(text="🇷🇺 RUB"), KeyboardButton(text="🇰🇿 KZT"), KeyboardButton(text="🇺🇸 USD")],
        [KeyboardButton(text="🇪🇺 EUR"), KeyboardButton(text="🇨🇳 CNY"), KeyboardButton(text="🇬🇧 GBP")],
        [KeyboardButton(text="🇹🇷 TRY"), KeyboardButton(text="🇦🇪 AED")],
        [KeyboardButton(text="◀ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_crypto_keyboard():
    """Клавиатура с криптовалютами"""
    buttons = [
        [KeyboardButton(text="₿ BTC"), KeyboardButton(text="⟠ ETH"), KeyboardButton(text="⧫ BNB")],
        [KeyboardButton(text="◎ SOL"), KeyboardButton(text="✕ XRP"), KeyboardButton(text="🅰 ADA")],
        [KeyboardButton(text="Ð DOGE"), KeyboardButton(text="⚡ TON"), KeyboardButton(text="◈ TRX")],
        [KeyboardButton(text="⬡ MATIC")],
        [KeyboardButton(text="◀ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_popular_pairs_keyboard():
    """Клавиатура с популярными парами (фиат + крипта)"""
    buttons = [
        [KeyboardButton(text="🇷🇺 RUB → 🇰🇿 KZT"), KeyboardButton(text="🇰🇿 KZT → 🇷🇺 RUB")],
        [KeyboardButton(text="🇺🇸 USD → 🇷🇺 RUB"), KeyboardButton(text="🇷🇺 RUB → 🇺🇸 USD")],
        [KeyboardButton(text="₿ BTC → 🇺🇸 USD"), KeyboardButton(text="🇺🇸 USD → ₿ BTC")],
        [KeyboardButton(text="⟠ ETH → 🇺🇸 USD"), KeyboardButton(text="₿ BTC → ⟠ ETH")],
        [KeyboardButton(text="◀ Назад в меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_back_keyboard():
    """Кнопка назад"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="◀ Назад")]],
        resize_keyboard=True
    )


# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Приветствие"""
    await message.answer(
        "👋 <b>WORM CONVERTER</b> с криптовалютой\n\n"
        "💱 Конвертация - перевести любые валюты/крипту\n"
        "📊 Курсы валют - курсы фиатных валют\n"
        "₿ Криптовалюта - курсы криптовалют\n"
        "⭐ Популярные пары - быстрая конвертация\n\n"
        "Выбери действие:",
        reply_markup=get_main_keyboard()
    )


@dp.message(F.text == "💱 Конвертация")
async def conversion_start(message: types.Message, state: FSMContext):
    """Начало конвертации"""
    await state.set_state(ConvertStates.waiting_for_from_currency)
    await message.answer(
        "💱 <b>Выбери тип валюты</b>, из которой хочешь конвертировать:",
        reply_markup=get_currency_type_keyboard()
    )


@dp.message(F.text == "📊 Курсы валют")
async def show_fiat_rates(message: types.Message):
    """Показать курсы фиатных валют к USD"""
    wait_msg = await message.answer("⏳ Получаю курсы валют...")

    rates_text = "<b>📊 Курсы валют к USD</b>\n\n"

    for code, name in currency_api.fiat_currencies.items():
        if code != 'USD':
            rate = await currency_api.get_fiat_rate('USD', code)
            if rate:
                flag = name.split()[0]
                rates_text += f"{flag} 1 USD = {rate} {code}\n"

    await wait_msg.delete()
    await message.answer(rates_text, reply_markup=get_main_keyboard())


@dp.message(F.text == "₿ Криптовалюта")
async def show_crypto_prices(message: types.Message):
    """Показать цены криптовалют"""
    wait_msg = await message.answer("⏳ Получаю цены криптовалют...")

    prices = await currency_api.get_all_crypto_prices('USD')

    if prices:
        text = "<b>₿ Цены криптовалют (USD)</b>\n\n"
        for code, price in prices.items():
            full_name = currency_api.crypto_currencies[code]
            symbol = full_name.split()[0]
            text += f"{symbol} {code}: ${price:,.2f}\n"

        await wait_msg.delete()
        await message.answer(text, reply_markup=get_main_keyboard())
    else:
        await wait_msg.delete()
        await message.answer("❌ Не удалось получить цены. Попробуй позже.", reply_markup=get_main_keyboard())


@dp.message(F.text == "⭐ Популярные пары")
async def popular_pairs(message: types.Message):
    """Меню популярных пар"""
    await message.answer(
        "🔥 <b>Популярные пары</b>\nВыбери для быстрой конвертации:",
        reply_markup=get_popular_pairs_keyboard()
    )


@dp.message(F.text == "❓ Помощь")
async def help_command(message: types.Message):
    """Помощь"""
    await message.answer(
        "<b>🤖 Как пользоваться:</b>\n\n"
        "1. Выбери '💱 Конвертация'\n"
        "2. Выбери тип валюты (фиат или крипта)\n"
        "3. Выбери валюту\n"
        "4. Введи сумму\n"
        "5. Получи результат!\n\n"
        "Или используй '⭐ Популярные пары' для быстрой конвертации\n\n"
        "<b>Доступные криптовалюты:</b>\n"
        "₿ BTC, ⟠ ETH, ⧫ BNB, ◎ SOL, ✕ XRP, 🅰 ADA, Ð DOGE, ⚡ TON, ◈ TRX, ⬡ MATIC\n\n"
        "<i>Курсы обновляются автоматически 📈</i>",
        reply_markup=get_main_keyboard()
    )


@dp.message(F.text == "◀ Назад в меню")
async def back_to_menu(message: types.Message, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    await start_command(message)


@dp.message(F.text == "◀ Назад")
async def back_to_previous(message: types.Message, state: FSMContext):
    """Возврат на шаг назад"""
    current_state = await state.get_state()

    if current_state == ConvertStates.waiting_for_from_currency:
        await state.clear()
        await start_command(message)
    elif current_state == ConvertStates.waiting_for_to_currency:
        await state.set_state(ConvertStates.waiting_for_from_currency)
        await message.answer(
            "💱 <b>Выбери тип валюты</b>, из которой хочешь конвертировать:",
            reply_markup=get_currency_type_keyboard()
        )
    elif current_state == ConvertStates.waiting_for_amount:
        await state.set_state(ConvertStates.waiting_for_to_currency)
        await message.answer(
            "💱 <b>Выбери валюту</b>, в которую хочешь конвертировать:",
            reply_markup=get_currency_type_keyboard()
        )
    else:
        await state.clear()
        await start_command(message)


@dp.message(F.text == "💵 Фиатные валюты")
async def select_fiat_from(message: types.Message, state: FSMContext):
    """Выбор фиатной валюты"""
    await message.answer(
        "💱 <b>Выбери валюту</b>:",
        reply_markup=get_fiat_keyboard()
    )


@dp.message(F.text == "₿ Криптовалюта")
async def select_crypto_from(message: types.Message, state: FSMContext):
    """Выбор криптовалюты"""
    await message.answer(
        "💱 <b>Выбери криптовалюту</b>:",
        reply_markup=get_crypto_keyboard()
    )


# Обработка выбора валюты
@dp.message(ConvertStates.waiting_for_from_currency)
async def process_from_currency(message: types.Message, state: FSMContext):
    """Выбор исходной валюты"""
    if message.text in ["💵 Фиатные валюты", "₿ Криптовалюта", "◀ Назад", "◀ Назад в меню"]:
        return

    # Проверяем, что выбрана валюта из списка
    currency_code = None
    currency_type = None

    # Проверяем фиатные
    for code, name in currency_api.fiat_currencies.items():
        if code in message.text or (len(message.text) > 2 and message.text[-3:] == code):
            currency_code = code
            currency_type = 'fiat'
            break

    # Проверяем крипту
    if not currency_code:
        for code, name in currency_api.crypto_currencies.items():
            if code in message.text or (len(message.text) > 2 and message.text[-3:] == code):
                currency_code = code
                currency_type = 'crypto'
                break

    if currency_code:
        await state.update_data(from_currency=currency_code, from_type=currency_type)
        await state.set_state(ConvertStates.waiting_for_to_currency)
        await message.answer(
            "💱 <b>Теперь выбери валюту</b>, в которую хочешь конвертировать:",
            reply_markup=get_currency_type_keyboard()
        )
    else:
        await message.answer("❌ Выбери валюту из списка!")


@dp.message(ConvertStates.waiting_for_to_currency)
async def process_to_currency(message: types.Message, state: FSMContext):
    """Выбор целевой валюты"""
    if message.text in ["💵 Фиатные валюты", "₿ Криптовалюта", "◀ Назад", "◀ Назад в меню"]:
        return

    currency_code = None
    currency_type = None

    # Проверяем фиатные
    for code, name in currency_api.fiat_currencies.items():
        if code in message.text or (len(message.text) > 2 and message.text[-3:] == code):
            currency_code = code
            currency_type = 'fiat'
            break

    # Проверяем крипту
    if not currency_code:
        for code, name in currency_api.crypto_currencies.items():
            if code in message.text or (len(message.text) > 2 and message.text[-3:] == code):
                currency_code = code
                currency_type = 'crypto'
                break

    if currency_code:
        data = await state.get_data()

        if currency_code == data['from_currency']:
            await message.answer("❌ Валюты должны быть разными! Выбери другую:")
            return

        await state.update_data(to_currency=currency_code, to_type=currency_type)
        await state.set_state(ConvertStates.waiting_for_amount)

        from_name = data['from_currency']
        if data['from_type'] == 'fiat':
            from_name = currency_api.fiat_currencies[data['from_currency']]
        else:
            from_name = currency_api.crypto_currencies[data['from_currency']]

        await message.answer(
            f"💵 <b>Введи сумму</b> в {from_name}:",
            reply_markup=get_back_keyboard()
        )
    else:
        await message.answer("❌ Выбери валюту из списка!")


# Обработка популярных пар
@dp.message(F.text.in_([
    "🇷🇺 RUB → 🇰🇿 KZT", "🇰🇿 KZT → 🇷🇺 RUB",
    "🇺🇸 USD → 🇷🇺 RUB", "🇷🇺 RUB → 🇺🇸 USD",
    "₿ BTC → 🇺🇸 USD", "🇺🇸 USD → ₿ BTC",
    "⟠ ETH → 🇺🇸 USD", "₿ BTC → ⟠ ETH"
]))
async def popular_pair_selected(message: types.Message, state: FSMContext):
    """Обработка выбора популярной пары"""
    pair_map = {
        "🇷🇺 RUB → 🇰🇿 KZT": ("RUB", "fiat", "KZT", "fiat"),
        "🇰🇿 KZT → 🇷🇺 RUB": ("KZT", "fiat", "RUB", "fiat"),
        "🇺🇸 USD → 🇷🇺 RUB": ("USD", "fiat", "RUB", "fiat"),
        "🇷🇺 RUB → 🇺🇸 USD": ("RUB", "fiat", "USD", "fiat"),
        "₿ BTC → 🇺🇸 USD": ("BTC", "crypto", "USD", "fiat"),
        "🇺🇸 USD → ₿ BTC": ("USD", "fiat", "BTC", "crypto"),
        "⟠ ETH → 🇺🇸 USD": ("ETH", "crypto", "USD", "fiat"),
        "₿ BTC → ⟠ ETH": ("BTC", "crypto", "ETH", "crypto")
    }

    from_curr, from_type, to_curr, to_type = pair_map[message.text]
    await state.update_data(
        from_currency=from_curr,
        from_type=from_type,
        to_currency=to_curr,
        to_type=to_type
    )
    await state.set_state(ConvertStates.waiting_for_amount)

    from_name = from_curr
    if from_type == 'fiat':
        from_name = currency_api.fiat_currencies[from_curr]
    else:
        from_name = currency_api.crypto_currencies[from_curr]

    await message.answer(
        f"💵 <b>Введи сумму</b> в {from_name}:",
        reply_markup=get_back_keyboard()
    )


# Обработка ввода суммы
@dp.message(ConvertStates.waiting_for_amount)
async def process_amount(message: types.Message, state: FSMContext):
    """Конвертация суммы"""
    if message.text == "◀ Назад":
        await back_to_previous(message, state)
        return

    try:
        # Чистим ввод
        amount = float(message.text.replace(',', '.').replace(' ', ''))

        if amount <= 0:
            await message.answer("❌ Сумма должна быть больше 0!")
            return

        data = await state.get_data()
        from_curr = data['from_currency']
        to_curr = data['to_currency']

        # Показываем процесс
        wait_msg = await message.answer("⏳ Получаю курс...")

        # Получаем курс
        rate = await currency_api.get_rate(from_curr, to_curr)

        if rate:
            # Считаем результат
            result = amount * rate

            # Форматируем числа
            if 'BTC' in [from_curr, to_curr] or 'ETH' in [from_curr, to_curr]:
                # Для крипты больше знаков
                amount_str = f"{amount:.8f}".rstrip('0').rstrip('.') if '.' in f"{amount:.8f}" else f"{amount:.8f}"
                result_str = f"{result:.8f}".rstrip('0').rstrip('.') if '.' in f"{result:.8f}" else f"{result:.8f}"
                rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
            else:
                amount_str = f"{amount:,.2f}".replace(',', ' ')
                result_str = f"{result:,.2f}".replace(',', ' ')
                rate_str = f"{rate:.4f}".rstrip('0').rstrip('.')

            await wait_msg.delete()

            # Отправляем результат
            await message.answer(
                f"✅ <b>{amount_str}</b> {from_curr} = <b>{result_str}</b> {to_curr}\n"
                f"📈 Курс: 1 {from_curr} = {rate_str} {to_curr}",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
        else:
            await wait_msg.delete()
            await message.answer(
                f"❌ Не удалось получить курс {from_curr} → {to_curr}\nПопробуй позже.",
                reply_markup=get_main_keyboard()
            )
            await state.clear()

    except ValueError:
        await message.answer("❌ Введи число нормально! (например: 1000 или 0.001)")
    except Exception as e:
        await message.answer("❌ Что-то пошло не так. Попробуй снова.", reply_markup=get_main_keyboard())
        await state.clear()
        logging.error(f"Ошибка: {e}")


# Запуск бота
async def main():
    print("🚀 Бот WORM с криптовалютой запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await currency_api.close()


if __name__ == "__main__":
    asyncio.run(main())
