# db.py
import os, asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, InterfaceError
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,       # проверка соединения перед использованием
    pool_recycle=1800,        # обновлять каждые 30 мин
    pool_size=5,
    max_overflow=10,
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()

async def get_session():
    retries = 3
    for attempt in range(retries):
        try:
            async with AsyncSessionLocal() as session:
                yield session
            break
        except (OperationalError, InterfaceError) as e:
            print(f"⚠️ Потеря соединения с БД (попытка {attempt+1}/{retries}): {e}")
            await asyncio.sleep(2)
    else:
        raise RuntimeError("❌ Не удалось установить соединение с БД после нескольких попыток")

async def test_connection():
    """Проверяет соединение при старте"""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        print("✅ Подключение к БД успешно")
