import asyncio
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в .env")

router = Router()

# ===== КНОПКИ =====
menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Товары")],
        [KeyboardButton(text="🛒 Продажа")],
    ],
    resize_keyboard=True
)

# ===== КОМАНДА /start =====
@router.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(
        "Привет! Это tech_bot 🤖",
        reply_markup=menu_kb
    )

# ===== КНОПКА =====
@router.message(lambda m: m.text == "📦 Товары")
async def products_handler(message: Message):
    await message.answer("Список товаров пока пуст")

# ===== КНОПКА ПРОДАЖА =====
@router.message(lambda m: m.text == "🛒 Продажа")
async def sale_handler(message: Message):
    await message.answer("Раздел продажи пока в разработке")

# ===== ЗАПУСК =====
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("Бот запущен 🚀")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())