-- lecturehub: rebuild-preserving-data
-- lecturehub: rebuild-table=CalendarEvent
-- Calendar kinds 'presentation' (발표) and 'report' (보고서) (user request 2026-11-24: "기타 말고 발표랑 보고서 칸도").
-- Data-preserving exception to additive-only changes, like 0003: SQLite cannot widen a CHECK in place, so the
-- table is rebuilt with the same columns, rows, indexes and triggers; only the kind list grows. The runner
-- backs up the file database first and verifies every row, index and trigger before commit.

CREATE TABLE CalendarEvent_v9 (
    id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT REFERENCES Course(id),
    kind TEXT NOT NULL CHECK (kind IN ('exam', 'quiz', 'assignment', 'presentation', 'report', 'closure', 'personal', 'other')),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    date TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    date_precision TEXT NOT NULL DEFAULT 'day' CHECK (date_precision IN ('day', 'approx')),
    date_text TEXT,
    start_ms INTEGER CHECK (start_ms IS NULL OR start_ms BETWEEN 0 AND 86400000),
    end_ms INTEGER CHECK (end_ms IS NULL OR (start_ms IS NOT NULL AND end_ms >= start_ms)),
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK (source IN ('user', 'etl', 'transcript')),
    source_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'confirmed', 'cancelled')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;

INSERT INTO CalendarEvent_v9 (id, course_id, kind, title, date, date_precision, date_text, start_ms, end_ms, note, source, source_ref, status, created_at, updated_at)
SELECT id, course_id, kind, title, date, date_precision, date_text, start_ms, end_ms, note, source, source_ref, status, created_at, updated_at FROM CalendarEvent;

DROP TABLE CalendarEvent;
ALTER TABLE CalendarEvent_v9 RENAME TO CalendarEvent;

CREATE INDEX idx_CalendarEvent_date ON CalendarEvent(date);

CREATE INDEX idx_CalendarEvent_course ON CalendarEvent(course_id);

CREATE INDEX idx_CalendarEvent_start ON CalendarEvent(start_ms);

CREATE INDEX idx_CalendarEvent_end ON CalendarEvent(end_ms);

CREATE INDEX idx_CalendarEvent_created ON CalendarEvent(created_at);

CREATE UNIQUE INDEX idx_CalendarEvent_source_ref ON CalendarEvent(source, source_ref) WHERE source_ref IS NOT NULL;

CREATE TRIGGER CalendarEvent_no_delete BEFORE DELETE ON CalendarEvent
BEGIN
    SELECT RAISE(ABORT, 'CalendarEvent rows are never deleted; set status to cancelled');
END;

CREATE TRIGGER CalendarEvent_transcript_needs_user BEFORE INSERT ON CalendarEvent
WHEN NEW.source = 'transcript' AND NEW.status = 'confirmed'
BEGIN
    SELECT RAISE(ABORT, 'a date found in a transcript starts as a candidate (D-052)');
END;
