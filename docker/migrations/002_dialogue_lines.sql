-- 002_dialogue_lines.sql — жёсткие ограничения dialogue_lines (step 2, T5.1).
--
-- Идемпотентен: повторный запуск — no-op. Если применять, то к уже поднятой БД:
--   docker exec -i -e PGPASSWORD=postgres_password postgres_container_eng \
--     psql -U postgres_user -d postgres_db -v ON_ERROR_STOP=1 \
--     < docker/migrations/002_dialogue_lines.sql
--
-- init.sql остаётся единственным источником начальной схемы (docker/init.sql:67);
-- этот файл только добавляет то, чего в ней сознательно не было.
--
-- СТАТУС (2026-09-18, T6.4): файл проверен на одноразовой КОПИИ схемы (двойной прогон —
-- идемпотентен; дубли (subtitle_id, line_index) и end<start отклоняются; прогон парсера
-- поверх копии проходит). На копии с «грязными» данными оба DO падают на in-place
-- валидации и НЕ оставляют половину констрейнов применённой. К ЖИВОЙ БД миграция по
-- решению пользователя НЕ применялась: идемпотентность импорта обеспечивает приложение
-- (DELETE перед вставкой, D3), а констрейны — страховка на случай конкурентных прогонов.
-- Применять вручную, только если такие прогоны появились (команда выше).

-- 1. Одна реплика на позицию внутри сабтитров — страховка от дублей при гонке
--    двух параллельных прогонов парсера (основной механизм — DELETE перед вставкой, D3).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'dialogue_lines'::regclass
          AND conname = 'uq_dialogue_lines_subtitle_index'
    ) THEN
        ALTER TABLE dialogue_lines
            ADD CONSTRAINT uq_dialogue_lines_subtitle_index
            UNIQUE (subtitle_id, line_index);
    END IF;
END
$$;

-- 2. Таймкод не может «идти назад»: некорректные интервалы чинит приложение (T2.3),
--    сюда они попадать не должны.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'dialogue_lines'::regclass
          AND conname = 'chk_dialogue_lines_time_order'
    ) THEN
        ALTER TABLE dialogue_lines
            ADD CONSTRAINT chk_dialogue_lines_time_order
            CHECK (end_time_ms > start_time_ms);
    END IF;
END
$$;
