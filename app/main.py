import asyncio
import os
import re

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

from app.db import db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в .env")

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {
    int(x.strip())
    for x in ADMIN_IDS_RAW.split(",")
    if x.strip().isdigit()
}

router = Router()


class AddProductState(StatesGroup):
    waiting_for_category = State()
    searching_category = State()
    waiting_for_brand = State()
    waiting_for_brand_manual = State()
    searching_brand = State()
    waiting_for_model = State()
    waiting_for_price = State()
    waiting_for_purchase_price = State()
    waiting_for_currency = State()
    waiting_for_sku = State()
    waiting_for_warranty = State()


class EditStockState(StatesGroup):
    waiting_for_product_id = State()
    waiting_for_new_stock = State()


class ReceiptState(StatesGroup):
    waiting_for_query = State()
    waiting_for_product_id = State()
    waiting_for_qty = State()
    waiting_for_purchase_price = State()


class SaleState(StatesGroup):
    waiting_for_query = State()
    waiting_for_product_id = State()
    waiting_for_qty = State()
    waiting_for_customer_phone = State()
    waiting_for_customer_name = State()
    waiting_for_customer_city = State()


class CancelSaleState(StatesGroup):
    waiting_for_sale_id = State()


class UserRoleState(StatesGroup):
    waiting_for_telegram_id = State()
    waiting_for_role = State()


class CurrencyRateState(StatesGroup):
    waiting_for_currency = State()
    waiting_for_rate = State()


admin_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Товары")],
        [KeyboardButton(text="🛒 Продажа")],
        [KeyboardButton(text="❌ Отмена продажи")],
        [KeyboardButton(text="🧾 История продаж")],
        [KeyboardButton(text="👤 Клиенты")],
        [KeyboardButton(text="👥 Пользователи")],
        [KeyboardButton(text="💱 Курсы валют")],
        [KeyboardButton(text="📈 Отчёты")],
        [KeyboardButton(text="💰 Прибыль")],
        [KeyboardButton(text="🌐 Язык")],
    ],
    resize_keyboard=True
)

seller_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Товары")],
        [KeyboardButton(text="🛒 Продажа")],
        [KeyboardButton(text="🧾 История продаж")],
        [KeyboardButton(text="👤 Клиенты")],
        [KeyboardButton(text="🌐 Язык")],
    ],
    resize_keyboard=True
)

# backward-compatible alias: default to seller menu
menu_kb = seller_menu_kb

products_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить товар")],
        [KeyboardButton(text="📋 Список товаров")],
        [KeyboardButton(text="✏️ Изменить остаток")],
        [KeyboardButton(text="➕ Приход")],
        [KeyboardButton(text="📥 История приходов")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


categories_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Стиральная машина"), KeyboardButton(text="Холодильник")],
        [KeyboardButton(text="Посудомоечная машина"), KeyboardButton(text="Духовой шкаф")],
        [KeyboardButton(text="Плита"), KeyboardButton(text="Вытяжка")],
        [KeyboardButton(text="Микроволновка"), KeyboardButton(text="Пылесос")],
        [KeyboardButton(text="Чайник"), KeyboardButton(text="Телевизор")],
        [KeyboardButton(text="🔍 Поиск категории")],
        [KeyboardButton(text="Другая техника")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


brands_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Samsung"), KeyboardButton(text="LG")],
        [KeyboardButton(text="Bosch"), KeyboardButton(text="Beko")],
        [KeyboardButton(text="Gorenje"), KeyboardButton(text="Electrolux")],
        [KeyboardButton(text="Philips"), KeyboardButton(text="Tefal")],
        [KeyboardButton(text="Xiaomi"), KeyboardButton(text="Dyson")],
        [KeyboardButton(text="🔍 Поиск бренда")],
        [KeyboardButton(text="Другое")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)

customers_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Список клиентов")],
        [KeyboardButton(text="🔍 Найти клиента")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


reports_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 Отчёт за сегодня")],
        [KeyboardButton(text="📆 Отчёт за месяц")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


profit_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Прибыль за сегодня")],
        [KeyboardButton(text="💰 Прибыль за месяц")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


lang_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Русский")],
        [KeyboardButton(text="Українська")],
    ],
    resize_keyboard=True
)


users_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Список пользователей")],
        [KeyboardButton(text="🔁 Изменить роль")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


roles_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="admin")],
        [KeyboardButton(text="seller")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)

currency_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="UAH"), KeyboardButton(text="USD"), KeyboardButton(text="EUR")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)

currency_rates_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="USD")],
        [KeyboardButton(text="EUR")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)
TEXTS = {
    "ru": {
        "menu": "Главное меню:",
        "no_access": "⛔ У вас нет доступа",
    },
    "uk": {
        "menu": "Головне меню:",
        "no_access": "⛔ У вас немає доступу",
    }
}


async def t(message: Message, key: str) -> str:
    user = await db.get_user_by_telegram_id(message.from_user.id)
    lang = user["language"] if user and user["language"] else "ru"
    return TEXTS.get(lang, TEXTS["ru"]).get(key, key)


@router.message(lambda m: m.text == "🌐 Язык")
async def choose_language_handler(message: Message, state: FSMContext):
    await state.set_state("choosing_language")
    await message.answer("Выберите язык / Оберіть мову:", reply_markup=lang_kb)


@router.message(lambda m: m.text in ["Русский", "Українська"])
async def set_language_handler(message: Message, state: FSMContext):
    lang = "ru" if message.text == "Русский" else "uk"

    await db.update_user_language(message.from_user.id, lang)

    await state.clear()

    menu = await get_main_menu_for_user(message)

    await message.answer(
        "Язык обновлён / Мову змінено",
        reply_markup=menu
    )


def normalize_phone(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone.strip())


def is_system_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


async def get_current_user_role(message: Message) -> str:
    if is_system_admin(message.from_user.id):
        return "admin"

    user = await db.get_user_by_telegram_id(message.from_user.id)

    if not user:
        await db.create_user_if_not_exists(
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name
        )
        return "seller"

    return user["role"] or "seller"


async def get_main_menu_for_user(message: Message):
    role = await get_current_user_role(message)
    return admin_menu_kb if role == "admin" else seller_menu_kb


async def require_admin(message: Message) -> bool:
    role = await get_current_user_role(message)
    if role != "admin":
        await message.answer("⛔ У вас нет доступа к этому разделу.")
        return False
    return True


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    await db.create_user_if_not_exists(
        telegram_id=message.from_user.id,
        full_name=message.from_user.full_name
    )

    if is_system_admin(message.from_user.id):
        await db.update_user_role(message.from_user.id, "admin")

    menu = await get_main_menu_for_user(message)

    await message.answer(
        "Привет! Это tech_bot 🤖",
        reply_markup=menu
    )



@router.message(lambda m: m.text == "📦 Товары")
async def products_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.clear()
    await message.answer(
        "Раздел товаров:",
        reply_markup=products_kb
    )


@router.message(lambda m: m.text == "👤 Клиенты")
async def customers_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Раздел клиентов:",
        reply_markup=customers_kb
    )

@router.message(lambda m: m.text == "📈 Отчёты")
async def reports_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.clear()
    await message.answer(
        "Раздел отчётов:",
        reply_markup=reports_kb
    )


@router.message(lambda m: m.text == "💰 Прибыль")
async def profit_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.clear()
    await message.answer(
        "Раздел прибыли:",
        reply_markup=profit_kb
    )


@router.message(lambda m: m.text == "⬅️ Назад")
async def back_handler(message: Message, state: FSMContext):
    await state.clear()
    menu = await get_main_menu_for_user(message)
    await message.answer(
        "Главное меню:",
        reply_markup=menu
    )


@router.message(lambda m: m.text == "➕ Добавить товар")
async def add_product_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.set_state(AddProductState.waiting_for_category)

    await message.answer(
        "Выберите категорию:",
        reply_markup=categories_kb
    )


@router.message(AddProductState.waiting_for_category)
async def add_product_category_handler(message: Message, state: FSMContext):
    category = (message.text or "").strip()

    if category == "🔍 Поиск категории":
        await state.set_state(AddProductState.searching_category)
        await message.answer("Введите часть названия категории:")
        return

    if category == "⬅️ Назад":
        await state.clear()
        menu = await get_main_menu_for_user(message)
        await message.answer("Главное меню:", reply_markup=menu)
        return

    await state.update_data(category=category)
    await state.set_state(AddProductState.waiting_for_brand)

    await message.answer(
        "Выберите бренд:",
        reply_markup=brands_kb
    )


@router.message(AddProductState.searching_category)
async def search_category_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()

    categories = [
        "Телевизоры", "Холодильники", "Стиральные машины", "Смартфоны", "Ноутбуки",
        "Пылесосы", "Микроволновки", "Плиты", "Утюги", "Кофемашины"
    ]

    found = [c for c in categories if query in c.lower()]

    if not found:
        await message.answer("Ничего не найдено. Попробуйте ещё:")
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=c)] for c in found] + [[KeyboardButton(text="⬅️ Назад")]],
        resize_keyboard=True
    )

    await state.set_state(AddProductState.waiting_for_category)
    await message.answer("Выберите категорию:", reply_markup=keyboard)


@router.message(AddProductState.waiting_for_brand)
async def add_product_brand_handler(message: Message, state: FSMContext):
    brand = (message.text or "").strip()

    if brand == "🔍 Поиск бренда":
        await state.set_state(AddProductState.searching_brand)
        await message.answer("Введите часть названия бренда:")
        return

    if brand == "⬅️ Назад":
        await state.set_state(AddProductState.waiting_for_category)
        await message.answer("Выберите категорию:", reply_markup=categories_kb)
        return

    if brand == "Другое":
        await state.set_state(AddProductState.waiting_for_brand_manual)
        await message.answer("Введите бренд вручную:")
        return

    await state.update_data(brand=brand)
    await state.set_state(AddProductState.waiting_for_model)

    await message.answer("Введите модель:")


@router.message(AddProductState.waiting_for_brand_manual)
async def add_product_brand_manual_handler(message: Message, state: FSMContext):
    brand = (message.text or "").strip()

    if not brand:
        await message.answer("Бренд не может быть пустым. Введите ещё раз:")
        return

    await state.update_data(brand=brand)
    await state.set_state(AddProductState.waiting_for_model)

    await message.answer("Введите модель:")


@router.message(AddProductState.searching_brand)
async def search_brand_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()

    brands = [
        "Samsung", "LG", "Bosch", "Beko", "Gorenje",
        "Electrolux", "Philips", "Tefal", "Xiaomi",
        "Dyson", "Braun", "Rowenta", "Zelmer"
    ]

    found = [b for b in brands if query in b.lower()]

    if not found:
        await message.answer("Ничего не найдено. Попробуй ещё:")
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=b)] for b in found] + [[KeyboardButton(text="⬅️ Назад")]],
        resize_keyboard=True
    )

    await state.set_state(AddProductState.waiting_for_brand)
    await message.answer("Выбери бренд:", reply_markup=keyboard)


@router.message(AddProductState.waiting_for_model)
async def add_product_model_handler(message: Message, state: FSMContext):
    model = (message.text or "").strip()

    if not model:
        await message.answer("Модель не может быть пустой. Введите модель:")
        return

    await state.update_data(model=model)
    await state.set_state(AddProductState.waiting_for_price)
    await message.answer("Введите цену товара, например: 18000")


@router.message(AddProductState.waiting_for_price)
async def add_product_price_handler(message: Message, state: FSMContext):
    raw_price = (message.text or "").strip().replace(",", ".")

    try:
        price = float(raw_price)
    except ValueError:
        await message.answer("Цена должна быть числом. Например: 18000")
        return

    if price < 0:
        await message.answer("Цена не может быть отрицательной. Введите цену заново:")
        return

    data = await state.get_data()
    category = data["category"]
    brand = data["brand"]
    model = data["model"]

    await state.update_data(price=price)

    await state.set_state(AddProductState.waiting_for_purchase_price)
    await message.answer("Введите закупочную цену (или 0):")


@router.message(AddProductState.waiting_for_purchase_price)
async def add_product_purchase_handler(message: Message, state: FSMContext):
    raw = (message.text or "").replace(",", ".")

    try:
        purchase_price = float(raw)
    except:
        await message.answer("Введите число")
        return

    await state.update_data(purchase_price=purchase_price)

    await state.set_state(AddProductState.waiting_for_currency)
    await message.answer("Выберите валюту закупки:", reply_markup=currency_kb)


@router.message(AddProductState.waiting_for_currency)
async def add_product_currency_handler(message: Message, state: FSMContext):
    currency = message.text

    if currency not in ["UAH", "USD", "EUR"]:
        await message.answer("Выберите валюту кнопкой")
        return

    await state.update_data(currency=currency)

    await state.set_state(AddProductState.waiting_for_sku)
    await message.answer("Введите артикул (или -):")


@router.message(AddProductState.waiting_for_sku)
async def add_product_sku_handler(message: Message, state: FSMContext):
    sku = (message.text or "").strip()
    if sku == "-":
        sku = None

    await state.update_data(sku=sku)

    await state.set_state(AddProductState.waiting_for_warranty)
    await message.answer("Введите гарантию (в месяцах, например 12):")


@router.message(AddProductState.waiting_for_warranty)
async def add_product_warranty_handler(message: Message, state: FSMContext):
    raw = (message.text or "").strip()

    if not raw.isdigit():
        await message.answer("Введите число месяцев")
        return

    warranty = int(raw)

    data = await state.get_data()

    await db.add_product(
        category=data["category"],
        brand=data["brand"],
        model=data["model"],
        price=data["price"],
        purchase_price=data.get("purchase_price", 0),
        purchase_currency=data.get("currency", "UAH"),
        sku=data.get("sku"),
        warranty_months=warranty,
    )

    await state.clear()

    await message.answer(
        f"✅ Товар добавлен\n\n"
        f"{data['brand']} {data['model']}\n"
        f"Цена: {data['price']} грн\n"
        f"Закупка: {data.get('purchase_price', 0)} {data.get('currency', 'UAH')}\n"
        f"Гарантия: {warranty} мес",
        reply_markup=products_kb
    )


@router.message(lambda m: m.text == "📋 Список товаров")
async def list_products_handler(message: Message):
    rows = await db.list_products()

    if not rows:
        await message.answer("Список товаров пока пуст.")
        return

    lines = ["📦 Список товаров:\n"]
    for row in rows:
        category = row["category"] or "-"
        brand = row["brand"] or "-"
        model = row["model"] or "-"
        price = float(row["price"])
        stock_qty = row["stock_qty"] or 0
        purchase_price = float(row["purchase_price"] or 0)
        purchase_currency = row["purchase_currency"] or "UAH"
        sku = row["sku"] or "-"
        warranty_months = row["warranty_months"] or 0

        lines.append(
            f"{row['id']}. {category} | {brand} | {model}\n"
            f"Цена: {price:.2f} грн\n"
            f"Закупка: {purchase_price:.2f} {purchase_currency}\n"
            f"Артикул: {sku}\n"
            f"Гарантия: {warranty_months} мес\n"
            f"Остаток: {stock_qty} шт\n"
        )

    await message.answer("\n".join(lines))

@router.message(lambda m: m.text == "❌ Отмена продажи")
async def cancel_sale_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    rows = await db.list_recent_sales(limit=10)

    if not rows:
        await message.answer("История продаж пока пустая.")
        return

    lines = ["❌ Последние продажи для отмены:\n"]

    for row in rows:
        status = row.get("status", "completed")
        status_text = "✅ completed" if status == "completed" else "❌ cancelled"
        created_at = row["created_at"].strftime("%d.%m.%Y %H:%M") if row["created_at"] else "-"
        brand = row["brand"] or "-"
        model = row["model"] or "-"
        qty = row["qty"] or 0
        total_amount = float(row["total_amount"] or 0)

        lines.append(
            f"#{row['id']} | {created_at} | {status_text}\n"
            f"{brand} {model} | Кол-во: {qty} | Сумма: {total_amount:.2f} грн\n"
        )

    await state.set_state(CancelSaleState.waiting_for_sale_id)
    await message.answer("\n".join(lines) + "\nВведите ID продажи для отмены:")


@router.message(CancelSaleState.waiting_for_sale_id)
async def cancel_sale_id_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("ID продажи должен быть числом. Введите корректный ID:")
        return

    sale_id = int(raw_id)
    sale = await db.get_sale_by_id(sale_id)

    if not sale:
        await message.answer("Продажа не найдена. Введите корректный ID:")
        return

    if sale["status"] == "cancelled":
        await message.answer("Эта продажа уже отменена.")
        return

    product = await db.get_product_by_id(sale["product_id"])
    if not product:
        await state.clear()
        await message.answer("Товар по этой продаже не найден.", reply_markup=menu_kb)
        return

    new_stock = (product["stock_qty"] or 0) + (sale["qty"] or 0)
    await db.update_stock_qty(sale["product_id"], new_stock)
    await db.cancel_sale(sale_id)

    await state.clear()
    await message.answer(
        "✅ Продажа отменена\n\n"
        f"Продажа ID: {sale['id']}\n"
        f"Товар: {sale['brand'] or '-'} {sale['model'] or '-'}\n"
        f"Возвращено на склад: {sale['qty']} шт\n"
        f"Новый остаток: {new_stock} шт",
        reply_markup=menu_kb
    )


@router.message(lambda m: m.text == "✏️ Изменить остаток")
async def edit_stock_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.set_state(EditStockState.waiting_for_product_id)
    await message.answer("Введите ID товара, у которого хотите изменить остаток:")


@router.message(EditStockState.waiting_for_product_id)
async def edit_stock_product_id_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("ID товара должен быть числом. Введите ID:")
        return

    product_id = int(raw_id)
    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer("Товар с таким ID не найден. Введите корректный ID:")
        return

    await state.update_data(product_id=product_id)

    category = product["category"] or "-"
    brand = product["brand"] or "-"
    model = product["model"] or "-"
    stock_qty = product["stock_qty"] or 0

    await state.set_state(EditStockState.waiting_for_new_stock)
    await message.answer(
        "Текущий товар:\n"
        f"{product['id']}. {category} | {brand} | {model}\n"
        f"Текущий остаток: {stock_qty} шт\n\n"
        "Введите новый остаток:"
    )


@router.message(EditStockState.waiting_for_new_stock)
async def edit_stock_new_stock_handler(message: Message, state: FSMContext):
    raw_stock = (message.text or "").strip()

    if not raw_stock.isdigit():
        await message.answer("Остаток должен быть целым числом 0 или больше. Введите заново:")
        return

    new_stock = int(raw_stock)

    data = await state.get_data()
    product_id = data["product_id"]

    await db.update_stock_qty(product_id, new_stock)
    product = await db.get_product_by_id(product_id)

    await state.clear()
    await message.answer(
        "✅ Остаток обновлён:\n\n"
        f"{product['id']}. {product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"Новый остаток: {product['stock_qty']} шт",
        reply_markup=products_kb
    )


@router.message(lambda m: m.text == "➕ Приход")
async def receipt_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.set_state(ReceiptState.waiting_for_query)
    await message.answer("Введите бренд, модель или категорию товара для прихода:")


@router.message(ReceiptState.waiting_for_query)
async def receipt_search_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip()

    rows = await db.search_products(query)

    if not rows:
        await message.answer("Ничего не найдено. Попробуй ещё:")
        return

    lines = ["Найдено:\n"]
    for row in rows:
        lines.append(
            f"{row['id']}. {row['category'] or '-'} | {row['brand'] or '-'} | {row['model'] or '-'} | "
            f"Цена: {float(row['price']):.2f} грн | Остаток: {row['stock_qty']} шт"
        )

    await state.set_state(ReceiptState.waiting_for_product_id)
    await message.answer("\n".join(lines) + "\n\nВведите ID товара:")


@router.message(ReceiptState.waiting_for_product_id)
async def receipt_product_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("Введите корректный ID товара")
        return

    product_id = int(raw_id)
    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer("Товар не найден")
        return

    await state.update_data(product_id=product_id)
    await state.set_state(ReceiptState.waiting_for_qty)

    await message.answer(
        f"Товар:\n{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"Текущий остаток: {product['stock_qty']} шт\n\n"
        "Введите количество для прихода:"
    )


@router.message(ReceiptState.waiting_for_qty)
async def receipt_qty_handler(message: Message, state: FSMContext):
    raw_qty = (message.text or "").strip()

    if not raw_qty.isdigit():
        await message.answer("Введите корректное количество")
        return

    qty = int(raw_qty)
    if qty <= 0:
        await message.answer("Количество должно быть больше 0")
        return

    await state.update_data(qty=qty)
    await state.set_state(ReceiptState.waiting_for_purchase_price)
    await message.answer("Введите закупочную цену за 1 шт, например: 15000")


@router.message(ReceiptState.waiting_for_purchase_price)
async def receipt_purchase_price_handler(message: Message, state: FSMContext):
    raw_price = (message.text or "").strip().replace(",", ".")

    try:
        purchase_price = float(raw_price)
    except ValueError:
        await message.answer("Закупочная цена должна быть числом. Например: 15000")
        return

    if purchase_price < 0:
        await message.answer("Закупочная цена не может быть отрицательной. Введите заново:")
        return

    data = await state.get_data()
    product_id = data["product_id"]
    qty = data["qty"]

    product = await db.get_product_by_id(product_id)
    if not product:
        await state.clear()
        await message.answer("Товар не найден", reply_markup=menu_kb)
        return

    total_amount = await db.create_purchase(product_id, qty, purchase_price)

    new_stock = (product["stock_qty"] or 0) + qty
    await db.update_stock_qty(product_id, new_stock)

    await state.clear()
    await message.answer(
        "✅ Приход сохранён\n\n"
        f"Товар: {product['brand'] or '-'} {product['model'] or '-'}\n"
        f"Количество: {qty} шт\n"
        f"Закупочная цена: {purchase_price:.2f} грн\n"
        f"Сумма прихода: {total_amount:.2f} грн\n"
        f"Новый остаток: {new_stock} шт",
        reply_markup=products_kb
    )


@router.message(lambda m: m.text == "🛒 Продажа")
async def sale_start_handler(message: Message, state: FSMContext):
    await state.set_state(SaleState.waiting_for_query)
    await message.answer("Введите бренд, модель или категорию товара:")


@router.message(SaleState.waiting_for_query)
async def sale_search_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip()

    rows = await db.search_products(query)

    if not rows:
        await message.answer("Ничего не найдено. Попробуй ещё:")
        return

    lines = ["Найдено:\n"]

    for row in rows:
        lines.append(
            f"{row['id']}. {row['category'] or '-'} | {row['brand'] or '-'} | {row['model'] or '-'} | "
            f"{float(row['price']):.2f} грн | Остаток: {row['stock_qty']}"
        )

    await state.set_state(SaleState.waiting_for_product_id)
    await message.answer("\n".join(lines) + "\n\nВведите ID товара:")


@router.message(SaleState.waiting_for_product_id)
async def sale_product_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("Введите корректный ID товара")
        return

    product_id = int(raw_id)
    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer("Товар не найден")
        return

    await state.update_data(product_id=product_id)
    await state.set_state(SaleState.waiting_for_qty)

    await message.answer(
        f"Товар:\n{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"Цена: {float(product['price']):.2f} грн\n"
        f"Остаток: {product['stock_qty']}\n\n"
        "Введите количество:"
    )


@router.message(SaleState.waiting_for_qty)
async def sale_qty_handler(message: Message, state: FSMContext):
    raw_qty = (message.text or "").strip()

    if not raw_qty.isdigit():
        await message.answer("Введите корректное количество")
        return

    qty = int(raw_qty)

    if qty <= 0:
        await message.answer("Количество должно быть больше 0")
        return

    data = await state.get_data()
    product_id = data["product_id"]
    product = await db.get_product_by_id(product_id)

    if not product:
        await state.clear()
        await message.answer("Товар не найден", reply_markup=menu_kb)
        return

    if qty > product["stock_qty"]:
        await message.answer("❌ Недостаточно товара на складе")
        return

    await state.update_data(qty=qty)
    await state.set_state(SaleState.waiting_for_customer_phone)
    await message.answer("Введите телефон клиента:")


@router.message(SaleState.waiting_for_customer_phone)
async def sale_customer_phone_handler(message: Message, state: FSMContext):
    raw_phone = (message.text or "").strip()
    phone = normalize_phone(raw_phone)

    if len(phone) < 8:
        await message.answer("Введите корректный телефон клиента:")
        return

    customer = await db.get_customer_by_phone(phone)

    if customer:
        data = await state.get_data()
        product_id = data["product_id"]
        qty = data["qty"]

        product = await db.get_product_by_id(product_id)
        if not product:
            await state.clear()
            await message.answer("Товар не найден", reply_markup=menu_kb)
            return

        price = float(product["price"])
        total = await db.create_sale(product_id, qty, price, customer["id"])

        new_stock = product["stock_qty"] - qty
        await db.update_stock_qty(product_id, new_stock)

        await state.clear()
        await message.answer(
            "✅ Продажа завершена\n\n"
            f"Клиент: {customer['name']} | {customer['phone']} | {customer['city'] or '-'}\n"
            f"Товар: {product['brand'] or '-'} {product['model'] or '-'}\n"
            f"Количество: {qty}\n"
            f"Сумма: {total:.2f} грн\n"
            f"Остаток: {new_stock} шт",
            reply_markup=menu_kb
        )
        return

    await state.update_data(customer_phone=phone)
    await state.set_state(SaleState.waiting_for_customer_name)
    await message.answer("Клиент не найден.\nВведите имя клиента:")


@router.message(SaleState.waiting_for_customer_name)
async def sale_customer_name_handler(message: Message, state: FSMContext):
    name = (message.text or "").strip()

    if not name:
        await message.answer("Имя не может быть пустым. Введите имя клиента:")
        return

    await state.update_data(customer_name=name)
    await state.set_state(SaleState.waiting_for_customer_city)
    await message.answer("Введите город клиента:")


@router.message(SaleState.waiting_for_customer_city)
async def sale_customer_city_handler(message: Message, state: FSMContext):
    city = (message.text or "").strip()

    if not city:
        await message.answer("Город не может быть пустым. Введите город клиента:")
        return

    data = await state.get_data()

    customer = await db.create_customer(
        name=data["customer_name"],
        phone=data["customer_phone"],
        city=city
    )

    product_id = data["product_id"]
    qty = data["qty"]

    product = await db.get_product_by_id(product_id)
    if not product:
        await state.clear()
        await message.answer("Товар не найден", reply_markup=menu_kb)
        return

    price = float(product["price"])
    total = await db.create_sale(product_id, qty, price, customer["id"])

    new_stock = product["stock_qty"] - qty
    await db.update_stock_qty(product_id, new_stock)

    await state.clear()
    await message.answer(
        "✅ Продажа завершена\n\n"
        f"Клиент: {customer['name']} | {customer['phone']} | {customer['city'] or '-'}\n"
        f"Товар: {product['brand'] or '-'} {product['model'] or '-'}\n"
        f"Количество: {qty}\n"
        f"Сумма: {total:.2f} грн\n"
        f"Остаток: {new_stock} шт",
        reply_markup=menu_kb
    )


@router.message(lambda m: m.text == "📋 Список клиентов")
async def list_customers_handler(message: Message):
    rows = await db.list_customers()

    if not rows:
        await message.answer("Список клиентов пока пуст.")
        return

    lines = ["👤 Список клиентов:\n"]
    for row in rows:
        lines.append(
            f"{row['id']}. {row['name']} | {row['phone']} | {row['city'] or '-'}"
        )

    await message.answer("\n".join(lines))


@router.message(lambda m: m.text == "🧾 История продаж")
async def sales_history_handler(message: Message):
    rows = await db.list_recent_sales()

    if not rows:
        await message.answer("История продаж пока пустая.")
        return

    lines = ["🧾 Последние продажи:\n"]

    for row in rows:
        category = row["category"] or "-"
        brand = row["brand"] or "-"
        model = row["model"] or "-"
        customer_name = row["customer_name"] or "Без имени"
        customer_phone = row["customer_phone"] or "-"
        qty = row["qty"] or 0
        sale_price = float(row["sale_price"] or 0)
        total_amount = float(row["total_amount"] or 0)
        cost_total = float(row["cost_total_uah"] or 0)
        profit = float(row["profit_uah"] or 0)
        created_at = row["created_at"].strftime("%d.%m.%Y %H:%M") if row["created_at"] else "-"
        status = row.get("status", "completed")
        status_text = "✅ completed" if status == "completed" else "❌ cancelled"

        lines.append(
            f"#{row['id']} | {created_at} | {status_text}\n"
            f"{category} | {brand} | {model}\n"
            f"Клиент: {customer_name} | {customer_phone}\n"
            f"Кол-во: {qty} | Цена: {sale_price:.2f} грн | Сумма: {total_amount:.2f} грн\n"
            f"Себестоимость: {cost_total:.2f} грн | Прибыль: {profit:.2f} грн\n"
        )

    await message.answer("\n".join(lines))


@router.message(lambda m: m.text == "📅 Отчёт за сегодня")
async def today_report_handler(message: Message):
    sales_stats = await db.get_today_sales_stats()
    purchase_stats = await db.get_today_purchases_stats()

    sales_count = int(sales_stats["sales_count"] or 0)
    sold_qty = int(sales_stats["total_qty"] or 0)
    revenue = float(sales_stats["revenue"] or 0)

    purchases_count = int(purchase_stats["purchases_count"] or 0)
    purchased_qty = int(purchase_stats["total_qty"] or 0)
    total_cost = float(purchase_stats["total_cost"] or 0)

    text = (
        "📈 Отчёт за сегодня\n\n"
        f"Продаж: {sales_count}\n"
        f"Продано единиц: {sold_qty}\n"
        f"Выручка: {revenue:.2f} грн\n\n"
        f"Приходов: {purchases_count}\n"
        f"Принято единиц: {purchased_qty}\n"
        f"Сумма закупок: {total_cost:.2f} грн"
    )

    await message.answer(text, reply_markup=reports_kb)


@router.message(lambda m: m.text == "📆 Отчёт за месяц")
async def month_report_handler(message: Message):
    sales_stats = await db.get_month_sales_stats()
    purchase_stats = await db.get_month_purchases_stats()

    sales_count = int(sales_stats["sales_count"] or 0)
    sold_qty = int(sales_stats["total_qty"] or 0)
    revenue = float(sales_stats["revenue"] or 0)

    purchases_count = int(purchase_stats["purchases_count"] or 0)
    purchased_qty = int(purchase_stats["total_qty"] or 0)
    total_cost = float(purchase_stats["total_cost"] or 0)

    text = (
        "📆 Отчёт за месяц\n\n"
        f"Продаж: {sales_count}\n"
        f"Продано единиц: {sold_qty}\n"
        f"Выручка: {revenue:.2f} грн\n\n"
        f"Приходов: {purchases_count}\n"
        f"Принято единиц: {purchased_qty}\n"
        f"Сумма закупок: {total_cost:.2f} грн"
    )

    await message.answer(text, reply_markup=reports_kb)


@router.message(lambda m: m.text == "🔍 Найти клиента")
async def find_customer_hint_handler(message: Message):
    await message.answer("Напиши часть имени, телефона или города, и я подскажу совпадения.\n\nПример: Иван или 099")


@router.message(lambda m: m.text == "💰 Прибыль")
async def profit_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Раздел прибыли:",
        reply_markup=profit_kb
    )


@router.message(lambda m: m.text == "💰 Прибыль за сегодня")
async def today_profit_handler(message: Message):
    stats = await db.get_today_profit_stats()

    revenue = float(stats["revenue"] or 0)
    cost = float(stats["cost"] or 0)
    profit = float(stats["profit"] or 0)

    text = (
        "💰 Прибыль за сегодня\n\n"
        f"Выручка: {revenue:.2f} грн\n"
        f"Закупки: {cost:.2f} грн\n"
        f"Прибыль: {profit:.2f} грн"
    )

    await message.answer(text, reply_markup=profit_kb)


@router.message(lambda m: m.text == "💰 Прибыль за месяц")
async def month_profit_handler(message: Message):
    stats = await db.get_month_profit_stats()

    revenue = float(stats["revenue"] or 0)
    cost = float(stats["cost"] or 0)
    profit = float(stats["profit"] or 0)

    text = (
        "💰 Прибыль за месяц\n\n"
        f"Выручка: {revenue:.2f} грн\n"
        f"Закупки: {cost:.2f} грн\n"
        f"Прибыль: {profit:.2f} грн"
    )

    await message.answer(text, reply_markup=profit_kb)


@router.message(lambda m: m.text == "👥 Пользователи")
async def users_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.clear()
    await message.answer(
        "Раздел пользователей:",
        reply_markup=users_kb
    )


@router.message(lambda m: m.text == "💱 Курсы валют")
async def currency_rates_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    rates = await db.get_currency_rates()

    await state.set_state(CurrencyRateState.waiting_for_currency)

    await message.answer(
        "💱 Курсы валют\n\n"
        f"USD: {rates['USD']:.2f} грн\n"
        f"EUR: {rates['EUR']:.2f} грн\n\n"
        "Выберите валюту для изменения:",
        reply_markup=currency_rates_kb
    )


@router.message(CurrencyRateState.waiting_for_currency)
async def currency_rate_choose_handler(message: Message, state: FSMContext):
    currency = (message.text or "").strip().upper()

    if currency == "⬅️ НАЗАД":
        await state.clear()
        menu = await get_main_menu_for_user(message)
        await message.answer("Главное меню:", reply_markup=menu)
        return

    if currency not in {"USD", "EUR"}:
        await message.answer("Выберите валюту кнопкой: USD или EUR")
        return

    await state.update_data(currency=currency)
    await state.set_state(CurrencyRateState.waiting_for_rate)

    await message.answer(f"Введите новый курс {currency} к гривне:")


@router.message(CurrencyRateState.waiting_for_rate)
async def currency_rate_save_handler(message: Message, state: FSMContext):
    raw_rate = (message.text or "").strip().replace(",", ".")

    try:
        rate = float(raw_rate)
    except ValueError:
        await message.answer("Курс должен быть числом. Например: 40.5")
        return

    if rate <= 0:
        await message.answer("Курс должен быть больше 0.")
        return

    data = await state.get_data()
    currency = data["currency"]

    key = "usd_rate" if currency == "USD" else "eur_rate"

    await db.set_setting(key, str(rate))

    rates = await db.get_currency_rates()

    await state.clear()
    await message.answer(
        "✅ Курс обновлён\n\n"
        f"USD: {rates['USD']:.2f} грн\n"
        f"EUR: {rates['EUR']:.2f} грн",
        reply_markup=currency_rates_kb
    )

@router.message(lambda m: m.text == "📋 Список пользователей")
async def list_users_handler(message: Message):
    if not await require_admin(message):
        return

    rows = await db.list_users()

    if not rows:
        await message.answer("Пользователей пока нет.")
        return

    lines = ["👥 Пользователи:\n"]

    for row in rows:
        lines.append(
            f"ID: {row['id']}\n"
            f"Telegram ID: {row['telegram_id']}\n"
            f"Имя: {row['full_name'] or '-'}\n"
            f"Роль: {row['role']}\n"
        )

    await message.answer("\n".join(lines), reply_markup=users_kb)

@router.message(lambda m: m.text == "🔁 Изменить роль")
async def change_role_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.set_state(UserRoleState.waiting_for_telegram_id)
    await message.answer(
        "Введите Telegram ID пользователя, которому нужно изменить роль:",
        reply_markup=users_kb
    )


@router.message(UserRoleState.waiting_for_telegram_id)
async def change_role_telegram_id_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if raw_id == "⬅️ Назад":
        await state.clear()
        await message.answer("Раздел пользователей:", reply_markup=users_kb)
        return

    if not raw_id.isdigit():
        await message.answer("Telegram ID должен быть числом. Введите ещё раз:")
        return

    telegram_id = int(raw_id)
    user = await db.get_user_by_telegram_id(telegram_id)

    if not user:
        await message.answer("Пользователь с таким Telegram ID не найден.")
        return

    await state.update_data(target_telegram_id=telegram_id)
    await state.set_state(UserRoleState.waiting_for_role)

    await message.answer(
        f"Пользователь: {user['full_name'] or '-'}\n"
        f"Текущая роль: {user['role']}\n\n"
        "Выберите новую роль:",
        reply_markup=roles_kb
    )

@router.message(UserRoleState.waiting_for_role)
async def change_role_finish_handler(message: Message, state: FSMContext):
    role = (message.text or "").strip()

    if role == "⬅️ Назад":
        await state.clear()
        await message.answer("Раздел пользователей:", reply_markup=users_kb)
        return

    if role not in {"admin", "seller"}:
        await message.answer("Выберите роль кнопкой: admin или seller")
        return

    data = await state.get_data()
    telegram_id = data["target_telegram_id"]

    await db.update_user_role(telegram_id, role)

    await state.clear()
    await message.answer(
        f"✅ Роль обновлена\n\n"
        f"Telegram ID: {telegram_id}\n"
        f"Новая роль: {role}",
        reply_markup=users_kb
    )


@router.message(lambda m: m.text not in {
    "📦 Товары", "🛒 Продажа", "❌ Отмена продажи", "🧾 История продаж", "👤 Клиенты",
    "👥 Пользователи", "📋 Список пользователей", "🔁 Изменить роль",
    "➕ Добавить товар", "📋 Список товаров", "✏️ Изменить остаток", "➕ Приход",
    "📋 Список клиентов", "🔍 Найти клиента", "📥 История приходов", "⬅️ Назад",
    "📈 Отчёты", "📅 Отчёт за сегодня", "📆 Отчёт за месяц",
    "💰 Прибыль", "💰 Прибыль за сегодня", "💰 Прибыль за месяц",
    "💱 Курсы валют", "USD", "EUR",
    "admin", "seller",
})
async def free_customer_search_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    text = (message.text or "").strip()
    if len(text) < 2:
        return

    rows = await db.search_customers(text)
    if not rows:
        return

    lines = ["🔍 Найдены клиенты:\n"]
    for row in rows:
        lines.append(
            f"{row['id']}. {row['name']} | {row['phone']} | {row['city'] or '-'}"
        )

    await message.answer("\n".join(lines))


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await db.connect()
    await db.init_schema()

    print("Бот запущен 🚀")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await db.close()


@router.message(lambda m: m.text == "📥 История приходов")
async def purchases_history_handler(message: Message):
    if not await require_admin(message):
        return

    rows = await db.list_recent_purchases()

    if not rows:
        await message.answer("История приходов пока пустая.")
        return

    lines = ["📥 Последние приходы:\n"]

    for row in rows:
        category = row["category"] or "-"
        brand = row["brand"] or "-"
        model = row["model"] or "-"
        qty = row["qty"] or 0
        purchase_price = float(row["purchase_price"] or 0)
        total_amount = float(row["total_amount"] or 0)
        created_at = row["created_at"].strftime("%d.%m.%Y %H:%M") if row["created_at"] else "-"

        lines.append(
            f"#{row['id']} | {created_at}\n"
            f"{category} | {brand} | {model}\n"
            f"Кол-во: {qty} шт\n"
            f"Закупка: {purchase_price:.2f}\n"
            f"Сумма: {total_amount:.2f}\n"
        )

    await message.answer("\n".join(lines), reply_markup=products_kb)


if __name__ == "__main__":
    asyncio.run(main())
