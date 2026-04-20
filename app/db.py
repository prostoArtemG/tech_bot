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

	async def add_product(self, name: str, price: float):
		await self.execute(
			"""
			INSERT INTO products (name, price)
			VALUES ($1, $2)
			""",
			name,
			price
		)

	async def list_products(self):
		return await self.fetch(
			"""
			SELECT id, name, price
			FROM products
			ORDER BY id DESC
			"""
		)


db = Database(DATABASE_URL)

