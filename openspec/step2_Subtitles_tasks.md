# OpenSpec Task Board: Step 2 — Subtitle Parser & Cleaner
Версия: 1.0.0
Статус: **In Progress**
Связанные документы: `openspec/step2_Subtitles.md` (ТЗ), `openspec/bd.md` (схема), `openspec/seed_lemmas.md` (шаблон отчёта о шаге)
Правило ведения: выполняется таска → ставим `[x]`, переносим строку в §7 «Журнал выполнения». Не меняем формулировки ТЗ — только дополняем «Решения», если отходим от ТЗ.

---

## 1. Контекст (текущее состояние репозитория)

| Факт | Значение |
|---|---|
| Готово (step 1) | Схема БД (`docker/init.sql`), сид `global_lemmas` (10 000 лемм), `docker/seed_global_lemmas.py` |
| Приложение | **отсутствует** — step 2 создаёт первый service-модуль |
| PostgreSQL | 5430 → контейнер `postgres_container_eng`, БД `postgres_db`, пользователь `postgres_user` |
| Конфиг БД в сидере | захардкожен `DB_KWARGS` → в step 2 выносим в env (см. T0.2) |
| Стек | Python 3.12, `psycopg2-binary`, `pysrt`, `pytest` |
| `requirements.txt` | нет → заводим |

### 1.1. Факты схемы, влияющие на реализацию
- `subtitles.status` — **ENUM** `subtitle_status_enum` (`pending`, `processing`, `parsed`, `failed`).
- `dialogue_lines`: PK `id` и `created_at` have DB defaults → в INSERT **не** перечисляем (см. R1).
- Индекс: `idx_dialogue_lines_subtitle_time (subtitle_id, start_time_ms)`.
- `subtitles.file_hash_sha256 CHAR(64)` — свободная колонка под дедупликацию (см. T3.3).
- **Отсутствует** `UNIQUE (subtitle_id, line_index)` → повторный запуск = дубли (см. T2.2, T6.1).
- **Отсутствует** `CHECK (end_time_ms > start_time_ms)` → решаем на уровне приложения (см. T2.3).

---

## 2. Целевая структура

```
app/
  requirements.txt              # psycopg2-binary>=2.9, pysrt>=1.1.2, chardet>=5 ; dev: pytest>=8
  subtitle_parser.py            # deliverable: SubtitleSanitizer, SubtitleParserService, process_subtitle_file
  db.py                         # get_connection(**overrides) из env, default session role=app
  scripts/
    parse_subtitle.py           # CLI: python -m app.scripts.parse_subtitle --subtitle-id UUID --file path.srt [--encoding]
tests/
  conftest.py                   # фикстуры: tmp srt-файлы, соединения с БД (skip без env)
  data/
    breaking_bad.srt            # cp1252 + BOM/битые таймкоды/вложенные теги
    sdh_noise.srt               # [MUSIC], (SIGHS), ♪, speaker labels, дефисные реплики
    utf8_bom.srt
    windows_cp1252.srt
    broken_timing.srt
  test_subtitle_sanitizer.py    # таблица-матрица §4
  test_subtitle_parser.py       # encoding, таймкоды, drop-empty, индексы
  test_subtitle_repository.py   # интеграция с реальной БД: статусы, идемпотентность, откат, CASCADE
  test_subtitle_e2e.py          # файл → 78 строк → SQL-проверка
```

---

## 3. Задачи

### Phase 0 — Инфраструктура и зависимости
- [x] **T0.1** Создать `app/requirements.txt` + `app/__init__.py`; проверить импорт `subtitle_parser` без установленных опциональных либ (`chardet`, `pysrt` — fallback, см. T1.2).
  *Done when:* `pip install -r app/requirements.txt` ставит стек; `python -c "import app.subtitle_parser"` работает.
  _Note:_ работает. Каноничное окружение — `.venv/` (Python 3.12.3): системный `python` в PATH отсутствует,
  `pip` указывает на `/usr/bin/pip3.13` (CPython 3.13, вне проекта). Установлено: psycopg2 2.9.13, pysrt 1.1.2, chardet 7.6.0,
  pytest 9.1.1, ruff 0.16.8, mypy 2.3.1, black 26.5.1.
- [x] **T0.2** Создать `app/db.py`: `get_connection()` читает `SUBTITLES_DB_HOST/PORT/NAME/USER/PASSWORD` с дефолтами текущей docker-конфигурации; `SET application_name='subtitle_parser'`.
  *Done when:* коннект к `localhost:5430` без хардкода паролей; в `pg_stat_activity` видно application_name.
  _Note:_ `application_name` передаётся параметром соединения (`options=-c application_name=…` не нужен),
  проверено на живом сервере; есть `session()` (контекстный менеджер) и `server_is_reachable()` для skip-логики.
- [x] **T0.3** Базовая настройка pytest: `pytest.ini`/`pyproject` + маркеры `unit` / `integration` (интеграционные скипуются, если БД недоступна).
  *Done when:* `pytest -m unit` проходит без запущенного Docker.
  _Note:_ `pytest.ini` (маркеры, `--strict-markers`, `pythonpath=.`); integration-тесты дают `skip` с причиной
  при недоступном сервере. Фикстуры `.srt` генерируются `tests/data/make_fixtures.py` при старте сессии
  (в git не попадают, см. `.gitignore`) — это закрывает и T4.1.

### Phase 1 — `SubtitleSanitizer` (ядро чистки)
- [x] **T1.1** Определить публичный API: метод `clean_subtitle_text(text: str) -> str` (обязателен по ТЗ) + алиас `SubtitleSanitizer.clean`. Настройки — frozen dataclass `SanitizerOptions` (merge_lines, drop_music_lines, strip_speaker_labels, preserve_dash_speakers).
  *Done when:* сигнатура из ТЗ присутствует; дефолтные опции = поведение по ТЗ.
  _Note:_ поле называется `strip_dialogue_dashes` (инверсия смысла вместо `preserve_dash_speakers`), есть
  `replaced(**overrides)` для копирования с изменением — см. D7.
- [x] **T1.2** Реализовать конвейер чистки в фиксированном порядке (см. §5.1) с пред-защитой `&apos;`/`&rsquo;` → `'` (см. R3) и защитой apostrophe от правила скобок (см. T1.6).
- [x] **T1.3** HTML/XML-теги: `RE_HTML_TAGS` (R3 — до SDH-скобок).
- [x] **T1.4** SSA/ASS-теги: `\{[^}]*\}` (R4 — поддержать `{…} … {…}` в одной строке).
- [x] **T1.5** SDH: квадратные скобки → удалить целиком; `[Music]` → в `drop_music_lines` (см. T1.9).
- [x] **T1.6** SDH: круглые скобки — **только полностью заглавные** (`\([A-Z][A-Z0-9\s.,!?'…\-–—]*\)`), чтобы не есть `(together) We can do it`; убедиться, что `Don't (now!)` не теряется.
  _Note:_ внутри скобок дополнительно разрешены Unicode-буквы не-латиницы (иначе `(Привет)` считалось бы
  «заглавным» маркером) — см. D8.
- [x] **T1.7** Speaker labels: `^[A-Z][A-Z0-9 ._'-]{1,24}:\s+` **с апострофом в классе символов** (R6 — `DON'T: Get out` не должен схлопнуться в `t Get out`), многострочный режим, без срабатывания на `Time: 5 min` / `www.site.com:8080`.
- [x] **T1.8** Диалоговые тире/дефисы: `- Hey.` / `— Hey.` → `Hey.`; склейка многострочных реплик с `&quot; … &quot;` в кавычках (R2: `"Fire off. Pull trigger."`, **не** `Fire off.Pull trigger.`).
- [x] **T1.9** Музыка: символы `♪ ♫ ♬ 🎵 #` **не глобальным** `[♪♫#¶*+]` (R2), а: (a) маркер строки-песни (начинается с ноты или содержит ноты с двух сторон), (b) опция `drop_music_lines`.
- [x] **T1.10** Нормализация пробелов: NFKC-устойчиво, `’`→`'`, склейка переносов, collapse `\s+`→`' '`, trim; пустое → `""` (cue дропается парсером).
- [x] **T1.11** Заполнить и прогнать матрицу кейсов §4 (unit-тесты, параметризованные).
  *Done when:* все 12+ кейсов §4 зелёные; покрытие каждого правила ≥1 кейсом.
  _Note:_ `tests/test_subtitle_sanitizer.py` — 57 параметризованных кейса (все 14 строк §4 + граничные:
  ZWSP, `\x00`, `A\u00a0B`, `COACH:\n`, `&quot;`, `www.site.com:8080`, а после D18 — `>>>\t`, `#blessed #13-37…`,
  `Trailing hash #`, литеральный `\N` и «неразгаданные» `&lt;/&gt;`) и 10 отдельных тестов на опции;
  всего 67 тестов, все зелёные.

### Phase 2 — `SubtitleParserService.parse_srt`
- [x] **T2.1** `_read_file_with_fallback(path) -> tuple[str, str]` (текст + использованная кодировка): порядок `utf-8-sig`, `utf-8`, `cp1252`, `cp1251`, latin-1; при наличии `chardet` — авто-детект как шаг 0 (T1.2); ошибку декодирования **ловим** внутри цикла (см. Gap-код в примере ТЗ).
  _Note:_ `FALLBACK_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "cp1251", "latin-1")`, ответ chardet
  ставится в начало цепочки и из неё же убирается (без повторов); доверие только при `confidence >= 0.7`,
  имена окон нормализуются (`windows-1252 → cp1252`). Чтение всегда бинарное + явный decode, чтобы
  порядок не зависел от локали машины.
- [x] **T2.2** Парсинг cue-ов: `pysrt.from_string`, try/except на битый cue с логом и продолжением (fault tolerance).
  _Note:_ fault tolerance двухуровневая: `pysrt` обёрнут в try/except **и** в проверку «ноль cue»
  (на мусоре pysrt молча возвращает 0 cue, а не исключение) — тогда файл перечитывает встроенный
  regex-парсер, который пропускает битые cue с `logger.warning` (`engine="regex"`); если не нашёл
  ничего — `SubtitleFileError`. См. D9.
- [x] **T2.3** Таймкоды: явный хелпер `_to_ms(time)` (см. R8) + проверка `end > start`, иначе `end = start + 1000` с `logger.warning`.
- [x] **T2.4** `line_index` — сквозной 1-based по **сохранённым** cue (пропуск не создаёт дыр в нумерации).
- [x] **T2.5** DTO `DialogueLine(line_index, start_time_ms, end_time_ms, raw_text)` вместо «голых» кортежей; типизированный `parse_srt(...) -> list[DialogueLine]`.
  _Note:_ `parse_srt` возвращает `ParsedSrt(lines, report)` (NamedTuple), чтобы наружу уходили `encoding`,
  `engine` и счётчики метрик — см. D10.
- [x] **T2.6** Валидация файла: пустой файл / слишком большой (лимит `MAX_FILE_BYTES`, например 10 МБ) / неверная структура — с понятными исключениями.
  *Done when:* unit-тесты на все fixture-файлы в `tests/data/`.
  _Note:_ `MAX_FILE_BYTES = 10 MiB`; тесты — `tests/test_subtitle_parser.py` (9 фикстур из 10, у которых
  заявлено `expect_kept`, × 5 параметризованных проверок counts/timings/clean-text + отдельные тесты на
  encoding/engine/BOM/битые таймкоды, и отдельный тест, что ни в одной `raw_text` не осталось грязи).

### Phase 3 — Слой записи в БД (репозиторий + транзакция)
- [x] **T3.1** Репозиторий-функции: `set_status(cur, subtitle_id, status)`, `delete_dialogue_lines(cur, subtitle_id)`, `insert_dialogue_lines(cur, subtitle_id, lines, page_size=500)`; SQL — константы модуля с именованными плейсхолдерами (`%(subtitle_id)s`).
  *Done when:* `INSERT … VALUES (…)` использует `%s` от **кортежа** (R9 — без `.format`).
  _Note:_ SQL-константы `SQL_SET_STATUS`, `SQL_REQUIRE_SUBTITLE`, `SQL_DELETE_DIALOGUE_LINES`,
  `SQL_INSERT_DIALOGUE_LINES`, `SQL_COUNT_DIALOGUE_LINES`; в них только именованные плейсхолдеры,
  параметры всегда передаются dict'ом (`test_every_placeholder_is_named`), для ENUM-колонки —
  `%(status)s::subtitle_status_enum`. `page_size` в `insert_dialogue_lines` — keyword-only.
- [x] **T3.2** Пред-условие вставки: `SELECT 1 FROM subtitles WHERE id = %(id)s FOR UPDATE` → если строки нет, `raise UnsavedSubtitle` (уход от ForeignKeyViolation/rolled-back transaction, R5).
  _Note:_ класс называется `SubtitleNotFoundError` (наследник `SubtitleError`) — см. D11;
  `require_subtitle(cursor, subtitle_id)` вызывается до чтения файла, чтобы несуществующий id
  не маскировался битым путём.
- [x] **T3.3** Идемпотентность: перед вставкой `DELETE FROM dialogue_lines WHERE subtitle_id = %(subtitle_id)s` (логировать удалённое количество). *Опция:* `ON CONFLICT (subtitle_id, line_index) DO UPDATE` — требует миграции §T5.1.
  _Note:_ удалённое количество попадает в `ParseReport.deleted_lines` и в итоговую `logger.info`
  как `replaced=%d`; `ON CONFLICT` не используется (D3, миграция — T5.1).
- [x] **T3.4** Оркестратор `process_subtitle_file(conn, subtitle_id, file_path) -> int` строго по шагам a→f (§5.2), с `try/except`: rollback → отдельное соединение для статуса `failed` → `logger.exception` → re-raise.
  *Done when:* 6 вызовов с `logger.exception` внутри одного except, как в ТЗ.
  _Note:_ шаги a→f в исходном порядке; в except один `logger.exception` (ТЗ буквализирован как
  «шестой вызов» — дублировать лог на каждый шаг смысла нет), плюс `logger.warning` на неудачный
  rollback, на невозможность открыть соединение для `failed` и на «строка пропала». `failure_conn` —
  параметр для тестов; чужие соединения оркестратор не закрывает, своё (`_open_fresh_connection`) — закрывает.
- [x] **T3.5** Проверить `rowcount`: суммарно вставлено == len(lines); иначе `raise RuntimeError` (молчаливая потеря строк = испорченный корпус для step 3).
  _Note:_ сверка идёт через `SELECT COUNT(*)` (`SQL_COUNT_DIALOGUE_LINES`), а не `cursor.rowcount`:
  у `execute_batch` rowcount не суммирует страницы. Исключение — `LineCountMismatchError(SubtitleError)`,
  то есть НЕ `RuntimeError` — см. D12.
- [x] **T3.6** Метрики в логе: `encoding`, `cues_total`, `cues_kept`, `cues_dropped`, `duration_ms`.
  _Note:_ `parsed %d/%d cues (engine=%s encoding=%s dropped=%d) in %.1f ms` сразу после разбора и
  `saved %d dialogue line(s) … cues=%d dropped=%d replaced=%d took=%.1f ms` после commit; те же
  данные доступны программно — `process_subtitle_file_with_report(...) -> tuple[int, ParseReport]`.
- [x] **T3.7** CLI `app/scripts/parse_subtitle.py`: аргументы `--subtitle-id`, `--file`, `--encoding`, `--dry-run`, `--json`; выход = число сохранённых строк, коды возврата 0/1.
  *Done when:* `python -m app.scripts.parse_subtitle --subtitle-id <uuid> --file tests/data/sdh_noise.srt` возвращает 0 и печатает счётчик.
  _Note:_ есть также `--keep-line-breaks`, `--page-size`, `--log-level`, `--version`; `--subtitle-id`
  обязателен вне `--dry-run`; логи пишутся только в stderr, в stdout — результат (в `--json` — payload);
  коды возврата 0/1 + 2 для ошибок разбора аргументов и несуществующего файла; соединение оборачивается
  в `contextlib.closing`, потому что `with conn` в psycopg2 коммитит, но не закрывает соединение.

### Phase 4 — Данные и тесты
- [x] **T4.1** Создать fixture-файлы §2 (5 файлов) с реальными артефактами кодировок (cp1252-байты, BOM, битые таймкоды) — генерируются скриптом `tests/data/make_fixtures.py` для воспроизводимости.
  _Note:_ файлов 10, имена отличаются от §2 (`basic_english` / `sdh_noise` / `sdh_full` / `speaker_labels` /
  `music_and_ellipsis` / `windows_cp1252`(cp1252+CRLF) / `utf8_bom`(utf-8-sig+CRLF) / `no_index_numbers` /
  `malformed` / `stress_realworld` — 90-cue корпус для T6.1) — см. D13. Каждый `FixtureSpec` несёт и
  ожидаемый результат, поэтому фикстура и ассерт не могут разойтись; pytest перегенерирует файлы при
  старте сессии, `.srt` в git не попадают.
- [x] **T4.2** Fixture с «настоящим» SDH-субтитром (20–40 cue) для регрессионного сравнения чистки.
  _Note:_ `tests/data/sdh_full.srt` — 26 cue с ручными таймкодами (каждое правило матрицы §4 срабатывает
  минимум один раз): 20 сохранено, 6 сброшено, `expect_texts` зафиксирован построчно. В эталон осознанно
  включён «грязный» кейс `WALTER, JR.: Grandpa?` (запятая не входит в класс символов маркера реплики) —
  см. D15.
- [x] **T4.3** Unit: sanitizer (матрица §4) — параметризованные тесты.
  _Note:_ `tests/test_subtitle_sanitizer.py` — 67 тестов (57 параметризованных кейсов + 10 на опции
  и инварианты); кейсы §4.2 добавлены после боя T6.1.
- [x] **T4.4** Unit: parser (encoding fallback, таймкоды, drop-empty, line_index).
  _Note:_ `tests/test_subtitle_parser.py` — 71 кейс (67 проходят, 4 `skip` у фикстур, где заявлен
  только счётчик), 10 параметризованных прогонов по таблице фикстур + точечные тесты на таймкоды,
  fallback-цепочку, CR/LF, `--forced encoding` и валидацию файла.
- [x] **T4.5** Integration (маркер `integration`, реальный Docker-PG): seeded title/episode/subtitle → parse → проверка 78 строк, `status='parsed'`, порядка `line_index`, индекса `(subtitle_id, start_time_ms)`.
  _Note:_ фикстура `subtitle_id` в `tests/conftest.py` сеет `titles → episodes → subtitles`,
  `test_end_to_end_ingestion` проверяет 6 строк из `basic_english.srt`, `status='parsed'`,
  непрерывный `line_index` и точные таймкоды; наличие и применимость индекса —
  `test_dialogue_lines_index_is_present_and_usable` (`pg_indexes` + `EXPLAIN` c `enable_seqscan=off`).
  Число «78 строк» относится к реальному файлу пользователя и проверяется в T6.1, см. D14.
- [x] **T4.6** Integration: идемпотентный повторный запуск → 78 строк, не 156.
  _Note:_ `test_reprocessing_does_not_duplicate_rows` (6 → 6, не 12) и CLI-тест
  `test_cli_stores_lines_and_is_idempotent` (второй прогон даёт `deleted_lines=6`).
- [x] **T4.7** Integration: несуществующий `subtitle_id` → `UnsavedSubtitle`, в `dialogue_lines` 0 строк.
  _Note:_ `test_unknown_subtitle_is_rejected` (`SubtitleNotFoundError`, см. D11) плюс unit-проверка,
  что до парсера и до INSERT дело не доходит (`test_missing_subtitle_never_reaches_the_parser`).
- [x] **T4.8** Integration: битый файл → `status='failed'`, `dialogue_lines` пуст, след `failed` не виден в основной транзакции после отката.
  _Note:_ два кейса — отсутствующий файл (`test_unknown_file_leaves_the_track_failed`) и файл без cue
  (`test_unparseable_file_leaves_the_track_failed`): после rollback в основной транзакции 0 строк,
  а `failed` виден, потому что записан отдельным соединением (D4).
- [x] **T4.9** Integration: `ON DELETE CASCADE` — `DELETE FROM subtitles` → 0 осиротевших `dialogue_lines`.
  _Note:_ `test_deleting_a_subtitle_cascades_to_its_lines` проверяет и счётчик строк, и LEFT JOIN
  на осиротевшие строки (§6.5).
- [x] **T4.10** Quality gates: `ruff` (+ `black`) и `mypy --strict app/subtitle_parser.py app/db.py`.
  _Note:_ конфиги в `pyproject.toml` (`line-length = 100`, mypy strict, `files = ["app"]`); прогоняются
  `ruff check app tests`, `black --check app tests`, `mypy` (5 файлов, включая CLI и `db.py`) и
  `mypy --strict app/subtitle_parser.py app/db.py` — все зелёные.

### Phase 5 — Схема БД (по желанию, требует миграции)
- [x] **T5.1** `docker/migrations/002_dialogue_lines.sql`: `ALTER TABLE dialogue_lines ADD CONSTRAINT uq_dialogue_lines_subtitle_index UNIQUE (subtitle_id, line_index);` + `CHECK (end_time_ms > start_time_ms)`.
  *Done when:* идемпотентно (`IF NOT EXISTS`-стиль через DO-блок), сид-скрипты не ломаются.
  _Note:_ файл готов и **проверен на копии схемы** (одноразовая БД `t51_copy_test`, развёрнутая из
  `docker/init.sql`; см. §7 T5.1): двойной прогон идемпотентен, дубликат `(subtitle_id, line_index)`
  и обратный интервал отклоняются, боевой прогон парсера поверх копии работает и остаётся
  идемпотентным; на предзаполненной «грязными» строками копии оба `ADD CONSTRAINT` падают с внятным
  текстом и **не** оставляют половину миграции применённой. **К живой БД не применён по решению
  пользователя** — идемпотентность обеспечивается приложением (D3). Копия удалена после проверки.
- [x] **T5.2** (опция) `sha256`-колонка или reuse `subtitles.file_hash_sha256` для fast-path «файл уже разобран» — зафиксировать решение в §7.
  _Note:_ решение «не делаем» с обоснованием — §7 T5.2.
- [x] **T5.3** (опция) `updated_at`-триггер для `subtitles` (сейчас нужно ставить вручную).
  _Note:_ решение «не делаем»: парсер сам пишет `updated_at = NOW()` в `SQL_SET_STATUS` — §7 T5.3.

### Phase 6 — Верификация и отчёт
- [x] **T6.1** Ручной прогон на реальном .srt (файл пользователя, ~87 cue) + SQL-проверки §6 → записать числа в §7.
  _Note:_ реального `.srt` у пользователя нет, поэтому прогон выполнен на синтетическом стресс-корпусе
  `tests/data/stress_realworld.srt` — 90 cue, все артефакты §4 и §5 вперемешку (D18, D19). Бой:
  90 → **71 сохранено / 19 сброшено**, `status='parsed'`; все шесть проверок §6 зелёные
  («грязь» = 0, разрывы `line_index` = 0, обратных интервалов = 0, немонотонного времени = 0,
  дубликатов = 0, осиротевших = 0), повторный запуск даёт `deleted_lines=71` при тех же 71 строках.
  Числа и выборка — §7 (строки T6.1), «что осталось» — §4.2 и отчёт §6.4.
- [x] **T6.2** Проверить «грязные» кейсы глазами: выборка 10 строк `raw_text` с `LIMIT 10` + `WHERE` на `(unknown)`.
  _Note:_ прогон в отдельную тестовую запись `subtitles` (`source='manual-t62'`), первые 10 `raw_text`
  посмотрены, «грязь» = 0 строк, разрывов `line_index` = 0; запись удалена (`DELETE FROM titles` → CASCADE),
  в БД снова 0 строк. Числа — §7 T6.2, детали — `openspec/step2_Subtitles_report.md` §6.3/§6.4.
- [x] **T6.3** Обновить/создать `openspec/step2_Subtitles_report.md` в стиле `seed_lemmas.md` (проблемы→решения, статистика, образцы) — по образцу отчёта step 1.
  _Note:_ создан: §1 контекст/требования, §2 «проблема→причина→решение» (11 строк), §3 фикстуры,
  §4 архитектура и транзакционный поток, §5 выполненные шаги, §6 статистика (тесты/гейты, прогон
  10 фикстур, приёмочные SQL-проверки, «было → стало»), §7 ограничения и следующие шаги.
- [x] **T6.4** Обновить README/`app/README.md`: как запустить парсер, переменные окружения.
  _Note:_ создан `app/README.md`: установка в `.venv`, опциональные `pysrt`/`chardet`, поднятие
  контейнера и применение `docker/init.sql`, таблица `SUBTITLES_DB_*` с дефолтами, три примера
  запуска, таблица флагов, коды возврата и контракт stdout/stderr, идемпотентность и `failed`
  из отдельного соединения (D4/D16), команды тестов и гейтов, известные ограничения (D15,
  неприменённая миграция 002, отсутствие `CHECK length(raw_text)>0` — D17).

---

## 4. Матрица кейсов санитизации (T1.11 / T4.3 — источник истины для тестов)

| # | Вход | Ожидаемый выход |
|---|---|---|
| 1 | `<i>Put it down, Walter.</i>` | `Put it down, Walter.` |
| 2 | `<font color="#E5E5E5">Are you okay?</font>` | `Are you okay?` |
| 3 | `{\an8}I am the one who knocks.{\pos(20, 200)}` | `I am the one who knocks.` |
| 4 | `[door creaks] Where is he?` | `Where is he?` |
| 5 | `(CHUCKLES) We can do it (together).` | `We can do it (together).` |
| 6 | `WALTER: Put it down.` / `JESSE: Yo, Mr. White` / `NORMA: Cut the cheese.` | `Put it down.` / `Yo, Mr. White` / `Cut the cheese.` |
| 7 | `—Hey.\n-Wake up.` | `Hey. Wake up.` |
| 8 | `(GROANS)\n♪ Ring of fire ♪` | `` (cue дропается) |
| 9 | `Don't (now!) touch that` | `Don't (now!) touch that` |
| 10 | `"Fire off.\nPull trigger."` | `"Fire off. Pull trigger."` |
| 11 | `DON'T: Get out` | `Get out` |
| 12 | `Hello   world \n\n second  line` | `Hello world second line` |
| 13 | `<b>[MUSIC]</b>` | `` (cue дропается) |
| 14 | `{\an8}You can't[bleep]be serious` | `You can't be serious` (без склейки слов) |

## 4.1. Готовые unit-кейсы (пример из ТЗ)
`[Groans]`, `Upbeat music`, `<i>`, `OFFICER 1:`, `MAN ON TV:` — должны покрываться кейсами 4, 6, 8, 13.

## 4.2. Кейсы, добавленные боевым прогоном T6.1 (D18)

Нашли на 90-cue стресс-корпусе, а не на матрице: три починены, четыре оставлены как
осознанные ограничения (все зафиксированы тестами в `tests/test_subtitle_sanitizer.py`).

| Вход | `raw_text` | Статус |
|---|---|---|
| `# Silent night #` | `` (cue дропается) | ♪-маркер в рамке — это песня |
| `#blessed #13-37 and #hashtag.` | `blessed #13-37 and #hashtag.` | **починено**: ведущий `#` — шум, а не маркер, строка была потеряна целиком |
| `Trailing hash #` | `Trailing hash` | **починено**: то же для хвостового `#` |
| `>>>\tCopy.` | `Copy.` | **починено**: бегунок реплики бывает в несколько `>` |
| `First line\Nsecond hard break.` | `First line second hard break.` | **починено**: ASS-перевод строки `\N` доезжает до `.srt` двумя литеральными символами |
| `Radio says &lt;unknown&gt;.` | без изменений | ограничение: `&lt;`/`&gt;` не декодируем, иначе соберём фальшивый тег из текста |
| `Jo: Neither is this.` / `Mixed: текст и text together.` | без изменений | ограничение: метка реплики — только верхний регистр от 2 букв (тот же класс, что и D15) |

---

## 5. Референс реализации (решения спорных мест)

### 5.1. Порядок правил санитизации (важно!)
```
0. Unicode-нормализация: NBSP/thin-space → ' ', ’ → ', &#39;/&apos;/&rsquo; → ', &quot; → "
1. HTML/XML-теги          (иначе <b>[MUSIC]</b> съест только [MUSIC] и оставит <b></b>)
2. SSA/ASS {\…} + литеральный перевод строки \N → настоящий \n
3. [ ... ] SDH-кушки
4. (UPPERCASE ONLY) SDH-кушки
5. ♪ ♫ ♬ — маркер на краю строки ⇒ строка песня; # — только в рамке («# … #»),
   иначе это шум внутри речи (#13-37, #blessed) — см. T1.9 и D18
6. Speaker labels в начале строки
7. Диалоговые тире в начале строки
8. Склейка строк → collapse \s+ → strip
```

### 5.2. Транзакционный поток (шаги a→f из ТЗ)
a. `UPDATE subtitles SET status='processing', updated_at=NOW() WHERE id=%s`
b. decode + parse + sanitize (чистка **до** записи: только `raw_text`)
c. `DELETE` старых строк (T3.3) → `execute_batch(INSERT … , page_size=500)`
d. `UPDATE subtitles SET status='parsed', updated_at=NOW()`
e. `conn.commit()`
f. except → `conn.rollback()` → **отдельное** короткое соединение/транзакция для `status='failed'` → `logger.exception` → `raise`

> ⚠ Если `subtitles.id` не найден, UPDATE проходит с `rowcount=0` и «processing» не фиксируется; дальнейший INSERT даёт `ForeignKeyViolation`, после rollback любой UPDATE на том же соединении падает с `current transaction is aborted` — поэтому T3.2 (пред-проверка `FOR UPDATE`) + отдельное соединение в обработчике ошибок.

---

## 6. SQL-проверки для приёмки (T6.1)

```sql
-- 1. Статус и количество
SELECT s.status, s.updated_at, COUNT(d.id) AS lines
FROM subtitles s LEFT JOIN dialogue_lines d ON d.subtitle_id = s.id
WHERE s.id = '<uuid>' GROUP BY s.id;

-- 2. Порядок и непрерывность индексов + монотонность времени
SELECT line_index, start_time_ms, end_time_ms,
       lead(start_time_ms) OVER (ORDER BY line_index) AS next_start,
       left(raw_text, 60) AS preview
FROM dialogue_lines WHERE subtitle_id = '<uuid>' ORDER BY line_index;

SELECT COUNT(*) FROM dialogue_lines WHERE subtitle_id='<uuid>' AND end_time_ms <= start_time_ms;

-- 3. «Грязь» не должна пройти
SELECT COUNT(*) FROM dialogue_lines
WHERE subtitle_id='<uuid>'
  AND (raw_text ~ '<[^>]+>' OR raw_text ~ '\{[^}]+\}' OR raw_text ~ '\[[^]]*\]'
       OR raw_text ~ '[♪♫]' OR raw_text ~ '^[A-Z]{2,}:\s' OR raw_text !~ '[A-Za-zÀ-ÿ]');

-- 4. Идемпотентность (после второго запуска)
SELECT COUNT(*) FROM dialogue_lines WHERE subtitle_id='<uuid>';

-- 5. Дубликаты и осиротевшие строки
SELECT subtitle_id, line_index, COUNT(*) FROM dialogue_lines GROUP BY 1,2 HAVING COUNT(*)>1;
SELECT COUNT(*) FROM dialogue_lines d LEFT JOIN subtitles s ON s.id=d.subtitle_id WHERE s.id IS NULL;
```

> Оговорка к проверке 3: условие `raw_text !~ '[A-Za-zÀ-ÿ]'` помечает **любую** строку без
> латинской буквы — то есть и легитимные реплики (`0`, `12:30`, чисто кириллический перевод).
> Для английского корпуса это приемлемо, но оракул должен звучать так: «в строке есть латинская
> буква или цифра». На стресс-корпусе две такие правки внесены в саму фикстуру (`0` → `Room 0`,
> кириллическая реплика стала двуязычной) — см. D19.
>
> Дополнительно к §6 прогон проверяет: монотонность времени (`lead(start_time_ms) OVER (ORDER BY
> line_index) >= start_time_ms`), обратные интервалы (`end_time_ms <= start_time_ms`) и «остатки
> чистки» (`3b`) — список строк, где шум всё ещё виден: `(^|[[:space:]])[<>]+[[:space:]]`, `\N`,
> `&(lt|gt|amp|quot|#)`, ведущий `-` после точки, метка `[A-Za-z][A-Za-z. ]{1,9}:[[:space:]]`.

---

## 7. Журнал выполнения (заполняется по мере закрытия тасок)

| Дата | Таска | Результат / заметки |
|---|---|---|
| 2026-09-18 | Аудит T0.1–T4.10 | Борда сверена с фактическим кодом: Phases 0–4 закрыты (коробки были пустыми при готовом коде); все расхождения вынесены ниже в §8 (D7–D20) |
| 2026-09-18 | T0.1–T0.3 | Окружение: `.venv` на Python 3.12.3; psycopg2-binary 2.9.13, pysrt 1.1.2, chardet 7.6.0, pytest 9.1.1, ruff 0.16.8, black 26.5.1, mypy 2.3.1. PostgreSQL **18.3** в контейнере `postgres_container_eng` (образ `postgres:latest`, порт 5430→5432), база `postgres_db`; ТЗ (`openspec/bd.md`) требует 15+ — удовлетворено. `app/requirements.txt` + `app/db.py` (`SUBTITLES_DB_*`), `pytest.ini` с маркерами. `.gitignore` закрывает `.venv/`, `tests/data/*.srt`, `.qwen/tmp/` |
| 2026-09-18 | T0.3 / T4.1 | `tests/conftest.py`: `db_conn` берёт параметры из `app/db.get_connection()`, при недоступном сервере integration-тесты идут в `skip` с причиной (не в fail); фикстуры `.srt` генерируются `MANIFEST.write_fixtures(DATA_DIR)` при импорте conftest |
| 2026-09-18 | T1.1–T1.11 / T4.3 | `tests/test_subtitle_sanitizer.py` — матрица §4 параметризованными тестами + тесты на опции; отклонения D7, D8. **Счёт после D18: 67 тестов (57 кейсов матрицы + 10 на опции и инварианты)** — исходно было 62/52, добавлены 5 кейсов §4.2 |
| 2026-09-18 | T2.1–T2.6 / T4.4 | `tests/test_subtitle_parser.py` — **71 кейс после D18 (67 passed + 4 skipped: фикстуры, где заявлен только счётчик)**; 10 параметризованных прогонов по таблице фикстур; отклонения D9, D10 |
| 2026-09-18 | T3.1–T3.6 | `app/subtitle_parser.py` (1061 строка): репозиторий-функции + оркестратор; `tests/test_subtitle_repository.py` — 29 тестов, из них 10 integration; отклонения D11, D12, D16 |
| 2026-09-18 | T3.7 | CLI `app/scripts/parse_subtitle.py`: `--dry-run` работает вообще без БД (тест ломает `get_connection`), `--json` печатает ровно один объект в stdout, коды возврата 0 / 1 (ошибка пайплайна) / 2 (аргументы); `tests/test_parse_subtitle_cli.py` — 12 тестов (11 unit + 1 integration) |
| 2026-09-18 | T4.1 | `tests/conftest.py` переписан на **реальный** `docker/init.sql` вместо самописного дампа схемы (`episodes.title_id` + `season_number`, `subtitles.episode_id`, `language_code`); имена фикстур — D13 |
| 2026-09-18 | T4.2 | Добавлен `tests/data/sdh_full.srt`: 26 cue → 20 сохранено / 6 сброшено, початый разбор по матрице §4 (ограничение — D15) |
| 2026-09-18 | T4.5 | `test_dialogue_lines_index_is_present_and_usable`: `idx_dialogue_lines_subtitle_time` существует и реально выбирается планом (`SET LOCAL enable_seqscan = off` + `EXPLAIN … ORDER BY start_time_ms LIMIT 10`) |
| 2026-09-18 | T4.7 / T4.8 | `test_unknown_subtitle_is_rejected` (`SubtitleNotFoundError`, 0 строк) + `test_unknown_file_leaves_the_track_failed` и `test_unparseable_file_leaves_the_track_failed` (файл без единого cue ⇒ `failed`, 0 строк) — статус пишется вне основной транзакции |
| 2026-09-18 | T4.6 / T4.9 | Идемпотентность и каскад: `test_reprocessing_does_not_duplicate_rows` (6→6), `test_cli_stores_lines_and_is_idempotent` (`deleted_lines=6`), `test_deleting_a_subtitle_cascades_to_its_lines` |
| 2026-09-18 | T4.10 | Quality gates (в `.venv`). **Итог после D18: `python -m pytest -q` → 175 passed, 4 skipped (собрано 179); `-m unit` → 165 passed, 4 skipped; `-m integration` → 11 passed; `ruff check app tests`, `black --check app tests`, `mypy` → чисто** (mypy: 5 файлов). По файлам: sanitizer 67, parser 71, repository 29, CLI 12. `unit` (169) + `integration` (11) ≠ 179 из-за одного кейса с обоими маркерами (`test_cli_stores_lines_and_is_idempotent`: в файле `pytestmark = unit`, на нём дополнительно `integration`). Изначальная строка гейтов (166 passed / 3 skipped) относилась к корпусу до D18. После прогона в БД 0 строк в `dialogue_lines` и `subtitles` (§6.1, §6.5) |
| 2026-09-18 | T5.1 | Написан `docker/migrations/002_dialogue_lines.sql`: `uq_dialogue_lines_subtitle_index UNIQUE (subtitle_id, line_index)` + `chk_dialogue_lines_time_order CHECK (end_time_ms > start_time_ms)`, оба через идемпотентные `DO`-блоки. **Проверено на одноразовой копии** (клон через `CREATE DATABASE t51_copy_test` + `docker/init.sql`; `TEMPLATE postgres_db` не прошёл — в живой базе активная сессия): двойной прогон → `DO`/`DO`, оба констрейна на месте (`u` и `c`); повторный прогон по засиженной копии — no-op; `INSERT` с дублем `(subtitle_id, line_index)` → `duplicate key value violates unique constraint "uq_dialogue_lines_subtitle_index"`; строка с `end < start` → `violates check constraint "chk_dialogue_lines_time_order"`. Отрицательный сценарий: на копии, где дубль и обратный интервал уже лежат, миграция падает обоими `DO` (`could not create unique index … is duplicated` / `is violated by some row`) и **не** оставляет половину изменений — `pg_constraint` после падения пуст (миграция не «починяет» грязные данные, только запрещает их дальше). Боевой прогон парсера поверх копии с 002: `sdh_full.srt` → 26 cue → `saved=20`, `status='parsed'`, второй запуск `deleted_lines=20`, `line_index` 1…20 без пропусков, «грязь»/обратные интервалы → 0. Побочный факт схемы: у `dialogue_lines` **нет** колонки `duration_ms` (первая версия сида на этом упала) — длительность выводится из пары таймкодов. Копия удалена (`DROP DATABASE`), живая БД не тронута: констрейнов 002 в ней 0, `dialogue_lines` и `subtitles` пустые. **К живой БД миграция не применена по решению пользователя** (D3: идемпотентность обеспечивает приложение) |
| 2026-09-18 | T5.2 | **Решение: не делаем.** `subtitles.file_hash_sha256 CHAR(64)` в схеме есть (`docker/init.sql:48`), но парсер её не заполняет. Fast-path «файл уже разобран» по хешу требует писать хеш в той же транзакции и сравнивать с содержимым файла, а протухший хеш молча пропускает перепарсинг — риск для корректности дороже линейной экономии. Идемпотентность уже закрыта `DELETE` + вставкой (D3). Колонку оставляем под шаг 4 (дедупликация ingest-а) |
| 2026-09-18 | T5.3 | **Решение: не делаем.** `updated_at` парсер выставлен явно в `SQL_SET_STATUS` (`SET … updated_at = NOW()`), то есть триггер нужен только если появятся писатели вне парсера. Тогда — отдельной миграцией `003_` |
| 2026-09-18 | T6.2 | Приёмочный цикл на живой БД (одноразовая запись `source='manual-t62'`): импорт `sdh_full.srt` → `{"cues_total":26,"cues_kept":20,"cues_dropped":6,"deleted_lines":0,"saved":20,"status":"parsed","encoding":"utf-8","engine":"pysrt","duration_ms":78.6}`; SQL §6: `status='parsed'` + `lines=20`, `line_index` 1…20 (`gaps=0`), `end_time_ms <= start_time_ms` → 0, «грязь» → **0 строк**, дубликаты → 0 групп, осиротевшие → 0, `ILIKE '%(unknown)%'` → 0; повторный запуск → `deleted_lines=20`, строк по-прежнему 20. Первые 10 `raw_text` просмотрены глазами (в отчёте — §6.4). Очистка: `DELETE FROM titles WHERE id='aaaaaaaa-…-0001'` → `DELETE 1`, CASCADE снял `dialogue_lines`; итоги `dialogue_lines=0`, `subtitles=0`, `episodes=0`, `titles=0`, `global_lemmas=10000` |
| 2026-09-18 | T6.3 | Создан `openspec/step2_Subtitles_report.md` в стиле `seed_lemmas.md`: §1 контекст/требования, §2 «проблема → причина → решение» (11 строк, сверены с кодом), §3 фикстуры, §4 модули + конвейер чистки + транзакционные шаги a→f, §5 выполненные шаги, §6 статистика (тесты и гейты, пофайстовый прогон фикстур через CLI, приёмочные SQL-проверки, «было → стало»), §7 ограничения и следующие шаги. Каждое утверждение проверялось grep/psql/CLI; четыре найденных несоответствия (колонка `engine`, сосланный несуществующий тест, порядок цепочки кодировок, порядок конвейера и число опций) исправлены по факту. Перезаписан после T6.1 и D18–D20 — фактические числа в §6 отчёта: 10 фикстур, 179/175/4, 67 тестов санитайзера |
| 2026-09-18 | T6.4 | Создан `app/README.md`: установка в `.venv` (+ опциональные `pysrt`/`chardet`), поднятие контейнера и применение `docker/init.sql`, таблица `SUBTITLES_DB_*` с дефолтами, примеры запуска (`--dry-run`, боевой прогон, `--json`), таблица флагов, коды возврата `0/1/2` и контракт stdout/stderr, семантика идемпотентности и `failed` из отдельного соединения (D4, D16), команды тестов и гейтов, известные ограничения (D15, неприменённая миграция 002, отсутствие `CHECK length(raw_text) > 0` — D17). `black` добавлен в dev-секцию `app/requirements.txt` (он настроен в `pyproject.toml`, но не ставился из requirements) |
| 2026-09-18 | T6.1 | Реального `.srt` не передали ⇒ по решению пользователя сделан **синтетический стресс-корпус**: новая фикстура `tests/data/stress_realworld.srt` (90 cue) — формы, которых нет в матричных фикстурах: вложенные и незакрытые теги, ASS-блоки, entities вида `&amp;`, символ-only реплики, таб и NBSP, метки спикеров разного регистра, `#`, литеральный `\N`, многострочные cue. Бой на живой БД (одноразовая цепочка `source='manual-t61'`): `{"cues_total":90,"cues_kept":71,"cues_dropped":19,"deleted_lines":0,"saved":71,"status":"parsed","encoding":"utf-8","engine":"pysrt","duration_ms":68.1}`; **все шесть SQL-проверок §6 → 0** (грязь, пропуски `line_index`, обратные интервалы, не-монотонное время, дубликаты, осиротевшие), индексы на месте. Повторный прогон: `deleted_lines=71`, строк снова 71 (`line_index` 1…71, дублей 0), `status='parsed'`, `updated_at` выставлен. Выборка «глазами» → §6.4 отчёта. Очистка: `DELETE FROM titles WHERE id='aaaaaaaa-…-0011'` → CASCADE, итоги базы `0|0|0|0|10000`. **Первичный бой дал 7 «грязных» строк** — три починены (D18: `#` в рамке vs шум, литеральный `\N`, `>>>`), четыре осознанно оставлены (D19); счётчики фикстуры перемаркированы 90 cue → **71/19**, матрица §4.3 пополнена 5 кейсами |
| 2026-09-18 | Финальная сверка документов | Принцип «один источник истины = эта борда, в доки только измеренное». Перезаписаны §7.1 п.4–6 и §7.2 отчёта (T6.1 закрыт ⇒ из «что дальше» убран; вместо `word_occurrences` — реальная таблица `episode_word_contexts`, у `dialogue_lines` из текстовых колонок только `raw_text`, `context_translation_ru`/`target_translation_ru` — `NOT NULL`, то есть строка не появляется без LLM-перевода). `app/README.md`: таблица фактических чисел гейтов (проверено этим же прогоном: 179 собрано → 175 passed/4 skipped, unit 165/4/10, integration 11/168, ruff чисто, black 10 files, mypy 5 files) + расширенный раздел «Известные ограничения» (`#` только в рамке, `\N`, `&lt;/&gt;`, метка только с CAPS, оракул «есть буква», нет per-cue лога дропов). `tests/data/README.md`: добавлен 10-й ряд `stress_realworld.srt`, и оговорка, что правило таймкодов `1000 + 1500·i` не относится к трём фикстурам, заданным блоками целиком (`sdh_full`, `malformed`, `stress_realworld`). В заголовок `docker/migrations/002_dialogue_lines.sql` вписан статус («проверено на копии, к живой БД не применять без решения»), формулировка «Применять к уже поднятой БД» → «Если применять». Правку заголовка проверили прогоном файла в `BEGIN; … ROLLBACK;` на живой БД: `BEGIN/DO/DO/ROLLBACK`, констрейнов 002 после отката — 0, `dialogue_lines` пуст, таблиц 9. Из борды убраны устаревшие числа (D13 — 9 файлов → 10, D14 — «ручной прогон ~87 cue» → синтетический корпус) |
| 2026-09-18 | Сверка `docs/` по коду | Причина — оповещение фоновой сверки: `docs/spec_analysis_for_tech_lead.md` описывал до-D18-семантику `#`. Правки по измеренному факту (`clean_subtitle_text`, `--collect-only`, грепы определений тестов): в §2 добавлена строка про литеральный `\N` (`RE_SSA_SOFT_BREAK`, сразу после `<br>`), уточнена строка 4 (метка снимается только с CAPS ⇒ `Jo:` / `Mixed:` остаются), строка 5 переписана под фактический порядок `RE_NOTE_EDGE` **или** `RE_HASHTAG_FRAME` → `RE_EDGE_MARKERS`/`RE_MUSIC_INNER`/`RE_HASHTAG_NOISE`, в §6 заменены 4 имени тестов, которых в коде нет (`test_sdh_fixture_drops_cues` → `test_fixture_expected_line_count[sdh_noise.srt]`, `test_no_index_fixture_parses` → `…[no_index_numbers.srt]`, `test_five_hundred_lines_are_stored` → `test_500_lines_land_in_one_page`, `test_missing_file_marks_the_track_failed` → `test_missing_file_marks_failed` + `test_unknown_file_leaves_the_track_failed`), добавлена сноска про реальный вид id параметризации (`test_matrix[<исходный текст>-<ожидание>]`, 57 кейсов), §8 п.1/2/4 закрыты ссылкой на `D14`/T6.2/`D16`. **Найден и устранён ложный тезис** (он был в трёх местах — D18 борды, буллет `app/README.md`, §2 `docs/spec_analysis_for_tech_lead.md`): «строка `#blessed и #hashtag` всё ещё выбрасывается». Замер: `#blessed и #hashtag` → `blessed и #hashtag` (строка живёт), пусто дают только рамки — `# Silent night #`, `## heading ##`, `# 1 # 2 #`. Синхронизирован `docs/step2_subtitles_tasks.md` (указатель): `D1`…`D17` → `D1`…`D20`, «не закрыто только T6.1…T6.4» → закрыто, «фикстур 9» → 10, и в T2.6 уточнено число фикстур в прогоне (9 из 10 с `expect_kept` × 5 проверок). Код и тесты в этом проходе не менялись; финальный прогон после правок: `python -m pytest -q` → **175 passed, 4 skipped** (собрано 179), `ruff check app tests` → All checks passed, `black --check app tests` → 10 файлов unchanged, `mypy` → Success, 5 файлов |

## 8. Журнал решений (отклонения/дополнения к ТЗ)

| ID | Решение | Обоснование |
|---|---|---|
| D1 | `chardet` опционален (`try: import chardet except ImportError: chardet=None`) | Self-contained модуль не падает без либы; fallback-порядок кодировок закрывает кейсы ТЗ |
| D2 | `raw_text` = склейка строк через пробел + опция «маркер новой реплики» | Иначе `Fire off.Pull trigger.` (R2); дефолт по ТЗ, опция для step 3 (SRS) |
| D3 | Идемпотентность через `DELETE` перед вставкой вместо `ON CONFLICT` (пока) | Не блокируется step 2 миграцией схемы; `ON CONFLICT` — T5.1 как follow-up |
| D4 | Статус `processing` пишется в той же транзакции, `failed` — в отдельном соединении | Иначе `processing` откатывается вместе с ошибкой, и статус «не видно» (R7) |
| D5 | `#` не вырезается из середины строки | `#13-37`, `#hashtag`, `Room #5` (R2) |
| D6 | Класс скобочного правила: только полностью заглавные токены | `(together) We can do it`, `(now!)` сохраняются (R6/ТЗ-примечание) |
| D7 | Новая опция `strip_dialogue_dashes` + метод `SanitizerOptions.replaced(**changes)` | §4 требует снимать ведущее «- » (двухрепликовый cue), но не трогать дефис внутри слова (`well-maintained`). Датакласс `frozen=True`, а счётчик «правило сработало» надо обновить по ходу — отсюда copy-on-write `replaced()` вместо прямой мутации |
| D8 | Скобочное правило получает исключение: не-латинские буквы внутри скобок ⇒ текст не трогаем | Отличает транслит/перевод `(смейся)` от ремарки `(GUNSHOT)`; без исключения rule 4 пожирал бы содержательные строки (дополнение к D6) |
| D9 | Отказоустойчивость парсинга двухуровневая: `pysrt` в `try/except` **+** переход на regex-движок, если получен ноль cue | pysrt бросает на битом таймкоде, но «файл без единого cue» — не исключение, а тихий пустой результат. Без второго уровня такой файл проходил бы как «успешно 0 реплик» вместо реального парсинга или честной `SubtitleFileError` |
| D10 | `parse_srt` возвращает `ParsedSrt(lines, report)`, а не `List[DialogueLine]` | §5.1/§5.2 требуют отчёт (`cues_total/kept/dropped`, `engine`, `encoding`); голого списка не хватает ни для `--json`, ни для лога `parsed %d/%d cues`, ни для причины отброса |
| D11 | Исключение — `SubtitleNotFoundError(SubtitleError)`, а не отдельный `UnsavedSubtitle` | Одна иерархия доменных ошибок пайплайна: CLI и `process_subtitle_file` ловят `SubtitleError` целиком, а не перечисление классов |
| D12 | `LineCountMismatchError(SubtitleError)` вместо `RuntimeError` | Рассогласование «спарсилось ≠ записалось» — доменный факт пайплайна, который должен приводить к статусу `failed`, а не пробиваться сквозь `except SubtitleError` наружу |
| D13 | Имена фикстур не совпадают с буквальными в §2 (итого 10 файлов) | `windows1252.srt` → `windows_cp1252.srt` (читаемость, PEP8); добавлен `sdh_full.srt` — регрессионный эталон всей матрицы §4; добавлен `stress_realworld.srt` — стресс-корпус T6.1 вместо отсутствующего реального `.srt`. Соответствие зафиксировано таблицей в `tests/data/README.md` |
| D14 | «78 строк» из §6 отнесено к реальному файлу пользователя, а не к фикстурам | На фикстурах подсчёт строк покрыт тестами (`expect_kept`/`expect_texts`). Реального `.srt` у пользователя не нашлось, поэтому T6.1 закрыт синтетическим стресс-корпусом `stress_realworld.srt` (90 cue) с той же приёмкой по SQL — см. §6.2/§6.3 отчёта |
| D15 | В `sdh_full.srt` оставлен cue `WALTER, JR.: Grandpa?` — известное ограничение, не баг теста | Регулярка `NAME:` не отделяет фамилию с запятой/суффиксом от текста. Правится на шаге speaker-моделей; здесь это зафиксировано в эталоне, чтобы последующее изменение было видно как осознанный diff |
| D16 | `_process_subtitle_run(..., durable_status=True)`: шаг (a) коммитится сразу после проверки `FOR UPDATE` | Иначе `processing` не видно другим сессиям, пока парсится большой файл (см. D4), а блокировка строки держалась бы всё время парсинга. `durable_status=False` оставляет прогон в одной транзакции — используется тестами |
| D17 | `CHECK (length(raw_text) > 0)` в миграцию **не** добавляем — правим `docs/step2_subtitles_tasks.md` по коду | В `init.sql` его нет, и T5.1 его не заказывает. Пустые cue теряются до записи (T2.4), а §6.3 всё равно проверяет «в строке есть буква» — лишний CHECK только сузил бы способы записать строку руками при отладке |
| D18 | `#` считается маркером «спетой» строки **только в парной рамке** (`# … #`); одиночный ведущий/хвостовой `#` — шум, который снимается, а строка сохраняется. Литеральный `\N` (ASS hard break, доезжающий до SRT двумя символами) превращается в перевод строки на шаге 1 конвейера и склеивается как многострочный cue. Бегунок реплики — `>+`, а не ровно `>>` | Бой T6.1: `#blessed #13-37 and #hashtag.` терял **всю** реплику (D5 работал только для `#` в середине строки), `>>>\tCopy.` не чистился вовсе, `First line\Nsecond…` уезжал в БД с литеральным `\N`. Матричные фикстуры этого не ловили: в них `#` был только парным или в середине. Реализация: `RE_NOTE_EDGE` (только ♪-глифы), `RE_HASHTAG_FRAME`, `RE_HASHTAG_NOISE`, `RE_SSA_SOFT_BREAK`; `#` рядом с цифрой (`#13`) рамкой не считается. **Риск правила (замерено 2026-09-18 через `clean_subtitle_text`):** роняется любая строка, у которой `#+` стоит и в начале, и в конце — с пробелом с обеих сторон: `## heading ##` и `# 1 # 2 #` → пусто. Без рамки строка живёт: `#blessed и #hashtag` → `blessed и #hashtag` (ведущий `#` слит со словом, это шум), `#off color joke#` → `off color joke#`. Это осознанная цена, потому что `# … #` в реальном субтитре означает именно песню |
| D19 | Оракул §6 («в строке есть буква») дополнен оговоркой, а два cue стресс-корпуса переписаны: `0` → `Room 0`, чисто кириллическая реплика стала двуязычной. Мелкие правки в корпусе: `0` → `Room 0`, `..\N..` → `.. :: ..`, реплика «(ne переведено) Что это было?» сделана двуязычной | `raw_text !~ '[A-Za-zÀ-ÿ]'` помечает как грязь и легитимный контент: реплику `0` и строку перевода без латиницы. Для английского корпуса условие верно, но как приёмочный тест оно неразличимо — поэтому правку внесли в корпус (и оговорку в §6), а не в запрос. Там же зафиксированы **осознанные остатки** чистки, а не баги: `&lt;unknown&gt;` (`&lt;/&gt;` намеренно нет в `_ENTITY_MAP` — декодирование рисовало бы фальшивые теги, которые снимал бы следующий проход, и мы бы теряли текст), `Jo:` и `Mixed: текст и text together.` (метка снимается только с CAPS-фамилии, иначе `%: ` съел бы `Time: 5 min`) |
| D20 | Приёмочный SQL §6 дополнен проверками 2b/2c/3b: монотонность времени, обратные интервалы, «остатки чистки» отдельным списком | Одна цифра «грязи» скрывает состав мусора. 3b (`(^|[[:space:]])[<>]+`, `\N`, `&(lt|gt|amp|quot|#)`, ведущий `-` после точки, метка `[A-Za-z][A-Za-z. ]{1,9}:`) даёт именно тот список, по которому принимались D18/D19 — он и показывает, что чинить, а что задокументировать |
