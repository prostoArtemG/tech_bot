import asyncio
import json
import math
import os
import re
from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

KYIV_TZ = ZoneInfo("Europe/Kyiv") if ZoneInfo else None


def now_kyiv_str() -> str:
    now = datetime.now(KYIV_TZ) if KYIV_TZ else datetime.now()
    return now.strftime("%d.%m.%Y %H:%M")

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from app.db import db
from app.i18n import TRANSLATIONS

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

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

web_app = FastAPI()
telegram_bot = None

WEB_NOTIFY_CHAT_ID = os.getenv("WEB_NOTIFY_CHAT_ID")

# ===== SaaS platform integration (notifications between bots) =====
SAAS_BOT_TOKEN = os.getenv("SAAS_BOT_TOKEN", "")
SAAS_ADMIN_CHAT_ID = os.getenv("SAAS_ADMIN_CHAT_ID", "")
SAAS_WEBHOOK_URL = os.getenv("SAAS_WEBHOOK_URL", "")  # optional HTTP webhook
SAAS_PLATFORM_URL = os.getenv("SAAS_PLATFORM_URL", "").rstrip("/")
SAAS_CLIENT_NAME = os.getenv("SAAS_CLIENT_NAME", "Technovlada")
SAAS_CLIENT_SLUG = os.getenv("SAAS_CLIENT_SLUG", "technovlada")


async def send_to_saas_platform(text: str, payload: dict | None = None) -> bool:
    """
    Send a payment-request notification to saas_platform.
    Two transports (any one configured is enough):
      1. SAAS_BOT_TOKEN + SAAS_ADMIN_CHAT_ID — direct Telegram Bot API call.
      2. SAAS_WEBHOOK_URL — HTTP POST with JSON payload.
    Returns True if at least one delivery succeeded.
    """
    import aiohttp
    ok = False
    try:
        async with aiohttp.ClientSession() as session:
            if SAAS_BOT_TOKEN and SAAS_ADMIN_CHAT_ID:
                url = f"https://api.telegram.org/bot{SAAS_BOT_TOKEN}/sendMessage"
                try:
                    async with session.post(
                        url,
                        json={"chat_id": SAAS_ADMIN_CHAT_ID, "text": text},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            ok = True
                        else:
                            print(f"[saas] telegram api {resp.status}: {await resp.text()}")
                except Exception as e:
                    print(f"[saas] telegram send failed: {e}")

            if SAAS_WEBHOOK_URL and payload is not None:
                try:
                    async with session.post(
                        SAAS_WEBHOOK_URL,
                        json={"text": text, **payload},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status < 400:
                            ok = True
                        else:
                            print(f"[saas] webhook {resp.status}: {await resp.text()}")
                except Exception as e:
                    print(f"[saas] webhook failed: {e}")
    except Exception as e:
        print(f"[saas] session error: {e}")
    return ok


# Subscription gate: check status from saas_platform before allowing edit actions.
# Cache to avoid hitting the API on every keystroke.
_saas_status_cache: dict = {"value": None, "ts": 0.0}
_SAAS_STATUS_TTL = 60.0  # seconds


async def get_saas_client_status() -> str:
    """Return 'active' / 'expired' / 'unknown'. Cached for _SAAS_STATUS_TTL seconds."""
    import time
    if not SAAS_PLATFORM_URL or not SAAS_CLIENT_SLUG:
        return "unknown"
    now_ts = time.monotonic()
    if _saas_status_cache["value"] and (now_ts - _saas_status_cache["ts"]) < _SAAS_STATUS_TTL:
        return _saas_status_cache["value"]
    import aiohttp
    url = f"{SAAS_PLATFORM_URL}/api/client-status/{SAAS_CLIENT_SLUG}"
    status = "unknown"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    raw = (data or {}).get("status") or (data or {}).get("subscription_status") or ""
                    raw = str(raw).lower().strip()
                    if raw in {"active", "ok", "valid"}:
                        status = "active"
                    elif raw in {"expired", "overdue", "blocked", "inactive"}:
                        status = "expired"
                else:
                    print(f"[saas] client-status http {resp.status}")
    except Exception as e:
        print(f"[saas] client-status failed: {e}")
    _saas_status_cache["value"] = status
    _saas_status_cache["ts"] = now_ts
    return status


async def require_active_subscription(message: Message) -> bool:
    """Return False (and notify user) if subscription is expired."""
    status = await get_saas_client_status()
    if status == "expired":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="💳 Оплатить подписку", callback_data="pay_subscription_inline")
            ]]
        )
        await message.answer(
            "⚠️ Подписка просрочена. Оплатите продление.",
            reply_markup=kb,
        )
        return False
    return True


@router.callback_query(lambda c: c.data == "pay_subscription_inline")
async def pay_subscription_inline_callback(callback: CallbackQuery):
    await callback.answer()
    # reuse stub handler logic by faking a Message-like flow:
    fake_text = "💰 Оплатить подписку"
    # build a message from the callback to reuse handler
    msg = callback.message
    # call the existing payment handler
    await payment_pay_stub_handler_internal(msg, callback.from_user, fake_text)


async def payment_pay_stub_handler_internal(message: Message, from_user, text_value: str):
    """Shared payment-request flow usable from both message and callback."""
    info = await get_payment_info()
    is_subscription = text_value == "💰 Оплатить подписку"
    payload = {
        "type": "subscription" if is_subscription else "domain",
        "client_name": SAAS_CLIENT_NAME,
        "client_slug": SAAS_CLIENT_SLUG,
        "telegram_user_id": from_user.id if from_user else None,
        "telegram_username": (from_user.username if from_user else None) or "",
        "created_at": now_kyiv_str(),
    }
    if is_subscription:
        payload["plan"] = info.get("pay_sub_plan", "—")
        payload["amount"] = info.get("pay_sub_price", "—")
        type_label = "Подписка"
        amount_line = f"💰 Сумма: {payload['amount']}"
        extra_line = f"📦 Тариф: {payload['plan']}"
    else:
        payload["domain"] = info.get("pay_domain_name", "—")
        type_label = "Домен"
        amount_line = f"🌐 Домен: {payload['domain']}"
        extra_line = ""

    notif = (
        "💳 Новый запрос на оплату\n\n"
        f"🏢 Клиент: {SAAS_CLIENT_NAME}\n"
        f"🌐 Slug: {SAAS_CLIENT_SLUG}\n"
        f"📦 Тип: {type_label}\n"
        + (f"{extra_line}\n" if extra_line else "")
        + f"{amount_line}\n"
        f"👤 Telegram ID: {payload['telegram_user_id'] or '—'}"
        + (f" (@{payload['telegram_username']})" if payload['telegram_username'] else "")
        + f"\n📅 {payload['created_at']}"
    )
    sent = await send_to_saas_platform(notif, payload)
    if sent:
        await message.answer("✅ Запрос на оплату отправлен.")
    else:
        await message.answer("⚠️ Не удалось отправить запрос. Попробуйте позже или свяжитесь с администратором.")



async def notify_admins(text: str):
    """Send a message to every admin (ADMIN_IDS + WEB_NOTIFY_CHAT_ID)."""
    if not telegram_bot:
        return
    recipients = set(ADMIN_IDS)
    if WEB_NOTIFY_CHAT_ID:
        try:
            recipients.add(int(WEB_NOTIFY_CHAT_ID))
        except (TypeError, ValueError):
            pass
    for chat_id in recipients:
        try:
            await telegram_bot.send_message(chat_id, text)
        except Exception as e:
            print(f"[notify_admins] failed to send to {chat_id}: {e}")


class SiteOrderRequest(BaseModel):
    product_id: int
    qty: int = 1
    name: str
    phone: str
    city: str | None = None
    comment: str | None = None


class SiteEventRequest(BaseModel):
    event_type: str
    product_id: int | None = None

templates = Jinja2Templates(directory="templates")
web_app.mount("/static", StaticFiles(directory="static"), name="static")


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


class AddAdminState(StatesGroup):
    waiting_for_tg_id = State()


class DeleteUserState(StatesGroup):
    waiting_for_tg_id = State()


class EditProductState(StatesGroup):
    waiting_for_query = State()
    waiting_for_product_id = State()
    waiting_for_field = State()
    waiting_for_value = State()
    waiting_for_category = State()
    confirm_delete = State()


class CurrencyRateState(StatesGroup):
    waiting_for_currency = State()
    waiting_for_rate = State()


class FindProductState(StatesGroup):
    waiting_for_query = State()
    waiting_for_product_id = State()


class WarrantyState(StatesGroup):
    waiting_for_phone = State()


class OrderState(StatesGroup):
    waiting_for_query = State()
    waiting_for_product_id = State()
    waiting_for_qty = State()
    waiting_for_customer_phone = State()
    waiting_for_customer_name = State()
    waiting_for_customer_city = State()
    waiting_for_comment = State()


class OrderStatusState(StatesGroup):
    waiting_for_order_id = State()
    waiting_for_status = State()


class SiteContactsState(StatesGroup):
    waiting_for_field = State()


class SitePhonesState(StatesGroup):
    waiting_for_add = State()
    waiting_for_delete = State()


class SiteCategoryState(StatesGroup):
    waiting_for_name_ru = State()
    waiting_for_name_uk = State()
    waiting_for_emoji = State()
    waiting_for_sort_order = State()
    waiting_for_toggle_id = State()


class SiteHeaderState(StatesGroup):
    waiting_for_field = State()


class SiteColorsState(StatesGroup):
    waiting_for_field = State()


class SiteBannerState(StatesGroup):
    waiting_for_field = State()


class SiteCategoryQuickState(StatesGroup):
    waiting = State()


class SiteProductPreviewState(StatesGroup):
    waiting_for_query = State()


admin_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Товары"), KeyboardButton(text="� Заказы")],
        [KeyboardButton(text="🌐 Сайт"), KeyboardButton(text="🌐 Язык")],
        [KeyboardButton(text="❌ Сброс")],
    ],
    resize_keyboard=True
)


@router.callback_query(lambda c: c.data == "cancel_flow")
async def cancel_flow_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()

    role = "admin" if is_system_admin(callback.from_user.id) else "seller"
    user = await db.get_user_by_telegram_id(callback.from_user.id)
    if user:
        role = user["role"] or role

    menu = await get_main_menu(callback.message)

    await callback.message.answer(await t(callback.message, "reset_done"), reply_markup=menu)


def inline_order_status_kb(order_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🆕 Новый", callback_data=f"order_status:{order_id}:new"),
                InlineKeyboardButton(text="🔄 В обработке", callback_data=f"order_status:{order_id}:processing"),
                InlineKeyboardButton(text="📦 Заказано у поставщика", callback_data=f"order_status:{order_id}:ordered_supplier"),
            ],
            [
                InlineKeyboardButton(text="🚚 В пути", callback_data=f"order_status:{order_id}:in_transit"),
                InlineKeyboardButton(text="📦 Готово", callback_data=f"order_status:{order_id}:ready"),
                InlineKeyboardButton(text="✅ Выполнен", callback_data=f"order_status:{order_id}:done"),
            ],
            [
                InlineKeyboardButton(text="❌ Отменён", callback_data=f"order_status:{order_id}:cancelled"),
            ],
        ]
    )


def inline_order_actions_kb(order_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сделать продажей", callback_data=f"order_to_sale:{order_id}")
            ],
            [
                InlineKeyboardButton(text="📦 Заказан у поставщика", callback_data=f"order_status:{order_id}:ordered_supplier"),
                InlineKeyboardButton(text="🚚 В пути", callback_data=f"order_status:{order_id}:in_transit"),
            ],
            [
                InlineKeyboardButton(text="📍 Готов", callback_data=f"order_status:{order_id}:ready"),
                InlineKeyboardButton(text="❌ Отменён", callback_data=f"order_status:{order_id}:cancelled"),
            ]
        ]
    )
seller_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="� Товары"), KeyboardButton(text="📋 Заказы")],
        [KeyboardButton(text="🌐 Сайт"), KeyboardButton(text="🌐 Язык")],
        [KeyboardButton(text="❌ Сброс")],
    ],
    resize_keyboard=True
)

# backward-compatible alias: default to seller menu
menu_kb = seller_menu_kb

products_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить товар")],
        [KeyboardButton(text="📋 Список товаров")],
        [KeyboardButton(text="🔍 Найти товар")],
        [KeyboardButton(text="✏️ Редактировать товар")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


categories_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Товары"), KeyboardButton(text="🛒 Продажа")],
        [KeyboardButton(text="❌ Отмена продажи"), KeyboardButton(text="🧾 История продаж")],
        [KeyboardButton(text="👤 Клиенты"), KeyboardButton(text="👥 Пользователи")],
        [KeyboardButton(text="📈 Отчёты"), KeyboardButton(text="💰 Прибыль")],
        [KeyboardButton(text="💱 Курсы валют"), KeyboardButton(text="🌐 Язык")],
    ],
    resize_keyboard=True

)

brands_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Samsung"), KeyboardButton(text="LG")],
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


warranty_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Найти гарантию")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


orders_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Создать заказ")],
        [KeyboardButton(text="📋 Список заказов")],
        [KeyboardButton(text="🔁 Изменить статус заказа")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)

order_status_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="new"), KeyboardButton(text="processing"), KeyboardButton(text="ordered_supplier")],
        [KeyboardButton(text="in_transit"), KeyboardButton(text="ready"), KeyboardButton(text="done")],
        [KeyboardButton(text="cancelled"), KeyboardButton(text="⬅️ Назад")],
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


site_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📞 Контакты сайта")],
        [KeyboardButton(text="🧢 Шапка сайта")],
        [KeyboardButton(text="🎨 Цвета сайта"), KeyboardButton(text="🖼 Баннер сайта")],
        [KeyboardButton(text="👀 Просмотр товара на сайте")],
        [KeyboardButton(text="📂 Категории сайта")],
        [KeyboardButton(text="✏️ Редактировать товар")],
        [KeyboardButton(text="🌐 Язык сайта")],
        [KeyboardButton(text="📊 Аналитика сайта")],
        [KeyboardButton(text="📋 Заявки/Покупатели"), KeyboardButton(text="👥 Пользователи")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


site_colors_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Показать цвета")],
        [KeyboardButton(text="🎯 Основной цвет")],
        [KeyboardButton(text="✨ Цвет акцента")],
        [KeyboardButton(text="🖼 Цвет фона/шапки")],
        [KeyboardButton(text="♻️ Сбросить цвета")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


site_banner_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Показать баннер")],
        [KeyboardButton(text="📝 Текст баннера")],
        [KeyboardButton(text="🖼 Фото баннера (URL)")],
        [KeyboardButton(text="👁 Баннер: вкл/выкл")],
        [KeyboardButton(text="♻️ Сбросить баннер")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


payment_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 Подписка")],
        [KeyboardButton(text="🌐 Домен")],
        [KeyboardButton(text="🧾 История оплат")],
        [KeyboardButton(text="📞 Связаться")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


payment_subscription_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Оплатить подписку")],
        [KeyboardButton(text="⬅️ Назад в оплату")],
    ],
    resize_keyboard=True
)


payment_domain_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Оплатить домен")],
        [KeyboardButton(text="⬅️ Назад в оплату")],
    ],
    resize_keyboard=True
)


payment_back_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="⬅️ Назад в оплату")]],
    resize_keyboard=True
)


site_contacts_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Показать контакты")],
        [KeyboardButton(text="➕ Добавить телефон"), KeyboardButton(text="🗑 Удалить телефон")],
        [KeyboardButton(text="📞 Телефон (1 номер)"), KeyboardButton(text="💬 Telegram")],
        [KeyboardButton(text="📷 Instagram"), KeyboardButton(text="📍 Адрес")],
        [KeyboardButton(text="⏰ График работы")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


site_header_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Название сайта")],
        [KeyboardButton(text="🏷 Подзаголовок")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


header_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Название сайта")],
        [KeyboardButton(text="🏷 Подзаголовок")],
        [KeyboardButton(text="🛒 Корзина: вкл/выкл")],
        [KeyboardButton(text="📞 Контакты: вкл/выкл")],
        [KeyboardButton(text="🌐 Язык: вкл/выкл")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


site_categories_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Показать категории сайта")],

        [KeyboardButton(text="➕ Холодильники"), KeyboardButton(text="➕ Стиральные машины")],
        [KeyboardButton(text="➕ Кондиционеры"), KeyboardButton(text="➕ Нагреватели")],

        [KeyboardButton(text="➕ Своя категория")],

        [KeyboardButton(text="👁 Вкл/выкл категорию")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


users_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Список пользователей")],
        [KeyboardButton(text="🔁 Изменить роль")],
        [KeyboardButton(text="➕ Добавить админа"), KeyboardButton(text="❌ Удалить пользователя")],
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


edit_product_fields_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Цена продажи"), KeyboardButton(text="Закупка")],
        [KeyboardButton(text="Валюта закупки"), KeyboardButton(text="Артикул")],
        [KeyboardButton(text="Гарантия"), KeyboardButton(text="Модель")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


def inline_edit_fields_kb(product=None):
    is_hidden = product and not product.get("is_active", True)
    visibility_text = "👁 Показать товар" if is_hidden else "👁 Скрыть товар"
    visibility_action = "show_product" if is_hidden else "hide_product"

    is_sale = bool(product and product.get("is_sale"))
    sale_text = "🔥 Акция: ВКЛ" if is_sale else "🔥 Акция: ВЫКЛ"

    stock_status = (product and product.get("stock_status")) or "in_stock"
    status_labels = {
        "in_stock": "🟢 В наличии",
        "preorder": "🟡 Под заказ",
        "out_of_stock": "🔴 Нет в наличии",
    }
    status_text = f"📦 Статус: {status_labels.get(stock_status, stock_status)}"

    rows = [
        [
            InlineKeyboardButton(text="Цена продажи", callback_data="edit_field:price"),
            InlineKeyboardButton(text="Закупка", callback_data="edit_field:purchase_price"),
        ],
        [
            InlineKeyboardButton(text="Валюта закупки", callback_data="edit_field:purchase_currency"),
            InlineKeyboardButton(text="Артикул", callback_data="edit_field:sku"),
        ],
        [
            InlineKeyboardButton(text="Гарантия", callback_data="edit_field:warranty_months"),
            InlineKeyboardButton(text="Модель", callback_data="edit_field:model"),
        ],
        [
            InlineKeyboardButton(text="Фото (URL)", callback_data="edit_field:photo_url"),
            InlineKeyboardButton(text="Описание", callback_data="edit_field:description"),
            InlineKeyboardButton(text="Характеристики", callback_data="edit_field:specs"),
        ],
        [
            InlineKeyboardButton(text="💰 Старая цена", callback_data="edit_field:old_price"),
            InlineKeyboardButton(text=sale_text, callback_data="edit_action:toggle_sale"),
        ],
        [
            InlineKeyboardButton(text=status_text, callback_data="edit_action:cycle_stock_status"),
        ],
    ]

    category = (product or {}).get("category") if product else None
    if category and "Бойлер" in str(category):
        rows.append([
            InlineKeyboardButton(text="🚿 Объем бойлера", callback_data="edit_field:boiler_volume_liters"),
            InlineKeyboardButton(text="🔥 Тип тена", callback_data="edit_action:set_ten_type"),
        ])

    rows.extend([
        [
            InlineKeyboardButton(text="📂 Изменить категорию", callback_data="edit_action:change_category"),
            InlineKeyboardButton(text="🖼 Управление фото", callback_data="edit_action:manage_photos"),
        ],
        [
            InlineKeyboardButton(text=visibility_text, callback_data=f"edit_action:{visibility_action}"),
            InlineKeyboardButton(text="❌ Удалить товар", callback_data="edit_action:soft_delete"),
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow"),
        ],
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)
@router.callback_query(lambda c: c.data and c.data.startswith("edit_field:"))
async def edit_field_callback(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":")[1]

    field_titles = {
        "price": "Цена продажи",
        "purchase_price": "Закупка",
        "purchase_currency": "Валюта закупки",
        "sku": "Артикул",
        "warranty_months": "Гарантия",
        "model": "Модель",
        "photo_url": "Фото",
        "description": "Описание",
        "specs": "Характеристики",
        "old_price": "Старая цена",
        "boiler_volume_liters": "Объем бойлера (л)",
    }

    await state.update_data(field=field, field_title=field_titles[field])
    await state.set_state(EditProductState.waiting_for_value)

    if field == "purchase_currency":
        await callback.message.answer("Выберите валюту: UAH / USD / EUR")
    elif field == "specs":
        await callback.message.answer(
            "Введите характеристики в формате:\nОбъём: 80 л\nТип: сушильная машина\nЗагрузка: 8 кг"
        )
    elif field == "old_price":
        await callback.message.answer(
            "Введите старую цену (число, грн).\nОтправьте «-» чтобы очистить."
        )
    elif field == "boiler_volume_liters":
        await callback.message.answer(
            "Введите объем бойлера в литрах (число): 50, 80, 100, 120 ...\nОтправьте «-» чтобы очистить."
        )
    else:
        await callback.message.answer(f"Введите новое значение для поля: {field_titles[field]}")

    await callback.answer()
def inline_categories_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Стиральная машина", callback_data="add_category:Стиральная машина"),
                InlineKeyboardButton(text="Холодильник", callback_data="add_category:Холодильник"),
            ],
            [
                InlineKeyboardButton(text="Пылесос", callback_data="add_category:Пылесос"),
                InlineKeyboardButton(text="Микроволновка", callback_data="add_category:Микроволновка"),
            ],
            [
                InlineKeyboardButton(text="Телевизор", callback_data="add_category:Телевизор"),
                InlineKeyboardButton(text="Бойлер", callback_data="add_category:Бойлер"),
            ],
            [
                InlineKeyboardButton(text="🔍 Поиск категории", callback_data="add_category_search"), InlineKeyboardButton(text="Другая техника", callback_data="add_category:Другая техника"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow"),
            ],
        ]
    )


def inline_brands_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Samsung", callback_data="add_brand:Samsung"), InlineKeyboardButton(text="LG", callback_data="add_brand:LG"),
            ],
            [
                InlineKeyboardButton(text="Bosch", callback_data="add_brand:Bosch"), InlineKeyboardButton(text="Beko", callback_data="add_brand:Beko"),
            ],
            [
                InlineKeyboardButton(text="Philips", callback_data="add_brand:Philips"), InlineKeyboardButton(text="Xiaomi", callback_data="add_brand:Xiaomi"),
            ],
            [
                InlineKeyboardButton(text="🔍 Поиск бренда", callback_data="add_brand_search"), InlineKeyboardButton(text="Другое", callback_data="add_brand_manual"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow"),
            ],
        ]
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

    lang = "ru"
    if user and user.get("language"):
        lang = user["language"]

    return TRANSLATIONS.get(lang, TRANSLATIONS["ru"]).get(key, key)


@router.message(lambda m: m.text in {"🌐 Язык", "🌐 Мова"})
async def language_menu(message: Message, state: FSMContext):
    await state.clear()

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Русский"), KeyboardButton(text="Українська")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Выберите язык / Оберіть мову:",
        reply_markup=kb
    )


@router.message(lambda m: m.text in {"Русский", "Українська"})
async def set_language(message: Message, state: FSMContext):
    lang = "ru" if message.text == "Русский" else "uk"

    await db.set_user_language(message.from_user.id, lang)

    await state.clear()

    menu = await get_main_menu_for_user(message)

    text = "Язык сохранён" if lang == "ru" else "Мову збережено"

    await message.answer(text, reply_markup=menu)


@router.message(lambda m: m.text == "⬅️ Назад")
async def back_handler(message: Message, state: FSMContext):
    await state.clear()
    menu = await get_main_menu_for_user(message)
    await message.answer(await t(message, "main_menu"), reply_markup=menu)


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

    if not user.get("is_active", True):
        return "seller"

    return user["role"] or "seller"


async def get_main_menu_for_user(message: Message):
    role = await get_current_user_role(message)
    # build menus dynamically using translations to ensure correct language labels
    if role == "admin":
        return ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text=await t(message, "products")),
                    KeyboardButton(text=await t(message, "orders")),
                ],
                [
                    KeyboardButton(text="🌐 Сайт"),
                    KeyboardButton(text="💳 Оплата"),
                ],
                [
                    KeyboardButton(text=await t(message, "language")),
                ],
                [
                    KeyboardButton(text="❌ Сброс"),
                ],
            ],
            resize_keyboard=True
        )

    # seller
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=await t(message, "products")),
                KeyboardButton(text=await t(message, "orders")),
            ],
            [
                KeyboardButton(text="🌐 Сайт"),
                KeyboardButton(text="💳 Оплата"),
            ],
            [
                KeyboardButton(text=await t(message, "language")),
            ],
            [KeyboardButton(text="❌ Сброс")],
        ],
        resize_keyboard=True
    )


async def get_main_menu(message: Message):
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=await t(message, "products")),
                KeyboardButton(text=await t(message, "orders")),
            ],
            [
                KeyboardButton(text="🌐 Сайт"),
                KeyboardButton(text="💳 Оплата"),
            ],
            [
                KeyboardButton(text=await t(message, "language")),
            ],
        ],
        resize_keyboard=True
    )


async def require_admin(message: Message) -> bool:
    role = await get_current_user_role(message)
    if role != "admin":
        await message.answer(await t(message, "no_access"))
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
    await message.answer(await t(message, "start"), reply_markup=menu)




@router.message(lambda m: m.text in {"🧾 Гарантии", "🧾 Гарантії"})
async def warranties_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Раздел гарантий:", reply_markup=warranty_kb)


@router.message(lambda m: m.text == "🔍 Найти гарантию")
async def warranty_search_start_handler(message: Message, state: FSMContext):
    await state.set_state(WarrantyState.waiting_for_phone)
    await message.answer("Введите телефон клиента:")


@router.message(WarrantyState.waiting_for_phone)
async def warranty_search_handler(message: Message, state: FSMContext):
    phone = normalize_phone(message.text or "")

    rows = await db.search_warranties_by_phone(phone)

    await state.clear()

    if not rows:
        await message.answer("Гарантий по этому телефону не найдено.", reply_markup=warranty_kb)
        return

    lines = ["🧾 Найденные гарантии:\n"]

    for row in rows:
        lines.append(
            f"#{row['id']}\n"
            f"Клиент: {row['customer_name'] or '-'} | {row['customer_phone'] or '-'}\n"
            f"Товар: {row['category'] or '-'} | {row['brand'] or '-'} | {row['model'] or '-'}\n"
            f"Гарантия: {row['warranty_months']} мес\n"
            f"С: {row['start_date']} До: {row['end_date']}\n"
        )

    await message.answer("\n".join(lines), reply_markup=warranty_kb)


@router.message(lambda m: m.text in {"📋 Заказы", "📋 Замовлення"})
async def orders_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await t(message, "orders_section"), reply_markup=orders_kb)
    
# Список заказов
@router.message(lambda m: m.text == "📋 Список заказов")
async def list_orders_handler(message: Message):
    rows = await db.list_orders()

    if not rows:
        await message.answer(await t(message, "orders_empty"), reply_markup=orders_kb)
        return

    status_map = {
        "new": "Новый",
        "processing": "В обработке",
        "ordered_supplier": "Заказан у поставщика",
        "in_transit": "В пути",
        "ready": "Готов",
        "done": "Выполнен",
        "cancelled": "Отменён",
    }

    messages = []

    for row in rows:
        created_at_raw = row["created_at"]
        if created_at_raw:
            if KYIV_TZ is not None:
                if created_at_raw.tzinfo is None:
                    from datetime import timezone as _tz
                    created_at_raw = created_at_raw.replace(tzinfo=_tz.utc)
                created_at_local = created_at_raw.astimezone(KYIV_TZ)
            else:
                created_at_local = created_at_raw
            created_at = created_at_local.strftime("%d.%m.%Y %H:%M")
        else:
            created_at = "-"
        status_ru = status_map.get(row["status"], row["status"])

        messages.append(
            """
🧾 Заказ #{id}
📅 {created_at}
📍 Статус: {status}
👤 Клиент: {name} | {phone}
🏙 Город: {city}
📦 Товар: {product}
🔢 Кол-во: {qty}
💰 Сумма: {total} грн
💬 Комментарий: {comment}
""".format(
                id=row["id"],
                created_at=created_at,
                status=status_ru,
                name=(row["customer_name"] or "-"),
                phone=(row["customer_phone"] or "-"),
                city=(row.get("customer_city") or "-"),
                product="{} {}".format(row.get("brand") or "", row.get("model") or "").strip() or "-",
                qty=row.get("qty") or 0,
                total=f"{float(row.get('total_amount') or 0):.0f}",
                comment=(row.get("comment") or "-")
            )
        )

    # Send one message per order with action buttons under it
    for i, row in enumerate(rows):
        await message.answer(messages[i], reply_markup=inline_order_actions_kb(row["id"]))


# Создание заказа — старт
@router.message(lambda m: m.text == "➕ Создать заказ")
async def create_order_start_handler(message: Message, state: FSMContext):
    await state.set_state(OrderState.waiting_for_query)
    await message.answer(await t(message, "enter_search"))


# Поиск товара для заказа
@router.message(
    StateFilter(OrderState.waiting_for_query),
    lambda m: m.text == "⬅️ Назад"
)
async def order_back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await t(message, "orders_section"), reply_markup=orders_kb)


@router.message(OrderState.waiting_for_query)
async def order_search_product_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    rows = await db.search_products(query)

    if not rows:
        await message.answer(await t(message, "no_products_found"))
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{row['brand'] or '-'} {row['model'] or '-'} | {float(row['price'] or 0):.0f} грн",
                    callback_data=f"order_product:{row['id']}"
                )
            ]
            for row in rows
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
    )

    await state.set_state(OrderState.waiting_for_product_id)
    await message.answer(await t(message, "choose_product"), reply_markup=keyboard)


# Callback выбора товара
@router.callback_query(lambda c: c.data and c.data.startswith("order_product:"))
async def order_product_callback_handler(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product_by_id(product_id)

    if not product:
        await callback.message.answer(await t(callback.message, "product_not_found"))
        await callback.answer()
        return

    await state.update_data(product_id=product_id, price=float(product["price"] or 0))
    await state.set_state(OrderState.waiting_for_qty)

    await callback.message.answer(
        f"Товар: {product['brand'] or '-'} {product['model'] or '-'}\n"
        f"{await t(callback.message, 'price')}: {float(product['price'] or 0):.2f} грн\n\n"
        "Введите количество:"
    )
    await callback.answer()


@router.message(
    StateFilter(OrderState),
    lambda m: m.text == "⬅️ Назад"
)
async def order_back_global(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await t(message, "orders_section"), reply_markup=orders_kb)


# Количество
@router.message(OrderState.waiting_for_qty)
async def order_qty_handler(message: Message, state: FSMContext):
    raw_qty = (message.text or "").strip()

    if not raw_qty.isdigit():
        await message.answer("Введите количество числом.")
        return

    qty = int(raw_qty)
    if qty <= 0:
        await message.answer("Количество должно быть больше 0.")
        return

    await state.update_data(qty=qty)
    await state.set_state(OrderState.waiting_for_customer_phone)
    await message.answer("Введите телефон клиента:")


# Клиент по телефону
@router.message(OrderState.waiting_for_customer_phone)
async def order_customer_phone_handler(message: Message, state: FSMContext):
    phone = normalize_phone(message.text or "")

    if len(phone) < 8:
        await message.answer("Введите корректный телефон:")
        return

    customer = await db.get_customer_by_phone(phone)

    if customer:
        await state.update_data(customer_id=customer["id"])
        await state.set_state(OrderState.waiting_for_comment)
        await message.answer(f"{await t(message, 'client_found')} {await t(message, 'enter_comment')}")
        return

    await state.update_data(customer_phone=phone)
    await state.set_state(OrderState.waiting_for_customer_name)
    await message.answer(await t(message, 'client_not_found'))


# Новый клиент
@router.message(OrderState.waiting_for_customer_name)
async def order_customer_name_handler(message: Message, state: FSMContext):
    name = (message.text or "").strip()

    if not name:
        await message.answer("Имя не может быть пустым.")
        return

    await state.update_data(customer_name=name)
    await state.set_state(OrderState.waiting_for_customer_city)
    await message.answer(await t(message, 'enter_city'))


@router.message(OrderState.waiting_for_customer_city)
async def order_customer_city_handler(message: Message, state: FSMContext):
    city = (message.text or "").strip()

    if not city:
        await message.answer("Город не может быть пустым.")
        return

    data = await state.get_data()

    customer = await db.create_customer(
        name=data["customer_name"],
        phone=data["customer_phone"],
        city=city
    )

    await state.update_data(customer_id=customer["id"])
    await state.set_state(OrderState.waiting_for_comment)
    await message.answer(await t(message, 'enter_comment'))


# Комментарий и сохранение заказа
@router.message(OrderState.waiting_for_comment)
async def order_comment_handler(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    if comment == "-":
        comment = None

    data = await state.get_data()

    product_id = data["product_id"]
    customer_id = data["customer_id"]
    qty = data["qty"]
    price = data["price"]
    total = qty * price

    row = await db.create_order(
        customer_id=customer_id,
        product_id=product_id,
        qty=qty,
        total_amount=total,
        comment=comment
    )

    await state.clear()

    await message.answer(
        f"{await t(message, 'order_created')}\n\n"
        f"ID: {row['id']}\n"
        f"Количество: {qty}\n"
        f"Сумма: {total:.2f} грн\n"
        f"Статус: new",
        reply_markup=orders_kb
    )


# Изменение статуса
@router.message(lambda m: m.text == "🔁 Изменить статус заказа")
async def order_status_start_handler(message: Message, state: FSMContext):
    await state.set_state(OrderStatusState.waiting_for_order_id)
    await message.answer(await t(message, "enter_order_id"))


@router.message(OrderStatusState.waiting_for_order_id)
async def order_status_id_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if raw_id == "⬅️ Назад":
        await state.clear()
        await message.answer(await t(message, "orders_section"), reply_markup=orders_kb)
        return

    if not raw_id.isdigit():
        await message.answer(await t(message, "order_id_must_be_number"))
        return

    order_id = int(raw_id)
    order = await db.get_order_by_id(order_id)

    if not order:
        await message.answer(await t(message, "order_not_found"))
        return

    await state.update_data(order_id=order_id)
    await state.set_state(OrderStatusState.waiting_for_status)

    await message.answer(
        f"Текущий статус: {order['status']}\n{await t(message, 'choose_status')}",
        reply_markup=order_status_kb
    )


@router.message(OrderStatusState.waiting_for_status)
async def order_status_finish_handler(message: Message, state: FSMContext):
    status = (message.text or "").strip()

    if status == "⬅️ Назад":
        await state.clear()
        await message.answer(await t(message, "orders_section"), reply_markup=orders_kb)
        return

    if status not in {"new", "processing", "ordered_supplier", "in_transit", "ready", "done", "cancelled"}:
        await message.answer("Выберите статус кнопкой.")
        return

    data = await state.get_data()
    order_id = data["order_id"]

    await db.update_order_status(order_id, status)
    await state.clear()

    await message.answer(
        f"{await t(message, 'order_status_updated')} #{order_id}: {status}",
        reply_markup=orders_kb
    )
@router.message(
    StateFilter(OrderStatusState),
    lambda m: m.text == "⬅️ Назад"
)
async def order_status_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await t(message, "orders_section"), reply_markup=orders_kb)



@router.message(lambda m: m.text in {"📞 Телефон (1 номер)", "💬 Telegram", "📷 Instagram", "📍 Адрес", "⏰ График работы"})
async def site_contact_field_start(message: Message, state: FSMContext):
    field_map = {
        "📞 Телефон (1 номер)": ("site_phone", "Введите телефон сайта:"),
        "💬 Telegram": ("site_tg", "Введите Telegram сайта:"),
        "📷 Instagram": ("site_instagram", "Введите Instagram сайта:"),
        "📍 Адрес": ("site_address", "Введите адрес сайта:"),
        "⏰ График работы": ("site_schedule", "Введите график работы:"),
    }

    key, prompt = field_map.get(message.text, (None, None))
    if not key:
        await message.answer("Неизвестное поле", reply_markup=site_contacts_kb)
        return

    await state.update_data(setting_key=key)
    await state.set_state(SiteContactsState.waiting_for_field)
    await message.answer(prompt)


@router.message(lambda m: m.text in {"📝 Название сайта", "🏷 Подзаголовок"})
async def site_header_field_start(message: Message, state: FSMContext):
    field_map = {
        "📝 Название сайта": ("site_title", "Введите название сайта:"),
        "🏷 Подзаголовок": ("site_subtitle", "Введите подзаголовок сайта:"),
    }

    key, prompt = field_map.get(message.text, (None, None))
    if not key:
        await message.answer("Неизвестное поле", reply_markup=site_header_kb)
        return

    await state.update_data(setting_key=key)
    await state.set_state(SiteHeaderState.waiting_for_field)
    await message.answer(prompt)


@router.message(SiteContactsState.waiting_for_field)
async def site_contact_field_save(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")

    if not key:
        await state.clear()
        await message.answer("Ошибка состояния.", reply_markup=site_contacts_kb)
        return

    value = (message.text or "").strip()
    await db.set_setting(key, value)

    await state.clear()
    await message.answer("✅ Сохранено", reply_markup=site_contacts_kb)


@router.message(SiteHeaderState.waiting_for_field)
async def site_header_field_save(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")

    if not key:
        await state.clear()
        await message.answer("Ошибка состояния.", reply_markup=site_header_kb)
        return

    value = (message.text or "").strip()
    await db.set_setting(key, value)

    await state.clear()
    await message.answer("✅ Сохранено", reply_markup=site_header_kb)


# ===== Site design: colors =====

DESIGN_DEFAULTS = {
    "design_primary_color": "#111827",
    "design_accent_color": "#16a34a",
    "design_header_bg": "#111827",
    "banner_text": "",
    "banner_image_url": "",
    "banner_enabled": "false",
}

HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


async def get_site_design():
    return {
        "primary_color": (await db.get_setting("design_primary_color")) or DESIGN_DEFAULTS["design_primary_color"],
        "accent_color": (await db.get_setting("design_accent_color")) or DESIGN_DEFAULTS["design_accent_color"],
        "header_bg": (await db.get_setting("design_header_bg")) or DESIGN_DEFAULTS["design_header_bg"],
        "banner_text": (await db.get_setting("banner_text")) or "",
        "banner_image_url": (await db.get_setting("banner_image_url")) or "",
        "banner_enabled": ((await db.get_setting("banner_enabled")) or "false") == "true",
    }


@router.message(lambda m: m.text == "🎨 Цвета сайта")
async def site_colors_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    if not await require_active_subscription(message):
        return
    await state.clear()
    await message.answer("Цвета сайта:", reply_markup=site_colors_kb)


@router.message(lambda m: m.text == "📋 Показать цвета")
async def site_colors_show(message: Message):
    design = await get_site_design()
    await message.answer(
        "🎨 Текущие цвета сайта:\n\n"
        f"🎯 Основной: {design['primary_color']}\n"
        f"✨ Акцент: {design['accent_color']}\n"
        f"🖼 Фон/шапка: {design['header_bg']}\n\n"
        "Если значение совпадает с дефолтом — оно подставлено по умолчанию."
    )


@router.message(lambda m: m.text in {"🎯 Основной цвет", "✨ Цвет акцента", "🖼 Цвет фона/шапки"})
async def site_color_field_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    field_map = {
        "🎯 Основной цвет": ("design_primary_color", "Введите HEX основного цвета (например, #111827):"),
        "✨ Цвет акцента": ("design_accent_color", "Введите HEX акцентного цвета (например, #ef4444):"),
        "🖼 Цвет фона/шапки": ("design_header_bg", "Введите HEX цвета фона/шапки (например, #ffffff):"),
    }
    key, prompt = field_map[message.text]
    await state.update_data(setting_key=key)
    await state.set_state(SiteColorsState.waiting_for_field)
    await message.answer(prompt + "\nОтправьте «-» чтобы вернуть значение по умолчанию.")


@router.message(SiteColorsState.waiting_for_field)
async def site_color_field_save(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")
    if not key:
        await state.clear()
        await message.answer("Ошибка состояния.", reply_markup=site_colors_kb)
        return

    value = (message.text or "").strip()

    if value == "-" or value == "":
        await db.set_setting(key, "")
        await state.clear()
        await message.answer("✅ Возвращено значение по умолчанию.", reply_markup=site_colors_kb)
        return

    if not HEX_COLOR_RE.match(value):
        await message.answer("Неверный HEX. Пример: #ef4444 или #fff. Попробуйте ещё раз или отправьте «-».")
        return

    await db.set_setting(key, value)
    await state.clear()
    await message.answer(f"✅ Сохранено: {value}", reply_markup=site_colors_kb)


@router.message(lambda m: m.text == "♻️ Сбросить цвета")
async def site_colors_reset(message: Message):
    if not await require_admin(message):
        return
    for key in ("design_primary_color", "design_accent_color", "design_header_bg"):
        await db.set_setting(key, "")
    await message.answer("✅ Цвета сброшены к значениям по умолчанию.", reply_markup=site_colors_kb)


# ===== Site design: banner =====

@router.message(lambda m: m.text == "🖼 Баннер сайта")
async def site_banner_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    if not await require_active_subscription(message):
        return
    await state.clear()
    await message.answer("Баннер сайта:", reply_markup=site_banner_kb)


@router.message(lambda m: m.text == "📋 Показать баннер")
async def site_banner_show(message: Message):
    design = await get_site_design()
    state_text = "ВКЛ" if design["banner_enabled"] else "ВЫКЛ"
    await message.answer(
        "🖼 Баннер сайта:\n\n"
        f"Состояние: {state_text}\n"
        f"Текст: {design['banner_text'] or '-'}\n"
        f"Фото: {design['banner_image_url'] or '-'}"
    )


@router.message(lambda m: m.text in {"📝 Текст баннера", "🖼 Фото баннера (URL)"})
async def site_banner_field_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    field_map = {
        "📝 Текст баннера": ("banner_text", "Введите текст баннера (отправьте «-» чтобы очистить):"),
        "🖼 Фото баннера (URL)": ("banner_image_url", "Введите URL изображения баннера (отправьте «-» чтобы очистить):"),
    }
    key, prompt = field_map[message.text]
    await state.update_data(setting_key=key)
    await state.set_state(SiteBannerState.waiting_for_field)
    await message.answer(prompt)


@router.message(SiteBannerState.waiting_for_field)
async def site_banner_field_save(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")
    if not key:
        await state.clear()
        await message.answer("Ошибка состояния.", reply_markup=site_banner_kb)
        return

    value = (message.text or "").strip()
    if value == "-":
        value = ""

    await db.set_setting(key, value)
    await state.clear()
    await message.answer("✅ Сохранено", reply_markup=site_banner_kb)


@router.message(lambda m: m.text == "👁 Баннер: вкл/выкл")
async def site_banner_toggle(message: Message):
    if not await require_admin(message):
        return
    value = await db.toggle_setting_bool("banner_enabled", "false")
    text = "включён" if value == "true" else "выключен"
    await message.answer(f"✅ Баннер {text}", reply_markup=site_banner_kb)


@router.message(lambda m: m.text == "♻️ Сбросить баннер")
async def site_banner_reset(message: Message):
    if not await require_admin(message):
        return
    await db.set_setting("banner_text", "")
    await db.set_setting("banner_image_url", "")
    await db.set_setting("banner_enabled", "false")
    await message.answer("✅ Баннер сброшен.", reply_markup=site_banner_kb)


# ===== Payment section =====

PAYMENT_DEFAULTS = {
    "pay_sub_status": "active",
    "pay_sub_expires": "—",
    "pay_sub_plan": "Базовый",
    "pay_sub_price": "10$/мес",
    "pay_domain_name": "—",
    "pay_domain_status": "—",
    "pay_domain_expires": "—",
    "pay_support_tg": "@support",
    "pay_support_phone": "—",
    "pay_support_text": "Если есть вопросы по оплате — напишите нам.",
}


async def get_payment_info():
    out = {}
    for key, default in PAYMENT_DEFAULTS.items():
        out[key] = (await db.get_setting(key)) or default
    return out


@router.message(lambda m: m.text == "💳 Оплата")
async def payment_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("💳 Раздел оплаты:", reply_markup=payment_kb)


@router.message(lambda m: m.text == "⬅️ Назад в оплату")
async def payment_back_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("💳 Раздел оплаты:", reply_markup=payment_kb)


@router.message(lambda m: m.text == "📅 Подписка")
async def payment_subscription_handler(message: Message):
    info = await get_payment_info()
    await message.answer(
        "📅 Подписка\n\n"
        f"Статус: {info['pay_sub_status']}\n"
        f"Дата окончания: {info['pay_sub_expires']}\n"
        f"Тариф: {info['pay_sub_plan']}\n"
        f"Стоимость: {info['pay_sub_price']}",
        reply_markup=payment_subscription_kb
    )


@router.message(lambda m: m.text == "🌐 Домен")
async def payment_domain_handler(message: Message):
    info = await get_payment_info()
    await message.answer(
        "🌐 Домен\n\n"
        f"Текущий домен: {info['pay_domain_name']}\n"
        f"Статус: {info['pay_domain_status']}\n"
        f"Дата окончания: {info['pay_domain_expires']}",
        reply_markup=payment_domain_kb
    )


@router.message(lambda m: m.text == "🧾 История оплат")
async def payment_history_handler(message: Message):
    await message.answer("🧾 История оплат скоро появится", reply_markup=payment_back_kb)


@router.message(lambda m: m.text == "📞 Связаться")
async def payment_contact_handler(message: Message):
    info = await get_payment_info()
    await message.answer(
        "📞 Связаться\n\n"
        f"Telegram: {info['pay_support_tg']}\n"
        f"Телефон: {info['pay_support_phone']}\n\n"
        f"{info['pay_support_text']}",
        reply_markup=payment_back_kb
    )


@router.message(lambda m: m.text in {"💰 Оплатить подписку", "💰 Оплатить домен"})
async def payment_pay_stub_handler(message: Message):
    await payment_pay_stub_handler_internal(message, message.from_user, message.text)


@router.message(StateFilter("*"), lambda m: m.text in {
    "📦 Товары", "🛒 Продажа", "➕ Приход", "➕ Добавить товар", "⬅️ Назад", "❌ Сброс",
    "🧾 Гарантии", "🔍 Найти гарантию",
    "📋 Заказы", "➕ Создать заказ", "📋 Список заказов", "🔁 Изменить статус заказа",
    "🌐 Сайт", "📞 Контакты сайта", " Telegram",
    "📂 Категории сайта", "📋 Показать категории сайта", "➕ Холодильники", "➕ Стиральные машины", "➕ Кондиционеры", "➕ Нагреватели", "➕ Своя категория", "👁 Вкл/выкл категорию", "📝 Описание товара",
    "⚙️ Характеристики товара", "🖼 Фото товара", "📷 Instagram", "📍 Адрес", "⏰ График работы", "🌐 Язык сайта",
    "new", "processing", "ordered_supplier", "in_transit", "ready", "done", "cancelled",
})
async def global_menu_buttons_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        return

    text = message.text

    if text in {"⬅️ Назад", "❌ Сброс"}:
        await state.clear()
        menu = await get_main_menu_for_user(message)
        await message.answer(await t(message, "main_menu"), reply_markup=menu)
        return

    if text == "📦 Товары":
        if not await require_admin(message):
            return
        await state.clear()
        await message.answer(await t(message, "products_section"), reply_markup=products_kb)
        return

    if text == "🛒 Продажа":
        await state.clear()
        await state.set_state(SaleState.waiting_for_query)
        await message.answer(await t(message, "enter_search"))
        return

    if text == "➕ Приход":
        if not await require_admin(message):
            return
        await state.clear()
        await state.set_state(ReceiptState.waiting_for_query)
        await message.answer("Введите бренд, модель или категорию товара для прихода:")
        return

    if text == "➕ Добавить товар":
        if not await require_admin(message):
            return
        await state.clear()
        await state.set_state(AddProductState.waiting_for_category)
        await message.answer(await t(message, "enter_search"), reply_markup=inline_categories_kb())
        return

    if text == "🌐 Сайт":
        if not await require_admin(message):
            return
        await state.clear()
        await message.answer("Раздел сайта:", reply_markup=site_kb)
        return
    
    if text == "📞 Контакты сайта":
        if not await require_admin(message):
            return
        await state.clear()
        await message.answer("Контакты сайта:", reply_markup=site_contacts_kb)
        return
    
    if text == "📋 Показать категории сайта":
        if not await require_admin(message):
            return

        rows = await db.list_site_categories()

        if not rows:
            await message.answer("Категорий сайта пока нет.", reply_markup=site_categories_kb)
            return

        lines = ["📂 Категории сайта:\n"]

        for row in rows:
            status = "✅" if row["is_active"] else "🚫"
            lines.append(
                f"{status} ID: {row['id']}\n"
                f"{row['emoji']} RU: {row['name_ru']}\n"
                f"{row['emoji']} UA: {row['name_uk']}\n"
                f"Порядок: {row['sort_order']}\n"
            )

        await message.answer("\n".join(lines), reply_markup=site_categories_kb)
        return

    if text == "➕ Холодильники":
        if await db.get_site_category_by_name("Холодильники"):
            await message.answer("Категория уже существует", reply_markup=site_categories_kb)
            return

        await db.add_site_category("Холодильники", "Холодильники", "🧊", 10)
        await message.answer("✅ Добавлено", reply_markup=site_categories_kb)
        return

    if text == "➕ Стиральные машины":
        if await db.get_site_category_by_name("Стиральные машины"):
            await message.answer("Категория уже существует", reply_markup=site_categories_kb)
            return

        await db.add_site_category("Стиральные машины", "Пральні машини", "🧺", 20)
        await message.answer("✅ Добавлено", reply_markup=site_categories_kb)
        return

    if text == "➕ Кондиционеры":
        if await db.get_site_category_by_name("Кондиционеры"):
            await message.answer("Категория уже существует", reply_markup=site_categories_kb)
            return

        await db.add_site_category("Кондиционеры", "Кондиціонери", "❄️", 30)
        await message.answer("✅ Добавлено", reply_markup=site_categories_kb)
        return

    if text == "➕ Нагреватели":
        if await db.get_site_category_by_name("Нагреватели"):
            await message.answer("Категория уже существует", reply_markup=site_categories_kb)
            return

        await db.add_site_category("Нагреватели", "Нагрівачі", "🔥", 40)
        await message.answer("✅ Добавлено", reply_markup=site_categories_kb)
        return

    if text == "➕ Своя категория":
        if not await require_admin(message):
            return
        await state.set_state(SiteCategoryQuickState.waiting)
        await message.answer(
            "Введите:\nНазвание RU | Назва UA | emoji | порядок\n\nПример:\nБойлеры | Бойлери | 🔥 | 50"
        )
        return

    if text == "👁 Вкл/выкл категорию":
        if not await require_admin(message):
            return
        await state.set_state(SiteCategoryState.waiting_for_toggle_id)
        await message.answer("Введите ID категории:")
        return

    if text == "📂 Категории сайта":
        if not await require_admin(message):
            return
        await state.clear()
        await message.answer("Категории сайта:", reply_markup=site_categories_kb)
        return

@router.message(lambda m: m.text in {"📦 Товары", "📦 Товари"})
async def products_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    if not await require_active_subscription(message):
        return

    await state.clear()
    await message.answer(await t(message, "products_section"), reply_markup=products_kb)


@router.message(lambda m: m.text == "🌐 Сайт")
async def site_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    if not await require_active_subscription(message):
        return

    await state.clear()
    await message.answer("Раздел сайта:", reply_markup=site_kb)


@router.message(lambda m: m.text == "📞 Контакты сайта")
async def site_contacts_handler(message: Message, state: FSMContext):
    if not await require_active_subscription(message):
        return
    await state.clear()
    await message.answer("Контакты сайта:", reply_markup=site_contacts_kb)


@router.message(lambda m: m.text == "📋 Показать контакты")
async def show_contacts(message: Message):
    phones = await get_phones_list()
    tg = await db.get_setting("site_tg") or "-"
    insta = await db.get_setting("site_instagram") or "-"
    address = await db.get_setting("site_address") or "-"
    schedule = await db.get_setting("site_schedule") or "-"

    if phones:
        def _fmt(p):
            name = (p.get("name") or "").strip()
            phone = (p.get("phone") or "-").strip()
            return f"{name}: {phone}" if name else phone
        phones_block = "\n".join(
            f"  {i+1}. {_fmt(p)}"
            for i, p in enumerate(phones)
        )
    else:
        single = await db.get_setting("site_phone") or "-"
        phones_block = f"  {single}"

    await message.answer(
        f"📞 Телефоны:\n{phones_block}\n\n"
        f"💬 Telegram: {tg}\n"
        f"📷 Instagram: {insta}\n"
        f"📍 Адрес: {address}\n"
        f"⏰ График: {schedule}",
        reply_markup=site_contacts_kb,
    )


# ===== Multiple phones =====

async def get_phones_list():
    raw = await db.get_setting("site_phones_json")
    if not raw:
        legacy = await db.get_setting("site_phone")
        if legacy:
            return [{"name": "", "phone": legacy}]
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [
                {"name": str(x.get("name", "")), "phone": str(x.get("phone", ""))}
                for x in data
                if isinstance(x, dict) and x.get("phone")
            ]
    except (ValueError, TypeError):
        pass
    return []


async def save_phones_list(phones):
    cleaned = [
        {"name": (p.get("name") or "").strip(), "phone": (p.get("phone") or "").strip()}
        for p in phones
        if (p.get("phone") or "").strip()
    ]
    await db.set_setting("site_phones_json", json.dumps(cleaned, ensure_ascii=False))
    # keep legacy single phone in sync (first one)
    await db.set_setting("site_phone", cleaned[0]["phone"] if cleaned else "")


@router.message(lambda m: m.text == "➕ Добавить телефон")
async def site_phone_add_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.set_state(SitePhonesState.waiting_for_add)
    await message.answer(
        "Введите телефон. Можно с подписью через двоеточие.\n"
        "Примеры:\n"
        "+380 (96) 812 84 45\n"
        "Олег: +380501234567\n"
        "Магазин: +380-44-123-45-67"
    )


@router.message(SitePhonesState.waiting_for_add)
async def site_phone_add_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пустой ввод. Попробуйте ещё раз.")
        return

    if ":" in raw:
        name, _, phone = raw.partition(":")
    elif re.fullmatch(r"[\d\s+\-().]+", raw):
        # phone-only input (digits, spaces, +, -, parentheses, dots)
        name, phone = "", raw
    else:
        # split on first whitespace: name first, phone after
        parts = raw.split(None, 1)
        if len(parts) == 2:
            name, phone = parts
        else:
            name, phone = "", parts[0]

    name = name.strip()
    phone = phone.strip()

    if not phone:
        await message.answer("Не указан телефон. Попробуйте ещё раз.")
        return

    phones = await get_phones_list()
    phones.append({"name": name, "phone": phone})
    await save_phones_list(phones)

    await state.clear()
    label = f"{name}: {phone}" if name else phone
    await message.answer(
        f"✅ Телефон добавлен: {label}",
        reply_markup=site_contacts_kb,
    )


@router.message(lambda m: m.text == "🗑 Удалить телефон")
async def site_phone_delete_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    phones = await get_phones_list()
    if not phones:
        await message.answer("Список телефонов пуст.", reply_markup=site_contacts_kb)
        return

    lines = ["Введите номер позиции для удаления:"]
    for i, p in enumerate(phones, 1):
        nm = (p.get("name") or "").strip()
        ph = (p.get("phone") or "").strip()
        lines.append(f"  {i}. " + (f"{nm}: {ph}" if nm else ph))
    await state.set_state(SitePhonesState.waiting_for_delete)
    await message.answer("\n".join(lines))


@router.message(SitePhonesState.waiting_for_delete)
async def site_phone_delete_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите номер позиции (число).")
        return
    idx = int(raw) - 1
    phones = await get_phones_list()
    if idx < 0 or idx >= len(phones):
        await message.answer("Неверный номер позиции.")
        return
    removed = phones.pop(idx)
    await save_phones_list(phones)
    await state.clear()
    rn = (removed.get("name") or "").strip()
    rp = (removed.get("phone") or "").strip()
    label = f"{rn}: {rp}" if rn else rp
    await message.answer(
        f"✅ Удалено: {label}",
        reply_markup=site_contacts_kb,
    )


@router.message(lambda m: m.text == "🧢 Шапка сайта")
async def site_header_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await message.answer("Настройки шапки сайта:", reply_markup=header_kb)


@router.message(lambda m: m.text == "🛒 Корзина: вкл/выкл")
async def toggle_header_cart(message: Message):
    value = await db.toggle_setting_bool("header_show_cart", "true")
    text = "включена" if value == "true" else "выключена"
    await message.answer(f"✅ Корзина в шапке: {text}", reply_markup=header_kb)


@router.message(lambda m: m.text == "📞 Контакты: вкл/выкл")
async def toggle_header_contacts(message: Message):
    value = await db.toggle_setting_bool("header_show_contacts", "true")
    text = "включены" if value == "true" else "выключены"
    await message.answer(f"✅ Контакты в шапке: {text}", reply_markup=header_kb)


@router.message(lambda m: m.text == "🌐 Язык: вкл/выкл")
async def toggle_header_language(message: Message):
    value = await db.toggle_setting_bool("header_show_language", "true")
    text = "включён" if value == "true" else "выключен"
    await message.answer(f"✅ Язык в шапке: {text}", reply_markup=header_kb)


@router.message(lambda m: m.text == "📋 Показать категории сайта")
async def show_site_categories(message: Message):
    if not await require_admin(message):
        return

    rows = await db.list_site_categories()

    if not rows:
        await message.answer("Категорий сайта пока нет.", reply_markup=site_categories_kb)
        return

    lines = ["📂 Категории сайта:\n"]

    for row in rows:
        status = "✅" if row["is_active"] else "🚫"
        lines.append(
            f"{status} ID: {row['id']}\n"
            f"{row['emoji']} RU: {row['name_ru']}\n"
            f"{row['emoji']} UA: {row['name_uk']}\n"
            f"Порядок: {row['sort_order']}\n"
        )

    await message.answer("\n".join(lines), reply_markup=site_categories_kb)


@router.message(lambda m: m.text == "➕ Холодильники")
async def add_cat_fridge(message: Message):
    if await db.get_site_category_by_name("Холодильники"):
        await message.answer("Категория уже существует", reply_markup=site_categories_kb)
        return

    await db.add_site_category("Холодильники", "Холодильники", "🧊", 10)
    await message.answer("✅ Добавлено", reply_markup=site_categories_kb)


@router.message(lambda m: m.text == "➕ Стиральные машины")
async def add_cat_wash(message: Message):
    if await db.get_site_category_by_name("Стиральные машины"):
        await message.answer("Категория уже существует", reply_markup=site_categories_kb)
        return

    await db.add_site_category("Стиральные машины", "Пральні машини", "🧺", 20)
    await message.answer("✅ Добавлено", reply_markup=site_categories_kb)


@router.message(lambda m: m.text == "➕ Кондиционеры")
async def add_cat_ac(message: Message):
    if await db.get_site_category_by_name("Кондиционеры"):
        await message.answer("Категория уже существует", reply_markup=site_categories_kb)
        return

    await db.add_site_category("Кондиционеры", "Кондиціонери", "❄️", 30)
    await message.answer("✅ Добавлено", reply_markup=site_categories_kb)


@router.message(lambda m: m.text == "➕ Нагреватели")
async def add_cat_heat(message: Message):
    if await db.get_site_category_by_name("Нагреватели"):
        await message.answer("Категория уже существует", reply_markup=site_categories_kb)
        return

    await db.add_site_category("Нагреватели", "Нагрівачі", "🔥", 40)
    await message.answer("✅ Добавлено", reply_markup=site_categories_kb)


@router.message(lambda m: m.text == "➕ Своя категория")
async def custom_category_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.set_state(SiteCategoryQuickState.waiting)
    await message.answer(
        "Введите:\nНазвание RU | Назва UA | emoji | порядок\n\nПример:\nБойлеры | Бойлери | 🔥 | 50"
    )


@router.message(SiteCategoryQuickState.waiting)
async def custom_category_save(message: Message, state: FSMContext):
    try:
        parts = [p.strip() for p in (message.text or "").split("|")]

        name_ru = parts[0]
        name_uk = parts[1] if len(parts) > 1 else parts[0]
        emoji = parts[2] if len(parts) > 2 else "📦"
        sort_order = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 100

        if await db.get_site_category_by_name(name_ru):
            await message.answer("Категория уже существует", reply_markup=site_categories_kb)
            await state.clear()
            return

        await db.add_site_category(name_ru, name_uk, emoji, sort_order)

        await message.answer("✅ Категория добавлена", reply_markup=site_categories_kb)

    except Exception:
        await message.answer("Ошибка формата. Пример:\nБойлеры | Бойлери | 🔥 | 50")

    await state.clear()


@router.message(lambda m: m.text == "👁 Вкл/выкл категорию")
async def toggle_site_category_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.set_state(SiteCategoryState.waiting_for_toggle_id)
    await message.answer("Введите ID категории:")


@router.message(SiteCategoryState.waiting_for_toggle_id)
async def toggle_site_category_finish(message: Message, state: FSMContext):
    raw = (message.text or "").strip()

    if not raw.isdigit():
        await message.answer("ID должен быть числом.")
        return

    await db.toggle_site_category(int(raw))

    await state.clear()
    await message.answer("✅ Статус категории изменён", reply_markup=site_categories_kb)







@router.message(lambda m: m.text == "📂 Категории сайта")
async def site_categories_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.clear()
    await message.answer("Категории сайта:", reply_markup=site_categories_kb)


@router.message(lambda m: m.text == "📝 Описание товара")
async def site_description_handler(message: Message, state: FSMContext):
    await message.answer("Здесь будет редактирование описания товара для сайта.")


@router.message(lambda m: m.text == "⚙️ Характеристики товара")
async def site_specs_handler(message: Message, state: FSMContext):
    await message.answer("Здесь будут характеристики товара.")


@router.message(lambda m: m.text == "👀 Просмотр товара на сайте")
async def site_product_preview_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.clear()
    await state.set_state(SiteProductPreviewState.waiting_for_query)
    await message.answer("Введите бренд, модель или категорию товара:")


@router.message(SiteProductPreviewState.waiting_for_query)
async def site_product_preview_search(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    rows = await db.search_products(query)

    if not rows:
        await message.answer("Ничего не найдено. Попробуйте ещё:")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{row['brand'] or '-'} {row['model'] or '-'} | {float(row['price'] or 0):.0f} грн",
                    callback_data=f"site_preview_product:{row['id']}"
                )
            ]
            for row in rows[:10]
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
    )

    await message.answer("Выберите товар:", reply_markup=keyboard)


@router.message(lambda m: m.text == "🖼 Фото товара")
async def site_photos_handler(message: Message, state: FSMContext):
    await message.answer("Здесь будет управление фото товара.")


@router.message(lambda m: m.text == "🌐 Язык сайта")
async def site_language_handler(message: Message, state: FSMContext):
    await message.answer("Здесь будет настройка языка сайта RU / UA.")


@router.message(lambda m: m.text == "📊 Аналитика сайта")
async def site_analytics_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    stats = await db.get_site_analytics_today()
    top = await db.get_top_site_products(limit=10)

    views = int(stats["views"] or 0) if stats else 0
    cart_adds = int(stats["cart_adds"] or 0) if stats else 0
    orders = int(stats["orders"] or 0) if stats else 0

    lines = [
        "📊 Аналитика сайта за сегодня\n",
        f"👁 Просмотры товаров: {views}",
        f"🛒 Добавлений в корзину: {cart_adds}",
        f"🧾 Заказов: {orders}",
    ]

    if top:
        lines.append("\n🔥 Популярные товары:")
        for i, row in enumerate(top, start=1):
            lines.append(f"{i}. {row['product_name'].strip()} — {row['views']} просмотров")

    await message.answer("\n".join(lines), reply_markup=site_kb)



@router.message(lambda m: m.text in {"👤 Клиенты", "👤 Клієнти", "📋 Заявки/Покупатели", "📋 Заявки/Покупці"})
async def customers_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Раздел клиентов:",
        reply_markup=customers_kb
    )

@router.message(lambda m: m.text in {"📈 Отчёты", "📊 Отчёты", "📊 Звіти"})
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
    await message.answer(await t(message, "main_menu"), reply_markup=menu)


@router.message(lambda m: m.text == "➕ Добавить товар")
async def add_product_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    if not await require_active_subscription(message):
        return

    await state.set_state(AddProductState.waiting_for_category)
    await message.answer(
        "Выберите категорию:",
        reply_markup=inline_categories_kb()
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
        await message.answer(await t(message, "main_menu"), reply_markup=menu)
        return

    await state.update_data(category=category)
    await state.set_state(AddProductState.waiting_for_brand)

    await message.answer(
        "Выберите бренд:",
        reply_markup=brands_kb
    )


@router.callback_query(lambda c: c.data and c.data.startswith("add_category:"))
async def add_category_callback(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split(":", 1)[1]

    await state.update_data(category=category)
    await state.set_state(AddProductState.waiting_for_brand)

    await callback.message.answer(
        f"Категория: {category}\n\nВыберите бренд:",
        reply_markup=inline_brands_kb()
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "add_category_search")
async def add_category_search_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddProductState.searching_category)
    await callback.message.answer("Введите часть названия категории:")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("add_brand:"))
async def add_brand_callback(callback: CallbackQuery, state: FSMContext):
    brand = callback.data.split(":", 1)[1]

    await state.update_data(brand=brand)
    await state.set_state(AddProductState.waiting_for_model)

    await callback.message.answer(
        f"Бренд: {brand}\n\nВведите модель:"
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "add_brand_manual")
async def add_brand_manual_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddProductState.waiting_for_brand_manual)
    await callback.message.answer("Введите бренд вручную:")
    await callback.answer()


@router.callback_query(lambda c: c.data == "add_brand_search")
async def add_brand_search_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddProductState.searching_brand)
    await callback.message.answer("Введите часть названия бренда:")
    await callback.answer()


@router.message(AddProductState.searching_category)
async def search_category_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()

    categories = [
        "Телевизоры", "Холодильники", "Стиральные машины", "Смартфоны", "Ноутбуки",
        "Пылесосы", "Микроволновки", "Плиты", "Утюги", "Кофемашины"
    ]

    found = [c for c in categories if query in c.lower()]

    if not found:
        await message.answer(await t(message, "no_products_found"))
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=c, callback_data=f"add_category:{c}")]
            for c in found
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
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
        await message.answer("Выберите категорию:", reply_markup=inline_categories_kb())
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
        await message.answer(await t(message, "no_products_found"))
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b, callback_data=f"add_brand:{b}")]
            for b in found
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
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
        await message.answer(await t(message, "enter_number"))
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
        f"{await t(message, 'price')}: {data['price']} грн\n"
        f"Закупка: {data.get('purchase_price', 0)} {data.get('currency', 'UAH')}\n"
        f"{await t(message, 'warranty')}: {warranty} мес",
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
            f"{await t(message, 'price')}: {price:.2f} грн\n"
            f"Закупка: {purchase_price:.2f} {purchase_currency}\n"
            f"Артикул: {sku}\n"
            f"{await t(message, 'warranty')}: {warranty_months} мес\n"
            f"{await t(message, 'stock')}: {stock_qty} шт\n"
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


@router.message(lambda m: m.text == "❌ Сброс")
async def reset_state_handler(message: Message, state: FSMContext):
    await state.finish()
    await message.answer("Состояние сброшено.", reply_markup=menu_kb)


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


@router.message(lambda m: m.text in {"➕ Приход", "➕ Прихід"})
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
        await message.answer(await t(message, "no_products_found"))
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{row['brand'] or '-'} {row['model'] or '-'} | Остаток: {row['stock_qty'] or 0} шт",
                    callback_data=f"receipt_product:{row['id']}"
                )
            ]
            for row in rows
        ]
    )

    await state.set_state(ReceiptState.waiting_for_product_id)
    await message.answer("Выберите товар для прихода:", reply_markup=keyboard)


@router.callback_query(lambda c: c.data and c.data.startswith("receipt_product:"))
async def receipt_product_callback_handler(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product_by_id(product_id)

    if not product:
        await callback.message.answer(await t(callback.message, "product_not_found"))
        await callback.answer()
        return

    await state.update_data(product_id=product_id)
    await state.set_state(ReceiptState.waiting_for_qty)

    await callback.message.answer(
        f"Товар:\n"
        f"{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"Текущий остаток: {product['stock_qty']} шт\n\n"
        "Введите количество для прихода:"
    )

    await callback.answer()


@router.message(ReceiptState.waiting_for_product_id)
async def receipt_product_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("Введите корректный ID товара")
        return

    product_id = int(raw_id)
    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer(await t(message, "product_not_found"))
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
        await message.answer(await t(message, "product_not_found"), reply_markup=menu_kb)
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


@router.message(lambda m: m.text in {"🛒 Продажа", "🛒 Продаж"})
async def sale_start_handler(message: Message, state: FSMContext):
    await state.set_state(SaleState.waiting_for_query)
    await message.answer(await t(message, "enter_search"))


@router.message(SaleState.waiting_for_query)
async def sale_search_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip()

    rows = await db.search_products(query)

    if not rows:
        await message.answer(await t(message, "no_products_found"))
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{row['brand'] or '-'} {row['model'] or '-'} | {float(row['price'] or 0):.0f} грн | {row['stock_qty'] or 0} шт",
                    callback_data=f"sale_product:{row['id']}"
                )
            ]
            for row in rows
        ]
    )

    await state.set_state(SaleState.waiting_for_product_id)
    await message.answer(await t(message, "choose_product"), reply_markup=keyboard)


@router.message(SaleState.waiting_for_product_id)
async def sale_product_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("Введите корректный ID товара")
        return

    product_id = int(raw_id)
    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer(await t(message, "product_not_found"))
        return

    await state.update_data(product_id=product_id)
    await state.set_state(SaleState.waiting_for_qty)

    await message.answer(
        f"Товар:\n{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"{await t(message, 'price')}: {float(product['price']):.2f} грн\n"
        f"{await t(message, 'stock')}: {product['stock_qty']}\n\n"
        f"{await t(message, 'enter_quantity')}"
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sale_product:"))
async def sale_product_callback_handler(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])

    product = await db.get_product_by_id(product_id)

    if not product:
        await callback.message.answer(await t(callback.message, "product_not_found"))
        await callback.answer()
        return

    await state.update_data(product_id=product_id)
    await state.set_state(SaleState.waiting_for_qty)

    await callback.message.answer(
        f"Товар:\n{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"{await t(callback.message, 'price')}: {float(product['price']):.2f} грн\n"
        f"{await t(callback.message, 'stock')}: {product['stock_qty']}\n\n"
        f"{await t(callback.message, 'enter_quantity')}"
    )

    await callback.answer()


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
        await message.answer(await t(message, "product_not_found"), reply_markup=menu_kb)
        return

    if qty > product["stock_qty"]:
        await message.answer(await t(message, "not_enough_stock"))
        return

    await state.update_data(qty=qty)
    await state.set_state(SaleState.waiting_for_customer_phone)
    await message.answer(await t(message, "enter_client_phone"))


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
            await message.answer(await t(message, "product_not_found"), reply_markup=menu_kb)
            return

        price = float(product["price"])
        sale_result = await db.create_sale(product_id, qty, price, customer["id"])
        total = sale_result["total"]
        sale_id = sale_result["sale_id"]

        warranty_months = int(product["warranty_months"] or 0)
        if warranty_months > 0:
            await db.create_warranty(
                sale_id=sale_id,
                product_id=product_id,
                customer_id=customer["id"],
                warranty_months=warranty_months
            )

        new_stock = product["stock_qty"] - qty
        await db.update_stock_qty(product_id, new_stock)

        await state.clear()
        await message.answer(
            await t(message, "sale_done") + "\n\n"
            f"Клиент: {customer['name']} | {customer['phone']} | {customer['city'] or '-'}\n"
            f"Товар: {product['brand'] or '-'} {product['model'] or '-'}\n"
            f"Количество: {qty}\n"
            f"Сумма: {total:.2f} грн\n"
            f"{await t(message, 'stock_available')}: {new_stock} шт",
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
        await message.answer(await t(message, "product_not_found"), reply_markup=menu_kb)
        return

    price = float(product["price"])
    sale_result = await db.create_sale(product_id, qty, price, customer["id"])
    total = sale_result["total"]
    sale_id = sale_result["sale_id"]

    warranty_months = int(product["warranty_months"] or 0)
    if warranty_months > 0:
        await db.create_warranty(
            sale_id=sale_id,
            product_id=product_id,
            customer_id=customer["id"],
            warranty_months=warranty_months
        )

    new_stock = product["stock_qty"] - qty
    await db.update_stock_qty(product_id, new_stock)

    await state.clear()
    await message.answer(
        await t(message, "sale_done") + "\n\n"
        f"Клиент: {customer['name']} | {customer['phone']} | {customer['city'] or '-'}\n"
        f"Товар: {product['brand'] or '-'} {product['model'] or '-'}\n"
        f"Количество: {qty}\n"
        f"Сумма: {total:.2f} грн\n"
        f"{await t(message, 'stock_available')}: {new_stock} шт",
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


@router.message(lambda m: m.text in {"💱 Курсы валют", "💱 Курс валют"})
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
        await message.answer(await t(message, "main_menu"), reply_markup=menu)
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
        status = "✅ активен" if row.get("is_active", True) else "🚫 отключён"
        lines.append(
            f"ID: {row['id']}\n"
            f"Telegram ID: {row['telegram_id']}\n"
            f"Имя: {row['full_name'] or '-'}\n"
            f"Роль: {row['role']}\n"
            f"Статус: {status}\n"
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


@router.message(lambda m: m.text == "➕ Добавить админа")
async def add_admin_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.set_state(AddAdminState.waiting_for_tg_id)
    await message.answer("Введите Telegram ID пользователя, которого нужно сделать админом:")


@router.message(AddAdminState.waiting_for_tg_id)
async def add_admin_finish_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if raw_id == "⬅️ Назад":
        await state.clear()
        await message.answer("Раздел пользователей:", reply_markup=users_kb)
        return

    if not raw_id.isdigit():
        await message.answer("Telegram ID должен быть числом. Введите ещё раз:")
        return

    telegram_id = int(raw_id)
    await db.add_admin_by_telegram_id(telegram_id)
    await state.clear()
    await message.answer(
        f"✅ Админ добавлен\n\nTelegram ID: {telegram_id}",
        reply_markup=users_kb
    )


@router.message(lambda m: m.text == "❌ Удалить пользователя")
async def delete_user_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return

    await state.set_state(DeleteUserState.waiting_for_tg_id)
    await message.answer("Введите Telegram ID пользователя, которого нужно отключить:")


@router.message(DeleteUserState.waiting_for_tg_id)
async def delete_user_finish_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if raw_id == "⬅️ Назад":
        await state.clear()
        await message.answer("Раздел пользователей:", reply_markup=users_kb)
        return

    if not raw_id.isdigit():
        await message.answer("Telegram ID должен быть числом. Введите ещё раз:")
        return

    telegram_id = int(raw_id)

    if telegram_id == message.from_user.id:
        await message.answer("❌ Нельзя отключить самого себя.")
        return

    user = await db.get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("Пользователь с таким Telegram ID не найден.")
        return

    if user["role"] == "admin":
        active_admins = await db.count_active_admins()
        if active_admins <= 1:
            await message.answer("❌ Нельзя отключить последнего активного администратора.")
            return

    await db.deactivate_user_by_telegram_id(telegram_id)
    await state.clear()
    await message.answer(
        f"✅ Пользователь отключён\n\n"
        f"Telegram ID: {telegram_id}\n"
        f"Имя: {user['full_name'] or '-'}",
        reply_markup=users_kb
    )



@router.message(lambda m: m.text == "✏️ Редактировать товар")
async def edit_product_start_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    if not await require_active_subscription(message):
        return

    await state.set_state(EditProductState.waiting_for_query)
    await message.answer(await t(message, "enter_search"))


@router.message(EditProductState.waiting_for_query)
async def edit_product_search_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip()

    rows = await db.search_products(query)

    if not rows:
        await message.answer(await t(message, "no_products_found"))
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{row['brand'] or '-'} {row['model'] or '-'} | {float(row['price'] or 0):.0f} грн | {row['stock_qty'] or 0} шт",
                    callback_data=f"edit_product:{row['id']}"
                )
            ]
            for row in rows
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
    )

    await state.set_state(EditProductState.waiting_for_product_id)
    await message.answer(await t(message, "choose_product"), reply_markup=keyboard)



@router.callback_query(lambda c: c.data and c.data.startswith("edit_product:"))
async def edit_product_callback_handler(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product_by_id(product_id)

    if not product:
        await callback.message.answer(await t(callback.message, "product_not_found"))
        await callback.answer()
        return

    await state.update_data(product_id=product_id)
    await state.set_state(EditProductState.waiting_for_field)

    visibility = "🙈 Скрыт" if not product.get("is_active", True) else "✅ Активен"
    deleted = " | 🗑 Удалён" if product.get("deleted_at") else ""

    await callback.message.answer(
        f"Товар:\n"
        f"ID: {product['id']}\n"
        f"{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"{await t(callback.message, 'price')}: {float(product['price'] or 0):.2f} грн\n"
        f"Закупка: {float(product['purchase_price'] or 0):.2f} {product['purchase_currency'] or 'UAH'}\n"
        f"Артикул: {product['sku'] or '-'}\n"
        f"{await t(callback.message, 'warranty')}: {product['warranty_months'] or 0} мес\n"
        f"Статус: {visibility}{deleted}\n\n"
        "Что изменить?",
        reply_markup=inline_edit_fields_kb(product)
    )

    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("edit_action:"))
async def edit_action_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    data = await state.get_data()
    product_id = data.get("product_id")

    if not product_id:
        await callback.answer("Нет выбранного товара.")
        return

    if action == "change_category":
        categories = await db.get_categories()
        if not categories:
            await callback.answer("Категории не найдены.")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=cat, callback_data=f"set_category:{cat}")]
                for cat in categories
            ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
        )
        await state.set_state(EditProductState.waiting_for_category)
        await callback.message.answer("Выберите новую категорию:", reply_markup=keyboard)
        await callback.answer()
        return

    if action == "remove_photo":
        await db.remove_product_photo(product_id)
        await state.clear()
        await callback.message.answer("🗑 Фото удалено.", reply_markup=products_kb)
        await callback.answer()
        return

    if action == "manage_photos":
        await show_product_photos_manager(callback.message, product_id)
        await callback.answer()
        return

    if action == "toggle_sale":
        product = await db.get_product_by_id(product_id)
        if not product:
            await callback.answer("Товар не найден.")
            return
        new_value = not bool(product.get("is_sale"))
        await db.update_product_field(product_id, "is_sale", new_value)
        product = await db.get_product_by_id(product_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=inline_edit_fields_kb(product))
        except Exception:
            pass
        await callback.answer("🔥 Акция включена" if new_value else "Акция выключена")
        return

    if action == "cycle_stock_status":
        product = await db.get_product_by_id(product_id)
        if not product:
            await callback.answer("Товар не найден.")
            return
        order = ["in_stock", "preorder", "out_of_stock"]
        current = product.get("stock_status") or "in_stock"
        try:
            idx = order.index(current)
        except ValueError:
            idx = 0
        new_status = order[(idx + 1) % len(order)]
        await db.update_product_field(product_id, "stock_status", new_status)
        product = await db.get_product_by_id(product_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=inline_edit_fields_kb(product))
        except Exception:
            pass
        labels = {
            "in_stock": "🟢 В наличии",
            "preorder": "🟡 Под заказ",
            "out_of_stock": "🔴 Нет в наличии",
        }
        await callback.answer(f"Статус: {labels[new_status]}")
        return

    if action == "hide_product":
        await db.hide_product(product_id)
        await state.clear()
        await callback.message.answer("👁 Товар скрыт с сайта.", reply_markup=products_kb)
        await callback.answer()
        return

    if action == "set_ten_type":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="💧 Мокрый ТЕН", callback_data="set_ten:wet"),
                    InlineKeyboardButton(text="✨ Сухой ТЕН", callback_data="set_ten:dry"),
                ],
                [InlineKeyboardButton(text="❌ Очистить", callback_data="set_ten:clear")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back_to_edit:{product_id}")],
            ]
        )
        await callback.message.answer("Выберите тип ТЕНа:", reply_markup=kb)
        await callback.answer()
        return

    if action == "show_product":
        await db.show_product(product_id)
        await state.clear()
        await callback.message.answer("✅ Товар снова виден на сайте.", reply_markup=products_kb)
        await callback.answer()
        return

    if action == "soft_delete":
        await db.soft_delete_product(product_id)
        await state.clear()
        await callback.message.answer("🗑 Товар удалён (скрыт из базы).", reply_markup=products_kb)
        await callback.answer()
        return

    await callback.answer()


async def show_product_photos_manager(message: Message, product_id: int):
    images = await db.get_product_images(product_id)
    product = await db.get_product_by_id(product_id)

    # backward compat: if no rows in product_images, but product.photo_url exists,
    # show a single legacy entry that deletes via remove_product_photo
    if not images and product and product.get("photo_url"):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🗑 Фото 1 (основное)",
                    callback_data=f"del_main_photo:{product_id}"
                )],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back_to_edit:{product_id}")],
            ]
        )
        await message.answer(
            f"🖼 Фото товара #{product_id}: 1 шт.\nВыберите, какое удалить:",
            reply_markup=kb
        )
        return

    if not images:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back_to_edit:{product_id}")],
            ]
        )
        await message.answer(
            f"🖼 У товара #{product_id} нет фото.",
            reply_markup=kb
        )
        return

    rows = []
    main_url = (product.get("photo_url") if product else None) or (images[0]["image_url"] if images else None)
    for idx, img in enumerate(images, start=1):
        is_main = img["image_url"] == main_url
        main_label = f"✅ Фото {idx} (главное)" if is_main else f"⭐ Сделать главным (Фото {idx})"
        rows.append([
            InlineKeyboardButton(
                text=main_label,
                callback_data=f"set_main_image:{img['id']}"
            ),
            InlineKeyboardButton(
                text=f"🗑 Фото {idx}",
                callback_data=f"del_image:{img['id']}"
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back_to_edit:{product_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        f"🖼 Фото товара #{product_id}: {len(images)} шт.\nВыберите, какое удалить:",
        reply_markup=kb
    )


@router.callback_query(lambda c: c.data and c.data.startswith("del_image:"))
async def del_image_callback(callback: CallbackQuery, state: FSMContext):
    try:
        image_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID фото.")
        return

    img = await db.get_product_image_by_id(image_id)
    if not img:
        await callback.answer("Фото не найдено.")
        return

    product_id = img["product_id"]
    image_url = img["image_url"]

    await db.delete_product_image(image_id)

    # sync legacy products.photo_url: if removed image was the main one,
    # set photo_url to next remaining image or clear it
    product = await db.get_product_by_id(product_id)
    if product and product.get("photo_url") == image_url:
        remaining = await db.get_product_images(product_id)
        new_main = remaining[0]["image_url"] if remaining else None
        if new_main:
            await db.update_product_field(product_id, "photo_url", new_main)
        else:
            await db.remove_product_photo(product_id)

    await callback.answer("🗑 Фото удалено")
    await show_product_photos_manager(callback.message, product_id)


@router.callback_query(lambda c: c.data and c.data.startswith("set_main_image:"))
async def set_main_image_callback(callback: CallbackQuery, state: FSMContext):
    try:
        image_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID фото.")
        return

    img = await db.set_main_product_image(image_id)
    if not img:
        await callback.answer("Фото не найдено.")
        return

    await callback.answer("⭐ Фото установлено главным")
    await show_product_photos_manager(callback.message, img["product_id"])


@router.callback_query(lambda c: c.data and c.data.startswith("del_main_photo:"))
async def del_main_photo_callback(callback: CallbackQuery, state: FSMContext):
    try:
        product_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID товара.")
        return

    await db.remove_product_photo(product_id)
    await callback.answer("🗑 Фото удалено")
    await show_product_photos_manager(callback.message, product_id)


@router.callback_query(lambda c: c.data and c.data.startswith("back_to_edit:"))
async def back_to_edit_callback(callback: CallbackQuery, state: FSMContext):
    try:
        product_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID товара.")
        return

    product = await db.get_product_by_id(product_id)
    if not product:
        await callback.answer("Товар не найден.")
        return

    await state.update_data(product_id=product_id)
    await state.set_state(EditProductState.waiting_for_field)

    await callback.message.answer(
        f"Товар:\n"
        f"ID: {product['id']}\n"
        f"{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"{await t(callback.message, 'price')}: {float(product['price'] or 0):.2f} грн\n"
        f"Закупка: {float(product['purchase_price'] or 0):.2f} {product['purchase_currency'] or 'UAH'}\n"
        f"Артикул: {product['sku'] or '-'}\n"
        f"{await t(callback.message, 'warranty')}: {product['warranty_months'] or 0} мес\n\n"
        "Что изменить?",
        reply_markup=inline_edit_fields_kb(product)
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("set_ten:"))
async def set_ten_callback(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    data = await state.get_data()
    product_id = data.get("product_id")

    if not product_id:
        await callback.answer("Нет выбранного товара.")
        return

    if choice == "clear":
        new_value = None
        label = "очищен"
    elif choice in {"wet", "dry"}:
        new_value = choice
        label = "💧 Мокрый ТЕН" if choice == "wet" else "✨ Сухой ТЕН"
    else:
        await callback.answer("Неизвестный тип.")
        return

    await db.update_product_field(product_id, "boiler_ten_type", new_value)
    await callback.answer(f"Тип ТЕНа: {label}")

    product = await db.get_product_by_id(product_id)
    await callback.message.answer(
        f"✅ Тип ТЕНа обновлён.",
        reply_markup=None
    )
    # show edit card again
    await callback.message.answer(
        f"Товар:\n"
        f"ID: {product['id']}\n"
        f"{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n\n"
        "Что изменить?",
        reply_markup=inline_edit_fields_kb(product)
    )


@router.callback_query(lambda c: c.data and c.data.startswith("set_category:"))
async def set_category_callback(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split(":", 1)[1]
    data = await state.get_data()
    product_id = data.get("product_id")

    if not product_id:
        await callback.answer("Нет выбранного товара.")
        return

    await db.update_product_category(product_id, category)
    await state.clear()
    await callback.message.answer(
        f"✅ Категория обновлена: {category}",
        reply_markup=products_kb
    )
    await callback.answer()


@router.message(lambda m: m.text == "🔍 Найти товар")
async def find_product_start(message: Message, state: FSMContext):
    await state.set_state(FindProductState.waiting_for_query)
    await message.answer(await t(message, "enter_product_search"))


@router.message(FindProductState.waiting_for_query)
async def find_product_search(message: Message, state: FSMContext):
    query = (message.text or "").strip()

    rows = await db.search_products(query)

    if not rows:
        await message.answer(await t(message, "no_products_found"))
        return

    lines = ["Найдено:\n"]

    for row in rows:
        lines.append(
            f"{row['id']}. {row['category'] or '-'} | {row['brand'] or '-'} | {row['model'] or '-'} | "
            f"{float(row['price'] or 0):.2f} грн | Остаток: {row['stock_qty'] or 0} шт"
        )

    await state.set_state(FindProductState.waiting_for_product_id)
    await message.answer("\n".join(lines) + "\n\nВведите ID товара:")


@router.message(FindProductState.waiting_for_product_id)
async def find_product_show(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("Введите ID товара числом.")
        return

    product_id = int(raw_id)
    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer(await t(message, "product_not_found"))
        return

    await state.clear()

    await message.answer(
        f"📦 Товар\n\n"
        f"ID: {product['id']}\n"
        f"{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n\n"
        f"{await t(message, 'price')}: {float(product['price'] or 0):.2f} грн\n"
        f"Закупка: {float(product['purchase_price'] or 0):.2f} {product['purchase_currency'] or 'UAH'}\n"
        f"Артикул: {product['sku'] or '-'}\n"
        f"{await t(message, 'warranty')}: {product['warranty_months'] or 0} мес\n"
        f"{await t(message, 'stock')}: {product['stock_qty'] or 0} шт",
        reply_markup=products_kb
    )



@router.message(EditProductState.waiting_for_product_id)
async def edit_product_id_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("ID товара должен быть числом.")
        return

    product_id = int(raw_id)
    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer(f"{await t(message, 'product_not_found')} Введите другой ID:")
        return

    await state.update_data(product_id=product_id)
    await state.set_state(EditProductState.waiting_for_field)

    await message.answer(
        f"Товар:\n"
        f"ID: {product['id']}\n"
        f"{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n"
        f"{await t(message, 'price')}: {float(product['price'] or 0):.2f} грн\n"
        f"Закупка: {float(product['purchase_price'] or 0):.2f} {product['purchase_currency'] or 'UAH'}\n"
        f"Артикул: {product['sku'] or '-'}\n"
        f"{await t(message, 'warranty')}: {product['warranty_months'] or 0} мес\n\n"
        "Что изменить?",
        reply_markup=inline_edit_fields_kb()
    )


@router.message(EditProductState.waiting_for_value)
async def edit_product_value_handler(message: Message, state: FSMContext):
    # allow cancelling photo upload flow
    if (message.text or "").strip() == "⬅️ Назад":
        await state.clear()
        await message.answer(await t(message, "products_section"), reply_markup=products_kb)
        return

    # finish photo upload flow explicitly
    if (message.text or "").strip() == "✅ Готово с фото":
        await state.clear()
        await message.answer("Готово. Возвращаюсь в меню товаров.", reply_markup=products_kb)
        return

    if message.photo:
        data = await state.get_data()
        field = data.get("field")

        if field != "photo_url":
            await message.answer("Это поле не для фото.")
            return

        product_id = data.get("product_id")

        # PRE-CHECK: enforce 6-photo limit before Cloudinary upload
        if await db.count_product_images_total(product_id) >= 6:
            await message.answer(
                "⚠️ Максимум 6 фото для одного товара. Удалите одно фото, чтобы добавить новое."
            )
            return

        product = await db.get_product_by_id(product_id)
        legacy = product.get("photo_url") if product else None

        file_id = message.photo[-1].file_id
        photo_url = await save_telegram_photo(message.bot, file_id)

        # ATOMIC add with limit check (handles race when sending media group)
        inserted_id = await db.add_product_image_if_under_limit(product_id, photo_url, limit=6)
        if inserted_id is None:
            await message.answer(
                "⚠️ Максимум 6 фото для одного товара. Удалите одно фото, чтобы добавить новое."
            )
            return

        # Update main photo_url only if not set yet (don't overwrite chosen main)
        if not legacy:
            await db.update_product_field(product_id, "photo_url", photo_url)

        # Do not clear state — allow sending multiple photos
        await message.answer("✅ Фото сохранено. Можно отправить ещё или нажмите ⬅️ Назад.", reply_markup=products_kb)
        return

    value = (message.text or "").strip()
    data = await state.get_data()

    product_id = data["product_id"]
    field = data["field"]
    field_title = data.get("field_title", field)

    if field in {"price", "purchase_price"}:
        try:
            value = float(value.replace(",", "."))
        except ValueError:
            await message.answer(await t(message, "enter_number"))
            return

    elif field == "old_price":
        if value == "-" or value == "":
            value = None
        else:
            try:
                value = float(value.replace(",", "."))
            except ValueError:
                await message.answer(await t(message, "enter_number"))
                return

    elif field == "warranty_months":
        if not value.isdigit():
            await message.answer("Введите число месяцев.")
            return
        value = int(value)

    elif field == "purchase_currency":
        value = value.upper()
        if value not in {"UAH", "USD", "EUR"}:
            await message.answer("Валюта должна быть UAH, USD или EUR.")
            return

    elif field == "sku":
        if value == "-":
            value = None

    elif field == "boiler_volume_liters":
        if value == "-" or value == "":
            value = None
        else:
            if not value.isdigit():
                await message.answer("Введите объём бойлера числом (например, 80).")
                return
            value = int(value)
            if value <= 0 or value > 1000:
                await message.answer("Объём должен быть от 1 до 1000 литров.")
                return

    await db.update_product_field(product_id, field, value)

    product = await db.get_product_by_id(product_id)
    await state.clear()

    if field == "boiler_volume_liters":
        if value is None:
            await message.answer("✅ Объём бойлера очищен")
        else:
            await message.answer(f"✅ Объём бойлера сохранён: {value} л")
        await state.update_data(product_id=product_id)
        await state.set_state(EditProductState.waiting_for_field)
        await message.answer(
            f"Товар:\n"
            f"ID: {product['id']}\n"
            f"{product['category'] or '-'} | {product['brand'] or '-'} | {product['model'] or '-'}\n\n"
            "Что изменить?",
            reply_markup=inline_edit_fields_kb(product)
        )
        return

    await message.answer(
        await t(message, "product_updated") + "\n\n"
        f"ID: {product['id']}\n"
        f"{product['brand'] or '-'} {product['model'] or '-'}\n"
        f"Изменено: {field_title}",
        reply_markup=products_kb
    )


@router.message(lambda m: m.text not in {
    "📦 Товары", "🛒 Продажа", "❌ Отмена продажи", "🧾 История продаж", "👤 Клиенты",
    "👥 Пользователи", "📋 Список пользователей", "🔁 Изменить роль",
    "➕ Добавить админа", "❌ Удалить пользователя",
    "➕ Добавить товар", "📋 Список товаров", "✏️ Изменить остаток", "➕ Приход",
    "📋 Список клиентов", "🔍 Найти клиента", "📥 История приходов", "⚠️ Мало остатков", "✏️ Редактировать товар", "🔍 Найти товар", "⬅️ Назад",
    "📈 Отчёты", "📅 Отчёт за сегодня", "📆 Отчёт за месяц",
    "💰 Прибыль", "💰 Прибыль за сегодня", "💰 Прибыль за месяц",
    "💱 Курсы валют", "USD", "EUR",
    "Цена продажи", "Закупка", "Валюта закупки", "Артикул", "Гарантия", "Модель",
    "admin", "seller", "❌ Сброс",
    "📂 Категории сайта", "📞 Контакты сайта", "🌐 Язык сайта", "📋 Показать категории сайта", "➕ Холодильники", "➕ Стиральные машины", "➕ Кондиционеры", "➕ Нагреватели", "➕ Своя категория", "👁 Вкл/выкл категорию",
    "👀 Просмотр товара на сайте",
    "📋 Показать контакты", "📞 Телефон", "💬 Telegram", "📷 Instagram", "📍 Адрес", "⏰ График работы",
    "🧢 Шапка сайта",
    "🛒 Корзина: вкл/выкл",
    "📞 Контакты: вкл/выкл",
    "🌐 Язык: вкл/выкл",
    "📊 Аналитика сайта",
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


@router.message(lambda m: m.text == "⚠️ Мало остатков")
async def low_stock_handler(message: Message):
    if not await require_admin(message):
        return

    rows = await db.list_low_stock_products(limit_qty=2)

    if not rows:
        await message.answer("✅ Товаров с низким остатком нет.", reply_markup=products_kb)
        return

    lines = ["⚠️ Мало остатков:\n"]

    for row in rows:
        lines.append(
            f"{row['id']}. {row['category'] or '-'} | {row['brand'] or '-'} | {row['model'] or '-'}\n"
            f"{await t(message, 'price')}: {float(row['price'] or 0):.2f} грн | {await t(message, 'stock')}: {row['stock_qty'] or 0} шт\n"
        )

    await message.answer("\n".join(lines), reply_markup=products_kb)


@router.callback_query(lambda c: c.data and c.data.startswith("order_product:"))
async def order_product_callback_handler(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product_by_id(product_id)

    if not product:
        await callback.message.answer(await t(callback.message, "product_not_found"))
        await callback.answer()
        return

    await state.update_data(product_id=product_id, price=float(product["price"] or 0))
    await state.set_state(OrderState.waiting_for_qty)

    await callback.message.answer(
        f"Товар: {product['brand'] or '-'} {product['model'] or '-'}\n"
        f"{await t(callback.message, 'price')}: {float(product['price'] or 0):.2f} грн\n\n"
        "Введите количество:"
    )

    await callback.answer()

@router.message(OrderStatusState.waiting_for_order_id)
@router.message(StateFilter(OrderStatusState), lambda m: m.text == "⬅️ Назад")
async def order_status_back_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await t(message, "orders_section"), reply_markup=orders_kb)

@router.callback_query(lambda c: c.data and c.data.startswith("order_status:"))
async def order_status_callback_handler(callback: CallbackQuery):
    _, order_id_raw, status = callback.data.split(":")
    order_id = int(order_id_raw)

    if status not in {"new", "processing", "ordered_supplier", "in_transit", "ready", "done", "cancelled"}:
        await callback.answer("Неверный статус")
        return

    await db.update_order_status(order_id, status)

    status_map = {
        "new": "Новый",
        "processing": "В обработке",
        "ordered": "Заказан у поставщика",
        "ordered_supplier": "Заказан у поставщика",
        "in_transit": "В пути",
        "ready": "Готов",
        "done": "Выполнен",
        "cancelled": "Отменён",
    }

    status_ru = status_map.get(status, status)

    order = await db.get_order(order_id)

    if not order:
        await callback.message.answer(f"{await t(callback.message, 'order_status_updated')} #{order_id}: {status_ru}")
        await callback.answer()
        return

    await callback.message.answer(f"""
✅ Заказ обновлён

🧾 Заказ #{order['id']}
👤 {order['name'] or '-'} | {order['phone'] or '-'}
🏙 {order['city'] or '-'}

📦 Товар: {order['product_name'] or '-'}
💰 {order['total_price'] or 0} грн

📍 Новый статус: {status_ru}
""")

    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("order_to_sale:"))
async def order_to_sale_handler(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])

    order = await db.get_order_full_by_id(order_id)

    if not order:
        await callback.answer(await t(callback.message, "order_not_found"))
        return

    if order["status"] == "done":
        await callback.answer("Уже выполнен")
        return

    stock = int(order.get("stock_qty") or 0)
    qty = int(order.get("qty") or 0)

    if stock < qty:
        await callback.message.answer(
            await t(callback.message, "not_enough_stock") + "\n"
            f"{await t(callback.message, 'stock_available')}: {stock}\n"
            f"{await t(callback.message, 'need_qty')}: {qty}\n\n"
            f"Используй статусы:\n📦 Заказан у поставщика\n🚚 В пути"
        )
        await callback.answer()
        return

    sale = await db.create_sale(
        product_id=order["product_id"],
        qty=qty,
        price=float(order["price"]),
        customer_id=order["customer_id"]
    )

    new_stock = stock - qty
    await db.update_stock_qty(order["product_id"], new_stock)

    await db.update_order_status(order_id, "done")

    await callback.message.answer(
        f"✅ Заказ #{order_id} оформлен как продажа\n"
        f"{order.get('brand') or ''} {order.get('model') or ''}\n"
        f"Кол-во: {qty}"
    )

    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("site_preview_product:"))
async def site_product_preview_callback(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])

    product = await db.get_product_by_id(product_id)
    if not product:
        await callback.message.answer("Товар не найден.")
        await callback.answer()
        return

    await state.clear()

    base_url = os.getenv("PUBLIC_SITE_URL", "").rstrip("/")
    if not base_url:
        base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").rstrip("/")
        if base_url and not base_url.startswith("http"):
            base_url = "https://" + base_url

    if not base_url:
        base_url = "https://techbot-production-11c5.up.railway.app"

    url = f"{base_url}/product/{product_id}"

    await callback.message.answer(
        f"👀 Карточка товара на сайте:\n\n"
        f"{product['brand'] or '-'} {product['model'] or '-'}\n"
        f"{url}",
        reply_markup=site_kb
    )

    await callback.answer()
@web_app.get("/health")
async def health():
    return {"status": "ok"}


@web_app.post("/api/site-order")
async def create_site_order(data: SiteOrderRequest):
    product = await db.get_product_by_id(data.product_id)

    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    phone = normalize_phone(data.phone)

    customer = await db.get_customer_by_phone(phone)

    if not customer:
        customer = await db.create_customer(
            name=data.name,
            phone=phone,
            city=data.city or "-"
        )

    qty = data.qty if data.qty > 0 else 1
    price = float(product["price"] or 0)
    total = qty * price

    order = await db.create_order(
        customer_id=customer["id"],
        product_id=data.product_id,
        qty=qty,
        total_amount=total,
        comment=data.comment
    )

    if telegram_bot:
        await notify_admins(
            "🛒 Новый заказ с сайта\n\n"
            f"📅 {now_kyiv_str()}\n\n"
            f"ID заказа: {order['id']}\n"
            f"Клиент: {data.name}\n"
            f"Телефон: {phone}\n"
            f"Город: {data.city or '-'}\n\n"
            f"Товар: {product['brand'] or '-'} {product['model'] or '-'}\n"
            f"Количество: {qty}\n"
            f"Сумма: {total:.2f} грн\n\n"
            f"Комментарий: {data.comment or '-'}"
        )

    return {
        "ok": True,
        "order_id": order["id"],
        "total": total
    }


@web_app.post("/api/site-event")
async def api_site_event(data: SiteEventRequest):
    allowed = {"product_view", "add_to_cart", "site_order"}
    event_type = (data.event_type or "").strip()
    if event_type not in allowed:
        return {"ok": False, "error": "unknown_event"}
    try:
        await db.add_site_event(event_type, data.product_id)
    except Exception:
        pass
    return {"ok": True}


@web_app.get("/", response_class=HTMLResponse)
async def site_home(request: Request, q: str = "", category: str = "", page: int = 1, brand: str = "", price_min: str = "", price_max: str = "", in_stock: str = ""):
    q = (q or "").strip()
    category = (category or "").strip()
    brand = (brand or "").strip()
    price_min = (price_min or "").strip()
    price_max = (price_max or "").strip()

    if q:
        products = await db.search_site_products(q)
    else:
        products = await db.list_site_products()

    brands = sorted(set([p["brand"] for p in products if p["brand"]]))

    if category:
        # category aliases — let "Нагреватели" / "Нагрівачі" cover boiler-like
        heater_aliases = {"Нагреватели", "Нагрівачі", "Нагреватель", "Бойлер", "Бойлеры", "Бойлери", "Водонагреватель", "Водонагрівач"}
        if category in heater_aliases:
            allowed = heater_aliases
            products = [p for p in products if (p["category"] or "") in allowed]
        else:
            products = [p for p in products if (p["category"] or "") == category]

    if brand:
        products = [p for p in products if (p["brand"] or "") == brand]

    if price_min:
        try:
            products = [p for p in products if float(p["price"] or 0) >= float(price_min)]
        except ValueError:
            pass

    if price_max:
        try:
            products = [p for p in products if float(p["price"] or 0) <= float(price_max)]
        except ValueError:
            pass

    if in_stock:
        products = [p for p in products if (p["stock_qty"] or 0) > 0]

    per_page = 12
    total = len(products)
    pages = math.ceil(total / per_page) if total else 1
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    products_page = products[start:start + per_page]

    categories = await db.get_categories()
    site_categories = await db.list_active_site_categories()

    site_contacts = {
        "phone": await db.get_setting("site_phone") or "",
        "phones": await get_phones_list(),
        "tg": await db.get_setting("site_tg") or "",
        "instagram": await db.get_setting("site_instagram") or "",
        "address": await db.get_setting("site_address") or "",
        "schedule": await db.get_setting("site_schedule") or "",
    }

    site_title = await db.get_setting("site_title") or "Technovlada"
    site_subtitle = await db.get_setting("site_subtitle") or "Бытовая техника под заказ и в наличии"
    header_show_cart = (await db.get_setting("header_show_cart") or "true") == "true"
    header_show_contacts = (await db.get_setting("header_show_contacts") or "true") == "true"
    header_show_language = (await db.get_setting("header_show_language") or "true") == "true"
    site_design = await get_site_design()

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "products": products_page,
            "categories": categories,
            "site_categories": site_categories,
            "q": q,
            "current_category": category,
            "page": page,
            "pages": pages,
            "brands": brands,
            "current_brand": brand,
            "price_min": price_min,
            "price_max": price_max,
            "in_stock": in_stock,
            "site_contacts": site_contacts,
            "site_title": site_title,
            "site_subtitle": site_subtitle,
            "header_show_cart": header_show_cart,
            "header_show_contacts": header_show_contacts,
            "header_show_language": header_show_language,
            "site_design": site_design,
        }
    )


@web_app.post("/order")
async def site_order_form(
    product_id: int = Form(...),
    name: str = Form(...),
    phone: str = Form(...),
    city: str = Form(""),
    comment: str = Form("")
):
    data = SiteOrderRequest(
        product_id=product_id,
        qty=1,
        name=name,
        phone=phone,
        city=city,
        comment=comment
    )

    result = await create_site_order(data)

    return RedirectResponse(
        url=f"/?order=success&id={result['order_id']}",
        status_code=303
    )


@web_app.get("/product/{product_id}", response_class=HTMLResponse)
async def product_page(request: Request, product_id: int):
    product = await db.get_product_by_id(product_id)

    if not product:
        return HTMLResponse("Товар не найден", status_code=404)

    if not product.get("is_active", True) or product.get("deleted_at") is not None:
        return HTMLResponse("Товар недоступен", status_code=404)

    # track view event (fire-and-forget, do not fail on error)
    try:
        await db.add_site_event("product_view", product_id)
    except Exception:
        pass

    images = await db.get_product_images(product_id)
    site_contacts = {
        "phone": await db.get_setting("site_phone") or "",
        "phones": await get_phones_list(),
        "tg": await db.get_setting("site_tg") or "",
        "instagram": await db.get_setting("site_instagram") or "",
        "address": await db.get_setting("site_address") or "",
        "schedule": await db.get_setting("site_schedule") or "",
    }

    site_title = await db.get_setting("site_title") or "Technovlada"
    site_subtitle = await db.get_setting("site_subtitle") or "Бытовая техника под заказ и в наличии"
    header_show_cart = (await db.get_setting("header_show_cart") or "true") == "true"
    header_show_contacts = (await db.get_setting("header_show_contacts") or "true") == "true"
    header_show_language = (await db.get_setting("header_show_language") or "true") == "true"

    return templates.TemplateResponse(
        request=request,
        name="product.html",
        context={
            "product": product,
            "images": images,
            "site_contacts": site_contacts,
            "site_title": site_title,
            "site_subtitle": site_subtitle,
            "header_show_cart": header_show_cart,
            "header_show_contacts": header_show_contacts,
            "header_show_language": header_show_language,
            "site_design": await get_site_design(),
        }
    )


@web_app.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request):
    site_contacts = {
        "phone": await db.get_setting("site_phone") or "",
        "phones": await get_phones_list(),
        "tg": await db.get_setting("site_tg") or "",
        "instagram": await db.get_setting("site_instagram") or "",
        "address": await db.get_setting("site_address") or "",
        "schedule": await db.get_setting("site_schedule") or "",
    }
    site_title = await db.get_setting("site_title") or "Technovlada"
    site_subtitle = await db.get_setting("site_subtitle") or "Бытовая техника под заказ и в наличии"
    header_show_cart = (await db.get_setting("header_show_cart") or "true") == "true"
    header_show_contacts = (await db.get_setting("header_show_contacts") or "true") == "true"
    header_show_language = (await db.get_setting("header_show_language") or "true") == "true"

    return templates.TemplateResponse(
        request=request,
        name="cart.html",
        context={
            "site_contacts": site_contacts,
            "site_title": site_title,
            "site_subtitle": site_subtitle,
            "header_show_cart": header_show_cart,
            "header_show_contacts": header_show_contacts,
            "header_show_language": header_show_language,
            "site_design": await get_site_design(),
        }
    )


@web_app.post("/api/cart-order")
async def api_cart_order(request: Request):
    data = await request.json()
    name = data.get('name') or '-'
    phone = normalize_phone(data.get('phone') or '')
    city = data.get('city') or '-'
    comment = data.get('comment') or ''
    items = data.get('items') or []

    if not items:
        return {"ok": False, "error": "no_items"}

    customer = await db.get_customer_by_phone(phone)
    if not customer:
        customer = await db.create_customer(name=name, phone=phone, city=city)

    total_sum = 0
    lines = []

    for idx, it in enumerate(items, start=1):
        pid = int(it.get('product_id'))
        qty = int(it.get('qty') or 1)
        prod = await db.get_product_by_id(pid)
        if not prod:
            continue
        price = float(prod.get('price') or 0)
        total = price * qty
        total_sum += total

        await db.create_order(customer_id=customer['id'], product_id=pid, qty=qty, total_amount=total, comment=comment)

        try:
            await db.add_site_event("site_order", pid)
        except Exception:
            pass

        lines.append(f"{idx}) {prod.get('brand') or ''} {prod.get('model') or ''} — {qty} шт — {int(total)} грн")

    # send telegram notification
    if telegram_bot:
        text = (
            "🛒 Новый заказ с сайта\n\n"
            f"📅 {now_kyiv_str()}\n\n"
            f"Клиент: {name}\n"
            f"Телефон: {phone}\n"
            f"Город: {city}\n\n"
            "Товары:\n"
            + "\n".join(lines)
            + f"\n\nИтого: {int(total_sum)} грн\n"
            + f"Комментарий: {comment or '-'}"
        )
        await notify_admins(text)

    return {"ok": True, "total": total_sum}


async def start_web_server():
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()

async def save_telegram_photo(bot: Bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    file_path = file.file_path

    local_filename = f"/tmp/{uuid4()}.jpg"

    await bot.download_file(file_path, local_filename)

    result = cloudinary.uploader.upload(
        local_filename,
        folder="tech_bot_products"
    )

    return result["secure_url"]

async def main():
    global telegram_bot

    bot = Bot(token=BOT_TOKEN)
    telegram_bot = bot

    dp = Dispatcher()
    dp.include_router(router)

    await db.connect()
    await db.init_schema()

    print("Бот и сайт API запущены 🚀")

    try:
        await asyncio.gather(
            dp.start_polling(bot),
            start_web_server()
        )
    finally:
        await bot.session.close()
        await db.close()
@router.message(EditProductState.waiting_for_product_id)
async def edit_product_id_handler(message: Message, state: FSMContext):
    raw_id = (message.text or "").strip()

    if not raw_id.isdigit():
        await message.answer("ID товара должен быть числом.")
        return

    product_id = int(raw_id)
    product = await db.get_product_by_id(product_id)

    if not product:
        await message.answer(f"{await t(message, 'product_not_found')} Введите другой ID:")
        return

    await state.update_data(product_id=product_id)

    await state.set_state(EditProductState.waiting_for_field)
    await message.answer(
        f"Товар:\n{product['brand'] or '-'} {product['model'] or '-'}\n\n"
        "Что изменить?",
        reply_markup=inline_edit_fields_kb()
    )

async def edit_product_field_handler(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "⬅️ Назад":
        await state.clear()
        await message.answer(await t(message, "products_section"), reply_markup=products_kb)
        return

    field_map = {
        "Цена продажи": "price",
        "Закупка": "purchase_price",
        "Валюта закупки": "purchase_currency",
        "Артикул": "sku",
        "Гарантия": "warranty_months",
        "Модель": "model",
    }

    if text not in field_map:
        await message.answer("Выберите поле кнопкой.")
        return

    await state.update_data(field=field_map[text], field_title=text)
    await state.set_state(EditProductState.waiting_for_value)

    if field_map[text] == "purchase_currency":
        await message.answer("Выберите валюту: UAH / USD / EUR", reply_markup=currency_kb)
    else:
        await message.answer(f"Введите новое значение для поля: {text}")


@router.message(EditProductState.waiting_for_value)
async def edit_product_value_handler(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    data = await state.get_data()

    product_id = data["product_id"]
    field = data["field"]
    field_title = data["field_title"]

    if field in {"price", "purchase_price"}:
        try:
            value = float(value.replace(",", "."))
        except ValueError:
            await message.answer(await t(message, "enter_number"))
            return

    elif field == "warranty_months":
        if not value.isdigit():
            await message.answer("Введите число месяцев.")
            return
        value = int(value)

    elif field == "purchase_currency":
        value = value.upper()
        if value not in {"UAH", "USD", "EUR"}:
            await message.answer("Валюта должна быть UAH, USD или EUR.")
            return

    elif field == "sku":
        if value == "-":
            value = None

    await db.update_product_field(product_id, field, value)

    product = await db.get_product_by_id(product_id)
    await state.clear()

    await message.answer(
        await t(message, "product_updated") + "\n\n"
        f"ID: {product['id']}\n"
        f"{product['brand'] or '-'} {product['model'] or '-'}\n"
        f"Изменено: {field_title}",
        reply_markup=products_kb
    )


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
