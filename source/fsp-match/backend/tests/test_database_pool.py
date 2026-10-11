from sqlalchemy.pool import StaticPool
from app.database import build_database, dispose_database


def test_postgres_business_and_limiter_pools_are_distinct_and_bounded():
    # Engine construction does not connect; this is a resource-safety guard,
    # not a substitute for the opt-in real PostgreSQL concurrency run.
    engine, _ = build_database("postgresql+psycopg://synthetic@127.0.0.1/fsp_match_validation")
    try:
        assert engine.pool.size() == 10 and engine.pool._max_overflow == 14
        assert engine.pool.timeout() == 5
        limiter = engine._fsp_limiter_engine
        assert limiter is not engine
        assert limiter.pool.size() == 3 and limiter.pool._max_overflow == 5
        assert engine.pool.checkedout() == 0 and limiter.pool.checkedout() == 0
    finally:
        dispose_database(engine)


def test_postgres_pool_change_does_not_modify_sqlite_file_or_memory(tmp_path):
    file_engine, _ = build_database("sqlite:///" + str(tmp_path / "pool.db"))
    memory_engine, _ = build_database("sqlite:///:memory:")
    try:
        assert file_engine.pool.size() == 5 and file_engine.pool._max_overflow == 10
        assert file_engine._fsp_limiter_engine is not file_engine
        assert isinstance(memory_engine.pool,StaticPool)
        assert memory_engine._fsp_limiter_engine is memory_engine
        assert not (tmp_path / "pool.db").exists(), "Constructing either pool must not open/create a database"
    finally:
        dispose_database(file_engine);dispose_database(memory_engine)
