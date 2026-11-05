import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine, get_db


def test_engine_can_connect():
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1


def test_session_can_query():
    with SessionLocal() as session:
        assert session.execute(text("SELECT current_database()")).scalar() == "atlas"


def _patch_session_factory(monkeypatch):
    """Make get_db() build its session from a factory we control."""
    import app.database

    expected = Session(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(app.database, "SessionLocal", lambda: expected)
    return expected


def test_get_db_yields_the_session_and_closes_it_on_generator_close(monkeypatch):
    expected = _patch_session_factory(monkeypatch)
    checked_out_before = engine.pool.checkedout()

    generator = get_db()
    session = next(generator)

    assert session is expected
    # Use the session so it holds a transaction and a pooled connection.
    session.execute(text("SELECT 1"))
    assert session.in_transaction()
    assert engine.pool.checkedout() == checked_out_before + 1

    generator.close()

    assert not session.in_transaction()
    assert engine.pool.checkedout() == checked_out_before


def test_get_db_closes_the_session_when_the_request_finishes_normally(monkeypatch):
    expected = _patch_session_factory(monkeypatch)
    checked_out_before = engine.pool.checkedout()

    generator = get_db()
    session = next(generator)
    session.execute(text("SELECT 1"))
    assert session.in_transaction()

    # FastAPI resumes the dependency after the response is sent; it then finishes.
    with pytest.raises(StopIteration):
        next(generator)

    assert session is expected
    assert not session.in_transaction()
    assert engine.pool.checkedout() == checked_out_before


def test_get_db_closes_the_session_when_an_exception_is_thrown_in(monkeypatch):
    expected = _patch_session_factory(monkeypatch)
    checked_out_before = engine.pool.checkedout()

    generator = get_db()
    session = next(generator)
    session.execute(text("SELECT 1"))
    assert session.in_transaction()

    # FastAPI throws the endpoint's exception into the dependency generator.
    with pytest.raises(RuntimeError, match="endpoint failed"):
        generator.throw(RuntimeError("endpoint failed"))

    assert session is expected
    assert not session.in_transaction()
    assert engine.pool.checkedout() == checked_out_before
