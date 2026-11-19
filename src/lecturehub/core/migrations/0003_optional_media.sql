-- lecturehub: rebuild-preserving-data
-- lecturehub: rebuild-table=Transcript
-- Approved data-preserving exception to additive-only changes. 0001/0002
-- remain unchanged. The runner backs up existing file databases first.
-- SQLite cannot relax NOT NULL in place. This replacement preserves every
-- existing Transcript ID, source value, unique key, foreign key and guard.
-- core.db.migrate wraps it atomically, preserves referencing trigger names
-- with legacy_alter_table, verifies all FKs, and restores connection settings.
-- no_media is derived from media_id; callers omit it on INSERT/UPDATE.

CREATE TABLE Transcript_v3 (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_id TEXT NOT NULL REFERENCES Lecture(id),
    media_id TEXT REFERENCES MediaAsset(id),
    engine TEXT NOT NULL,
    engine_version TEXT,
    role TEXT NOT NULL CHECK (role IN ('timeline', 'content', 'reference')),
    role_decided_by TEXT NOT NULL CHECK (role_decided_by IN ('user', 'policy', 'recommendation')),
    chunk_plan_id TEXT REFERENCES ChunkPlan(id),
    status TEXT NOT NULL,
    source_file TEXT,
    sha256 TEXT NOT NULL UNIQUE,
    no_media INTEGER GENERATED ALWAYS AS (media_id IS NULL) VIRTUAL,
    CHECK (media_id IS NOT NULL OR no_media = 1)
) STRICT, WITHOUT ROWID;

INSERT INTO Transcript_v3 ("id", "lecture_id", "media_id", "engine", "engine_version", "role", "role_decided_by", "chunk_plan_id", "status", "source_file", "sha256") SELECT "id", "lecture_id", "media_id", "engine", "engine_version", "role", "role_decided_by", "chunk_plan_id", "status", "source_file", "sha256" FROM Transcript;

DROP TABLE Transcript;
ALTER TABLE Transcript_v3 RENAME TO Transcript;

CREATE INDEX idx_Transcript_lecture ON Transcript(lecture_id);

CREATE INDEX idx_Transcript_media ON Transcript(media_id);

CREATE INDEX idx_Transcript_plan ON Transcript(chunk_plan_id);

CREATE TRIGGER "Transcript_generate_id" BEFORE INSERT ON "Transcript"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Transcript" ("id", "lecture_id", "media_id", "engine", "engine_version", "role", "role_decided_by", "chunk_plan_id", "status", "source_file", "sha256") VALUES (COALESCE(NEW.id, new_ulid()), NEW."lecture_id", NEW."media_id", NEW."engine", NEW."engine_version", NEW."role", NEW."role_decided_by", NEW."chunk_plan_id", NEW."status", NEW."source_file", NEW."sha256");
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER Transcript_no_delete BEFORE DELETE ON Transcript BEGIN
    SELECT RAISE(ABORT, 'Transcript is immutable');
END;

CREATE TRIGGER Transcript_no_replace BEFORE INSERT ON Transcript
WHEN EXISTS (SELECT 1 FROM Transcript WHERE id = NEW.id OR sha256 = NEW.sha256) BEGIN
    SELECT RAISE(ABORT, 'Transcript is immutable; duplicate id or sha256');
END;

CREATE TRIGGER Transcript_no_update BEFORE UPDATE ON Transcript
WHEN NEW.id IS NOT OLD.id OR NEW.lecture_id IS NOT OLD.lecture_id
    OR NEW.media_id IS NOT OLD.media_id OR NEW.engine IS NOT OLD.engine
    OR NEW.engine_version IS NOT OLD.engine_version OR NEW.chunk_plan_id IS NOT OLD.chunk_plan_id
    OR NEW.source_file IS NOT OLD.source_file OR NEW.sha256 IS NOT OLD.sha256 BEGIN
    SELECT RAISE(ABORT, 'Transcript source identity is immutable');
END;

CREATE TRIGGER "Transcript_stable_id" BEFORE UPDATE OF id ON "Transcript"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
