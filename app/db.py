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
                purchase_price, purchase_currency, sku, warranty_months
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

    async def get_product_by_id(self, product_id: int):
        return await self.fetchrow(
            """
            SELECT id, category, brand, model, price, stock_qty
            FROM products
            WHERE id = $1
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
            SELECT id, telegram_id, full_name, role
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
        total = qty * price

        await self.execute(
            """
            INSERT INTO sales (product_id, qty, sale_price, total_amount, customer_id, status)
            VALUES ($1, $2, $3, $4, $5, 'completed')
            """,
            product_id,
            qty,
            price,
            total,
            customer_id
        )

        return total

    async def list_recent_sales(self, limit: int = 20):
        return await self.fetch(
            """
            SELECT
                s.id,
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
            ORDER BY s.created_at DESC
            LIMIT $1
            """,
            limit
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
                COALESCE((SELECT SUM(total_amount) FROM sales WHERE created_at::date = CURRENT_DATE), 0) AS revenue,
                COALESCE((SELECT SUM(total_amount) FROM purchases WHERE created_at::date = CURRENT_DATE), 0) AS cost
            """
        )

    async def get_month_profit_stats(self):
        return await self.fetchrow(
            """
            SELECT
                COALESCE((
                    SELECT SUM(total_amount)
                    FROM sales
                    WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)
                ), 0) AS revenue,
                COALESCE((
                    SELECT SUM(total_amount)
                    FROM purchases
                    WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)
                ), 0) AS cost
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


db = Database(DATABASE_URL)
