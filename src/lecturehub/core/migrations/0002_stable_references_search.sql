-- Approved B-001 through B-004 and B-2 search. Additive upgrade of v0.
-- Old columns, keys and data remain available. SQL new_ulid() is registered
-- by core.db.connect/migrate. Callers may also supply IDs explicitly.
-- FTS5 shadow tables are SQLite-owned storage, not domain entities.

ALTER TABLE Bookmark ADD COLUMN lecture_id TEXT REFERENCES Lecture(id);
ALTER TABLE Bookmark ADD COLUMN media_id TEXT REFERENCES MediaAsset(id);
ALTER TABLE Bookmark ADD COLUMN offset_ms INTEGER CHECK (offset_ms >= 0);
UPDATE Bookmark SET offset_ms = timestamp WHERE timestamp >= 0;
CREATE INDEX idx_Bookmark_lecture ON Bookmark(lecture_id);
CREATE INDEX idx_Bookmark_media ON Bookmark(media_id);
CREATE INDEX idx_Bookmark_offset ON Bookmark(offset_ms);

ALTER TABLE AlignmentAnchor ADD COLUMN start_ms INTEGER CHECK (start_ms >= 0);
ALTER TABLE AlignmentAnchor ADD COLUMN end_ms INTEGER CHECK (end_ms >= start_ms);
ALTER TABLE AlignmentAnchor ADD COLUMN transcript_id TEXT REFERENCES Transcript(id);
ALTER TABLE AlignmentAnchor ADD COLUMN seq INTEGER;
ALTER TABLE AlignmentAnchor ADD COLUMN page INTEGER CHECK (page >= 1);
ALTER TABLE AlignmentAnchor ADD COLUMN artifact_id TEXT REFERENCES StudyArtifact(id);
ALTER TABLE AlignmentAnchor ADD COLUMN artifact_anchor TEXT;
UPDATE AlignmentAnchor SET start_ms = start_time_ms, end_ms = end_time_ms
    WHERE start_time_ms >= 0;
UPDATE AlignmentAnchor SET page = page_or_slide WHERE page_or_slide >= 1;
CREATE INDEX idx_AlignmentAnchor_new_start ON AlignmentAnchor(start_ms);
CREATE INDEX idx_AlignmentAnchor_new_end ON AlignmentAnchor(end_ms);
CREATE INDEX idx_AlignmentAnchor_transcript_seq ON AlignmentAnchor(transcript_id, seq);
CREATE INDEX idx_AlignmentAnchor_artifact ON AlignmentAnchor(artifact_id);

ALTER TABLE QuizItem ADD COLUMN external_blocks TEXT NOT NULL DEFAULT '[]'
    CHECK (json_valid(external_blocks) AND json_type(external_blocks) = 'array');

-- Only the new identity field changes during the atomic migration; raw
-- transcript text, time, speaker and source references are never rewritten.
DROP TRIGGER TranscriptSegment_no_update;
ALTER TABLE "schema_version" ADD COLUMN id TEXT;
UPDATE "schema_version" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_schema_version_id" ON "schema_version"(id);
ALTER TABLE "Schedule" ADD COLUMN id TEXT;
UPDATE "Schedule" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_Schedule_id" ON "Schedule"(id);
ALTER TABLE "ScheduleException" ADD COLUMN id TEXT;
UPDATE "ScheduleException" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_ScheduleException_id" ON "ScheduleException"(id);
ALTER TABLE "StopReport" ADD COLUMN id TEXT;
UPDATE "StopReport" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_StopReport_id" ON "StopReport"(id);
ALTER TABLE "RecordingAnomaly" ADD COLUMN id TEXT;
UPDATE "RecordingAnomaly" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_RecordingAnomaly_id" ON "RecordingAnomaly"(id);
ALTER TABLE "MoveLog" ADD COLUMN id TEXT;
UPDATE "MoveLog" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_MoveLog_id" ON "MoveLog"(id);
ALTER TABLE "TranscriptChunkRun" ADD COLUMN id TEXT;
UPDATE "TranscriptChunkRun" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_TranscriptChunkRun_id" ON "TranscriptChunkRun"(id);
ALTER TABLE "TranscriptSegment" ADD COLUMN id TEXT;
UPDATE "TranscriptSegment" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_TranscriptSegment_id" ON "TranscriptSegment"(id);
ALTER TABLE "TranscriptQA" ADD COLUMN id TEXT;
UPDATE "TranscriptQA" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_TranscriptQA_id" ON "TranscriptQA"(id);
ALTER TABLE "CanonicalSegment" ADD COLUMN id TEXT;
UPDATE "CanonicalSegment" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_CanonicalSegment_id" ON "CanonicalSegment"(id);
ALTER TABLE "MaterialPage" ADD COLUMN id TEXT;
UPDATE "MaterialPage" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_MaterialPage_id" ON "MaterialPage"(id);
ALTER TABLE "MaterialMatch" ADD COLUMN id TEXT;
UPDATE "MaterialMatch" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_MaterialMatch_id" ON "MaterialMatch"(id);
ALTER TABLE "Syllabus" ADD COLUMN id TEXT;
UPDATE "Syllabus" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_Syllabus_id" ON "Syllabus"(id);
ALTER TABLE "ProviderConsent" ADD COLUMN id TEXT;
UPDATE "ProviderConsent" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_ProviderConsent_id" ON "ProviderConsent"(id);
ALTER TABLE "QuotaLedger" ADD COLUMN id TEXT;
UPDATE "QuotaLedger" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_QuotaLedger_id" ON "QuotaLedger"(id);
ALTER TABLE "Bookmark" ADD COLUMN id TEXT;
UPDATE "Bookmark" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_Bookmark_id" ON "Bookmark"(id);
ALTER TABLE "AlignmentAnchor" ADD COLUMN id TEXT;
UPDATE "AlignmentAnchor" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_AlignmentAnchor_id" ON "AlignmentAnchor"(id);
ALTER TABLE "SlideViewEvent" ADD COLUMN id TEXT;
UPDATE "SlideViewEvent" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_SlideViewEvent_id" ON "SlideViewEvent"(id);
ALTER TABLE "QuizAttempt" ADD COLUMN id TEXT;
UPDATE "QuizAttempt" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_QuizAttempt_id" ON "QuizAttempt"(id);
ALTER TABLE "ExamOutcome" ADD COLUMN id TEXT;
UPDATE "ExamOutcome" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_ExamOutcome_id" ON "ExamOutcome"(id);
ALTER TABLE "EngineMetricAggregate" ADD COLUMN id TEXT;
UPDATE "EngineMetricAggregate" SET id = new_ulid();
CREATE UNIQUE INDEX "idx_EngineMetricAggregate_id" ON "EngineMetricAggregate"(id);

CREATE TRIGGER TranscriptSegment_no_update BEFORE UPDATE ON TranscriptSegment BEGIN
    SELECT RAISE(ABORT, 'TranscriptSegment is immutable');
END;
DROP TRIGGER TranscriptSegment_no_replace;
CREATE TRIGGER TranscriptSegment_no_replace BEFORE INSERT ON TranscriptSegment
WHEN EXISTS (SELECT 1 FROM TranscriptSegment
    WHERE (transcript_id = NEW.transcript_id AND seq = NEW.seq) OR id = NEW.id) BEGIN
    SELECT RAISE(ABORT, 'TranscriptSegment is immutable');
END;

-- Auto-generated identities use a BEFORE INSERT re-insertion, so immutable
-- rows never need an UPDATE after creation. Explicit IDs are recommended
-- when callers need RETURNING/lastrowid (SQLite ignores the outer insert).
CREATE TRIGGER "schema_version_generate_id" BEFORE INSERT ON "schema_version"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "schema_version" ("version", "id") VALUES (NEW."version", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "schema_version_stable_id" BEFORE UPDATE OF id ON "schema_version"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Course_generate_id" BEFORE INSERT ON "Course"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Course" ("id", "code", "folder_name", "abbr", "term") VALUES (COALESCE(NEW.id, new_ulid()), NEW."code", NEW."folder_name", NEW."abbr", NEW."term");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Course_stable_id" BEFORE UPDATE OF id ON "Course"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Schedule_generate_id" BEFORE INSERT ON "Schedule"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Schedule" ("course_id", "weekday", "start", "end", "id") VALUES (NEW."course_id", NEW."weekday", NEW."start", NEW."end", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Schedule_stable_id" BEFORE UPDATE OF id ON "Schedule"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "ScheduleException_generate_id" BEFORE INSERT ON "ScheduleException"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "ScheduleException" ("course_id", "date", "kind", "note", "id") VALUES (NEW."course_id", NEW."date", NEW."kind", NEW."note", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "ScheduleException_stable_id" BEFORE UPDATE OF id ON "ScheduleException"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Lecture_generate_id" BEFORE INSERT ON "Lecture"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Lecture" ("id", "course_id", "date", "session_index", "title", "lecture_type") VALUES (COALESCE(NEW.id, new_ulid()), NEW."course_id", NEW."date", NEW."session_index", NEW."title", NEW."lecture_type");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Lecture_stable_id" BEFORE UPDATE OF id ON "Lecture"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "MediaAsset_generate_id" BEFORE INSERT ON "MediaAsset"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "MediaAsset" ("id", "lecture_id", "path", "sha256", "duration_ms", "codec", "sample_rate", "channels", "bytes", "origin", "device_type", "is_master", "derived_from", "preset_id", "recorded_start", "partial", "loudness_profile", "decided_by", "confidence_class") VALUES (COALESCE(NEW.id, new_ulid()), NEW."lecture_id", NEW."path", NEW."sha256", NEW."duration_ms", NEW."codec", NEW."sample_rate", NEW."channels", NEW."bytes", NEW."origin", NEW."device_type", NEW."is_master", NEW."derived_from", NEW."preset_id", NEW."recorded_start", NEW."partial", NEW."loudness_profile", NEW."decided_by", NEW."confidence_class");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "MediaAsset_stable_id" BEFORE UPDATE OF id ON "MediaAsset"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "RecordingSession_generate_id" BEFORE INSERT ON "RecordingSession"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "RecordingSession" ("id", "lecture_id", "media_id", "mode", "device_name", "started_at", "ended_at", "heartbeat_path", "segments", "gaps") VALUES (COALESCE(NEW.id, new_ulid()), NEW."lecture_id", NEW."media_id", NEW."mode", NEW."device_name", NEW."started_at", NEW."ended_at", NEW."heartbeat_path", NEW."segments", NEW."gaps");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "RecordingSession_stable_id" BEFORE UPDATE OF id ON "RecordingSession"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "StopReport_generate_id" BEFORE INSERT ON "StopReport"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "StopReport" ("session_id", "cause", "certainty", "at_ms", "recovered", "id") VALUES (NEW."session_id", NEW."cause", NEW."certainty", NEW."at_ms", NEW."recovered", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "StopReport_stable_id" BEFORE UPDATE OF id ON "StopReport"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "RecordingAnomaly_generate_id" BEFORE INSERT ON "RecordingAnomaly"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "RecordingAnomaly" ("session_id", "kind", "from_ms", "to_ms", "certainty", "id") VALUES (NEW."session_id", NEW."kind", NEW."from_ms", NEW."to_ms", NEW."certainty", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "RecordingAnomaly_stable_id" BEFORE UPDATE OF id ON "RecordingAnomaly"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "IngestItem_generate_id" BEFORE INSERT ON "IngestItem"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "IngestItem" ("id", "source_path", "suggestion", "state") VALUES (COALESCE(NEW.id, new_ulid()), NEW."source_path", NEW."suggestion", NEW."state");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "IngestItem_stable_id" BEFORE UPDATE OF id ON "IngestItem"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "MoveLog_generate_id" BEFORE INSERT ON "MoveLog"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "MoveLog" ("from_path", "to_path", "at", "estimated_start", "estimated_end", "id") VALUES (NEW."from_path", NEW."to_path", NEW."at", NEW."estimated_start", NEW."estimated_end", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "MoveLog_stable_id" BEFORE UPDATE OF id ON "MoveLog"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "ChunkPlan_generate_id" BEFORE INSERT ON "ChunkPlan"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "ChunkPlan" ("id", "media_id", "preset_id", "chunks") VALUES (COALESCE(NEW.id, new_ulid()), NEW."media_id", NEW."preset_id", NEW."chunks");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "ChunkPlan_stable_id" BEFORE UPDATE OF id ON "ChunkPlan"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Transcript_generate_id" BEFORE INSERT ON "Transcript"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Transcript" ("id", "lecture_id", "media_id", "engine", "engine_version", "role", "role_decided_by", "chunk_plan_id", "status", "source_file", "sha256") VALUES (COALESCE(NEW.id, new_ulid()), NEW."lecture_id", NEW."media_id", NEW."engine", NEW."engine_version", NEW."role", NEW."role_decided_by", NEW."chunk_plan_id", NEW."status", NEW."source_file", NEW."sha256");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Transcript_stable_id" BEFORE UPDATE OF id ON "Transcript"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "TranscriptChunkRun_generate_id" BEFORE INSERT ON "TranscriptChunkRun"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "TranscriptChunkRun" ("transcript_id", "chunk_index", "attempt", "state", "error", "raw_output_path", "started_at", "ended_at", "id") VALUES (NEW."transcript_id", NEW."chunk_index", NEW."attempt", NEW."state", NEW."error", NEW."raw_output_path", NEW."started_at", NEW."ended_at", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "TranscriptChunkRun_stable_id" BEFORE UPDATE OF id ON "TranscriptChunkRun"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "TranscriptSegment_generate_id" BEFORE INSERT ON "TranscriptSegment"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "TranscriptSegment" ("transcript_id", "seq", "start_ms", "end_ms", "speaker", "text", "chunk_index", "id") VALUES (NEW."transcript_id", NEW."seq", NEW."start_ms", NEW."end_ms", NEW."speaker", NEW."text", NEW."chunk_index", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "TranscriptSegment_stable_id" BEFORE UPDATE OF id ON "TranscriptSegment"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "TranscriptQA_generate_id" BEFORE INSERT ON "TranscriptQA"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "TranscriptQA" ("transcript_id", "chunk_index", "kind", "detail", "severity", "id") VALUES (NEW."transcript_id", NEW."chunk_index", NEW."kind", NEW."detail", NEW."severity", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "TranscriptQA_stable_id" BEFORE UPDATE OF id ON "TranscriptQA"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "CanonicalTranscript_generate_id" BEFORE INSERT ON "CanonicalTranscript"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "CanonicalTranscript" ("id", "lecture_id", "version", "parent_version", "created_at", "created_by", "inputs", "material_ids", "stage_reached", "status") VALUES (COALESCE(NEW.id, new_ulid()), NEW."lecture_id", NEW."version", NEW."parent_version", NEW."created_at", NEW."created_by", NEW."inputs", NEW."material_ids", NEW."stage_reached", NEW."status");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "CanonicalTranscript_stable_id" BEFORE UPDATE OF id ON "CanonicalTranscript"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "CanonicalSegment_generate_id" BEFORE INSERT ON "CanonicalSegment"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "CanonicalSegment" ("canonical_id", "seq", "start_ms", "end_ms", "time_precision", "text", "confidence_class", "flags", "source_spans", "id") VALUES (NEW."canonical_id", NEW."seq", NEW."start_ms", NEW."end_ms", NEW."time_precision", NEW."text", NEW."confidence_class", NEW."flags", NEW."source_spans", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "CanonicalSegment_stable_id" BEFORE UPDATE OF id ON "CanonicalSegment"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Correction_generate_id" BEFORE INSERT ON "Correction"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Correction" ("id", "lecture_id", "anchor", "before", "after", "kind", "status", "decided_by", "created_at") VALUES (COALESCE(NEW.id, new_ulid()), NEW."lecture_id", NEW."anchor", NEW."before", NEW."after", NEW."kind", NEW."status", NEW."decided_by", NEW."created_at");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Correction_stable_id" BEFORE UPDATE OF id ON "Correction"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Material_generate_id" BEFORE INSERT ON "Material"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Material" ("id", "course_id", "path", "sha256", "kind", "title", "etl_file_id", "etl_uploaded_at", "etl_module", "page_count") VALUES (COALESCE(NEW.id, new_ulid()), NEW."course_id", NEW."path", NEW."sha256", NEW."kind", NEW."title", NEW."etl_file_id", NEW."etl_uploaded_at", NEW."etl_module", NEW."page_count");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Material_stable_id" BEFORE UPDATE OF id ON "Material"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "MaterialPage_generate_id" BEFORE INSERT ON "MaterialPage"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "MaterialPage" ("material_id", "page_no", "text", "image_path", "terms", "id") VALUES (NEW."material_id", NEW."page_no", NEW."text", NEW."image_path", NEW."terms", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "MaterialPage_stable_id" BEFORE UPDATE OF id ON "MaterialPage"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "MaterialMatch_generate_id" BEFORE INSERT ON "MaterialMatch"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "MaterialMatch" ("lecture_id", "material_id", "page_from", "page_to", "confidence_class", "decided_by", "id") VALUES (NEW."lecture_id", NEW."material_id", NEW."page_from", NEW."page_to", NEW."confidence_class", NEW."decided_by", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "MaterialMatch_stable_id" BEFORE UPDATE OF id ON "MaterialMatch"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Syllabus_generate_id" BEFORE INSERT ON "Syllabus"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Syllabus" ("course_id", "week", "topic", "planned_date", "id") VALUES (NEW."course_id", NEW."week", NEW."topic", NEW."planned_date", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Syllabus_stable_id" BEFORE UPDATE OF id ON "Syllabus"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "StudyArtifact_generate_id" BEFORE INSERT ON "StudyArtifact"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "StudyArtifact" ("id", "lecture_ids", "path", "kind", "version", "parent_version", "build_script_path", "mode", "inputs", "created_by", "qa_status") VALUES (COALESCE(NEW.id, new_ulid()), NEW."lecture_ids", NEW."path", NEW."kind", NEW."version", NEW."parent_version", NEW."build_script_path", NEW."mode", NEW."inputs", NEW."created_by", NEW."qa_status");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "StudyArtifact_stable_id" BEFORE UPDATE OF id ON "StudyArtifact"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "ProcessingJob_generate_id" BEFORE INSERT ON "ProcessingJob"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "ProcessingJob" ("id", "kind", "executor", "lecture_id", "state", "attempts", "manifest_path", "log_path", "next_eligible_at") VALUES (COALESCE(NEW.id, new_ulid()), NEW."kind", NEW."executor", NEW."lecture_id", NEW."state", NEW."attempts", NEW."manifest_path", NEW."log_path", NEW."next_eligible_at");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "ProcessingJob_stable_id" BEFORE UPDATE OF id ON "ProcessingJob"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "ProviderConsent_generate_id" BEFORE INSERT ON "ProviderConsent"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "ProviderConsent" ("provider", "course_id", "granted_at", "note", "id") VALUES (NEW."provider", NEW."course_id", NEW."granted_at", NEW."note", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "ProviderConsent_stable_id" BEFORE UPDATE OF id ON "ProviderConsent"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "QuotaLedger_generate_id" BEFORE INSERT ON "QuotaLedger"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "QuotaLedger" ("provider", "date", "requests", "audio_seconds", "tokens", "id") VALUES (NEW."provider", NEW."date", NEW."requests", NEW."audio_seconds", NEW."tokens", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "QuotaLedger_stable_id" BEFORE UPDATE OF id ON "QuotaLedger"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Bookmark_generate_id" BEFORE INSERT ON "Bookmark"
WHEN NEW.id IS NULL OR (NEW.timestamp IS NULL AND NEW.offset_ms IS NOT NULL) OR (NEW.offset_ms IS NULL AND NEW.timestamp IS NOT NULL) BEGIN
    INSERT INTO "Bookmark" ("timestamp", "optional_label", "lecture_id", "media_id", "offset_ms", "id") VALUES (COALESCE(NEW.timestamp, NEW.offset_ms), NEW."optional_label", NEW."lecture_id", NEW."media_id", COALESCE(NEW.offset_ms, NEW.timestamp), COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Bookmark_stable_id" BEFORE UPDATE OF id ON "Bookmark"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "AlignmentAnchor_generate_id" BEFORE INSERT ON "AlignmentAnchor"
WHEN NEW.id IS NULL OR NEW.confidence IS NULL OR (NEW.start_time_ms IS NULL AND NEW.start_ms IS NOT NULL) OR (NEW.end_time_ms IS NULL AND NEW.end_ms IS NOT NULL) OR (NEW.start_ms IS NULL AND NEW.start_time_ms IS NOT NULL) OR (NEW.end_ms IS NULL AND NEW.end_time_ms IS NOT NULL) OR (NEW.page IS NULL AND NEW.page_or_slide IS NOT NULL) OR (NEW.page_or_slide IS NULL AND NEW.page IS NOT NULL) BEGIN
    INSERT INTO "AlignmentAnchor" ("lecture_id", "start_time_ms", "end_time_ms", "transcript_segment_id", "material_id", "page_or_slide", "study_artifact_anchor", "confidence", "provenance", "start_ms", "end_ms", "transcript_id", "seq", "page", "artifact_id", "artifact_anchor", "id") VALUES (NEW."lecture_id", COALESCE(NEW.start_time_ms, NEW.start_ms), COALESCE(NEW.end_time_ms, NEW.end_ms), NEW."transcript_segment_id", NEW."material_id", COALESCE(NEW.page_or_slide, NEW.page), NEW."study_artifact_anchor", COALESCE(NEW.confidence, 'none'), NEW."provenance", COALESCE(NEW.start_ms, NEW.start_time_ms), COALESCE(NEW.end_ms, NEW.end_time_ms), NEW."transcript_id", NEW."seq", COALESCE(NEW.page, NEW.page_or_slide), NEW."artifact_id", NEW."artifact_anchor", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "AlignmentAnchor_stable_id" BEFORE UPDATE OF id ON "AlignmentAnchor"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "SlideViewEvent_generate_id" BEFORE INSERT ON "SlideViewEvent"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "SlideViewEvent" ("session_id", "material_id", "page_no", "at_ms", "id") VALUES (NEW."session_id", NEW."material_id", NEW."page_no", NEW."at_ms", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "SlideViewEvent_stable_id" BEFORE UPDATE OF id ON "SlideViewEvent"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "ExamCue_generate_id" BEFORE INSERT ON "ExamCue"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "ExamCue" ("id", "lecture_id", "at_ms", "kind", "quote", "context_before", "context_after", "material_page", "engines_agree", "source", "confidence_class", "decided_by") VALUES (COALESCE(NEW.id, new_ulid()), NEW."lecture_id", NEW."at_ms", NEW."kind", NEW."quote", NEW."context_before", NEW."context_after", NEW."material_page", NEW."engines_agree", NEW."source", NEW."confidence_class", NEW."decided_by");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "ExamCue_stable_id" BEFORE UPDATE OF id ON "ExamCue"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "EmphasisItem_generate_id" BEFORE INSERT ON "EmphasisItem"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "EmphasisItem" ("id", "course_id", "term_or_topic", "lectures", "mention_count", "minutes_spent", "example_solved", "score_components") VALUES (COALESCE(NEW.id, new_ulid()), NEW."course_id", NEW."term_or_topic", NEW."lectures", NEW."mention_count", NEW."minutes_spent", NEW."example_solved", NEW."score_components");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "EmphasisItem_stable_id" BEFORE UPDATE OF id ON "EmphasisItem"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "QuizSet_generate_id" BEFORE INSERT ON "QuizSet"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "QuizSet" ("id", "course_id", "scope", "created_by", "created_at", "generator_version") VALUES (COALESCE(NEW.id, new_ulid()), NEW."course_id", NEW."scope", NEW."created_by", NEW."created_at", NEW."generator_version");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "QuizSet_stable_id" BEFORE UPDATE OF id ON "QuizSet"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "QuizItem_generate_id" BEFORE INSERT ON "QuizItem"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "QuizItem" ("id", "quiz_set_id", "type", "question", "choices", "answer", "explanation", "sources", "quoted_evidence", "difficulty", "flags", "status", "external_blocks") VALUES (COALESCE(NEW.id, new_ulid()), NEW."quiz_set_id", NEW."type", NEW."question", NEW."choices", NEW."answer", NEW."explanation", NEW."sources", NEW."quoted_evidence", NEW."difficulty", NEW."flags", NEW."status", NEW."external_blocks");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "QuizItem_stable_id" BEFORE UPDATE OF id ON "QuizItem"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "QuizAttempt_generate_id" BEFORE INSERT ON "QuizAttempt"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "QuizAttempt" ("quiz_item_id", "at", "answer_given", "correct", "review_state", "id") VALUES (NEW."quiz_item_id", NEW."at", NEW."answer_given", NEW."correct", NEW."review_state", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "QuizAttempt_stable_id" BEFORE UPDATE OF id ON "QuizAttempt"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "PastExam_generate_id" BEFORE INSERT ON "PastExam"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "PastExam" ("id", "course_id", "path", "year", "term", "exam_kind", "professor", "ocr_used") VALUES (COALESCE(NEW.id, new_ulid()), NEW."course_id", NEW."path", NEW."year", NEW."term", NEW."exam_kind", NEW."professor", NEW."ocr_used");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "PastExam_stable_id" BEFORE UPDATE OF id ON "PastExam"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "PastExamQuestion_generate_id" BEFORE INSERT ON "PastExamQuestion"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "PastExamQuestion" ("id", "past_exam_id", "number", "text", "points", "type", "topic_links") VALUES (COALESCE(NEW.id, new_ulid()), NEW."past_exam_id", NEW."number", NEW."text", NEW."points", NEW."type", NEW."topic_links");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "PastExamQuestion_stable_id" BEFORE UPDATE OF id ON "PastExamQuestion"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "TopicExamLikelihood_generate_id" BEFORE INSERT ON "TopicExamLikelihood"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "TopicExamLikelihood" ("id", "course_id", "topic", "exam", "prob_lo", "prob_hi", "level", "signals", "method_version", "n_past_exams", "confidence_class", "created_at") VALUES (COALESCE(NEW.id, new_ulid()), NEW."course_id", NEW."topic", NEW."exam", NEW."prob_lo", NEW."prob_hi", NEW."level", NEW."signals", NEW."method_version", NEW."n_past_exams", NEW."confidence_class", NEW."created_at");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "TopicExamLikelihood_stable_id" BEFORE UPDATE OF id ON "TopicExamLikelihood"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "ExamOutcome_generate_id" BEFORE INSERT ON "ExamOutcome"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "ExamOutcome" ("course_id", "exam", "topic", "appeared", "recorded_by", "at", "id") VALUES (NEW."course_id", NEW."exam", NEW."topic", NEW."appeared", NEW."recorded_by", NEW."at", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "ExamOutcome_stable_id" BEFORE UPDATE OF id ON "ExamOutcome"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "TopicLink_generate_id" BEFORE INSERT ON "TopicLink"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "TopicLink" ("id", "subject", "lecture_id", "material_id", "page", "at_ms", "confidence_class", "decided_by") VALUES (COALESCE(NEW.id, new_ulid()), NEW."subject", NEW."lecture_id", NEW."material_id", NEW."page", NEW."at_ms", NEW."confidence_class", NEW."decided_by");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "TopicLink_stable_id" BEFORE UPDATE OF id ON "TopicLink"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Evidence_generate_id" BEFORE INSERT ON "Evidence"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Evidence" ("id", "subject_type", "subject_id", "field", "kind", "detail", "weight", "source", "created_at") VALUES (COALESCE(NEW.id, new_ulid()), NEW."subject_type", NEW."subject_id", NEW."field", NEW."kind", NEW."detail", NEW."weight", NEW."source", NEW."created_at");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Evidence_stable_id" BEFORE UPDATE OF id ON "Evidence"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "EngineMetricObservation_generate_id" BEFORE INSERT ON "EngineMetricObservation"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "EngineMetricObservation" ("id", "metric_kind", "metric_name", "engine", "engine_b", "engine_version", "comparison_set_id", "device_type", "course_id", "lecture_type", "value", "sample_size", "method", "window_start", "created_at") VALUES (COALESCE(NEW.id, new_ulid()), NEW."metric_kind", NEW."metric_name", NEW."engine", NEW."engine_b", NEW."engine_version", NEW."comparison_set_id", NEW."device_type", NEW."course_id", NEW."lecture_type", NEW."value", NEW."sample_size", NEW."method", NEW."window_start", NEW."created_at");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "EngineMetricObservation_stable_id" BEFORE UPDATE OF id ON "EngineMetricObservation"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "EngineMetricAggregate_generate_id" BEFORE INSERT ON "EngineMetricAggregate"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "EngineMetricAggregate" ("engine", "metric_name", "scope", "scope_value", "value", "range_lo", "range_hi", "n", "method", "calculated_at", "id") VALUES (NEW."engine", NEW."metric_name", NEW."scope", NEW."scope_value", NEW."value", NEW."range_lo", NEW."range_hi", NEW."n", NEW."method", NEW."calculated_at", COALESCE(NEW.id, new_ulid()));
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "EngineMetricAggregate_stable_id" BEFORE UPDATE OF id ON "EngineMetricAggregate"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;
CREATE TRIGGER "Event_generate_id" BEFORE INSERT ON "Event"
WHEN NEW.id IS NULL BEGIN
    INSERT INTO "Event" ("id", "at", "type", "subject_type", "subject_id", "payload_json") VALUES (COALESCE(NEW.id, new_ulid()), NEW."at", NEW."type", NEW."subject_type", NEW."subject_id", NEW."payload_json");
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER "Event_stable_id" BEFORE UPDATE OF id ON "Event"
WHEN NEW.id IS NOT OLD.id BEGIN
    SELECT RAISE(ABORT, 'Stable row id cannot change');
END;

-- v2 composite references retain their existing parent keys. SQLite cannot
-- add a composite FK with ALTER COLUMN, so symmetric reference triggers
-- enforce it without rebuilding or dropping legacy tables.
CREATE TRIGGER AlignmentAnchor_validate_insert BEFORE INSERT ON AlignmentAnchor BEGIN
    SELECT CASE WHEN (NEW.transcript_id IS NULL) != (NEW.seq IS NULL)
        OR (NEW.transcript_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM TranscriptSegment AS segment JOIN Transcript AS transcript
                ON transcript.id = segment.transcript_id
            WHERE segment.transcript_id = NEW.transcript_id AND segment.seq = NEW.seq
                AND transcript.lecture_id = NEW.lecture_id
        )) THEN RAISE(ABORT, 'Invalid transcript segment reference') END;
    SELECT CASE WHEN NEW.artifact_id IS NOT NULL AND (
        NEW.artifact_anchor IS NULL OR NEW.artifact_anchor NOT GLOB '#sec[0-9]*'
        OR substr(NEW.artifact_anchor, 5) GLOB '*[^0-9]*'
    ) OR (NEW.artifact_id IS NULL AND NEW.artifact_anchor IS NOT NULL)
        THEN RAISE(ABORT, 'Artifact reference requires artifact_id and #secN') END;
    SELECT CASE WHEN COALESCE(NEW.page, NEW.page_or_slide) IS NOT NULL AND NEW.material_id IS NULL
        THEN RAISE(ABORT, 'Page reference requires material_id') END;
    SELECT CASE WHEN NEW.start_ms IS NOT NULL AND NEW.start_time_ms IS NOT NULL
        AND NEW.start_ms != NEW.start_time_ms THEN RAISE(ABORT, 'Conflicting alignment start times') END;
    SELECT CASE WHEN NEW.end_ms IS NOT NULL AND NEW.end_time_ms IS NOT NULL
        AND NEW.end_ms != NEW.end_time_ms THEN RAISE(ABORT, 'Conflicting alignment end times') END;
    SELECT CASE WHEN NEW.page IS NOT NULL AND NEW.page_or_slide IS NOT NULL
        AND NEW.page != NEW.page_or_slide THEN RAISE(ABORT, 'Conflicting alignment pages') END;
END;
CREATE TRIGGER AlignmentAnchor_validate_update BEFORE UPDATE ON AlignmentAnchor BEGIN
    SELECT CASE WHEN NEW.start_ms IS NOT OLD.start_ms AND NEW.start_time_ms IS NOT OLD.start_time_ms
        AND NEW.start_ms IS NOT NEW.start_time_ms THEN RAISE(ABORT, 'Conflicting alignment start times') END;
    SELECT CASE WHEN NEW.end_ms IS NOT OLD.end_ms AND NEW.end_time_ms IS NOT OLD.end_time_ms
        AND NEW.end_ms IS NOT NEW.end_time_ms THEN RAISE(ABORT, 'Conflicting alignment end times') END;
    SELECT CASE WHEN NEW.page IS NOT OLD.page AND NEW.page_or_slide IS NOT OLD.page_or_slide
        AND NEW.page IS NOT NEW.page_or_slide THEN RAISE(ABORT, 'Conflicting alignment pages') END;
    SELECT CASE WHEN (NEW.transcript_id IS NULL) != (NEW.seq IS NULL)
        OR (NEW.transcript_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM TranscriptSegment AS segment JOIN Transcript AS transcript
                ON transcript.id = segment.transcript_id
            WHERE segment.transcript_id = NEW.transcript_id AND segment.seq = NEW.seq
                AND transcript.lecture_id = NEW.lecture_id
        )) THEN RAISE(ABORT, 'Invalid transcript segment reference') END;
    SELECT CASE WHEN NEW.artifact_id IS NOT NULL AND (
        NEW.artifact_anchor IS NULL OR NEW.artifact_anchor NOT GLOB '#sec[0-9]*'
        OR substr(NEW.artifact_anchor, 5) GLOB '*[^0-9]*'
    ) OR (NEW.artifact_id IS NULL AND NEW.artifact_anchor IS NOT NULL)
        THEN RAISE(ABORT, 'Artifact reference requires artifact_id and #secN') END;
    SELECT CASE WHEN COALESCE(NEW.page, NEW.page_or_slide) IS NOT NULL AND NEW.material_id IS NULL
        THEN RAISE(ABORT, 'Page reference requires material_id') END;
END;
CREATE TRIGGER Bookmark_validate_insert BEFORE INSERT ON Bookmark BEGIN
    SELECT CASE WHEN (NEW.lecture_id IS NULL) != (NEW.media_id IS NULL)
        THEN RAISE(ABORT, 'Bookmark requires both lecture_id and media_id') END;
    SELECT CASE WHEN NEW.timestamp IS NOT NULL AND NEW.offset_ms IS NOT NULL
        AND NEW.timestamp != NEW.offset_ms THEN RAISE(ABORT, 'Conflicting bookmark offsets') END;
    SELECT CASE WHEN NEW.media_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM MediaAsset WHERE id = NEW.media_id AND lecture_id IS NOT NULL AND lecture_id != NEW.lecture_id
    ) THEN RAISE(ABORT, 'Bookmark media belongs to a different lecture') END;
END;
CREATE TRIGGER Bookmark_validate_update BEFORE UPDATE ON Bookmark BEGIN
    SELECT CASE WHEN NEW.timestamp IS NOT OLD.timestamp AND NEW.offset_ms IS NOT OLD.offset_ms
        AND NEW.timestamp IS NOT NEW.offset_ms THEN RAISE(ABORT, 'Conflicting bookmark offsets') END;
    SELECT CASE WHEN (NEW.lecture_id IS NULL) != (NEW.media_id IS NULL)
        THEN RAISE(ABORT, 'Bookmark requires both lecture_id and media_id') END;
    SELECT CASE WHEN NEW.media_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM MediaAsset WHERE id = NEW.media_id AND lecture_id IS NOT NULL AND lecture_id != NEW.lecture_id
    ) THEN RAISE(ABORT, 'Bookmark media belongs to a different lecture') END;
END;
CREATE TRIGGER Bookmark_sync_offset AFTER UPDATE OF timestamp, offset_ms ON Bookmark
WHEN NEW.timestamp IS NOT NEW.offset_ms BEGIN
    UPDATE Bookmark SET
        timestamp = CASE WHEN NEW.offset_ms IS NOT OLD.offset_ms THEN NEW.offset_ms ELSE NEW.timestamp END,
        offset_ms = CASE WHEN NEW.offset_ms IS NOT OLD.offset_ms THEN NEW.offset_ms ELSE NEW.timestamp END
    WHERE id = NEW.id;
END;
CREATE TRIGGER MediaAsset_bookmark_lecture BEFORE UPDATE OF lecture_id ON MediaAsset
WHEN NEW.lecture_id IS NOT OLD.lecture_id AND NEW.lecture_id IS NOT NULL
    AND EXISTS (SELECT 1 FROM Bookmark WHERE media_id = OLD.id AND lecture_id != NEW.lecture_id) BEGIN
    SELECT RAISE(ABORT, 'Media lecture conflicts with an existing bookmark');
END;
CREATE TRIGGER AlignmentAnchor_sync_fields AFTER UPDATE OF start_time_ms, end_time_ms, start_ms, end_ms,
    page, page_or_slide ON AlignmentAnchor
WHEN NEW.start_ms IS NOT NEW.start_time_ms OR NEW.end_ms IS NOT NEW.end_time_ms
    OR NEW.page IS NOT NEW.page_or_slide BEGIN
    UPDATE AlignmentAnchor SET
        start_ms = CASE WHEN NEW.start_ms IS NOT OLD.start_ms THEN NEW.start_ms ELSE NEW.start_time_ms END,
        start_time_ms = CASE WHEN NEW.start_ms IS NOT OLD.start_ms THEN NEW.start_ms ELSE NEW.start_time_ms END,
        end_ms = CASE WHEN NEW.end_ms IS NOT OLD.end_ms THEN NEW.end_ms ELSE NEW.end_time_ms END,
        end_time_ms = CASE WHEN NEW.end_ms IS NOT OLD.end_ms THEN NEW.end_ms ELSE NEW.end_time_ms END,
        page = CASE WHEN NEW.page IS NOT OLD.page THEN NEW.page ELSE NEW.page_or_slide END,
        page_or_slide = CASE WHEN NEW.page IS NOT OLD.page THEN NEW.page ELSE NEW.page_or_slide END
    WHERE id = NEW.id;
END;
CREATE TRIGGER TranscriptChunkRun_time_insert BEFORE INSERT ON TranscriptChunkRun
WHEN NEW.ended_at IS NOT NULL AND NEW.started_at IS NULL BEGIN
    SELECT RAISE(ABORT, 'A finished chunk run requires a real start time');
END;
CREATE TRIGGER TranscriptChunkRun_time_update BEFORE UPDATE ON TranscriptChunkRun
WHEN NEW.ended_at IS NOT NULL AND NEW.started_at IS NULL BEGIN
    SELECT RAISE(ABORT, 'A finished chunk run requires a real start time');
END;
CREATE TRIGGER QuizItem_external_blocks_insert BEFORE INSERT ON QuizItem BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.external_blocks) WHERE type != 'object'
            OR COALESCE(json_type(value, '$.text'), '') != 'text'
            OR COALESCE(json_extract(value, '$.confidence'), '') NOT IN ('high', 'mid', 'low')
            OR COALESCE(json_extract(value, '$.source_kind'), '') NOT IN ('general', 'textbook', 'web')
            OR (json_type(value, '$.source_ref') IS NOT NULL AND json_type(value, '$.source_ref') NOT IN ('text', 'null'))
    ) THEN RAISE(ABORT, 'Invalid external knowledge block') END;
END;
CREATE TRIGGER QuizItem_external_blocks_update BEFORE UPDATE OF external_blocks ON QuizItem BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.external_blocks) WHERE type != 'object'
            OR COALESCE(json_type(value, '$.text'), '') != 'text'
            OR COALESCE(json_extract(value, '$.confidence'), '') NOT IN ('high', 'mid', 'low')
            OR COALESCE(json_extract(value, '$.source_kind'), '') NOT IN ('general', 'textbook', 'web')
            OR (json_type(value, '$.source_ref') IS NOT NULL AND json_type(value, '$.source_ref') NOT IN ('text', 'null'))
    ) THEN RAISE(ABORT, 'Invalid external knowledge block') END;
END;

CREATE VIRTUAL TABLE SearchIndex USING fts5(
    id UNINDEXED, source_kind UNINDEXED, source_id UNINDEXED, position UNINDEXED, text,
    tokenize='trigram'
);
INSERT INTO SearchIndex(id, source_kind, source_id, position, text)
    SELECT id, 'transcript', transcript_id, seq, text FROM TranscriptSegment;
INSERT INTO SearchIndex(id, source_kind, source_id, position, text)
    SELECT id, 'material', material_id, page_no, text FROM MaterialPage;
CREATE TRIGGER TranscriptSegment_search_insert AFTER INSERT ON TranscriptSegment BEGIN
    INSERT INTO SearchIndex(id, source_kind, source_id, position, text)
        VALUES (NEW.id, 'transcript', NEW.transcript_id, NEW.seq, NEW.text);
END;
CREATE TRIGGER MaterialPage_search_insert AFTER INSERT ON MaterialPage BEGIN
    DELETE FROM SearchIndex WHERE source_kind = 'material'
        AND (id = NEW.id OR (source_id = NEW.material_id AND position = NEW.page_no));
    INSERT INTO SearchIndex(id, source_kind, source_id, position, text)
        VALUES (NEW.id, 'material', NEW.material_id, NEW.page_no, NEW.text);
END;
CREATE TRIGGER MaterialPage_search_update AFTER UPDATE ON MaterialPage BEGIN
    DELETE FROM SearchIndex WHERE source_kind = 'material'
        AND (id = OLD.id OR id = NEW.id
            OR (source_id = OLD.material_id AND position = OLD.page_no)
            OR (source_id = NEW.material_id AND position = NEW.page_no));
    INSERT INTO SearchIndex(id, source_kind, source_id, position, text)
        VALUES (NEW.id, 'material', NEW.material_id, NEW.page_no, NEW.text);
END;
CREATE TRIGGER MaterialPage_search_delete AFTER DELETE ON MaterialPage BEGIN
    DELETE FROM SearchIndex WHERE source_kind = 'material'
        AND (id = OLD.id OR (source_id = OLD.material_id AND position = OLD.page_no));
END;
