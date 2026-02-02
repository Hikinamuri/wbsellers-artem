# create_db.py
import asyncio
import sys
import os
from sqlalchemy import text

# Добавляем текущую директорию в PYTHONPATH
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from db import engine, Base
import models

async def init_db():
    try:
        async with engine.begin() as conn:
            print(f"🔗 Подключаемся к базе: {engine.url}")
            
            # Проверяем подключение (используем text() для SQL)
            await conn.execute(text("SELECT 1"))
            print("✅ Подключение к БД успешно")
            
            # Создаем таблицы
            await conn.run_sync(Base.metadata.create_all)
            print("✅ Таблицы созданы!")
            
            # Выводим список созданных таблиц
            result = await conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public'
            """))
            tables = result.fetchall()
            print(f"📊 Создано таблиц: {len(tables)}")
            for table in tables:
                print(f"  - {table[0]}")
                
    except Exception as e:
        print(f"❌ Ошибка: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(init_db())