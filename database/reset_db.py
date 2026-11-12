"""Drop and recreate schema public in a disposable local PostgreSQL test database.

Refuses by default (exit 2). It acts only when DATABASE_URL from the process
environment (no .env) is PostgreSQL on 127.0.0.1, localhost or ::1, the database
name starts with test_ or ends with _test, there are no "?" parameters (libpq
could redirect the connection), and LUNARIA_RESET_DB_CONFIRM equals that name.
All checks run before any connection is opened; the address is never printed.

    DATABASE_URL=... LUNARIA_RESET_DB_CONFIRM=<name> python database/reset_db.py
"""
import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

WRONG_TARGET = (
    "Отказ: этот скрипт удаляет все таблицы и данные. Он работает только с одноразовой базой "
    "PostgreSQL на этом компьютере: в DATABASE_URL адрес 127.0.0.1, localhost или ::1, имя базы "
    "начинается с test_ или заканчивается на _test, без параметров после «?»."
)


def main() -> int:
    try:
        url = make_url(os.environ.get("DATABASE_URL", ""))
    except (ArgumentError, ValueError):
        url = None
    name = (url.database or "") if url is not None else ""
    if (url is None or url.get_backend_name() != "postgresql" or url.host not in LOCAL_HOSTS
            or url.query or not (name.startswith("test_") or name.endswith("_test"))):
        print(WRONG_TARGET, file=sys.stderr)
        return 2
    # The name is printed only after the target is known to be local and a test database.
    if os.environ.get("LUNARIA_RESET_DB_CONFIRM") != name:
        print(f"Отказ: будут удалены все таблицы и данные базы «{name}». Если эта база одноразовая, "
              f"запустите снова, задав переменную окружения LUNARIA_RESET_DB_CONFIRM={name}.",
              file=sys.stderr)
        return 2

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    print(f"Готово: схема public базы «{name}» очищена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
