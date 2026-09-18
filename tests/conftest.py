"""Shared fixtures for step 2 tests.

``unit`` tests never touch the database. ``integration`` tests use the container
from ``docker/docker-compose.yml`` (host localhost, port 5430) and are skipped
automatically when the server is not reachable, so ``pytest`` stays green on a
machine without Docker.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # makes `import app...` work with any pytest version
    sys.path.insert(0, str(ROOT))

DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_manifest():
    """Import tests/data/make_fixtures.py by path (it is a script, not a package).

    The module must be registered in ``sys.modules`` before ``exec_module``:
    make_fixtures.py uses ``from __future__ import annotations``, and resolving
    those postponed annotations inside ``@dataclass`` looks the module up by
    name in ``sys.modules`` (a missing entry raises AttributeError there).
    """
    spec = importlib.util.spec_from_file_location("make_fixtures", DATA_DIR / "make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


MANIFEST = _load_manifest()
MANIFEST.write_fixtures(DATA_DIR)  # fixtures are generated, not committed to git


@pytest.fixture(scope="session")
def fixture_specs():
    """The expectation table shared by make_fixtures.py and the tests."""
    return {spec.name: spec for spec in MANIFEST.FIXTURES}


def _database_available() -> str | None:
    """Return None when the DB answers, otherwise the reason it does not."""
    try:
        from app.db import get_connection
    except ImportError as exc:  # psycopg2 missing
        return f"psycopg2 is not importable: {exc}"
    try:
        conn = get_connection()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    conn.close()
    return None


@pytest.fixture(scope="session")
def srt_fixture_path():
    """Factory fixture: ``srt_fixture_path("windows_cp1252.srt")`` → Path."""

    def _path(name: str) -> Path:
        path = DATA_DIR / name
        if not path.exists():
            pytest.fail(
                f"{path} is missing — run `python tests/data/make_fixtures.py` once before pytest"
            )
        return path

    return _path


@pytest.fixture(scope="session")
def fixture_names() -> list[str]:
    return sorted(path.name for path in DATA_DIR.glob("*.srt"))


@pytest.fixture(scope="session")
def db_conn():
    """A single psycopg2 connection for the whole test session (rolled back per test)."""
    reason = _database_available()
    if reason:
        pytest.skip(f"PostgreSQL is not reachable ({reason})")

    from app.db import get_connection

    conn = get_connection()
    conn.autocommit = False
    yield conn
    conn.close()


@pytest.fixture
def subtitle_id(db_conn) -> Iterator[str]:
    """Create a throwaway subtitles row (title + episode + track) and clean it up.

    Mirrors ``docker/init.sql``: ``episodes`` hangs off ``titles`` through a
    ``season_number`` column (there is no ``seasons`` table) and ``subtitles``
    references ``episode_id`` plus a plain ``language_code`` (there is no
    ``languages`` table). The row is committed because the orchestrator records
    ``status='failed'`` from a second connection, which cannot see uncommitted data.
    """
    uid = str(uuid.uuid4())
    title_id = str(uuid.uuid4())
    episode_id = str(uuid.uuid4())

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO titles (id, name, original_name, type) VALUES (%s, %s, %s, 'series')",
            (title_id, "pytest title", "pytest original"),
        )
        cur.execute(
            """
            INSERT INTO episodes (id, title_id, season_number, episode_number, name)
            VALUES (%s, %s, 1, 1, 'pytest episode')
            """,
            (episode_id, title_id),
        )
        cur.execute(
            """
            INSERT INTO subtitles (id, episode_id, language_code, source, status, raw_storage_path)
            VALUES (%s, %s, 'en', 'pytest', 'pending', 'pytest://fixtures/basic_english.srt')
            """,
            (uid, episode_id),
        )
    db_conn.commit()

    yield uid

    db_conn.rollback()  # drop anything a test left open
    with db_conn.cursor() as cur:
        # One DELETE suffices: CASCADE removes the episode, the track and its lines.
        cur.execute("DELETE FROM titles WHERE id = %s", (title_id,))
    db_conn.commit()


@pytest.fixture
def clean_transaction(db_conn):
    """Roll back after each test so integration tests cannot leak rows."""
    yield db_conn
    db_conn.rollback()


@pytest.fixture(scope="session")
def compose_env() -> dict:
    """Environment used by docker-compose (handy when debugging connection errors)."""
    return {
        key: os.environ.get(key)
        for key in ("SUBTITLES_DB_HOST", "SUBTITLES_DB_PORT", "SUBTITLES_DB_NAME")
    }
