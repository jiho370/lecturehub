-- Calendar: exams, quizzes, assignments, closures and personal events (user request 2026-09-29).
-- Sources: 'user' (typed in the app), 'etl' (assignment deadlines read from eTL, imported read-only),
-- 'transcript' (a date spoken in class, found by rule; always a candidate until the user confirms, D-052).
-- Rows are never deleted: cancel with status='cancelled'. Every change is also an Event.
-- date is YYYY-MM-DD; date_precision 'approx' keeps the spoken wording in date_text ("10월 중순").
-- source_ref de-duplicates imports: the eTL item id, or the ExamCue / transcript segment key.
CREATE TABLE CalendarEvent (
    id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT REFERENCES Course(id),
    kind TEXT NOT NULL CHECK (kind IN ('exam', 'quiz', 'assignment', 'closure', 'personal', 'other')),
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
