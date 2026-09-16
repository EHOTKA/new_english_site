# OpenSpec: Subtitle Vocabulary Learning Engine (Data Layer)
Version: 1.0.0
Engine: PostgreSQL 15+
Encoding: UTF-8

## 1. Domain Overview
Сервис анализирует субтитры к фильмам и сериалам, лемматизирует текст реплик, выделяет лексику с таймкодами и сопоставляет ее с индивидуальным словарным запасом пользователя для подготовки персональных колод перед просмотром.

---

## 2. Entity Relational Requirements

### 2.1 Catalog & Media Entities
1. **titles** (Каталог медиа)
   - id: UUID / BIGSERIAL, Primary Key.
   - external_imdb_id: VARCHAR(16), Unique, Nullable (e.g., "tt0903747").
   - external_tmdb_id: INTEGER, Unique, Nullable.
   - type: ENUM ('movie', 'series').
   - name: VARCHAR(255), Not Null.
   - original_name: VARCHAR(255), Not Null.
   - release_year: SMALLINT.
   - poster_url: TEXT.
   - created_at, updated_at: TIMESTAMPTZ.

2. **episodes** (Серии / эпизоды)
   - Примечание: Для фильмов создается 1 связанный фиктивный эпизод (season=1, episode=1) или выстраивается опциональная связь.
   - id: UUID / BIGSERIAL, Primary Key.
   - title_id: FK -> titles(id) ON DELETE CASCADE.
   - season_number: SMALLINT, Not Null (e.g., 1).
   - episode_number: SMALLINT, Not Null (e.g., 3).
   - name: VARCHAR(255).
   - duration_seconds: INTEGER.
   - created_at: TIMESTAMPTZ.
   - Constraint: UNIQUE (title_id, season_number, episode_number).

3. **subtitles** (Метаданные дорожек субтитров)
   - id: UUID / BIGSERIAL, Primary Key.
   - episode_id: FK -> episodes(id) ON DELETE CASCADE.
   - language_code: VARCHAR(10) DEFAULT 'en', Not Null.
   - source: VARCHAR(50) (e.g., 'opensubtitles', 'user_upload').
   - file_hash_sha256: CHAR(64), Nullable (для дедупликации дорожек).
   - status: ENUM ('pending', 'processing', 'parsed', 'failed').
   - raw_storage_path: TEXT (ссылка на S3/локальное хранилище .srt файла).
   - created_at, updated_at: TIMESTAMPTZ.

---

### 2.2 Subtitle Content & Context Entities
4. **dialogue_lines** (Конкретные реплики диалогов с таймингом)
   - id: UUID / BIGSERIAL, Primary Key.
   - subtitle_id: FK -> subtitles(id) ON DELETE CASCADE.
   - line_index: INTEGER, Not Null (порядковый номер реплики в .srt файле).
   - start_time_ms: INTEGER, Not Null (время начала в миллисекундах).
   - end_time_ms: INTEGER, Not Null (время конца в миллисекундах).
   - raw_text: TEXT, Not Null (очищенная строка реплики).
   - created_at: TIMESTAMPTZ.
   - Indexes: (subtitle_id, start_time_ms).

---

### 2.3 Lexical & Enrichment Entities
5. **global_lemmas** (Глобальный справочник лемм английского языка)
   - id: UUID / BIGSERIAL, Primary Key.
   - lemma: VARCHAR(100), Not Null, Unique (в нижнем регистре, например, "run", "bail on").
   - part_of_speech: VARCHAR(20) (e.g., 'verb', 'noun', 'idiom', 'phrasal_verb').
   - cefr_level: ENUM ('A1', 'A2', 'B1', 'B2', 'C1', 'C2', 'unknown') DEFAULT 'unknown'.
   - frequency_rank: INTEGER, Nullable (ранг частотности в корпусе языка: 1..50000+).
   - created_at: TIMESTAMPTZ.
   - Indexes: lemma, cefr_level, frequency_rank.

6. **episode_word_contexts** (Разбор слов конкретной серии, обогащенный контекстом)
   - Суть: Единица знания для изучения. Связывает лемму, конкретный эпизод и строку диалога.
   - id: UUID / BIGSERIAL, Primary Key.
   - episode_id: FK -> episodes(id) ON DELETE CASCADE.
   - line_id: FK -> dialogue_lines(id) ON DELETE CASCADE.
   - lemma_id: FK -> global_lemmas(id) ON DELETE CASCADE.
   - inflected_form: VARCHAR(100) (как слово звучало в реплике, например "bailed").
   - context_translation_ru: TEXT, Not Null (перевод всей реплики, сгенерированный LLM).
   - target_translation_ru: VARCHAR(255), Not Null (контекстный перевод именно этого слова в инфинитиве).
   - short_explanation_ru: TEXT (краткое пояснение значения / идиомы).
   - is_target_candidate: BOOLEAN DEFAULT true (подходит ли слово для обучения, или это имя/мусор).
   - created_at: TIMESTAMPTZ.
   - Indexes: (episode_id, lemma_id), (line_id).

---

### 2.4 User Profile & Progress Entities
7. **users** (Пользователи системы)
   - id: UUID / BIGSERIAL, Primary Key.
   - email: VARCHAR(255), Unique, Not Null.
   - password_hash: VARCHAR(255), Not Null.
   - base_cefr_level: ENUM ('A1', 'A2', 'B1', 'B2', 'C1', 'C2') DEFAULT 'B1'.
   - created_at, updated_at: TIMESTAMPTZ.

8. **user_vocabulary** (Персональный прогресс знания слов)
   - id: UUID / BIGSERIAL, Primary Key.
   - user_id: FK -> users(id) ON DELETE CASCADE.
   - lemma_id: FK -> global_lemmas(id) ON DELETE CASCADE.
   - status: ENUM ('known', 'learning', 'ignored') DEFAULT 'learning'.
   - repetitions_count: INTEGER DEFAULT 0.
   - last_reviewed_at: TIMESTAMPTZ.
   - next_review_at: TIMESTAMPTZ (для интеграции с SRS / интервальным повторением).
   - created_at, updated_at: TIMESTAMPTZ.
   - Constraint: UNIQUE (user_id, lemma_id).
   - Indexes: (user_id, status).

9. **user_episode_exports** (Логи выгрузок в Anki/Quizlet)
   - id: UUID / BIGSERIAL, Primary Key.
   - user_id: FK -> users(id) ON DELETE CASCADE.
   - episode_id: FK -> episodes(id) ON DELETE CASCADE.
   - target_format: ENUM ('anki_apkg', 'quizlet_tsv', 'csv').
   - words_count: INTEGER Not Null.
   - exported_at: TIMESTAMPTZ DEFAULT NOW().