"""
Command line entry point for step 2: parse an .srt file into ``dialogue_lines``.

    # real ingestion
    python -m app.scripts.parse_subtitle --subtitle-id <uuid> --file ./raw/movie.en.srt

    # try the cleaner without touching the database
    python -m app.scripts.parse_subtitle --file ./raw/movie.en.srt --dry-run

    # machine readable (for the acceptance report)
    python -m app.scripts.parse_subtitle --subtitle-id <uuid> --file movie.srt --json

Connection parameters are taken from the environment (see ``app/db.py``):
SUBTITLES_DB_HOST / _PORT / _NAME / _USER / _PASSWORD / _TIMEOUT.

Output contract: stdout carries only the result (a summary in text mode, one JSON
object with ``--json``); every log record goes to stderr, so ``--json`` stays pipeable.

Exit codes: 0 on success, 1 when the pipeline failed (the orchestrator records
status='failed'), 2 on usage errors (argparse default).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any

from app.db import connection_kwargs, get_connection
from app.subtitle_parser import (
    DEFAULT_BATCH_SIZE,
    ParseReport,
    SanitizerOptions,
    SubtitleError,
    SubtitleParserService,
    process_subtitle_file_with_report,
)

logger = logging.getLogger("app.scripts.parse_subtitle")

#: How many cleaned lines ``--dry-run`` echoes back as a sanity check.
SAMPLE_LINES = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parse_subtitle",
        description="Parse an SRT subtitle file into the dialogue_lines table.",
    )
    parser.add_argument(
        "--subtitle-id",
        "--id",
        dest="subtitle_id",
        default=None,
        help="uuid of the subtitles row to fill (optional with --dry-run)",
    )
    parser.add_argument(
        "--file", "--path", dest="file_path", required=True, help="path to the .srt file"
    )
    parser.add_argument(
        "--encoding", default=None, help="force a codec instead of detection + fallback chain"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="decode, parse and clean only: no database connection at all",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print one JSON object with the run statistics instead of text",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"rows per bulk-insert statement (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--keep-music-lines",
        action="store_true",
        help="strip music markers but keep the lyrics instead of dropping the line",
    )
    parser.add_argument(
        "--keep-speaker-labels",
        action="store_true",
        help="do not strip 'WALTER:' style speaker labels",
    )
    parser.add_argument(
        "--keep-line-breaks", action="store_true", help="do not merge multi-line cues into one row"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def options_from_args(args: argparse.Namespace) -> SanitizerOptions:
    """Map CLI toggles onto sanitizer options (all default to the spec behaviour)."""
    return SanitizerOptions(
        merge_lines=not args.keep_line_breaks,
        drop_music_lines=not args.keep_music_lines,
        strip_speaker_labels=not args.keep_speaker_labels,
    )


def _stats(
    report: ParseReport, *, saved: int, dry_run: bool, sample: Sequence[str]
) -> dict[str, Any]:
    """The numeric part of the result, identical for both run modes."""
    return {
        "dry_run": dry_run,
        "saved": saved,
        "encoding": report.encoding,
        "engine": report.engine,
        "cues_total": report.cues_total,
        "cues_kept": report.cues_kept,
        "cues_dropped": report.cues_dropped,
        "deleted_lines": report.deleted_lines,
        "duration_ms": report.duration_ms,
        "sample": list(sample),
    }


def _run_dry(args: argparse.Namespace, options: SanitizerOptions) -> dict[str, Any]:
    """Decode + parse + clean only — ``get_connection`` is never called."""
    parsed = SubtitleParserService.parse_srt(args.file_path, args.encoding, options=options)
    return _stats(
        parsed.report,
        saved=len(parsed.lines),
        dry_run=True,
        sample=[line.raw_text for line in parsed.lines[:SAMPLE_LINES]],
    )


def _run_ingest(args: argparse.Namespace, options: SanitizerOptions) -> dict[str, Any]:
    """Full pipeline: the orchestrator keeps the transaction, we keep the handle."""
    kwargs = connection_kwargs()
    logger.info(
        "connecting to host=%s port=%s db=%s user=%s",
        kwargs["host"],
        kwargs["port"],
        kwargs["dbname"],
        kwargs["user"],
    )
    # ``app.db.session()`` would commit on our behalf and the psycopg2 ``with conn``
    # form commits but never closes; contextlib.closing gives just the cleanup.
    with contextlib.closing(get_connection()) as conn:
        saved, report = process_subtitle_file_with_report(
            conn,
            args.subtitle_id,
            args.file_path,
            encoding=args.encoding,
            options=options,
            page_size=args.page_size,
        )
    return _stats(report, saved=saved, dry_run=False, sample=())


def _emit(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    """Print exactly one result block and map the status onto an exit code."""
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload["status"] != "error" else 1

    if payload["status"] == "error":
        print(f"FAILED: {payload['error']}")
        return 1

    verb = "would be stored" if payload["dry_run"] else "stored"
    target = args.subtitle_id or args.file_path
    print(f"OK: {payload['saved']} dialogue lines {verb} for subtitle {target}")
    print(
        f"encoding={payload['encoding']} engine={payload['engine']} "
        f"cues={payload['cues_total']} kept={payload['cues_kept']} "
        f"dropped={payload['cues_dropped']} replaced={payload['deleted_lines']} "
        f"duration={payload['duration_ms']}ms"
    )
    for index, text in enumerate(payload["sample"], start=1):
        print(f"  {index:>3} | {text}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stderr,
    )
    if not args.dry_run and not args.subtitle_id:
        parser.error("--subtitle-id is required unless --dry-run is given")

    options = options_from_args(args)
    payload: dict[str, Any] = {
        "subtitle_id": args.subtitle_id,
        "file": args.file_path,
        "dry_run": bool(args.dry_run),
    }
    try:
        run = _run_dry if args.dry_run else _run_ingest
        stats = run(args, options)
    except SubtitleError as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        payload.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
    except Exception:
        logger.exception("subtitle run failed")
        payload.update({"status": "error", "error": "unexpected failure, see the log output"})
    else:
        payload.update({"status": "dry-run" if args.dry_run else "parsed", **stats})

    return _emit(args, payload)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
