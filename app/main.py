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
    waiting_for_name = State()
    waiting_for_price = State()


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
async def sale_handler(message: Message):
    await message.answer("Раздел продажи пока в разработке")


@router.message(lambda m: m.text == "➕ Добавить товар")
async def add_product_start_handler(message: Message, state: FSMContext):
    await state.set_state(AddProductState.waiting_for_name)
    await message.answer("Введите название товара:")


@router.message(AddProductState.waiting_for_name)
async def add_product_name_handler(message: Message, state: FSMContext):
    name = (message.text or "").strip()

    if not name:
        await message.answer("Название не может быть пустым. Введите название товара:")
        return

    await state.update_data(product_name=name)
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
    product_name = data["product_name"]

    await db.add_product(product_name, price)

    await state.clear()
    await message.answer(
        f"✅ Товар добавлен:\n\nНазвание: {product_name}\nЦена: {price:.2f} грн",
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
        lines.append(f"{row['id']}. {row['name']} — {float(row['price']):.2f} грн")

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
