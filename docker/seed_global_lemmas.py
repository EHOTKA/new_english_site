#!/usr/bin/env python3
"""
Direct PostgreSQL seeder for global_lemmas table.

Reads top English lemmas enriched with parts of speech (POS) and CEFR levels (A1-C2)
from data/en_50k.txt and data/word_cefr_minified.db, then bulk-inserts them into PostgreSQL.

Usage:
    python3 seed_global_lemmas.py          # inserts 10,000 lemmas into Postgres
    python3 seed_global_lemmas.py --dry-run  # preview only
"""

import sys
import argparse
from pathlib import Path
from contextlib import contextmanager

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None

from generate_seed_sql import load_dataset

DB_KWARGS = dict(
    host="localhost",
    port=5430,
    dbname="postgres_db",
    user="postgres_user",
    password="postgres_password",
)

BATCH_SIZE = 500


@contextmanager
def db_connect():
    if psycopg2 is None:
        raise ImportError("psycopg2 is required for direct DB insertion. Run: pip install psycopg2-binary")
    conn = psycopg2.connect(**DB_KWARGS)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_lemmas(lemmas: list[dict], dry_run: bool = False) -> None:
    if dry_run:
        print("\n── DRY RUN ──")
        for entry in lemmas[:20]:
            print(
                f"  rank={entry['frequency_rank']:>5}  lemma={entry['lemma']:<20}  "
                f"pos={entry['part_of_speech']:<14}  cefr={entry['cefr_level']}"
            )
        if len(lemmas) > 20:
            print(f"  … and {len(lemmas) - 20} more entries (not shown)")
        return

    print(f"\nInserting {len(lemmas)} lemmas into global_lemmas…")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM global_lemmas;")
            print("  Existing rows cleared.")

        values = [
            (e["lemma"], e["part_of_speech"], e["cefr_level"], e["frequency_rank"])
            for e in lemmas
        ]

        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO global_lemmas (lemma, part_of_speech, cefr_level, frequency_rank)
                VALUES %s
                """,
                values,
                page_size=BATCH_SIZE,
                template="(%s, %s, %s::cefr_level_enum, %s)",
            )
            print(f"  ✓ Inserted {len(values)} rows in batches of {BATCH_SIZE}.")

        # Verification
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM global_lemmas;")
            count = cur.fetchone()[0]
            print(f"\n  Table now contains {count} rows.")

            cur.execute(
                "SELECT lemma, part_of_speech, cefr_level, frequency_rank FROM global_lemmas "
                "ORDER BY frequency_rank LIMIT 10;"
            )
            print("  Top 10 lemmas:")
            for row in cur.fetchall():
                print(f"    #{row[3]:<4} {row[0]:<15} pos={row[1]:<12} cefr={row[2]}")


def main():
    parser = argparse.ArgumentParser(description="Seed global_lemmas table directly")
    parser.add_argument("--dry-run", action="store_true", help="Preview data without inserting")
    parser.add_argument("--limit", type=int, default=10_000, help="Number of lemmas to insert (default: 10000)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Global Lemmas Seeder")
    print("=" * 60)

    lemmas = load_dataset(limit=args.limit)
    insert_lemmas(lemmas, dry_run=args.dry_run)
    print("\nDone.")


if __name__ == "__main__":
    main()
