from sqlalchemy import text
from connection import engine


with engine.connect() as conn:
    print("⚠️ Удаляем все таблицы через CASCADE...")
    conn.execute(text("DROP SCHEMA public CASCADE;"))
    conn.execute(text("CREATE SCHEMA public;"))
    conn.commit()  # <--- важно!
    print("✅ Схема public очищена")
