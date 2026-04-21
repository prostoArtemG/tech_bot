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

router = Router()


class AddProductState(StatesGroup):
    waiting_for_category = State()
    waiting_for_brand = State()
    waiting_for_model = State()
    waiting_for_price = State()


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


menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Товары")],
        [KeyboardButton(text="🛒 Продажа")],
        [KeyboardButton(text="🧾 История продаж")],
        [KeyboardButton(text="👤 Клиенты")],
        [KeyboardButton(text="📈 Отчёты")],
        [KeyboardButton(text="💰 Прибыль")],
    ],
    resize_keyboard=True
)

products_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить товар")],
        [KeyboardButton(text="📋 Список товаров")],
        [KeyboardButton(text="✏️ Изменить остаток")],
        [KeyboardButton(text="➕ Приход")],
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


def normalize_phone(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone.strip())


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Это tech_bot 🤖",
        reply_markup=menu_kb
    )


@router.message(lambda m: m.text == "📦 Товары")
async def products_menu_handler(message: Message, state: FSMContext):
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
    await state.clear()
    await message.answer(
        "Раздел отчётов:",
        reply_markup=reports_kb
    )


@router.message(lambda m: m.text == "💰 Прибыль")
async def profit_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Раздел прибыли:",
        reply_markup=profit_kb
    )


@router.message(lambda m: m.text == "⬅️ Назад")
async def back_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Главное меню:",
        reply_markup=menu_kb
    )


@router.message(lambda m: m.text == "➕ Добавить товар")
async def add_product_start_handler(message: Message, state: FSMContext):
    await state.set_state(AddProductState.waiting_for_category)
    await message.answer("Введите категорию товара:\nНапример: Стиральная машина")


@router.message(AddProductState.waiting_for_category)
async def add_product_category_handler(message: Message, state: FSMContext):
    category = (message.text or "").strip()

    if not category:
        await message.answer("Категория не может быть пустой. Введите категорию:")
        return

    await state.update_data(category=category)
    await state.set_state(AddProductState.waiting_for_brand)
    await message.answer("Введите бренд:\nНапример: Samsung")


@router.message(AddProductState.waiting_for_brand)
async def add_product_brand_handler(message: Message, state: FSMContext):
    brand = (message.text or "").strip()

    if not brand:
        await message.answer("Бренд не может быть пустым. Введите бренд:")
        return

    await state.update_data(brand=brand)
    await state.set_state(AddProductState.waiting_for_model)
    await message.answer("Введите модель:\nНапример: WW90T554CAT")


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

    await db.add_product(category, brand, model, price)

    await state.clear()
    await message.answer(
        "✅ Товар добавлен:\n\n"
        f"Категория: {category}\n"
        f"Бренд: {brand}\n"
        f"Модель: {model}\n"
        f"Цена: {price:.2f} грн\n"
        f"Остаток: 0 шт",
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

        lines.append(
            f"{row['id']}. {category} | {brand} | {model} | {price:.2f} грн | Остаток: {stock_qty} шт"
        )

    await message.answer("\n".join(lines))


@router.message(lambda m: m.text == "✏️ Изменить остаток")
async def edit_stock_start_handler(message: Message, state: FSMContext):
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
        created_at = row["created_at"].strftime("%d.%m.%Y %H:%M") if row["created_at"] else "-"

        lines.append(
            f"#{row['id']} | {created_at}\n"
            f"{category} | {brand} | {model}\n"
            f"Клиент: {customer_name} | {customer_phone}\n"
            f"Кол-во: {qty} | Цена: {sale_price:.2f} грн | Сумма: {total_amount:.2f} грн\n"
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


@router.message(lambda m: m.text not in {
    "📦 Товары", "🛒 Продажа", "🧾 История продаж", "👤 Клиенты",
    "➕ Добавить товар", "📋 Список товаров", "✏️ Изменить остаток", "➕ Приход",
    "📋 Список клиентов", "🔍 Найти клиента", "⬅️ Назад",
    "📈 Отчёты", "📅 Отчёт за сегодня", "📆 Отчёт за месяц",
    "💰 Прибыль", "💰 Прибыль за сегодня", "💰 Прибыль за месяц",
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


if __name__ == "__main__":
    asyncio.run(main())
