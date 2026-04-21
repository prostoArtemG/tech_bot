import asyncio
import os

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


class SaleState(StatesGroup):
    waiting_for_query = State()
    waiting_for_product_id = State()
    waiting_for_qty = State()


menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Товары")],
        [KeyboardButton(text="🛒 Продажа")],
    ],
    resize_keyboard=True
)

products_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить товар")],
        [KeyboardButton(text="📋 Список товаров")],
        [KeyboardButton(text="✏️ Изменить остаток")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


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


@router.message(lambda m: m.text == "⬅️ Назад")
async def back_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Главное меню:",
        reply_markup=menu_kb
    )


@router.message(lambda m: m.text == "🛒 Продажа")
async def sale_start_handler(message: Message, state: FSMContext):
    await state.set_state(SaleState.waiting_for_query)
    await message.answer("Введите бренд или модель товара:")


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
            f"{row['id']}. {row['category']} | {row['brand']} | {row['model']} | "
            f"{float(row['price']):.2f} грн | Остаток: {row['stock_qty']}"
        )

    await state.set_state(SaleState.waiting_for_product_id)
    await message.answer("\n".join(lines) + "\n\nВведите ID товара:")


@router.message(SaleState.waiting_for_product_id)
async def sale_product_handler(message: Message, state: FSMContext):
    if not (message.text or "").isdigit():
        await message.answer("Введите корректный ID товара")
        return

    product_id = int(message.text)

    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer("Товар не найден")
        return

    await state.update_data(product_id=product_id)

    await state.set_state(SaleState.waiting_for_qty)

    await message.answer(
        f"Товар:\n{product['category']} | {product['brand']} | {product['model']}\n"
        f"Цена: {float(product['price']):.2f} грн\n"
        f"Остаток: {product['stock_qty']}\n\n"
        "Введите количество:"
    )


@router.message(SaleState.waiting_for_qty)
async def sale_qty_handler(message: Message, state: FSMContext):
    if not (message.text or "").isdigit():
        await message.answer("Введите корректное количество")
        return

    qty = int(message.text)

    data = await state.get_data()
    product_id = data["product_id"]

    product = await db.get_product_by_id(product_id)

    if qty > product["stock_qty"]:
        await message.answer("❌ Недостаточно товара на складе")
        return

    price = float(product["price"])
    total = await db.create_sale(product_id, qty, price)

    new_stock = product["stock_qty"] - qty
    await db.update_stock_qty(product_id, new_stock)

    await state.clear()

    await message.answer(
        "✅ Продажа завершена\n\n"
        f"{product['brand']} {product['model']}\n"
        f"Количество: {qty}\n"
        f"Сумма: {total:.2f} грн\n"
        f"Остаток: {new_stock} шт"
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
