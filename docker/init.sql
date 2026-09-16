-- English Learning Subtitle Vocabulary Engine: Database Initialization
-- PostgreSQL 15+

-- ─── Enum Types ───

CREATE TYPE title_type_enum AS ENUM ('movie', 'series');
CREATE TYPE subtitle_status_enum AS ENUM ('pending', 'processing', 'parsed', 'failed');
CREATE TYPE cefr_level_enum AS ENUM ('A1', 'A2', 'B1', 'B2', 'C1', 'C2', 'unknown');
CREATE TYPE user_cefr_level_enum AS ENUM ('A1', 'A2', 'B1', 'B2', 'C1', 'C2');
CREATE TYPE user_vocab_status_enum AS ENUM ('known', 'learning', 'ignored');
CREATE TYPE export_format_enum AS ENUM ('anki_apkg', 'quizlet_tsv', 'csv');

-- ─── 1. titles ───

CREATE TABLE titles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_imdb_id VARCHAR(16) UNIQUE,
    external_tmdb_id INTEGER UNIQUE,
    type title_type_enum NOT NULL,
    name VARCHAR(255) NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    release_year SMALLINT,
    poster_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── 2. episodes ───

CREATE TABLE episodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title_id UUID NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    season_number SMALLINT NOT NULL,
    episode_number SMALLINT NOT NULL,
    name VARCHAR(255),
    duration_seconds INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_episodes_title_season_episode UNIQUE (title_id, season_number, episode_number)
);

-- ─── 3. subtitles ───

CREATE TABLE subtitles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    language_code VARCHAR(10) NOT NULL DEFAULT 'en',
    source VARCHAR(50),
    file_hash_sha256 CHAR(64),
    status subtitle_status_enum NOT NULL,
    raw_storage_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── 4. dialogue_lines ───

CREATE TABLE dialogue_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subtitle_id UUID NOT NULL REFERENCES subtitles(id) ON DELETE CASCADE,
    line_index INTEGER NOT NULL,
    start_time_ms INTEGER NOT NULL,
    end_time_ms INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_dialogue_lines_subtitle_time ON dialogue_lines (subtitle_id, start_time_ms);

-- ─── 5. global_lemmas ───

CREATE TABLE global_lemmas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lemma VARCHAR(100) NOT NULL UNIQUE,
    part_of_speech VARCHAR(20),
    cefr_level cefr_level_enum NOT NULL DEFAULT 'unknown',
    frequency_rank INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_global_lemmas_lemma ON global_lemmas (lemma);
CREATE INDEX idx_global_lemmas_cefr ON global_lemmas (cefr_level);
CREATE INDEX idx_global_lemmas_freq ON global_lemmas (frequency_rank);

-- ─── 6. episode_word_contexts ───

CREATE TABLE episode_word_contexts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    line_id UUID NOT NULL REFERENCES dialogue_lines(id) ON DELETE CASCADE,
    lemma_id UUID NOT NULL REFERENCES global_lemmas(id) ON DELETE CASCADE,
    inflected_form VARCHAR(100),
    context_translation_ru TEXT NOT NULL,
    target_translation_ru VARCHAR(255) NOT NULL,
    short_explanation_ru TEXT,
    is_target_candidate BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_episode_word_contexts_episode_lemma ON episode_word_contexts (episode_id, lemma_id);
CREATE INDEX idx_episode_word_contexts_line ON episode_word_contexts (line_id);

-- ─── 7. users ───

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    base_cefr_level user_cefr_level_enum NOT NULL DEFAULT 'B1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── 8. user_vocabulary ───

CREATE TABLE user_vocabulary (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lemma_id UUID NOT NULL REFERENCES global_lemmas(id) ON DELETE CASCADE,
    status user_vocab_status_enum NOT NULL DEFAULT 'learning',
    repetitions_count INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at TIMESTAMPTZ,
    next_review_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_vocabulary_user_lemma UNIQUE (user_id, lemma_id)
);

CREATE INDEX idx_user_vocabulary_user_status ON user_vocabulary (user_id, status);

-- ─── 9. user_episode_exports ───

CREATE TABLE user_episode_exports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    target_format export_format_enum NOT NULL,
    words_count INTEGER NOT NULL,
    exported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
