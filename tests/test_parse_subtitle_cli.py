"""
Tests for the CLI of T3.7 (``app/scripts/parse_subtitle.py``).

Everything except the last test is a ``unit`` test: ``--dry-run`` has to work with no
PostgreSQL at all, which ``test_dry_run_does_not_touch_the_database`` pins down by
making ``get_connection`` explode.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from app.scripts import parse_subtitle as cli

pytestmark = pytest.mark.unit

BASIC_LINES = 6  # cues kept from tests/data/basic_english.srt


def payload_of(stdout: str) -> dict:
    """The ``--json`` contract: exactly one JSON object on stdout, nothing else."""
    lines = stdout.strip().splitlines()
    assert len(lines) == 1, f"expected a single JSON line, got {lines!r}"
    return json.loads(lines[0])


def _state(db_conn, subtitle_id: str) -> tuple[int, str]:
    """(row count, status) as the database sees them after the CLI committed."""
    db_conn.rollback()  # a fresh READ COMMITTED snapshot, independent of the CLI
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dialogue_lines WHERE subtitle_id = %s", (subtitle_id,))
        rows = cur.fetchone()[0]
        cur.execute("SELECT status FROM subtitles WHERE id = %s", (subtitle_id,))
        status = cur.fetchone()[0]
    db_conn.rollback()
    return int(rows), str(status)


def test_dry_run_text_output_is_human_readable(srt_fixture_path, capsys) -> None:
    path = srt_fixture_path("basic_english.srt")

    assert cli.main(["--file", str(path), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert f"OK: {BASIC_LINES} dialogue lines would be stored for subtitle" in out
    assert "encoding=utf-8" in out and "kept=6 dropped=0" in out
    assert "The world is quiet here." in out  # the cleaned-line preview


def test_dry_run_json_carries_the_full_report(srt_fixture_path, capsys) -> None:
    path = srt_fixture_path("basic_english.srt")

    assert cli.main(["--file", str(path), "--dry-run", "--json"]) == 0

    data = payload_of(capsys.readouterr().out)
    assert data["status"] == "dry-run"
    assert data["dry_run"] is True
    assert data["saved"] == BASIC_LINES
    assert data["cues_total"] == BASIC_LINES
    assert data["cues_kept"] == BASIC_LINES
    assert data["cues_dropped"] == 0
    assert data["deleted_lines"] == 0  # nothing was read, so nothing was replaced
    assert data["encoding"] == "utf-8"
    assert data["engine"] in {"pysrt", "regex"}
    assert data["file"] == str(path)
    assert data["subtitle_id"] is None
    assert data["sample"][0] == "The world is quiet here."


def test_dry_run_does_not_touch_the_database(srt_fixture_path, monkeypatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("--dry-run must not open a database connection")

    monkeypatch.setattr(cli, "get_connection", boom)
    path = srt_fixture_path("basic_english.srt")

    assert cli.main(["--file", str(path), "--dry-run"]) == 0


def test_subtitle_id_is_required_without_dry_run(srt_fixture_path) -> None:
    path = srt_fixture_path("basic_english.srt")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--file", str(path)])

    assert excinfo.value.code == 2  # argparse usage error, distinct from a pipeline failure


def test_missing_file_exits_with_one(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope.srt"

    assert cli.main(["--file", str(missing), "--dry-run"]) == 1

    assert "SRT file not found" in capsys.readouterr().out


def test_missing_file_reports_a_json_error(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope.srt"

    assert cli.main(["--file", str(missing), "--dry-run", "--json"]) == 1

    data = payload_of(capsys.readouterr().out)
    assert data["status"] == "error"
    assert "SubtitleFileError" in data["error"]
    assert "saved" not in data


def test_unexpected_error_keeps_stdout_machine_readable(
    srt_fixture_path, monkeypatch, capsys
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cli.SubtitleParserService, "parse_srt", staticmethod(boom))
    path = srt_fixture_path("basic_english.srt")

    assert cli.main(["--file", str(path), "--dry-run"]) == 1

    out = capsys.readouterr().out
    assert "FAILED: unexpected failure" in out
    assert "Traceback" not in out  # the traceback belongs to the log, not to the result


def test_sanitizer_toggles_reach_the_parser(srt_fixture_path, capsys) -> None:
    path = srt_fixture_path("music_and_ellipsis.srt")

    assert cli.main(["--file", str(path), "--dry-run", "--json"]) == 0
    default = payload_of(capsys.readouterr().out)

    assert cli.main(["--file", str(path), "--dry-run", "--json", "--keep-music-lines"]) == 0
    kept_music = payload_of(capsys.readouterr().out)

    assert default["cues_dropped"] == 2
    assert kept_music["cues_dropped"] == 0
    assert kept_music["cues_kept"] > default["cues_kept"]


def test_keep_speaker_labels_leaves_the_label_in(srt_fixture_path, capsys) -> None:
    path = srt_fixture_path("speaker_labels.srt")

    assert cli.main(["--file", str(path), "--dry-run", "--json", "--keep-speaker-labels"]) == 0
    kept = payload_of(capsys.readouterr().out)

    assert cli.main(["--file", str(path), "--dry-run", "--json"]) == 0
    stripped = payload_of(capsys.readouterr().out)

    assert kept["sample"][0].startswith("JESSE:")
    assert not stripped["sample"][0].startswith("JESSE:")


def test_keep_line_breaks_preserves_a_multiline_cue(tmp_path: Path, capsys) -> None:
    """Default options flatten a two-line cue; the flag keeps the break in ``raw_text``."""
    path = tmp_path / "two_line.srt"
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello there\nhow are you\n"
        "\n2\n00:00:03,000 --> 00:00:04,000\nI am fine.\n",
        encoding="utf-8",
    )

    assert cli.main(["--file", str(path), "--dry-run", "--json"]) == 0
    merged = payload_of(capsys.readouterr().out)

    assert cli.main(["--file", str(path), "--dry-run", "--json", "--keep-line-breaks"]) == 0
    split = payload_of(capsys.readouterr().out)

    assert merged["saved"] == split["saved"] == 2
    assert merged["sample"][0] == "Hello there how are you"
    assert split["sample"][0] == "Hello there\nhow are you"


def test_encoding_override_is_passed_through(srt_fixture_path, monkeypatch, capsys) -> None:
    seen: list = []

    def spy(file_path: object, encoding: object = None, **kwargs: object) -> tuple[str, str]:
        seen.append(encoding)
        return "not an srt file", str(encoding)

    monkeypatch.setattr(cli.SubtitleParserService, "read_file", staticmethod(spy))
    path = srt_fixture_path("windows_cp1252.srt")

    assert cli.main(["--file", str(path), "--dry-run", "--encoding", "cp1252"]) == 1

    assert seen == ["cp1252"]  # the flag reached the decoder; the stub has no cues


@pytest.mark.integration
def test_cli_stores_lines_and_is_idempotent(
    srt_fixture_path: Callable[[str], Path], subtitle_id: str, db_conn, capsys
) -> None:
    path = srt_fixture_path("basic_english.srt")
    argv = ["--subtitle-id", subtitle_id, "--file", str(path), "--json"]

    assert cli.main(argv) == 0

    first = payload_of(capsys.readouterr().out)
    assert first["status"] == "parsed"
    assert first["saved"] == BASIC_LINES
    assert first["subtitle_id"] == subtitle_id
    assert first["deleted_lines"] == 0
    assert _state(db_conn, subtitle_id) == (BASIC_LINES, "parsed")

    # A second run replaces the lines instead of appending them (decision D3).
    assert cli.main(argv) == 0

    second = payload_of(capsys.readouterr().out)
    assert second["deleted_lines"] == BASIC_LINES
    assert second["saved"] == BASIC_LINES
    assert _state(db_conn, subtitle_id) == (BASIC_LINES, "parsed")
