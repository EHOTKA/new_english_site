"""
Database connection helper for the subtitle pipeline (and friends).

Credentials come from the environment so nothing is hardcoded in the repository.
The defaults match ``docker/docker-compose.yml`` of this project.

Environment variables
---------------------
SUBTITLES_DB_HOST      default ``localhost``
SUBTITLES_DB_PORT      default ``5430``
SUBTITLES_DB_NAME      default ``postgres_db``
SUBTITLES_DB_USER      default ``postgres_user``
SUBTITLES_DB_PASSWORD  default ``postgres_password``
SUBTITLES_DB_TIMEOUT   connect timeout in seconds, default ``10``

Usage::

    from app.db import get_connection

    with get_connection() as conn:
        saved = process_subtitle_file(conn, subtitle_id, "movie.srt")
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extensions

__all__ = ["DEFAULTS", "connection_kwargs", "get_connection", "server_is_reachable"]

#: (env var, fallback value) — fallbacks mirror docker/docker-compose.yml.
DEFAULTS: dict[str, tuple[str, str | None]] = {
    "host": ("SUBTITLES_DB_HOST", "localhost"),
    "port": ("SUBTITLES_DB_PORT", "5430"),
    "dbname": ("SUBTITLES_DB_NAME", "postgres_db"),
    "user": ("SUBTITLES_DB_USER", "postgres_user"),
    "password": ("SUBTITLES_DB_PASSWORD", "postgres_password"),
}


def connection_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build ``psycopg2.connect`` kwargs: explicit > environment > compose defaults."""
    kwargs: dict[str, Any] = {}
    for key, (env_name, default) in DEFAULTS.items():
        if overrides.get(key) is not None:
            kwargs[key] = overrides[key]
        else:
            kwargs[key] = os.environ.get(env_name) or default

    timeout = os.environ.get("SUBTITLES_DB_TIMEOUT")
    kwargs["connect_timeout"] = int(timeout) if timeout and timeout.isdigit() else 10
    # Makes the pipeline's queries identifiable in pg_stat_activity / pg_locks.
    kwargs["application_name"] = overrides.get("application_name") or "subtitle_parser"
    return kwargs


def get_connection(**overrides: Any) -> psycopg2.extensions.connection:
    """Open a PostgreSQL connection in manual-commit mode (``autocommit=False``)."""
    return psycopg2.connect(**connection_kwargs(**overrides))


@contextmanager
def session(**overrides: Any) -> Iterator[psycopg2.extensions.connection]:
    """Context manager committing on success and rolling back on failure."""
    conn = get_connection(**overrides)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def server_is_reachable(**overrides: Any) -> bool:
    """Return ``True`` when a connection can be opened (used to skip DB tests)."""
    try:
        conn = get_connection(**overrides)
    except Exception:
        return False
    conn.close()
    return True
