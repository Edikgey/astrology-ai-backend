"""Generates charts.json: the saved calculation rows of the four synthetic charts (EVAL-SET.md, section 2).

Uses the product calculation path (api.endpoints._create_natal_chart) of the checkout it is run from, in an
in-memory SQLite database, with the JPL asteroid request switched off (as on the stand: no asteroids) and every
non-loopback connection refused. Run once on the base code and commit the result; all configurations read it.
"""
import argparse
import json
import os
import socket
import sys
from pathlib import Path
from uuid import uuid4

# name -> (NatalChartCreate fields). Synthetic dates; places are public cities, no real person.
CHARTS = {
    "A": dict(year=1991, month=3, day=14, hour=7, minute=20, timezone="Europe/Moscow", lon=37.6173, lat=55.7558,
              city="Москва", region="Москва", country="Россия"),
    "B": dict(year=1987, month=8, day=29, hour=23, minute=45, timezone="Asia/Novosibirsk", lon=82.9357, lat=55.0084,
              city="Новосибирск", region="Новосибирская область", country="Россия"),
    "C": dict(year=2001, month=12, day=5, hour=13, minute=5, timezone="Europe/Kyiv", lon=30.5234, lat=50.4501,
              city="Киев", region="Киев", country="Украина"),
    "D": dict(year=1979, month=6, day=21, hour=4, minute=10, timezone="Europe/Moscow", lon=30.3609, lat=59.9311,
              city="Санкт-Петербург", region="Санкт-Петербург", country="Россия"),
}


def _refuse_network():
    def refuse(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ConnectionRefusedError("make_charts: no network")
        return real(self, address)
    real = socket.socket.connect
    socket.socket.connect = refuse


def build(repo):
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    os.environ.update({"DATABASE_URL": "sqlite://", "SECRET_KEY": "chat-eval-charts-only",
                       "OPENAI_API_KEY": "stub-not-a-key"})
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    _refuse_network()
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from database.connection import Base
    from database.queries import NatalChart, ChartData
    from models.natal_chart import NatalChartCreate
    from modules.ephemeris import Ephemeris
    from api.endpoints import _create_natal_chart
    Ephemeris._asteroid_snapshot = lambda self: {}  # no JPL request: asteroids absent, as on the stand
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    result = {}
    with sessionmaker(bind=engine, autoflush=False)() as db:
        for key, fields in CHARTS.items():
            created = _create_natal_chart(NatalChartCreate(**fields), {"user": None, "session_token": uuid4()}, db)
            chart = db.get(NatalChart, created["chart_id"])
            data = db.query(ChartData).filter_by(chart_id=chart.id).one()
            result[key] = {
                "chart": {"year": chart.year, "month": chart.month, "day": chart.day, "hour": chart.hour,
                          "timezone": chart.timezone, "birth_utc": chart.birth_utc.isoformat(),
                          "lon": chart.lon, "lat": chart.lat, "city": chart.city, "region": chart.region,
                          "country": chart.country},
                "data": {"bodies_for_circle": data.bodies_for_circle, "aspects_for_circle": data.aspects_for_circle,
                         "points_data": data.points_data, "patterns_data": data.patterns_data,
                         "aspects_structured": data.aspects_structured, "houses": data.houses,
                         "house_system": data.house_system},
            }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True, help="checkout whose calculator is used (the base code)")
    parser.add_argument("--out", default=str(Path(__file__).with_name("charts.json")))
    args = parser.parse_args(argv)
    charts = build(Path(args.repo).resolve())
    Path(args.out).write_text(json.dumps(charts, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("chat_eval.make_charts outcome=ok charts=%d" % len(charts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
