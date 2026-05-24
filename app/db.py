import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


class Database:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        if not self.database_url:
            raise ValueError("Не найден DATABASE_URL в .env")
        self.pool = await asyncpg.create_pool(self.database_url)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def execute(self, query: str, *args):
        if not self.pool:
            raise RuntimeError("База данных не подключена")
        return await self.pool.execute(query, *args)

    async def fetch(self, query: str, *args):
        if not self.pool:
            raise RuntimeError("База данных не подключена")
        return await self.pool.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        if not self.pool:
            raise RuntimeError("База данных не подключена")
        return await self.pool.fetchrow(query, *args)

    async def init_schema(self):
        if not self.pool:
            raise RuntimeError("База данных не подключена")

        await self.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            price NUMERIC(12, 2) NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS category TEXT;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS brand TEXT;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS model TEXT;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS name TEXT;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS price NUMERIC(12, 2) NOT NULL DEFAULT 0;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS stock_qty INTEGER NOT NULL DEFAULT 0;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS purchase_price NUMERIC(12,2) DEFAULT 0;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS purchase_currency TEXT DEFAULT 'UAH';
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS sku TEXT;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS warranty_months INTEGER DEFAULT 0;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS photo_url TEXT;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS description TEXT;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS specs TEXT;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS specifications_json JSONB NOT NULL DEFAULT '{}'::jsonb;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS availability_status TEXT NOT NULL DEFAULT 'in_stock';
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP DEFAULT NULL;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS current_price NUMERIC(12,2);
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS old_price NUMERIC(12,2);
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS is_sale BOOLEAN NOT NULL DEFAULT FALSE;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS stock_status TEXT NOT NULL DEFAULT 'in_stock';
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS boiler_volume_liters INTEGER;
        """)

        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS boiler_ten_type TEXT;
        """)

        # --- Variants foundation: model_group ------------------------------
        # Группирует варианты одной модельной серии (разные объёмы
        # бойлеров, разные площади кондиционеров и т.п.). При пустом
        # значении варианты ищутся фолбэком через brand + нормализованное
        # название модели.
        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS model_group TEXT;
        """)
        await self.execute("""
        CREATE INDEX IF NOT EXISTS idx_products_model_group
            ON products (model_group)
            WHERE model_group IS NOT NULL;
        """)

        # --- Foundation: stable category_key (этап 1) ----------------------
        # Хранится параллельно с человеко-читаемым `category` для обратной
        # совместимости. Заполняется автоматически при INSERT/UPDATE и
        # бэкфиллится по существующим товарам ниже.
        await self.execute("""
        ALTER TABLE products
        ADD COLUMN IF NOT EXISTS category_key TEXT;
        """)
        await self.execute("""
        CREATE INDEX IF NOT EXISTS idx_products_category_key
        ON products (category_key);
        """)

        # Foundation для будущих auto-filters: справочник атрибутов категории.
        # Пока никем не читается — только создаём схему.
        await self.execute("""
        CREATE TABLE IF NOT EXISTS category_attributes (
            id SERIAL PRIMARY KEY,
            category_key TEXT NOT NULL,
            attr_key TEXT NOT NULL,
            label_ru TEXT NOT NULL,
            label_uk TEXT,
            attr_type TEXT NOT NULL DEFAULT 'string',
            unit TEXT,
            options_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            is_filter BOOLEAN NOT NULL DEFAULT FALSE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE (category_key, attr_key)
        );
        """)

        # Seed дефолтных атрибутов (idempotent — ON CONFLICT DO NOTHING).
        try:
            await self._seed_default_category_attributes()
        except Exception as e:
            print(f"[migrate] seed category_attributes failed: {e}")

        # Backfill category_key из existing category (idempotent).
        try:
            from app.categories import CATEGORY_LABELS, _ALIASES
            # Сопоставление "имя→ключ" собираем из словаря.
            pairs = []
            for key, labels in CATEGORY_LABELS.items():
                pairs.append((labels["ru"], key))
                pairs.append((labels["uk"], key))
            for alias, key in _ALIASES.items():
                pairs.append((alias, key))
            for name, key in pairs:
                await self.execute(
                    """
                    UPDATE products
                    SET category_key = $2
                    WHERE category_key IS NULL
                      AND LOWER(TRIM(category)) = LOWER(TRIM($1))
                    """,
                    name, key,
                )
        except Exception as e:
            print(f"[migrate] category_key backfill failed: {e}")

        # ── One-shot: normalize legacy spec labels to canonical keys ──
        try:
            await self._migrate_normalize_specifications()
        except Exception as e:
            print(f"[migrate] normalize specifications failed: {e}")

        # ── One-shot: обновить атрибуты air_conditioners v2 ──
        try:
            await self._migrate_ac_attributes_v2()
        except Exception as e:
            print(f"[migrate] ac_attributes_v2 failed: {e}")

        await self.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            city TEXT,
            comment TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL UNIQUE,
            full_name TEXT,
            role TEXT NOT NULL DEFAULT 'seller',
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        await self.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS language TEXT DEFAULT 'ru';
        """)

        await self.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id SERIAL PRIMARY KEY,
            product_id INTEGER,
            qty INTEGER,
            sale_price NUMERIC(12,2),
            total_amount NUMERIC(12,2),
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        await self.execute("""
        ALTER TABLE sales
        ADD COLUMN IF NOT EXISTS customer_id INTEGER;
        """)

        await self.execute("""
        ALTER TABLE sales
        ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed';
        """)

        await self.execute("""
        ALTER TABLE sales
        ADD COLUMN IF NOT EXISTS purchase_price_snapshot NUMERIC(12,2) DEFAULT 0;
        """)

        await self.execute("""
        ALTER TABLE sales
        ADD COLUMN IF NOT EXISTS purchase_currency_snapshot TEXT DEFAULT 'UAH';
        """)

        await self.execute("""
        ALTER TABLE sales
        ADD COLUMN IF NOT EXISTS currency_rate_snapshot NUMERIC(12,4) DEFAULT 1;
        """)

        await self.execute("""
        ALTER TABLE sales
        ADD COLUMN IF NOT EXISTS cost_total_uah NUMERIC(12,2) DEFAULT 0;
        """)

        await self.execute("""
        ALTER TABLE sales
        ADD COLUMN IF NOT EXISTS profit_uah NUMERIC(12,2) DEFAULT 0;
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            purchase_price NUMERIC(12,2) NOT NULL,
            total_amount NUMERIC(12,2) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

        await self.execute("""
        INSERT INTO settings (key, value)
        VALUES ('usd_rate', '40'), ('eur_rate', '43')
        ON CONFLICT (key) DO NOTHING;
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS warranties (
            id SERIAL PRIMARY KEY,
            sale_id INTEGER,
            product_id INTEGER,
            customer_id INTEGER,
            warranty_months INTEGER NOT NULL DEFAULT 0,
            start_date DATE NOT NULL DEFAULT CURRENT_DATE,
            end_date DATE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)
        
        await self.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            customer_id INTEGER,
            product_id INTEGER,
            qty INTEGER NOT NULL DEFAULT 1,
            total_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'new',
            comment TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS site_categories (
            id SERIAL PRIMARY KEY,
            name_ru TEXT NOT NULL,
            name_uk TEXT NOT NULL,
            emoji TEXT DEFAULT '📦',
            sort_order INTEGER DEFAULT 100,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS site_brands (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 100,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)
        await self.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS site_brands_name_lower_uq
        ON site_brands (LOWER(TRIM(name)));
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS product_images (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
            image_url TEXT NOT NULL,
            sort_order INTEGER DEFAULT 100
        );
        """)

        await self.execute("""
        CREATE TABLE IF NOT EXISTS site_events (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            product_id INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        # ── Multi-banner slider ───────────────────────────────────
        await self.execute("""
        CREATE TABLE IF NOT EXISTS banners (
            id SERIAL PRIMARY KEY,
            image_url TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            subtitle TEXT NOT NULL DEFAULT '',
            button_text TEXT NOT NULL DEFAULT '',
            button_link TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 100,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        # ── SEO per category ─────────────────────────────────────
        await self.execute("""
        CREATE TABLE IF NOT EXISTS seo_categories (
            id SERIAL PRIMARY KEY,
            site_category_id INTEGER NOT NULL UNIQUE REFERENCES site_categories(id) ON DELETE CASCADE,
            meta_title TEXT NOT NULL DEFAULT '',
            meta_description TEXT NOT NULL DEFAULT '',
            h1 TEXT NOT NULL DEFAULT '',
            seo_text TEXT NOT NULL DEFAULT '',
            indexable BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

        # ── SEO per product ──────────────────────────────────────
        await self.execute("""
        CREATE TABLE IF NOT EXISTS seo_products (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL UNIQUE REFERENCES products(id) ON DELETE CASCADE,
            meta_title TEXT NOT NULL DEFAULT '',
            meta_description TEXT NOT NULL DEFAULT '',
            h1 TEXT NOT NULL DEFAULT '',
            seo_text TEXT NOT NULL DEFAULT '',
            indexable BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

    async def add_product(
        self,
        category: str,
        brand: str,
        model: str,
        price: float,
        purchase_price: float = 0,
        purchase_currency: str = "UAH",
        sku: str | None = None,
        warranty_months: int = 0,
        specifications: dict | None = None,
    ):
        name = f"{brand} {model}".strip()

        # Foundation: параллельно проставляем стабильный category_key.
        try:
            from app.categories import category_key as _cat_key
            cat_key = _cat_key(category)
        except Exception:
            cat_key = None

        import json
        specs_json = json.dumps(specifications or {})

        row = await self.fetchrow(
            """
            INSERT INTO products (
                name, category, category_key, brand, model, price,
                purchase_price, purchase_currency, sku, warranty_months, stock_qty,
                specifications_json
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,0,$11::jsonb)
            RETURNING id
            """,
            name,
            category,
            cat_key,
            brand,
            model,
            price,
            purchase_price,
            purchase_currency,
            sku,
            warranty_months,
            specs_json,
        )
        return int(row["id"]) if row else None

    async def list_products(self):
        return await self.fetch(
            """
            SELECT
                id, category, brand, model, price, stock_qty,
                purchase_price, purchase_currency, sku, warranty_months,
                photo_url, description, specs,
                current_price, old_price, is_sale, stock_status,
                availability_status
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND deleted_at IS NULL
              AND NOT (
                LOWER(COALESCE(NULLIF(TRIM(brand), ''), '-')) IN ('-', 'none')
                AND LOWER(COALESCE(NULLIF(TRIM(model), ''), '-')) IN ('-', 'none')
              )
            ORDER BY id ASC
            """
        )

    async def count_products_active(self) -> int:
        row = await self.fetchrow(
            """
            SELECT COUNT(*) AS c FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND deleted_at IS NULL
            """
        )
        return int(row["c"]) if row else 0

    async def list_site_products(self):
        return await self.fetch(
            """
            SELECT
                id, category, category_key, brand, model, price, stock_qty,
                warranty_months, photo_url, description, availability_status,
                current_price, old_price, is_sale, stock_status,
                boiler_volume_liters, specifications_json
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND deleted_at IS NULL
              AND COALESCE(availability_status, 'in_stock') != 'hidden'
              AND NOT (
                LOWER(COALESCE(NULLIF(TRIM(brand), ''), '-')) IN ('-', 'none')
                AND LOWER(COALESCE(NULLIF(TRIM(model), ''), '-')) IN ('-', 'none')
              )
            ORDER BY id DESC
            """
        )

    async def search_products(self, query: str, limit: int = 10):
        q = (query or "").strip()
        # Разбиваем запрос на слова — каждое слово должно встречаться
        # в объединённом поле (brand + model + category + sku).
        tokens = [t for t in q.split() if t]
        if not tokens:
            tokens = [q] if q else [""]

        params = []
        conds = []
        haystack = (
            "LOWER(CONCAT_WS(' ', "
            "COALESCE(brand, ''), "
            "COALESCE(model, ''), "
            "COALESCE(category, ''), "
            "COALESCE(sku, '')"
            "))"
        )
        for tok in tokens:
            params.append(f"%{tok.lower()}%")
            conds.append(f"{haystack} LIKE ${len(params)}")

        token_where = " AND ".join(conds) if conds else "TRUE"
        params.append(limit)
        limit_idx = len(params)

        sql = f"""
            SELECT id, category, brand, model, price, stock_qty, sku
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND deleted_at IS NULL
              AND NOT (
                LOWER(COALESCE(NULLIF(TRIM(brand), ''), '-')) IN ('-', 'none')
                AND LOWER(COALESCE(NULLIF(TRIM(model), ''), '-')) IN ('-', 'none')
              )
              AND ({token_where})
            ORDER BY id DESC
            LIMIT ${limit_idx}
        """
        return await self.fetch(sql, *params)

    async def search_site_products(self, query: str):
        return await self.fetch(
            """
            SELECT
                id, category, category_key, brand, model, price, stock_qty,
                warranty_months, photo_url, description, availability_status,
                current_price, old_price, is_sale, stock_status,
                boiler_volume_liters, specifications_json
            FROM products
            WHERE COALESCE(availability_status, 'in_stock') != 'hidden'
              AND COALESCE(is_active, TRUE) = TRUE
              AND deleted_at IS NULL
              AND NOT (
                LOWER(COALESCE(NULLIF(TRIM(brand), ''), '-')) IN ('-', 'none')
                AND LOWER(COALESCE(NULLIF(TRIM(model), ''), '-')) IN ('-', 'none')
              )
              AND (
                LOWER(COALESCE(category, '')) LIKE LOWER($1)
                OR LOWER(COALESCE(brand, '')) LIKE LOWER($1)
                OR LOWER(COALESCE(model, '')) LIKE LOWER($1)
              )
            ORDER BY id DESC
            """,
            f"%{query}%"
        )

    async def get_categories(self):
        rows = await self.fetch("""
            SELECT DISTINCT category
            FROM products
            WHERE category IS NOT NULL
            ORDER BY category
        """)
        return [r["category"] for r in rows]

    async def get_product_by_id(self, product_id: int):
        return await self.fetchrow(
            """
            SELECT
                id, category, brand, model, price, stock_qty,
                purchase_price, purchase_currency, sku, warranty_months,
                photo_url, description, specs, is_active, deleted_at,
                current_price, old_price, is_sale, stock_status,
                boiler_volume_liters, boiler_ten_type,
                model_group,
                specifications_json
            FROM products
            WHERE id = $1
            """,
            product_id
        )

    async def get_product_variants(self, category_key: str, brand: str):
        """
        Кандидаты-варианты: тот же category_key, тот же brand,
        активные, не скрытые, не удалённые. Финальный отбор «той же модели»
        делает Python-слой (по model_group или нормализованному stem).
        """
        if not category_key or not brand:
            return []
        return await self.fetch(
            """
            SELECT
                id, category, category_key, brand, model, price,
                photo_url, availability_status, current_price, old_price,
                is_sale, stock_status, boiler_volume_liters, model_group,
                specifications_json
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND deleted_at IS NULL
              AND COALESCE(availability_status, 'in_stock') != 'hidden'
              AND LOWER(COALESCE(brand, '')) = LOWER($2)
              AND (
                category_key = $1
                OR (category_key IS NULL AND LOWER(COALESCE(category, '')) = LOWER($1))
              )
            ORDER BY id ASC
            """,
            category_key, brand,
        )

    async def get_product_images(self, product_id: int):
        return await self.fetch(
            """
            SELECT id, image_url
            FROM product_images
            WHERE product_id = $1
            ORDER BY sort_order ASC, id ASC
            """,
            product_id
        )

    async def get_product_image_by_id(self, image_id: int):
        return await self.fetchrow(
            """
            SELECT id, product_id, image_url
            FROM product_images
            WHERE id = $1
            """,
            image_id
        )

    async def delete_product_image(self, image_id: int):
        await self.execute(
            "DELETE FROM product_images WHERE id = $1",
            image_id
        )

    async def add_product_image(self, product_id: int, image_url: str):
        await self.execute(
            """
            INSERT INTO product_images (product_id, image_url, sort_order)
            VALUES ($1, $2, 100)
            """,
            product_id,
            image_url
        )

    async def count_product_images(self, product_id: int) -> int:
        row = await self.fetchrow(
            "SELECT COUNT(*) AS c FROM product_images WHERE product_id = $1",
            product_id
        )
        return int(row["c"]) if row else 0

    async def count_product_images_total(self, product_id: int) -> int:
        """Count gallery images + legacy products.photo_url if not duplicated."""
        row = await self.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM product_images WHERE product_id = $1) AS gallery,
                (SELECT photo_url FROM products WHERE id = $1) AS legacy
            """,
            product_id
        )
        if not row:
            return 0
        gallery = int(row["gallery"] or 0)
        legacy = row["legacy"]
        if not legacy:
            return gallery
        dup = await self.fetchrow(
            "SELECT 1 FROM product_images WHERE product_id = $1 AND image_url = $2 LIMIT 1",
            product_id, legacy
        )
        return gallery + (0 if dup else 1)

    async def add_product_image_if_under_limit(self, product_id: int, image_url: str, limit: int = 6) -> int | None:
        """Atomically add image only if total photos < limit. Returns new image id or None."""
        if not self.pool:
            raise RuntimeError("DB pool not initialized")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", int(product_id))
                row = await conn.fetchrow(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM product_images WHERE product_id = $1) AS gallery,
                        (SELECT photo_url FROM products WHERE id = $1) AS legacy
                    """,
                    product_id
                )
                gallery = int(row["gallery"] or 0) if row else 0
                legacy = row["legacy"] if row else None
                total = gallery
                if legacy:
                    dup = await conn.fetchrow(
                        "SELECT 1 FROM product_images WHERE product_id = $1 AND image_url = $2 LIMIT 1",
                        product_id, legacy
                    )
                    if not dup:
                        total += 1
                if total >= limit:
                    return None
                inserted = await conn.fetchrow(
                    """
                    INSERT INTO product_images (product_id, image_url, sort_order)
                    VALUES ($1, $2, 100)
                    RETURNING id
                    """,
                    product_id, image_url
                )
                return int(inserted["id"]) if inserted else None

    async def set_main_product_image(self, image_id: int):
        img = await self.fetchrow(
            "SELECT id, product_id, image_url FROM product_images WHERE id = $1",
            image_id
        )
        if not img:
            return None
        product_id = img["product_id"]
        await self.execute(
            "UPDATE product_images SET sort_order = 100 WHERE product_id = $1",
            product_id
        )
        await self.execute(
            "UPDATE product_images SET sort_order = 0 WHERE id = $1",
            image_id
        )
        await self.execute(
            "UPDATE products SET photo_url = $2 WHERE id = $1",
            product_id,
            img["image_url"]
        )
        return img

    async def update_stock_qty(self, product_id: int, stock_qty: int):
        await self.execute(
            """
            UPDATE products
            SET stock_qty = $2
            WHERE id = $1
            """,
            product_id,
            stock_qty
        )

    async def update_product_field(self, product_id: int, field: str, value):
        allowed_fields = {
            "price",
            "purchase_price",
            "purchase_currency",
            "sku",
            "warranty_months",
            "model",
            "photo_url",
            "description",
            "specs",
            "current_price",
            "old_price",
            "is_sale",
            "stock_status",
            "boiler_volume_liters",
            "boiler_ten_type",
            "model_group",
        }

        if field not in allowed_fields:
            raise ValueError("Недопустимое поле")

        await self.execute(
            f"""
            UPDATE products
            SET {field} = $2
            WHERE id = $1
            """,
            product_id,
            value
        )

    async def update_product_category(self, product_id: int, category: str):
        try:
            from app.categories import category_key as _cat_key
            cat_key = _cat_key(category)
        except Exception:
            cat_key = None
        await self.execute(
            "UPDATE products SET category = $2, category_key = $3 WHERE id = $1",
            product_id,
            category,
            cat_key,
        )

    async def remove_product_photo(self, product_id: int):
        await self.execute(
            "UPDATE products SET photo_url = NULL WHERE id = $1",
            product_id
        )

    async def hide_product(self, product_id: int):
        await self.execute(
            "UPDATE products SET is_active = FALSE WHERE id = $1",
            product_id
        )

    async def show_product(self, product_id: int):
        await self.execute(
            "UPDATE products SET is_active = TRUE WHERE id = $1",
            product_id
        )

    async def soft_delete_product(self, product_id: int):
        await self.execute(
            "UPDATE products SET deleted_at = NOW(), is_active = FALSE WHERE id = $1",
            product_id
        )

    async def get_product_specifications(self, product_id: int) -> dict:
        row = await self.fetchrow(
            "SELECT specifications_json FROM products WHERE id = $1",
            product_id,
        )
        if not row:
            return {}
        raw = row["specifications_json"]
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            import json
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def set_product_specification(self, product_id: int, key: str, value: str):
        import json
        await self.execute(
            """
            UPDATE products
            SET specifications_json = COALESCE(specifications_json, '{}'::jsonb) || $2::jsonb
            WHERE id = $1
            """,
            product_id,
            json.dumps({key: value}),
        )

    async def clear_product_specification(self, product_id: int, key: str):
        await self.execute(
            """
            UPDATE products
            SET specifications_json = COALESCE(specifications_json, '{}'::jsonb) - $2
            WHERE id = $1
            """,
            product_id,
            key,
        )

    async def soft_delete_broken_products(self) -> int:
        """Soft-delete products whose display name (brand + model) is empty/null/'-'/'None'."""
        row = await self.fetchrow(
            """
            WITH updated AS (
                UPDATE products
                SET deleted_at = NOW(), is_active = FALSE
                WHERE COALESCE(is_active, TRUE) = TRUE
                  AND deleted_at IS NULL
                  AND LOWER(COALESCE(NULLIF(TRIM(brand), ''), '-')) IN ('-', 'none')
                  AND LOWER(COALESCE(NULLIF(TRIM(model), ''), '-')) IN ('-', 'none')
                RETURNING id
            )
            SELECT COUNT(*) AS c FROM updated
            """
        )
        return int(row["c"]) if row else 0

    async def create_purchase(self, product_id: int, qty: int, purchase_price: float):
        total_amount = qty * purchase_price

        await self.execute(
            """
            INSERT INTO purchases (product_id, qty, purchase_price, total_amount)
            VALUES ($1, $2, $3, $4)
            """,
            product_id,
            qty,
            purchase_price,
            total_amount
        )

        return total_amount

    async def get_customer_by_phone(self, phone: str):
        return await self.fetchrow(
            """
            SELECT id, name, phone, city, comment
            FROM customers
            WHERE phone = $1
            """,
            phone
        )

    async def create_customer(self, name: str, phone: str, city: str, comment: str | None = None):
        return await self.fetchrow(
            """
            INSERT INTO customers (name, phone, city, comment)
            VALUES ($1, $2, $3, $4)
            RETURNING id, name, phone, city, comment
            """,
            name,
            phone,
            city,
            comment
        )

    async def get_user_by_telegram_id(self, telegram_id: int):
        return await self.fetchrow(
            """
            SELECT id, telegram_id, full_name, role, language, is_active
            FROM users
            WHERE telegram_id = $1
            """,
            telegram_id
        )

    async def create_user_if_not_exists(self, telegram_id: int, full_name: str):
        await self.execute(
            """
            INSERT INTO users (telegram_id, full_name)
            VALUES ($1, $2)
            ON CONFLICT (telegram_id) DO NOTHING
            """,
            telegram_id,
            full_name
        )

    async def update_user_role(self, telegram_id: int, role: str):
        await self.execute(
            """
            UPDATE users
            SET role = $2
            WHERE telegram_id = $1
            """,
            telegram_id,
            role
        )

    async def update_user_language(self, telegram_id: int, language: str):
        await self.execute(
            """
            UPDATE users
            SET language = $2
            WHERE telegram_id = $1
            """,
            telegram_id,
            language
        )

    async def set_user_language(self, telegram_id: int, language: str):
        await self.execute(
            """
            UPDATE users
            SET language = $2
            WHERE telegram_id = $1
            """,
            telegram_id,
            language
        )

    async def list_users(self):
        return await self.fetch(
            """
            SELECT id, telegram_id, full_name, role, is_active
            FROM users
            ORDER BY id DESC
            """
        )

    async def add_admin_by_telegram_id(self, telegram_id: int):
        existing = await self.fetchrow(
            "SELECT id FROM users WHERE telegram_id = $1",
            telegram_id
        )
        if existing:
            await self.execute(
                "UPDATE users SET role = 'admin', is_active = TRUE WHERE telegram_id = $1",
                telegram_id
            )
        else:
            await self.execute(
                """
                INSERT INTO users (telegram_id, full_name, role, is_active)
                VALUES ($1, NULL, 'admin', TRUE)
                """,
                telegram_id
            )

    async def deactivate_user_by_telegram_id(self, telegram_id: int):
        await self.execute(
            "UPDATE users SET is_active = FALSE WHERE telegram_id = $1",
            telegram_id
        )

    async def count_active_admins(self) -> int:
        row = await self.fetchrow(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin' AND is_active = TRUE"
        )
        return int(row["cnt"]) if row else 0

    async def list_customers(self):
        return await self.fetch(
            """
            SELECT id, name, phone, city
            FROM customers
            ORDER BY id DESC
            LIMIT 50
            """
        )

    async def search_customers(self, query: str):
        return await self.fetch(
            """
            SELECT id, name, phone, city
            FROM customers
            WHERE LOWER(name) LIKE LOWER($1)
               OR phone LIKE $1
               OR LOWER(COALESCE(city, '')) LIKE LOWER($1)
            ORDER BY id DESC
            LIMIT 20
            """,
            f"%{query}%"
        )

    async def create_sale(self, product_id: int, qty: int, price: float, customer_id: int):
        product = await self.get_product_by_id(product_id)

        if not product:
            raise ValueError("Товар не найден")

        rates = await self.get_currency_rates()

        purchase_price = float(product["purchase_price"] or 0)
        purchase_currency = product["purchase_currency"] or "UAH"
        currency_rate = float(rates.get(purchase_currency, 1))

        total = qty * price
        cost_total_uah = qty * purchase_price * currency_rate
        profit_uah = total - cost_total_uah

        row = await self.fetchrow(
            """
            INSERT INTO sales (
                product_id, qty, sale_price, total_amount, customer_id, status,
                purchase_price_snapshot, purchase_currency_snapshot,
                currency_rate_snapshot, cost_total_uah, profit_uah
            )
            VALUES ($1,$2,$3,$4,$5,'completed',$6,$7,$8,$9,$10)
            RETURNING id
            """,
            product_id,
            qty,
            price,
            total,
            customer_id,
            purchase_price,
            purchase_currency,
            currency_rate,
            cost_total_uah,
            profit_uah,
        )

        return {
            "sale_id": row["id"],
            "total": total,
        }

    async def list_recent_sales(self, limit: int = 20):
        return await self.fetch(
            """
            SELECT
                s.id,
                s.qty,
                s.sale_price,
                s.total_amount,
                s.cost_total_uah,
                s.profit_uah,
                s.status,
                s.created_at,
                p.category,
                p.brand,
                p.model,
                c.name AS customer_name,
                c.phone AS customer_phone
            FROM sales s
            LEFT JOIN products p ON p.id = s.product_id
            LEFT JOIN customers c ON c.id = s.customer_id
            ORDER BY s.created_at DESC
            LIMIT $1
            """,
            limit
        )

    async def list_recent_purchases(self, limit: int = 20):
        return await self.fetch(
            """
            SELECT
                pu.id,
                pu.qty,
                pu.purchase_price,
                pu.total_amount,
                pu.created_at,
                p.category,
                p.brand,
                p.model
            FROM purchases pu
            LEFT JOIN products p ON p.id = pu.product_id
            ORDER BY pu.created_at DESC
            LIMIT $1
            """,
            limit
        )

    async def list_low_stock_products(self, limit_qty: int = 2):
        return await self.fetch(
            """
            SELECT id, category, brand, model, price, stock_qty
            FROM products
            WHERE stock_qty <= $1
            ORDER BY stock_qty ASC, id DESC
            """,
            limit_qty
        )

    async def get_sale_by_id(self, sale_id: int):
        return await self.fetchrow(
            """
            SELECT
                s.id,
                s.product_id,
                s.qty,
                s.sale_price,
                s.total_amount,
                s.status,
                s.created_at,
                p.category,
                p.brand,
                p.model,
                c.name AS customer_name,
                c.phone AS customer_phone
            FROM sales s
            LEFT JOIN products p ON p.id = s.product_id
            LEFT JOIN customers c ON c.id = s.customer_id
            WHERE s.id = $1
            """,
            sale_id
        )

    async def cancel_sale(self, sale_id: int):
        await self.execute(
            """
            UPDATE sales
            SET status = 'cancelled'
            WHERE id = $1
            """,
            sale_id
        )

    async def get_today_sales_stats(self):
        return await self.fetchrow(
            """
            SELECT
                COUNT(*) AS sales_count,
                COALESCE(SUM(qty), 0) AS total_qty,
                COALESCE(SUM(total_amount), 0) AS revenue
            FROM sales
            WHERE created_at::date = CURRENT_DATE
            """
        )

    async def get_today_purchases_stats(self):
        return await self.fetchrow(
            """
            SELECT
                COUNT(*) AS purchases_count,
                COALESCE(SUM(qty), 0) AS total_qty,
                COALESCE(SUM(total_amount), 0) AS total_cost
            FROM purchases
            WHERE created_at::date = CURRENT_DATE
            """
        )

    async def get_month_sales_stats(self):
        return await self.fetchrow(
            """
            SELECT
                COUNT(*) AS sales_count,
                COALESCE(SUM(qty), 0) AS total_qty,
                COALESCE(SUM(total_amount), 0) AS revenue
            FROM sales
            WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)
            """
        )

    async def get_month_purchases_stats(self):
        return await self.fetchrow(
            """
            SELECT
                COUNT(*) AS purchases_count,
                COALESCE(SUM(qty), 0) AS total_qty,
                COALESCE(SUM(total_amount), 0) AS total_cost
            FROM purchases
            WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)
            """
        )

    async def get_today_profit_stats(self):
        return await self.fetchrow(
            """
            SELECT
                COALESCE(SUM(total_amount), 0) AS revenue,
                COALESCE(SUM(cost_total_uah), 0) AS cost,
                COALESCE(SUM(profit_uah), 0) AS profit
            FROM sales
            WHERE created_at::date = CURRENT_DATE
              AND status = 'completed'
            """
        )

    async def get_month_profit_stats(self):
        return await self.fetchrow(
            """
            SELECT
                COALESCE(SUM(total_amount), 0) AS revenue,
                COALESCE(SUM(cost_total_uah), 0) AS cost,
                COALESCE(SUM(profit_uah), 0) AS profit
            FROM sales
            WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)
              AND status = 'completed'
            """
        )

    async def get_setting(self, key: str, default: str | None = None):
        row = await self.fetchrow(
            """
            SELECT value
            FROM settings
            WHERE key = $1
            """,
            key
        )
        return row["value"] if row else default


    async def set_setting(self, key: str, value: str):
        await self.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value
            """,
            key,
            value
        )


    async def toggle_setting_bool(self, key: str, default: str = "true"):
        current = await self.get_setting(key)
        if current is None:
            current = default

        new_value = "false" if current == "true" else "true"
        await self.set_setting(key, new_value)
        return new_value


    async def get_currency_rates(self):
        usd = await self.get_setting("usd_rate", "40")
        eur = await self.get_setting("eur_rate", "43")

        return {
            "USD": float(usd),
            "EUR": float(eur),
            "UAH": 1.0,
        }

    # ── site events ──────────────────────────────────────────
    async def add_site_event(self, event_type: str, product_id: int | None = None):
        await self.execute(
            """
            INSERT INTO site_events (event_type, product_id)
            VALUES ($1, $2)
            """,
            event_type,
            product_id,
        )

    async def get_site_analytics_today(self):
        return await self.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE event_type = 'product_view') AS views,
                COUNT(*) FILTER (WHERE event_type = 'add_to_cart')  AS cart_adds,
                COUNT(*) FILTER (WHERE event_type = 'site_order')   AS orders
            FROM site_events
            WHERE created_at::date = CURRENT_DATE
            """
        )

    # ── Banners CRUD ─────────────────────────────────────────
    async def list_active_banners(self):
        return await self.fetch(
            "SELECT * FROM banners WHERE is_active = TRUE ORDER BY sort_order ASC, id ASC"
        )

    async def list_all_banners(self):
        return await self.fetch(
            "SELECT * FROM banners ORDER BY sort_order ASC, id ASC"
        )

    async def get_banner(self, banner_id: int):
        return await self.fetchrow("SELECT * FROM banners WHERE id = $1", banner_id)

    async def create_banner(
        self,
        title: str = "",
        subtitle: str = "",
        button_text: str = "",
        button_link: str = "",
        image_url: str = "",
        sort_order: int = 100,
    ):
        return await self.fetchrow(
            """
            INSERT INTO banners
                (image_url, title, subtitle, button_text, button_link, sort_order, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
            RETURNING *
            """,
            image_url, title, subtitle, button_text, button_link, sort_order,
        )

    async def update_banner_field(self, banner_id: int, field: str, value):
        allowed = {"title", "subtitle", "button_text", "button_link", "image_url", "sort_order"}
        if field not in allowed:
            raise ValueError(f"Banner field {field!r} not allowed")
        await self.execute(
            f"UPDATE banners SET {field} = $1 WHERE id = $2",
            value, banner_id,
        )

    async def toggle_banner_active(self, banner_id: int):
        row = await self.fetchrow(
            "UPDATE banners SET is_active = NOT is_active WHERE id = $1 RETURNING is_active",
            banner_id,
        )
        return bool(row and row["is_active"])

    async def delete_banner(self, banner_id: int):
        await self.execute("DELETE FROM banners WHERE id = $1", banner_id)

    # ── SEO pages ──────────────────────────────────────────────────────────────
    async def get_seo_page(self, page_key: str) -> dict:
        """Return dict with all 4 SEO fields for a given page key (e.g. 'index')."""
        fields = ["meta_title", "meta_description", "h1", "seo_text"]
        result = {}
        for f in fields:
            result[f] = await self.get_setting(f"seo_{page_key}_{f}") or ""
        return result

    async def set_seo_page_field(self, page_key: str, field: str, value: str):
        allowed = {"meta_title", "meta_description", "h1", "seo_text"}
        if field not in allowed:
            raise ValueError(f"Invalid SEO field: {field}")
        await self.set_setting(f"seo_{page_key}_{field}", value)

    # ── SEO per site category ──────────────────────────────────────────────────
    async def get_category_seo(self, site_category_id: int):
        return await self.fetchrow(
            "SELECT * FROM seo_categories WHERE site_category_id = $1",
            site_category_id,
        )

    async def upsert_category_seo_field(self, site_category_id: int, field: str, value: str):
        allowed = {"meta_title", "meta_description", "h1", "seo_text"}
        if field not in allowed:
            raise ValueError(f"Invalid SEO field: {field}")
        await self.execute(
            f"""
            INSERT INTO seo_categories (site_category_id, {field}, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (site_category_id) DO UPDATE
            SET {field} = EXCLUDED.{field}, updated_at = NOW()
            """,
            site_category_id,
            value,
        )

    async def toggle_category_seo_indexable(self, site_category_id: int) -> bool:
        row = await self.fetchrow(
            """
            INSERT INTO seo_categories (site_category_id, indexable, updated_at)
            VALUES ($1, FALSE, NOW())
            ON CONFLICT (site_category_id) DO UPDATE
            SET indexable = NOT seo_categories.indexable, updated_at = NOW()
            RETURNING indexable
            """,
            site_category_id,
        )
        return bool(row and row["indexable"])

    # ── SEO per product ─────────────────────────────────────────────────────
    async def get_product_seo(self, product_id: int):
        return await self.fetchrow(
            "SELECT * FROM seo_products WHERE product_id = $1",
            product_id,
        )

    async def upsert_product_seo_field(self, product_id: int, field: str, value: str):
        allowed = {"meta_title", "meta_description", "h1", "seo_text"}
        if field not in allowed:
            raise ValueError(f"Invalid SEO field: {field}")
        await self.execute(
            f"""
            INSERT INTO seo_products (product_id, {field}, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (product_id) DO UPDATE
            SET {field} = EXCLUDED.{field}, updated_at = NOW()
            """,
            product_id,
            value,
        )

    async def toggle_product_seo_indexable(self, product_id: int) -> bool:
        row = await self.fetchrow(
            """
            INSERT INTO seo_products (product_id, indexable, updated_at)
            VALUES ($1, FALSE, NOW())
            ON CONFLICT (product_id) DO UPDATE
            SET indexable = NOT seo_products.indexable, updated_at = NOW()
            RETURNING indexable
            """,
            product_id,
        )
        return bool(row and row["indexable"])

    async def list_noindex_product_ids(self) -> set:
        rows = await self.fetch(
            "SELECT product_id FROM seo_products WHERE indexable = FALSE"
        )
        return {r["product_id"] for r in rows}

    async def get_top_site_products(self, limit: int = 10):
        return await self.fetch(
            """
            SELECT
                se.product_id,
                COALESCE(p.brand, '') || ' ' || COALESCE(p.model, '') AS product_name,
                COUNT(*) AS views
            FROM site_events se
            LEFT JOIN products p ON p.id = se.product_id
            WHERE se.event_type = 'product_view'
              AND se.created_at::date = CURRENT_DATE
              AND se.product_id IS NOT NULL
            GROUP BY se.product_id, product_name
            ORDER BY views DESC
            LIMIT $1
            """,
            limit,
        )

    async def create_warranty(self, sale_id: int, product_id: int, customer_id: int, warranty_months: int):
        await self.execute(
            """
            INSERT INTO warranties (sale_id, product_id, customer_id, warranty_months, end_date)
            VALUES ($1, $2, $3, $4, CURRENT_DATE + ($4 || ' months')::interval)
            """,
            sale_id,
            product_id,
            customer_id,
            warranty_months
        )

    async def search_warranties_by_phone(self, phone: str):
        return await self.fetch(
            """
            SELECT
                w.id,
                w.start_date,
                w.end_date,
                w.warranty_months,
                c.name AS customer_name,
                c.phone AS customer_phone,
                p.category,
                p.brand,
                p.model
            FROM warranties w
            LEFT JOIN customers c ON c.id = w.customer_id
            LEFT JOIN products p ON p.id = w.product_id
            WHERE c.phone LIKE $1
            ORDER BY w.created_at DESC
            LIMIT 20
            """,
            f"%{phone}%"
        )

    async def create_order(self, customer_id: int, product_id: int, qty: int, total_amount: float, comment: str | None = None):
        return await self.fetchrow(
            """
            INSERT INTO orders (customer_id, product_id, qty, total_amount, comment)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            customer_id,
            product_id,
            qty,
            total_amount,
            comment
        )


    async def list_orders(self, limit: int = 20):
        return await self.fetch(
            """
            SELECT
                o.id,
                o.qty,
                o.total_amount,
                o.status,
                o.comment,
                o.created_at,
                c.name AS customer_name,
                c.phone AS customer_phone,
                c.city AS customer_city,
                p.category,
                p.brand,
                p.model
            FROM orders o
            LEFT JOIN customers c ON c.id = o.customer_id
            LEFT JOIN products p ON p.id = o.product_id
            ORDER BY o.created_at DESC
            LIMIT $1
            """,
            limit
        )


    async def update_order_status(self, order_id: int, status: str):
        await self.execute(
            """
            UPDATE orders
            SET status = $2
            WHERE id = $1
            """,
            order_id,
            status
        )


    async def get_order_by_id(self, order_id: int):
        return await self.fetchrow(
            """
            SELECT id, status
            FROM orders
            WHERE id = $1
            """,
            order_id
        )

    async def get_order(self, order_id: int):
        return await self.fetchrow(
            """
            SELECT
                o.id,
                o.total_amount AS total_price,
                c.name,
                c.phone,
                c.city,
                (COALESCE(p.brand, '') || ' ' || COALESCE(p.model, '')) AS product_name
            FROM orders o
            LEFT JOIN customers c ON c.id = o.customer_id
            LEFT JOIN products p ON p.id = o.product_id
            WHERE o.id = $1
            """,
            order_id
        )

    async def get_order_full_by_id(self, order_id: int):
        return await self.fetchrow(
            """
            SELECT
                o.id,
                o.qty,
                o.total_amount,
                o.status,
                o.customer_id,
                o.product_id,
                p.price,
                p.stock_qty,
                p.brand,
                p.model
            FROM orders o
            LEFT JOIN products p ON p.id = o.product_id
            WHERE o.id = $1
            """,
            order_id
        )


    async def add_site_category(self, name_ru: str, name_uk: str, emoji: str = "📦", sort_order: int = 100):
        await self.execute(
            """
            INSERT INTO site_categories (name_ru, name_uk, emoji, sort_order)
            VALUES ($1, $2, $3, $4)
            """,
            name_ru, name_uk, emoji, sort_order
        )


    async def list_site_categories(self):
        return await self.fetch(
            """
            SELECT id, name_ru, name_uk, emoji, sort_order, is_active
            FROM site_categories
            ORDER BY sort_order ASC, id ASC
            """
        )


    async def list_active_site_categories(self):
        return await self.fetch(
            """
            SELECT id, name_ru, name_uk, emoji, sort_order
            FROM site_categories
            WHERE is_active = TRUE
            ORDER BY sort_order ASC, id ASC
            """
        )


    async def get_site_category_by_name(self, name_ru: str):
        return await self.fetchrow(
            """
            SELECT id
            FROM site_categories
            WHERE name_ru = $1
            """,
            name_ru
        )


    async def toggle_site_category(self, category_id: int):
        await self.execute(
            """
            UPDATE site_categories
            SET is_active = NOT is_active
            WHERE id = $1
            """,
            category_id
        )


    # ——— category attributes (foundation для auto-filters) ———
    # Дефолтные атрибуты по категориям. Колонки в БД: attr_key, label_ru,
    # label_uk, attr_type, unit, options_json, is_filter, sort_order.
    # options_json для select-атрибутов — массив {"value": ..., "ru": ..., "uk": ...}.
    DEFAULT_CATEGORY_ATTRIBUTES = {
        "boilers": [
            {
                "attr_key": "volume",
                "label_ru": "Объём", "label_uk": "Об'єм",
                "attr_type": "number", "unit": "л",
                "options_json": [],
                "is_filter": True, "sort_order": 10,
            },
            {
                "attr_key": "heater_type",
                "label_ru": "Тип ТЭНа", "label_uk": "Тип ТЕНу",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "dry", "ru": "Сухой", "uk": "Сухий"},
                    {"value": "wet", "ru": "Мокрый", "uk": "Мокрий"},
                ],
                "is_filter": True, "sort_order": 20,
            },
            {
                "attr_key": "tank_shape",
                "label_ru": "Форма бака", "label_uk": "Форма баку",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "cylindrical", "ru": "Цилиндрический", "uk": "Циліндричний"},
                    {"value": "flat",        "ru": "Плоский",       "uk": "Плоский"},
                    {"value": "cubic",       "ru": "Кубический",    "uk": "Кубічний"},
                ],
                "is_filter": True, "sort_order": 30,
            },
            {
                "attr_key": "installation",
                "label_ru": "Установка", "label_uk": "Установка",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "vertical",   "ru": "Вертикальная",   "uk": "Вертикальна"},
                    {"value": "horizontal", "ru": "Горизонтальная", "uk": "Горизонтальна"},
                    {"value": "universal",  "ru": "Универсальная",  "uk": "Універсальна"},
                ],
                "is_filter": True, "sort_order": 40,
            },
            {
                "attr_key": "power",
                "label_ru": "Мощность", "label_uk": "Потужність",
                "attr_type": "number", "unit": "Вт",
                "options_json": [],
                "is_filter": False, "sort_order": 50,
            },
        ],
        "air_conditioners": [
            {
                "attr_key": "room_area",
                "label_ru": "Площадь помещения", "label_uk": "Площа приміщення",
                "attr_type": "number", "unit": "м²",
                "options_json": [],
                "is_filter": True, "sort_order": 10,
            },
            {
                # legacy — kept for backward compat with existing products
                "attr_key": "inverter",
                "label_ru": "Инвертор", "label_uk": "Інвертор",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "yes", "ru": "Да", "uk": "Так"},
                    {"value": "no",  "ru": "Нет", "uk": "Ні"},
                ],
                "is_filter": False, "sort_order": 20,
            },
            {
                # legacy — kept for backward compat with existing products
                "attr_key": "wifi",
                "label_ru": "Wi-Fi", "label_uk": "Wi-Fi",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "yes", "ru": "Да", "uk": "Так"},
                    {"value": "no",  "ru": "Нет", "uk": "Ні"},
                ],
                "is_filter": False, "sort_order": 30,
            },
            {
                "attr_key": "compressor_type",
                "label_ru": "Тип компрессора", "label_uk": "Тип компресора",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "inverter",     "ru": "Инверторный", "uk": "Інверторний"},
                    {"value": "non_inverter", "ru": "Обычный",     "uk": "Звичайний"},
                ],
                "is_filter": True, "sort_order": 40,
            },
            {
                "attr_key": "freon",
                "label_ru": "Хладагент", "label_uk": "Фреон",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "r32",   "ru": "R32",   "uk": "R32"},
                    {"value": "r410a", "ru": "R410A", "uk": "R410A"},
                ],
                "is_filter": True, "sort_order": 50,
            },
            {
                # legacy — kept for backward compat with existing products
                "attr_key": "power",
                "label_ru": "Мощность", "label_uk": "Потужність",
                "attr_type": "number", "unit": "кВт",
                "options_json": [],
                "is_filter": False, "sort_order": 60,
            },
            {
                "attr_key": "energy_class",
                "label_ru": "Класс энергоэффективности", "label_uk": "Клас енергоефективності",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "a",               "ru": "A",    "uk": "A"},
                    {"value": "a_plus",          "ru": "A+",   "uk": "A+"},
                    {"value": "a_plus_plus",     "ru": "A++",  "uk": "A++"},
                    {"value": "a_plus_plus_plus","ru": "A+++", "uk": "A+++"},
                ],
                "is_filter": True, "sort_order": 70,
            },
            {
                "attr_key": "power_consumption",
                "label_ru": "Потребляемая мощность холод/тепло, Вт",
                "label_uk": "Споживана потужність холод/тепло, Вт",
                "attr_type": "text", "unit": None,
                "options_json": [],
                "is_filter": False, "sort_order": 80,
            },
            {
                "attr_key": "cooling_heating_capacity",
                "label_ru": "Производительность, кВт холод/тепло",
                "label_uk": "Продуктивність, кВт холод/тепло",
                "attr_type": "text", "unit": None,
                "options_json": [],
                "is_filter": False, "sort_order": 90,
            },
            {
                "attr_key": "indoor_outdoor_dimensions",
                "label_ru": "Размеры внутр./внешн. блока, мм",
                "label_uk": "Розміри внутр./зовн. блоку, мм",
                "attr_type": "text", "unit": None,
                "options_json": [],
                "is_filter": False, "sort_order": 100,
            },
            {
                "attr_key": "indoor_noise_level",
                "label_ru": "Уровень шума внутреннего блока, дБ",
                "label_uk": "Рівень шуму внутрішнього блоку, дБ",
                "attr_type": "number", "unit": "дБ",
                "options_json": [],
                "is_filter": False, "sort_order": 110,
            },
        ],
        "refrigerators": [
            {
                "attr_key": "no_frost",
                "label_ru": "No Frost", "label_uk": "No Frost",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "yes", "ru": "Да",  "uk": "Так"},
                    {"value": "no",  "ru": "Нет", "uk": "Ні"},
                ],
                "is_filter": True, "sort_order": 10,
            },
            {
                "attr_key": "volume",
                "label_ru": "Объём", "label_uk": "Об'єм",
                "attr_type": "number", "unit": "л",
                "options_json": [],
                "is_filter": True, "sort_order": 20,
            },
            {
                "attr_key": "height",
                "label_ru": "Высота", "label_uk": "Висота",
                "attr_type": "number", "unit": "см",
                "options_json": [],
                "is_filter": True, "sort_order": 30,
            },
            {
                "attr_key": "freezer_position",
                "label_ru": "Морозильная камера", "label_uk": "Морозильна камера",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "top",           "ru": "Сверху",      "uk": "Зверху"},
                    {"value": "bottom",        "ru": "Снизу",       "uk": "Знизу"},
                    {"value": "side_by_side",  "ru": "Side-by-Side","uk": "Side-by-Side"},
                ],
                "is_filter": True, "sort_order": 40,
            },
            {
                "attr_key": "doors",
                "label_ru": "Количество дверей", "label_uk": "Кількість дверей",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "1", "ru": "1", "uk": "1"},
                    {"value": "2", "ru": "2", "uk": "2"},
                    {"value": "3", "ru": "3", "uk": "3"},
                    {"value": "4", "ru": "4", "uk": "4"},
                ],
                "is_filter": True, "sort_order": 50,
            },
            {
                "attr_key": "energy_class",
                "label_ru": "Класс энергопотребления", "label_uk": "Клас енергоспоживання",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "a",              "ru": "A",    "uk": "A"},
                    {"value": "a_plus",         "ru": "A+",   "uk": "A+"},
                    {"value": "a_plus_plus",    "ru": "A++",  "uk": "A++"},
                    {"value": "a_plus_plus_plus","ru": "A+++","uk": "A+++"},
                ],
                "is_filter": True, "sort_order": 60,
            },
        ],
        "washing_machines": [
            {
                "attr_key": "load_capacity",
                "label_ru": "Загрузка", "label_uk": "Завантаження",
                "attr_type": "number", "unit": "кг",
                "options_json": [],
                "is_filter": True, "sort_order": 10,
            },
            {
                "attr_key": "spin_speed",
                "label_ru": "Отжим", "label_uk": "Віджим",
                "attr_type": "number", "unit": "об/хв",
                "options_json": [],
                "is_filter": True, "sort_order": 20,
            },
            {
                "attr_key": "depth",
                "label_ru": "Глубина", "label_uk": "Глибина",
                "attr_type": "number", "unit": "см",
                "options_json": [],
                "is_filter": True, "sort_order": 30,
            },
            {
                "attr_key": "dryer",
                "label_ru": "Сушка", "label_uk": "Сушіння",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "yes", "ru": "Да",  "uk": "Так"},
                    {"value": "no",  "ru": "Нет", "uk": "Ні"},
                ],
                "is_filter": True, "sort_order": 40,
            },
            {
                "attr_key": "loading_type",
                "label_ru": "Тип загрузки", "label_uk": "Тип завантаження",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "front", "ru": "Фронтальная", "uk": "Фронтальне"},
                    {"value": "top",   "ru": "Вертикальная","uk": "Вертикальне"},
                ],
                "is_filter": True, "sort_order": 50,
            },
            {
                "attr_key": "energy_class",
                "label_ru": "Класс энергопотребления", "label_uk": "Клас енергоспоживання",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "a",              "ru": "A",    "uk": "A"},
                    {"value": "a_plus",         "ru": "A+",   "uk": "A+"},
                    {"value": "a_plus_plus",    "ru": "A++",  "uk": "A++"},
                    {"value": "a_plus_plus_plus","ru": "A+++","uk": "A+++"},
                ],
                "is_filter": True, "sort_order": 60,
            },
        ],
        "hoods": [
            {
                "attr_key": "width",
                "label_ru": "Ширина", "label_uk": "Ширина",
                "attr_type": "number", "unit": "см",
                "options_json": [],
                "is_filter": True, "sort_order": 10,
            },
            {
                "attr_key": "productivity",
                "label_ru": "Производительность", "label_uk": "Продуктивність",
                "attr_type": "number", "unit": "м³/год",
                "options_json": [],
                "is_filter": True, "sort_order": 20,
            },
            {
                "attr_key": "control_type",
                "label_ru": "Управление", "label_uk": "Керування",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "mechanical", "ru": "Механическое", "uk": "Механічне"},
                    {"value": "electronic", "ru": "Электронное",  "uk": "Електронне"},
                    {"value": "touch",      "ru": "Сенсорное",    "uk": "Сенсорне"},
                ],
                "is_filter": True, "sort_order": 30,
            },
            {
                "attr_key": "installation_type",
                "label_ru": "Тип монтажа", "label_uk": "Тип монтажу",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "wall",       "ru": "Настенная",   "uk": "Настінна"},
                    {"value": "built_in",   "ru": "Встраиваемая","uk": "Вбудована"},
                    {"value": "island",     "ru": "Островная",   "uk": "Острівна"},
                    {"value": "telescopic", "ru": "Телескопическая","uk": "Телескопічна"},
                ],
                "is_filter": True, "sort_order": 40,
            },
            {
                "attr_key": "noise_level",
                "label_ru": "Уровень шума", "label_uk": "Рівень шуму",
                "attr_type": "number", "unit": "дБ",
                "options_json": [],
                "is_filter": True, "sort_order": 50,
            },
            {
                "attr_key": "color",
                "label_ru": "Цвет", "label_uk": "Колір",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "white",           "ru": "Белый",            "uk": "Білий"},
                    {"value": "black",           "ru": "Чёрный",           "uk": "Чорний"},
                    {"value": "stainless_steel", "ru": "Нержавеющая сталь","uk": "Нержавіюча сталь"},
                    {"value": "gray",            "ru": "Серый",            "uk": "Сірий"},
                ],
                "is_filter": True, "sort_order": 60,
            },
        ],
        "microwaves": [
            {
                "attr_key": "volume",
                "label_ru": "Объем", "label_uk": "Обʼєм",
                "attr_type": "number", "unit": "л",
                "options_json": [],
                "is_filter": True, "sort_order": 10,
            },
            {
                "attr_key": "power",
                "label_ru": "Мощность", "label_uk": "Потужність",
                "attr_type": "number", "unit": "Вт",
                "options_json": [],
                "is_filter": True, "sort_order": 20,
            },
            {
                "attr_key": "control_type",
                "label_ru": "Управление", "label_uk": "Керування",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "mechanical", "ru": "Механическое", "uk": "Механічне"},
                    {"value": "electronic", "ru": "Электронное",  "uk": "Електронне"},
                    {"value": "touch",      "ru": "Сенсорное",    "uk": "Сенсорне"},
                ],
                "is_filter": True, "sort_order": 30,
            },
            {
                "attr_key": "grill",
                "label_ru": "Гриль", "label_uk": "Гриль",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "yes", "ru": "Да",  "uk": "Так"},
                    {"value": "no",  "ru": "Нет", "uk": "Ні"},
                ],
                "is_filter": True, "sort_order": 40,
            },
            {
                "attr_key": "convection",
                "label_ru": "Конвекция", "label_uk": "Конвекція",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "yes", "ru": "Да",  "uk": "Так"},
                    {"value": "no",  "ru": "Нет", "uk": "Ні"},
                ],
                "is_filter": True, "sort_order": 50,
            },
            {
                "attr_key": "installation_type",
                "label_ru": "Тип", "label_uk": "Тип",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "solo",     "ru": "Соло",          "uk": "Соло"},
                    {"value": "built_in", "ru": "Встраиваемая",  "uk": "Вбудована"},
                ],
                "is_filter": True, "sort_order": 60,
            },
            {
                "attr_key": "color",
                "label_ru": "Цвет", "label_uk": "Колір",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "white",           "ru": "Белый",            "uk": "Білий"},
                    {"value": "black",           "ru": "Чёрный",           "uk": "Чорний"},
                    {"value": "stainless_steel", "ru": "Нержавеющая сталь","uk": "Нержавіюча сталь"},
                    {"value": "gray",            "ru": "Серый",            "uk": "Сірий"},
                ],
                "is_filter": True, "sort_order": 70,
            },
        ],
        "gas_stoves": [
            {
                "attr_key": "stove_type",
                "label_ru": "Тип плиты", "label_uk": "Тип плити",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "gas",       "ru": "Газовая",       "uk": "Газова"},
                    {"value": "electric",  "ru": "Электрическая", "uk": "Електрична"},
                    {"value": "combined",  "ru": "Комбинированная","uk": "Комбінована"},
                    {"value": "induction", "ru": "Индукционная",  "uk": "Індукційна"},
                ],
                "is_filter": True, "sort_order": 10,
            },
            {
                "attr_key": "burners_count",
                "label_ru": "Количество конфорок", "label_uk": "Кількість конфорок",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "2", "ru": "2", "uk": "2"},
                    {"value": "3", "ru": "3", "uk": "3"},
                    {"value": "4", "ru": "4", "uk": "4"},
                    {"value": "5", "ru": "5", "uk": "5"},
                ],
                "is_filter": True, "sort_order": 20,
            },
            {
                "attr_key": "oven_type",
                "label_ru": "Тип духовки", "label_uk": "Тип духовки",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "gas",      "ru": "Газовая",       "uk": "Газова"},
                    {"value": "electric", "ru": "Электрическая", "uk": "Електрична"},
                    {"value": "none",     "ru": "Без духовки",   "uk": "Без духовки"},
                ],
                "is_filter": True, "sort_order": 30,
            },
            {
                "attr_key": "width",
                "label_ru": "Ширина", "label_uk": "Ширина",
                "attr_type": "number", "unit": "см",
                "options_json": [],
                "is_filter": True, "sort_order": 40,
            },
            {
                "attr_key": "control_type",
                "label_ru": "Управление", "label_uk": "Керування",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "mechanical", "ru": "Механическое", "uk": "Механічне"},
                    {"value": "electronic", "ru": "Электронное",  "uk": "Електронне"},
                    {"value": "touch",      "ru": "Сенсорное",    "uk": "Сенсорне"},
                ],
                "is_filter": True, "sort_order": 50,
            },
            {
                "attr_key": "ignition",
                "label_ru": "Электроподжиг", "label_uk": "Електропідпал",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "yes", "ru": "Да",  "uk": "Так"},
                    {"value": "no",  "ru": "Нет", "uk": "Ні"},
                ],
                "is_filter": True, "sort_order": 60,
            },
            {
                "attr_key": "color",
                "label_ru": "Цвет", "label_uk": "Колір",
                "attr_type": "select", "unit": None,
                "options_json": [
                    {"value": "white",           "ru": "Белый",            "uk": "Білий"},
                    {"value": "black",           "ru": "Чёрный",           "uk": "Чорний"},
                    {"value": "stainless_steel", "ru": "Нержавеющая сталь","uk": "Нержавіюча сталь"},
                    {"value": "gray",            "ru": "Серый",            "uk": "Сірий"},
                ],
                "is_filter": True, "sort_order": 70,
            },
        ],
    }

    # ── Legacy → canonical mapping для specifications_json (для миграции) ──
    # Маленькая копия SPEC_VALUE_MAP из app/main.py — чтобы не было
    # circular-import. Если добавляешь новое поле в main.py — продублируй сюда.
    _SPEC_LEGACY_TO_CANONICAL = {
        "tank_shape": {
            "циліндричний": "cylindrical",
            "цилиндрический": "cylindrical",
            "плоский": "flat",
            "кубічний": "cubic",
            "кубический": "cubic",
        },
        "installation": {
            "вертикальна": "vertical",
            "вертикальная": "vertical",
            "горизонтальна": "horizontal",
            "горизонтальная": "horizontal",
            "універсальна": "universal",
            "универсальная": "universal",
        },
        "ten_type": {
            "сухий": "dry",
            "сухой": "dry",
            "мокрий": "wet",
            "мокрый": "wet",
        },
        "heater_type": {
            "сухий": "dry",
            "сухой": "dry",
            "мокрий": "wet",
            "мокрый": "wet",
        },
    }
    _SPEC_NUMBER_KEYS = {"volume", "power"}

    @classmethod
    def _normalize_spec_value(cls, key: str, value):
        """Привести значение к каноническому виду (для миграции)."""
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        if key in cls._SPEC_NUMBER_KEYS:
            import re
            m = re.search(r"-?\d+(?:[.,]\d+)?", s)
            if not m:
                return s
            num = m.group(0).replace(",", ".")
            try:
                f = float(num)
                return str(int(f) if f.is_integer() else f)
            except ValueError:
                return s
        mp = cls._SPEC_LEGACY_TO_CANONICAL.get(key)
        if mp:
            canon = mp.get(s.lower())
            if canon:
                return canon
        return s

    async def _migrate_normalize_specifications(self):
        """Одноразовая нормализация specifications_json у всех товаров.

        Идемпотентна: помечает завершение через app_settings.
        """
        import json

        # v2: + переименование ключа ten_type → heater_type
        flag_key = "migration_normalize_specs_v2"
        done = await self.get_setting(flag_key)
        if done == "1":
            return

        # Алиасы ключей: legacy_key → canonical_key
        spec_key_aliases = {"ten_type": "heater_type"}

        rows = await self.fetch(
            """
            SELECT id, specifications_json
            FROM products
            WHERE specifications_json IS NOT NULL
              AND specifications_json::text <> '{}'
            """
        )
        updated = 0
        for r in rows:
            raw = r["specifications_json"]
            if isinstance(raw, str):
                try:
                    specs = json.loads(raw)
                except (ValueError, TypeError):
                    continue
            else:
                specs = raw
            if not isinstance(specs, dict) or not specs:
                continue

            changed = False
            new_specs = {}
            for k, v in specs.items():
                # Переименование ключа (ten_type → heater_type) с приоритетом
                # уже существующего нового ключа.
                canon_key = spec_key_aliases.get(k, k)
                if canon_key != k:
                    changed = True
                if canon_key in new_specs and new_specs[canon_key]:
                    # уже есть значение под новым ключом — не перетираем
                    continue
                nv = self._normalize_spec_value(canon_key, v)
                if nv is None:
                    if v not in (None, "", "-"):
                        changed = True
                    continue
                if nv != v:
                    changed = True
                new_specs[canon_key] = nv

            if changed:
                await self.execute(
                    "UPDATE products SET specifications_json = $2::jsonb WHERE id = $1",
                    r["id"], json.dumps(new_specs, ensure_ascii=False),
                )
                updated += 1

        await self.set_setting(flag_key, "1")
        print(f"[migrate] normalize specifications v2: scanned={len(rows)} updated={updated}")

    async def _migrate_ac_attributes_v2(self):
        """Идемпотентно обновляет category_attributes для air_conditioners.

        - inverter / wifi / power → is_filter = FALSE (legacy, данные не трогаем).
        - compressor_type: опция non_inverter → «Звичайний» / «Обычный».
        - freon: label_uk → «Фреон».
        - Новые поля (power_consumption, cooling_heating_capacity,
          indoor_outdoor_dimensions, indoor_noise_level) добавит сидер
          через ON CONFLICT DO NOTHING.

        Безопасно вызывать многократно (только UPDATE-ы).
        """
        import json
        # 1. Снять is_filter с устаревших полей
        await self.execute(
            "UPDATE category_attributes SET is_filter = FALSE "
            "WHERE category_key = 'air_conditioners' "
            "AND attr_key IN ('inverter', 'wifi', 'power')",
        )
        # 2. Обновить метку compressor_type (non_inverter → Звичайний)
        new_ct_opts = json.dumps([
            {"value": "inverter",     "ru": "Инверторный", "uk": "Інверторний"},
            {"value": "non_inverter", "ru": "Обычный",     "uk": "Звичайний"},
        ])
        await self.execute(
            "UPDATE category_attributes SET options_json = $1::jsonb "
            "WHERE category_key = 'air_conditioners' AND attr_key = 'compressor_type'",
            new_ct_opts,
        )
        # 3. Обновить label_uk для freon
        await self.execute(
            "UPDATE category_attributes SET label_uk = 'Фреон' "
            "WHERE category_key = 'air_conditioners' AND attr_key = 'freon'",
        )
        # 4. Гарантировать, что новые text-поля НЕ являются фильтрами,
        #    даже если кто-то их вручную пометил.
        await self.execute(
            "UPDATE category_attributes SET is_filter = FALSE "
            "WHERE category_key = 'air_conditioners' "
            "AND attr_key IN ('power_consumption', 'cooling_heating_capacity', "
            "                 'indoor_outdoor_dimensions', 'indoor_noise_level')",
        )
        print("[migrate] ac_attributes_v2: applied")

    async def _seed_default_category_attributes(self):
        """Идемпотентный сидер. Вставляет только отсутствующие записи."""
        import json
        for cat_key, attrs in self.DEFAULT_CATEGORY_ATTRIBUTES.items():
            for a in attrs:
                await self.execute(
                    """
                    INSERT INTO category_attributes
                        (category_key, attr_key, label_ru, label_uk,
                         attr_type, unit, options_json, is_filter, sort_order)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                    ON CONFLICT (category_key, attr_key) DO NOTHING
                    """,
                    cat_key,
                    a["attr_key"],
                    a["label_ru"],
                    a.get("label_uk"),
                    a["attr_type"],
                    a.get("unit"),
                    json.dumps(a.get("options_json") or []),
                    bool(a.get("is_filter", False)),
                    int(a.get("sort_order", 0)),
                )

    async def get_category_attributes(self, category_key: str, only_filterable: bool = False):
        """Возвращает список атрибутов категории, отсортированный по sort_order.

        Каждая запись — dict с ключами:
            attribute_key, name_ru, name_ua, type, unit,
            options (распарсенный options_json), is_filterable, sort_order.

        Имена ключей возвращаемого dict совместимы со спецификацией
        этапа 2 (name_ua/attribute_key/type/is_filterable), независимо
        от внутренних имён колонок.
        """
        import json
        k = (category_key or "").strip().lower()
        if not k:
            return []
        if only_filterable:
            rows = await self.fetch(
                """
                SELECT attr_key, label_ru, label_uk, attr_type, unit,
                       options_json, is_filter, sort_order
                FROM category_attributes
                WHERE category_key = $1 AND is_filter = TRUE
                ORDER BY sort_order ASC, id ASC
                """,
                k,
            )
        else:
            rows = await self.fetch(
                """
                SELECT attr_key, label_ru, label_uk, attr_type, unit,
                       options_json, is_filter, sort_order
                FROM category_attributes
                WHERE category_key = $1
                ORDER BY sort_order ASC, id ASC
                """,
                k,
            )

        result = []
        for r in rows:
            raw = r["options_json"]
            if isinstance(raw, str):
                try:
                    opts = json.loads(raw)
                except Exception:
                    opts = []
            else:
                opts = raw or []
            result.append({
                "attribute_key": r["attr_key"],
                "name_ru": r["label_ru"],
                "name_ua": r["label_uk"] or r["label_ru"],
                "type": r["attr_type"],
                "unit": r["unit"],
                "options": opts,
                "is_filterable": bool(r["is_filter"]),
                "sort_order": int(r["sort_order"]),
            })
        return result


    # ——— site brands ———
    async def list_site_brands(self):
        return await self.fetch(
            """
            SELECT id, name, sort_order, is_active
            FROM site_brands
            ORDER BY sort_order ASC, LOWER(name) ASC, id ASC
            """
        )

    async def list_active_site_brands(self):
        return await self.fetch(
            """
            SELECT id, name, sort_order
            FROM site_brands
            WHERE is_active = TRUE
            ORDER BY sort_order ASC, LOWER(name) ASC, id ASC
            """
        )

    async def get_site_brand_by_name(self, name: str):
        return await self.fetchrow(
            """
            SELECT id, name, is_active
            FROM site_brands
            WHERE LOWER(TRIM(name)) = LOWER(TRIM($1))
            """,
            name
        )

    async def add_site_brand(self, name: str, sort_order: int = 100) -> dict | None:
        """Создаёт бренд (case-insensitive).

        Возвращает dict бренда. Поле ``_status``:
        - ``"created"`` — бренд добавлен;
        - ``"active"`` — уже существовал и был активен;
        - ``"hidden"`` — существовал, но скрыт (is_active=FALSE). НЕ активируем
          автоматически — это решает пользователь явной кнопкой.
        """
        name = (name or "").strip()
        if not name:
            return None
        existing = await self.get_site_brand_by_name(name)
        if existing:
            result = dict(existing)
            result["_status"] = "active" if existing["is_active"] else "hidden"
            return result
        await self.execute(
            """
            INSERT INTO site_brands (name, sort_order, is_active)
            VALUES ($1, $2, TRUE)
            ON CONFLICT DO NOTHING
            """,
            name, sort_order
        )
        row = await self.get_site_brand_by_name(name)
        if row:
            result = dict(row)
            result["_status"] = "created"
            return result
        return None

    async def activate_site_brand(self, brand_id: int):
        await self.execute(
            "UPDATE site_brands SET is_active = TRUE WHERE id = $1",
            brand_id,
        )

    async def hide_site_brands_by_names(self, names: list[str]) -> int:
        """Soft-hide брендов по списку имён (case-insensitive)."""
        if not names:
            return 0
        norm = [n.strip().lower() for n in names if n and n.strip()]
        if not norm:
            return 0
        result = await self.execute(
            """
            UPDATE site_brands
            SET is_active = FALSE
            WHERE LOWER(TRIM(name)) = ANY($1::text[])
              AND is_active = TRUE
            """,
            norm,
        )
        # asyncpg execute returns string like "UPDATE 3"
        try:
            return int((result or "UPDATE 0").split()[-1])
        except Exception:
            return 0

    async def list_brands_from_active_products(self) -> list[str]:
        """Уникальные бренды, которые реально встречаются в активных товарах сайта.

        Без пустых/None/'-'. Сортировка алфавитная (case-insensitive).
        """
        rows = await self.fetch(
            """
            SELECT DISTINCT TRIM(brand) AS brand
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND deleted_at IS NULL
              AND COALESCE(availability_status, 'in_stock') != 'hidden'
              AND brand IS NOT NULL
              AND LOWER(COALESCE(NULLIF(TRIM(brand), ''), '-')) NOT IN ('-', 'none')
            ORDER BY TRIM(brand)
            """
        )
        return [r["brand"] for r in rows if (r["brand"] or "").strip()]

    async def sync_site_brands_from_products(self) -> dict:
        """Подтягивает в site_brands бренды, реально используемые в товарах.

        - Бренда нет в справочнике → INSERT с is_active=TRUE.
        - Бренд есть и активен → пропуск.
        - Бренд есть, но скрыт → авто-активация (раз он реально используется,
          его нельзя оставлять скрытым).
        Сравнение case-insensitive по LOWER(TRIM(name)).
        Возвращает {'added': int, 'reactivated': int, 'skipped': int}.
        """
        used = await self.list_brands_from_active_products()
        if not used:
            return {"added": 0, "reactivated": 0, "skipped": 0}

        existing_rows = await self.fetch(
            "SELECT id, LOWER(TRIM(name)) AS lname, is_active FROM site_brands"
        )
        existing: dict[str, dict] = {
            r["lname"]: {"id": r["id"], "is_active": r["is_active"]}
            for r in existing_rows
        }

        added = 0
        reactivated = 0
        skipped = 0
        for name in used:
            key = name.strip().lower()
            if not key:
                continue
            row = existing.get(key)
            if row is None:
                await self.execute(
                    """
                    INSERT INTO site_brands (name, sort_order, is_active)
                    VALUES ($1, 100, TRUE)
                    ON CONFLICT DO NOTHING
                    """,
                    name.strip(),
                )
                existing[key] = {"id": None, "is_active": True}
                added += 1
            elif not row["is_active"]:
                await self.execute(
                    "UPDATE site_brands SET is_active = TRUE WHERE id = $1",
                    row["id"],
                )
                row["is_active"] = True
                reactivated += 1
            else:
                skipped += 1
        return {"added": added, "reactivated": reactivated, "skipped": skipped}

    async def list_brands_for_selection(self) -> list[str]:
        """Бренды для выбора в боте при добавлении товара.

        Объединение:
          1) site_brands.is_active = TRUE
          2) бренды из активных товаров (включая случаи, когда они помечены
             is_active=FALSE в site_brands — реально используемый бренд
             всегда показываем).
        Сортировка алфавитная (case-insensitive). Без дублей.
        """
        active_rows = await self.fetch(
            """
            SELECT TRIM(name) AS name, LOWER(TRIM(name)) AS lname
            FROM site_brands
            WHERE is_active = TRUE
              AND TRIM(name) <> ''
            """
        )

        result: dict[str, str] = {}
        for r in active_rows:
            name = (r["name"] or "").strip()
            if name:
                result[r["lname"]] = name

        # Бренды из активных товаров включаем безусловно — даже если
        # они скрыты в site_brands.
        used = await self.list_brands_from_active_products()
        for name in used:
            key = name.strip().lower()
            if not key or key in result:
                continue
            result[key] = name.strip()

        return sorted(result.values(), key=lambda s: s.lower())

    async def count_active_products_by_brand(self, brand_name: str) -> int:
        """Сколько активных товаров используют этот бренд (case-insensitive)."""
        key = (brand_name or "").strip().lower()
        if not key:
            return 0
        row = await self.fetchrow(
            """
            SELECT COUNT(*) AS cnt
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND deleted_at IS NULL
              AND COALESCE(availability_status, 'in_stock') != 'hidden'
              AND LOWER(TRIM(COALESCE(brand, ''))) = $1
            """,
            key,
        )
        return int(row["cnt"]) if row else 0

    async def toggle_site_brand(self, brand_id: int):
        await self.execute(
            """
            UPDATE site_brands
            SET is_active = NOT is_active
            WHERE id = $1
            """,
            brand_id
        )

    async def delete_site_brand(self, brand_id: int):
        await self.execute("DELETE FROM site_brands WHERE id = $1", brand_id)

    async def deactivate_all_site_brands(self):
        await self.execute("UPDATE site_brands SET is_active = FALSE")

    async def delete_all_site_brands(self):
        await self.execute("DELETE FROM site_brands")


db = Database(DATABASE_URL)
