#!/usr/bin/env python3
"""Apply SQL migrations in filename order against a Postgres database.

Idempotent: applied migrations are recorded in `schema_migrations` and skipped on
re-runs. Each migration runs inside its own transaction.

Usage:
    python scripts/migrate.py                     # uses $VERITAS_DATABASE_URL or default
    python scripts/migrate.py --database-url postgresql://user:pass@host/db
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
DEFAULT_URL = "postgresql://localhost/veritas"


def run(database_url: str) -> int:
    with psycopg.connect(database_url) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}

        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            print(f"no migrations found in {MIGRATIONS_DIR}", file=sys.stderr)
            return 1

        for path in files:
            if path.name in applied:
                print(f"[skip]    {path.name}")
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                )
            print(f"[applied] {path.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Veritas SQL migrations.")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("VERITAS_DATABASE_URL", DEFAULT_URL),
        help="Postgres connection string (default: $VERITAS_DATABASE_URL or localhost/veritas)",
    )
    args = parser.parse_args()
    return run(args.database_url)


if __name__ == "__main__":
    raise SystemExit(main())
