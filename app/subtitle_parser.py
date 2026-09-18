"""
Subtitle parser and cleaner — SRT → PostgreSQL ingestion (pipeline step 2).
================================================================================

The module is deliberately split into three independently usable layers:

``SubtitleSanitizer`` / :func:`clean_subtitle_text`
    Turns a raw subtitle cue into publishable speech text: drops HTML/ASS markup,
    hearing-impaired (SDH) sound cues, speaker labels, dialogue dashes, music
    markers and typographic noise.

``SubtitleParserService``
    Encoding-resilient decoding plus SRT parsing into :class:`DialogueLine` DTOs.

``process_subtitle_file``
    Transactional orchestrator: ``processing`` → parse → bulk insert (500 rows per
    batch) → ``parsed`` → ``commit``; on failure it rolls back, records ``failed``
    out of band and re-raises with the traceback in the log.

Target schema (see ``openspec/bd.md`` / ``docker/init.sql``)::

    dialogue_lines(id, subtitle_id, line_index, start_time_ms, end_time_ms, raw_text, created_at)
    subtitles(id, ..., status subtitle_status_enum, ...)

Typical usage::

    from app.db import get_connection
    from app.subtitle_parser import process_subtitle_file

    with get_connection() as conn:
        saved = process_subtitle_file(conn, subtitle_id, "raw/movie.en.srt")

Only ``psycopg2`` is mandatory. ``pysrt`` (nicer timecode errors) and ``chardet``
(better encoding guess) are used when importable — otherwise the built-in
zero-dependency SRT parser and the fallback decoding chain take over.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, NamedTuple

from psycopg2.extras import execute_batch

try:  # pragma: no cover - depends on the deployment image
    import chardet
except ImportError:  # pragma: no cover
    chardet = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ─── Statuses of subtitles.status (subtitle_status_enum) ──────────────────────

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_PARSED = "parsed"
STATUS_FAILED = "failed"

#: ``psycopg2`` binds Python ``str`` as ``text``, so enum columns need an explicit cast.
#: Named placeholder only — mixing ``%s`` and ``%(name)s`` in one statement is invalid.
STATUS_CAST = "%(status)s::subtitle_status_enum"

DEFAULT_BATCH_SIZE = 500  # page_size of psycopg2.extras.execute_batch
DEFAULT_CUE_DURATION_MS = 1000  # substituted when a cue has end <= start
MAX_FILE_BYTES = 10 * 1024 * 1024  # an .srt is ~30 KB; anything above 10 MB is a red flag

# Decoder fallback chain. utf-8-sig comes first: it strips a leading BOM and
# behaves exactly like utf-8 for files without one. cp1252 precedes cp1251
# because the corpus is English (see docker/data/en_50k.txt); pass
# encoding= explicitly for Cyrillic tracks.
FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1252", "cp1251", "latin-1")


# ─── Exceptions ───────────────────────────────────────────────────────────────


class SubtitleError(Exception):
    """Base class for every recoverable error of the subtitle pipeline."""


class SubtitleFileError(SubtitleError):
    """The file is missing, unreadable, oversized or not a subtitle file."""


class SubtitleNotFoundError(SubtitleError):
    """There is no ``subtitles`` row with the given id."""


class LineCountMismatchError(SubtitleError):
    """Fewer rows landed in ``dialogue_lines`` than were parsed."""


# Kept as (str, Enum) rather than StrEnum: StrEnum would change this API, and psycopg2
# adapts a str subclass by its value, so SQL sees 'parsed' and not the member name.
class SubtitleStatus(str, Enum):  # noqa: UP042
    """Values of the PostgreSQL enum ``subtitle_status_enum``."""

    PENDING = "pending"
    PROCESSING = "processing"
    PARSED = "parsed"
    FAILED = "failed"


# ─── Sanitization: regular expressions ────────────────────────────────────────

# XML entities produced by subtitle sites. They are decoded first, because a
# literal "&quot;" swallows the '>' of a tag and breaks RE_HTML_TAGS.
# Deliberately NOT decoded: &lt; / &gt; — they would forge fake tags.
_ENTITY_MAP: Mapping[str, str] = {
    "&quot;": '"',
    "&#34;": '"',
    "&#x22;": '"',
    "&apos;": "'",
    "&#39;": "'",
    "&#x27;": "'",
    "&nbsp;": " ",
    "&#160;": " ",
    "&hellip;": "...",
    "&mdash;": "-",
    "&ndash;": "-",
    "&#8211;": "-",
    "&#8212;": "-",
    "&lsquo;": "'",
    "&rsquo;": "'",
    "&ldquo;": '"',
    "&rdquo;": '"',
    "&amp;": "&",
}
RE_ENTITIES = re.compile("|".join(map(re.escape, _ENTITY_MAP)), re.IGNORECASE)

# Non-breaking/thin spaces, curly quotes, guillemets, en/em dash, ellipsis → ASCII.
_TYPOGRAPHY: Mapping[str, str] = {
    "\u00a0": " ",
    "\u2007": " ",
    "\u202f": " ",
    "\u2009": " ",
    "\ufeff": "",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u2026": "...",
}
RE_TYPOGRAPHY = re.compile("|".join(map(re.escape, _TYPOGRAPHY)))
RE_FORMAT_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029]")

# 1. HTML/XML markup: <i>, </b>, <font color="#E5E5E5">, <br />. Markup inside a
#    word is removed without a replacement ("hot<b>dog</b>" stays "hotdog"), so
#    line-break tags are turned into a real newline first, then merged in step 8.
RE_HTML_LINE_BREAK = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
RE_HTML_TAGS = re.compile(r"<[^>]+>", re.IGNORECASE)

# 2. SubStation Alpha overrides: {\an8}, {\pos(20,28)}, {\b1}. Applied twice so
#    "{\an8}text{\an2}" does not leave a stray brace behind. The hard line break "\N"
#    survives an ASS→SRT conversion as two literal characters, so it becomes a real
#    newline here and is merged like any other multi-line cue (D18).
RE_SSA_TAGS = re.compile(r"\{[^{}]*\}")
RE_SSA_SOFT_BREAK = re.compile(r"\\N")

# 3. SDH cues in square brackets: [door creaks], [suspenseful music plays].
RE_BRACKETS_SDH = re.compile(r"\[[^\]\n]*\]")
RE_BRACKET_DANGLING_OPEN = re.compile(r"\[[^\]\n]*$")  # unclosed noise at the cue tail

# 4. SDH cues in parentheses — ALL-CAPS only, so "(together) We can do it" survives.
RE_PAREN_CONTENT = re.compile(r"\(([^()]*)\)")
RE_HAS_UPPER = re.compile(r"[A-Z]")
# (No. 5), (U.S.A.), (Mr.) are abbreviations, not sound effects.
RE_ABBREV_TAIL = re.compile(
    r"(?:^|[\s.])(?:[A-Z]{1,3}|Mr|Mrs|Ms|Dr|St|No|Jr|Sr|vs|etc|Inc|Ltd|Co|Vol|approx)$",
    re.IGNORECASE,
)

# 5. Speaker labels: "WALTER:", "OFFICER 1:", "MAN ON TV:", "DR. SMITH:" and the
#    label alone on its line ("COACH:" followed by the text). An apostrophe is
#    allowed inside the label so "DON'T:" does not degrade into "t".
_LABEL_BODY = r"[A-Z][A-Z0-9 ._'-]{1,24}"
RE_SPEAKER_LABEL = re.compile(rf"^{_LABEL_BODY}:[ \t]+|^{_LABEL_BODY}:[ \t]*$", re.MULTILINE)

# 6. Music. A musical glyph on either edge marks a *sung* line; inner symbols are
#    always noise. A "#" belongs to speech unless it frames the whole line
#    ("# Silent night #"): "#13-37" is a number and "#blessed" is a hashtag, so
#    dropping a line that merely starts with "#" would throw away real dialogue (D18).
_MUSIC_CHARS = "\u266a\u266b\u266c\u2669\U0001f3b5\U0001f3b6\u00b6"  # ♪ ♫ ♬ ♩ 🎵 🎶 ¶
_MUSIC_CLASS = f"[{re.escape(_MUSIC_CHARS)}]"
_MUSIC_ALT = f"{_MUSIC_CLASS}+|#(?![0-9])"
RE_MUSIC_INNER = re.compile(_MUSIC_CLASS)
RE_NOTE_EDGE = re.compile(rf"^\s*{_MUSIC_CLASS}+\s*|\s+{_MUSIC_CLASS}+\s*$")
RE_EDGE_MARKERS = re.compile(rf"^\s*(?:{_MUSIC_ALT})\s*|\s+(?:{_MUSIC_ALT})\s*$")
RE_HASHTAG_FRAME = re.compile(r"^\s*#+(?![0-9])\s.+\s#+(?![0-9])\s*$")
RE_HASHTAG_NOISE = re.compile(r"^\s*#+(?![0-9])\s*|\s+#+(?![0-9])\s*$")

# 7. Dialogue dash at line edges: "- Hey.", "—Hey.", ">> Come here.".
#    Quote characters are deliberately NOT stripped here (they carry meaning:
#    '"Fire off." "Pull trigger."'), and so is "..." (an unfinished thought is
#    valuable for step 3). They are removed only by the "no alnum" rule below.
RE_LEADING_DASH = re.compile(r"^(?:[-\u2013\u2014]+|>+)\s*")
RE_TRAILING_DASH = re.compile(r"\s*(?:[-\u2013\u2014]+)$")

# 8. A line without a single letter or digit carries no speech.
RE_HAS_ALNUM = re.compile(r"[0-9A-Za-z\u00c0-\u024f\u0400-\u04ff\u4e00-\u9fff]")
RE_MULTI_WS = re.compile(r"\s+")
RE_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")

# 9. Guards for the ALL-CAPS parenthesis rule.
MAX_SDH_WORDS = 4  # longer uppercase runs are shouting, not SFX
MAX_SDH_CHARS = 32
PAREN_KEEP_TOKENS = frozenset(
    {
        "us",
        "usa",
        "uk",
        "un",
        "eu",
        "nato",
        "tv",
        "dvd",
        "cd",
        "lp",
        "hq",
        "phd",
        "md",
        "esq",
        "jr",
        "sr",
        "mr",
        "mrs",
        "ms",
        "dr",
        "st",
        "vs",
        "etc",
        "approx",
        "dna",
        "rna",
        "gdp",
        "iq",
        "id",
        "ok",
        "ap",
        "bc",
        "ad",
        "fbi",
        "cia",
        "kgb",
    }
)


@dataclass(frozen=True)
class SanitizerOptions:
    """Toggles for :class:`SubtitleSanitizer`. Defaults follow the step-2 spec."""

    #: Join multi-line cues with a single space (True) or keep newlines (False).
    merge_lines: bool = True
    #: Drop a line framed by music markers (True) or strip markers and keep lyrics.
    drop_music_lines: bool = True
    #: Strip leading speaker labels such as "WALTER:".
    strip_speaker_labels: bool = True
    #: Strip dialogue dashes at line edges.
    strip_dialogue_dashes: bool = True

    def replaced(self, **changes: Any) -> SanitizerOptions:
        """Return a copy with the given fields overridden (the dataclass is frozen)."""
        return SanitizerOptions(**{**self.__dict__, **changes})


class SubtitleSanitizer:
    """Removes everything that is not spoken dialogue from a subtitle cue."""

    def __init__(self, options: SanitizerOptions | None = None) -> None:
        self.opts = options or SanitizerOptions()

    # ------------------------------------------------------------ inner helpers

    @staticmethod
    def _decode_entity(match: re.Match[str]) -> str:
        return _ENTITY_MAP.get(match.group(0).lower(), match.group(0))

    @staticmethod
    def _is_sdh_paren(content: str) -> bool:
        """Tell a sound cue ``(CHUCKLES)`` from real speech such as ``(together)``.

        Conservative on purpose: anything containing a lowercase letter, sentence
        punctuation, an abbreviation tail or more than four words is kept, because
        false deletion costs more for NLP than a leftover noise tag.
        """
        inner = content.strip()
        if not inner:
            return False
        if any(ch.islower() for ch in inner):  # (together), (now!)
            return False
        if not RE_HAS_UPPER.search(inner):  # (1999), (...)
            return False
        if len(inner) > MAX_SDH_CHARS:
            return False
        if len(inner.split()) > MAX_SDH_WORDS:
            return False
        if any(ch in inner for ch in ".?!"):  # (NO!) — a shout, not an SFX
            return False
        if RE_ABBREV_TAIL.search(inner):  # (No. 5), (U.S.A.)
            return False
        return inner.strip(". ").lower() not in PAREN_KEEP_TOKENS

    def _strip_paren(self, match: re.Match[str]) -> str:
        """RE_PAREN_CONTENT callable: blank SDH cues, keep everything else."""
        content = match.group(1)
        if not content.strip():
            return " "
        return " " if self._is_sdh_paren(content) else match.group(0)

    @staticmethod
    def _strip_speaker_label(line: str) -> str:
        """Remove a ``NAME:`` / ``OFFICER 1:`` / lone ``COACH:`` prefix.

        Timecodes cannot be confused with labels: the pattern starts with a capital
        letter and requires the colon to be the character right after the label, so
        "12:30" (digit first) and "CHAPTER 12:30" (no space after the first colon)
        are left alone — which is also why "OFFICER 1:" is stripped correctly.
        """
        match = RE_SPEAKER_LABEL.match(line)
        if not match:
            return line
        return line[match.end() :].strip()

    def _clean_line(self, text: str) -> str:
        """Apply line-scoped rules. Returns "" when the line carries no speech."""
        line = text.strip()
        if not line:
            return ""

        # A line framed by music markers is a sung line, i.e. not dialogue.
        if RE_NOTE_EDGE.search(line) or RE_HASHTAG_FRAME.match(line):
            if self.opts.drop_music_lines:
                return ""
            line = RE_EDGE_MARKERS.sub(" ", line)

        line = RE_MUSIC_INNER.sub(" ", line)
        line = RE_HASHTAG_NOISE.sub("", line).strip()

        if self.opts.strip_speaker_labels:
            line = self._strip_speaker_label(line).strip()

        if self.opts.strip_dialogue_dashes:
            line = RE_LEADING_DASH.sub("", line).strip()
            line = RE_TRAILING_DASH.sub("", line).strip()

        line = RE_MULTI_WS.sub(" ", line).strip()
        if not line or not RE_HAS_ALNUM.search(line):
            return ""
        return line

    # ---------------------------------------------------------------- public API

    def clean(self, text: str) -> str:
        """Clean one subtitle cue.

        Returns ``""`` when only noise is left, so the caller drops the cue
        entirely instead of storing an empty ``raw_text``.
        """
        if not text or not text.strip():
            return ""

        # 0. Control characters, XML entities, typography.
        t = RE_FORMAT_CHARS.sub("", text)
        t = RE_ENTITIES.sub(self._decode_entity, t)
        t = RE_TYPOGRAPHY.sub(lambda m: _TYPOGRAPHY[m.group(0)], t)

        # 1/2. Markup — tags before bracketed cues, otherwise <b>[MUSIC]</b>
        #      leaves "<b></b>" behind.
        t = RE_HTML_LINE_BREAK.sub("\n", t)
        t = RE_SSA_SOFT_BREAK.sub("\n", t)
        t = RE_HTML_TAGS.sub("", t)
        t = RE_SSA_TAGS.sub(" ", t)
        t = RE_SSA_TAGS.sub(" ", t)

        # 3/4. Non-speech insertions.
        t = RE_BRACKETS_SDH.sub(" ", t)
        t = RE_BRACKET_DANGLING_OPEN.sub(" ", t)
        t = RE_PAREN_CONTENT.sub(self._strip_paren, t)

        # 5..8. Line-scoped rules, then re-flow the cue.
        kept = [
            cleaned for cleaned in (self._clean_line(line) for line in t.splitlines()) if cleaned
        ]
        separator = " " if self.opts.merge_lines else "\n"
        result = (
            RE_MULTI_WS.sub(" ", separator.join(kept))
            if self.opts.merge_lines
            else separator.join(kept)
        )
        result = RE_SPACE_BEFORE_PUNCT.sub(r"\1", result)
        return result.strip()

    def clean_subtitle_text(self, text: str) -> str:
        """Alias of :meth:`clean` (instance-friendly name)."""
        return self.clean(text)


_DEFAULT_SANITIZER = SubtitleSanitizer()


def clean_subtitle_text(text: str) -> str:
    """Clean a single cue with the default options (see :class:`SanitizerOptions`)."""
    return _DEFAULT_SANITIZER.clean(text)


# ─── Parsing ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DialogueLine:
    """One cleaned, ready-to-insert cue (``raw_text`` is already sanitized)."""

    line_index: int
    start_time_ms: int
    end_time_ms: int
    raw_text: str

    @property
    def duration_ms(self) -> int:
        return self.end_time_ms - self.start_time_ms

    def as_row(self, subtitle_id: str) -> dict[str, Any]:
        """Row for the named-placeholder bulk insert (id/created_at are DB defaults)."""
        return {
            "subtitle_id": subtitle_id,
            "line_index": self.line_index,
            "start_time_ms": self.start_time_ms,
            "end_time_ms": self.end_time_ms,
            "raw_text": self.raw_text,
        }


class CueRead(NamedTuple):
    """Raw cue as read from the file, before sanitization."""

    ordinal: int
    start_ms: int
    end_ms: int
    text: str


class ParseReport(NamedTuple):
    """Metrics of one ingestion run (logging + acceptance reports)."""

    cues_total: int = 0
    cues_kept: int = 0
    cues_dropped: int = 0
    engine: str = "unknown"
    encoding: str = "unknown"
    duration_ms: float = 0.0
    deleted_lines: int = 0


class ParsedSrt(NamedTuple):
    """Result of :meth:`SubtitleParserService.parse_srt`."""

    lines: Sequence[DialogueLine]
    report: ParseReport


def _timecode_to_ms(hours: int, minutes: int, seconds: int, millis: int) -> int:
    """``HH:MM:SS,mmm`` → integer milliseconds."""
    return ((hours * 3600 + minutes * 60 + seconds) * 1000) + millis


def _srt_time_to_ms(timecode: str) -> int:
    """Parse one SRT time token.

    Tolerant of ``,``/``.`` as the millisecond separator, 1-3 digit ms and a
    missing hours field (``MM:SS,mmm`` files exist in the wild).
    """
    token = timecode.strip().replace(".", ",")
    parts = token.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, rest = parts
        elif len(parts) == 2:  # MM:SS,mmm
            hours, minutes, rest = "0", parts[0], parts[1]
        else:
            raise ValueError(f"unrecognized timecode {timecode!r}")
        seconds, _, millis = rest.partition(",")
        return _timecode_to_ms(
            int(hours), int(minutes), int(seconds), int((millis or "0").ljust(3, "0")[:3])
        )
    except ValueError as exc:
        raise SubtitleFileError(f"Invalid SRT timecode {timecode!r}") from exc


def _sub_rip_time_to_ms(value: Any) -> int:
    """Convert ``pysrt.SubRipTime`` (or any ``timedelta``-like) to milliseconds."""
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        return round(float(total_seconds()) * 1000)
    hours = getattr(value, "hours", 0)
    minutes = getattr(value, "minutes", 0)
    seconds = getattr(value, "seconds", 0)
    millis = getattr(value, "milliseconds", 0)
    return _timecode_to_ms(hours, minutes, seconds, millis)


# Zero-dependency fallback parser: optional index line, timecode line, then the
# text block up to the next cue or EOF. Tolerates CRLF/CR/LF, "." as the
# millisecond separator and files without numbering.
_TIME_TOKEN = r"\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?"
RE_CUE_BLOCK = re.compile(
    rf"[ \t]*(?P<index>\d+)[ \t]*\r?\n?[ \t]*"
    rf"(?P<start>{_TIME_TOKEN})[ \t]*--+>[ \t]*(?P<end>{_TIME_TOKEN})[ \t]*\r?\n"
    rf"(?P<text>.*?)(?=[ \t]*\r?\n[ \t]*\r?\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)


class SubtitleParserService:
    """Decoding, parsing and persisting of subtitle tracks."""

    sanitizer = SubtitleSanitizer()

    # ------------------------------------------------------------------- reading

    @staticmethod
    def _check_file(file_path: str) -> int:
        if not os.path.exists(file_path):
            raise SubtitleFileError(f"SRT file not found: {file_path!r}")
        size = os.path.getsize(file_path)
        if size > MAX_FILE_BYTES:
            raise SubtitleFileError(
                f"{file_path!r} is {size} bytes — above the {MAX_FILE_BYTES} byte safety limit"
            )
        return size

    @staticmethod
    def _sniff_encoding(raw: bytes) -> str | None:
        """Ask ``chardet`` (if installed) and trust it only above 70% confidence."""
        if chardet is None or not raw:
            return None
        try:
            guess = chardet.detect(raw[:64_000])
        except Exception:  # chardet must never break ingestion
            logger.debug("chardet failed", exc_info=True)
            return None
        encoding = guess.get("encoding")
        confidence = guess.get("confidence") or 0.0
        if not encoding or confidence < 0.7:
            return None
        normalized = str(encoding).lower()
        return {
            "windows-1252": "cp1252",
            "windows-1251": "cp1251",
            "iso-8859-1": "latin-1",
            "ascii": "utf-8",
        }.get(normalized, normalized)

    @classmethod
    def _read_file_with_fallback(cls, file_path: str) -> tuple[str, str]:
        """Decode an .srt file trying UTF-8, UTF-8-BOM, CP1252, CP1251, Latin-1.

        Returns ``(content, encoding_used)``. Binary read + explicit decode keeps
        the fallback order independent of the machine locale.
        """
        cls._check_file(file_path)
        with open(file_path, "rb") as handle:
            raw = handle.read()

        if not raw.strip():
            raise SubtitleFileError(f"Subtitle file {file_path!r} is empty")

        sniffed = cls._sniff_encoding(raw)
        chain = ([sniffed] if sniffed else []) + [e for e in FALLBACK_ENCODINGS if e != sniffed]
        for encoding in chain:
            try:
                return raw.decode(encoding), encoding
            except (UnicodeDecodeError, LookupError):
                continue

        # Last resort: never fail on encoding alone, but flag it loudly.
        logger.warning("%s: undecodable bytes; decoding as utf-8 with replacement", file_path)
        return raw.decode("utf-8", errors="replace"), "utf-8(replace)"

    @classmethod
    def read_file(cls, file_path: str, encoding: str | None = None) -> tuple[str, str]:
        """Public decoding entry point; ``encoding`` forces a single codec."""
        if encoding:
            cls._check_file(file_path)
            with open(file_path, "rb") as handle:
                return handle.read().decode(encoding), encoding
        return cls._read_file_with_fallback(file_path)

    # ------------------------------------------------------------------- parsing

    @staticmethod
    def _extract_cues_pysrt(content: str) -> Sequence[CueRead]:
        """Parse with pysrt (raises on malformed files — the caller falls back)."""
        import pysrt  # imported lazily: optional dependency

        return [
            CueRead(
                ordinal=index,
                start_ms=_sub_rip_time_to_ms(sub.start),
                end_ms=_sub_rip_time_to_ms(sub.end),
                text=sub.text or "",
            )
            for index, sub in enumerate(pysrt.from_string(content), start=1)
        ]

    @staticmethod
    def _extract_cues_regex(content: str) -> Sequence[CueRead]:
        """Fallback SRT parser used when ``pysrt`` is absent or chokes on the file."""
        cues: list[CueRead] = []
        for match in RE_CUE_BLOCK.finditer(content):
            try:
                start_ms = _srt_time_to_ms(match.group("start"))
                end_ms = _srt_time_to_ms(match.group("end"))
            except SubtitleFileError:
                logger.warning("skipping cue with an unreadable timecode: %r", match.group(0)[:60])
                continue
            cues.append(CueRead(len(cues) + 1, start_ms, end_ms, match.group("text") or ""))
        return cues

    @classmethod
    def _extract_cues(cls, content: str) -> tuple[Sequence[CueRead], str]:
        """Return ``(cues, engine_name)``; pysrt first, built-in parser as backup."""
        try:
            cues: Sequence[CueRead] = cls._extract_cues_pysrt(content)
        except ImportError:  # pragma: no cover - depends on the deployment env
            logger.debug("pysrt is not installed; using the built-in SRT parser")
            cues = ()
        except Exception:
            logger.warning(
                "pysrt rejected the file, retrying with the built-in parser", exc_info=True
            )
            cues = ()
        if cues:
            return cues, "pysrt"

        # pysrt reports a file it cannot understand as *zero cues* instead of
        # raising, so an empty result goes through the regex parser before the
        # payload is refused outright.
        cues = cls._extract_cues_regex(content)
        if not cues:
            raise SubtitleFileError("No cues found — is this really an .srt file?")
        return cues, "regex"

    @classmethod
    def parse_cues(
        cls,
        cues: Iterable[CueRead],
        *,
        engine: str = "unknown",
        encoding: str = "unknown",
        started_at: datetime | None = None,
        sanitizer: SubtitleSanitizer | None = None,
    ) -> tuple[Sequence[DialogueLine], ParseReport]:
        """Sanitize cues and renumber the survivors (gap-free ``line_index``).

        Cues whose text collapses to an empty string after cleaning are dropped,
        which is exactly what "if resulting text is empty, drop the cue" means.
        """
        active_sanitizer = sanitizer or cls.sanitizer
        lines: list[DialogueLine] = []
        total = 0

        for cue in cues:
            total += 1
            cleaned = active_sanitizer.clean(cue.text)
            if not cleaned:
                continue

            if cue.end_ms <= cue.start_ms:
                logger.warning(
                    "cue %d has end<=start (%d<=%d); forcing %d ms duration",
                    cue.ordinal,
                    cue.end_ms,
                    cue.start_ms,
                    DEFAULT_CUE_DURATION_MS,
                )
                end_ms = cue.start_ms + DEFAULT_CUE_DURATION_MS
            else:
                end_ms = cue.end_ms

            lines.append(
                DialogueLine(
                    line_index=len(lines) + 1,
                    start_time_ms=cue.start_ms,
                    end_time_ms=end_ms,
                    raw_text=cleaned,
                )
            )

        elapsed = 0.0
        if started_at is not None:
            elapsed = (datetime.now(UTC) - started_at).total_seconds() * 1000

        report = ParseReport(
            cues_total=total,
            cues_kept=len(lines),
            cues_dropped=total - len(lines),
            engine=engine,
            encoding=encoding,
            duration_ms=round(elapsed, 1),
        )
        logger.info(
            "parsed %d/%d cues (engine=%s encoding=%s dropped=%d) in %.1f ms",
            report.cues_kept,
            report.cues_total,
            engine,
            encoding,
            report.cues_dropped,
            elapsed,
        )
        return lines, report

    @classmethod
    def parse_srt_string(
        cls,
        content: str,
        *,
        started_at: datetime | None = None,
        options: SanitizerOptions | None = None,
    ) -> tuple[Sequence[DialogueLine], ParseReport]:
        """Parse already-decoded SRT text into sanitized dialogue lines."""
        cues, engine = cls._extract_cues(content)
        sanitizer = SubtitleSanitizer(options) if options else None
        return cls.parse_cues(cues, engine=engine, started_at=started_at, sanitizer=sanitizer)

    @classmethod
    def parse_srt(
        cls,
        file_path: str,
        encoding: str | None = None,
        *,
        options: SanitizerOptions | None = None,
    ) -> ParsedSrt:
        """Parse an .srt file into :class:`DialogueLine` records (no DB access)."""
        started_at = datetime.now(UTC)
        content, used_encoding = cls.read_file(file_path, encoding)
        cues, engine = cls._extract_cues(content)
        sanitizer = SubtitleSanitizer(options) if options else None
        lines, report = cls.parse_cues(
            cues,
            engine=engine,
            encoding=used_encoding,
            started_at=started_at,
            sanitizer=sanitizer,
        )
        return ParsedSrt(lines=lines, report=report)

    @classmethod
    def parse_file(cls, file_path: str, encoding: str | None = None) -> list[DialogueLine]:
        """Convenience wrapper returning parsed lines only."""
        lines, _ = cls.parse_srt(file_path, encoding)
        return list(lines)

    @classmethod
    def process_and_save(cls, conn: Any, subtitle_id: str, file_path: str, **kwargs: Any) -> int:
        """Alias of :func:`process_subtitle_file`, kept for the documented API."""
        return process_subtitle_file(conn, subtitle_id, file_path, **kwargs)


# ─── Repository: SQL as module constants, always parameterized ────────────────

SQL_SET_STATUS = f"""
    UPDATE subtitles
       SET status = {STATUS_CAST},
           updated_at = NOW()
     WHERE id = %(subtitle_id)s::uuid
"""

# FOR UPDATE both checks existence and serializes two workers on one track.
SQL_REQUIRE_SUBTITLE = """
    SELECT id
      FROM subtitles
     WHERE id = %(subtitle_id)s::uuid
       FOR UPDATE
"""

SQL_DELETE_DIALOGUE_LINES = """
    DELETE FROM dialogue_lines
     WHERE subtitle_id = %(subtitle_id)s::uuid
"""

SQL_COUNT_DIALOGUE_LINES = """
    SELECT COUNT(*)
      FROM dialogue_lines
     WHERE subtitle_id = %(subtitle_id)s::uuid
"""

# id / created_at are omitted on purpose: gen_random_uuid() and NOW() are the
# column defaults. Passing them as %s parameters would store the *literal* text
# "gen_random_uuid()" instead of calling the function.
SQL_INSERT_DIALOGUE_LINES = """
    INSERT INTO dialogue_lines
        (subtitle_id, line_index, start_time_ms, end_time_ms, raw_text)
    VALUES
        (%(subtitle_id)s::uuid, %(line_index)s, %(start_time_ms)s, %(end_time_ms)s, %(raw_text)s)
"""


def _status_value(status: Any) -> str:
    """Plain string for an enum member, constant or ``str`` subclass.

    ``str(SubtitleStatus.PROCESSING)`` is not reliable across Python versions
    (3.11 changed ``Enum.__str__``), so the ``value`` attribute is preferred.
    """
    return str(getattr(status, "value", status))


def require_subtitle(cursor: Any, subtitle_id: str) -> None:
    """Fail fast (and keep the transaction usable) when the row does not exist.

    Without this check a bad uuid raises ``ForeignKeyViolation`` and aborts the
    transaction, so even the ``failed`` status could not be written afterwards.
    """
    cursor.execute(SQL_REQUIRE_SUBTITLE, {"subtitle_id": subtitle_id})
    if cursor.fetchone() is None:
        raise SubtitleNotFoundError(f"subtitles row {subtitle_id!r} does not exist")


def set_status(cursor: Any, subtitle_id: str, status: Any) -> int:
    """Update ``subtitles.status``; returns ``rowcount`` (0 means the row is gone)."""
    cursor.execute(SQL_SET_STATUS, {"status": _status_value(status), "subtitle_id": subtitle_id})
    return int(cursor.rowcount or 0)


def delete_dialogue_lines(cursor: Any, subtitle_id: str) -> int:
    """Idempotency guard: dialogue_lines has no UNIQUE (subtitle_id, line_index)."""
    cursor.execute(SQL_DELETE_DIALOGUE_LINES, {"subtitle_id": subtitle_id})
    return int(cursor.rowcount or 0)


def count_dialogue_lines(cursor: Any, subtitle_id: str) -> int:
    """Rows currently stored for the track (used to verify the bulk insert)."""
    cursor.execute(SQL_COUNT_DIALOGUE_LINES, {"subtitle_id": subtitle_id})
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def insert_dialogue_lines(
    cursor: Any,
    subtitle_id: str,
    lines: Sequence[DialogueLine],
    *,
    page_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Bulk-insert parsed lines via ``execute_batch`` (``page_size`` rows per statement)."""
    rows = [line.as_row(subtitle_id) for line in lines]
    if not rows:
        return 0
    execute_batch(cursor, SQL_INSERT_DIALOGUE_LINES, rows, page_size=page_size)
    return len(rows)


def _open_fresh_connection() -> Any:
    """New connection for out-of-band status writes (lazy import avoids a cycle)."""
    from app.db import get_connection

    return get_connection()


# ─── Orchestration ────────────────────────────────────────────────────────────


def _process_subtitle_run(
    conn: Any,
    subtitle_id: str,
    file_path: str,
    *,
    encoding: str | None = None,
    options: SanitizerOptions | None = None,
    page_size: int = DEFAULT_BATCH_SIZE,
    durable_status: bool = True,
    failure_conn: Any = None,
    started_at: datetime | None = None,
) -> tuple[int, ParseReport]:
    """Parse ``file_path`` and store its cues for ``subtitle_id``.

    Flow (spec §5):
        a. ``subtitles.status = 'processing'`` (existence checked with ``FOR UPDATE``)
        b. decode + parse + sanitize
        c. bulk ``INSERT`` into ``dialogue_lines`` (500 rows per batch), preceded by a
           ``DELETE`` of this track's lines so re-runs stay idempotent
        d. ``subtitles.status = 'parsed'``
        e. ``commit``
        f. on any error: rollback, record ``failed`` on a separate connection,
           log the traceback and re-raise.

    Args:
        conn: open ``psycopg2`` connection with autocommit disabled.
        subtitle_id: PK of the ``subtitles`` row.
        file_path: path to the ``.srt`` file.
        encoding: force one codec instead of the detection/fallback chain.
        options: sanitizer toggles; ``None`` means the spec defaults.
        page_size: rows per bulk-insert statement.
        durable_status: commit step (a) immediately, so other sessions observe
            ``processing`` while a large file is parsed. Set False to keep the
            whole run inside a single transaction.
        failure_conn: pre-made connection for the ``failed`` write (used by tests).
        started_at: timing origin override (tests).

    Returns:
        ``(rows_saved, report)``: rows written plus the statistics of this run.

    Raises:
        SubtitleFileError: missing/empty/oversized file, no cues, bad timecodes.
        SubtitleNotFoundError: no such ``subtitles`` row.
        LineCountMismatchError: stored row count differs from the parsed count.
        Exception: anything the driver raises — after ``failed`` was recorded.
    """
    started_at = started_at or datetime.now(UTC)
    service = SubtitleParserService()
    report = ParseReport(engine="unknown", encoding="unknown")

    try:
        # (a) Existence check first: a missing row must not become a FK violation
        #     that aborts the transaction and blocks the 'failed' status.
        with conn.cursor() as cursor:
            require_subtitle(cursor, subtitle_id)
            if set_status(cursor, subtitle_id, STATUS_PROCESSING) == 0:
                raise SubtitleNotFoundError(f"subtitles row {subtitle_id!r} disappeared mid-run")
        if durable_status:
            conn.commit()  # 'processing' becomes visible while the file is parsed

        content, used_encoding = service.read_file(file_path, encoding)
        cues, engine = service._extract_cues(content)
        sanitizer = SubtitleSanitizer(options) if options else SubtitleSanitizer()
        lines, report = service.parse_cues(
            cues,
            engine=engine,
            encoding=used_encoding,
            started_at=started_at,
            sanitizer=sanitizer,
        )

        if not lines:
            logger.warning(
                "%s: nothing but noise in %d cue(s) for subtitle %s",
                file_path,
                report.cues_total,
                subtitle_id,
            )

        with conn.cursor() as cursor:
            deleted = delete_dialogue_lines(cursor, subtitle_id)
            insert_dialogue_lines(cursor, subtitle_id, lines, page_size=page_size)
            stored = count_dialogue_lines(cursor, subtitle_id)
            if stored != len(lines):
                raise LineCountMismatchError(
                    f"parsed {len(lines)} lines but {stored} rows are stored "
                    f"for subtitle {subtitle_id}"
                )
            set_status(cursor, subtitle_id, STATUS_PARSED)

        report = report._replace(deleted_lines=deleted)
        conn.commit()
        logger.info(
            "saved %d dialogue line(s) for subtitle %s (encoding=%s engine=%s cues=%d "
            "dropped=%d replaced=%d took=%.1f ms)",
            len(lines),
            subtitle_id,
            report.encoding,
            report.engine,
            report.cues_total,
            report.cues_dropped,
            report.deleted_lines,
            report.duration_ms,
        )
        return len(lines), report

    except Exception:
        logger.exception("failed to process subtitle %s from %r", subtitle_id, file_path)
        try:
            conn.rollback()  # discard every dialogue_lines write of the aborted run
        except Exception:  # pragma: no cover - a dead connection cannot be rescued
            logger.warning("rollback failed for subtitle %s", subtitle_id, exc_info=True)

        # 'failed' has to survive the rollback, hence its own connection/transaction.
        owns_status_conn = False
        status_conn = failure_conn
        if status_conn is None:
            try:
                status_conn = _open_fresh_connection()
                owns_status_conn = True
            except Exception:
                logger.warning("cannot open a connection to record status='failed'")
                status_conn = None

        if status_conn is not None:
            try:
                with status_conn.cursor() as cursor:
                    updated = set_status(cursor, subtitle_id, STATUS_FAILED)
                status_conn.commit()
                if updated == 0:
                    logger.warning("status='failed' not recorded: subtitle %s is gone", subtitle_id)
            except Exception:  # pragma: no cover - never mask the original error
                logger.warning(
                    "could not persist status='failed' for subtitle %s", subtitle_id, exc_info=True
                )
            finally:
                if owns_status_conn:
                    status_conn.close()
        raise


def process_subtitle_file(conn: Any, subtitle_id: str, file_path: str, **kwargs: Any) -> int:
    """Spec entry point: parse and store one track, return the number of rows saved."""
    saved, _ = _process_subtitle_run(conn, subtitle_id, file_path, **kwargs)
    return saved


def process_subtitle_file_with_report(
    conn: Any, subtitle_id: str, file_path: str, **kwargs: Any
) -> tuple[int, ParseReport]:
    """Same as :func:`process_subtitle_file` but also returns the parse statistics."""
    return _process_subtitle_run(conn, subtitle_id, file_path, **kwargs)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_CUE_DURATION_MS",
    "FALLBACK_ENCODINGS",
    "MAX_FILE_BYTES",
    "STATUS_FAILED",
    "STATUS_PARSED",
    "STATUS_PENDING",
    "STATUS_PROCESSING",
    "CueRead",
    "DialogueLine",
    "LineCountMismatchError",
    "ParseReport",
    "ParsedSrt",
    "SanitizerOptions",
    "SubtitleError",
    "SubtitleFileError",
    "SubtitleNotFoundError",
    "SubtitleParserService",
    "SubtitleSanitizer",
    "SubtitleStatus",
    "clean_subtitle_text",
    "count_dialogue_lines",
    "delete_dialogue_lines",
    "insert_dialogue_lines",
    "process_subtitle_file",
    "process_subtitle_file_with_report",
    "require_subtitle",
    "set_status",
]
