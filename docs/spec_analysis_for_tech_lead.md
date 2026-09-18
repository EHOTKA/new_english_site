# Анализ шага 2 для техлида: парсинг субтитров (SRT → PostgreSQL)

Дата: 2026-07-27. Спека: `openspec/step2_Subtitles.md`. Схема: `openspec/bd.md`,
`docker/init.sql`. Файл сопровождает борду [`../openspec/step2_Subtitles_tasks.md`](../openspec/step2_Subtitles_tasks.md)
(сам `docs/step2_subtitles_tasks.md` сведён к указателю на неё).

Цель документа — до кода показать: как именно требования спеки ложатся на модуль,
где спека упрощает реальность, где правила очистки могут навредить, и чем это
проверяется. Ссылки вида §2.1.2(4) — на нумерацию спеки.

> **Актуализация (2026-09-18).** Документ написан до кода; правится по факту. После боевого
> прогона изменилось правило «музыки» (`D18`): спетая строка — это нотный глиф на краю **или**
> парная рамка `# … #`, одиночный `#` больше строку не роняет; литеральный `\N` разворачивается
> в перевод строки; бегунок реплики — `>+`. Числа прогонов и журнал решений — борда
> [`../openspec/step2_Subtitles_tasks.md`](../openspec/step2_Subtitles_tasks.md) (§7, §8 `D1`…`D20`);
> здесь они не дублируются.

## 1. Контракт

| Аспект | Требование спеки | Фактическая реализация |
| --- | --- | --- |
| Вход | путь к `.srt` + `subtitle_id` | `process_subtitle_file(conn, subtitle_id, file_path, *, encoding=None, options=None, page_size=500, durable_status=True, failure_conn=None, started_at=None)` |
| Выход | строки в `dialogue_lines`, статус → `parsed` | то же; функция возвращает число вставленных строк; метрики — в `ParseReport` |
| Обработка ошибок | лог + `status='failed'` + проброс исключения | §5(f): `logger.exception` → `rollback` → запись `failed` отдельным соединением → `raise` |
| Совместимость | сигнатура §3.2 | `def process_subtitle_file(conn, subtitle_id, file_path)` — позиционные аргументы совпадают, остальное keyword-only |

Публичное API: `clean_subtitle_text(text) -> str`,
`SubtitleParserService.parse_srt(file_path) -> list[DialogueLine]` (возвращает
`ParsedSrt(lines, report)`, т.е. совместимо по индексируемому первому элементу),
`parse_srt_string(content)`, `SubtitleParserService.process_and_save(...)`.

## 2. Правила очистки §2.1 → реализация

| № | Правило | Регулярка / метод | Порядок |
| --- | --- | --- | --- |
| — | HTML-сущности `&quot;` `&apos;` `&nbsp;` `&hellip;` | `RE_ENTITIES` + `_ENTITY_MAP` | до тегов (литерал `&quot;` иначе «съедает» `>` тега); `&lt;`/`&gt;` не декодируем, чтобы не подделывать теги |
| 1 | `<i>` `</i>` `<b>` `<font …>` | `RE_HTML_LINE_BREAK` (`<br>` → `\n`), затем `RE_HTML_TAGS` | теги снимаются без пробела, иначе «hot`<b>`dog`</b>`» развалится |
| 1 | литеральный `\N` (ASS hard break, доезжает до `.srt` двумя символами) | `RE_SSA_SOFT_BREAK` → `\n` | сразу после `<br>`, до снятия тегов; дальше склейка через `merge_lines` (`D18`) |
| 1 | `{…}` (SSA/ASS) | `RE_SSA_TAGS` дважды | `{\an8}текст{\an2}` |
| 2 | `[door creaks]` | `RE_BRACKETS_SDH`, `RE_BRACKET_DANGLING_OPEN` | снято всегда, независимо от регистра |
| 3 | `(CHUCKLES)` | `RE_PAREN_CONTENT` + `_is_sdh_paren` | только ALL-CAPS без строчных, ≤4 слов, ≤32 символов, без `.?!`, не аббревиатура, не токен из `PAREN_KEEP_TOKENS` |
| 4 | `WALTER:` | `RE_SPEAKER_LABEL` (2 альтернативы: с текстом и одиночный ярлык) | апостроф и цифры внутри ярлыка разрешены (`DON'T:`, `OFFICER 1:`, `DR. SMITH:`); метка снимается только с CAPS, поэтому `Jo:` / `Mixed:` остаются |
| 5 | `♪ … ♪`, `# … #` | «спетая» строка = `RE_NOTE_EDGE` (нашёл) **или** `RE_HASHTAG_FRAME` (рамка) → `""` при `drop_music_lines`, иначе снятие маркеров `RE_EDGE_MARKERS`; внутренние `♪` → `RE_MUSIC_INNER`; одиночный `#` на краю → `RE_HASHTAG_NOISE` | `#` роняет строку **только в парной рамке**: `#13-37` — номер, `#blessed` — хэштег, `# text` — шум (`D18`). Замерено: `# Go ahead` → `Go ahead`, `#blessed и #hashtag` → `blessed и #hashtag`. Цена правила — строка, где `#+` стоит и в начале, и в конце с пробелами, читается как песня: `# Silent night #`, `## heading ##`, `# 1 # 2 #` → пусто |
| 6 | `- Hey.` | `RE_LEADING_DASH` (`-`/`–`/`—`, бегунок `>+` по `D18`), `RE_TRAILING_DASH` | кавычки и `…` на краях НЕ срезаются |
| 7 | лишние пробелы/пустые строки | `RE_MULTI_WS`, `RE_SPACE_BEFORE_PUNCT`, `merge_lines` | склейка multiline-реплик одним пробелом |
| 8 | строка без букв/цифр | `RE_HAS_ALNUM` | «...», «-», «♪» → пустая строка |
| — | пустой результат → реплика удаляется | `parse_cues` | §2.1.2(9): `line_index` перенумеровывается без разрывов |

## 3. Главные риски и как они сняты

1. **Пере-очистка** опаснее недочищення: §4.2 требует «не удалять лишнего».
   Стражи: `_is_sdh_paren` (5 проверок), `PAREN_KEEP_TOKENS` (`(USA)`, `(TV)`, `(Dr)`),
   `RE_ABBREV_TAIL` (`(U.S.A.)`, `(No. 5)`), `#(?![0-9])` (`#13-37`), `[ \t]+` после
   двоеточия (`12:30`, `CHAPTER 12:30`), отказ от срезания кавычек (`"Fire off."`),
   отсутствие часового пояса в `line_index`.
2. **Ошибки в самой спеке.** §2.1.2(4) хочет убрать `COACH:` из «строки только
   `COACH:`» — одинарной регуляркой `^NAME:\s+` это не делается, добавлена вторая
   альтернатива. §8 обещает «10 cues → 8 lines» при 12 кейсах; в матрицу добавлена
   колонка «что происходит с остальными кейсами», и заодно закрыто противоречие
   §2.1.2(3) (пример `(together) We can do it (CHUCKLES).` против правила ALL-CAPS).
3. **Расхождения спеки со схемой.** §1.1 говорит «status enum» — тип называется
   `subtitle_status_enum`; §2.4 вставляет `id, created_at` — это defaults БД, поэтому
   в `SQL_INSERT_DIALOGUE_LINES` их нет (иначе в `uuid` поехала бы строка
   `'gen_random_uuid()'`); §2.5 не делает `DELETE`, но `UNIQUE(subtitle_id, line_index)`
   в схеме отсутствует → повторный прогон удваивал бы строки, добавлен `DELETE` +
   сверка `COUNT(*)` (только так можно заметить, что вставилось меньше).
4. **Ловушки psycopg2.** `str(SubtitleStatus.PROCESSING)` по-разному ведёт себя в
   3.10/3.11+ → `_status_value()` берёт `.value`; смешивать `%s` и `%(name)s` в одном
   запросе нельзя → `STATUS_CAST` именованный; enum-колонка требует явный каст.
5. **Двойной парсер.** `pysrt` не умеет ни BOM, ни `MM:SS`, ни отсутствие нумерации,
   и бросает исключение на файле целиком → есть собственный `RE_CUE_BLOCK` (DOTALL,
   опциональный индекс, `.` как разделитель мс) и деградация не в прогон «0 реплик»,
   а в понятную ошибку «файл не похож на SRT».
6. **Отсутствующие зависимости.** `pysrt`/`chardet` импортируются лениво: без них
   модуль работает (собственный парсер, декодирование без автоопределения). Обязателен
   только `psycopg2`.

## 4. Правила §2.3 и §2.4

- Таймкоды → целые миллисекунды; допускаются `,` и `.` как разделитель мс, 1–3 цифры
  мс и формат `MM:SS,mmm`; невалидный токен → `SubtitleFileError`.
- Аномалия интервала: вместо `pass` (в примере спеки) — `logger.warning` и
  `end = start + DEFAULT_CUE_DURATION_MS`, иначе CHECK `end_time > start_time`
  отвергает весь батч.
- Bulk: `execute_batch(cursor, SQL_INSERT_DIALOGUE_LINES, rows, page_size=500)`,
  строки — словари под именованные плейсхолдеры.

## 5. Оркестрация §5: чем оправдан каждый шаг

| Шаг | Зачем |
| --- | --- |
| (a) `SELECT … FOR UPDATE` | плохой uuid иначе даёт `ForeignKeyViolation`, транзакция абортируется и `failed` записать нельзя; плюс блокировка гонки двух воркеров на одном треке |
| commit после `processing` | мониторинг видит «в работе» во время долгого парсинга; отключается `durable_status=False` |
| `DELETE` своих строк | идемпотентность повторного прогона |
| `COUNT(*)` после вставки | detects потерянные/не вставленные строки, сравнивает с числом парсинга |
| `failed` отдельным соединением | после rollback исходная транзакция чиста, а статус обязан пережить откат; если соединения нет — warning, исходная ошибка не подменяется |

## 6. Спека → код → тест

| Пункт спеки | Где в коде | Чем проверяется |
| --- | --- | --- |
| 1.1 вход/выход, 3.2 интеграция | `process_subtitle_file` | `test_happy_path_sequence`, `test_end_to_end_ingestion` |
| 2.1.1(1) HTML | `RE_HTML_LINE_BREAK`, `RE_HTML_TAGS` | матрица `MATRIX` кейсы 1 |
| 2.1.1(2) SSA | `RE_SSA_TAGS` | кейсы 2, `test_fixture_declared_texts[sdh_noise.srt]` |
| 2.1.1(3) `[...]` | `RE_BRACKETS_SDH` | кейсы 3 |
| 2.1.1(4) `(...)` ALL-CAPS | `_is_sdh_paren` | кейсы 4 |
| 2.1.1(5) `NAME:` | `RE_SPEAKER_LABEL` | кейсы 5, `speaker_labels.srt` |
| 2.1.1(6) музыка | `RE_NOTE_EDGE`, `RE_HASHTAG_FRAME` (+ `RE_EDGE_MARKERS` для снятия) | кейсы 7, `music_and_ellipsis.srt`, `stress_realworld.srt` |
| 2.1.1(7) дефисы | `RE_LEADING_DASH`, `RE_TRAILING_DASH` | кейсы 6 |
| 2.1.2(1–3) HTML/SSA/music | `clean()` | `test_matrix` |
| 2.1.2(4) перенос строк | `merge_lines`, `RE_SSA_SOFT_BREAK` | кейс 8, `test_merge_lines_can_be_disabled` |
| 2.1.2(5) unicode | `_TYPOGRAPHY`, `RE_FORMAT_CHARS` | кейс 10, `test_no_control_characters_left` |
| 2.1.2(6) сжатие пробелов | `RE_MULTI_WS` | `test_no_double_spaces_after_merge` |
| 2.1.2(7) `[MUSIC]` | `RE_BRACKETS_SDH` | кейс 3, `test_fixture_expected_line_count[sdh_noise.srt]` |
| 2.1.2(8) пустые строки | `RE_HAS_ALNUM` | кейсы 9 |
| 2.1.2(9) пустая реплика | `parse_cues` | `test_all_cues_noise_returns_zero_lines_without_error` |
| 2.1.3 структура | `app/subtitle_parser.py` | `test_both_documented_entry_points_agree` |
| 2.2 `.srt`-парсер | `_extract_cues_*` | `test_crlf_and_lf_content_parse_identically`, `test_fixture_expected_line_count[no_index_numbers.srt]` |
| 2.3 кодировки | `_read_file_with_fallback` | `test_cp1252_fixture_decodes_without_mojibake`, `test_bom_fixture_has_no_bom_in_text` |
| 2.3 таймкоды | `_srt_time_to_ms` | `test_timecode_to_ms` |
| 2.4 bulk 500 | `insert_dialogue_lines` | `test_bulk_insert_uses_one_batched_call`, `test_500_lines_land_in_one_page` (integration) |
| 2.4 enum-статус | `set_status`, `_status_value` | `test_status_sql_uses_named_parameters_and_enum_cast`, `test_set_status_updates_the_row` |
| 2.5 (a)–(f) | `process_subtitle_file` | `test_missing_subtitle_never_reaches_the_parser`, `test_missing_file_marks_failed`, `test_unknown_file_leaves_the_track_failed` (integration), `test_row_count_verification_keeps_failed_state` |
| 2.6 ошибки | классы исключений | `test_missing_file_raises_domain_error`, `test_non_subtitle_content_raises` |
| 3.1/3.2 шаг 3 | — (вне объёма шага 2) | зафиксировано: `raw_text` уже очищен, шаг 3 читает `SELECT raw_text` |
| 4.1–4.4 приёмка | модули тестов | см. §7 |
| 8 матрица | `SubtitleSanitizer` | `MATRIX` в `test_subtitle_sanitizer.py` |

## 7. Матрица приёмки §8 → тесты

id кейсов ниже — сокращения: реальный параметр pytest выглядит как
`test_matrix[<исходный текст>-<ожидание>]` (`MATRIX` в `tests/test_subtitle_sanitizer.py`,
57 кейсов после D18).

| # | Кейс | Тест |
| --- | --- | --- |
| 1 | `<i>Put it down, Walter.</i>` → `Put it down, Walter.` | `test_matrix[<i>...]` |
| 2 | `{\an8}I am the one who knocks.{\pos(20, 200)}` | `test_matrix[{...}]` |
| 3 | `[door creaks] Where is he?` | `test_matrix[[door creaks]...]` |
| 4 | `[Upbeat music]` → пусто | `test_matrix[[Upbeat music]]` |
| 5 | `(CHUCKLES) We can do it (together).` | `test_matrix[(CHUCKLES)...]` |
| 6 | `WALTER: Put it down.` | `test_matrix[WALTER:...]` |
| 7 | `♪ Fair is foul... ♪` | `test_matrix` + `music_and_ellipsis.srt` |
| 8 | `- Hey.` / `- Wake up.` | `test_matrix[- Hey.\n-Wake up...]` |
| 9 | `[suspenseful music plays]` | `test_matrix` |
| 10 | `"Fire off.\n" "Pull trigger."` | `test_matrix` (кавычки сохранены) |
| 11 | `JESSE: Yo, Mr. White` / `MAN ON TV: ...` | `test_matrix` + `speaker_labels.srt` |
| 12 | `{\an8}You can't[bleep]be serious` | `test_matrix` + `sdh_noise.srt` |
| — | «10 cues → 8 lines» | противоречие с числом кейсов: в `ParseReport` добавлены `cues_total`/`cues_dropped`, а в фикстуре `sdh_noise.srt` проверяется 8 cues → 6 lines (2 шумовых) |

## 8. Что проверить в первую очередь при ревью

1. Поведение на реальном corpus-файле: `pytest -m integration` + CLI на 500-строчном `.srt`.
   Закрыто иначе: реального `.srt` под рукой не оказалось, вместо него — синтетический стресс-корпус
   `stress_realworld.srt` (90 cue) и SQL-приёмка §6 борды; причина — `D14`, числа — §6 отчёта.
2. Не слишком ли агрессивно `_is_sdh_paren`/`RE_BRACKETS_SDH` (выборка 100 реплик).
   Частично снято T6.2: «глазами» просмотрены 10 строк `raw_text` + все кейсы, добавленные боем
   (`D18`/`D19`) — легитимный контент скобки не съедают. Выборка в 100 реплик остаётся: её сделают
   на реальном корпусе, когда он появится.
3. Нужен ли срез кавычек и `...` на краях строки (сейчас — нет).
4. Хватает ли `durable_status=True` или `processing` лучше писать отдельным соединением — решено в
   `D16`: `processing` коммитится сразу после `FOR UPDATE`, а `failed` пишется отдельным
   соединением и переживает откат основной транзакции.
