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
        CREATE TABLE IF NOT EXISTS purchases (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            purchase_price NUMERIC(12,2) NOT NULL,
            total_amount NUMERIC(12,2) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)

    async def add_product(self, category: str, brand: str, model: str, price: float):
        name = f"{brand} {model}".strip()

        await self.execute(
            """
            INSERT INTO products (name, category, brand, model, price, stock_qty)
            VALUES ($1, $2, $3, $4, $5, 0)
            """,
            name,
            category,
            brand,
            model,
            price
        )

    async def list_products(self):
        return await self.fetch(
            """
            SELECT id, category, brand, model, price, stock_qty
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
            INSERT INTO sales (product_id, qty, sale_price, total_amount, customer_id)
            VALUES ($1, $2, $3, $4, $5)
            """,
            product_id,
            qty,
            price,
            total,
            customer_id
        )

        return total

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


db = Database(DATABASE_URL)
