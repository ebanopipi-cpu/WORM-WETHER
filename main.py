import asyncio
import logging
import os
import time
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from dotenv import load_dotenv
import aiohttp
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties

# Загружаем переменные из .env файла
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("Токен не найден! Проверь файл .env")

# Включаем логирование
logging.basicConfig(level=logging.INFO)


# ========== ANTI-SPAM MIDDLEWARE ==========
class AntiSpamMiddleware(BaseMiddleware):
    """Защита от флуда - не дает спамить командами"""
    def __init__(self, rate_limit=2, per_seconds=3):
        self.rate_limit = rate_limit
        self.per_seconds = per_seconds
        self.user_timestamps = defaultdict(list)
        
    async def __call__(self, handler, event, data):
        if not isinstance(event, types.Message):
            return await handler(event, data)
            
        user_id = event.from_user.id
        now = time.time()
        
        # Очищаем старые записи
        self.user_timestamps[user_id] = [
            ts for ts in self.user_timestamps[user_id] 
            if now - ts < self.per_seconds
        ]
        
        # Проверяем лимит
        if len(self.user_timestamps[user_id]) >= self.rate_limit:
            await event.answer("⏳ Не так быстро! Подожди пару секунд.")
            return
            
        self.user_timestamps[user_id].append(now)
        return await handler(event, data)


# Состояния для конвертации
class ConvertStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_from_currency = State()
    waiting_for_to_currency = State()


# ========== УСКОРЕННЫЙ КЛАСС ДЛЯ КУРСОВ ==========
class CurrencyAPI:
    def __init__(self):
        self.session = None
        # Кэш для хранения курсов {from_curr_to_curr: {'rate': value, 'decimal': Decimal, 'timestamp': time}}
        self.cache = {}
        self.cache_ttl = 600  # 10 минут (600 секунд)
        
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
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def _get_cache_key(self, from_currency, to_currency):
        return f"{from_currency}_{to_currency}"
    
    def _get_from_cache(self, from_currency, to_currency):
        """Получить курс из кэша, если он свежий"""
        key = self._get_cache_key(from_currency, to_currency)
        if key in self.cache:
            data = self.cache[key]
            if time.time() - data['timestamp'] < self.cache_ttl:
                return data
            else:
                # Протухло - удаляем
                del self.cache[key]
        return None
    
    def _save_to_cache(self, from_currency, to_currency, rate_float):
        """Сохранить курс в кэш"""
        key = self._get_cache_key(from_currency, to_currency)
        self.cache[key] = {
            'rate_float': rate_float,
            'rate_decimal': Decimal(str(rate_float)),
            'timestamp': time.time()
        }

    async def get_rate(self, from_currency, to_currency):
        """Получаем курс валют/крипты с использованием кэша"""
        # Проверяем кэш
        cached = self._get_from_cache(from_currency, to_currency)
        if cached:
            return cached['rate_decimal']
        
        # Если в кэше нет - идем в API
        try:
            session = await self.get_session()
            
            # Определяем тип валют
            from_is_crypto = from_currency in self.crypto_currencies
            to_is_crypto = to_currency in self.crypto_currencies
            
            rate_float = None
            
            # Для крипты используем Binance (быстрее) или CoinGecko
            if from_is_crypto or to_is_crypto:
                rate_float = await self._get_crypto_rate_fast(from_currency, to_currency, from_is_crypto, to_is_crypto)
            else:
                # Для фиатных валют используем floatrates (быстрее exchangerate-api)
                rate_float = await self._get_fiat_rate_fast(from_currency, to_currency)
            
            if rate_float:
                # Сохраняем в кэш
                self._save_to_cache(from_currency, to_currency, rate_float)
                return Decimal(str(rate_float))
            
        except Exception as e:
            logging.error(f"Ошибка получения курса {from_currency}→{to_currency}: {e}")
        
        # Если всё сломалось - возвращаем примерный курс
        default_rate = self._get_default_rate(from_currency, to_currency)
        if default_rate:
            self._save_to_cache(from_currency, to_currency, default_rate)
            return Decimal(str(default_rate))
        
        return None
    
    async def _get_fiat_rate_fast(self, from_currency, to_currency):
        """Быстрый API для фиатных валют (floatrates)"""
        try:
            session = await self.get_session()
            # floatrates отдает JSON быстрее чем exchangerate-api
            async with session.get(
                f"http://www.floatrates.com/daily/{from_currency.lower()}.json",
                timeout=5
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if to_currency.lower() in data:
                        return data[to_currency.lower()]['rate']
        except:
            pass
        
        # Если floatrates не сработал - пробуем exchangerate-api
        return await self._get_fiat_rate_backup(from_currency, to_currency)
    
    async def _get_fiat_rate_backup(self, from_currency, to_currency):
        """Запасной API для фиатных валют"""
        try:
            session = await self.get_session()
            async with session.get(
                f"https://api.exchangerate-api.com/v4/latest/{from_currency}",
                timeout=5
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'rates' in data and to_currency in data['rates']:
                        return data['rates'][to_currency]
        except:
            pass
        return None
    
    async def _get_crypto_rate_fast(self, from_currency, to_currency, from_is_crypto, to_is_crypto):
        """Быстрый API для крипты (Binance)"""
        try:
            session = await self.get_session()
            
            # Binance API очень быстрый
            if from_is_crypto and not to_is_crypto and to_currency == 'USD':
                # BTC → USD
                symbol = f"{from_currency}USDT"
                async with session.get(
                    f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
                    timeout=5
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return float(data['price'])
            
            elif not from_is_crypto and to_is_crypto and from_currency == 'USD':
                # USD → BTC
                symbol = f"{to_currency}USDT"
                async with session.get(
                    f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
                    timeout=5
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        price = float(data['price'])
                        return 1 / price
            
        except:
            pass
        
        # Если Binance не сработал - используем CoinGecko
        return await self._get_crypto_rate_backup(from_currency, to_currency, from_is_crypto, to_is_crypto)
    
    async def _get_crypto_rate_backup(self, from_currency, to_currency, from_is_crypto, to_is_crypto):
        """Запасной API для крипты (CoinGecko)"""
        try:
            session = await self.get_session()
            
            if from_is_crypto and to_is_crypto:
                # крипта → крипта
                from_id = self.crypto_ids.get(from_currency)
                to_id = self.crypto_ids.get(to_currency)
                if from_id and to_id:
                    async with session.get(
                        f"https://api.coingecko.com/api/v3/simple/price?ids={from_id},{to_id}&vs_currencies=usd",
                        timeout=5
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            if from_id in data and to_id in data:
                                from_price = data[from_id]['usd']
                                to_price = data[to_id]['usd']
                                return from_price / to_price
            
            elif from_is_crypto and not to_is_crypto:
                # крипта → фиат
                from_id = self.crypto_ids.get(from_currency)
                if from_id:
                    async with session.get(
                        f"https://api.coingecko.com/api/v3/simple/price?ids={from_id}&vs_currencies=usd",
                        timeout=5
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            if from_id in data:
                                price_usd = data[from_id]['usd']
                                if to_currency == 'USD':
                                    return price_usd
                                else:
                                    usd_rate = await self._get_fiat_rate_fast('USD', to_currency)
                                    if usd_rate:
                                        return price_usd * usd_rate
            
            elif not from_is_crypto and to_is_crypto:
                # фиат → крипта
                to_id = self.crypto_ids.get(to_currency)
                if to_id:
                    async with session.get(
                        f"https://api.coingecko.com/api/v3/simple/price?ids={to_id}&vs_currencies=usd",
                        timeout=5
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            if to_id in data:
                                price_usd = data[to_id]['usd']
                                if from_currency == 'USD':
                                    return 1 / price_usd
                                else:
                                    usd_rate = await self._get_fiat_rate_fast(from_currency, 'USD')
                                    if usd_rate:
                                        return usd_rate / price_usd
        except:
            pass
        return None
    
    def _get_default_rate(self, from_currency, to_currency):
        """Примерные курсы для популярных пар"""
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
        return default_rates.get((from_currency, to_currency))

    async def get_all_rates(self, base="USD"):
        """Получаем курсы всех валют к базовой (параллельно)"""
        tasks = []
        currencies = []
        
        # Собираем все валюты (фиат + крипта)
        all_currencies = list(self.fiat_currencies.keys()) + list(self.crypto_currencies.keys())
        
        for currency in all_currencies:
            if currency != base:
                tasks.append(self.get_rate(base, currency))
                currencies.append(currency)
        
        # Запускаем все запросы параллельно
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Собираем только успешные результаты
        rates = {}
        for currency, result in zip(currencies, results):
            if isinstance(result, Decimal) and result > 0:
                rates[currency] = float(result)
        
        return rates

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Подключаем middleware
dp.message.middleware(AntiSpamMiddleware())

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
    """Клавиатура с популярными парами"""
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
    
    tasks = []
    currencies = []
    
    for code, name in currency_api.fiat_currencies.items():
        if code != 'USD':
            tasks.append(currency_api.get_rate('USD', code))
            currencies.append((code, name.split()[0]))
    
    # Параллельно получаем все курсы
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for (code, flag), result in zip(currencies, results):
        if isinstance(result, Decimal) and result > 0:
            rates_text += f"{flag} 1 USD = {float(result):.2f} {code}\n"
    
    await wait_msg.delete()
    await message.answer(rates_text, reply_markup=get_main_keyboard())


@dp.message(F.text == "₿ Криптовалюта")
async def show_crypto_prices(message: types.Message):
    """Показать цены криптовалют"""
    wait_msg = await message.answer("⏳ Получаю цены криптовалют...")
    
    prices = await currency_api.get_all_rates('USD')
    
    if prices:
        text = "<b>₿ Цены криптовалют (USD)</b>\n\n"
        crypto_prices = {k: v for k, v in prices.items() if k in currency_api.crypto_currencies}
        
        for code, price in crypto_prices.items():
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
        
        # Получаем курс (теперь из кэша, если есть)
        rate = await currency_api.get_rate(from_curr, to_curr)
        
        if rate:
            # Считаем результат
            result = amount * float(rate)
            
            # Форматируем числа
            if 'BTC' in [from_curr, to_curr] or 'ETH' in [from_curr, to_curr]:
                amount_str = f"{amount:.8f}".rstrip('0').rstrip('.')
                result_str = f"{result:.8f}".rstrip('0').rstrip('.')
                rate_str = f"{float(rate):.8f}".rstrip('0').rstrip('.')
            else:
                amount_str = f"{amount:,.2f}".replace(',', ' ')
                result_str = f"{result:,.2f}".replace(',', ' ')
                rate_str = f"{float(rate):.4f}".rstrip('0').rstrip('.')
            
            await wait_msg.delete()
            
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
    print("🚀 Ускоренный бот WORM с криптовалютой запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await currency_api.close()


if __name__ == "__main__":
    asyncio.run(main())
