-- A transcript file may move into 전사본\<과목> once (user decision 2026-11-14: "애초에 저기에 저장해야지",
-- "일단 다 옮기고"). Text, audio link, engine and the bytes (sha256) stay immutable. The file path may
-- change only after an Evidence row (source 'user', kind 'user_file_move') naming this transcript and the
-- exact from -> to paths has been written; the lecture rule of 0007 stays as it was.
DROP TRIGGER Transcript_no_update;
CREATE TRIGGER Transcript_no_update BEFORE UPDATE ON Transcript
WHEN NEW.id IS NOT OLD.id
    OR (NEW.lecture_id IS NOT OLD.lecture_id AND NOT EXISTS (
        SELECT 1 FROM Evidence e WHERE e.subject_type = 'Transcript' AND e.subject_id = OLD.id
            AND e.kind = 'user_relink' AND e.source = 'user' AND json_valid(e.detail)
            AND json_extract(e.detail, '$.from') IS OLD.lecture_id
            AND json_extract(e.detail, '$.to') IS NEW.lecture_id))
    OR (NEW.source_file IS NOT OLD.source_file AND NOT EXISTS (
        SELECT 1 FROM Evidence e WHERE e.subject_type = 'Transcript' AND e.subject_id = OLD.id
            AND e.kind = 'user_file_move' AND e.source = 'user' AND json_valid(e.detail)
            AND json_extract(e.detail, '$.from') IS OLD.source_file
            AND json_extract(e.detail, '$.to') IS NEW.source_file))
    OR NEW.media_id IS NOT OLD.media_id OR NEW.engine IS NOT OLD.engine
    OR NEW.engine_version IS NOT OLD.engine_version OR NEW.chunk_plan_id IS NOT OLD.chunk_plan_id
    OR NEW.sha256 IS NOT OLD.sha256 BEGIN
    SELECT RAISE(ABORT, 'Transcript source identity is immutable');
END;
