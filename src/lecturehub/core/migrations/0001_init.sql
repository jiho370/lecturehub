-- Schema v0, Lecture_Hub_Plan.md sections 6 through 6.5.
-- IDs are TEXT ULIDs supplied by the caller. Calendar dates stay ISO text;
-- clock times, offsets and instants are integer milliseconds.
-- JSON lists/objects retain their documented names, without extra columns.

CREATE TABLE Course (
    id TEXT PRIMARY KEY NOT NULL,
    code TEXT NOT NULL,
    folder_name TEXT NOT NULL,
    abbr TEXT NOT NULL,
    term TEXT NOT NULL
) STRICT;

CREATE TABLE Schedule (
    course_id TEXT NOT NULL REFERENCES Course(id),
    weekday INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL CHECK (end >= start),
    PRIMARY KEY (course_id, weekday, start)
) STRICT;

CREATE TABLE ScheduleException (
    course_id TEXT REFERENCES Course(id),
    date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('휴강', '보강', '대체', '시간변경')),
    note TEXT NOT NULL
) STRICT;

CREATE TABLE Lecture (
    id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT NOT NULL REFERENCES Course(id),
    date TEXT NOT NULL,
    session_index INTEGER NOT NULL,
    title TEXT,
    lecture_type TEXT CHECK (lecture_type IN ('교수단독', '실습·대화', '외국어', '기타'))
) STRICT;

CREATE TABLE MediaAsset (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_id TEXT REFERENCES Lecture(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    codec TEXT NOT NULL,
    sample_rate INTEGER NOT NULL CHECK (sample_rate > 0),
    channels INTEGER NOT NULL CHECK (channels > 0),
    bytes INTEGER NOT NULL CHECK (bytes >= 0),
    origin TEXT NOT NULL CHECK (origin IN (
        'app_mic', 'app_loopback', 'win_recorder', 'daglo', 'kakao_phone', 'clova', 'other'
    )),
    device_type TEXT NOT NULL CHECK (device_type IN ('laptop', 'phone', 'external_mic')),
    is_master INTEGER NOT NULL CHECK (is_master IN (0, 1)),
    derived_from TEXT REFERENCES MediaAsset(id),
    preset_id TEXT,
    recorded_start INTEGER NOT NULL,
    partial INTEGER NOT NULL CHECK (partial IN (0, 1)),
    loudness_profile TEXT NOT NULL CHECK (json_valid(loudness_profile)),
    decided_by TEXT NOT NULL,
    confidence_class TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE RecordingSession (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_id TEXT REFERENCES Lecture(id),
    media_id TEXT REFERENCES MediaAsset(id),
    mode TEXT NOT NULL CHECK (mode IN ('mic', 'loopback')),
    device_name TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at INTEGER CHECK (ended_at >= started_at),
    heartbeat_path TEXT NOT NULL,
    segments TEXT NOT NULL CHECK (json_valid(segments) AND json_type(segments) = 'array'),
    gaps TEXT NOT NULL CHECK (json_valid(gaps) AND json_type(gaps) = 'array')
) STRICT;

CREATE TABLE StopReport (
    session_id TEXT PRIMARY KEY NOT NULL REFERENCES RecordingSession(id),
    cause TEXT NOT NULL CHECK (cause IN (
        'user_stop', 'scheduled_stop', 'device_lost', 'default_device_changed',
        'system_sleep', 'system_shutdown', 'battery_critical', 'disk_full', 'write_error',
        'app_crash', 'process_killed', 'unknown'
    )),
    certainty TEXT NOT NULL CHECK (certainty IN ('confirmed', 'estimated', 'unknown')),
    at_ms INTEGER NOT NULL,
    recovered INTEGER NOT NULL CHECK (recovered IN (0, 1))
) STRICT;

CREATE TABLE RecordingAnomaly (
    session_id TEXT NOT NULL REFERENCES RecordingSession(id),
    kind TEXT NOT NULL CHECK (kind IN (
        'source_silent', 'playback_stopped', 'network_drop_suspected', 'dropout', 'clipping'
    )),
    from_ms INTEGER NOT NULL,
    to_ms INTEGER CHECK (to_ms >= from_ms),
    certainty TEXT NOT NULL CHECK (certainty IN ('confirmed', 'estimated', 'unknown'))
) STRICT;

CREATE TABLE IngestItem (
    id TEXT PRIMARY KEY NOT NULL,
    source_path TEXT NOT NULL,
    suggestion TEXT NOT NULL CHECK (json_valid(suggestion) AND json_type(suggestion) = 'object'),
    state TEXT NOT NULL CHECK (state IN ('pending', 'confirmed', 'held', 'rejected'))
) STRICT;

CREATE TABLE MoveLog (
    from_path TEXT NOT NULL,
    to_path TEXT NOT NULL,
    at INTEGER NOT NULL,
    estimated_start INTEGER NOT NULL,
    estimated_end INTEGER NOT NULL
) STRICT;

CREATE TABLE ChunkPlan (
    id TEXT PRIMARY KEY NOT NULL,
    media_id TEXT NOT NULL REFERENCES MediaAsset(id),
    preset_id TEXT NOT NULL,
    chunks TEXT NOT NULL CHECK (json_valid(chunks) AND json_type(chunks) = 'array')
) STRICT;

CREATE TABLE Transcript (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_id TEXT NOT NULL REFERENCES Lecture(id),
    media_id TEXT NOT NULL REFERENCES MediaAsset(id),
    engine TEXT NOT NULL,
    engine_version TEXT,
    role TEXT NOT NULL CHECK (role IN ('timeline', 'content', 'reference')),
    role_decided_by TEXT NOT NULL CHECK (role_decided_by IN ('user', 'policy', 'recommendation')),
    chunk_plan_id TEXT REFERENCES ChunkPlan(id),
    status TEXT NOT NULL,
    source_file TEXT,
    sha256 TEXT NOT NULL UNIQUE
) STRICT, WITHOUT ROWID;

CREATE TABLE TranscriptChunkRun (
    transcript_id TEXT NOT NULL REFERENCES Transcript(id),
    chunk_index INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'running', 'done', 'rate_limited', 'retrying', 'qa_warning', 'failed'
    )),
    error TEXT,
    raw_output_path TEXT NOT NULL,
    started_at INTEGER,
    ended_at INTEGER CHECK (ended_at >= started_at),
    PRIMARY KEY (transcript_id, chunk_index, attempt)
) STRICT;

CREATE TABLE TranscriptSegment (
    transcript_id TEXT NOT NULL REFERENCES Transcript(id),
    seq INTEGER NOT NULL,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER CHECK (end_ms >= start_ms),
    speaker TEXT,
    text TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    PRIMARY KEY (transcript_id, seq)
) STRICT, WITHOUT ROWID;

CREATE TABLE TranscriptQA (
    transcript_id TEXT NOT NULL REFERENCES Transcript(id),
    chunk_index INTEGER,
    kind TEXT NOT NULL CHECK (kind IN (
        'coverage', 'monotonic', 'repetition', 'density_gap', 'empty_chunk', 'early_stop'
    )),
    detail TEXT NOT NULL,
    severity TEXT NOT NULL
) STRICT;

CREATE TABLE CanonicalTranscript (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_id TEXT NOT NULL REFERENCES Lecture(id),
    version INTEGER NOT NULL,
    parent_version INTEGER,
    created_at INTEGER NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by IN ('pipeline', 'user', 'ai')),
    inputs TEXT NOT NULL CHECK (json_valid(inputs) AND json_type(inputs) = 'array'),
    material_ids TEXT NOT NULL CHECK (json_valid(material_ids) AND json_type(material_ids) = 'array'),
    stage_reached TEXT NOT NULL CHECK (stage_reached IN ('C0', 'C1', 'C2', 'C3')),
    status TEXT NOT NULL,
    UNIQUE (lecture_id, version),
    FOREIGN KEY (lecture_id, parent_version) REFERENCES CanonicalTranscript(lecture_id, version)
) STRICT;

CREATE TABLE CanonicalSegment (
    canonical_id TEXT NOT NULL REFERENCES CanonicalTranscript(id),
    seq INTEGER NOT NULL,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms >= start_ms),
    time_precision TEXT NOT NULL CHECK (time_precision IN ('anchored', 'interpolated', 'single_source')),
    text TEXT NOT NULL,
    confidence_class TEXT NOT NULL CHECK (confidence_class IN ('high', 'medium', 'low', 'unresolved')),
    flags TEXT NOT NULL CHECK (json_valid(flags) AND json_type(flags) = 'array'),
    source_spans TEXT NOT NULL CHECK (json_valid(source_spans) AND json_type(source_spans) = 'array'),
    PRIMARY KEY (canonical_id, seq)
) STRICT;

CREATE TABLE Correction (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_id TEXT NOT NULL REFERENCES Lecture(id),
    anchor TEXT NOT NULL CHECK (json_valid(anchor) AND json_type(anchor) = 'object'),
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('term', 'number', 'name', 'other')),
    status TEXT NOT NULL CHECK (status IN ('active', 'orphaned', 'rejected')),
    decided_by TEXT NOT NULL CHECK (decided_by IN ('user', 'material_rule', 'engine_agreement', 'ai')),
    created_at INTEGER NOT NULL
) STRICT;

CREATE TABLE Material (
    id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT NOT NULL REFERENCES Course(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    etl_file_id TEXT,
    etl_uploaded_at INTEGER,
    etl_module TEXT,
    page_count INTEGER NOT NULL CHECK (page_count >= 0)
) STRICT;

CREATE TABLE MaterialPage (
    material_id TEXT NOT NULL REFERENCES Material(id),
    page_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    image_path TEXT,
    terms TEXT NOT NULL CHECK (json_valid(terms) AND json_type(terms) = 'array'),
    PRIMARY KEY (material_id, page_no)
) STRICT;

CREATE TABLE MaterialMatch (
    lecture_id TEXT NOT NULL REFERENCES Lecture(id),
    material_id TEXT NOT NULL REFERENCES Material(id),
    page_from INTEGER NOT NULL,
    page_to INTEGER NOT NULL CHECK (page_to >= page_from),
    confidence_class TEXT NOT NULL,
    decided_by TEXT NOT NULL
) STRICT;

CREATE TABLE Syllabus (
    course_id TEXT NOT NULL REFERENCES Course(id),
    week INTEGER NOT NULL,
    topic TEXT NOT NULL,
    planned_date TEXT
) STRICT;

CREATE TABLE StudyArtifact (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_ids TEXT NOT NULL CHECK (json_valid(lecture_ids) AND json_type(lecture_ids) = 'array'),
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    build_script_path TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('new', 'append_mai_check')),
    inputs TEXT NOT NULL CHECK (json_valid(inputs) AND json_type(inputs) = 'object'),
    created_by TEXT NOT NULL,
    qa_status TEXT NOT NULL
) STRICT;

CREATE TABLE ProcessingJob (
    id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL,
    executor TEXT NOT NULL CHECK (executor IN ('app', 'claude_cli', 'codex_cli', 'manual')),
    lecture_id TEXT REFERENCES Lecture(id),
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'running', 'waiting_for_quota', 'waiting_user', 'retrying',
        'done', 'qa_warning', 'failed', 'cancelled'
    )),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    manifest_path TEXT,
    log_path TEXT NOT NULL,
    next_eligible_at INTEGER
) STRICT;

CREATE TABLE ProviderConsent (
    provider TEXT NOT NULL,
    course_id TEXT REFERENCES Course(id),
    granted_at INTEGER NOT NULL,
    note TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX idx_ProviderConsent_course ON ProviderConsent(provider, course_id) WHERE course_id IS NOT NULL;
CREATE UNIQUE INDEX idx_ProviderConsent_global ON ProviderConsent(provider) WHERE course_id IS NULL;

CREATE TABLE QuotaLedger (
    provider TEXT NOT NULL,
    date TEXT NOT NULL,
    requests INTEGER NOT NULL CHECK (requests >= 0),
    audio_seconds INTEGER NOT NULL CHECK (audio_seconds >= 0),
    tokens INTEGER CHECK (tokens >= 0),
    PRIMARY KEY (provider, date)
) STRICT;

-- v2 section 9 specifies only these two fields. Session linkage awaits a
-- specification decision; do not invent an id or a recording reference.
CREATE TABLE Bookmark (
    timestamp INTEGER NOT NULL,
    optional_label TEXT
) STRICT;

-- v2 section 11 names transcript_segment_id, but v3 segments have the
-- composite key (transcript_id, seq). Keep the documented field unresolved.
CREATE TABLE AlignmentAnchor (
    lecture_id TEXT NOT NULL REFERENCES Lecture(id),
    start_time_ms INTEGER NOT NULL,
    end_time_ms INTEGER NOT NULL CHECK (end_time_ms >= start_time_ms),
    transcript_segment_id TEXT,
    material_id TEXT REFERENCES Material(id),
    page_or_slide INTEGER,
    study_artifact_anchor TEXT,
    confidence TEXT NOT NULL,
    provenance TEXT NOT NULL
) STRICT;

CREATE TABLE SlideViewEvent (
    session_id TEXT NOT NULL REFERENCES RecordingSession(id),
    material_id TEXT NOT NULL REFERENCES Material(id),
    page_no INTEGER NOT NULL CHECK (page_no >= 1),
    at_ms INTEGER NOT NULL
) STRICT;

CREATE TABLE ExamCue (
    id TEXT PRIMARY KEY NOT NULL,
    lecture_id TEXT NOT NULL REFERENCES Lecture(id),
    at_ms INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('exam_mention', 'exam_exclusion', 'emphasis', 'assignment', 'scope_notice')),
    quote TEXT NOT NULL,
    context_before TEXT NOT NULL,
    context_after TEXT NOT NULL,
    material_page TEXT,
    engines_agree INTEGER NOT NULL CHECK (engines_agree >= 0),
    source TEXT NOT NULL,
    confidence_class TEXT NOT NULL,
    decided_by TEXT NOT NULL CHECK (decided_by IN ('rule', 'ai', 'user'))
) STRICT;

CREATE TABLE EmphasisItem (
    id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT NOT NULL REFERENCES Course(id),
    term_or_topic TEXT NOT NULL,
    lectures TEXT NOT NULL CHECK (json_valid(lectures) AND json_type(lectures) = 'array'),
    mention_count INTEGER NOT NULL CHECK (mention_count >= 0),
    minutes_spent REAL,
    example_solved INTEGER NOT NULL CHECK (example_solved IN (0, 1)),
    score_components TEXT NOT NULL CHECK (json_valid(score_components) AND json_type(score_components) = 'object')
) STRICT;

CREATE TABLE QuizSet (
    id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT NOT NULL REFERENCES Course(id),
    scope TEXT NOT NULL CHECK (json_valid(scope) AND json_type(scope) = 'object'),
    created_by TEXT NOT NULL CHECK (created_by = 'ai'),
    created_at INTEGER NOT NULL,
    generator_version TEXT NOT NULL
) STRICT;

CREATE TABLE QuizItem (
    id TEXT PRIMARY KEY NOT NULL,
    quiz_set_id TEXT NOT NULL REFERENCES QuizSet(id),
    type TEXT NOT NULL CHECK (type IN ('mcq', 'short', 'ox', 'calc', 'essay')),
    question TEXT NOT NULL,
    choices TEXT CHECK (json_valid(choices) AND json_type(choices) = 'array'),
    answer TEXT NOT NULL,
    explanation TEXT NOT NULL,
    sources TEXT NOT NULL CHECK (json_valid(sources) AND json_type(sources) = 'array'),
    quoted_evidence TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    flags TEXT NOT NULL CHECK (json_valid(flags) AND json_type(flags) = 'array'),
    status TEXT NOT NULL CHECK (status IN ('active', 'rejected'))
) STRICT;

CREATE TABLE QuizAttempt (
    quiz_item_id TEXT NOT NULL REFERENCES QuizItem(id),
    at INTEGER NOT NULL,
    answer_given TEXT NOT NULL,
    correct INTEGER NOT NULL CHECK (correct IN (0, 1)),
    review_state TEXT NOT NULL CHECK (review_state IN ('new', 'wrong', 'reviewing', 'mastered'))
) STRICT;

CREATE TABLE PastExam (
    id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT NOT NULL REFERENCES Course(id),
    path TEXT NOT NULL,
    year INTEGER,
    term TEXT,
    exam_kind TEXT NOT NULL CHECK (exam_kind IN ('중간', '기말', '퀴즈', '기타')),
    professor TEXT,
    ocr_used INTEGER NOT NULL CHECK (ocr_used IN (0, 1))
) STRICT;

CREATE TABLE PastExamQuestion (
    id TEXT PRIMARY KEY NOT NULL,
    past_exam_id TEXT NOT NULL REFERENCES PastExam(id),
    number TEXT NOT NULL,
    text TEXT NOT NULL,
    points REAL,
    type TEXT NOT NULL,
    topic_links TEXT NOT NULL CHECK (json_valid(topic_links) AND json_type(topic_links) = 'array')
) STRICT;

CREATE TABLE TopicExamLikelihood (
    id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT NOT NULL REFERENCES Course(id),
    topic TEXT NOT NULL,
    exam TEXT NOT NULL CHECK (exam IN ('중간', '기말', '퀴즈')),
    prob_lo REAL NOT NULL CHECK (prob_lo BETWEEN 0 AND 1),
    prob_hi REAL NOT NULL CHECK (prob_hi BETWEEN prob_lo AND 1),
    level TEXT NOT NULL CHECK (level IN ('높음', '중간', '낮음')),
    signals TEXT NOT NULL CHECK (json_valid(signals) AND json_type(signals) = 'object'),
    method_version TEXT NOT NULL,
    n_past_exams INTEGER NOT NULL CHECK (n_past_exams >= 0),
    confidence_class TEXT NOT NULL,
    created_at INTEGER NOT NULL
) STRICT;

CREATE TABLE ExamOutcome (
    course_id TEXT NOT NULL REFERENCES Course(id),
    exam TEXT NOT NULL,
    topic TEXT NOT NULL,
    appeared INTEGER NOT NULL CHECK (appeared IN (0, 1)),
    recorded_by TEXT NOT NULL CHECK (recorded_by = 'user'),
    at INTEGER NOT NULL
) STRICT;

CREATE TABLE TopicLink (
    id TEXT PRIMARY KEY NOT NULL,
    subject TEXT NOT NULL CHECK (subject IN ('past_exam_question', 'quiz_item', 'emphasis_item')),
    lecture_id TEXT REFERENCES Lecture(id),
    material_id TEXT REFERENCES Material(id),
    page INTEGER,
    at_ms INTEGER,
    confidence_class TEXT NOT NULL,
    decided_by TEXT NOT NULL CHECK (decided_by IN ('rule', 'ai', 'user'))
) STRICT;

CREATE TABLE Evidence (
    id TEXT PRIMARY KEY NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    field TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    weight REAL,
    source TEXT NOT NULL CHECK (source IN ('user', 'metadata', 'heuristic', 'engine', 'material', 'ai', 'agent')),
    created_at INTEGER NOT NULL
) STRICT;

CREATE TABLE EngineMetricObservation (
    id TEXT PRIMARY KEY NOT NULL,
    metric_kind TEXT NOT NULL CHECK (metric_kind IN (
        'estimate', 'material_anchored', 'observed_qa', 'operational', 'ground_truth'
    )),
    metric_name TEXT NOT NULL,
    engine TEXT NOT NULL,
    engine_b TEXT,
    engine_version TEXT,
    comparison_set_id TEXT NOT NULL,
    device_type TEXT NOT NULL,
    course_id TEXT NOT NULL REFERENCES Course(id),
    lecture_type TEXT NOT NULL,
    value REAL NOT NULL,
    sample_size INTEGER NOT NULL CHECK (sample_size >= 0),
    method TEXT NOT NULL,
    window_start INTEGER,
    created_at INTEGER NOT NULL
) STRICT;

-- Optional cache only. Observations remain the source of truth (section 4 R11).
CREATE TABLE EngineMetricAggregate (
    engine TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    value REAL NOT NULL,
    range_lo REAL,
    range_hi REAL,
    n INTEGER NOT NULL CHECK (n >= 0),
    method TEXT NOT NULL,
    calculated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE Event (
    id TEXT PRIMARY KEY NOT NULL,
    at INTEGER NOT NULL,
    type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object')
) STRICT, WITHOUT ROWID;

-- Child foreign keys need a leading index column. Separate temporal indexes
-- also support time-window queries independently of their parent entity.
CREATE INDEX idx_Schedule_start ON Schedule(start);
CREATE INDEX idx_Schedule_end ON Schedule(end);
CREATE INDEX idx_ScheduleException_course ON ScheduleException(course_id);
CREATE INDEX idx_ScheduleException_date ON ScheduleException(date);
CREATE INDEX idx_Lecture_course ON Lecture(course_id);
CREATE INDEX idx_Lecture_date ON Lecture(date);
CREATE INDEX idx_MediaAsset_lecture ON MediaAsset(lecture_id);
CREATE INDEX idx_MediaAsset_derived ON MediaAsset(derived_from);
CREATE INDEX idx_MediaAsset_recorded ON MediaAsset(recorded_start);
CREATE INDEX idx_RecordingSession_lecture ON RecordingSession(lecture_id);
CREATE INDEX idx_RecordingSession_media ON RecordingSession(media_id);
CREATE INDEX idx_RecordingSession_started ON RecordingSession(started_at);
CREATE INDEX idx_RecordingSession_ended ON RecordingSession(ended_at);
CREATE INDEX idx_StopReport_at ON StopReport(at_ms);
CREATE INDEX idx_RecordingAnomaly_session ON RecordingAnomaly(session_id);
CREATE INDEX idx_RecordingAnomaly_from ON RecordingAnomaly(from_ms);
CREATE INDEX idx_RecordingAnomaly_to ON RecordingAnomaly(to_ms);
CREATE INDEX idx_MoveLog_at ON MoveLog(at);
CREATE INDEX idx_MoveLog_start ON MoveLog(estimated_start);
CREATE INDEX idx_MoveLog_end ON MoveLog(estimated_end);
CREATE INDEX idx_ChunkPlan_media ON ChunkPlan(media_id);
CREATE INDEX idx_Transcript_lecture ON Transcript(lecture_id);
CREATE INDEX idx_Transcript_media ON Transcript(media_id);
CREATE INDEX idx_Transcript_plan ON Transcript(chunk_plan_id);
CREATE INDEX idx_TranscriptChunkRun_started ON TranscriptChunkRun(started_at);
CREATE INDEX idx_TranscriptChunkRun_ended ON TranscriptChunkRun(ended_at);
CREATE INDEX idx_TranscriptSegment_start ON TranscriptSegment(start_ms);
CREATE INDEX idx_TranscriptSegment_end ON TranscriptSegment(end_ms);
CREATE INDEX idx_TranscriptQA_transcript ON TranscriptQA(transcript_id);
CREATE INDEX idx_CanonicalTranscript_parent ON CanonicalTranscript(lecture_id, parent_version);
CREATE INDEX idx_CanonicalTranscript_created ON CanonicalTranscript(created_at);
CREATE INDEX idx_CanonicalSegment_start ON CanonicalSegment(start_ms);
CREATE INDEX idx_CanonicalSegment_end ON CanonicalSegment(end_ms);
CREATE INDEX idx_Correction_lecture ON Correction(lecture_id);
CREATE INDEX idx_Correction_created ON Correction(created_at);
CREATE INDEX idx_Material_course ON Material(course_id);
CREATE INDEX idx_Material_uploaded ON Material(etl_uploaded_at);
CREATE INDEX idx_MaterialMatch_lecture ON MaterialMatch(lecture_id);
CREATE INDEX idx_MaterialMatch_material ON MaterialMatch(material_id);
CREATE INDEX idx_Syllabus_course ON Syllabus(course_id);
CREATE INDEX idx_Syllabus_date ON Syllabus(planned_date);
CREATE INDEX idx_ProcessingJob_lecture ON ProcessingJob(lecture_id);
CREATE INDEX idx_ProcessingJob_next ON ProcessingJob(next_eligible_at);
CREATE INDEX idx_ProviderConsent_course_id ON ProviderConsent(course_id);
CREATE INDEX idx_ProviderConsent_granted ON ProviderConsent(granted_at);
CREATE INDEX idx_QuotaLedger_date ON QuotaLedger(date);
CREATE INDEX idx_Bookmark_time ON Bookmark(timestamp);
CREATE INDEX idx_AlignmentAnchor_lecture ON AlignmentAnchor(lecture_id);
CREATE INDEX idx_AlignmentAnchor_material ON AlignmentAnchor(material_id);
CREATE INDEX idx_AlignmentAnchor_segment ON AlignmentAnchor(transcript_segment_id);
CREATE INDEX idx_AlignmentAnchor_start ON AlignmentAnchor(start_time_ms);
CREATE INDEX idx_AlignmentAnchor_end ON AlignmentAnchor(end_time_ms);
CREATE INDEX idx_SlideViewEvent_session ON SlideViewEvent(session_id);
CREATE INDEX idx_SlideViewEvent_page ON SlideViewEvent(material_id, page_no);
CREATE INDEX idx_SlideViewEvent_at ON SlideViewEvent(at_ms);
CREATE INDEX idx_ExamCue_lecture ON ExamCue(lecture_id);
CREATE INDEX idx_ExamCue_at ON ExamCue(at_ms);
CREATE INDEX idx_EmphasisItem_course ON EmphasisItem(course_id);
CREATE INDEX idx_QuizSet_course ON QuizSet(course_id);
CREATE INDEX idx_QuizSet_created ON QuizSet(created_at);
CREATE INDEX idx_QuizItem_set ON QuizItem(quiz_set_id);
CREATE INDEX idx_QuizAttempt_item ON QuizAttempt(quiz_item_id);
CREATE INDEX idx_QuizAttempt_at ON QuizAttempt(at);
CREATE INDEX idx_PastExam_course ON PastExam(course_id);
CREATE INDEX idx_PastExamQuestion_exam ON PastExamQuestion(past_exam_id);
CREATE INDEX idx_TopicExamLikelihood_course ON TopicExamLikelihood(course_id);
CREATE INDEX idx_TopicExamLikelihood_created ON TopicExamLikelihood(created_at);
CREATE INDEX idx_ExamOutcome_course ON ExamOutcome(course_id);
CREATE INDEX idx_ExamOutcome_at ON ExamOutcome(at);
CREATE INDEX idx_TopicLink_lecture ON TopicLink(lecture_id);
CREATE INDEX idx_TopicLink_material ON TopicLink(material_id);
CREATE INDEX idx_TopicLink_at ON TopicLink(at_ms);
CREATE INDEX idx_Evidence_subject ON Evidence(subject_type, subject_id);
CREATE INDEX idx_Evidence_created ON Evidence(created_at);
CREATE INDEX idx_EngineMetricObservation_course ON EngineMetricObservation(course_id);
CREATE INDEX idx_EngineMetricObservation_window ON EngineMetricObservation(window_start);
CREATE INDEX idx_EngineMetricObservation_created ON EngineMetricObservation(created_at);
CREATE INDEX idx_EngineMetricAggregate_calculated ON EngineMetricAggregate(calculated_at);
CREATE INDEX idx_Event_subject ON Event(subject_type, subject_id);
CREATE INDEX idx_Event_at ON Event(at);

-- Preserve immutable evidence, including INSERT OR REPLACE attempts. The
-- explicit insert guards do not rely on callers enabling recursive triggers.
CREATE TRIGGER Event_no_update BEFORE UPDATE ON Event BEGIN
    SELECT RAISE(ABORT, 'Event is append-only');
END;
CREATE TRIGGER Event_no_delete BEFORE DELETE ON Event BEGIN
    SELECT RAISE(ABORT, 'Event is append-only');
END;
CREATE TRIGGER Event_no_replace BEFORE INSERT ON Event
WHEN EXISTS (SELECT 1 FROM Event WHERE id = NEW.id) BEGIN
    SELECT RAISE(ABORT, 'Event is append-only');
END;

CREATE TRIGGER Transcript_no_update BEFORE UPDATE ON Transcript
WHEN NEW.id IS NOT OLD.id OR NEW.lecture_id IS NOT OLD.lecture_id
    OR NEW.media_id IS NOT OLD.media_id OR NEW.engine IS NOT OLD.engine
    OR NEW.engine_version IS NOT OLD.engine_version OR NEW.chunk_plan_id IS NOT OLD.chunk_plan_id
    OR NEW.source_file IS NOT OLD.source_file OR NEW.sha256 IS NOT OLD.sha256 BEGIN
    SELECT RAISE(ABORT, 'Transcript source identity is immutable');
END;
CREATE TRIGGER Transcript_no_delete BEFORE DELETE ON Transcript BEGIN
    SELECT RAISE(ABORT, 'Transcript is immutable');
END;
CREATE TRIGGER Transcript_no_replace BEFORE INSERT ON Transcript
WHEN EXISTS (SELECT 1 FROM Transcript WHERE id = NEW.id OR sha256 = NEW.sha256) BEGIN
    SELECT RAISE(ABORT, 'Transcript is immutable; duplicate id or sha256');
END;

CREATE TRIGGER TranscriptSegment_no_update BEFORE UPDATE ON TranscriptSegment BEGIN
    SELECT RAISE(ABORT, 'TranscriptSegment is immutable');
END;
CREATE TRIGGER TranscriptSegment_no_delete BEFORE DELETE ON TranscriptSegment BEGIN
    SELECT RAISE(ABORT, 'TranscriptSegment is immutable');
END;
CREATE TRIGGER TranscriptSegment_no_replace BEFORE INSERT ON TranscriptSegment
WHEN EXISTS (SELECT 1 FROM TranscriptSegment WHERE transcript_id = NEW.transcript_id AND seq = NEW.seq) BEGIN
    SELECT RAISE(ABORT, 'TranscriptSegment is immutable');
END;

CREATE TRIGGER MediaAsset_master_no_update BEFORE UPDATE ON MediaAsset
WHEN OLD.is_master = 1 AND (
    NEW.id IS NOT OLD.id OR NEW.path IS NOT OLD.path OR NEW.sha256 IS NOT OLD.sha256
    OR NEW.duration_ms IS NOT OLD.duration_ms OR NEW.codec IS NOT OLD.codec
    OR NEW.sample_rate IS NOT OLD.sample_rate OR NEW.channels IS NOT OLD.channels
    OR NEW.bytes IS NOT OLD.bytes OR NEW.origin IS NOT OLD.origin OR NEW.device_type IS NOT OLD.device_type
    OR NEW.is_master IS NOT OLD.is_master OR NEW.derived_from IS NOT OLD.derived_from
    OR NEW.preset_id IS NOT OLD.preset_id OR NEW.recorded_start IS NOT OLD.recorded_start
) BEGIN
    SELECT RAISE(ABORT, 'Master MediaAsset source identity is immutable');
END;
CREATE TRIGGER MediaAsset_master_no_delete BEFORE DELETE ON MediaAsset WHEN OLD.is_master = 1 BEGIN
    SELECT RAISE(ABORT, 'Master MediaAsset is immutable');
END;
CREATE TRIGGER MediaAsset_no_replace BEFORE INSERT ON MediaAsset
WHEN EXISTS (SELECT 1 FROM MediaAsset WHERE id = NEW.id OR sha256 = NEW.sha256) BEGIN
    SELECT RAISE(ABORT, 'MediaAsset duplicate id or sha256');
END;
CREATE TRIGGER MediaAsset_no_update_replace BEFORE UPDATE ON MediaAsset
WHEN EXISTS (SELECT 1 FROM MediaAsset WHERE (id = NEW.id OR sha256 = NEW.sha256) AND id != OLD.id) BEGIN
    SELECT RAISE(ABORT, 'MediaAsset duplicate id or sha256');
END;
