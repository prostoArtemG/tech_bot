import asyncio
import json
import math
import os
import re
from datetime import datetime
from urllib.parse import quote
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
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Update
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from app.db import db, make_slug
from app.i18n import TRANSLATIONS
from app.categories import (
    CATEGORY_KEYS,
    CATEGORY_LABELS,
    category_key,
    category_label,
    canonical_ru as category_canonical_ru,
    category_emoji,
    categories_for_lang,
    same_category as same_category_key,
)

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
telegram_bot: Bot | None = None
dispatcher: Dispatcher | None = None
_polling_task: asyncio.Task | None = None

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = "/telegram/webhook"
LOCAL_POLLING = os.getenv("LOCAL_POLLING", "").lower() in {"1", "true", "yes"}
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()
SAAS_PAYMENT_PROVIDER = os.getenv("SAAS_PAYMENT_PROVIDER", "mock").strip() or "mock"

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


_saas_domain_cache: dict = {"value": None, "ts": 0.0}
_SAAS_DOMAIN_TTL = 60.0


async def get_saas_client_domain() -> dict:
    """Return dict with keys: domain, status, dns_connected, expires_at. Empty dict on failure."""
    import time
    if not SAAS_PLATFORM_URL or not SAAS_CLIENT_SLUG:
        return {}
    now_ts = time.monotonic()
    cached = _saas_domain_cache["value"]
    if cached is not None and (now_ts - _saas_domain_cache["ts"]) < _SAAS_DOMAIN_TTL:
        return cached
    import aiohttp
    url = f"{SAAS_PLATFORM_URL}/api/client-domain/{SAAS_CLIENT_SLUG}"
    out: dict = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None) or {}
                    out = {
                        "domain": data.get("domain") or data.get("name") or "",
                        "status": data.get("status") or "",
                        "dns_connected": data.get("dns_connected"),
                        "expires_at": data.get("expires_at") or data.get("expires") or "",
                    }
                else:
                    print(f"[saas] client-domain http {resp.status}")
    except Exception as e:
        print(f"[saas] client-domain failed: {e}")
    _saas_domain_cache["value"] = out
    _saas_domain_cache["ts"] = now_ts
    return out


async def get_saas_client_payments() -> list:
    """Return list of recent payments. Empty list on failure or if none."""
    if not SAAS_PLATFORM_URL or not SAAS_CLIENT_SLUG:
        return []
    import aiohttp
    url = f"{SAAS_PLATFORM_URL}/api/client-payments/{SAAS_CLIENT_SLUG}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    print(f"[saas] client-payments http {resp.status}")
                    return []
                data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    items = data.get("payments") or data.get("items") or []
                elif isinstance(data, list):
                    items = data
                else:
                    items = []
                return items if isinstance(items, list) else []
    except Exception as e:
        print(f"[saas] client-payments failed: {e}")
        return []


_saas_limits_cache: dict = {"value": None, "ts": 0.0}
_SAAS_LIMITS_TTL = 60.0


async def get_saas_client_limits() -> dict:
    """Return dict with keys: products_limit, products_used, images_per_product_limit. Empty dict on failure."""
    import time
    if not SAAS_PLATFORM_URL or not SAAS_CLIENT_SLUG:
        return {}
    now_ts = time.monotonic()
    cached = _saas_limits_cache["value"]
    if cached is not None and (now_ts - _saas_limits_cache["ts"]) < _SAAS_LIMITS_TTL:
        return cached
    import aiohttp
    url = f"{SAAS_PLATFORM_URL}/api/client-limits/{SAAS_CLIENT_SLUG}"
    out: dict = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None) or {}
                    out = {
                        "products_limit": data.get("products_limit"),
                        "products_used": data.get("products_used"),
                        "images_per_product_limit": data.get("images_per_product_limit"),
                    }
                else:
                    print(f"[saas] client-limits http {resp.status}")
    except Exception as e:
        print(f"[saas] client-limits failed: {e}")
    _saas_limits_cache["value"] = out
    _saas_limits_cache["ts"] = now_ts
    return out


async def get_image_limit_for_product() -> int:
    """Return effective per-product image limit (from API, fallback 6)."""
    limits = await get_saas_client_limits()
    raw = limits.get("images_per_product_limit")
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return 6


async def require_under_products_limit(message: Message) -> bool:
    """Check products_used vs products_limit from saas_platform. Returns False if exceeded."""
    limits = await get_saas_client_limits()
    products_limit = limits.get("products_limit")
    products_used = limits.get("products_used")
    # if API not available, count locally as best-effort upper bound
    if products_used is None:
        try:
            products_used = await db.count_products_active()
        except Exception:
            products_used = None
    try:
        limit_int = int(products_limit) if products_limit is not None else None
        used_int = int(products_used) if products_used is not None else None
    except (TypeError, ValueError):
        return True
    if limit_int is None or used_int is None or limit_int <= 0:
        return True  # unknown / unlimited → allow
    if used_int >= limit_int:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="💳 Оновити тариф", callback_data="pay_subscription_inline")
            ]]
        )
        await message.answer(
            f"⚠️ Лимит товаров по тарифу достигнут ({used_int}/{limit_int}).",
            reply_markup=kb,
        )
        return False
    return True


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


async def create_saas_payment_link(payload: dict) -> str | None:
    """POST {SAAS_PLATFORM_URL}/api/create-payment-link → returns payment_url or None."""
    if not SAAS_PLATFORM_URL:
        return None
    import aiohttp
    url = f"{SAAS_PLATFORM_URL}/api/create-payment-link"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    print(f"[saas] create-payment-link http {resp.status}: {await resp.text()}")
                    return None
                data = await resp.json(content_type=None) or {}
                link = data.get("payment_url") or data.get("url") or data.get("link")
                if not isinstance(link, str) or not link:
                    return None
                # Telegram inline button требует absolute URL — нормализуем относительные ссылки.
                if link.startswith("/"):
                    base = (os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
                            or SAAS_PLATFORM_URL)
                    if base:
                        link = base + link
                if not (link.startswith("http://") or link.startswith("https://")):
                    print(f"[saas] create-payment-link: невалидный URL: {link}")
                    return None
                return link
    except Exception as e:
        print(f"[saas] create-payment-link failed: {e}")
        return None


def _parse_amount_currency(raw: str, default_amount: float = 15.0, default_currency: str = "USD") -> tuple[float, str]:
    """Parse '15$/мес', '10 USD', '500 грн' → (amount, currency)."""
    if not raw:
        return default_amount, default_currency
    s = str(raw)
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    amount = default_amount
    if m:
        try:
            amount = float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    s_low = s.lower()
    if "$" in s or "usd" in s_low:
        currency = "USD"
    elif "€" in s or "eur" in s_low:
        currency = "EUR"
    elif "грн" in s_low or "uah" in s_low or "₴" in s:
        currency = "UAH"
    else:
        currency = default_currency
    return amount, currency


async def payment_pay_stub_handler_internal(message: Message, from_user, text_value: str):
    """Create a payment link via saas_platform and present it to the client."""
    info = await get_payment_info()
    is_subscription = text_value == "💰 Оплатить подписку"

    if is_subscription:
        payment_type = "subscription"
        amount, currency = _parse_amount_currency(info.get("pay_sub_price", ""), default_amount=15.0)
        title = "💳 Оплата подписки"
    else:
        payment_type = "domain"
        amount, currency = _parse_amount_currency(info.get("pay_domain_price", ""), default_amount=15.0)
        title = "💳 Оплата домена"

    payload = {
        "client_slug": SAAS_CLIENT_SLUG,
        "payment_type": payment_type,
        "amount": amount,
        "currency": currency,
        "provider": SAAS_PAYMENT_PROVIDER,
        "telegram_user_id": from_user.id if from_user else None,
    }

    payment_url = await create_saas_payment_link(payload)
    if not payment_url:
        await message.answer("Не удалось создать ссылку на оплату. Попробуйте позже.")
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)]]
    )
    await message.answer(title, reply_markup=kb)



async def notify_admins(text: str):
    """Send a message to every admin (ADMIN_IDS env + WEB_NOTIFY_CHAT_ID + DB users with role='admin')."""
    if not telegram_bot:
        print("[notify_admins] telegram_bot is not ready, skipping")
        return

    recipients: set[int] = set()

    # 1) ADMIN_IDS из env
    for tid in ADMIN_IDS:
        try:
            recipients.add(int(tid))
        except (TypeError, ValueError):
            pass

    # 2) WEB_NOTIFY_CHAT_ID (отдельный канал/чат)
    if WEB_NOTIFY_CHAT_ID:
        try:
            recipients.add(int(WEB_NOTIFY_CHAT_ID))
        except (TypeError, ValueError):
            pass

    # 3) Активные админы из БД (роль 'admin', is_active=TRUE)
    try:
        users = await db.list_users()
        for u in users or []:
            if u.get("role") == "admin" and u.get("is_active") and u.get("telegram_id"):
                try:
                    recipients.add(int(u["telegram_id"]))
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        print(f"[notify_admins] failed to load admins from DB: {e}")

    if not recipients:
        print("[notify_admins] no recipients found")
        return

    print(f"[notify_admins] recipients={len(recipients)} ids={sorted(recipients)}")

    sent_ok: list[int] = []
    sent_fail: list[tuple[int, str]] = []

    for chat_id in recipients:
        try:
            await telegram_bot.send_message(chat_id, text)
            sent_ok.append(chat_id)
        except Exception as e:
            sent_fail.append((chat_id, str(e)))
            print(f"[notify_admins] failed to send to {chat_id}: {e}")

    print(f"[notify_admins] ok={sent_ok} fail={[c for c, _ in sent_fail]}")


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
# Jinja-фильтры локализации категорий.
templates.env.filters["cat_ru"] = lambda v: category_label(v, "ru")
templates.env.filters["cat_uk"] = lambda v: category_label(v, "uk")
templates.env.filters["cat_key"] = lambda v: category_key(v) or ""
templates.env.globals["category_canonical_ru"] = category_canonical_ru
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
    # Specifications menu (etap 6.1): reuses edit-specs UI in "add" mode.
    editing_specs = State()


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


class EditSpecsState(StatesGroup):
    waiting_for_value = State()


# ——— Specifications (JSON-based) ———
# Базовый набор полей (используется по умолчанию и для категории boilers).
SPEC_FIELDS = [
    ("volume",                "Обʼєм"),
    ("tank_shape",            "Форма баку"),
    ("installation",          "Установка"),
    ("control",               "Керування"),
    ("heater_type",           "Тип ТЕНу"),
    ("power",                 "Потужність"),
    ("ten_count",             "Кількість ТЕНів"),
    ("brand_country",         "Країна реєстрації бренду"),
    ("manufacturer_country",  "Країна виробник"),
    ("dimensions",            "Розміри В*Ш*Г мм"),
    ("warranty_manufacturer", "Гарантія від виробника"),
]
SPEC_LABELS = dict(SPEC_FIELDS)
SPEC_OPTIONS = {
    "heater_type":           ["Сухий", "Мокрий"],
    "control":               ["Механічне", "Електронне", "Wi-Fi"],
    "ten_count":             ["1", "2"],
    "tank_shape":            ["Циліндричний", "Плоский", "Кубічний"],
    "installation":          ["Горизонтальна", "Вертикальна", "Універсальна"],
    "warranty_manufacturer": ["12 міс", "24 міс", "36 міс", "60 міс", "72 міс", "84 міс", "96 міс", "120 міс"],
}

# ── Per-category overrides ──
# Для категорий, у которых форма характеристик отличается от boilers.
# Архитектура та же: список (key, label) + опции-кнопки для select-полей.
_AC_SPEC_FIELDS = [
    ("room_area",                 "Площа приміщення, м²"),
    ("compressor_type",           "Тип компресора"),
    ("freon",                     "Фреон"),
    ("power_consumption",         "Споживана потужність холод/тепло, Вт"),
    ("cooling_heating_capacity",  "Продуктивність, кВт холод/тепло"),
    ("indoor_outdoor_dimensions", "Розміри внутр./зовн. блоку, мм"),
    ("indoor_noise_level",        "Рівень шуму внутрішнього блоку, дБ"),
    ("energy_class",              "Клас енергоефективності"),
]
_AC_SPEC_OPTIONS = {
    "compressor_type": ["Інверторний", "Звичайний"],
    "freon":           ["R32", "R410A"],
    "energy_class":    ["A", "A+", "A++", "A+++"],
}

# refrigerators
_REFR_SPEC_FIELDS = [
    ("no_frost",         "No Frost"),
    ("volume",           "Обʼєм, л"),
    ("height",           "Висота, см"),
    ("freezer_position", "Морозильна камера"),
    ("doors",            "Кількість дверей"),
    ("energy_class",     "Клас енергоспоживання"),
]
_REFR_SPEC_OPTIONS = {
    "no_frost":         ["Так", "Ні"],
    "freezer_position": ["Зверху", "Знизу", "Side-by-Side"],
    "doors":            ["1", "2", "3", "4"],
    "energy_class":     ["A", "A+", "A++", "A+++"],
}

# washing machines
_WM_SPEC_FIELDS = [
    ("load_capacity", "Завантаження, кг"),
    ("spin_speed",    "Віджим, об/хв"),
    ("depth",         "Глибина, см"),
    ("dryer",         "Сушіння"),
    ("loading_type",  "Тип завантаження"),
    ("energy_class",  "Клас енергоспоживання"),
]
_WM_SPEC_OPTIONS = {
    "dryer":        ["Так", "Ні"],
    "loading_type": ["Фронтальне", "Вертикальне"],
    "energy_class": ["A", "A+", "A++", "A+++"],
}

# hoods
_HOOD_SPEC_FIELDS = [
    ("width",             "Ширина, см"),
    ("productivity",      "Продуктивність, м³/год"),
    ("control_type",      "Керування"),
    ("installation_type", "Тип монтажу"),
    ("noise_level",       "Рівень шуму, дБ"),
    ("color",             "Колір"),
]
_HOOD_SPEC_OPTIONS = {
    "control_type":      ["Механічне", "Електронне", "Сенсорне"],
    "installation_type": ["Настінна", "Вбудована", "Острівна", "Телескопічна"],
    "color":             ["Білий", "Чорний", "Нержавіюча сталь", "Сірий"],
}

# microwaves
_MW_SPEC_FIELDS = [
    ("volume",            "Обʼєм, л"),
    ("power",             "Потужність, Вт"),
    ("control_type",      "Керування"),
    ("grill",             "Гриль"),
    ("convection",        "Конвекція"),
    ("installation_type", "Тип"),
    ("color",             "Колір"),
]
_MW_SPEC_OPTIONS = {
    "control_type":      ["Механічне", "Електронне", "Сенсорне"],
    "grill":             ["Так", "Ні"],
    "convection":        ["Так", "Ні"],
    "installation_type": ["Соло", "Вбудована"],
    "color":             ["Білий", "Чорний", "Нержавіюча сталь", "Сірий"],
}

# gas stoves (covers all stove types via stove_type select)
_STOVE_SPEC_FIELDS = [
    ("stove_type",    "Тип плити"),
    ("burners_count", "Кількість конфорок"),
    ("oven_type",     "Тип духовки"),
    ("width",         "Ширина, см"),
    ("control_type",  "Керування"),
    ("ignition",      "Електропідпал"),
    ("color",         "Колір"),
]
_STOVE_SPEC_OPTIONS = {
    "stove_type":    ["Газова", "Електрична", "Комбінована", "Індукційна"],
    "burners_count": ["2", "3", "4", "5"],
    "oven_type":     ["Газова", "Електрична", "Без духовки"],
    "control_type":  ["Механічне", "Електронне", "Сенсорне"],
    "ignition":      ["Так", "Ні"],
    "color":         ["Білий", "Чорний", "Нержавіюча сталь", "Сірий"],
}

SPEC_FIELDS_BY_CATEGORY = {
    "boilers":          SPEC_FIELDS,
    "air_conditioners": _AC_SPEC_FIELDS,
    "refrigerators":    _REFR_SPEC_FIELDS,
    "washing_machines": _WM_SPEC_FIELDS,
    "hoods":            _HOOD_SPEC_FIELDS,
    "microwaves":       _MW_SPEC_FIELDS,
    "gas_stoves":       _STOVE_SPEC_FIELDS,
}
SPEC_OPTIONS_BY_CATEGORY = {
    "boilers":          SPEC_OPTIONS,
    "air_conditioners": _AC_SPEC_OPTIONS,
    "refrigerators":    _REFR_SPEC_OPTIONS,
    "washing_machines": _WM_SPEC_OPTIONS,
    "hoods":            _HOOD_SPEC_OPTIONS,
    "microwaves":       _MW_SPEC_OPTIONS,
    "gas_stoves":       _STOVE_SPEC_OPTIONS,
}


def spec_fields_for(cat_key: str | None):
    """Список (key, label) для категории. Fallback — глобальный SPEC_FIELDS."""
    if cat_key and cat_key in SPEC_FIELDS_BY_CATEGORY:
        return SPEC_FIELDS_BY_CATEGORY[cat_key]
    return SPEC_FIELDS


def spec_labels_for(cat_key: str | None):
    return dict(spec_fields_for(cat_key))


def spec_options_for(cat_key: str | None):
    if cat_key and cat_key in SPEC_OPTIONS_BY_CATEGORY:
        return SPEC_OPTIONS_BY_CATEGORY[cat_key]
    return SPEC_OPTIONS


async def _get_product_cat_key(product_id) -> str | None:
    """Достаёт canonical category key товара (для category-aware spec UI)."""
    try:
        p = await db.get_product_by_id(int(product_id))
    except Exception:
        return None
    if not p:
        return None
    try:
        cat = p["category"]
    except (KeyError, TypeError):
        cat = None
    return category_key(cat) if cat else None

# ── Canonical value mapping (mirrors db.DEFAULT_CATEGORY_ATTRIBUTES.options_json) ──
# Любой ключ — приведённый к нижнему регистру (label UA/RU, или сам canonical) →
# canonical key, который и кладём в products.specifications_json.
SPEC_VALUE_MAP = {
    "tank_shape": {
        "циліндричний": "cylindrical",
        "цилиндрический": "cylindrical",
        "cylindrical": "cylindrical",
        "плоский": "flat",
        "flat": "flat",
        "кубічний": "cubic",
        "кубический": "cubic",
        "cubic": "cubic",
    },
    "installation": {
        "вертикальна": "vertical",
        "вертикальная": "vertical",
        "vertical": "vertical",
        "горизонтальна": "horizontal",
        "горизонтальная": "horizontal",
        "horizontal": "horizontal",
        "універсальна": "universal",
        "универсальная": "universal",
        "universal": "universal",
    },
    "heater_type": {
        "сухий": "dry",
        "сухой": "dry",
        "dry": "dry",
        "мокрий": "wet",
        "мокрый": "wet",
        "wet": "wet",
    },
    # ── air_conditioners ──
    "inverter": {
        "так": "yes", "да": "yes", "yes": "yes", "+": "yes", "true": "yes", "1": "yes",
        "ні": "no",  "нет": "no", "no": "no",  "-": "no",  "false": "no", "0": "no",
    },
    "wifi": {
        "так": "yes", "да": "yes", "yes": "yes",
        "ні": "no",  "нет": "no", "no": "no",
    },
    "compressor_type": {
        "інверторний": "inverter",
        "инверторный":  "inverter",
        "inverter":     "inverter",
        "неінверторний": "non_inverter",
        "неинверторный": "non_inverter",
        "non_inverter":  "non_inverter",
        "non-inverter":  "non_inverter",
        "звичайний":    "non_inverter",
        "обычный":      "non_inverter",
    },
    "freon": {
        "r32":   "r32",
        "r-32":  "r32",
        "r410a": "r410a",
        "r-410a": "r410a",
        "r 410a": "r410a",
    },
    "energy_class": {
        "a":    "a",
        "a+":   "a_plus",
        "a++":  "a_plus_plus",
        "a+++": "a_plus_plus_plus",
        "a_plus":            "a_plus",
        "a_plus_plus":       "a_plus_plus",
        "a_plus_plus_plus":  "a_plus_plus_plus",
    },
    # ── refrigerators ──
    "no_frost": {
        "так": "yes", "да": "yes", "yes": "yes", "+": "yes", "true": "yes", "1": "yes",
        "ні": "no",  "нет": "no", "no": "no",  "-": "no",  "false": "no", "0": "no",
    },
    "freezer_position": {
        "зверху": "top", "сверху": "top", "верх": "top", "top": "top",
        "знизу": "bottom", "снизу": "bottom", "низ": "bottom", "bottom": "bottom",
        "side-by-side": "side_by_side",
        "side_by_side": "side_by_side",
        "side by side": "side_by_side",
        "сайд-бай-сайд": "side_by_side",
        "сайд бай сайд": "side_by_side",
    },
    "doors": {
        "1": "1", "2": "2", "3": "3", "4": "4",
    },
    # ── washing machines ──
    "dryer": {
        "так": "yes", "да": "yes", "yes": "yes", "+": "yes", "true": "yes", "1": "yes",
        "ні": "no",  "нет": "no", "no": "no",  "-": "no",  "false": "no", "0": "no",
    },
    "loading_type": {
        "фронтальне": "front", "фронтальная": "front", "фронтальний": "front",
        "фронт": "front", "front": "front",
        "вертикальне": "top", "вертикальная": "top", "вертикальний": "top",
        "верт": "top", "top": "top", "верхнє": "top", "верхняя": "top",
    },
    # ── hoods ──
    "control_type": {
        "механічне": "mechanical", "механическое": "mechanical",
        "механический": "mechanical", "механічний": "mechanical",
        "mechanical": "mechanical",
        "електронне": "electronic", "электронное": "electronic",
        "електронний": "electronic", "электронный": "electronic",
        "electronic": "electronic",
        "сенсорне": "touch", "сенсорное": "touch",
        "сенсорний": "touch", "сенсорный": "touch",
        "touch": "touch",
    },
    "installation_type": {
        "настінна": "wall", "настенная": "wall", "настінний": "wall",
        "настенный": "wall", "wall": "wall",
        "вбудована": "built_in", "встраиваемая": "built_in",
        "встроенная": "built_in", "вбудований": "built_in",
        "built_in": "built_in", "built-in": "built_in",
        "острівна": "island", "островная": "island",
        "острівний": "island", "островной": "island",
        "island": "island",
        "телескопічна": "telescopic", "телескопическая": "telescopic",
        "телескопічний": "telescopic", "телескопический": "telescopic",
        "telescopic": "telescopic",
        "соло": "solo", "solo": "solo", "окрема": "solo", "отдельная": "solo",
        "окремий": "solo", "отдельный": "solo", "standalone": "solo",
    },
    "color": {
        "білий": "white", "белый": "white", "white": "white",
        "чорний": "black", "чёрный": "black", "черный": "black", "black": "black",
        "нержавіюча сталь": "stainless_steel",
        "нержавеющая сталь": "stainless_steel",
        "нержавіюча": "stainless_steel", "нержавеющая": "stainless_steel",
        "нержавійка": "stainless_steel", "нержавейка": "stainless_steel",
        "stainless_steel": "stainless_steel", "stainless steel": "stainless_steel",
        "inox": "stainless_steel",
        "сірий": "gray", "серый": "gray", "gray": "gray", "grey": "gray",
    },
    # ── microwaves (extra yes/no keys) ──
    "grill": {
        "так": "yes", "да": "yes", "yes": "yes", "+": "yes", "true": "yes", "1": "yes",
        "ні": "no",  "нет": "no", "no": "no",  "-": "no",  "false": "no", "0": "no",
    },
    "convection": {
        "так": "yes", "да": "yes", "yes": "yes", "+": "yes", "true": "yes", "1": "yes",
        "ні": "no",  "нет": "no", "no": "no",  "-": "no",  "false": "no", "0": "no",
    },
    # ── gas stoves ──
    "stove_type": {
        "газова": "gas", "газовая": "gas", "gas": "gas", "газ": "gas",
        "електрична": "electric", "электрическая": "electric",
        "електро": "electric", "электро": "electric", "electric": "electric",
        "комбінована": "combined", "комбинированная": "combined",
        "комбі": "combined", "комби": "combined", "combined": "combined",
        "індукційна": "induction", "индукционная": "induction",
        "индукция": "induction", "індукція": "induction", "induction": "induction",
    },
    "burners_count": {
        "2": "2", "3": "3", "4": "4", "5": "5",
    },
    "oven_type": {
        "газова": "gas", "газовая": "gas", "gas": "gas",
        "електрична": "electric", "электрическая": "electric",
        "електро": "electric", "электро": "electric", "electric": "electric",
        "без духовки": "none", "немає": "none", "нет": "none",
        "відсутня": "none", "отсутствует": "none", "none": "none", "no": "none",
    },
    "ignition": {
        "так": "yes", "да": "yes", "yes": "yes", "+": "yes", "true": "yes", "1": "yes",
        "ні": "no",  "нет": "no", "no": "no",  "-": "no",  "false": "no", "0": "no",
    },
}

# canonical → UA label (для отображения в боте и на сайте).
SPEC_CANON_LABEL_UK = {
    "tank_shape":   {"cylindrical": "Циліндричний", "flat": "Плоский", "cubic": "Кубічний"},
    "installation": {"vertical": "Вертикальна", "horizontal": "Горизонтальна", "universal": "Універсальна"},
    "heater_type":  {"dry": "Сухий", "wet": "Мокрий"},
    "inverter":     {"yes": "Так", "no": "Ні"},
    "wifi":         {"yes": "Так", "no": "Ні"},
    "compressor_type": {"inverter": "Інверторний", "non_inverter": "Звичайний"},
    "freon":        {"r32": "R32", "r410a": "R410A"},
    "energy_class": {"a": "A", "a_plus": "A+", "a_plus_plus": "A++", "a_plus_plus_plus": "A+++"},
    "no_frost":     {"yes": "Так", "no": "Ні"},
    "freezer_position": {"top": "Зверху", "bottom": "Знизу", "side_by_side": "Side-by-Side"},
    "doors":        {"1": "1", "2": "2", "3": "3", "4": "4"},
    "dryer":        {"yes": "Так", "no": "Ні"},
    "loading_type": {"front": "Фронтальне", "top": "Вертикальне"},
    "control_type": {"mechanical": "Механічне", "electronic": "Електронне", "touch": "Сенсорне"},
    "installation_type": {
        "wall": "Настінна", "built_in": "Вбудована",
        "island": "Острівна", "telescopic": "Телескопічна",
        "solo": "Соло",
    },
    "color": {
        "white": "Білий", "black": "Чорний",
        "stainless_steel": "Нержавіюча сталь", "gray": "Сірий",
    },
    "grill":      {"yes": "Так", "no": "Ні"},
    "convection": {"yes": "Так", "no": "Ні"},
    "stove_type": {
        "gas": "Газова", "electric": "Електрична",
        "combined": "Комбінована", "induction": "Індукційна",
    },
    "burners_count": {"2": "2", "3": "3", "4": "4", "5": "5"},
    "oven_type":  {"gas": "Газова", "electric": "Електрична", "none": "Без духовки"},
    "ignition":   {"yes": "Так", "no": "Ні"},
}


def _normalize_spec_value(key: str, value) -> str:
    """Сохраняемое значение → canonical (где применимо).

    - volume / room_area / power: возвращаем только число ('80' из '80 л').
    - select-поля: label → canonical key (через SPEC_VALUE_MAP).
    - остальное: возвращаем .strip() без изменений.
    """
    if value is None:
        return value
    v = str(value).strip()
    if not v:
        return v
    if key in ("volume", "room_area", "power", "height", "load_capacity", "spin_speed", "depth", "width", "productivity", "noise_level", "indoor_noise_level"):
        n = _extract_number(v)
        if n is not None:
            return str(int(n) if float(n).is_integer() else n)
        return v
    mp = SPEC_VALUE_MAP.get(key)
    if mp:
        canon = mp.get(v.lower())
        if canon:
            return canon
    return v


def _label_for_spec_value(key: str, value) -> str:
    """canonical (или legacy label) → UA label для отображения.

    - volume: '80' → '80 л'.
    - tank_shape/installation/ten_type: canonical → UA label.
    - legacy labels возвращаем как есть.
    """
    if value is None or value == "":
        return value
    v = str(value).strip()
    if not v:
        return v
    if key == "volume":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} л"
        return v
    if key == "room_area":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} м²"
        return v
    if key == "height":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} см"
        return v
    if key == "depth":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} см"
        return v
    if key == "load_capacity":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} кг"
        return v
    if key == "spin_speed":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} об/хв"
        return v
    if key == "width":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} см"
        return v
    if key == "productivity":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} м³/год"
        return v
    if key == "noise_level":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} дБ"
        return v
    if key == "power":
        # Единица зависит от категории — выводим без неё, чтобы не врать.
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return str(num)
        return v
    if key == "indoor_noise_level":
        n = _extract_number(v)
        if n is not None:
            num = int(n) if float(n).is_integer() else n
            return f"{num} дБ"
        return v
    canon_map = SPEC_CANON_LABEL_UK.get(key)
    if canon_map and v in canon_map:
        return canon_map[v]
    return v


def inline_specs_kb(product_id: int, current: dict, mode: str = "edit", cat_key: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for key, label in spec_fields_for(cat_key):
        raw_val = (current or {}).get(key)
        display = _label_for_spec_value(key, raw_val) if raw_val else None
        text = f"{label}: {display}" if display else label
        if len(text) > 60:
            text = text[:57] + "…"
        rows.append([InlineKeyboardButton(text=text, callback_data=f"specs_field:{product_id}:{key}")])
    if mode == "add":
        # Финальные кнопки сценария добавления товара.
        rows.append([InlineKeyboardButton(text="📝 Описание", callback_data=f"specs_desc:{product_id}")])
        rows.append([InlineKeyboardButton(
            text="✅ Завершить добавление",
            callback_data=f"addspec_done:{product_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="⏭ Пропустить характеристики",
            callback_data=f"addspec_done:{product_id}",
        )])
    else:
        rows.append([InlineKeyboardButton(text="📝 Описание", callback_data=f"specs_desc:{product_id}")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"specs_back:{product_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_specs_options_kb(product_id: int, key: str, cat_key: str | None = None) -> InlineKeyboardMarkup:
    options = spec_options_for(cat_key).get(key, [])
    rows = []
    for idx, opt in enumerate(options):
        rows.append([InlineKeyboardButton(text=opt, callback_data=f"specs_opt:{product_id}:{key}:{idx}")])
    rows.append([
        InlineKeyboardButton(text="🗑 Очистить", callback_data=f"specs_clear:{product_id}:{key}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"specs_open:{product_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


class BannersAddState(StatesGroup):
    title = State()
    subtitle = State()
    button_text = State()
    button_link = State()
    image_url = State()
    sort_order = State()


class BannersEditState(StatesGroup):
    waiting_for_value = State()


class SeoMainState(StatesGroup):
    waiting_for_value = State()


class SeoCategoryState(StatesGroup):
    waiting_for_value = State()


class SeoProductState(StatesGroup):
    waiting_for_value = State()


class AutoSeoState(StatesGroup):
    waiting_for_value = State()


class SitePagesState(StatesGroup):
    waiting_for_text = State()


class SiteCategoryQuickState(StatesGroup):
    waiting = State()


class SiteProductPreviewState(StatesGroup):
    waiting_for_query = State()


class AddProductGroupState(StatesGroup):
    waiting_for_category_key = State()
    waiting_for_name = State()


class AddFilterFieldState(StatesGroup):
    waiting_for_group = State()
    waiting_for_name = State()
    waiting_for_type = State()


class AddFilterValueState(StatesGroup):
    waiting_for_category = State()
    waiting_for_field = State()
    waiting_for_value = State()


class SetProductFilterState(StatesGroup):
    waiting_for_category = State()
    waiting_for_status = State()
    waiting_for_product = State()
    waiting_for_filter = State()
    waiting_for_value = State()


admin_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Товары"), KeyboardButton(text="📋 Заказы")],
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
        [KeyboardButton(text="📦 Товары"), KeyboardButton(text="📋 Заказы")],
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
        [KeyboardButton(text="🧹 Очистить битые товары")],
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
        [KeyboardButton(text="🖼 Баннер сайта"), KeyboardButton(text="🖼 Баннеры")],
        [KeyboardButton(text="📣 Промо-плашка")],
        [KeyboardButton(text="👀 Просмотр товара на сайте")],
        [KeyboardButton(text="📂 Категории сайта")],
        [KeyboardButton(text="📄 Страницы сайта")],
        [KeyboardButton(text="✏️ Редактировать товар")],
        [KeyboardButton(text="🌐 Язык сайта")],
        [KeyboardButton(text="📊 Аналитика сайта")],
        [KeyboardButton(text="📋 Заявки/Покупатели"), KeyboardButton(text="👥 Пользователи")],
        [KeyboardButton(text="🔎 SEO")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


seo_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏠 Главная страница")],
        [KeyboardButton(text="📂 Категории SEO")],
        [KeyboardButton(text="🗺 Sitemap")],
        [KeyboardButton(text="🤖 Robots.txt")],
        [KeyboardButton(text="⚙️ Авто SEO")],
        [KeyboardButton(text="ℹ️ SEO Підказка")],
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

directories_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📁 Категории сайта")],
        [KeyboardButton(text="➕ Настроить категорию")],
        [KeyboardButton(text="🔧 Фильтры")],
        [KeyboardButton(text="➕ Добавить фильтр")],
        [KeyboardButton(text="📋 Значения")],
        [KeyboardButton(text="➕ Добавить значение")],
        [KeyboardButton(text="🧩 Фільтри товару")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)


site_banner_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Показать баннер")],
        [KeyboardButton(text="📝 Текст баннера")],
        [KeyboardButton(text="🏷 Заголовок hero")],
        [KeyboardButton(text="📄 Подзаголовок hero")],
        [KeyboardButton(text="🔘 Кнопка hero (текст)")],
        [KeyboardButton(text="🔗 Кнопка hero (URL)")],
        [KeyboardButton(text="🖼 Фото баннера (URL)")],
        [KeyboardButton(text="👁 Баннер: вкл/выкл")],
        [KeyboardButton(text="♻️ Сбросить баннер")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)

site_promo_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Показать промо")],
        [KeyboardButton(text="📝 Текст промо")],
        [KeyboardButton(text="📅 Дата окончания промо")],
        [KeyboardButton(text="👁 Промо: вкл/выкл")],
        [KeyboardButton(text="♻️ Сбросить промо")],
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
        [KeyboardButton(text="📋 Інструкція підключення")],
        [KeyboardButton(text="💳 Продовжити домен")],
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


site_pages_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚚 Доставка")],
        [KeyboardButton(text="🛡 Гарантия")],
        [KeyboardButton(text="↩️ Повернення")],
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
            InlineKeyboardButton(text="Артикул", callback_data="edit_field:sku"),
        ],
        [
            InlineKeyboardButton(text="Гарантия", callback_data="edit_field:warranty_months"),
            InlineKeyboardButton(text="Модель", callback_data="edit_field:model"),
        ],
        [
            InlineKeyboardButton(text="Фото (URL)", callback_data="edit_field:photo_url"),
            InlineKeyboardButton(text="📋 Характеристики", callback_data="edit_action:specs_open"),
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
    if category and category_key(category) == "boilers":
        rows.append([
            InlineKeyboardButton(text="🚿 Объем бойлера", callback_data="edit_field:boiler_volume_liters"),
            InlineKeyboardButton(text="🔥 Тип тена", callback_data="edit_action:set_ten_type"),
        ])

    rows.extend([
        [
            InlineKeyboardButton(text="� Модельная группа", callback_data="edit_field:model_group"),
            InlineKeyboardButton(text="📂 Изменить категорию", callback_data="edit_action:change_category"),
        ],
        [
            InlineKeyboardButton(text="🖼 Управление фото", callback_data="edit_action:manage_photos"),
            InlineKeyboardButton(text=visibility_text, callback_data=f"edit_action:{visibility_action}"),
        ],
        [
            InlineKeyboardButton(text="❌ Удалить товар", callback_data="edit_action:soft_delete"),
        ],
        [
            InlineKeyboardButton(text="🔎 SEO товара", callback_data=f"seo_prod_open:{product['id']}" if product and product.get("id") else "seo_prod_open:0"),
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
        "model_group": "Модельная группа",
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
    elif field == "model_group":
        await callback.message.answer(
            "Введите код модельной группы (латиница/цифры/дефис), например: «atlantic-steatite».\n"
            "Одинаковый код у всех вариантов одной модели (разных объёмов / площадей).\n"
            "Отправьте «-» чтобы очистить."
        )
    else:
        await callback.message.answer(f"Введите новое значение для поля: {field_titles[field]}")

    await callback.answer()
def inline_categories_kb(lang: str = "ru"):
    """Клавиатура категорий, локализованная по языку пользователя.

    Callback: add_category:<key> (стабильный ключ из app.categories).
    """
    rows: list[list[InlineKeyboardButton]] = []
    items = categories_for_lang(lang)
    # Парами в ряд для компактности.
    for i in range(0, len(items), 2):
        pair = items[i:i + 2]
        rows.append([
            InlineKeyboardButton(
                text=f"{c['emoji']} {c['name']}",
                callback_data=f"add_category:{c['key']}",
            )
            for c in pair
        ])
    rows.append([
        InlineKeyboardButton(
            text="🔍 Поиск категории" if lang == "ru" else "🔍 Пошук категорії",
            callback_data="add_category_search",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена" if lang == "ru" else "❌ Скасувати",
            callback_data="cancel_flow",
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _user_lang(telegram_id: int) -> str:
    try:
        u = await db.get_user_by_telegram_id(telegram_id)
        return (u or {}).get("language") or "ru"
    except Exception:
        return "ru"


async def inline_brands_kb():
    """Список активных брендов сайта + кнопки управления.

    Источник: union активных site_brands + бренды из активных товаров
    (если не скрыты в site_brands). Никаких hardcoded брендов.
    """
    try:
        names = await db.list_brands_for_selection()
    except Exception as e:
        print(f"[brands] load failed: {e}")
        names = []

    # Гарантируем, что в основном списке нет скрытых брендов.
    try:
        all_rows = await db.list_site_brands()
    except Exception:
        all_rows = []
    hidden_lower = {
        (r["name"] or "").strip().lower()
        for r in all_rows
        if not r["is_active"]
    }
    names = [n for n in names if (n or "").strip().lower() not in hidden_lower]

    keyboard: list[list[InlineKeyboardButton]] = []
    # Бренды — по одной кнопке в строку.
    for name in names:
        if not name:
            continue
        keyboard.append([
            InlineKeyboardButton(text=name, callback_data=f"add_brand:{name}")
        ])

    # Кнопка добавления бренда всегда доступна — и при пустом списке тоже.
    keyboard.append([
        InlineKeyboardButton(text="➕ Добавить бренд", callback_data="add_brand_new"),
    ])
    # Поиск показываем только если есть, что искать.
    if names:
        keyboard.append([
            InlineKeyboardButton(text="🔍 Поиск бренда", callback_data="add_brand_search"),
        ])
    # Кнопка «Неактивные бренды» — если есть хоть один скрытый.
    has_hidden = bool(hidden_lower)
    if has_hidden:
        keyboard.append([
            InlineKeyboardButton(text="👁 Неактивные бренды", callback_data="add_brand_show_hidden"),
        ])
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
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
                [
                    KeyboardButton(text="⚙️ Справочники"),
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


@router.message(Command("reset"), StateFilter("*"))
async def reset_handler(message: Message, state: FSMContext):
    await state.clear()
    menu = await get_main_menu_for_user(message)
    await message.answer(
        "✅ Состояние сброшено. Главное меню:",
        reply_markup=menu,
    )


DEFAULT_BRANDS_TO_HIDE = ["Samsung", "LG", "Bosch", "Beko", "Philips", "Xiaomi"]


async def _send_brands_admin(message: Message, edit: bool = False, note: str | None = None):
    """Главное меню /brands (hub)."""
    # Idempotent sync: подтягиваем недостающие бренды из товаров,
    # авто-реактивируем скрытые-но-используемые.
    try:
        await db.sync_site_brands_from_products()
    except Exception as e:
        print(f"[brands] sync from products failed: {e}")

    try:
        rows = await db.list_site_brands()
    except Exception as e:
        print(f"[brands] admin load failed: {e}")
        rows = []

    active_cnt = sum(1 for r in rows if r["is_active"])
    hidden_cnt = sum(1 for r in rows if not r["is_active"])

    keyboard = [
        [InlineKeyboardButton(text=f"✅ Активные бренды ({active_cnt})", callback_data="brands_menu_active")],
        [InlineKeyboardButton(text=f"👁 Скрытые бренды ({hidden_cnt})", callback_data="brands_menu_hidden")],
        [InlineKeyboardButton(text="🧹 Синхронизировать с товарами", callback_data="brands_sync")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="brands_menu_close")],
    ]
    text = (
        "📋 Бренды сайта\n"
        f"Активных: {active_cnt}, скрытых: {hidden_cnt}.\n"
        "Источник для бота и сайта общий: активные бренды + бренды активных товаров."
    )
    if note:
        text = f"{note}\n\n{text}"

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    try:
        if edit:
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except Exception:
                pass
        await message.answer(text, reply_markup=markup)
    except Exception as e:
        print(f"[brands] admin send failed: {e}")


async def _send_brands_list(message: Message, mode: str, note: str | None = None):
    """Подменю: 'active' или 'hidden'. Каждая запись — кнопка действия."""
    try:
        rows = await db.list_site_brands()
    except Exception as e:
        print(f"[brands] list failed: {e}")
        rows = []

    try:
        used = await db.list_brands_from_active_products()
    except Exception:
        used = []
    used_lower = {(n or "").strip().lower() for n in used}

    if mode == "active":
        items = [r for r in rows if r["is_active"]]
        header = f"✅ Активные бренды ({len(items)})"
        empty_text = "Активных брендов нет."
    else:
        items = [r for r in rows if not r["is_active"]]
        header = f"👁 Скрытые бренды ({len(items)})"
        empty_text = "Скрытых брендов нет."

    keyboard: list[list[InlineKeyboardButton]] = []
    for r in items:
        name = r["name"]
        if mode == "active":
            lock = " 🔒" if (name or "").strip().lower() in used_lower else ""
            label = f"👁 Скрыть: {name}{lock}"
        else:
            label = f"✅ Активировать: {name}"
        keyboard.append([
            InlineKeyboardButton(text=label, callback_data=f"brand_toggle:{r['id']}:{mode}"),
        ])

    keyboard.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="brands_menu_main"),
    ])

    text = header
    if not items:
        text = f"{header}\n\n{empty_text}"
    elif mode == "active":
        text = (
            f"{header}\n"
            "🔒 — используется активными товарами, скрыть нельзя."
        )
    if note:
        text = f"{note}\n\n{text}"

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    try:
        try:
            await message.edit_text(text, reply_markup=markup)
        except Exception:
            await message.answer(text, reply_markup=markup)
    except Exception as e:
        print(f"[brands] list send failed: {e}")


@router.message(Command("brands"))
async def brands_admin_handler(message: Message):
    if not await require_admin(message):
        return
    await _send_brands_admin(message, edit=False)


@router.callback_query(lambda c: c.data == "brands_menu_main")
async def brands_menu_main_callback(callback: CallbackQuery):
    await _send_brands_admin(callback.message, edit=True)
    await callback.answer()


@router.callback_query(lambda c: c.data == "brands_menu_active")
async def brands_menu_active_callback(callback: CallbackQuery):
    await _send_brands_list(callback.message, mode="active")
    await callback.answer()


@router.callback_query(lambda c: c.data == "brands_menu_hidden")
async def brands_menu_hidden_callback(callback: CallbackQuery):
    await _send_brands_list(callback.message, mode="hidden")
    await callback.answer()


@router.callback_query(lambda c: c.data == "brands_menu_close")
async def brands_menu_close_callback(callback: CallbackQuery):
    try:
        await callback.message.edit_text("✅ Закрыто.")
    except Exception:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("brand_toggle:"))
async def brand_toggle_callback(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    # формат: brand_toggle:<id>[:<return_mode>]
    return_mode = parts[2] if len(parts) >= 3 else None
    try:
        brand_id = int(parts[1])
        row = await db.fetchrow(
            "SELECT id, name, is_active FROM site_brands WHERE id = $1",
            brand_id,
        )
        if row is None:
            await callback.answer("Бренд не найден", show_alert=False)
            return
        # Защита: нельзя скрывать бренд, если есть активные товары
        if row["is_active"]:
            cnt = await db.count_active_products_by_brand(row["name"])
            if cnt > 0:
                await callback.answer(
                    f"Нельзя скрыть бренд, пока есть активные товары этого бренда ({cnt}).",
                    show_alert=True,
                )
                return
        await db.toggle_site_brand(brand_id)
    except Exception as e:
        print(f"[brands] toggle failed: {e}")
        await callback.answer("Ошибка", show_alert=False)
        return

    # После действия — обновляем то подменю, откуда пришли (иначе hub).
    if return_mode in ("active", "hidden"):
        await _send_brands_list(callback.message, mode=return_mode)
    else:
        await _send_brands_admin(callback.message, edit=True)
    await callback.answer("Готово")


@router.callback_query(lambda c: c.data == "brands_sync")
async def brands_sync_callback(callback: CallbackQuery):
    try:
        stats = await db.sync_site_brands_from_products()
    except Exception as e:
        print(f"[brands] sync failed: {e}")
        await callback.answer("Ошибка", show_alert=False)
        return
    note = (
        f"🧹 Синхронизация: добавлено {stats.get('added', 0)}, "
        f"реактивировано {stats.get('reactivated', 0)}."
    )
    await _send_brands_admin(callback.message, edit=True, note=note)
    await callback.answer("Готово")



@router.errors()
async def global_error_handler(event):
    import traceback
    try:
        exc = getattr(event, "exception", None)
        upd = getattr(event, "update", None)
        print(f"[bot-error] {type(exc).__name__}: {exc}")
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    except Exception as log_err:
        print(f"[bot-error] logging failed: {log_err}")

    # Попытка ответить пользователю
    try:
        upd = getattr(event, "update", None)
        target = None
        if upd is not None:
            if getattr(upd, "message", None) is not None:
                target = upd.message
            elif getattr(upd, "callback_query", None) is not None:
                cq = upd.callback_query
                try:
                    await cq.answer("⚠️ Произошла ошибка", show_alert=False)
                except Exception:
                    pass
                target = cq.message
        if target is not None:
            await target.answer(
                "⚠️ Произошла ошибка. Нажмите /start и повторите действие."
            )
    except Exception as send_err:
        print(f"[bot-error] reply failed: {send_err}")
    return True




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
    # promo bar
    "promo_enabled": "false",
    "promo_text": "",
    "promo_end_date": "",  # ISO date string YYYY-MM-DD
    # hero banner extra fields
    "hero_title": "",
    "hero_subtitle": "",
    "hero_button_text": "",
    "hero_button_url": "/",
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
        "hero_title": (await db.get_setting("hero_title")) or "",
        "hero_subtitle": (await db.get_setting("hero_subtitle")) or "",
        "hero_button_text": (await db.get_setting("hero_button_text")) or "",
        "hero_button_url": (await db.get_setting("hero_button_url")) or "/",
        # promo bar
        "promo_enabled": ((await db.get_setting("promo_enabled")) or "false") == "true",
        "promo_text": (await db.get_setting("promo_text")) or "",
        "promo_end_date": (await db.get_setting("promo_end_date")) or "",
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
        f"Текст баннера: {design['banner_text'] or '-'}\n"
        f"Фото: {design['banner_image_url'] or '-'}\n\n"
        f"— Hero-блок —\n"
        f"Заголовок: {design['hero_title'] or '-'}\n"
        f"Подзаголовок: {design['hero_subtitle'] or '-'}\n"
        f"Кнопка: {design['hero_button_text'] or '-'} → {design['hero_button_url'] or '/'}"
    )


@router.message(lambda m: m.text in {"📝 Текст баннера", "🖼 Фото баннера (URL)",
                                      "🏷 Заголовок hero", "📄 Подзаголовок hero",
                                      "🔘 Кнопка hero (текст)", "🔗 Кнопка hero (URL)"})
async def site_banner_field_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    field_map = {
        "📝 Текст баннера":        ("banner_text",       "Введите текст баннера (отправьте «-» чтобы очистить):"),
        "🖼 Фото баннера (URL)":   ("banner_image_url",  "Введите URL изображения баннера (отправьте «-» чтобы очистить):"),
        "🏷 Заголовок hero":       ("hero_title",        "Введите заголовок hero-баннера (отправьте «-» чтобы очистить):"),
        "📄 Подзаголовок hero":    ("hero_subtitle",     "Введите подзаголовок hero-баннера (отправьте «-» чтобы очистить):"),
        "🔘 Кнопка hero (текст)":  ("hero_button_text",  "Введите текст кнопки hero-баннера (отправьте «-» чтобы очистить):"),
        "🔗 Кнопка hero (URL)":    ("hero_button_url",   "Введите URL кнопки hero-баннера (отправьте «-» чтобы очистить):"),
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
    await db.set_setting("hero_title", "")
    await db.set_setting("hero_subtitle", "")
    await db.set_setting("hero_button_text", "")
    await db.set_setting("hero_button_url", "")
    await message.answer("✅ Баннер сброшен.", reply_markup=site_banner_kb)


# ===== Multi-banner slider CMS =====

def _banner_list_kb(banners):
    """Inline-клавиатура: список баннеров + кнопка добавить."""
    rows = []
    for b in banners:
        title = (b["title"] or b["subtitle"] or "Без названия")[:40]
        state = "✅" if b["is_active"] else "❌"
        rows.append([
            InlineKeyboardButton(
                text=f"{state} #{b['id']} {title}",
                callback_data=f"bnr_open:{b['id']}",
            )
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить баннер", callback_data="bnr_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="bnr_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _banner_edit_kb(banner_id: int, is_active: bool):
    """Inline-клавиатура управления одним баннером."""
    toggle_text = "👁 Выключить" if is_active else "👁 Включить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Заголовок",   callback_data=f"bnr_ef:{banner_id}:title"),
            InlineKeyboardButton(text="📄 Подзаголовок", callback_data=f"bnr_ef:{banner_id}:subtitle"),
        ],
        [
            InlineKeyboardButton(text="🔘 Кнопка (текст)", callback_data=f"bnr_ef:{banner_id}:button_text"),
            InlineKeyboardButton(text="🔗 Кнопка (URL)",   callback_data=f"bnr_ef:{banner_id}:button_link"),
        ],
        [
            InlineKeyboardButton(text="🖼 Фото (URL)",  callback_data=f"bnr_ef:{banner_id}:image_url"),
            InlineKeyboardButton(text="🔢 Порядок",    callback_data=f"bnr_ef:{banner_id}:sort_order"),
        ],
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"bnr_toggle:{banner_id}"),
            InlineKeyboardButton(text="🗑 Удалить",    callback_data=f"bnr_delete:{banner_id}"),
        ],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="bnr_list")],
    ])


async def _send_banners_list(message, edit: bool = False):
    banners = await db.list_all_banners()
    text = f"🖼 Баннеры сайта ({len(banners)} шт.):"
    kb = _banner_list_kb(banners)
    try:
        if edit:
            await message.edit_text(text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)
    except Exception:
        await message.answer(text, reply_markup=kb)


async def _send_banner_card(message, banner_id: int, edit: bool = False):
    b = await db.get_banner(banner_id)
    if not b:
        await message.answer("Баннер не найден.")
        return
    state_text = "✅ Активен" if b["is_active"] else "❌ Выключен"
    text = (
        f"🖼 Баннер #{b['id']}\n"
        f"Статус: {state_text}\n"
        f"Заголовок: {b['title'] or '—'}\n"
        f"Подзаголовок: {b['subtitle'] or '—'}\n"
        f"Кнопка: {b['button_text'] or '—'} → {b['button_link'] or '—'}\n"
        f"Фото: {b['image_url'] or '—'}\n"
        f"Порядок: {b['sort_order']}"
    )
    kb = _banner_edit_kb(banner_id, bool(b["is_active"]))
    try:
        if edit:
            await message.edit_text(text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)
    except Exception:
        await message.answer(text, reply_markup=kb)


@router.message(lambda m: m.text == "🖼 Баннеры")
async def banners_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await _send_banners_list(message)


@router.callback_query(lambda c: c.data == "bnr_list")
async def bnr_list_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _send_banners_list(callback.message, edit=True)
    await callback.answer()


@router.callback_query(lambda c: c.data == "bnr_close")
async def bnr_close_callback(callback: CallbackQuery):
    try:
        await callback.message.edit_text("✅ Закрыто.")
    except Exception:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("bnr_open:"))
async def bnr_open_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    banner_id = int(callback.data.split(":")[1])
    await _send_banner_card(callback.message, banner_id, edit=True)
    await callback.answer()


# — Добавить баннер: пошаговый FSM —

@router.callback_query(lambda c: c.data == "bnr_add")
async def bnr_add_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(BannersAddState.title)
    await callback.message.answer(
        "➕ Добавить баннер\n\n"
        "Шаг 1/6 — Заголовок (например: «Акция — скидки до 30%»)\n"
        "Отправьте «-» чтобы пропустить."
    )
    await callback.answer()


@router.message(BannersAddState.title)
async def bnr_add_title(message: Message, state: FSMContext):
    await state.update_data(title="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(BannersAddState.subtitle)
    await message.answer("Шаг 2/6 — Подзаголовок (отправьте «-» чтобы пропустить):")


@router.message(BannersAddState.subtitle)
async def bnr_add_subtitle(message: Message, state: FSMContext):
    await state.update_data(subtitle="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(BannersAddState.button_text)
    await message.answer("Шаг 3/6 — Текст кнопки (например: «Смотреть»; «-» пропустить):")


@router.message(BannersAddState.button_text)
async def bnr_add_button_text(message: Message, state: FSMContext):
    await state.update_data(button_text="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(BannersAddState.button_link)
    await message.answer("Шаг 4/6 — URL кнопки (например: «/?category=konditsionery»; «-» пропустить):")


@router.message(BannersAddState.button_link)
async def bnr_add_button_link(message: Message, state: FSMContext):
    await state.update_data(button_link="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(BannersAddState.image_url)
    await message.answer("Шаг 5/6 — URL изображения баннера («-» пропустить):")


@router.message(BannersAddState.image_url)
async def bnr_add_image_url(message: Message, state: FSMContext):
    await state.update_data(image_url="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(BannersAddState.sort_order)
    await message.answer("Шаг 6/6 — Порядок сортировки (число; меньше = раньше; по умолчанию 100; «-» пропустить):")


@router.message(BannersAddState.sort_order)
async def bnr_add_sort_order(message: Message, state: FSMContext):
    raw = message.text.strip()
    sort_order = 100
    if raw != "-":
        try:
            sort_order = int(raw)
        except ValueError:
            await message.answer("Введите число или «-» для пропуска:")
            return
    data = await state.get_data()
    await state.clear()
    banner = await db.create_banner(
        title=data.get("title", ""),
        subtitle=data.get("subtitle", ""),
        button_text=data.get("button_text", ""),
        button_link=data.get("button_link", ""),
        image_url=data.get("image_url", ""),
        sort_order=sort_order,
    )
    await message.answer(f"✅ Баннер #{banner['id']} создан!")
    await _send_banners_list(message)


# — Редактировать поле баннера —

_BANNER_FIELD_LABELS = {
    "title": "Заголовок",
    "subtitle": "Подзаголовок",
    "button_text": "Текст кнопки",
    "button_link": "URL кнопки",
    "image_url": "URL изображения",
    "sort_order": "Порядок сортировки",
}


@router.callback_query(lambda c: c.data and c.data.startswith("bnr_ef:"))
async def bnr_edit_field_start(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    banner_id, field = int(parts[1]), parts[2]
    if field not in _BANNER_FIELD_LABELS:
        await callback.answer("Неизвестное поле", show_alert=True)
        return
    await state.update_data(bnr_edit_id=banner_id, bnr_edit_field=field)
    await state.set_state(BannersEditState.waiting_for_value)
    label = _BANNER_FIELD_LABELS[field]
    hint = " (число)" if field == "sort_order" else ""
    if field == "image_url":
        prompt = (
            f"✏️ Редактирование баннера #{banner_id}\n"
            f"Поле: {label}\n"
            "Отправьте фото (бот загрузит в облако) или URL изображения.\n"
            "«-» — очистить."
        )
    else:
        prompt = (
            f"✏️ Редактирование баннера #{banner_id}\n"
            f"Поле: {label}{hint}\n"
            "Отправьте новое значение или «-» чтобы очистить:"
        )
    await callback.message.answer(prompt)
    await callback.answer()


@router.message(BannersEditState.waiting_for_value, lambda m: m.photo)
async def bnr_edit_field_photo(message: Message, state: FSMContext):
    """Принимает фото при редактировании поля image_url баннера."""
    data = await state.get_data()
    banner_id = data.get("bnr_edit_id")
    field = data.get("bnr_edit_field")

    if not banner_id or field != "image_url":
        # Фото отправлено не в режиме image_url — игнорируем
        await message.answer("Сейчас ожидается текстовое значение, а не фото. Отправьте текст или «-».")
        return

    # Largest photo size
    photo = message.photo[-1]
    try:
        secure_url = await save_telegram_photo(message.bot, photo.file_id)
    except Exception as e:
        print(f"[bnr_edit_photo] upload failed: {e}")
        await message.answer(
            "⚠️ Не удалось загрузить фото (Cloudinary недоступен или ошибка соединения).\n"
            "Попробуйте ещё раз или отправьте URL изображения."
        )
        return

    await db.update_banner_field(banner_id, "image_url", secure_url)
    await state.clear()
    await message.answer("✅ Фото баннера обновлено.")
    await _send_banner_card(message, banner_id)


@router.message(BannersEditState.waiting_for_value)
async def bnr_edit_field_save(message: Message, state: FSMContext):
    data = await state.get_data()
    banner_id = data.get("bnr_edit_id")
    field = data.get("bnr_edit_field")
    if not banner_id or not field:
        await state.clear()
        await message.answer("Ошибка состояния.")
        return

    raw = (message.text or "").strip()
    if raw == "-":
        value = "" if field != "sort_order" else 100
    elif field == "sort_order":
        try:
            value = int(raw)
        except ValueError:
            await message.answer("Введите целое число:")
            return
    elif field == "image_url":
        if not raw.startswith("http"):
            await message.answer(
                "Отправьте URL изображения (начинается с http…), "
                "фото-файл или «-» чтобы очистить:"
            )
            return
        value = raw
    else:
        value = raw

    await db.update_banner_field(banner_id, field, value)
    await state.clear()
    await message.answer("✅ Сохранено.")
    await _send_banner_card(message, banner_id)


# — Включить/выключить —

@router.callback_query(lambda c: c.data and c.data.startswith("bnr_toggle:"))
async def bnr_toggle_callback(callback: CallbackQuery):
    banner_id = int(callback.data.split(":")[1])
    is_active = await db.toggle_banner_active(banner_id)
    state_text = "включён" if is_active else "выключен"
    await callback.answer(f"Баннер {state_text}", show_alert=False)
    await _send_banner_card(callback.message, banner_id, edit=True)


# — Удалить —

@router.callback_query(lambda c: c.data and c.data.startswith("bnr_delete:"))
async def bnr_delete_callback(callback: CallbackQuery):
    banner_id = int(callback.data.split(":")[1])
    await db.delete_banner(banner_id)
    await callback.answer("🗑 Баннер удалён", show_alert=False)
    await _send_banners_list(callback.message, edit=True)


# ===== SEO =====

def _seo_field_label(field: str) -> str:
    labels = {
        "meta_title": "🔤 meta title",
        "meta_description": "📄 meta description",
        "h1": "📌 H1",
        "seo_text": "📝 SEO-текст",
    }
    return labels.get(field, field)


DEFAULT_AUTO_SEO_TEMPLATES = {
    "seo_tpl_product_title": "{product_name} купити у Запоріжжі — {site_title}",
    "seo_tpl_product_desc": "{product_name} за ціною {price} грн. Гарантія {warranty} міс. Замовлення онлайн, консультація.",
    "seo_tpl_category_title": "{category_name} купити у Запоріжжі — {site_title}",
    "seo_tpl_category_desc": "{category_name}. Ціни, гарантія, доставка.",
}


def _apply_seo_template(template: str, vars_dict: dict) -> str:
    """Replace {variable} placeholders with values from vars_dict."""
    for key, value in vars_dict.items():
        template = template.replace(f"{{{key}}}", str(value) if value else "")
    return template.strip()


def _seo_field_hint(field: str) -> str:
    hints = {
        "meta_title": "📏 <i>Рекомендовано 50–60 символів (до 70)</i>",
        "meta_description": "📏 <i>Рекомендовано 120–160 символів (до 180)</i>",
        "h1": "💡 <i>Один заголовок на сторінку, включайте ключовий запит</i>",
        "seo_text": "📏 <i>Рекомендовано від 300 символів</i>",
    }
    return hints.get(field, "")


def _seo_length_warning(field: str, value: str) -> str:
    if not value:
        return ""
    length = len(value)
    if field == "meta_title" and length > 70:
        return f"\n⚠️ <b>Заголовок занадто довгий:</b> {length} символів (рекомендовано до 70)"
    if field == "meta_description" and length > 180:
        return f"\n⚠️ <b>Опис занадто довгий:</b> {length} символів (рекомендовано до 180)"
    return ""


def _seo_main_inline_kb() -> InlineKeyboardMarkup:
    fields = ["meta_title", "meta_description", "h1", "seo_text"]
    rows = [
        [InlineKeyboardButton(
            text=f"✏️ {_seo_field_label(f)}",
            callback_data=f"seo_ef:index:{f}"
        )]
        for f in fields
    ]
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="seo_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_seo_main_card(target, edit: bool = False):
    seo = await db.get_seo_page("index")

    def _v(val):
        return val if val else "—"

    text = (
        "🏠 <b>SEO: Главная страница</b>\n\n"
        f"🔤 <b>meta title:</b> {_v(seo['meta_title'])}\n"
        f"📄 <b>meta description:</b> {_v(seo['meta_description'])}\n"
        f"📌 <b>H1:</b> {_v(seo['h1'])}\n"
        f"📝 <b>SEO-текст:</b> {_v(seo['seo_text'][:80] + '…' if seo['seo_text'] and len(seo['seo_text']) > 80 else seo['seo_text'])}\n"
    )
    kb = _seo_main_inline_kb()
    if edit and hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(lambda m: m.text == "🔎 SEO")
async def seo_menu_handler(message: Message):
    if not (is_system_admin(message.from_user.id) or await is_admin(message)):
        return
    await message.answer("🔎 SEO", reply_markup=seo_kb)


@router.message(lambda m: m.text == "🏠 Главная страница")
async def seo_main_page_handler(message: Message):
    if not (is_system_admin(message.from_user.id) or await is_admin(message)):
        return
    await _send_seo_main_card(message)


@router.message(lambda m: m.text == "📂 Категории SEO")
async def seo_categories_handler(message: Message):
    if not (is_system_admin(message.from_user.id) or await is_admin(message)):
        return
    cats = await db.list_active_site_categories()
    if not cats:
        await message.answer("Категорий сайта пока нет.")
        return
    rows = []
    for cat in cats:
        rows.append([InlineKeyboardButton(
            text=f"{cat.get('emoji', '📦')} {cat['name_ru']}",
            callback_data=f"seo_cat_open:{cat['id']}"
        )])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="seo_close")])
    await message.answer(
        "📂 <b>SEO категорий — выберите категорию:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )


def _seo_cat_card_text(cat_name: str, seo) -> str:
    def _v(val):
        return val if val else "—"
    indexable = seo["indexable"] if seo is not None else True
    idx_icon = "✅" if indexable else "🚫"
    idx_label = "Да" if indexable else "Нет"
    seo_text_preview = ""
    if seo and seo["seo_text"]:
        seo_text_preview = seo["seo_text"][:80] + ("…" if len(seo["seo_text"]) > 80 else "")
    return (
        f"📂 <b>SEO: {cat_name}</b>\n\n"
        f"🔤 <b>meta title:</b> {_v(seo['meta_title'] if seo else '')}\n"
        f"📄 <b>meta description:</b> {_v(seo['meta_description'] if seo else '')}\n"
        f"📌 <b>H1:</b> {_v(seo['h1'] if seo else '')}\n"
        f"📝 <b>SEO-текст:</b> {_v(seo_text_preview)}\n"
        f"{idx_icon} <b>Индексация:</b> {idx_label}\n"
    )


def _seo_cat_inline_kb(cat_id: int, seo) -> InlineKeyboardMarkup:
    fields = ["meta_title", "meta_description", "h1", "seo_text"]
    rows = [
        [InlineKeyboardButton(
            text=f"✏️ {_seo_field_label(f)}",
            callback_data=f"seo_cat_ef:{cat_id}:{f}"
        )]
        for f in fields
    ]
    indexable = seo["indexable"] if seo is not None else True
    idx_text = "🚫 Выключить индексацию" if indexable else "✅ Включить индексацию"
    rows.append([InlineKeyboardButton(text=idx_text, callback_data=f"seo_cat_idx:{cat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="seo_cat_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data == "seo_cat_list")
async def seo_cat_list_callback(callback: CallbackQuery):
    cats = await db.list_active_site_categories()
    if not cats:
        await callback.answer("Категорий нет", show_alert=False)
        return
    rows = []
    for cat in cats:
        rows.append([InlineKeyboardButton(
            text=f"{cat.get('emoji', '📦')} {cat['name_ru']}",
            callback_data=f"seo_cat_open:{cat['id']}"
        )])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="seo_close")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(
            "📂 <b>SEO категорий — выберите категорию:</b>",
            reply_markup=kb, parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            "📂 <b>SEO категорий — выберите категорию:</b>",
            reply_markup=kb, parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("seo_cat_open:"))
async def seo_cat_open_callback(callback: CallbackQuery):
    cat_id = int(callback.data.split(":")[1])
    cats = await db.list_site_categories()
    cat = next((c for c in cats if c["id"] == cat_id), None)
    if not cat:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    seo = await db.get_category_seo(cat_id)
    text = _seo_cat_card_text(cat["name_ru"], seo)
    kb = _seo_cat_inline_kb(cat_id, seo)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("seo_cat_idx:"))
async def seo_cat_idx_callback(callback: CallbackQuery):
    cat_id = int(callback.data.split(":")[1])
    new_val = await db.toggle_category_seo_indexable(cat_id)
    cats = await db.list_site_categories()
    cat = next((c for c in cats if c["id"] == cat_id), None)
    if not cat:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    seo = await db.get_category_seo(cat_id)
    text = _seo_cat_card_text(cat["name_ru"], seo)
    kb = _seo_cat_inline_kb(cat_id, seo)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    idx_label = "включена" if new_val else "выключена"
    await callback.answer(f"Индексация {idx_label}", show_alert=False)


@router.callback_query(lambda c: c.data and c.data.startswith("seo_cat_ef:"))
async def seo_cat_edit_start(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    cat_id = int(parts[1])
    field = parts[2]
    label = _seo_field_label(field)
    hint = _seo_field_hint(field)
    await state.set_state(SeoCategoryState.waiting_for_value)
    await state.update_data(seo_cat_id=cat_id, seo_field=field)
    hint_line = f"\n{hint}" if hint else ""
    await callback.message.answer(
        f"✏️ Введите новое значение для <b>{label}</b>{hint_line}\n"
        f"Отправьте «-» чтобы очистить поле.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SeoCategoryState.waiting_for_value)
async def seo_cat_edit_save(message: Message, state: FSMContext):
    data = await state.get_data()
    cat_id = data.get("seo_cat_id")
    field = data.get("seo_field")
    if not cat_id or not field:
        await state.clear()
        await message.answer("Ошибка состояния.")
        return
    raw = (message.text or "").strip()
    value = "" if raw == "-" else raw
    await db.upsert_category_seo_field(cat_id, field, value)
    await state.clear()
    warning = _seo_length_warning(field, value)
    await message.answer(f"✅ Сохранено.{warning}", parse_mode="HTML")
    cats = await db.list_site_categories()
    cat = next((c for c in cats if c["id"] == cat_id), None)
    if cat:
        seo = await db.get_category_seo(cat_id)
        await message.answer(
            _seo_cat_card_text(cat["name_ru"], seo),
            reply_markup=_seo_cat_inline_kb(cat_id, seo),
            parse_mode="HTML",
        )


# ===== SEO: Products =====

def _seo_product_card_text(product, seo) -> str:
    def _v(val):
        return val if val else "—"
    indexable = seo["indexable"] if seo is not None else True
    idx_icon = "✅" if indexable else "🚫"
    idx_label = "Да" if indexable else "Нет"
    seo_text_preview = ""
    if seo and seo["seo_text"]:
        seo_text_preview = seo["seo_text"][:80] + ("…" if len(seo["seo_text"]) > 80 else "")
    prod_name = f"{product.get('brand') or ''} {product.get('model') or ''}".strip() if product else ""
    return (
        f"🔎 <b>SEO: {prod_name}</b>\n\n"
        f"🔤 <b>meta title:</b> {_v(seo['meta_title'] if seo else '')}\n"
        f"📄 <b>meta description:</b> {_v(seo['meta_description'] if seo else '')}\n"
        f"📌 <b>H1:</b> {_v(seo['h1'] if seo else '')}\n"
        f"📝 <b>SEO-текст:</b> {_v(seo_text_preview)}\n"
        f"{idx_icon} <b>Индексация:</b> {idx_label}\n"
    )


def _seo_product_inline_kb(product_id: int, seo) -> InlineKeyboardMarkup:
    fields = ["meta_title", "meta_description", "h1", "seo_text"]
    rows = [
        [InlineKeyboardButton(
            text=f"✏️ {_seo_field_label(f)}",
            callback_data=f"seo_prod_ef:{product_id}:{f}"
        )]
        for f in fields
    ]
    indexable = seo["indexable"] if seo is not None else True
    idx_text = "🚫 Выключить индексацию" if indexable else "✅ Включить индексацию"
    rows.append([InlineKeyboardButton(text=idx_text, callback_data=f"seo_prod_idx:{product_id}")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="seo_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data and c.data.startswith("seo_prod_open:"))
async def seo_prod_open_callback(callback: CallbackQuery):
    try:
        product_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return
    product = await db.get_product_by_id(product_id)
    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return
    seo = await db.get_product_seo(product_id)
    await callback.message.answer(
        _seo_product_card_text(product, seo),
        reply_markup=_seo_product_inline_kb(product_id, seo),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("seo_prod_idx:"))
async def seo_prod_idx_callback(callback: CallbackQuery):
    try:
        product_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return
    new_val = await db.toggle_product_seo_indexable(product_id)
    product = await db.get_product_by_id(product_id)
    seo = await db.get_product_seo(product_id)
    text = _seo_product_card_text(product, seo)
    kb = _seo_product_inline_kb(product_id, seo)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    idx_label = "включена" if new_val else "выключена"
    await callback.answer(f"Индексация {idx_label}", show_alert=False)


@router.callback_query(lambda c: c.data and c.data.startswith("seo_prod_ef:"))
async def seo_prod_edit_start(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    try:
        product_id = int(parts[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return
    field = parts[2]
    label = _seo_field_label(field)
    hint = _seo_field_hint(field)
    await state.set_state(SeoProductState.waiting_for_value)
    await state.update_data(seo_product_id=product_id, seo_field=field)
    hint_line = f"\n{hint}" if hint else ""
    await callback.message.answer(
        f"✏️ Введите новое значение для <b>{label}</b>{hint_line}\n"
        f"Отправьте «-» чтобы очистить поле.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SeoProductState.waiting_for_value)
async def seo_prod_edit_save(message: Message, state: FSMContext):
    data = await state.get_data()
    product_id = data.get("seo_product_id")
    field = data.get("seo_field")
    if not product_id or not field:
        await state.clear()
        await message.answer("Ошибка состояния.")
        return
    raw = (message.text or "").strip()
    value = "" if raw == "-" else raw
    await db.upsert_product_seo_field(product_id, field, value)
    await state.clear()
    warning = _seo_length_warning(field, value)
    await message.answer(f"✅ Сохранено.{warning}", parse_mode="HTML")
    product = await db.get_product_by_id(product_id)
    seo = await db.get_product_seo(product_id)
    await message.answer(
        _seo_product_card_text(product, seo),
        reply_markup=_seo_product_inline_kb(product_id, seo),
        parse_mode="HTML",
    )


@router.message(lambda m: m.text == "🗺 Sitemap")
async def seo_sitemap_handler(message: Message):
    if not (is_system_admin(message.from_user.id) or await is_admin(message)):
        return
    base = await db.get_setting("site_url") or ""
    url = f"{base.rstrip('/')}/sitemap.xml" if base else "/sitemap.xml"
    await message.answer(f"🗺 Sitemap доступен по адресу:\n<code>{url}</code>", parse_mode="HTML")


@router.message(lambda m: m.text == "🤖 Robots.txt")
async def seo_robots_handler(message: Message):
    if not (is_system_admin(message.from_user.id) or await is_admin(message)):
        return
    base = await db.get_setting("site_url") or ""
    url = f"{base.rstrip('/')}/robots.txt" if base else "/robots.txt"
    await message.answer(f"🤖 Robots.txt доступен по адресу:\n<code>{url}</code>", parse_mode="HTML")


@router.message(lambda m: m.text == "ℹ️ SEO Підказка")
async def seo_hint_handler(message: Message):
    if not (is_system_admin(message.from_user.id) or await is_admin(message)):
        return
    text = (
        "ℹ️ <b>SEO Підказка</b>\n\n"
        "🔤 <b>Title (мета-заголовок)</b>\n"
        "Назва сторінки у вкладці браузера та у рядку пошукової видачі.\n"
        "📏 Рекомендовано: <b>50–60 символів</b>. До 70 — допустимо.\n\n"
        "📄 <b>Description (мета-опис)</b>\n"
        "Короткий опис під заголовком у результатах пошуку.\n"
        "📏 Рекомендовано: <b>120–160 символів</b>. До 180 — допустимо.\n\n"
        "📌 <b>H1</b>\n"
        "Головний заголовок на сторінці — <b>один</b> на сторінку.\n"
        "Має відображати суть сторінки. Бажано включати ключовий запит.\n\n"
        "📝 <b>SEO-текст</b>\n"
        "Текст внизу сторінки для пошукових роботів та відвідувачів.\n"
        "📏 Рекомендовано: <b>від 300 символів</b>.\n\n"
        "✅ <b>Як писати без переспаму</b>\n"
        "• Кожне ключове слово — не більше 2–3 разів у тексті\n"
        "• Текст має бути природним і корисним для людини\n"
        "• Не дублюйте title і H1 дослівно\n"
        "• Уникайте повторів типу «купити купити купити» — це спам\n"
        "• Title і description — унікальні для кожної сторінки"
    )
    await message.answer(text, parse_mode="HTML")


# ===== Auto SEO Templates =====

_AUTO_SEO_LABEL_MAP = {
    "seo_tpl_product_title": "📦 Title товару",
    "seo_tpl_product_desc": "📦 Description товару",
    "seo_tpl_category_title": "📂 Title категорії",
    "seo_tpl_category_desc": "📂 Description категорії",
}

_AUTO_SEO_VARS_LINE = (
    "\n\n🔧 <b>Доступні змінні:</b>\n"
    "<code>{product_name}</code> <code>{category_name}</code> "
    "<code>{price}</code> <code>{brand}</code> "
    "<code>{warranty}</code> <code>{site_title}</code>"
)


async def _send_auto_seo_card(target, edit: bool = False):
    tpls = await db.get_auto_seo_templates()

    def _tv(key):
        val = (tpls.get(key) or "").strip()
        if not val:
            default = DEFAULT_AUTO_SEO_TEMPLATES.get(key, "")
            return f"<i>{default}</i> <i>(за замовчуванням)</i>"
        return val[:120] + ("…" if len(val) > 120 else "")

    text = (
        "⚙️ <b>Авто SEO — шаблони</b>\n\n"
        f"📦 <b>Title товару:</b>\n{_tv('seo_tpl_product_title')}\n\n"
        f"📦 <b>Description товару:</b>\n{_tv('seo_tpl_product_desc')}\n\n"
        f"📂 <b>Title категорії:</b>\n{_tv('seo_tpl_category_title')}\n\n"
        f"📂 <b>Description категорії:</b>\n{_tv('seo_tpl_category_desc')}\n\n"
        "🔧 <b>Змінні:</b> <code>{product_name}</code> <code>{category_name}</code> "
        "<code>{price}</code> <code>{brand}</code> <code>{warranty}</code> <code>{site_title}</code>"
    )
    rows = [
        [InlineKeyboardButton(text="✏️ Title товару", callback_data="auto_seo_ef:seo_tpl_product_title")],
        [InlineKeyboardButton(text="✏️ Description товару", callback_data="auto_seo_ef:seo_tpl_product_desc")],
        [InlineKeyboardButton(text="✏️ Title категорії", callback_data="auto_seo_ef:seo_tpl_category_title")],
        [InlineKeyboardButton(text="✏️ Description категорії", callback_data="auto_seo_ef:seo_tpl_category_desc")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="seo_close")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if edit and hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(lambda m: m.text == "⚙️ Авто SEO")
async def auto_seo_handler(message: Message):
    if not (is_system_admin(message.from_user.id) or await is_admin(message)):
        return
    await _send_auto_seo_card(message)


@router.callback_query(lambda c: c.data and c.data.startswith("auto_seo_ef:"))
async def auto_seo_edit_start(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in _AUTO_SEO_LABEL_MAP:
        await callback.answer("Невідомий ключ", show_alert=True)
        return
    label = _AUTO_SEO_LABEL_MAP[key]
    default = DEFAULT_AUTO_SEO_TEMPLATES.get(key, "")
    await state.set_state(AutoSeoState.waiting_for_value)
    await state.update_data(auto_seo_key=key)
    default_line = f"\n\n💡 <i>Поточний за замовчуванням:</i>\n<code>{default}</code>" if default else ""
    await callback.message.answer(
        f"✏️ Введіть новий шаблон для <b>{label}</b>{default_line}{_AUTO_SEO_VARS_LINE}\n\n"
        f"Відправте «-» щоб скинути до стандартного.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AutoSeoState.waiting_for_value)
async def auto_seo_edit_save(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("auto_seo_key")
    if not key or key not in _AUTO_SEO_LABEL_MAP:
        await state.clear()
        await message.answer("Помилка стану.")
        return
    raw = (message.text or "").strip()
    value = "" if raw == "-" else raw
    await db.set_setting(key, value)
    await state.clear()
    status = "✅ Шаблон збережено." if value else "✅ Шаблон скинуто до стандартного."
    await message.answer(status)
    await _send_auto_seo_card(message)


@router.callback_query(lambda c: c.data and c.data.startswith("seo_ef:"))
async def seo_edit_field_start(callback: CallbackQuery, state: FSMContext):
    _, page_key, field = callback.data.split(":")
    label = _seo_field_label(field)
    hint = _seo_field_hint(field)
    await state.set_state(SeoMainState.waiting_for_value)
    await state.update_data(seo_page_key=page_key, seo_field=field)
    hint_line = f"\n{hint}" if hint else ""
    await callback.message.answer(
        f"✏️ Введите новое значение для <b>{label}</b>{hint_line}\n"
        f"Отправьте «-» чтобы очистить поле.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "seo_close")
async def seo_close_callback(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.message(SeoMainState.waiting_for_value)
async def seo_edit_field_save(message: Message, state: FSMContext):
    data = await state.get_data()
    page_key = data.get("seo_page_key", "index")
    field = data.get("seo_field")
    if not field:
        await state.clear()
        await message.answer("Ошибка состояния.")
        return

    raw = (message.text or "").strip()
    value = "" if raw == "-" else raw

    await db.set_seo_page_field(page_key, field, value)
    await state.clear()
    warning = _seo_length_warning(field, value)
    await message.answer(f"✅ Сохранено.{warning}", parse_mode="HTML")
    await _send_seo_main_card(message)


# ===== Promo bar =====

class SitePromoState(StatesGroup):
    waiting_for_field = State()


def _normalize_promo_date(value: str) -> str | None:
    """Normalize user-entered date to YYYY-MM-DDTHH:MM:SS+03:00.
    Accepts: 'DD.MM.YYYY HH:MM', 'DD.MM.YYYY', 'YYYY-MM-DD HH:MM', 'YYYY-MM-DD'.
    Returns None if parsing fails.
    """
    value = value.strip()
    formats = [
        ("%d.%m.%Y %H:%M", True),
        ("%d.%m.%Y %H:%M:%S", True),
        ("%d.%m.%Y", False),
        ("%Y-%m-%d %H:%M", True),
        ("%Y-%m-%d %H:%M:%S", True),
        ("%Y-%m-%d", False),
    ]
    for fmt, has_time in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if not has_time:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")
        except ValueError:
            continue
    return None


@router.message(lambda m: m.text == "📣 Промо-плашка")
async def site_promo_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await message.answer("Промо-плашка:", reply_markup=site_promo_kb)


@router.message(lambda m: m.text == "📋 Показать промо")
async def site_promo_show(message: Message):
    if not await require_admin(message):
        return
    design = await get_site_design()
    state_text = "ВКЛ" if design["promo_enabled"] else "ВЫКЛ"
    await message.answer(
        "📣 Промо-плашка:\n\n"
        f"Состояние: {state_text}\n"
        f"Текст: {design['promo_text'] or '-'}\n"
        f"Дата окончания: {design['promo_end_date'] or 'не задана (показывать всегда)'}"
    )


@router.message(lambda m: m.text in {"📝 Текст промо", "📅 Дата окончания промо"})
async def site_promo_field_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    field_map = {
        "📝 Текст промо":           ("promo_text",     "Введите текст промо-плашки (отправьте «-» чтобы очистить):\nПример: 🎯 Безкоштовна доставка по Запоріжжю до 18.05"),
        "📅 Дата окончания промо":  ("promo_end_date", "Введіть дату закінчення промо.\nФормат: 2026-05-20 23:59 або 20.05.2026 23:59\nЯкщо не вказати час — буде 23:59 за Києвом.\nВідправте «-» щоб прибрати дату — плашка буде показуватись завжди."),
    }
    key, prompt = field_map[message.text]
    await state.update_data(setting_key=key)
    await state.set_state(SitePromoState.waiting_for_field)
    await message.answer(prompt)


@router.message(SitePromoState.waiting_for_field)
async def site_promo_field_save(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")
    if not key:
        await state.clear()
        await message.answer("Ошибка состояния.", reply_markup=site_promo_kb)
        return
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    elif key == "promo_end_date" and value:
        value = _normalize_promo_date(value)
        if value is None:
            await message.answer(
                "❌ Невірний формат дати.\n"
                "Приклади: 2026-05-20 23:59  або  20.05.2026 23:59\n"
                "Спробуйте ще раз або відправте «-» щоб прибрати дату."
            )
            return
    await db.set_setting(key, value)
    await state.clear()
    await message.answer("✅ Збережено", reply_markup=site_promo_kb)


@router.message(lambda m: m.text == "👁 Промо: вкл/выкл")
async def site_promo_toggle(message: Message):
    if not await require_admin(message):
        return
    value = await db.toggle_setting_bool("promo_enabled", "false")
    text = "включена" if value == "true" else "выключена"
    await message.answer(f"✅ Промо-плашка {text}", reply_markup=site_promo_kb)


@router.message(lambda m: m.text == "♻️ Сбросить промо")
async def site_promo_reset(message: Message):
    if not await require_admin(message):
        return
    await db.set_setting("promo_text", "")
    await db.set_setting("promo_end_date", "")
    await db.set_setting("promo_enabled", "false")
    await message.answer("✅ Промо-плашка сброшена.", reply_markup=site_promo_kb)


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
    remote = await get_saas_client_domain()
    domain = remote.get("domain") or info.get("pay_domain_name") or "—"
    status = remote.get("status") or info.get("pay_domain_status") or "—"
    dns_raw = remote.get("dns_connected")
    if dns_raw is None:
        dns_text = "—"
    else:
        dns_text = "✅ підключено" if dns_raw else "❌ не підключено"
    expires = remote.get("expires_at") or info.get("pay_domain_expires") or "—"
    await message.answer(
        "🌐 Домен\n\n"
        f"Текущий домен: {domain}\n"
        f"Статус: {status}\n"
        f"DNS: {dns_text}\n"
        f"Дата окончания: {expires}",
        reply_markup=payment_domain_kb
    )


@router.message(lambda m: m.text == "📋 Інструкція підключення")
async def payment_domain_instruction_handler(message: Message):
    await message.answer(
        "📋 Інструкція підключення домену\n\n"
        "1. Додайте A-record\n"
        "2. Або CNAME\n"
        "3. Підключіть Railway Custom Domain",
        reply_markup=payment_domain_kb
    )


@router.message(lambda m: m.text == "💳 Продовжити домен")
async def payment_domain_extend_handler(message: Message):
    # reuse existing domain-payment flow
    await payment_pay_stub_handler_internal(message, message.from_user, "💰 Оплатить домен")


@router.message(lambda m: m.text == "🧾 История оплат")
async def payment_history_handler(message: Message):
    payments = await get_saas_client_payments()
    if not payments:
        await message.answer("История оплат пуста", reply_markup=payment_back_kb)
        return

    status_icons = {
        "paid": "✅",
        "success": "✅",
        "completed": "✅",
        "pending": "⏳",
        "processing": "⏳",
        "failed": "❌",
        "error": "❌",
        "cancelled": "❌",
    }
    type_labels = {
        "subscription": "Подписка",
        "domain": "Домен",
    }

    lines = ["🧾 История оплат (последние платежи):"]
    for p in payments[:20]:
        if not isinstance(p, dict):
            continue
        date = p.get("date") or p.get("created_at") or p.get("paid_at") or "—"
        ptype_raw = str(p.get("type") or "").lower()
        ptype = type_labels.get(ptype_raw, p.get("type") or "—")
        amount = p.get("amount")
        if amount is None:
            amount_text = "—"
        else:
            amount_text = f"{amount}"
            currency = p.get("currency")
            if currency:
                amount_text = f"{amount} {currency}"
        status_raw = str(p.get("status") or "").lower()
        icon = status_icons.get(status_raw, "•")
        lines.append(f"\n{icon} {date} — {ptype}\n   Сумма: {amount_text} | Статус: {p.get('status') or '—'}")

    await message.answer("\n".join(lines), reply_markup=payment_back_kb)


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
    "📄 Страницы сайта", "🚚 Доставка", "🛡 Гарантия", "↩️ Повернення", "✏️ Изменить текст",
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
        _lang = await _user_lang(message.from_user.id)
        await message.answer(await t(message, "enter_search"), reply_markup=inline_categories_kb(_lang))
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

    if text == "📄 Страницы сайта":
        if not await require_admin(message):
            return
        await state.clear()
        await message.answer("📄 Страницы сайта:", reply_markup=site_pages_kb)
        return

    if text in {"🚚 Доставка", "🛡 Гарантия", "↩️ Повернення"}:
        if not await require_admin(message):
            return
        key, label = PAGE_BUTTONS.get(text, (None, None))
        if not key:
            return
        current = await db.get_setting(key) or PAGE_DEFAULTS.get(key, "")
        await state.clear()
        await state.update_data(page_key=key, page_label=label)
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="✏️ Изменить текст")],
                [KeyboardButton(text="⬅️ Назад")],
            ],
            resize_keyboard=True
        )
        await message.answer(
            f"📄 {label}\n\nТекущий текст:\n\n{current}",
            reply_markup=kb
        )
        return

    if text == "✏️ Изменить текст":
        if not await require_admin(message):
            return
        data = await state.get_data()
        key = data.get("page_key")
        label = data.get("page_label")
        if not key:
            await message.answer("Сначала выберите страницу.", reply_markup=site_pages_kb)
            return
        await state.set_state(SitePagesState.waiting_for_text)
        await message.answer(
            f"Отправьте новый текст для «{label}» или «-» чтобы сбросить к умолчанию.",
            reply_markup=ReplyKeyboardRemove()
        )
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


@router.message(lambda m: m.text == "⚙️ Справочники")
async def directories_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await message.answer("Справочники:", reply_markup=directories_kb)


@router.message(lambda m: m.text == "📁 Категории сайта")
async def directories_product_groups_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    cats = categories_for_lang("uk")
    existing = await db.list_product_groups()
    configured_keys = {g["category_key"] for g in existing}
    lines = []
    for c in cats:
        status = "✅" if c["key"] in configured_keys else "➖"
        lines.append(f"{status} {c['emoji']} <b>{c['name']}</b>")
    text = "📁 <b>Категорії сайту</b>:\n\n" + "\n".join(lines)
    await message.answer(text, parse_mode="HTML", reply_markup=directories_kb)


@router.message(lambda m: m.text == "🔧 Фильтры")
async def directories_filter_fields_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    # Получаем все уникальные category_key из filter_fields
    all_rows = await db.fetch(
        "SELECT id, category_key, field_key, label_ru, field_type, unit, is_active "
        "FROM filter_fields ORDER BY category_key, sort_order ASC, id ASC"
    )
    if not all_rows:
        await message.answer("🔧 Фильтров пока нет.", reply_markup=directories_kb)
        return
    # Группируем по category_key
    by_cat: dict = {}
    for r in all_rows:
        key = r["category_key"] or "(без категории)"
        by_cat.setdefault(key, [])
        unit = f" ({r['unit']}) " if r["unit"] else " "
        active = "✅" if r["is_active"] else "❌"
        by_cat[key].append(f"  {active} {r['field_key']} — {r['label_ru']}{unit}[{r['field_type']}]")
    lines = []
    for cat, fields in by_cat.items():
        lines.append(f"\n📂 <b>{cat}</b>")
        lines.extend(fields)
    text = "🔧 <b>Фильтры</b> ({}):{}".format(len(all_rows), "\n".join(lines))
    await message.answer(text, parse_mode="HTML", reply_markup=directories_kb)


@router.message(lambda m: m.text == "📋 Значения")
async def directories_filter_values_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await message.answer(
        "📋 Чтобы посмотреть значения, сначала выберите фильтр в разделе 🔧 Фильтры.",
        reply_markup=directories_kb
    )


@router.message(lambda m: m.text == "➕ Настроить категорию")
async def add_product_group_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await state.set_state(AddProductGroupState.waiting_for_name)
    cats = categories_for_lang("uk")
    buttons = [[KeyboardButton(text=c["name"])] for c in cats]
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    await message.answer(
        "📁 Оберіть категорію для налаштування:",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(AddProductGroupState.waiting_for_name)
async def add_product_group_name(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Оберіть категорію зі списку:")
        return
    key = category_key(text)
    if not key:
        cats = categories_for_lang("uk")
        buttons = [[KeyboardButton(text=c["name"])] for c in cats]
        buttons.append([KeyboardButton(text="⬅️ Назад")])
        await message.answer(
            "⚠️ Категорію не розпізнано. Оберіть зі списку:",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
        return
    existing = await db.list_product_groups()
    for g in existing:
        if g["category_key"] == key:
            await message.answer(
                f"ℹ️ Категорія «<b>{category_label(key, 'uk')}</b>» вже налаштована.",
                parse_mode="HTML",
                reply_markup=directories_kb,
            )
            await state.clear()
            return
    name = category_label(key, "uk")
    await db.create_product_group(category_key=key, name=name)
    await state.clear()
    cats = categories_for_lang("uk")
    updated = await db.list_product_groups()
    configured_keys = {g["category_key"] for g in updated}
    lines = []
    for c in cats:
        status = "✅" if c["key"] in configured_keys else "➖"
        lines.append(f"{status} {c['emoji']} <b>{c['name']}</b>")
    await message.answer(
        f"✅ Категорію «<b>{name}</b>» налаштовано.\n\n"
        "📁 <b>Категорії сайту</b>:\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=directories_kb,
    )


_FILTER_KEY_MAP = {
    "бренд": "brand", "brand": "brand",
    "літраж": "volume", "литраж": "volume", "обʼєм": "volume", "объём": "volume", "объем": "volume",
    "тип тена": "ten_type", "тип тэна": "ten_type",
    "монтаж": "mount_type",
    "форма": "shape",
    "потужність": "power", "мощность": "power",
    "площа": "area", "площадь": "area",
    "btu": "btu",
    "wi-fi": "wifi", "wifi": "wifi",
    "інвертор": "inverter", "инвертор": "inverter",
}


def _resolve_field_key(label: str) -> str:
    mapped = _FILTER_KEY_MAP.get(label.lower().strip())
    if mapped:
        return mapped
    return make_slug(label)


@router.message(lambda m: m.text == "➕ Добавить фильтр")
async def add_filter_field_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await state.set_state(AddFilterFieldState.waiting_for_group)
    cats = categories_for_lang("uk")
    buttons = [[KeyboardButton(text=c["name"])] for c in cats]
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    await message.answer(
        "📁 Оберіть категорію для фільтра:",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(AddFilterFieldState.waiting_for_group)
async def add_filter_field_group_selected(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    key = category_key(message.text)
    if not key:
        cats = categories_for_lang("uk")
        buttons = [[KeyboardButton(text=c["name"])] for c in cats]
        buttons.append([KeyboardButton(text="⬅️ Назад")])
        await message.answer(
            "⚠️ Оберіть категорію зі списку або натисніть ⬅️ Назад.",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
        return
    group_name = category_label(key, "uk")
    await state.update_data(category_key=key, group_name=group_name)
    await state.set_state(AddFilterFieldState.waiting_for_name)
    await message.answer(
        f"📂 Категорія: <b>{group_name}</b>\n\n"
        "🔤 Введіть назву фільтра:\n"
        "<i>наприклад: Бренд, Літраж, Тип ТЕНа, Монтаж</i>\n\n"
        "❌ Для скасування натисніть ⬅️ Назад.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True,
        ),
    )


@router.message(AddFilterFieldState.waiting_for_name)
async def add_filter_field_name(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    label = (message.text or "").strip()
    if not label:
        await message.answer("⚠️ Введіть назву фільтра:")
        return
    field_key = _resolve_field_key(label)
    await state.update_data(label=label, field_key=field_key)
    await state.set_state(AddFilterFieldState.waiting_for_type)
    await message.answer(
        f"🔑 field_key: <code>{field_key}</code>\n\n"
        "📊 Оберіть тип фільтра:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="select"), KeyboardButton(text="range"), KeyboardButton(text="boolean")],
                [KeyboardButton(text="⬅️ Назад")],
            ],
            resize_keyboard=True,
        ),
    )


@router.message(AddFilterFieldState.waiting_for_type)
async def add_filter_field_type(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    field_type = (message.text or "").strip().lower()
    if field_type not in ("select", "range", "boolean"):
        await message.answer("⚠️ Оберіть тип: select, range або boolean.")
        return
    data = await state.get_data()
    cat_key = data["category_key"]
    field_key = data["field_key"]
    label = data["label"]
    await db.create_filter_field(
        category_key=cat_key,
        field_key=field_key,
        label_ru=label,
        label_uk=label,
        field_type=field_type,
    )
    await state.clear()
    fields = await db.list_filter_fields(cat_key)
    lines = [
        f"• <b>{f['label_ru']}</b> | <code>{f['field_key']}</code> | {f['field_type']}"
        for f in fields
    ]
    fields_text = "\n".join(lines) if lines else "—"
    await message.answer(
        f"✅ Фільтр «<b>{label}</b>» додано.\n\n"
        f"🔧 <b>Фільтри групи «{data['group_name']}»</b> ({len(fields)}):\n{fields_text}",
        parse_mode="HTML",
        reply_markup=directories_kb,
    )


@router.message(lambda m: m.text == "➕ Добавить значение")
async def add_filter_value_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await state.set_state(AddFilterValueState.waiting_for_category)
    cats = categories_for_lang("uk")
    buttons = [[KeyboardButton(text=c["name"])] for c in cats]
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    await message.answer(
        "📁 Оберіть категорію:",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(AddFilterValueState.waiting_for_category)
async def add_filter_value_category(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    key = category_key(message.text)
    if not key:
        cats = categories_for_lang("uk")
        buttons = [[KeyboardButton(text=c["name"])] for c in cats]
        buttons.append([KeyboardButton(text="⬅️ Назад")])
        await message.answer(
            "⚠️ Оберіть категорію зі списку або натисніть ⬅️ Назад.",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
        return
    fields = await db.list_filter_fields(key)
    if not fields:
        await message.answer(
            f"⚠️ Для категорії «{category_label(key, 'uk')}» ще немає фільтрів.\n"
            "Спочатку додайте фільтр.",
            reply_markup=directories_kb,
        )
        await state.clear()
        return
    await state.update_data(category_key=key, category_name=category_label(key, "uk"))
    await state.set_state(AddFilterValueState.waiting_for_field)
    buttons = [[KeyboardButton(text=f["label_ru"])] for f in fields]
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    await message.answer(
        f"🔧 Категорія: <b>{category_label(key, 'uk')}</b>\n\nОберіть фільтр:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(AddFilterValueState.waiting_for_field)
async def add_filter_value_field(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    data = await state.get_data()
    fields = await db.list_filter_fields(data["category_key"])
    matched = next((f for f in fields if f["label_ru"] == message.text), None)
    if not matched:
        buttons = [[KeyboardButton(text=f["label_ru"])] for f in fields]
        buttons.append([KeyboardButton(text="⬅️ Назад")])
        await message.answer(
            "⚠️ Оберіть фільтр зі списку або натисніть ⬅️ Назад.",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
        return
    await state.update_data(filter_field_id=matched["id"], field_label=matched["label_ru"])
    await state.set_state(AddFilterValueState.waiting_for_value)
    await message.answer(
        f"🔧 Фільтр: <b>{matched['label_ru']}</b>\n\n"
        "✏️ Введіть значення:\n"
        "<i>наприклад: Atlantic, 80 л, Так, Ні</i>\n\n"
        "❌ Для скасування натисніть ⬅️ Назад.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True,
        ),
    )


@router.message(AddFilterValueState.waiting_for_value)
async def add_filter_value_value(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("⚠️ Введіть значення:")
        return
    data = await state.get_data()
    filter_field_id = data["filter_field_id"]
    field_label = data["field_label"]
    await db.create_filter_value(
        filter_field_id=filter_field_id,
        value=value,
        label_ru=value,
        label_uk=value,
    )
    await state.clear()
    values = await db.list_filter_values(filter_field_id)
    lines = [f"• <b>{v['label_ru']}</b>" for v in values]
    values_text = "\n".join(lines) if lines else "—"
    await message.answer(
        f"✅ Значення «<b>{value}</b>» додано.\n\n"
        f"📋 <b>Значення фільтра «{field_label}»</b> ({len(values)}):\n{values_text}",
        parse_mode="HTML",
        reply_markup=directories_kb,
    )


@router.message(lambda m: m.text == "🧩 Фільтри товару")
async def set_product_filter_start(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    products = await db.list_products()
    if not products:
        await message.answer("📦 Товарів ще немає.", reply_markup=directories_kb)
        return
    by_cat: dict = {}
    for p in products:
        key = category_key(p["category"])
        if key:
            by_cat[key] = by_cat.get(key, 0) + 1
    if not by_cat:
        await message.answer("⚠️ Товари без розпізнаної категорії.", reply_markup=directories_kb)
        return
    cats = categories_for_lang("uk")
    buttons = []
    for cat in cats:
        k = cat["key"]
        if k in by_cat:
            label = f"{cat['emoji']} {cat['name']} ({by_cat[k]})"
            buttons.append([KeyboardButton(text=label)])
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    await state.set_state(SetProductFilterState.waiting_for_category)
    await message.answer(
        "📂 Оберіть категорію:",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(SetProductFilterState.waiting_for_category)
async def set_product_filter_category(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    text = (message.text or "").strip()
    selected_key = None
    cats = categories_for_lang("uk")
    for cat in cats:
        if text.startswith(f"{cat['emoji']} {cat['name']}"):
            selected_key = cat["key"]
            break
    if not selected_key:
        await message.answer("⚠️ Оберіть категорію зі списку або натисніть ⬅️ Назад.")
        return
    all_products = await db.list_products()
    products_cat = [p for p in all_products if category_key(p["category"]) == selected_key]
    if not products_cat:
        await message.answer("📦 У цій категорії немає товарів.", reply_markup=directories_kb)
        await state.clear()
        return
    total_filters = await db.count_filter_fields(selected_key)
    if total_filters == 0:
        await message.answer(
            f"⚠️ Фільтри для категорії «{category_label(selected_key, 'uk')}» ще не налаштовані.",
            reply_markup=directories_kb,
        )
        await state.clear()
        return
    product_ids = [p["id"] for p in products_cat]
    filled_counts = await db.get_filled_filter_counts(product_ids)
    products_no: list = []
    products_partial: list = []
    products_complete: list = []
    for p in products_cat:
        lbl = f"{p['brand']} {p['model']}".strip() or f"ID {p['id']}"
        fc = filled_counts.get(p["id"], 0)
        item = {"id": p["id"], "label": lbl}
        if fc == 0:
            products_no.append(item)
        elif fc >= total_filters:
            products_complete.append(item)
        else:
            products_partial.append(item)
    await state.update_data(
        category_key=selected_key,
        total_filters=total_filters,
        products_no=products_no,
        products_partial=products_partial,
        products_complete=products_complete,
    )
    await state.set_state(SetProductFilterState.waiting_for_status)
    buttons = []
    if products_no:
        buttons.append([KeyboardButton(text=f"⚪ Без фільтрів ({len(products_no)})")])
    if products_partial:
        buttons.append([KeyboardButton(text=f"🟡 Частково ({len(products_partial)})")])
    if products_complete:
        buttons.append([KeyboardButton(text=f"✅ Готові ({len(products_complete)})")])
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    total = len(products_cat)
    await message.answer(
        f"📂 <b>{category_label(selected_key, 'uk')}</b> — {total} товар(ів):\n\n"
        f"⚪ Без фільтрів: {len(products_no)}\n"
        f"🟡 Частково: {len(products_partial)}\n"
        f"✅ Готові: {len(products_complete)}",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(SetProductFilterState.waiting_for_status)
async def set_product_filter_status(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    text = (message.text or "").strip()
    data = await state.get_data()
    if "⚪" in text:
        products_list = data.get("products_no", [])
        status_label = "⚪ Без фільтрів"
    elif "🟡" in text:
        products_list = data.get("products_partial", [])
        status_label = "🟡 Частково"
    elif "✅" in text:
        products_list = data.get("products_complete", [])
        status_label = "✅ Готові"
    else:
        await message.answer("⚠️ Оберіть статус зі списку або натисніть ⬅️ Назад.")
        return
    if not products_list:
        await message.answer("⚠️ Товарів у цій групі немає.", reply_markup=directories_kb)
        await state.clear()
        return
    await state.update_data(products_list=products_list, selected_status=status_label)
    await state.set_state(SetProductFilterState.waiting_for_product)
    buttons = [[KeyboardButton(text=p["label"])] for p in products_list[:30]]
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    shown = min(len(products_list), 30)
    await message.answer(
        f"{status_label} — оберіть товар ({shown} з {len(products_list)}):",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(SetProductFilterState.waiting_for_product)
async def set_product_filter_product(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    data = await state.get_data()
    products_list = data.get("products_list", [])
    matched = next((p for p in products_list if p["label"] == message.text), None)
    if not matched:
        buttons = [[KeyboardButton(text=p["label"])] for p in products_list[:30]]
        buttons.append([KeyboardButton(text="⬅️ Назад")])
        await message.answer(
            "⚠️ Оберіть товар зі списку або натисніть ⬅️ Назад.",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
        return
    key = data["category_key"]
    fields = await db.list_filter_fields(key)
    product_name = matched["label"]
    await state.update_data(
        product_id=matched["id"],
        product_name=product_name,
    )
    await state.set_state(SetProductFilterState.waiting_for_filter)
    buttons = [[KeyboardButton(text=f["label_ru"])] for f in fields]
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    await message.answer(
        f"📦 <b>{product_name}</b>\n📂 {category_label(key, 'uk')}\n\n"
        "Оберіть фільтр для заповнення:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
    )


@router.message(SetProductFilterState.waiting_for_filter)
async def set_product_filter_field(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    data = await state.get_data()
    fields = await db.list_filter_fields(data["category_key"])
    matched = next((f for f in fields if f["label_ru"] == message.text), None)
    if not matched:
        buttons = [[KeyboardButton(text=f["label_ru"])] for f in fields]
        buttons.append([KeyboardButton(text="⬅️ Назад")])
        await message.answer(
            "⚠️ Оберіть фільтр зі списку або натисніть ⬅️ Назад.",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
        return
    values = await db.list_filter_values(matched["id"])
    await state.update_data(
        filter_field_id=matched["id"],
        field_label=matched["label_ru"],
        has_preset_values=bool(values),
    )
    await state.set_state(SetProductFilterState.waiting_for_value)
    if values:
        buttons = [[KeyboardButton(text=v["label_ru"])] for v in values]
        buttons.append([KeyboardButton(text="⬅️ Назад")])
        await message.answer(
            f"🔧 <b>{matched['label_ru']}</b>\n\nОберіть значення:",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
        )
    else:
        await message.answer(
            f"🔧 <b>{matched['label_ru']}</b>\n\n"
            "✏️ Введіть значення вручну:\n\n"
            "❌ Для скасування натисніть ⬅️ Назад.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="⬅️ Назад")]],
                resize_keyboard=True,
            ),
        )


@router.message(SetProductFilterState.waiting_for_value)
async def set_product_filter_value(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("Скасовано.", reply_markup=directories_kb)
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("⚠️ Введіть значення:")
        return
    data = await state.get_data()
    filter_field_id = data["filter_field_id"]
    product_id = data["product_id"]
    field_label = data["field_label"]
    has_preset = data["has_preset_values"]
    filter_value_id = None
    value_text = None
    if has_preset:
        values = await db.list_filter_values(filter_field_id)
        preset = next((v for v in values if v["label_ru"] == value), None)
        if preset:
            filter_value_id = preset["id"]
        else:
            value_text = value
    else:
        value_text = value
    await db.upsert_product_filter_value(
        product_id=product_id,
        filter_field_id=filter_field_id,
        value_text=value_text,
        filter_value_id=filter_value_id,
    )
    await state.clear()
    saved = await db.get_product_filter_values(product_id)
    lines = []
    for s in saved:
        label = s["value_label"] or s["value_text"] or "—"
        lines.append(f"• <b>{s['field_label']}</b>: {label}")
    saved_text = "\n".join(lines) if lines else "—"
    await message.answer(
        f"✅ Фільтр «<b>{field_label}</b>» збережено.\n\n"
        f"📦 <b>{data['product_name']}</b> — поточні фільтри:\n{saved_text}",
        parse_mode="HTML",
        reply_markup=directories_kb,
    )


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


# ── Site pages CMS ──────────────────────────────────────────

PAGE_DEFAULTS = {
    "delivery_text": (
        "🚚 Доставка\n\n"
        "Доставляємо по всій Україні:\n"
        "• Нова Пошта\n"
        "• Укрпошта\n"
        "• Кур'єрська доставка (Київ та область)\n\n"
        "Термін доставки: 1–3 робочих дні.\n"
        "Велика техніка — доставка за домовленістю.\n\n"
        "Самовивіз — за адресою магазину."
    ),
    "warranty_text": (
        "🛡 Гарантія\n\n"
        "На всі товари надається офіційна гарантія виробника.\n\n"
        "• Побутова техніка — від 12 до 36 місяців.\n"
        "• Гарантійний талон видається разом із товаром.\n\n"
        "При виникненні несправності протягом гарантійного терміну — "
        "ремонт або заміна безкоштовно."
    ),
    "returns_text": (
        "↩️ Повернення\n\n"
        "Повернення товару можливе протягом 14 днів з моменту отримання.\n\n"
        "Умови повернення:\n"
        "• Товар у заводській упаковці\n"
        "• Не був у використанні\n"
        "• Наявність чека або накладної\n\n"
        "Для оформлення зверніться до нас за контактами."
    ),
}

PAGE_BUTTONS = {
    "🚚 Доставка": ("delivery_text", "Доставка"),
    "🛡 Гарантия": ("warranty_text", "Гарантия"),
    "↩️ Повернення": ("returns_text", "Повернення"),
}


@router.message(lambda m: m.text == "📄 Страницы сайта")
async def site_pages_menu_handler(message: Message, state: FSMContext):
    if not await require_admin(message):
        return
    await state.clear()
    await message.answer("📄 Страницы сайта:", reply_markup=site_pages_kb)


@router.message(SitePagesState.waiting_for_text)
async def site_page_save_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("page_key")
    if not key:
        await state.clear()
        await message.answer("Ошибка состояния.", reply_markup=site_pages_kb)
        return

    value = (message.text or "").strip()
    if value == "-":
        await db.set_setting(key, "")
        await state.clear()
        await message.answer("✅ Текст сброшен к значению по умолчанию.", reply_markup=site_pages_kb)
        return

    await db.set_setting(key, value)
    await state.clear()
    await message.answer("✅ Текст сохранён.", reply_markup=site_pages_kb)


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
    if not await require_under_products_limit(message):
        return

    await state.set_state(AddProductState.waiting_for_category)
    _lang = await _user_lang(message.from_user.id)
    await message.answer(
        "Выберите категорию:" if _lang == "ru" else "Оберіть категорію:",
        reply_markup=inline_categories_kb(_lang)
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

    await state.update_data(category=category_canonical_ru(category) or category)
    await state.set_state(AddProductState.waiting_for_brand)

    await message.answer(
        "Выберите бренд:",
        reply_markup=await inline_brands_kb()
    )


@router.callback_query(lambda c: c.data and c.data.startswith("add_category:"))
async def add_category_callback(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":", 1)[1]
    # raw может быть и ключом (boilers), и легаси-текстом (Бойлер). Храним канон.
    category = category_canonical_ru(raw) or raw
    lang = await _user_lang(callback.from_user.id)
    display = category_label(category, lang) or category

    await state.update_data(category=category)
    await state.set_state(AddProductState.waiting_for_brand)

    await callback.message.answer(
        (f"Категория: {display}\n\nВыберите бренд:" if lang == "ru"
         else f"Категорія: {display}\n\nОберіть бренд:"),
        reply_markup=await inline_brands_kb()
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


@router.callback_query(lambda c: c.data == "add_brand_new")
async def add_brand_new_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddProductState.waiting_for_brand_manual)
    await callback.message.answer(
        "Введите название нового бренда — он сразу появится в справочнике:"
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "add_brand_search")
async def add_brand_search_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddProductState.searching_brand)
    await callback.message.answer("Введите часть названия бренда:")
    await callback.answer()


@router.callback_query(lambda c: c.data == "add_brand_show_hidden")
async def add_brand_show_hidden_callback(callback: CallbackQuery, state: FSMContext):
    """Показать скрытые бренды в flow добавления товара."""
    try:
        rows = await db.list_site_brands()
    except Exception as e:
        print(f"[brands] load hidden failed: {e}")
        rows = []
    hidden = [r for r in rows if not r["is_active"]]

    keyboard: list[list[InlineKeyboardButton]] = []
    for r in hidden:
        keyboard.append([
            InlineKeyboardButton(
                text=r["name"],
                callback_data=f"add_brand_hidden:{r['id']}",
            ),
        ])
    keyboard.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="add_brand_back_to_active"),
    ])
    keyboard.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow"),
    ])

    text = (
        "👁 Неактивные бренды:" if hidden
        else "👁 Неактивные бренды:\n\nСкрытых брендов нет."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(lambda c: c.data == "add_brand_back_to_active")
async def add_brand_back_to_active_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    category = data.get("category") or ""
    text = f"Категория: {category}\n\nВыберите бренд:" if category else "Выберите бренд:"
    try:
        await callback.message.edit_text(text, reply_markup=await inline_brands_kb())
    except Exception:
        await callback.message.answer(text, reply_markup=await inline_brands_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("add_brand_hidden:"))
async def add_brand_hidden_callback(callback: CallbackQuery, state: FSMContext):
    """Клик на скрытый бренд — спросить подтверждение активации."""
    try:
        brand_id = int(callback.data.split(":", 1)[1])
        row = await db.fetchrow(
            "SELECT id, name, is_active FROM site_brands WHERE id = $1",
            brand_id,
        )
    except Exception as e:
        print(f"[brands] hidden pick failed: {e}")
        await callback.answer("Ошибка", show_alert=False)
        return
    if row is None:
        await callback.answer("Бренд не найден", show_alert=True)
        return
    if row["is_active"]:
        # Уже активен — просто выбираем как обычный бренд.
        await state.update_data(brand=row["name"])
        await state.set_state(AddProductState.waiting_for_model)
        try:
            await callback.message.edit_text(
                f"Бренд: {row['name']}\n\nВведите модель:"
            )
        except Exception:
            await callback.message.answer(
                f"Бренд: {row['name']}\n\nВведите модель:"
            )
        await callback.answer()
        return

    # Сохраняем в state, чтобы brand_activate_callback подхватил имя.
    await state.update_data(
        pending_hidden_brand_id=row["id"],
        pending_hidden_brand_name=row["name"],
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Активировать и выбрать",
            callback_data=f"brand_activate:{row['id']}",
        )],
        [InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="brand_activate_cancel",
        )],
    ])
    text = f"Бренд «{row['name']}» скрыт. Активировать и выбрать его?"
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("brand_activate:"))
async def brand_activate_callback(callback: CallbackQuery, state: FSMContext):
    try:
        brand_id = int(callback.data.split(":", 1)[1])
        await db.activate_site_brand(brand_id)
    except Exception as e:
        print(f"[brands] activate failed: {e}")
        await callback.answer("Ошибка", show_alert=False)
        return

    data = await state.get_data()
    brand_name = data.get("pending_hidden_brand_name") or ""
    if not brand_name:
        try:
            row = await db.get_site_brand_by_name(str(brand_id))
        except Exception:
            row = None
        brand_name = (row or {}).get("name") or ""

    await state.update_data(brand=brand_name, pending_hidden_brand_id=None, pending_hidden_brand_name=None)
    await state.set_state(AddProductState.waiting_for_model)

    try:
        await callback.message.edit_text(
            f"✅ Бренд «{brand_name}» активирован.\n\nВведите модель:"
        )
    except Exception:
        await callback.message.answer(
            f"✅ Бренд «{brand_name}» активирован.\n\nВведите модель:"
        )
    await callback.answer("Активировано")


@router.callback_query(lambda c: c.data == "brand_activate_cancel")
async def brand_activate_cancel_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    category = data.get("category") or ""
    await state.update_data(pending_hidden_brand_id=None, pending_hidden_brand_name=None)
    await state.set_state(AddProductState.waiting_for_brand)
    try:
        await callback.message.edit_text(
            f"Категория: {category}\n\nВыберите бренд:" if category else "Выберите бренд:",
            reply_markup=await inline_brands_kb(),
        )
    except Exception:
        await callback.message.answer(
            f"Категория: {category}\n\nВыберите бренд:" if category else "Выберите бренд:",
            reply_markup=await inline_brands_kb(),
        )
    await callback.answer("Отменено")


@router.message(AddProductState.searching_category)
async def search_category_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()

    lang = await _user_lang(message.from_user.id)
    items = categories_for_lang(lang)
    found = [c for c in items if query in c["name"].lower() or query in c["name_ru"].lower() or query in c["name_uk"].lower()]

    if not found:
        await message.answer(await t(message, "no_products_found"))
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"add_category:{c['key']}")]
            for c in found
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
    )

    await state.set_state(AddProductState.waiting_for_category)
    await message.answer("Выберите категорию:" if lang == "ru" else "Оберіть категорію:", reply_markup=keyboard)


@router.message(AddProductState.waiting_for_brand)
async def add_product_brand_handler(message: Message, state: FSMContext):
    brand = (message.text or "").strip()

    if brand == "🔍 Поиск бренда":
        await state.set_state(AddProductState.searching_brand)
        await message.answer("Введите часть названия бренда:")
        return

    if brand == "⬅️ Назад":
        await state.set_state(AddProductState.waiting_for_category)
        _lang = await _user_lang(message.from_user.id)
        await message.answer(
            "Выберите категорию:" if _lang == "ru" else "Оберіть категорію:",
            reply_markup=inline_categories_kb(_lang),
        )
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

    # Сохраняем в справочник (без дублей, case-insensitive)
    saved = None
    try:
        saved = await db.add_site_brand(brand)
    except Exception as e:
        print(f"[brands] save '{brand}' failed: {e}")

    if not saved:
        # Не удалось ни найти, ни создать — продолжаем без БД, чтобы не блокировать.
        await state.update_data(brand=brand)
        await state.set_state(AddProductState.waiting_for_model)
        await message.answer(f"➕ Бренд «{brand}» принят.\n\nВведите модель:")
        return

    status = saved.get("_status")
    brand = saved["name"]  # каноническое имя из БД

    if status == "hidden":
        # Бренд уже существует, но скрыт — спрашиваем подтверждение активации.
        await state.update_data(pending_hidden_brand_id=saved["id"], pending_hidden_brand_name=brand)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Активировать",
                    callback_data=f"brand_activate:{saved['id']}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="brand_activate_cancel",
                ),
            ]
        ])
        await message.answer(
            f"Бренд «{brand}» уже есть, но скрыт. Активировать?",
            reply_markup=kb,
        )
        return

    await state.update_data(brand=brand)
    await state.set_state(AddProductState.waiting_for_model)

    if status == "active":
        note = f"✅ Бренд «{brand}» уже есть в справочнике.\n\n"
    else:
        note = f"➕ Бренд «{brand}» добавлен.\n\n"
    await message.answer(note + "Введите модель:")


@router.message(AddProductState.searching_brand)
async def search_brand_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()

    try:
        brands = await db.list_brands_for_selection()
    except Exception as e:
        print(f"[brands] search failed: {e}")
        brands = []

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
    if not data.get("category") or not data.get("brand") or not data.get("model"):
        await state.clear()
        menu = await get_main_menu_for_user(message)
        await message.answer(
            "⚠️ Сессия устарела. Начните действие заново.",
            reply_markup=menu,
        )
        return
    category = data.get("category")
    brand = data.get("brand")
    model = data.get("model")

    await state.update_data(price=price)

    # Закупочная цена и валюта при добавлении не запрашиваются —
    # ставятся дефолты (0 / UAH). Их можно поправить позже в редактировании.
    await state.update_data(purchase_price=0, currency="UAH")

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

    if not data.get("category") or not data.get("brand") or not data.get("model") or data.get("price") is None:
        await state.clear()
        menu = await get_main_menu_for_user(message)
        await message.answer(
            "⚠️ Сессия устарела. Начните действие заново.",
            reply_markup=menu,
        )
        return

    # 1) Сохраняем товар сразу с пустыми specs — получаем id для меню.
    try:
        product_id = await db.add_product(
            category=data.get("category"),
            brand=data.get("brand"),
            model=data.get("model"),
            price=data.get("price"),
            purchase_price=data.get("purchase_price", 0),
            purchase_currency=data.get("currency", "UAH"),
            sku=data.get("sku"),
            warranty_months=warranty,
            specifications=None,
        )
    except Exception as e:
        print(f"[add_product] save failed: {e}")
        await state.clear()
        await message.answer(
            f"❌ Не удалось сохранить товар: {e}",
            reply_markup=products_kb,
        )
        return

    # 2) Переходим в меню характеристик (то же, что и при редактировании,
    #    но с финальными кнопками «Завершить» / «Пропустить»).
    await state.set_state(AddProductState.editing_specs)
    await state.update_data(warranty=warranty, add_product_id=product_id)

    current = await db.get_product_specifications(product_id)
    cat = category_key(data.get("category") or "")
    await message.answer(
        "📋 Характеристики товара\nВыберите поле для заполнения "
        "или нажмите «Завершить добавление».",
        reply_markup=inline_specs_kb(product_id, current, mode="add", cat_key=cat),
    )


async def _finalize_add_product_summary(target_message: Message, state: FSMContext):
    """Показывает итог добавления товара и очищает state."""
    data = await state.get_data()
    product_id = data.get("add_product_id")
    if not product_id:
        await state.clear()
        await target_message.answer("Готово.", reply_markup=products_kb)
        return
    try:
        specs = await db.get_product_specifications(int(product_id))
    except Exception:
        specs = {}

    specs_summary = ""
    if specs:
        cat_for_labels = category_key(data.get("category") or "")
        labels_map = spec_labels_for(cat_for_labels)
        lines = []
        for k, v in specs.items():
            label = labels_map.get(k, k)
            lines.append(f"• {label}: {_label_for_spec_value(k, v)}")
        specs_summary = "\n\n📋 Характеристики:\n" + "\n".join(lines)

    warranty = int(data.get("warranty") or 0)
    await state.clear()
    await target_message.answer(
        f"✅ Товар добавлен (#{product_id})\n\n"
        f"{data.get('brand', '')} {data.get('model', '')}\n"
        f"{await t(target_message, 'price')}: {data.get('price', 0)} грн\n"
        f"Закупка: {data.get('purchase_price', 0)} {data.get('currency', 'UAH')}\n"
        f"{await t(target_message, 'warranty')}: {warranty} мес"
        f"{specs_summary}",
        reply_markup=products_kb,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("addspec_done:"))
async def add_product_specs_done_callback(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Готово")
    await _finalize_add_product_summary(callback.message, state)


PRODUCTS_PAGE_SIZE = 8


def _sanitize_plain(s: str) -> str:
    """Удаляет управляющие символы и обрезает крайние пробелы.
    Сообщения отправляются без parse_mode, поэтому экранировать HTML/MD не нужно,
    но убираем символы, которые могут ломать рендер."""
    if s is None:
        return ""
    s = str(s)
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return s.strip()


def _product_status_emoji(row) -> str:
    """🟢 / 🔴 / 👁️ — статус товара для кнопки."""
    try:
        avail = (row["availability_status"] or "").lower()
    except (KeyError, TypeError):
        avail = ""
    try:
        qty = int(row["stock_qty"] or 0)
    except (TypeError, ValueError, KeyError):
        qty = 0
    if avail == "hidden":
        return "👁️"
    if avail == "out_of_stock" or qty <= 0:
        return "🔴"
    return "🟢"


def _format_product_line(row) -> str:
    """Текст одной кнопки: '🟢 #21 | Brand Model | 8500 грн'."""
    try:
        pid = row["id"]
        brand = _sanitize_plain(row["brand"]) or "-"
        model = _sanitize_plain(row["model"]) or "-"
        try:
            price = float(row["price"] or 0)
        except (TypeError, ValueError):
            price = 0.0
        title = f"{brand} {model}".strip() or "-"
        status = _product_status_emoji(row)
        line = f"{status} #{pid} | {title} | {price:.0f} грн"
        # Telegram-кнопка ограничена ~64 символами в тексте
        if len(line) > 60:
            line = line[:57] + "…"
        return line
    except Exception as e:
        print(f"[list_products] row failed: {e}")
        try:
            return f"#{row['id']} | (битые данные)"
        except Exception:
            return "(пропущено)"


def _products_page_kb(rows, page: int, total_pages: int) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows:
        try:
            pid = row["id"]
        except Exception:
            continue
        keyboard.append([
            InlineKeyboardButton(
                text=_format_product_line(row),
                callback_data=f"edit_product:{pid}",
            )
        ])
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"products_page:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="products_page:noop"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"products_page:{page + 1}"))
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔍 Найти товар", callback_data="products_search")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _send_products_page(message: Message, page: int, edit: bool = False):
    try:
        rows = await db.list_products()
    except Exception as e:
        print(f"[list_products] db failed: {e}")
        await message.answer("⚠️ Не удалось загрузить список товаров.")
        return

    if not rows:
        await message.answer("Товары не найдены")
        return

    total = len(rows)
    total_pages = max(1, (total + PRODUCTS_PAGE_SIZE - 1) // PRODUCTS_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PRODUCTS_PAGE_SIZE
    page_rows = rows[start:start + PRODUCTS_PAGE_SIZE]

    text = (
        f"📦 Список товаров (стр. {page}/{total_pages}, всего {total})\n"
        "🟢 В наличии  🔴 Нет в наличии  👁️ Скрыт"
    )
    kb = _products_page_kb(page_rows, page, total_pages)
    try:
        if edit:
            try:
                await message.edit_text(text, reply_markup=kb)
                return
            except Exception:
                pass
        await message.answer(text, reply_markup=kb)
    except Exception as e:
        print(f"[list_products] send failed: {e}")


@router.message(lambda m: m.text == "📋 Список товаров")
async def list_products_handler(message: Message):
    await _send_products_page(message, page=1, edit=False)


@router.callback_query(lambda c: c.data and c.data.startswith("products_page:"))
async def products_page_callback(callback: CallbackQuery):
    raw = callback.data.split(":", 1)[1]
    if raw == "noop":
        await callback.answer()
        return
    try:
        page = int(raw)
    except ValueError:
        await callback.answer()
        return
    await _send_products_page(callback.message, page=page, edit=True)
    await callback.answer()


@router.callback_query(lambda c: c.data == "products_search")
async def products_search_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(EditProductState.waiting_for_query)
    try:
        await callback.message.answer(await t(callback.message, "enter_search"))
    except Exception:
        await callback.message.answer("Введите часть названия, модель или артикул:")
    await callback.answer()


@router.message(lambda m: m.text == "🧹 Очистить битые товары")
async def cleanup_broken_products_handler(message: Message):
    if not await require_admin(message):
        return
    count = await db.soft_delete_broken_products()
    await message.answer(f"✅ Битые товары скрыты: {count} шт.", reply_markup=products_kb)

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

    rows = await db.search_products(query, limit=11)

    if not rows:
        await message.answer(
            "Товар не найден. Попробуйте ввести часть названия, модель или артикул."
        )
        return

    has_more = len(rows) > 10
    shown = rows[:10]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"#{row['id']} | {row['brand'] or '-'} {row['model'] or '-'} | {float(row['price'] or 0):.0f} грн",
                    callback_data=f"edit_product:{row['id']}"
                )
            ]
            for row in shown
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
    )

    await state.set_state(EditProductState.waiting_for_product_id)
    prompt = await t(message, "choose_product")
    if has_more:
        prompt = f"{prompt}\n\n⚠️ Найдено больше 10, уточните запрос."
    await message.answer(prompt, reply_markup=keyboard)



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
        lang = await _user_lang(callback.from_user.id)
        items = categories_for_lang(lang)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"{c['emoji']} {c['name']}",
                    callback_data=f"set_category:{c['key']}",
                )]
                for c in items
            ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow")]]
        )
        await state.set_state(EditProductState.waiting_for_category)
        await callback.message.answer("Выберите новую категорию:" if lang == "ru" else "Оберіть нову категорію:", reply_markup=keyboard)
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

    if action == "specs_open":
        current = await db.get_product_specifications(product_id)
        cat = await _get_product_cat_key(product_id)
        await callback.message.answer(
            "📋 Характеристики товара. Выберите поле для редактирования:",
            reply_markup=inline_specs_kb(product_id, current, cat_key=cat),
        )
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


@router.callback_query(lambda c: c.data and c.data.startswith("specs_open:"))
async def specs_open_callback(callback: CallbackQuery, state: FSMContext):
    try:
        product_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка")
        return
    current = await db.get_product_specifications(product_id)
    cat = await _get_product_cat_key(product_id)
    cur_state = await state.get_state()
    mode = "add" if cur_state == AddProductState.editing_specs.state else "edit"
    try:
        await callback.message.edit_text(
            "📋 Характеристики товара. Выберите поле для редактирования:",
            reply_markup=inline_specs_kb(product_id, current, mode=mode, cat_key=cat),
        )
    except Exception:
        await callback.message.answer(
            "📋 Характеристики товара. Выберите поле для редактирования:",
            reply_markup=inline_specs_kb(product_id, current, mode=mode, cat_key=cat),
        )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("specs_field:"))
async def specs_field_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("Ошибка")
        return
    try:
        product_id = int(parts[1])
    except ValueError:
        await callback.answer("Ошибка")
        return
    key = parts[2]
    cat = await _get_product_cat_key(product_id)
    labels = spec_labels_for(cat)
    options_map = spec_options_for(cat)
    label = labels.get(key)
    if not label:
        await callback.answer("Неизвестное поле")
        return

    # Запоминаем, идём ли мы из add-flow, чтобы вернуться в add-меню после ввода.
    cur_state = await state.get_state()
    add_mode = (cur_state == AddProductState.editing_specs.state)

    if key in options_map:
        await callback.message.answer(
            f"Выберите значение для поля «{label}»:",
            reply_markup=inline_specs_options_kb(product_id, key, cat_key=cat),
        )
        await callback.answer()
        return

    # Free-text field
    if add_mode:
        # Сохраняем все add-данные, добавляем флаг и ключи.
        await state.update_data(
            specs_product_id=product_id,
            specs_key=key,
            specs_label=label,
            specs_add_mode=True,
        )
    else:
        await state.update_data(
            specs_product_id=product_id,
            specs_key=key,
            specs_label=label,
        )
    await state.set_state(EditSpecsState.waiting_for_value)
    await callback.message.answer(
        f"Введите значение для поля «{label}». Отправьте «-» чтобы очистить."
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("specs_opt:"))
async def specs_opt_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":", 3)
    if len(parts) < 4:
        await callback.answer("Ошибка")
        return
    try:
        product_id = int(parts[1])
        idx = int(parts[3])
    except ValueError:
        await callback.answer("Ошибка")
        return
    key = parts[2]
    cat = await _get_product_cat_key(product_id)
    options = spec_options_for(cat).get(key) or []
    if idx < 0 or idx >= len(options):
        await callback.answer("Опция не найдена")
        return
    value = options[idx]
    stored = _normalize_spec_value(key, value)
    await db.set_product_specification(product_id, key, stored)
    current = await db.get_product_specifications(product_id)
    cur_state = await state.get_state()
    mode = "add" if cur_state == AddProductState.editing_specs.state else "edit"
    try:
        await callback.message.edit_text(
            "📋 Характеристики товара. Выберите поле для редактирования:",
            reply_markup=inline_specs_kb(product_id, current, mode=mode, cat_key=cat),
        )
    except Exception:
        await callback.message.answer(
            "📋 Характеристики товара. Выберите поле для редактирования:",
            reply_markup=inline_specs_kb(product_id, current, mode=mode, cat_key=cat),
        )
    await callback.answer(f"✅ {spec_labels_for(cat).get(key, key)}: {value}")


@router.callback_query(lambda c: c.data and c.data.startswith("specs_clear:"))
async def specs_clear_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("Ошибка")
        return
    try:
        product_id = int(parts[1])
    except ValueError:
        await callback.answer("Ошибка")
        return
    key = parts[2]
    await db.clear_product_specification(product_id, key)
    current = await db.get_product_specifications(product_id)
    cat = await _get_product_cat_key(product_id)
    cur_state = await state.get_state()
    mode = "add" if cur_state == AddProductState.editing_specs.state else "edit"
    try:
        await callback.message.edit_text(
            "📋 Характеристики товара. Выберите поле для редактирования:",
            reply_markup=inline_specs_kb(product_id, current, mode=mode, cat_key=cat),
        )
    except Exception:
        await callback.message.answer(
            "📋 Характеристики товара. Выберите поле для редактирования:",
            reply_markup=inline_specs_kb(product_id, current, mode=mode, cat_key=cat),
        )
    await callback.answer("🗑 Очищено")


@router.callback_query(lambda c: c.data and c.data.startswith("specs_desc:"))
async def specs_desc_callback(callback: CallbackQuery, state: FSMContext):
    try:
        product_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка")
        return
    await state.update_data(product_id=product_id, field="description", field_title="Описание")
    await state.set_state(EditProductState.waiting_for_value)
    await callback.message.answer("Введите новое значение для поля: Описание")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("specs_back:"))
async def specs_back_callback(callback: CallbackQuery, state: FSMContext):
    try:
        product_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка")
        return
    product = await db.get_product_by_id(product_id)
    if not product:
        await callback.answer("Товар не найден")
        return
    await state.update_data(product_id=product_id)
    await state.set_state(EditProductState.waiting_for_field)
    try:
        await callback.message.edit_text(
            f"Товар:\n{product['brand'] or '-'} {product['model'] or '-'}\n\nЧто изменить?",
            reply_markup=inline_edit_fields_kb(product),
        )
    except Exception:
        await callback.message.answer(
            f"Товар:\n{product['brand'] or '-'} {product['model'] or '-'}\n\nЧто изменить?",
            reply_markup=inline_edit_fields_kb(product),
        )
    await callback.answer()


@router.message(EditSpecsState.waiting_for_value)
async def edit_specs_value_handler(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    data = await state.get_data()
    product_id = data.get("specs_product_id")
    key = data.get("specs_key")
    label = data.get("specs_label", "")
    add_mode = bool(data.get("specs_add_mode"))

    if not product_id or not key:
        await state.clear()
        await message.answer("Сессия редактирования потеряна. Откройте характеристики заново.")
        return

    if value == "-" or value == "":
        await db.clear_product_specification(int(product_id), key)
        await message.answer(f"🗑 Поле «{label}» очищено.")
    else:
        stored = _normalize_spec_value(key, value)
        await db.set_product_specification(int(product_id), key, stored)
        await message.answer(f"✅ {label}: {_label_for_spec_value(key, stored)}")

    current = await db.get_product_specifications(int(product_id))
    cat = await _get_product_cat_key(product_id)

    if add_mode:
        # Возвращаемся в меню добавления: чистим только spec-ключи, не теряем
        # данные add-flow (category, brand, model, price, warranty, add_product_id).
        await state.update_data(
            specs_product_id=None,
            specs_key=None,
            specs_label=None,
            specs_add_mode=False,
        )
        await state.set_state(AddProductState.editing_specs)
        await message.answer(
            "📋 Характеристики товара\nВыберите поле для заполнения "
            "или нажмите «Завершить добавление».",
            reply_markup=inline_specs_kb(int(product_id), current, mode="add", cat_key=cat),
        )
    else:
        await state.clear()
        await message.answer(
            "📋 Характеристики товара. Выберите поле для редактирования:",
            reply_markup=inline_specs_kb(int(product_id), current, cat_key=cat),
        )


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
    raw = callback.data.split(":", 1)[1]
    category = category_canonical_ru(raw) or raw
    data = await state.get_data()
    product_id = data.get("product_id")

    if not product_id:
        await callback.answer("Нет выбранного товара.")
        return

    await db.update_product_category(product_id, category)
    lang = await _user_lang(callback.from_user.id)
    display = category_label(category, lang) or category
    await state.clear()
    await callback.message.answer(
        (f"✅ Категория обновлена: {display}" if lang == "ru"
         else f"✅ Категорію оновлено: {display}"),
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

        # PRE-CHECK: enforce per-product photo limit (from saas_platform tariff, fallback 6)
        photo_limit = await get_image_limit_for_product()
        if await db.count_product_images_total(product_id) >= photo_limit:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="💳 Оновити тариф", callback_data="pay_subscription_inline")
                ]]
            )
            await message.answer(
                f"⚠️ Максимум {photo_limit} фото для одного товара. Удалите одно фото, чтобы добавить новое.",
                reply_markup=kb,
            )
            return

        product = await db.get_product_by_id(product_id)
        legacy = product.get("photo_url") if product else None

        file_id = message.photo[-1].file_id
        photo_url = await save_telegram_photo(message.bot, file_id)

        # ATOMIC add with limit check (handles race when sending media group)
        inserted_id = await db.add_product_image_if_under_limit(product_id, photo_url, limit=photo_limit)
        if inserted_id is None:
            await message.answer(
                f"⚠️ Максимум {photo_limit} фото для одного товара. Удалите одно фото, чтобы добавить новое."
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

    elif field == "model_group":
        if value == "-" or value == "":
            value = None
        else:
            # Нормализуем: lowercase, пробелы/пунктуацию → дефис, обрезаем до 64.
            v = re.sub(r"[^\w]+", "-", value.strip().lower(), flags=re.UNICODE)
            v = re.sub(r"-+", "-", v).strip("-")
            if not v:
                await message.answer("Код пустой после нормализации. Введите буквы/цифры.")
                return
            value = v[:64]

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
    "➕ Добавить товар", "📋 Список товаров", "✏️ Изменить остаток", "➕ Приход", "🧹 Очистить битые товары",
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
    "📄 Страницы сайта", "🚚 Доставка", "🛡 Гарантия", "↩️ Повернення", "✏️ Изменить текст",
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


def _extract_number(raw):
    """Достаём первое число из произвольной строки/значения."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    s = str(raw).replace(",", ".").strip()
    if not s:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _product_specs(p):
    """Безопасно достать specifications_json как dict (или {})."""
    try:
        raw = p["specifications_json"]
    except (KeyError, TypeError):
        return {}
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _product_attr_value(p, key):
    """Универсальный getter значения атрибута товара.

    Приоритет: specifications_json[key] → legacy-колонки (для volume/heater_type).
    Возвращает строку (нормализованную) или None.
    """
    specs = _product_specs(p)
    val = specs.get(key) if isinstance(specs, dict) else None
    # Back-compat: до миграции часть товаров хранит тип ТЭНа под старым
    # ключом "ten_type" в specifications_json.
    if (val is None or val == "") and key == "heater_type" and isinstance(specs, dict):
        val = specs.get("ten_type")
    if val is None or val == "":
        # legacy fallback
        if key == "volume":
            try:
                v = p["boiler_volume_liters"]
            except (KeyError, TypeError):
                v = None
            if v:
                return str(int(float(v)))
            return None
        if key == "heater_type":
            try:
                v = p["boiler_ten_type"]
            except (KeyError, TypeError):
                v = None
            if not v:
                return None
            # legacy-колонка может содержать UA/RU label — приводим к canonical
            return _normalize_spec_value("heater_type", v)
        return None
    return str(val).strip()


@web_app.get("/", response_class=HTMLResponse)
async def site_home(request: Request, q: str = "", category: str = "", page: int = 1, brand: str = "", price_min: str = "", price_max: str = "", in_stock: str = "", volume: str = "", sort: str = ""):
    q = (q or "").strip()
    category = (category or "").strip()
    brand = (brand or "").strip()
    price_min = (price_min or "").strip()
    price_max = (price_max or "").strip()
    volume = (volume or "").strip()
    sort = (sort or "").strip().lower()
    if sort not in ("", "price_asc", "price_desc", "new", "newest", "popular"):
        sort = ""

    if q:
        products = await db.search_site_products(q)
    else:
        products = await db.list_site_products()

    # Список брендов для фильтра берём из того же источника, что и бот:
    # union (site_brands.is_active=TRUE) + (бренды, реально используемые в
    # активных товарах) — даже если они помечены is_active=FALSE.
    # Параллельно подтягиваем недостающие бренды в справочник и
    # авто-реактивируем скрытые-но-используемые (idempotent).
    try:
        await db.sync_site_brands_from_products()
    except Exception as e:
        print(f"[site] brands sync failed: {e}")
    try:
        brands = await db.list_brands_for_selection()
    except Exception as e:
        print(f"[site] list_brands_for_selection failed: {e}")
        brands = sorted({(p["brand"] or "").strip() for p in products if p["brand"]})

    if category:
        # Сравниваем по стабильному ключу — покрывает все алиасы (RU/UA/legacy).
        target_key = category_key(category)
        if target_key:
            def _row_key(p):
                # Если у товара уже есть колонка category_key — используем её,
                # иначе вычисляем из текста для совместимости со старыми.
                return p.get("category_key") or category_key(p.get("category"))
            products = [p for p in products if _row_key(p) == target_key]
        else:
            # Неизвестная категория — fallback на текстовое сравнение.
            products = [p for p in products if (p["category"] or "").strip().lower() == category.strip().lower()]

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
        # На сайте не используем количественный остаток (stock_qty) — он не
        # отражает реальную доступность. Опираемся на stock_status, который
        # ведётся вручную в боте: in_stock / preorder / out_of_stock.
        # Если у старого товара stock_status пустой — считаем как in_stock.
        def _is_in_stock(p):
            try:
                status = p["stock_status"]
            except (KeyError, TypeError):
                status = None
            status = (status or "").strip().lower()
            return status in ("", "in_stock")
        products = [p for p in products if _is_in_stock(p)]

    # ── Dynamic category filters (etap 6) ──
    # Атрибуты — из таблицы category_attributes (is_filter=TRUE).
    # Значения — из products.specifications_json + legacy-колонок (через _product_attr_value).
    # Виды рендера:
    #   render_kind = "checkbox" — select-атрибуты И «дискретные» number-атрибуты
    #                              (см. DISCRETE_NUMBER_KEYS). Multi-select через
    #                              ?<key>=<value>.
    #   render_kind = "range"    — number-атрибуты с реальным диапазоном
    #                              (?<key>_min=, ?<key>_max=).
    #
    # volume — всегда чекбоксы (см. ТЗ). Остальные number — пока range.
    DISCRETE_NUMBER_KEYS = {"volume", "height", "load_capacity", "spin_speed", "depth", "width", "productivity", "noise_level"}
    # Категорийные переопределения: для каких категорий доп. number-атрибуты тоже чекбоксы.
    DISCRETE_NUMBER_KEYS_BY_CATEGORY = {
        "microwaves": {"power"},
        "air_conditioners": {"room_area"},
    }
    dyn_attrs = []
    dyn_options = {}   # attr_key → list[{value, label_ru, label_uk}] (checkbox-режим)
    dyn_selected = {}  # attr_key → list[str] (checkbox-режим)
    dyn_range = {}     # attr_key → {min, max, current_min, current_max, unit} (range-режим)
    dyn_query_extras = []
    target_key_dyn = category_key(category) if category else ""
    # ── filter_fields/filter_values (select & boolean) ──────────────────────
    _ff_used = False
    if target_key_dyn:
        try:
            _ff_fields = await db.get_filter_fields_with_values(target_key_dyn)
            _ff_sel = [f for f in _ff_fields if f["field_type"] in ("select", "boolean")]
        except Exception as _e:
            print(f"[site] get_filter_fields_with_values failed: {_e}")
            _ff_sel = []
        if _ff_sel:
            _ff_used = True
            qp = request.query_params
            try:
                _pfv_rows = await db.get_product_filter_values_for_category(target_key_dyn)
            except Exception as _e:
                print(f"[site] get_product_filter_values_for_category failed: {_e}")
                _pfv_rows = []
            # index: product_id → {field_id: (filter_value_id, value_text)}
            _pfv_idx: dict = {}
            for _r in _pfv_rows:
                _pid2 = _r["product_id"]
                if _pid2 not in _pfv_idx:
                    _pfv_idx[_pid2] = {}
                _pfv_idx[_pid2][_r["filter_field_id"]] = (
                    _r["filter_value_id"], _r["value_text"]
                )
            for _ff in _ff_sel:
                _fkey = _ff["field_key"]
                _ftype = _ff["field_type"]
                _fid = _ff["field_id"]
                _fvals = _ff["values"]
                if _ftype == "boolean" and not _fvals:
                    # boolean without preset values — match by value_text
                    _present: set = set()
                    for _pvals in _pfv_idx.values():
                        if _fid in _pvals:
                            _, _vtxt = _pvals[_fid]
                            if _vtxt:
                                _present.add(_vtxt.strip().lower())
                    _opts_render = [
                        o for o in [
                            {"value": "yes", "label_ru": "Так", "label_uk": "Так"},
                            {"value": "no",  "label_ru": "Ні",  "label_uk": "Ні"},
                        ] if o["value"] in _present
                    ]
                    if not _opts_render:
                        continue
                    _raw = qp.getlist(_fkey) if hasattr(qp, "getlist") else []
                    _sel = [v.strip() for v in _raw if v.strip()]
                    if _sel:
                        dyn_selected[_fkey] = _sel
                        _wanted_txt = {s.lower() for s in _sel}
                        def _match_bool(p, _fid=_fid, _w=_wanted_txt, _idx=_pfv_idx):
                            _entry = _idx.get(p["id"], {}).get(_fid)
                            return _entry is not None and (_entry[1] or "").strip().lower() in _w
                        products = [p for p in products if _match_bool(p)]
                        for _s in _sel:
                            dyn_query_extras.append((_fkey, _s))
                else:
                    # select (or boolean with preset filter_values)
                    _vkey_to_id = {v["value_key"]: v["value_id"] for v in _fvals}
                    _assigned_vids: set = set()
                    for _pvals in _pfv_idx.values():
                        if _fid in _pvals:
                            _vid, _ = _pvals[_fid]
                            if _vid is not None:
                                _assigned_vids.add(_vid)
                    _opts_render = [
                        {
                            "value": v["value_key"],
                            "label_ru": v["label_ru"] or v["value_key"],
                            "label_uk": v["label_uk"] or v["label_ru"] or v["value_key"],
                        }
                        for v in _fvals
                        if v["value_id"] in _assigned_vids
                    ]
                    if not _opts_render:
                        continue
                    _raw = qp.getlist(_fkey) if hasattr(qp, "getlist") else []
                    _sel = [v.strip() for v in _raw if v.strip()]
                    if _sel:
                        dyn_selected[_fkey] = _sel
                        _wanted_vids = {_vkey_to_id[s] for s in _sel if s in _vkey_to_id}
                        if _wanted_vids:
                            def _match_sel(p, _fid=_fid, _w=_wanted_vids, _idx=_pfv_idx):
                                _entry = _idx.get(p["id"], {}).get(_fid)
                                return _entry is not None and _entry[0] in _w
                            products = [p for p in products if _match_sel(p)]
                        for _s in _sel:
                            dyn_query_extras.append((_fkey, _s))
                dyn_attrs.append({
                    "attribute_key": _fkey,
                    "name_ru": _ff["label_ru"],
                    "name_ua": _ff["label_uk"] or _ff["label_ru"],
                    "type": _ftype,
                    "unit": _ff["unit"] or "",
                    "render_kind": "checkbox",
                })
                dyn_options[_fkey] = _opts_render
    # ── OLD fallback (category_attributes) ──────────────────────────────────
    if not _ff_used and target_key_dyn in ("boilers", "air_conditioners", "refrigerators", "washing_machines", "hoods", "microwaves", "gas_stoves"):
        try:
            dyn_attrs = await db.get_category_attributes(target_key_dyn, only_filterable=True)
        except Exception as e:
            print(f"[site] get_category_attributes failed: {e}")
            dyn_attrs = []

    # Расширяем discrete-набор переопределениями для текущей категории.
    discrete_keys_effective = DISCRETE_NUMBER_KEYS | DISCRETE_NUMBER_KEYS_BY_CATEGORY.get(target_key_dyn, set())

    products_for_options = list(products) if (dyn_attrs and not _ff_used) else []

    if dyn_attrs and not _ff_used:
        qp = request.query_params
        for attr in dyn_attrs:
            key = attr["attribute_key"]
            atype = (attr.get("type") or "").lower()
            unit = (attr.get("unit") or "").strip()

            # Определяем тип рендера и проставляем в attr для шаблона.
            if atype == "select":
                render_kind = "checkbox"
            elif atype == "number" and key in discrete_keys_effective:
                render_kind = "checkbox"
            elif atype == "number":
                render_kind = "range"
            else:
                # text / unknown — UI не рендерим.
                attr["render_kind"] = "none"
                continue
            attr["render_kind"] = render_kind

            if render_kind == "checkbox":
                raw_values = qp.getlist(key) if hasattr(qp, "getlist") else []
                selected = [v.strip() for v in raw_values if v and v.strip()]

                if atype == "number":
                    # Сравнение по числовому значению (50 ≡ "50" ≡ "50 л").
                    wanted_nums = set()
                    for s in selected:
                        n = _extract_number(s)
                        if n is not None:
                            wanted_nums.add(n)
                    if wanted_nums:
                        dyn_selected[key] = selected

                        def _match_num_eq(p, k=key, nums=wanted_nums):
                            val = _product_attr_value(p, k)
                            n = _extract_number(val)
                            return n is not None and n in nums

                        products = [p for p in products if _match_num_eq(p)]
                        for s in selected:
                            dyn_query_extras.append((key, s))

                    # Опции — все реально встречающиеся числовые значения, отсортированные.
                    seen = set()
                    for p in products_for_options:
                        val = _product_attr_value(p, key)
                        n = _extract_number(val)
                        if n is not None:
                            seen.add(int(n) if float(n).is_integer() else n)
                    opts_render = []
                    for n in sorted(seen):
                        sv = str(n)
                        label = f"{n} {unit}".strip() if unit else sv
                        opts_render.append({
                            "value": sv,
                            "label_ru": label,
                            "label_uk": label,
                        })
                    dyn_options[key] = opts_render

                else:
                    # select: используем canonical мапинг (value/ru/uk → canonical)
                    # из category_attributes.options_json. Это даёт:
                    #   • поддержку legacy-меток в БД ('Плоский' и 'flat'),
                    #   • правильную дедупликацию в UI,
                    #   • стабильный URL-параметр (canonical value).
                    opts_def = attr.get("options") or []
                    canon_map = {}  # lower(any: value/ru/uk) → canonical_lower
                    canon_to_opt = {}  # canonical_lower → opt
                    for opt in opts_def:
                        canon = str(opt.get("value", "")).strip()
                        if not canon:
                            continue
                        cl = canon.lower()
                        canon_to_opt[cl] = opt
                        canon_map[cl] = cl
                        for lab in (opt.get("ru"), opt.get("uk")):
                            if lab:
                                canon_map[str(lab).strip().lower()] = cl

                    def _to_canon(val, m=canon_map):
                        if not val:
                            return None
                        s = str(val).strip().lower()
                        return m.get(s, s) if s else None

                    if selected:
                        wanted_canon = set()
                        for s in selected:
                            c = _to_canon(s)
                            if c:
                                wanted_canon.add(c)
                        # Шаблон сверяет opt.value|string с selected_vals,
                        # поэтому пишем туда канонические значения,
                        # чтобы чекбокс оставался отмечен после сабмита.
                        dyn_selected[key] = sorted(wanted_canon) if wanted_canon else selected

                        def _match_sel(p, k=key, w=wanted_canon, tc=_to_canon):
                            c = tc(_product_attr_value(p, k))
                            return c is not None and c in w

                        if wanted_canon:
                            products = [p for p in products if _match_sel(p)]
                        # URL-параметры: канонические значения.
                        for c in (dyn_selected[key] or selected):
                            dyn_query_extras.append((key, c))

                    # Канонически встречающиеся варианты у активных товаров.
                    present_canon = set()
                    for p in products_for_options:
                        c = _to_canon(_product_attr_value(p, key))
                        if c:
                            present_canon.add(c)

                    opts_render = []
                    for opt in opts_def:
                        canon = str(opt.get("value", "")).strip().lower()
                        if not canon or canon not in present_canon:
                            continue
                        opts_render.append({
                            "value": canon,  # URL/checkbox value = canonical
                            "label_ru": opt.get("ru") or opt.get("value"),
                            "label_uk": opt.get("uk") or opt.get("ru") or opt.get("value"),
                        })
                    dyn_options[key] = opts_render

            else:
                # render_kind == "range" (number с непрерывным диапазоном).
                seen_nums = []
                for p in products_for_options:
                    val = _product_attr_value(p, key)
                    n = _extract_number(val)
                    if n is not None:
                        seen_nums.append(n)
                if not seen_nums:
                    dyn_range[key] = None
                    continue

                lo_avail = min(seen_nums)
                hi_avail = max(seen_nums)

                raw_min = (qp.get(f"{key}_min") or "").strip()
                raw_max = (qp.get(f"{key}_max") or "").strip()
                legacy_vals = qp.getlist(key) if hasattr(qp, "getlist") else []
                legacy_first = next((v for v in legacy_vals if v and v.strip()), "").strip()
                if legacy_first and not raw_min and not raw_max:
                    raw_min = legacy_first
                    raw_max = legacy_first

                cur_min = _extract_number(raw_min)
                cur_max = _extract_number(raw_max)

                if cur_min is not None or cur_max is not None:
                    lo = cur_min if cur_min is not None else lo_avail
                    hi = cur_max if cur_max is not None else hi_avail

                    def _match_range(p, k=key, a=lo, b=hi):
                        val = _product_attr_value(p, k)
                        n = _extract_number(val)
                        return n is not None and a <= n <= b

                    products = [p for p in products if _match_range(p)]
                    if cur_min is not None:
                        dyn_query_extras.append((f"{key}_min", raw_min))
                    if cur_max is not None:
                        dyn_query_extras.append((f"{key}_max", raw_max))

                def _fmt(n):
                    return str(int(n)) if float(n).is_integer() else str(n)

                dyn_range[key] = {
                    "min": _fmt(lo_avail),
                    "max": _fmt(hi_avail),
                    "current_min": raw_min,
                    "current_max": raw_max,
                    "unit": unit,
                }

    per_page = 12
    # ── Sort ──
    # price_asc / price_desc — по current_price (fallback → price);
    # new/newest — по id desc (новые товары имеют больший id);
    # popular / "" — не трогаем порядок из БД (это выборка из list_site_products).
    def _price_of(p):
        try:
            return float(p.get("current_price") or p.get("price") or 0)
        except (TypeError, ValueError):
            return 0.0
    def _id_of(p):
        try:
            return int(p.get("id") or 0)
        except (TypeError, ValueError):
            return 0
    if sort == "price_asc":
        products = sorted(products, key=_price_of)
    elif sort == "price_desc":
        products = sorted(products, key=_price_of, reverse=True)
    elif sort in ("new", "newest"):
        products = sorted(products, key=_id_of, reverse=True)
    # popular / "" — без изменений.

    total = len(products)
    pages = math.ceil(total / per_page) if total else 1
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    products_page = products[start:start + per_page]

    categories = await db.get_categories()
    site_categories = await db.list_active_site_categories()

    # Локализованные карточки категорий: сначала стабильный набор,
    # дальше — кастомные категории сайта, которых нет в нашем словаре.
    seen_keys = set()
    category_cards = []
    for c in categories_for_lang("ru"):
        category_cards.append({
            "key": c["key"],
            "name_ru": c["name_ru"],
            "name_uk": c["name_uk"],
            "emoji": c["emoji"],
            "filter_value": c["name_ru"],  # canonical RU → совпадёт по ключу
        })
        seen_keys.add(c["key"])
    for sc in site_categories or []:
        nm_ru = (sc.get("name_ru") or "").strip()
        if not nm_ru:
            continue
        k = category_key(nm_ru)
        if k and k in seen_keys:
            continue
        category_cards.append({
            "key": k or nm_ru.lower(),
            "name_ru": nm_ru,
            "name_uk": (sc.get("name_uk") or nm_ru),
            "emoji": sc.get("emoji") or "📦",
            "filter_value": nm_ru,
        })

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
    active_banners = await db.list_active_banners()
    seo_index = await db.get_seo_page("index")

    # Build effective SEO: category overrides main page when a category is selected
    if category:
        _cat_row = await db.get_site_category_by_name(category)
        _auto_tpls = await db.get_auto_seo_templates()
        _tpl_title = _auto_tpls.get("seo_tpl_category_title") or DEFAULT_AUTO_SEO_TEMPLATES["seo_tpl_category_title"]
        _tpl_desc = _auto_tpls.get("seo_tpl_category_desc") or DEFAULT_AUTO_SEO_TEMPLATES["seo_tpl_category_desc"]
        _cat_vars = {"category_name": category, "site_title": site_title}
        _auto_title = _apply_seo_template(_tpl_title, _cat_vars)
        _auto_desc = _apply_seo_template(_tpl_desc, _cat_vars)
        if _cat_row:
            _cat_seo = await db.get_category_seo(_cat_row["id"])
        else:
            _cat_seo = None
        seo_effective = {
            "meta_title": (_cat_seo["meta_title"] if _cat_seo and _cat_seo["meta_title"] else _auto_title),
            "meta_description": (_cat_seo["meta_description"] if _cat_seo and _cat_seo["meta_description"] else _auto_desc),
            "h1": (_cat_seo["h1"] if _cat_seo and _cat_seo["h1"] else category),
            "seo_text": (_cat_seo["seo_text"] if _cat_seo and _cat_seo["seo_text"] else ""),
            "indexable": (bool(_cat_seo["indexable"]) if _cat_seo is not None else True),
        }
    else:
        seo_effective = {
            "meta_title": seo_index.get("meta_title") or "",
            "meta_description": seo_index.get("meta_description") or "",
            "h1": seo_index.get("h1") or "",
            "seo_text": seo_index.get("seo_text") or "",
            "indexable": True,
        }

    # ── Canonical URL ────────────────────────────────────────────
    _site_base = _seo_base_url(request)
    if request.url.path.startswith("/category/"):
        canonical_url = f"{_site_base}{request.url.path}"
    elif category:
        _cat_for_slug = await db.get_site_category_by_name(category)
        _cat_slug = (_cat_for_slug.get("slug") if _cat_for_slug else None) or ""
        if _cat_slug:
            canonical_url = f"{_site_base}/category/{_cat_slug}"
        else:
            canonical_url = f"{_site_base}/?category={quote(category, safe='')}"
    else:
        canonical_url = f"{_site_base}/"

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "products": products_page,
            "categories": categories,
            "site_categories": site_categories,
            "category_cards": category_cards,
            "q": q,
            "current_category": category,
            "current_category_key": category_key(category) or "",
            "page": page,
            "pages": pages,
            "brands": brands,
            "current_brand": brand,
            "price_min": price_min,
            "price_max": price_max,
            "in_stock": in_stock,
            "show_volume_filter": False,
            "available_volumes": [],
            "current_volume": "",
            "dyn_attrs": dyn_attrs,
            "dyn_options": dyn_options,
            "dyn_selected": dyn_selected,
            "dyn_range": dyn_range,
            "dyn_query_extras": dyn_query_extras,
            "current_sort": sort,
            "site_contacts": site_contacts,
            "site_title": site_title,
            "site_subtitle": site_subtitle,
            "header_show_cart": header_show_cart,
            "header_show_contacts": header_show_contacts,
            "header_show_language": header_show_language,
            "site_design": site_design,
            "active_banners": active_banners,
            "seo_index": seo_index,
            "seo_effective": seo_effective,
            "canonical_url": canonical_url,
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


_MODEL_STEM_NOISE = {
    # фичи / опции
    "wifi", "wi-fi", "wf", "smart", "inverter", "inv", "dc",
    "eco", "econom", "premium", "pro", "plus", "lite", "mini", "max",
    # цвета
    "black", "white", "silver", "gray", "grey", "gold", "blue", "red",
    "green", "beige", "brown", "graphite", "titan", "titanium",
    "чорний", "білий", "сірий", "сріблястий", "золотий", "синій",
    "червоний", "зелений", "коричневий", "графіт",
    "черный", "белый", "серый", "серебристый", "золотой", "синий",
    "красный", "зеленый", "коричневый",
}


def _model_stem(model: str) -> str:
    """
    Нормализуем модель для группировки вариантов одной серии.
    Удаляем объёмные/площадные/числовые токены и шумовые слова
    (WiFi, Inverter, цвета), оставляем буквенно-цифровой костяк.

    "Atlantic Steatite 80л"      → "atlantic steatite"
    "Bosch TR2000 50 л"          → "bosch tr"
    "Fujico FMA-12 WiFi"         → "fujico fma"
    "Fujico FMA-18 Inverter"     → "fujico fma"
    """
    s = (model or "")
    if not s:
        return ""
    # NFKD нормализация (полноширинные/спецсимволы → ascii-эквиваленты)
    try:
        import unicodedata
        s = unicodedata.normalize("NFKD", s)
    except Exception:
        pass
    s = s.lower().strip()
    # объёмные токены: "80л", "80 л", "80l", "100 liter(s)", "150 літр(ів)"
    s = re.sub(r"\b\d+[\.,]?\d*\s*(?:л|l|liters?|liter|літр\w*|литр\w*)\b",
               " ", s, flags=re.IGNORECASE)
    # площадные токены: "25м²", "35 m2", "40 кв.м", "50 sqm"
    s = re.sub(r"\b\d+[\.,]?\d*\s*(?:м²|m²|м2|m2|кв\.?\s*м|sq\s*m|sqm)\b",
               " ", s, flags=re.IGNORECASE)
    # мощности/частоты: "2.5 кВт", "12000 BTU", "220В", "50Гц"
    s = re.sub(r"\b\d+[\.,]?\d*\s*(?:квт|kw|btu|вт|w|в|v|гц|hz|а|a)\b",
               " ", s, flags=re.IGNORECASE)
    # дефисы/подчёркивания → пробел (до удаления чисел, чтобы "FMA-12" → "FMA 12")
    s = re.sub(r"[\-_/]+", " ", s)
    # одиночные числовые токены 2–5 цифр (вероятный объём/мощность/индекс модели)
    s = re.sub(r"\b\d{2,5}\b", " ", s)
    # пунктуация → пробел
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    # удаляем шумовые слова (WiFi, Inverter, цвета и т.п.)
    tokens = [t for t in s.split() if t and t not in _MODEL_STEM_NOISE]
    s = " ".join(tokens)
    # двойные пробелы
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _product_volume_l(p):
    """Объём бойлера в литрах: колонка → specifications_json.volume."""
    try:
        v = p["boiler_volume_liters"] if "boiler_volume_liters" in p.keys() else None
    except (KeyError, TypeError, AttributeError):
        v = None
    if v in (None, ""):
        try:
            v = _product_attr_value(p, "volume")
        except Exception:
            v = None
    return _extract_number(v)


def _product_variant_value(p, cat_key):
    """
    Возвращает (value_number, label, unit) — то, по чему различаются варианты
    в данной категории. None — если у товара нет осмысленного значения.
    """
    if cat_key == "boilers":
        n = _product_volume_l(p)
        return (n, "л", "volume") if n is not None else None
    if cat_key == "air_conditioners":
        try:
            v = _product_attr_value(p, "room_area")
        except Exception:
            v = None
        n = _extract_number(v)
        return (n, "м²", "room_area") if n is not None else None
    return None


def _collect_product_variants(current, candidates):
    """
    Финальный отбор вариантов «той же модели».
    Приоритет — model_group: если у текущего товара поле непустое, варианты
    отбираются строго по совпадению model_group (надёжный путь). Если пусто —
    fallback: brand + category + нормализованный stem названия.
    Для бойлеров и кондиционеров требуется наличие различающего значения
    (volume / room_area), и убираются дубликаты с тем же значением.
    """
    if not current or not candidates:
        return []

    cur_id = current["id"]
    cur_brand = (current["brand"] or "")
    cur_model = (current["model"] or "")
    try:
        cur_cat_key = category_key(current["category"] or "") or ""
    except Exception:
        cur_cat_key = ""

    def _model_group_of(p):
        try:
            mg = p["model_group"] if "model_group" in p.keys() else None
        except (KeyError, TypeError, AttributeError):
            mg = None
        return (str(mg).strip().lower() if mg else "")

    cur_mg = _model_group_of(current)
    cur_stem = _model_stem(cur_model)
    if not cur_mg and not cur_stem:
        return []

    def _is_same_group(p):
        mg = _model_group_of(p)
        if cur_mg:
            # Strict matching by model_group when current has it.
            return bool(mg) and mg == cur_mg
        # Fallback: same brand + same normalized stem.
        if (p["brand"] or "").strip().lower() != cur_brand.strip().lower():
            return False
        return _model_stem(p["model"]) == cur_stem

    cur_diff = _product_variant_value(current, cur_cat_key)
    cur_diff_value = cur_diff[0] if cur_diff else None
    diff_unit = cur_diff[1] if cur_diff else ""

    variants = []
    seen_ids = {cur_id}
    for p in candidates:
        if p["id"] in seen_ids:
            continue
        if not _is_same_group(p):
            continue
        diff = _product_variant_value(p, cur_cat_key)
        if cur_cat_key in ("boilers", "air_conditioners"):
            if diff is None:
                continue
            if cur_diff_value is not None and diff[0] == cur_diff_value:
                continue
        seen_ids.add(p["id"])
        variants.append({
            "id": p["id"],
            "brand": p["brand"],
            "model": p["model"],
            "price": p["price"],
            "current_price": p["current_price"],
            "old_price": p["old_price"],
            "is_sale": p["is_sale"],
            "stock_status": p["stock_status"],
            "availability_status": p["availability_status"],
            "diff_value": diff[0] if diff else None,
            "diff_unit": diff[1] if diff else "",
            "is_current": False,
        })

    if not variants:
        return []

    # Текущий товар как active.
    variants.append({
        "id": cur_id,
        "brand": cur_brand,
        "model": cur_model,
        "price": current["price"],
        "current_price": current["current_price"],
        "old_price": current["old_price"],
        "is_sale": current["is_sale"],
        "stock_status": current["stock_status"],
        "availability_status": None,
        "diff_value": cur_diff_value,
        "diff_unit": diff_unit,
        "is_current": True,
    })

    # Сортировка по различающему значению (если есть), иначе по id.
    variants.sort(key=lambda v: (
        v["diff_value"] is None,
        v["diff_value"] if v["diff_value"] is not None else 0,
        v["id"],
    ))
    return variants


def _build_product_seo(product, seo_row, site_title: str, auto_templates: dict = None) -> dict:
    """Build effective SEO dict for a product page, falling back to auto-templates."""
    product_name = f"{product.get('brand') or ''} {product.get('model') or ''}".strip()
    try:
        _price_val = float(product.get("current_price") or product.get("price") or 0)
        _price_str = f"{_price_val:.0f}"
    except Exception:
        _price_str = ""
    brand = str(product.get("brand") or "")
    warranty = str(product.get("warranty_months") or "")

    tpls = auto_templates or {}
    _default_site = site_title or "Technovlada"
    vars_dict = {
        "product_name": product_name,
        "price": _price_str,
        "brand": brand,
        "warranty": warranty,
        "site_title": _default_site,
    }
    tpl_title = tpls.get("seo_tpl_product_title") or DEFAULT_AUTO_SEO_TEMPLATES["seo_tpl_product_title"]
    tpl_desc = tpls.get("seo_tpl_product_desc") or DEFAULT_AUTO_SEO_TEMPLATES["seo_tpl_product_desc"]
    auto_title = _apply_seo_template(tpl_title, vars_dict)
    auto_desc = _apply_seo_template(tpl_desc, vars_dict)

    return {
        "meta_title": (seo_row["meta_title"] if seo_row and seo_row["meta_title"] else auto_title),
        "meta_description": (seo_row["meta_description"] if seo_row and seo_row["meta_description"] else auto_desc),
        "h1": (seo_row["h1"] if seo_row and seo_row["h1"] else product_name),
        "seo_text": (seo_row["seo_text"] if seo_row and seo_row["seo_text"] else ""),
        "indexable": (bool(seo_row["indexable"]) if seo_row is not None else True),
    }


@web_app.get("/product/{slug}", response_class=HTMLResponse)
async def product_page(request: Request, slug: str):
    # Legacy numeric ID → 301 redirect to slug URL
    if slug.isdigit():
        product_id = int(slug)
        p = await db.get_product_by_id(product_id)
        if not p:
            return HTMLResponse("Товар не найден", status_code=404)
        prod_slug = (p.get("slug") or "").strip()
        if prod_slug:
            qs = str(request.url.query)
            target = f"/product/{prod_slug}" + (f"?{qs}" if qs else "")
            return RedirectResponse(target, status_code=301)
        # Slug not yet generated — render directly as fallback
        product = p
        product_id = p["id"]
    else:
        product = await db.get_product_by_slug(slug)
        if not product:
            return HTMLResponse("Товар не найден", status_code=404)
        product_id = product["id"]

    if not product.get("is_active", True) or product.get("deleted_at") is not None:
        return HTMLResponse("Товар недоступен", status_code=404)

    # track view event (fire-and-forget, do not fail on error)
    try:
        await db.add_site_event("product_view", product_id)
    except Exception:
        pass

    images = await db.get_product_images(product_id)
    specifications = await db.get_product_specifications(product_id)

    # ── Варианты той же модели (другие объёмы для бойлеров и т.п.) ──
    variants = []
    try:
        cat_key_for_var = category_key(product["category"] or "") or ""
        if cat_key_for_var and (product["brand"] or "").strip():
            cands = await db.get_product_variants(cat_key_for_var, product["brand"])
            variants = _collect_product_variants(product, cands)
    except Exception as e:
        print(f"[site] product variants failed: {e}")
        variants = []
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

    # ── Category-aware spec labels/order для шаблона ──
    prod_cat = category_key(product.get("category") or "") if isinstance(product, dict) else None
    if not prod_cat:
        try:
            prod_cat = category_key(product["category"] or "")
        except Exception:
            prod_cat = None
    fields_for_product = spec_fields_for(prod_cat)
    spec_labels_ctx = dict(fields_for_product)
    spec_field_order_ctx = [k for k, _ in fields_for_product]

    return templates.TemplateResponse(
        request=request,
        name="product.html",
        context={
            "product": product,
            "images": images,
            "specifications": {
                k: _label_for_spec_value(k, v)
                for k, v in (specifications or {}).items()
            },
            "spec_labels": spec_labels_ctx,
            "spec_field_order": spec_field_order_ctx,
            "variants": variants,
            "site_contacts": site_contacts,
            "site_title": site_title,
            "site_subtitle": site_subtitle,
            "header_show_cart": header_show_cart,
            "header_show_contacts": header_show_contacts,
            "header_show_language": header_show_language,
            "site_design": await get_site_design(),
            "seo_product": _build_product_seo(
                product,
                await db.get_product_seo(product_id),
                site_title,
                await db.get_auto_seo_templates(),
            ),
            "canonical_url": f"{_seo_base_url(request)}/product/{(product.get('slug') or '').strip() or slug}",
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


async def _get_page_context(request: Request, page_key: str, page_title: str):
    """Shared helper for info pages (delivery, warranty, returns)."""
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
    raw_text = await db.get_setting(page_key) or PAGE_DEFAULTS.get(page_key, "")
    # Convert newlines to <br> for HTML display
    page_html = raw_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return {
        "site_contacts": site_contacts,
        "site_title": site_title,
        "site_subtitle": site_subtitle,
        "header_show_cart": header_show_cart,
        "header_show_contacts": header_show_contacts,
        "header_show_language": header_show_language,
        "site_design": await get_site_design(),
        "page_title": page_title,
        "page_html": page_html,
    }


@web_app.get("/dostavka", response_class=HTMLResponse)
async def delivery_page(request: Request):
    ctx = await _get_page_context(request, "delivery_text", "🚚 Доставка")
    return templates.TemplateResponse(request=request, name="infopage.html", context=ctx)


@web_app.get("/garantiya", response_class=HTMLResponse)
async def warranty_info_page(request: Request):
    ctx = await _get_page_context(request, "warranty_text", "🛡 Гарантія")
    return templates.TemplateResponse(request=request, name="infopage.html", context=ctx)


@web_app.get("/povernennya", response_class=HTMLResponse)
async def returns_page(request: Request):
    ctx = await _get_page_context(request, "returns_text", "↩️ Повернення")
    return templates.TemplateResponse(request=request, name="infopage.html", context=ctx)


def _seo_base_url(request: Request) -> str:
    base = (os.getenv("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        if not (base.startswith("http://") or base.startswith("https://")):
            base = "https://" + base
        return base
    # Fallback на текущий хост запроса
    return str(request.base_url).rstrip("/")


def _xml_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


@web_app.get("/category/{slug}", response_class=HTMLResponse)
async def category_slug_page(
    request: Request, slug: str,
    q: str = "", brand: str = "", page: int = 1,
    price_min: str = "", price_max: str = "",
    in_stock: str = "", volume: str = "", sort: str = "",
):
    cat_row = await db.get_site_category_by_slug(slug)
    if not cat_row:
        return HTMLResponse("Категорія не знайдена", status_code=404)
    return await site_home(
        request=request,
        category=cat_row["name_ru"],
        q=q, brand=brand, page=page,
        price_min=price_min, price_max=price_max,
        in_stock=in_stock, volume=volume, sort=sort,
    )


@web_app.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    base = _seo_base_url(request)
    today = datetime.now().strftime("%Y-%m-%d")

    # Ensure all slugs are generated before building sitemap
    try:
        await db.ensure_all_slugs()
    except Exception as e:
        print(f"[sitemap] ensure_all_slugs failed: {e}")

    urls: list[tuple[str, str, str]] = []  # (loc, changefreq, priority)
    urls.append((f"{base}/", "daily", "1.0"))
    urls.append((f"{base}/dostavka", "monthly", "0.5"))
    urls.append((f"{base}/garantiya", "monthly", "0.5"))
    urls.append((f"{base}/povernennya", "monthly", "0.5"))

    # Категории
    try:
        categories = await db.list_active_site_categories()
    except Exception as e:
        print(f"[sitemap] categories failed: {e}")
        categories = []
    for cat in categories:
        try:
            name_ru = cat["name_ru"] if not isinstance(cat, dict) else cat.get("name_ru")
            cat_slug = cat["slug"] if not isinstance(cat, dict) else cat.get("slug")
        except Exception:
            name_ru = None
            cat_slug = None
        if not name_ru:
            continue
        if cat_slug:
            urls.append((f"{base}/category/{cat_slug}", "weekly", "0.8"))
        else:
            urls.append((f"{base}/?category={quote(str(name_ru), safe='')}", "weekly", "0.8"))

    # Товары
    try:
        products = await db.list_site_products()
        noindex_ids = await db.list_noindex_product_ids()
    except Exception as e:
        print(f"[sitemap] products failed: {e}")
        products = []
        noindex_ids = set()
    for p in products:
        try:
            pid = p["id"] if not isinstance(p, dict) else p.get("id")
            p_slug = p["slug"] if not isinstance(p, dict) else p.get("slug")
        except Exception:
            pid = None
            p_slug = None
        if not pid:
            continue
        if pid in noindex_ids:
            continue
        if p_slug:
            urls.append((f"{base}/product/{p_slug}", "weekly", "0.7"))
        else:
            urls.append((f"{base}/product/{pid}", "weekly", "0.7"))

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, changefreq, priority in urls:
        parts.append("  <url>")
        parts.append(f"    <loc>{_xml_escape(loc)}</loc>")
        parts.append(f"    <lastmod>{today}</lastmod>")
        parts.append(f"    <changefreq>{changefreq}</changefreq>")
        parts.append(f"    <priority>{priority}</priority>")
        parts.append("  </url>")
    parts.append("</urlset>")

    return Response(content="\n".join(parts), media_type="application/xml")


@web_app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt(request: Request):
    base = _seo_base_url(request)
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return PlainTextResponse(content=body, media_type="text/plain")


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


@web_app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if telegram_bot is None or dispatcher is None:
        raise HTTPException(status_code=503, detail="Bot not ready")
    if TELEGRAM_SECRET_TOKEN:
        header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_token != TELEGRAM_SECRET_TOKEN:
            raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    update = Update.model_validate(data, context={"bot": telegram_bot})
    await dispatcher.feed_update(telegram_bot, update)
    return {"ok": True}


@web_app.on_event("startup")
async def _on_startup():
    global telegram_bot, dispatcher, _polling_task

    if telegram_bot is not None:
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    telegram_bot = bot
    dispatcher = dp

    await db.connect()
    await db.init_schema()
    try:
        await db.ensure_all_slugs()
    except Exception as e:
        print(f"[startup] ensure_all_slugs failed: {e}")

    if LOCAL_POLLING:
        await bot.delete_webhook(drop_pending_updates=True)
        _polling_task = asyncio.create_task(dp.start_polling(bot))
        print("Бот запущен в режиме polling (LOCAL_POLLING=true) 🚀")
    elif WEBHOOK_URL:
        full_url = WEBHOOK_URL + WEBHOOK_PATH
        set_webhook_kwargs = {"drop_pending_updates": True}
        if TELEGRAM_SECRET_TOKEN:
            set_webhook_kwargs["secret_token"] = TELEGRAM_SECRET_TOKEN
        await bot.set_webhook(full_url, **set_webhook_kwargs)
        print(f"Webhook установлен: {full_url} 🚀")
    else:
        print("⚠️ Ни WEBHOOK_URL, ни LOCAL_POLLING не заданы — бот не получает апдейты.")


@web_app.on_event("shutdown")
async def _on_shutdown():
    global _polling_task
    if _polling_task is not None:
        _polling_task.cancel()
        try:
            await _polling_task
        except (asyncio.CancelledError, Exception):
            pass
        _polling_task = None
    if telegram_bot is not None:
        try:
            await telegram_bot.session.close()
        except Exception:
            pass
    try:
        await db.close()
    except Exception:
        pass

async def save_telegram_photo(bot: Bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    file_path = file.file_path

    local_filename = f"/tmp/{uuid4()}.jpg"

    try:
        await bot.download_file(file_path, local_filename)

        result = cloudinary.uploader.upload(
            local_filename,
            folder="tech_bot_products"
        )

        return result["secure_url"]
    finally:
        try:
            if os.path.exists(local_filename):
                os.remove(local_filename)
        except Exception as e:
            print(f"[save_telegram_photo] cleanup failed: {e}")

async def main():
    # Запускаем uvicorn; startup-хук поднимет бота (webhook или polling).
    print("Бот и сайт API запускаются 🚀")
    await start_web_server()
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
