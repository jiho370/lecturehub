-- A transcript may follow its recording to another lecture, only when the user said so (2026-09-29:
-- "내가 허용하면 바꿀 수 있게"). The text, audio link, engine and file identity stay immutable.
-- The lecture may change only after an Evidence row (source 'user', kind 'user_relink') naming this
-- transcript and the exact from -> to lectures has been written; everything else raises as before.
DROP TRIGGER Transcript_no_update;
CREATE TRIGGER Transcript_no_update BEFORE UPDATE ON Transcript
WHEN NEW.id IS NOT OLD.id
    OR (NEW.lecture_id IS NOT OLD.lecture_id AND NOT EXISTS (
        SELECT 1 FROM Evidence e WHERE e.subject_type = 'Transcript' AND e.subject_id = OLD.id
            AND e.kind = 'user_relink' AND e.source = 'user' AND json_valid(e.detail)
            AND json_extract(e.detail, '$.from') IS OLD.lecture_id
            AND json_extract(e.detail, '$.to') IS NEW.lecture_id))
    OR NEW.media_id IS NOT OLD.media_id OR NEW.engine IS NOT OLD.engine
    OR NEW.engine_version IS NOT OLD.engine_version OR NEW.chunk_plan_id IS NOT OLD.chunk_plan_id
    OR NEW.source_file IS NOT OLD.source_file OR NEW.sha256 IS NOT OLD.sha256 BEGIN
    SELECT RAISE(ABORT, 'Transcript source identity is immutable');
END;
