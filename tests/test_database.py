from sqlalchemy import text

from app.database import SessionLocal, engine


def test_engine_can_connect():
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1


def test_session_can_query():
    with SessionLocal() as session:
        assert session.execute(text("SELECT current_database()")).scalar() == "atlas"
