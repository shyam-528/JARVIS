"""PostgreSQL connection adapter for the public store's fixed SQL statements.

No user-supplied SQL is accepted. Parameters remain bound by psycopg.
Short write transactions use a database-wide advisory lock, equivalent to the
SQLite store's BEGIN IMMEDIATE. This preserves quotas and leases across workers.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager


class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def row_factory(cursor):
    columns = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(zip(columns, values, strict=True))


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        if sql.strip() == "BEGIN IMMEDIATE":
            return self.connection.execute("SELECT pg_advisory_xact_lock(827349102)")
        return self.connection.execute(sql.replace("?", "%s"), params)

    def executemany(self, sql, params):
        with self.connection.cursor() as cursor:
            cursor.executemany(sql.replace("?", "%s"), params)

    def executescript(self, sql):
        # Serialize schema creation across cold starts. Existing local SQLite
        # schema is translated only for these known, static DDL statements.
        self.execute("BEGIN IMMEDIATE")
        sql = sql.replace("PRAGMA journal_mode=WAL;", "")
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        sql = sql.replace(" REAL", " DOUBLE PRECISION")
        for statement in sql.split(";"):
            if statement.strip():
                self.connection.execute(statement)


@contextmanager
def connect(database_url):
    import psycopg

    try:
        with psycopg.connect(
            database_url,
            connect_timeout=8,
            row_factory=row_factory,
        ) as db:
            # Pooled Neon connections reject these as startup parameters.
            # Transaction-local settings work with PgBouncer and reset on commit.
            db.execute("SET LOCAL statement_timeout = '10s'")
            db.execute("SET LOCAL lock_timeout = '5s'")
            yield Connection(db)
    except psycopg.IntegrityError:
        # Keep the existing store/API duplicate-account contract.
        raise sqlite3.IntegrityError("Database constraint rejected the operation") from None
    except psycopg.Error:
        # Connection strings and remote server details must never reach clients.
        raise sqlite3.OperationalError("Public database temporarily unavailable") from None
