from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

MIGRATION_LOCK_ID = 7_243_944_686_731_002_001


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    sql: str
    sha256_hex: str


def bundled_migrations() -> tuple[Migration, ...]:
    root = files("radiacode_app.migrations")
    migrations: list[Migration] = []
    for resource in sorted(root.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".sql"):
            continue
        sql = resource.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=resource.name.removesuffix(".sql"),
                sql=sql,
                sha256_hex=hashlib.sha256(sql.encode()).hexdigest(),
            )
        )
    return tuple(migrations)


def migrate(dsn: str) -> tuple[str, ...]:
    import psycopg

    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=False) as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        connection.execute("CREATE SCHEMA IF NOT EXISTS radiacode_private")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS radiacode_private.schema_migrations (
                version text PRIMARY KEY,
                sha256_hex text NOT NULL CHECK (length(sha256_hex) = 64),
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
            """
        )
        existing: dict[str, str] = dict(
            connection.execute(
                "SELECT version, sha256_hex FROM radiacode_private.schema_migrations"
            ).fetchall()
        )
        for migration in bundled_migrations():
            previous_checksum = existing.get(migration.version)
            if previous_checksum is not None:
                if previous_checksum != migration.sha256_hex:
                    raise MigrationError(f"checksum drift for applied migration {migration.version}")
                continue
            connection.execute(migration.sql)
            connection.execute(
                "INSERT INTO radiacode_private.schema_migrations(version, sha256_hex) VALUES (%s, %s)",
                (migration.version, migration.sha256_hex),
            )
            applied.append(migration.version)
    return tuple(applied)
