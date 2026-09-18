# `app/` — парсер субтитров (шаг 2)

Модуль `app.subtitle_parser` читает `.srt`, чистит текст от SDH/ASS-мусора и складывает
реплики диалога в таблицу `dialogue_lines`. `app/scripts/parse_subtitle.py` — тонкий CLI
вокруг него, `app/db.py` — единственное место, где соединяются с PostgreSQL.

Полный объём работ, критерии приёмки и журнал — борда
[`../openspec/step2_Subtitles_tasks.md`](../openspec/step2_Subtitles_tasks.md).

## Установка

Виртуальное окружение лежит в `.venv/` (в git не попадает, восстанавливается из
`app/requirements.txt`):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r app/requirements.txt
```

`pysrt` и `chardet` опциональны: без них работают regex-движок разбора и цепочка пробных
кодировок (`app/subtitle_parser.py`, `fallback_encodings`).

## База данных

Контейнер: `docker/docker-compose.yml` → PostgreSQL 18.3 (образ `postgres:latest`, ТЗ требует 15+),
порт `5430` наружу.

```bash
docker compose -f docker/docker-compose.yml up -d
docker exec -i -e PGPASSWORD=postgres_password postgres_container_eng \
  psql -U postgres_user -d postgres_db -v ON_ERROR_STOP=1 < docker/init.sql
```

Строка подключения собирается в `app/db.py` (`get_connection()`), приоритет —
явные аргументы функции → переменные окружения → дефолты compose:

| Переменная | Дефолт |
|---|---|
| `SUBTITLES_DB_HOST` | `localhost` |
| `SUBTITLES_DB_PORT` | `5430` |
| `SUBTITLES_DB_NAME` | `postgres_db` |
| `SUBTITLES_DB_USER` | `postgres_user` |
| `SUBTITLES_DB_PASSWORD` | `postgres_password` |
| `SUBTITLES_DB_TIMEOUT` | `10` (секунд на коннект) |

Строку `subtitles` (трек сабтитров) создаёт не этот парсер, а загрузка субтитра (шаг 1/3 по
`openspec/bd.md`): парсер только заполняет `dialogue_lines` и двигает `status` между
`pending → processing → parsed/failed`.

## Запуск

```bash
source .venv/bin/activate

# 1. Без базы вообще: декодирование + парсинг + санитайзер, ничего не пишется
python -m app.scripts.parse_subtitle --file path/to/episode.srt --dry-run

# 2. Боевой прогон: строчка subtitles должна существовать
python -m app.scripts.parse_subtitle --subtitle-id 0f1c…uuid --file path/to/episode.srt

# 3. Отчёт одним JSON-объектом в stdout (для автоматизации) + подробный лог
python -m app.scripts.parse_subtitle --subtitle-id … --file … --json -v
```

| Флаг | Назначение |
|---|---|
| `--file` / `--path` | путь к `.srt` (обязателен) |
| `--subtitle-id` / `--id` | uuid строки `subtitles`; обязателен без `--dry-run` |
| `--encoding` | форсировать кодек вместо автоопределения и цепочки fallback |
| `--dry-run` | только разобрать и почистить; **соединения с БД нет вообще** |
| `--json` | вывести ровно один JSON-объект со статистикой прогона |
| `--page-size` | строк на один bulk-insert (`mogrify` + `execute_batch`), по умолчанию 500 |
| `--keep-music-lines` | маркеры ♪/[MUSIC] вырезаются, но строка сохраняется, а не выбрасывается |
| `--keep-speaker-labels` | не срезать `WALTER:` |
| `--keep-line-breaks` | многострочные cue не склеиваются в одну реплику |
| `-v` | DEBUG-лог |

Коды возврата: `0` — успех (в том числе «все cue вычистились в ноль», `saved: 0`), `1` — ошибка
пайплайна (файла нет, файл пуст или не разбирается, `subtitle_id` не найден, БД недоступна),
`2` — ошибка аргументов (`--subtitle-id` обязателен без `--dry-run`). Логи (`logging`) пишутся в
`stderr`, а результат — в `stdout`: сводка в текстовом режиме либо ровно один JSON-объект при
`--json`, поэтому вывод перенаправляется в конвейер без мусора. При `--json` ошибка тоже
закрывается JSON-объектом (`"status": "error"` + `"error": "SubtitleFileError: …"`), а не
текстом.

Повторный прогон по тому же `subtitle_id` безопасен: старые строки удаляются перед
вставкой, поэтому строки не дублируются, а `line_index` перенумеровается заново.
`status='failed'` пишется из отдельного соединения и коммитится сразу, чтобы запись о
сбое доживала до отката основной транзакции (`D4`/`D16` на борде).

## Тесты

```bash
source .venv/bin/activate
python tests/data/make_fixtures.py   # .srt-фикстуры генерируются, а не лежат в git
python -m pytest -q                  # unit всегда; integration — если доступна БД, иначе skip
python -m pytest -q -m integration   # только боевые прогоны через реальный docker/init.sql
```

Маркеры объявлены в `pytest.ini`. Гейты качества: `ruff check app tests`,
`black --check app tests`, `mypy` (настройки в `pyproject.toml`, strict для `app/`).

Фактические числа прогона (2026-09-18, Python 3.12.3, БД в Docker запущена):

| Команда | Результат |
|---|---|
| `python -m pytest -q` | собрано 179 → **175 passed, 4 skipped** (4 skip — прогоны `test_fixture_declared_texts` для фикстур, где заявлен только счётчик: `basic_english.srt`, `no_index_numbers.srt`, `utf8_bom.srt`, `stress_realworld.srt`) |
| `python -m pytest -q -m unit` | 165 passed, 4 skipped, 10 deselected |
| `python -m pytest -q -m integration` | 11 passed, 168 deselected |
| `ruff check app tests` · `black --check app tests` · `mypy` | чисто · 10 файлов unchanged · Success, 5 файлов |

По файлам: `test_subtitle_sanitizer.py` — 67 тестов (57 параметризованных кейсов матрицы §4 +
10 на опции и инварианты), `test_subtitle_parser.py` — 71, `test_subtitle_repository.py` — 29,
`test_parse_subtitle_cli.py` — 12. `.srt`-фикстур 10 (`tests/data/README.md`), они генерируются
`tests/data/make_fixtures.py` и в git не лежат.

## Известные ограничения

- Файл больше `MAX_FILE_BYTES` (10 МиБ) отвергается как «красный флаг» — обычный `.srt` весит
  десятки килобайт. Если реально нужен больше, константу в `app/subtitle_parser.py` правят осознанно.
- `#` считается маркером «спетой» строки **только в парной рамке** (`# … #`): одиночный ведущий или
  хвостовой `#` снимается как шум, а строка сохраняется (`D18`). Замерено: `# Go ahead` → `Go ahead`,
  `#blessed и #hashtag` → `blessed и #hashtag`, `# Silent night #` → пусто. Цена правила — любая
  строка, где `#+` стоит и в начале, и в конце (с пробелами с обеих сторон), выбрасывается:
  `## heading ##`, `# 1 # 2 #` → пусто.
- Литеральный `\N` (ASS hard break, доезжающий до `.srt` двумя символами) разворачивается в перевод
  строки на шаге 1 конвейера и склеивается как многострочный cue (`D18`); с `--keep-line-breaks`
  остаётся переводом строки.
- `&lt;` / `&gt;` намеренно **не** декодируются (их нет в `_ENTITY_MAP`, `D19`): декодирование
  рисовало бы фальшивые теги, которые снял бы следующий проход чистки, и текст потерялся бы. Поэтому
  `Radio says &lt;unknown&gt;.` доезжает до базы как есть.
- Метка диктора снимается только с CAPS-фамилии (`SARAH:`, `OFFICER 2:`), поэтому `Jo: Neither is
  this.` и `Mixed: текст и text together.` остаются в `raw_text` — иначе `%: ` съел бы `Time: 5 min`.
- Реплика вида `WALTER, JR.: Grandpa?` не чистится — регулярка меток спикера не отделяет
  фамилию с запятой/суффиксом от текста (`D15` на борде). Эталон это фиксирует намеренно.
- `dialogue_lines` не имеет `UNIQUE (subtitle_id, line_index)` в `init.sql`; страховка от
  гонок лежит в накладной миграции `../docker/migrations/002_dialogue_lines.sql`. Она написана и
  проверена на одноразовой копии схемы (идемпотентность, констрейны, прогон парсера поверх копии),
  но **к живой БД не применена** — решение пользователя (`T5.1`/`T6.4` на борде).
- Пустые cue в базу не попадают, но `CHECK (length(raw_text) > 0)` в схеме нет — контроль
  на стороне приложения (`D17`).
- Приёмочный SQL «в строке есть буква» (`raw_text !~ '[A-Za-zÀ-ÿ]'`) неразличим на не-английских
  строках: легитимный перевод без латиницы попадёт в «грязь». Для английского корпуса условие верно
  (`D19`).
- Дропы видны только счётчиками (`cues_total/kept/dropped`) плюс `drop_reason` у отдельной строки:
  в журнале CLI не видно, *какой именно* cue выпал и почему. Разбирается фикстурами и `--keep-*`,
  `--verbose-drops` пока не делали.
