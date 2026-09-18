"""
Tests for the persistence layer (step 2.4 / 2.5) and the orchestrator.

Two groups:
* ``unit``     — SQL contracts and :func:`process_subtitle_file` driven through a
                 fake cursor, so the transaction logic is verifiable without a server.
* ``integration`` — real PostgreSQL from ``docker/docker-compose.yml``; skipped
                 automatically when the server is unreachable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from app.subtitle_parser import (
    DEFAULT_BATCH_SIZE,
    SQL_COUNT_DIALOGUE_LINES,
    SQL_DELETE_DIALOGUE_LINES,
    SQL_INSERT_DIALOGUE_LINES,
    SQL_SET_STATUS,
    STATUS_FAILED,
    STATUS_PARSED,
    STATUS_PROCESSING,
    DialogueLine,
    LineCountMismatchError,
    SanitizerOptions,
    SubtitleFileError,
    SubtitleNotFoundError,
    SubtitleParserService,
    SubtitleStatus,
    count_dialogue_lines,
    delete_dialogue_lines,
    insert_dialogue_lines,
    process_subtitle_file,
    require_subtitle,
    set_status,
)

FIXTURES = Path(__file__).resolve().parent / "data"
BASIC_SRT = FIXTURES / "basic_english.srt"
SDH_SRT = FIXTURES / "sdh_noise.srt"
SUBTITLE_UID = "11111111-1111-1111-1111-111111111111"
UNKNOWN_UID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def block_real_status_connection(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit tests must never dial a database; integration tests keep the real helper."""
    if request.node.get_closest_marker("integration"):
        return

    import app.subtitle_parser as module

    def _boom() -> None:
        raise AssertionError("unit tests must pass failure_conn or stay on the error path")

    monkeypatch.setattr(module, "_open_fresh_connection", _boom)


# ─── SQL contracts (no server needed) ────────────────────────────────────────


@pytest.mark.unit
def test_status_sql_uses_named_parameters_and_enum_cast() -> None:
    assert "%(status)s::subtitle_status_enum" in SQL_SET_STATUS
    assert "%(subtitle_id)s" in SQL_SET_STATUS
    assert "UPDATE subtitles" in SQL_SET_STATUS
    assert "updated_at = NOW()" in SQL_SET_STATUS


@pytest.mark.unit
def test_insert_sql_columns_match_the_schema() -> None:
    for column in ("subtitle_id", "line_index", "start_time_ms", "end_time_ms", "raw_text"):
        assert column in SQL_INSERT_DIALOGUE_LINES
    # id/created_at come from the column defaults; passing them as parameters
    # would store the literal text "gen_random_uuid()".
    assert "gen_random_uuid" not in SQL_INSERT_DIALOGUE_LINES
    assert "created_at" not in SQL_INSERT_DIALOGUE_LINES


@pytest.mark.unit
def test_scope_of_the_line_queries_is_the_subtitle_only() -> None:
    assert "subtitle_id = %(subtitle_id)s::uuid" in SQL_DELETE_DIALOGUE_LINES
    assert "subtitle_id = %(subtitle_id)s::uuid" in SQL_COUNT_DIALOGUE_LINES
    assert "DELETE FROM dialogue_lines" in SQL_DELETE_DIALOGUE_LINES


@pytest.mark.unit
def test_every_placeholder_is_named() -> None:
    """Parameters are always passed as dicts, so a bare %s would break at runtime."""
    statements = (
        SQL_SET_STATUS,
        SQL_INSERT_DIALOGUE_LINES,
        SQL_DELETE_DIALOGUE_LINES,
        SQL_COUNT_DIALOGUE_LINES,
    )
    for statement in statements:
        assert "%s" not in statement, statement
        assert "{}" not in statement, statement  # no f-string interpolation of data


@pytest.mark.unit
def test_status_enum_members_mirror_the_database_enum() -> None:
    assert {member.value for member in SubtitleStatus} == {
        "pending",
        "processing",
        "parsed",
        "failed",
    }


# ─── Orchestration with a fake cursor ────────────────────────────────────────


class FakeCursor:
    """Records executed SQL and serves canned results; no database involved."""

    def __init__(
        self,
        *,
        subtitle_exists: bool = True,
        stored_count_offset: int = 0,
        fail_on: str | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.exists = subtitle_exists
        self.stored_count_offset = stored_count_offset
        self.fail_on = fail_on
        self.log = log if log is not None else []
        self.rows: list[dict[str, Any]] = []
        self.stored = 0
        self.closed = False

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    @property
    def rowcount(self) -> int:
        return 1

    def mogrify(self, sql: str, params: Any = None) -> bytes:
        """psycopg2 mogrifies every row of a batch, then joins the statements."""
        statement = " ".join(sql.split())
        self.log.append(statement)
        self.last = statement
        if statement.startswith("INSERT"):
            if self.fail_on == "insert":
                raise RuntimeError("simulated insert failure")
            self.rows.append(params)
            self.stored = len(self.rows)
        return statement.encode("utf-8")

    def execute(self, sql: str, params: Any = None) -> None:
        if params is None:
            # The single joined statement execute_batch sends after mogrify(): the
            # rows were already recorded one by one, so this call must not count twice.
            return
        statement = " ".join(sql.split())
        self.log.append(statement)
        self.last = statement
        if statement.startswith("SELECT id FROM subtitles"):
            return
        if "UPDATE subtitles" in statement:
            if self.fail_on == "status":
                raise RuntimeError("simulated server error")
            self.log.append(f"status={params['status']}")
            return
        if statement.startswith("DELETE"):
            if self.fail_on == "delete":
                raise RuntimeError("simulated delete failure")
            self.rows.clear()
            self.stored = 0
            return
        if statement.startswith("INSERT"):
            if self.fail_on == "insert":
                raise RuntimeError("simulated insert failure")
            self.rows.append(params)
            self.stored = len(self.rows)
            return
        if statement.startswith("SELECT COUNT"):
            return

    def fetchone(self) -> tuple | None:
        if getattr(self, "last", "").startswith("SELECT id FROM subtitles"):
            return (SUBTITLE_UID,) if self.exists else None
        if getattr(self, "last", "").startswith("SELECT COUNT"):
            return (self.stored + self.stored_count_offset,)
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.unit
def test_happy_path_sequence(tmp_path: Path) -> None:
    cursor = FakeCursor()
    conn = FakeConnection(cursor)

    saved = process_subtitle_file(conn, "11111111-1111-1111-1111-111111111111", str(BASIC_SRT))

    assert saved == 6
    assert cursor.log[0].startswith("SELECT id FROM subtitles")  # existence first
    assert "status=processing" in cursor.log
    assert "status=parsed" in cursor.log
    assert sum(1 for entry in cursor.log if entry.startswith("INSERT")) == saved
    assert conn.commits >= 1
    assert conn.rollbacks == 0


@pytest.mark.unit
def test_bulk_insert_uses_one_batched_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_batch is called once; psycopg2 pages the rows itself (500 per statement)."""
    import app.subtitle_parser as module

    calls: list[tuple[int, int]] = []

    def fake_execute_batch(
        cursor: Any, sql: str, rows: Any, page_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        rows = list(rows)
        calls.append((len(rows), page_size))

    monkeypatch.setattr(module, "execute_batch", fake_execute_batch)

    lines = [
        DialogueLine(
            line_index=index, start_time_ms=index, end_time_ms=index + 1, raw_text=f"row {index}"
        )
        for index in range(1, DEFAULT_BATCH_SIZE + 7)
    ]
    cursor = FakeCursor()

    assert insert_dialogue_lines(cursor, SUBTITLE_UID, lines) == len(lines)
    assert len(lines) == DEFAULT_BATCH_SIZE + 6  # a full page plus a partial one
    assert calls == [(len(lines), DEFAULT_BATCH_SIZE)]
    assert DEFAULT_BATCH_SIZE == 500


@pytest.mark.unit
def test_bulk_insert_of_nothing_sends_no_sql() -> None:
    cursor = FakeCursor()
    assert insert_dialogue_lines(cursor, SUBTITLE_UID, []) == 0
    assert cursor.log == []


@pytest.mark.unit
def test_missing_subtitle_never_reaches_the_parser() -> None:
    cursor = FakeCursor(subtitle_exists=False)
    conn = FakeConnection(cursor)

    with pytest.raises(SubtitleNotFoundError):
        process_subtitle_file(conn, UNKNOWN_UID, str(BASIC_SRT))

    assert not any(entry.startswith("INSERT") for entry in cursor.log)
    assert not any("status=parsed" in entry for entry in cursor.log)
    assert conn.commits == 0
    assert conn.rollbacks == 1  # the aborted transaction is closed


@pytest.mark.unit
def test_failure_records_failed_status(tmp_path: Path) -> None:
    cursor = FakeCursor(fail_on="status")
    conn = FakeConnection(cursor)
    status_cursor = FakeCursor()
    status_conn = FakeConnection(status_cursor)

    with pytest.raises(RuntimeError, match="simulated server error"):
        process_subtitle_file(
            conn,
            "11111111-1111-1111-1111-111111111111",
            str(tmp_path / "does-not-exist.srt"),
            failure_conn=status_conn,
        )

    assert conn.rollbacks == 1
    assert "status=failed" in status_cursor.log
    assert not any("status=parsed" in entry for entry in status_cursor.log)


@pytest.mark.unit
def test_missing_file_marks_failed(tmp_path: Path) -> None:
    cursor = FakeCursor()
    conn = FakeConnection(cursor)
    status_cursor = FakeCursor()

    with pytest.raises(SubtitleFileError):
        process_subtitle_file(
            conn,
            "11111111-1111-1111-1111-111111111111",
            str(tmp_path / "absent.srt"),
            failure_conn=FakeConnection(status_cursor),
        )

    assert "status=processing" in cursor.log
    assert "status=failed" in status_cursor.log
    assert conn.rollbacks == 1


@pytest.mark.unit
def test_row_count_verification_keeps_failed_state() -> None:
    cursor = FakeCursor(stored_count_offset=-1)  # COUNT reports one row short
    conn = FakeConnection(cursor)
    status_cursor = FakeCursor()

    with pytest.raises(LineCountMismatchError):
        process_subtitle_file(
            conn,
            "11111111-1111-1111-1111-111111111111",
            str(BASIC_SRT),
            failure_conn=FakeConnection(status_cursor),
        )

    assert "status=parsed" not in cursor.log
    assert "status=failed" in status_cursor.log


@pytest.mark.unit
def test_rollback_failure_is_logged_and_original_error_propagates(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class DeadConnection(FakeConnection):
        def rollback(self) -> None:
            raise RuntimeError("connection lost")

    cursor = FakeCursor(fail_on="status")
    conn = DeadConnection(cursor)

    # The orchestrator logs the failed rollback at WARNING (the original error is
    # already logged at ERROR by logger.exception), so capture from WARNING up.
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError, match="simulated server error"),
    ):
        process_subtitle_file(
            conn,
            SUBTITLE_UID,
            str(tmp_path / "absent.srt"),
            failure_conn=FakeConnection(FakeCursor()),
        )

    assert "rollback failed" in caplog.text


@pytest.mark.unit
def test_delete_before_insert_makes_the_run_idempotent() -> None:
    cursor = FakeCursor()
    conn = FakeConnection(cursor)
    subtitle_id = "11111111-1111-1111-1111-111111111111"

    process_subtitle_file(conn, subtitle_id, str(BASIC_SRT))
    first = [entry for entry in cursor.log if entry.startswith("INSERT")]
    process_subtitle_file(conn, subtitle_id, str(BASIC_SRT))
    second = [entry for entry in cursor.log if entry.startswith("INSERT")]

    deletes = [entry for entry in cursor.log if entry.startswith("DELETE")]
    assert len(deletes) == 2
    assert len(second) == 2 * len(first)
    assert len(cursor.rows) == 6  # the DELETE cleared the previous run


# ─── Orchestrator: options and reporting ─────────────────────────────────────


@pytest.mark.unit
def test_sanitizer_options_reach_the_stored_rows() -> None:
    cursor = FakeCursor()
    process_subtitle_file(
        FakeConnection(cursor),
        SUBTITLE_UID,
        str(SDH_SRT),
        options=SanitizerOptions(drop_music_lines=False, strip_speaker_labels=False),
        failure_conn=FakeConnection(FakeCursor()),
    )
    stored = " ".join(row["raw_text"] for row in cursor.rows)

    assert "Ring of fire" in stored  # lyrics kept, only the markers removed
    assert "WALTER:" in stored  # label stripping disabled


@pytest.mark.unit
def test_default_options_drop_the_noise_only_cues() -> None:
    cursor = FakeCursor()
    saved = process_subtitle_file(
        FakeConnection(cursor),
        SUBTITLE_UID,
        str(SDH_SRT),
        failure_conn=FakeConnection(FakeCursor()),
    )

    assert saved == 6 == len(cursor.rows)  # 8 cues in the fixture, 2 are pure noise
    assert all(row["subtitle_id"] == SUBTITLE_UID for row in cursor.rows)
    assert [row["line_index"] for row in cursor.rows] == list(range(1, saved + 1))


@pytest.mark.unit
def test_durable_status_commit_can_be_switched_off() -> None:
    conn = FakeConnection(FakeCursor())
    process_subtitle_file(
        conn,
        SUBTITLE_UID,
        str(BASIC_SRT),
        durable_status=False,
        failure_conn=FakeConnection(FakeCursor()),
    )
    assert conn.commits == 1  # only the final commit of the whole run


@pytest.mark.unit
def test_failed_status_that_cannot_be_written_does_not_hide_the_real_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken_status_conn = FakeConnection(FakeCursor(fail_on="status"))

    with caplog.at_level(logging.WARNING), pytest.raises(SubtitleFileError):
        process_subtitle_file(
            FakeConnection(FakeCursor()),
            SUBTITLE_UID,
            str(tmp_path / "absent.srt"),
            failure_conn=broken_status_conn,
        )

    assert "could not persist status='failed'" in caplog.text


@pytest.mark.unit
def test_unreachable_database_is_logged_when_recording_failure(tmp_path: Path) -> None:
    with pytest.raises(SubtitleFileError):
        process_subtitle_file(
            FakeConnection(FakeCursor()), SUBTITLE_UID, str(tmp_path / "absent.srt")
        )
    # the autouse fixture turns _open_fresh_connection into an error on purpose


# ─── Repository helpers against the real schema ──────────────────────────────


@pytest.mark.integration
def test_set_status_updates_the_row(db_conn: Any, subtitle_id: str) -> None:
    with db_conn.cursor() as cursor:
        assert set_status(cursor, subtitle_id, SubtitleStatus.PROCESSING) == 1
    db_conn.commit()
    with db_conn.cursor() as cursor:
        cursor.execute("SELECT status FROM subtitles WHERE id = %s::uuid", (subtitle_id,))
        assert cursor.fetchone()[0] == STATUS_PROCESSING


@pytest.mark.integration
def test_unknown_subtitle_is_rejected(db_conn: Any) -> None:
    with db_conn.cursor() as cursor, pytest.raises(SubtitleNotFoundError):
        require_subtitle(cursor, "00000000-0000-0000-0000-000000000000")
    db_conn.rollback()


@pytest.mark.integration
def test_bulk_insert_and_count(db_conn: Any, subtitle_id: str) -> None:
    lines, _ = SubtitleParserService.parse_srt_string(
        "\n\n".join(
            [
                "1\n00:00:01,000 --> 00:00:02,000\nHello there.",
                "2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi.",
            ]
        )
    )
    with db_conn.cursor() as cursor:
        assert insert_dialogue_lines(cursor, subtitle_id, lines) == 2
        assert count_dialogue_lines(cursor, subtitle_id) == 2
        assert delete_dialogue_lines(cursor, subtitle_id) == 2
        assert count_dialogue_lines(cursor, subtitle_id) == 0
    db_conn.rollback()


@pytest.mark.integration
def test_end_to_end_ingestion(db_conn: Any, subtitle_id: str) -> None:
    saved = process_subtitle_file(db_conn, subtitle_id, str(BASIC_SRT))
    assert saved == 6

    with db_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT line_index, start_time_ms, end_time_ms, raw_text
              FROM dialogue_lines
             WHERE subtitle_id = %s::uuid
             ORDER BY line_index
            """,
            (subtitle_id,),
        )
        rows = cursor.fetchall()
        cursor.execute("SELECT status FROM subtitles WHERE id = %s::uuid", (subtitle_id,))
        status = cursor.fetchone()[0]

    assert [row[0] for row in rows] == list(range(1, 7))
    assert rows[0][1] == 1000 and rows[0][2] == 3500
    assert rows[0][3] == "The world is quiet here."
    assert status == STATUS_PARSED


@pytest.mark.integration
def test_reprocessing_does_not_duplicate_rows(db_conn: Any, subtitle_id: str) -> None:
    assert process_subtitle_file(db_conn, subtitle_id, str(BASIC_SRT)) == 6
    assert process_subtitle_file(db_conn, subtitle_id, str(BASIC_SRT)) == 6

    with db_conn.cursor() as cursor:
        assert count_dialogue_lines(cursor, subtitle_id) == 6


@pytest.mark.integration
def test_unknown_file_leaves_the_track_failed(
    db_conn: Any, subtitle_id: str, tmp_path: Path
) -> None:
    with pytest.raises(SubtitleFileError):
        process_subtitle_file(db_conn, subtitle_id, str(tmp_path / "absent.srt"))

    with db_conn.cursor() as cursor:
        assert count_dialogue_lines(cursor, subtitle_id) == 0
        cursor.execute("SELECT status FROM subtitles WHERE id = %s::uuid", (subtitle_id,))
        assert cursor.fetchone()[0] == STATUS_FAILED


@pytest.mark.integration
def test_500_lines_land_in_one_page(db_conn: Any, subtitle_id: str) -> None:
    cues = [
        f"{index}\n00:00:0{index % 6},000 --> 00:00:0{index % 6},500\nLine number {index}"
        for index in range(1, 501)
    ]
    lines, _ = SubtitleParserService.parse_srt_string("\n\n".join(cues))
    assert len(lines) == 500

    with db_conn.cursor() as cursor:
        assert insert_dialogue_lines(cursor, subtitle_id, lines) == 500
        assert count_dialogue_lines(cursor, subtitle_id) == 500
    db_conn.rollback()


@pytest.mark.integration
def test_deleting_a_subtitle_cascades_to_its_lines(db_conn: Any, subtitle_id: str) -> None:
    """T4.9 — the FK is ``ON DELETE CASCADE``; the cleanup must never leave orphans."""
    assert process_subtitle_file(db_conn, subtitle_id, str(BASIC_SRT)) == 6

    with db_conn.cursor() as cursor:
        cursor.execute("DELETE FROM subtitles WHERE id = %s::uuid", (subtitle_id,))
        cursor.execute(
            "SELECT COUNT(*) FROM dialogue_lines WHERE subtitle_id = %s::uuid", (subtitle_id,)
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute("""
            SELECT COUNT(*) FROM dialogue_lines d
              LEFT JOIN subtitles s ON s.id = d.subtitle_id
             WHERE s.id IS NULL
            """)
        assert cursor.fetchone()[0] == 0
    db_conn.rollback()  # the fixture owns the row lifecycle, not this test


@pytest.mark.integration
def test_unparseable_file_leaves_the_track_failed(
    db_conn: Any, subtitle_id: str, tmp_path: Path
) -> None:
    """T4.8 — a file that exists but carries no cues must end up ``failed``, empty."""
    junk = tmp_path / "junk.srt"
    junk.write_text("prose without a single timecode\nsecond line\n", encoding="utf-8")

    with pytest.raises(SubtitleFileError):
        process_subtitle_file(db_conn, subtitle_id, str(junk))

    with db_conn.cursor() as cursor:
        # The main transaction was rolled back, so neither 'processing' nor any row
        # of that run is visible here; 'failed' comes from its own committed session.
        assert count_dialogue_lines(cursor, subtitle_id) == 0
        cursor.execute("SELECT status FROM subtitles WHERE id = %s::uuid", (subtitle_id,))
        assert cursor.fetchone()[0] == STATUS_FAILED


@pytest.mark.integration
def test_dialogue_lines_index_is_present_and_usable(db_conn: Any, subtitle_id: str) -> None:
    """T4.5 — step 3 walks contexts ordered by time, so the time index must exist."""
    with db_conn.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'dialogue_lines'"
            "  AND indexname = 'idx_dialogue_lines_subtitle_time'"
        )
        row = cursor.fetchone()
        assert row is not None, "idx_dialogue_lines_subtitle_time is missing"
        assert "subtitle_id" in row[0] and "start_time_ms" in row[0]

        # Force the planner off seqscan so it must reach for the index.
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute(
            "EXPLAIN SELECT raw_text FROM dialogue_lines"
            "  WHERE subtitle_id = %s::uuid ORDER BY start_time_ms LIMIT 10",
            (subtitle_id,),
        )
        plan = "\n".join(record[0] for record in cursor.fetchall())
    assert "idx_dialogue_lines_subtitle_time" in plan
