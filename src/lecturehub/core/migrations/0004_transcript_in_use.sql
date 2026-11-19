-- D-049: per-lecture "transcript in use" pointer with replacement history.
-- Additive only. Each row is one decision; the latest row per lecture is the
-- current pointer, and previous_transcript_id records what it replaced.
-- decided_by: 'user' (chosen in the app) or 'policy' (priority rule:
-- user > canonical > mai > google > daglo/clova > whisper). Rows are never
-- edited or removed.

CREATE TABLE TranscriptInUse (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_id TEXT NOT NULL REFERENCES Lecture(id),
    transcript_id TEXT NOT NULL REFERENCES Transcript(id),
    previous_transcript_id TEXT REFERENCES Transcript(id),
    decided_by TEXT NOT NULL CHECK (decided_by IN ('user', 'policy')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at INTEGER NOT NULL
) STRICT;

CREATE INDEX idx_TranscriptInUse_lecture ON TranscriptInUse(lecture_id, created_at);

CREATE INDEX idx_TranscriptInUse_transcript ON TranscriptInUse(transcript_id);

CREATE INDEX idx_TranscriptInUse_previous ON TranscriptInUse(previous_transcript_id);

CREATE INDEX idx_TranscriptInUse_created ON TranscriptInUse(created_at);

CREATE TRIGGER TranscriptInUse_same_lecture BEFORE INSERT ON TranscriptInUse
WHEN (SELECT lecture_id FROM Transcript WHERE id = NEW.transcript_id) IS NOT NEW.lecture_id BEGIN
    SELECT RAISE(ABORT, 'Transcript in use must belong to the same lecture');
END;

CREATE TRIGGER TranscriptInUse_no_update BEFORE UPDATE ON TranscriptInUse BEGIN
    SELECT RAISE(ABORT, 'Transcript-in-use history is append-only');
END;

CREATE TRIGGER TranscriptInUse_no_delete BEFORE DELETE ON TranscriptInUse BEGIN
    SELECT RAISE(ABORT, 'Transcript-in-use history is append-only');
END;

CREATE TRIGGER TranscriptInUse_no_replace BEFORE INSERT ON TranscriptInUse
WHEN EXISTS (SELECT 1 FROM TranscriptInUse WHERE id = NEW.id) BEGIN
    SELECT RAISE(ABORT, 'Transcript-in-use history is append-only');
END;
