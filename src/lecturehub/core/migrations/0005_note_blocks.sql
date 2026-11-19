-- S4 (§0.3 layer 3): evidence-linked lecture notes. Additive only.
-- A note version is one StudyArtifact row (kind 'lecture_note', version, parent_version).
-- NoteBlock rows belong to one version and never change: an edit or a regenerated block
-- makes a new version that carries the other blocks over (block_key stays the same).
-- Paragraph and item blocks need at least one citation (checked by the validator before
-- insert); 'external' blocks are outside knowledge and must carry confidence and source
-- (§8.18, SRC-001). Headings need no citation.

CREATE TABLE NoteBlock (
    id TEXT PRIMARY KEY NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES StudyArtifact(id),
    block_key TEXT NOT NULL CHECK (length(trim(block_key)) > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('heading', 'paragraph', 'item', 'external')),
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    ext_confidence TEXT CHECK (ext_confidence IN ('high', 'mid', 'low')),
    ext_source TEXT CHECK (ext_source IN ('general', 'textbook', 'web')),
    ext_ref TEXT,
    ext_conflict INTEGER NOT NULL DEFAULT 0 CHECK (ext_conflict IN (0, 1)),
    authored_by TEXT NOT NULL CHECK (authored_by IN ('ai', 'user')),
    warnings TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings) AND json_type(warnings) = 'array'),
    created_at INTEGER NOT NULL,
    CHECK ((kind = 'external') = (ext_confidence IS NOT NULL AND ext_source IS NOT NULL)),
    UNIQUE (artifact_id, seq),
    UNIQUE (artifact_id, block_key)
) STRICT;

CREATE TABLE NoteCitation (
    id TEXT PRIMARY KEY NOT NULL,
    block_id TEXT NOT NULL REFERENCES NoteBlock(id),
    kind TEXT NOT NULL CHECK (kind IN ('transcript', 'material')),
    transcript_id TEXT REFERENCES Transcript(id),
    seg_seq INTEGER,
    material_id TEXT REFERENCES Material(id),
    page_no INTEGER,
    quote TEXT,
    CHECK ((kind = 'transcript' AND transcript_id IS NOT NULL AND seg_seq IS NOT NULL
            AND material_id IS NULL AND page_no IS NULL)
        OR (kind = 'material' AND material_id IS NOT NULL AND page_no IS NOT NULL
            AND transcript_id IS NULL AND seg_seq IS NULL))
) STRICT;

CREATE INDEX idx_NoteBlock_artifact ON NoteBlock(artifact_id, seq);

CREATE INDEX idx_NoteBlock_created ON NoteBlock(created_at);

CREATE INDEX idx_NoteCitation_block ON NoteCitation(block_id);

CREATE INDEX idx_NoteCitation_transcript ON NoteCitation(transcript_id, seg_seq);

CREATE INDEX idx_NoteCitation_material ON NoteCitation(material_id, page_no);

CREATE TRIGGER NoteBlock_no_update BEFORE UPDATE ON NoteBlock BEGIN
    SELECT RAISE(ABORT, 'Note blocks are versioned: make a new version instead');
END;

CREATE TRIGGER NoteBlock_no_delete BEFORE DELETE ON NoteBlock BEGIN
    SELECT RAISE(ABORT, 'Note blocks are versioned: make a new version instead');
END;

CREATE TRIGGER NoteBlock_no_replace BEFORE INSERT ON NoteBlock
WHEN EXISTS (SELECT 1 FROM NoteBlock WHERE id = NEW.id) BEGIN
    SELECT RAISE(ABORT, 'Note blocks are versioned: make a new version instead');
END;

CREATE TRIGGER NoteCitation_no_update BEFORE UPDATE ON NoteCitation BEGIN
    SELECT RAISE(ABORT, 'Note citations are versioned with their block');
END;

CREATE TRIGGER NoteCitation_no_delete BEFORE DELETE ON NoteCitation BEGIN
    SELECT RAISE(ABORT, 'Note citations are versioned with their block');
END;

CREATE TRIGGER NoteCitation_no_replace BEFORE INSERT ON NoteCitation
WHEN EXISTS (SELECT 1 FROM NoteCitation WHERE id = NEW.id) BEGIN
    SELECT RAISE(ABORT, 'Note citations are versioned with their block');
END;
