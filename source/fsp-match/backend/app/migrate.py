"""Initial schema migration for the dedicated fsp-match database only.

This creates missing tables, does not drop data or alter existing projects.
Future upgrades must add explicit numbered migrations, not recreate the DB.
"""
import time
from sqlalchemy import Column, Float, MetaData, String, Table, select
from .config import get_settings
from .database import build_database, dispose_database
from .models import Base

metadata = MetaData()
versions = Table("schema_migrations", metadata, Column("version", String(48), primary_key=True), Column("applied_at", Float, nullable=False))
VERSION = "0001_initial"


def migrate(engine):
    Base.metadata.create_all(engine)
    metadata.create_all(engine)
    with engine.begin() as connection:
        if not connection.execute(select(versions.c.version).where(versions.c.version == VERSION)).first():
            connection.execute(versions.insert().values(version=VERSION, applied_at=time.time()))


def main():
    engine, _ = build_database(get_settings().database_url)
    try:
        migrate(engine)
        print("fsp-match schema: 0001_initial")
    finally:
        dispose_database(engine)


if __name__ == "__main__":
    main()
