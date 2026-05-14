"""Категории товаров: стабильные ключи + локализованные названия.

DB-колонка `products.category` продолжает хранить человеко-читаемое имя
(каноничное RU из CATEGORY_LABELS). Все сравнения/локализации идут через
функции этого модуля.
"""

# Стабильный порядок ключей — он же порядок кнопок в боте/на сайте.
CATEGORY_KEYS = [
    "boilers",
    "refrigerators",
    "washing_machines",
    "air_conditioners",
    "heaters",
    "hoods",
    "gas_stoves",
    "microwaves",
    "vacuum_cleaners",
    "other",
]

# Канонические названия и переводы.
CATEGORY_LABELS = {
    "boilers":          {"ru": "Бойлеры",          "uk": "Бойлери",         "emoji": "♨️"},
    "refrigerators":    {"ru": "Холодильники",     "uk": "Холодильники",    "emoji": "🧊"},
    "washing_machines": {"ru": "Стиральные машины","uk": "Пральні машини",  "emoji": "🧺"},
    "air_conditioners": {"ru": "Кондиционеры",     "uk": "Кондиціонери",    "emoji": "❄️"},
    "heaters":          {"ru": "Обогреватели",     "uk": "Обігрівачі",      "emoji": "🔥"},
    "hoods":            {"ru": "Вытяжки",          "uk": "Витяжки",         "emoji": "🌬️"},
    "gas_stoves":       {"ru": "Газовые плиты",    "uk": "Газові плити",    "emoji": "🍳"},
    "microwaves":       {"ru": "Микроволновки",    "uk": "Мікрохвильовки",  "emoji": "📡"},
    "vacuum_cleaners":  {"ru": "Пылесосы",         "uk": "Пилососи",        "emoji": "🧹"},
    "other":            {"ru": "Другая техника",   "uk": "Інша техніка",    "emoji": "📦"},
}

# Алиасы (старые/синонимичные написания) → ключ. Сравнение case-insensitive,
# по trim. Ключи CATEGORY_LABELS уже распознаются автоматически.
_ALIASES = {
    # boilers
    "бойлер": "boilers", "бойлеры": "boilers", "бойлери": "boilers",
    "нагреватель": "boilers", "нагреватели": "boilers",
    "нагрівач": "boilers", "нагрівачі": "boilers",
    # refrigerators
    "холодильник": "refrigerators", "холодильники": "refrigerators",
    "морозильник": "refrigerators", "морозильники": "refrigerators",
    # washing machines
    "стиральная машина": "washing_machines",
    "стиральные машины": "washing_machines",
    "пральна машина": "washing_machines",
    "пральні машини": "washing_machines",
    # air conditioners
    "кондиционер": "air_conditioners", "кондиционеры": "air_conditioners",
    "кондиціонер": "air_conditioners", "кондиціонери": "air_conditioners",
    # heaters (отдельно от бойлеров — электро/конвекторы)
    "обогреватель": "heaters", "обогреватели": "heaters",
    "обігрівач": "heaters", "обігрівачі": "heaters",
    # hoods
    "вытяжка": "hoods", "вытяжки": "hoods",
    "витяжка": "hoods", "витяжки": "hoods",
    # gas stoves
    "газовая плита": "gas_stoves", "газовые плиты": "gas_stoves",
    "газова плита": "gas_stoves", "газові плити": "gas_stoves",
    "плита": "gas_stoves", "плиты": "gas_stoves",
    # microwaves
    "микроволновка": "microwaves", "микроволновки": "microwaves",
    "мікрохвильовка": "microwaves", "мікрохвильовки": "microwaves",
    "свч": "microwaves",
    # vacuum cleaners
    "пылесос": "vacuum_cleaners", "пылесосы": "vacuum_cleaners",
    "пилосос": "vacuum_cleaners", "пилососи": "vacuum_cleaners",
    # other
    "другая техника": "other", "інша техніка": "other",
    "другое": "other", "інше": "other",
}


def category_key(value):
    """Нормализует значение (имя/ключ/легаси) в стабильный ключ.

    Возвращает None, если значение пустое или не распознаётся.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in CATEGORY_LABELS:  # уже ключ
        return s
    return _ALIASES.get(s)


def category_label(value, lang: str = "ru") -> str:
    """Возвращает локализованное название для любого ввода.

    Если ключ неизвестен — возвращает исходное значение как есть.
    """
    if value is None:
        return ""
    k = category_key(value)
    if k and k in CATEGORY_LABELS:
        return CATEGORY_LABELS[k].get(lang) or CATEGORY_LABELS[k]["ru"]
    return str(value).strip()


def canonical_ru(value) -> str:
    """RU-каноническое имя для записи в DB. Для неизвестных — исходный текст."""
    if value is None:
        return ""
    k = category_key(value)
    if k:
        return CATEGORY_LABELS[k]["ru"]
    return str(value).strip()


def category_emoji(value) -> str:
    k = category_key(value)
    if k and k in CATEGORY_LABELS:
        return CATEGORY_LABELS[k].get("emoji", "📦")
    return "📦"


def categories_for_lang(lang: str = "ru"):
    """Список словарей для UI: ключи + локализованное имя + emoji."""
    return [
        {
            "key": k,
            "name": CATEGORY_LABELS[k].get(lang) or CATEGORY_LABELS[k]["ru"],
            "name_ru": CATEGORY_LABELS[k]["ru"],
            "name_uk": CATEGORY_LABELS[k]["uk"],
            "emoji": CATEGORY_LABELS[k].get("emoji", "📦"),
        }
        for k in CATEGORY_KEYS
    ]


def same_category(a, b) -> bool:
    """Сравнение двух категорий по ключу (с учётом всех алиасов)."""
    ka = category_key(a)
    kb = category_key(b)
    if ka and kb:
        return ka == kb
    # неизвестные — сравниваем как строки case-insensitive
    return (str(a or "").strip().lower() == str(b or "").strip().lower())
