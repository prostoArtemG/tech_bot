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
        ADD COLUMN IF NOT EXISTS availability_status TEXT NOT NULL DEFAULT 'in_stock';
        """)

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
        CREATE TABLE IF NOT EXISTS product_images (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
            image_url TEXT NOT NULL,
            sort_order INTEGER DEFAULT 100
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
    ):
        name = f"{brand} {model}".strip()

        await self.execute(
            """
            INSERT INTO products (
                name, category, brand, model, price,
                purchase_price, purchase_currency, sku, warranty_months, stock_qty
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,0)
            """,
            name,
            category,
            brand,
            model,
            price,
            purchase_price,
            purchase_currency,
            sku,
            warranty_months,
        )

    async def list_products(self):
        return await self.fetch(
            """
            SELECT
                id, category, brand, model, price, stock_qty,
                purchase_price, purchase_currency, sku, warranty_months,
                photo_url, description
            FROM products
            ORDER BY id DESC
            """
        )

    async def search_products(self, query: str):
        return await self.fetch(
            """
            SELECT id, category, brand, model, price, stock_qty
            FROM products
            WHERE LOWER(COALESCE(brand, '')) LIKE LOWER($1)
               OR LOWER(COALESCE(model, '')) LIKE LOWER($1)
               OR LOWER(COALESCE(category, '')) LIKE LOWER($1)
            ORDER BY id DESC
            LIMIT 10
            """,
            f"%{query}%"
        )

    async def search_site_products(self, query: str):
        return await self.fetch(
            """
            SELECT
                id, category, brand, model, price, stock_qty,
                warranty_months, photo_url, description, availability_status
            FROM products
            WHERE COALESCE(availability_status, 'in_stock') != 'hidden'
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
                photo_url, description
            FROM products
            WHERE id = $1
            """,
            product_id
        )

    async def get_product_images(self, product_id: int):
        return await self.fetch(
            """
            SELECT image_url
            FROM product_images
            WHERE product_id = $1
            ORDER BY sort_order ASC, id ASC
            """,
            product_id
        )

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
            SELECT id, telegram_id, full_name, role, language
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
            SELECT id, telegram_id, full_name, role
            FROM users
            ORDER BY id DESC
            """
        )

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


    async def get_currency_rates(self):
        usd = await self.get_setting("usd_rate", "40")
        eur = await self.get_setting("eur_rate", "43")

        return {
            "USD": float(usd),
            "EUR": float(eur),
            "UAH": 1.0,
        }

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


db = Database(DATABASE_URL)
