Role: Senior Backend Engineer (Python / PostgreSQL).
Task: Implement a production-grade Subtitle Parser and Cleaner service module for an SRT-to-Database ingestion pipeline.

Context & Database Target:
We are inserting parsed subtitle lines into PostgreSQL table `dialogue_lines`:
- id: UUID (gen_random_uuid())
- subtitle_id: UUID (Foreign Key -> subtitles.id ON DELETE CASCADE)
- line_index: INTEGER (sequential index of the cue, starting from 1)
- start_time_ms: INTEGER (start time in milliseconds)
- end_time_ms: INTEGER (end time in milliseconds)
- raw_text: TEXT (sanitized, multi-line normalized speech text)
- created_at: TIMESTAMPTZ DEFAULT NOW()

Module Architecture Requirements:

1. Dependencies:
   - Use `pysrt` for robust timecue parsing (or a zero-dependency regex parser if you handle encoding properly).
   - Use `re` for sanitization rules.
   - Use `psycopg2.extras.execute_batch` (or `asyncpg`) for bulk insertions.

2. Sanitization & Cleaning Rules (CRITICAL):
   Subtitles contain significant non-dialogue noise that ruins NLP analysis. Implement `clean_subtitle_text(text: str) -> str`:
   - Strip formatting tags: HTML/XML tags (`<i>`, `</b>`, `<font color="...">`, etc.).
   - Strip Substation Alpha tags if any (`{\an8}`, `{\pos(...)}`).
   - Remove Hearing-Impaired (SDH) bracketed sound cues:
     - Square brackets: `[door creaks]`, `[suspenseful music plays]` -> remove.
     - Parentheses sound cues: `(CHUCKLES)`, `(SIGHS)`, `(LAUGHING)` -> remove.
     - Note: Avoid stripping valid text like "(together) We can do it" - if unsure, strip uppercase bracketed tokens like `\([A-Z\s,!'\.\-]+\)`.
   - Remove speaker label prefixes: e.g. "WALTER: Put it down" -> "Put it down", "JESSE: Yo, Mr. White" -> "Yo, Mr. White". Regex pattern: `^[A-Z0-9\s_\-\.]{2,20}:\s+`.
   - Normalize hyphens/dialogue dashes: lines starting with `- ` or `— ` (often indicating speaker change) should be kept clean as separate sentences or stripped of the leading dash if it's a single speaker.
   - Remove music symbols: `♪`, `♫`, `#`.
   - Whitespace normalization: collapse multiple spaces, trim each line, strip empty lines. If resulting text is empty, drop the cue entirely.

3. Time conversion:
   - Convert SRT time format (`HH:MM:SS,mmm`) to integer milliseconds:
     `total_ms = (hours * 3600 + minutes * 60 + seconds) * 1000 + milliseconds`.

4. Fault Tolerance & Encoding:
   - SRT files are frequently encoded in UTF-8, UTF-8-BOM, CP1252, or Latin-1.
   - Implement automatic fallback decoding: try `utf-8`, then `utf-8-sig`, then `cp1251`/`cp1252` with `chardet` or standard fallback order.

5. Transaction & Status Management:
   - Function signature: `process_subtitle_file(conn, subtitle_id: str, file_path: str) -> int` (returns number of lines saved).
   - Flow:
     a. Update `subtitles.status = 'processing'` WHERE id = subtitle_id.
     b. Parse and clean lines.
     c. Bulk INSERT into `dialogue_lines` (batch size = 500).
     d. Update `subtitles.status = 'parsed'`.
     e. Commit transaction.
     f. On Exception: Rollback, update `subtitles.status = 'failed'`, log error with traceback, re-raise exception.

Deliverable:
Produce a self-contained, typed, cleanly documented Python file `subtitle_parser.py` satisfying all the criteria above.

example:
```python
import os
import re
import logging
from typing import List, Tuple, Optional
import pysrt
import psycopg2
from psycopg2.extras import execute_batch

logger = logging.getLogger(__name__)


class SubtitleSanitizer:
    # 1. HTML / XML теги (<i>, </b>, <font ...>)
    RE_HTML_TAGS = re.compile(r"<[^>]+>")
    
    # 2. SSA/ASS позиционные теги ({\an8}, {\b1})
    RE_SSA_TAGS = re.compile(r"\{[^\}]+\}")
    
    # 3. Звуки для слабослышащих в квадратных скобках: [Groans], [Upbeat music]
    RE_BRACKETS_SDH = re.compile(r"\[[^\]]*\]")
    
    # 4. Звуки в круглых скобках только заглавными буквами: (LAUGHS), (GUNSHOT)
    RE_PARENTHESES_SDH = re.compile(r"\([A-Z\s\d,!'\.\-–—]+\)")
    
    # 5. Имена спикеров: "WALTER:", "OFFICER 1:", "MAN ON TV:"
    RE_SPEAKER_LABELS = re.compile(r"^[A-Z0-9\s_\-\.]{2,25}:\s*", re.MULTILINE)
    
    # 6. Музыкальные символы
    RE_MUSIC_SYMBOLS = re.compile(r"[♪♫#¶\*\+]")

    @classmethod
    def clean(cls, text: str) -> str:
        if not text:
            return ""

        # Применяем последовательную очистку
        t = cls.RE_HTML_TAGS.sub("", text)
        t = cls.RE_SSA_TAGS.sub("", t)
        t = cls.RE_BRACKETS_SDH.sub("", t)
        t = cls.RE_PARENTHESES_SDH.sub("", t)
        t = cls.RE_SPEAKER_LABELS.sub("", t)
        t = cls.RE_MUSIC_SYMBOLS.sub("", t)

        # Обработка дефисов диалогов в начале строк ("- Hello" -> "Hello")
        cleaned_lines = []
        for line in t.splitlines():
            line = line.strip()
            if line.startswith(("-", "—", "–")):
                line = line.lstrip("-—– ").strip()
            if line:
                cleaned_lines.append(line)

        result = " ".join(cleaned_lines)
        # Схлопываем множественные пробелы
        result = re.sub(r"\s+", " ", result).strip()
        return result


class SubtitleParserService:
    @staticmethod
    def _read_file_with_fallback(file_path: str) -> str:
        """Читает файл субтитров, устойчив к разным кодировкам."""
        encodings = ["utf-8", "utf-8-sig", "cp1252", "latin-1", "iso-8859-1"]
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, LookupError):
                continue
        raise ValueError(f"Could not decode file {file_path} with supported encodings.")

    @classmethod
    def parse_srt(cls, file_path: str) -> List[Tuple[int, int, int, str]]:
        """
        Парсит .srt файл.
        Возвращает список кортежей: (line_index, start_time_ms, end_time_ms, cleaned_text)
        """
        raw_content = cls._read_file_with_fallback(file_path)
        subs = pysrt.from_string(raw_content)

        parsed_lines: List[Tuple[int, int, int, str]] = []
        line_idx = 1

        for sub in subs:
            cleaned_text = SubtitleSanitizer.clean(sub.text)
            if not cleaned_text:
                continue

            # Перевод pysrt.SubRipTime в миллисекунды
            start_ms = (
                sub.start.hours * 3600000 +
                sub.start.minutes * 60000 +
                sub.start.seconds * 1000 +
                sub.start.milliseconds
            )
            end_ms = (
                sub.end.hours * 3600000 +
                sub.end.minutes * 60000 +
                sub.end.seconds * 1000 +
                sub.end.milliseconds
            )

            # Валидация таймкодов
            if end_ms <= start_ms:
                end_ms = start_ms + 1000  # Дефолтная длительность 1с при баге субтитров

            parsed_lines.append((line_idx, start_ms, end_ms, cleaned_text))
            line_idx += 1

        return parsed_lines

    @classmethod
    def process_and_save(cls, conn, subtitle_id: str, file_path: str) -> int:
        """
        Транзакционный метод парсинга и сохранения в PostgreSQL.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"SRT file not found: {file_path}")

        with conn.cursor() as cur:
            # 1. Помечаем статус 'processing'
            cur.execute(
                "UPDATE subtitles SET status = 'processing', updated_at = NOW() WHERE id = %s;",
                (subtitle_id,)
            )

        try:
            # 2. Парсим и чистим
            lines = cls.parse_srt(file_path)

            if not lines:
                logger.warning(f"No valid lines extracted from {file_path}")

            # 3. Батч-вставка в dialogue_lines
            insert_query = """
                INSERT INTO dialogue_lines (
                    id, subtitle_id, line_index, start_time_ms, end_time_ms, raw_text, created_at
                )
                VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, NOW());
            """
            
            # Подготавливаем кортежи для вставки: подставляем subtitle_id в начало
            records = [(subtitle_id, l_idx, s_ms, e_ms, txt) for (l_idx, s_ms, e_ms, txt) in lines]

            with conn.cursor() as cur:
                execute_batch(cur, insert_query, records, page_size=500)

                # 4. Помечаем статус 'parsed'
                cur.execute(
                    "UPDATE subtitles SET status = 'parsed', updated_at = NOW() WHERE id = %s;",
                    (subtitle_id,)
                )

            conn.commit()
            logger.info(f"Successfully saved {len(records)} dialogue lines for subtitle {subtitle_id}")
            return len(records)

        except Exception as e:
            conn.rollback()
            logger.exception(f"Failed to process subtitle {subtitle_id}: {str(e)}")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subtitles SET status = 'failed', updated_at = NOW() WHERE id = %s;",
                    (subtitle_id,)
                )
            conn.commit()
            raise e
```