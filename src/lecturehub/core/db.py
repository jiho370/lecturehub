"""SQLite connections and packaged, transactional schema migrations."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from lecturehub.core.ids import new_ulid

_MIGRATION_NAME = re.compile(r"(\d{4})_.+\.sql\Z")
_REBUILD_MARKER = "-- lecturehub: rebuild-preserving-data"
_REBUILD_TABLE = re.compile(r"^-- lecturehub: rebuild-table=([A-Za-z][A-Za-z0-9_]*)$", re.MULTILINE)


class SchemaTooNewError(ValueError):
    """D-051: the database was written by a newer app; refuse before any write."""


def _packaged_migrations() -> dict[int, object]:
    directory = resources.files("lecturehub.core").joinpath("migrations")
    migrations = {}
    for file in directory.iterdir():
        match = _MIGRATION_NAME.fullmatch(file.name)
        if not match or not file.is_file():
            continue
        version = int(match.group(1))
        if version < 1 or version in migrations:
            raise ValueError(f"Invalid or duplicate migration version: {file.name}")
        migrations[version] = file
    return migrations


def known_schema_version() -> int:
    """Highest schema version this copy of the app knows how to use."""
    return max(_packaged_migrations(), default=0)


def check_schema_version(connection: sqlite3.Connection) -> int:
    """Read-only check that the database is not newer than this app (D-051).

    Returns the database's highest applied version (0 for a new database).
    Raises SchemaTooNewError when it holds versions this app does not know.
    """
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_version'").fetchone()
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_version")} if has_table else set()
    known = set(_packaged_migrations())
    unknown = sorted(applied - known)
    if unknown:
        raise SchemaTooNewError(
            f"이 데이터베이스는 이 앱보다 새 버전입니다(DB 스키마 {max(applied)}, 앱이 아는 최대 {max(known, default=0)}; "
            f"모르는 버전 {unknown}). 아무것도 쓰지 않고 멈췄습니다. 앱을 최신 검증 버전으로 바꾸거나, "
            "이 데이터베이스를 만든 앱으로 여세요. 녹음은 데이터베이스와 무관하게 계속할 수 있습니다.")
    return max(applied, default=0)


def _precheck_file(path: str | Path) -> None:
    """Check an existing file without creating -wal/-shm or taking locks.

    ``immutable=1`` reads the main file only, so a version recorded solely in an
    un-checkpointed WAL can slip past; connect() checks again through the real
    connection before returning, still before any caller can write.
    """
    if str(path) == ":memory:" or not Path(path).is_file():
        return
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    probe = sqlite3.connect(uri, uri=True)
    try:
        check_schema_version(probe)
    except sqlite3.DatabaseError:
        pass  # not readable this way (e.g. not a database): the real open reports it
    finally:
        probe.close()


def _register_functions(connection: sqlite3.Connection) -> None:
    """Install functions used by additive migrations and automatic row IDs."""
    connection.create_function("new_ulid", 0, new_ulid)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a database with foreign keys, WAL, and named-column access.

    The parent directory must already exist. ``:memory:`` is also accepted
    for callers that do not need persistence (SQLite uses its memory journal).
    """
    _precheck_file(path)  # D-051, leaving the file and its directory untouched
    connection = sqlite3.connect(path, timeout=30)
    try:
        # D-051 again through the WAL, before anything that could write.
        check_schema_version(connection)
        _register_functions(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute("PRAGMA journal_mode = WAL")
    except BaseException:
        connection.close()
        raise
    return connection


def _statements(script: str):
    """Split complete SQL statements, preserving trigger bodies and literals."""
    start = 0
    for end, character in enumerate(script):
        if character == ";" and sqlite3.complete_statement(script[start : end + 1]):
            yield script[start : end + 1]
            start = end + 1
    if script[start:].strip():
        yield script[start:]


def _backup_before_rebuild(connection: sqlite3.Connection, version: int) -> Path | None:
    """Take a consistent WAL-aware backup; in-memory test databases need none."""
    filename = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
    if not filename:
        return None
    database = Path(filename)
    if database.is_symlink():
        raise ValueError("Refusing to back up a database through a symbolic link")
    parent = database.parent.resolve(strict=True)
    backup_dir = parent / "backup"
    if backup_dir.resolve().parent != parent:
        raise ValueError("Backup directory escapes the database directory")
    backup_dir.mkdir(exist_ok=True)
    if backup_dir.resolve().parent != parent:
        raise ValueError("Backup directory escapes the database directory")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{database.stem}_before_{version:04d}_{timestamp}_{new_ulid()}.sqlite"
    # Reserve a new path without ever overwriting an earlier recovery point.
    with destination.open("xb"):
        pass
    backup = sqlite3.connect(destination)
    try:
        connection.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("Database backup failed its integrity check")
    finally:
        backup.close()
    return destination


def _table_fingerprint(connection: sqlite3.Connection, table: str, columns: list[str]) -> tuple[int, str]:
    """Hash original values deterministically without retaining raw text in memory."""
    projection = ",".join(f'"{column}"' for column in columns)
    query = f'SELECT {projection} FROM "{table}"'
    if "id" in columns:
        query += ' ORDER BY "id"'
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(query):
        encoded = repr(tuple(row)).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def _snapshot_rebuild(connection: sqlite3.Connection, tables: list[str]) -> tuple[dict, dict, list[str]]:
    """Remember rebuilt parents, every direct child, events and schema objects."""
    all_tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
    protected = set(tables)
    targets = {table.casefold() for table in tables}
    for table in all_tables:
        if any(row[2].casefold() in targets for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')):
            protected.add(table)
    if "Event" in all_tables:
        protected.add("Event")
    fingerprints = {}
    for table in sorted(protected):
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        fingerprints[table] = (columns, _table_fingerprint(connection, table, columns))
    objects = {}
    for kind, name, table, definition in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type IN ('index','trigger','view')"
    ):
        if kind == "view" or table in tables:
            objects[name] = (kind, definition)
    return fingerprints, objects, tables


def _verify_rebuild(connection: sqlite3.Connection, snapshot: tuple[dict, dict, list[str]]) -> None:
    fingerprints, objects, tables = snapshot
    for name, (kind, definition) in objects.items():
        row = connection.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()
        if row is None and definition is not None:
            connection.execute(definition)
        elif row is None or tuple(row) != (kind, definition):
            raise sqlite3.IntegrityError(f"Rebuild changed existing schema object: {name}")
    actual_names = {
        name for kind, name, table in connection.execute(
            "SELECT type,name,tbl_name FROM sqlite_schema WHERE type IN ('index','trigger','view')"
        ) if kind == "view" or table in tables
    }
    if actual_names != objects.keys():
        raise sqlite3.IntegrityError("Rebuild changed the existing index/trigger/view inventory")
    for table, (columns, expected) in fingerprints.items():
        if _table_fingerprint(connection, table, columns) != expected:
            raise sqlite3.IntegrityError(f"Rebuild changed existing rows: {table}")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise sqlite3.IntegrityError("Migration would leave invalid foreign key references")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise sqlite3.IntegrityError("Migration failed its integrity check")


def migrate(connection: sqlite3.Connection) -> list[int]:
    """Apply pending packaged SQL files atomically and return their versions.

    Requires an idle connection: migration must never commit a caller's work.
    An immediate transaction serializes concurrent initializers, and migration
    DDL and version records roll back together if any statement fails.
    Explicitly marked data-preserving rebuilds temporarily use SQLite's
    supported foreign-key-off/legacy-alter mode and validate every foreign
    key before commit. File databases get a WAL-aware backup first; memory
    databases are intentionally excluded. Legacy-alter mode is restored and
    foreign-key enforcement is enabled on every rebuild exit, including errors.
    """
    if connection.in_transaction:
        raise ValueError("Migrations require a connection with no active transaction")
    _register_functions(connection)
    check_schema_version(connection)
    migrations = _packaged_migrations()

    scripts = {version: file.read_text(encoding="utf-8-sig") for version, file in migrations.items()}
    has_version_table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_version'"
    ).fetchone()
    previously_applied = {
        row[0] for row in connection.execute("SELECT version FROM schema_version")
    } if has_version_table else set()
    rebuilds = [
        version
        for version, script in scripts.items() if version not in previously_applied and script.splitlines()
        and script.splitlines()[0] == _REBUILD_MARKER
    ]
    needs_rebuild = bool(rebuilds)
    legacy_alter_table = connection.execute("PRAGMA legacy_alter_table").fetchone()[0]
    backup_path = None
    try:
        if needs_rebuild:
            if previously_applied:
                backup_path = _backup_before_rebuild(connection, min(rebuilds))
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA legacy_alter_table = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_version")}
        unknown = applied - migrations.keys()
        if unknown:  # a newer app wrote here between the check above and this lock
            raise SchemaTooNewError(f"Database has unavailable migration versions: {sorted(unknown)}")
        pending = sorted(migrations.keys() - applied)
        if applied and pending and min(pending) < max(applied):
            raise ValueError("An unapplied migration precedes the current schema version")
        for version in pending:
            snapshot = None
            if version in rebuilds:
                tables = _REBUILD_TABLE.findall(scripts[version])
                if not tables:
                    raise ValueError("A data-preserving rebuild must declare its rebuilt tables")
                snapshot = _snapshot_rebuild(connection, tables)
            for statement in _statements(scripts[version]):
                connection.execute(statement)
            if snapshot is not None:
                _verify_rebuild(connection, snapshot)
            connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
        if needs_rebuild and connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("Migration would leave invalid foreign key references")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        if needs_rebuild:
            connection.execute(f"PRAGMA legacy_alter_table = {int(legacy_alter_table)}")
            connection.execute("PRAGMA foreign_keys = ON")
    if backup_path is not None:
        auto_prune_backups(backup_path.parent)
    return pending


# ---------------------------------------------------------------- backup retention (user decision 2026-11-09)
# Keep every pre-migration backup and the newest KEEP_LATEST others; zip the rest into one archive.
KEEP_LATEST = 5
_SIDECARS = ("-wal", "-shm", "-journal")
# "<stem>_before_0003_<time>_<ulid>.sqlite" (this module) and the hand-made "lecturehub_before_0005_0006_<time>.sqlite":
# a 4-digit schema version (or several) right after "_before_". Hand-made pre-operation backups name a task instead
# ("_before_notes_…", "_20261106_before_mai_sync").
_PRE_MIGRATION = re.compile(r"_before_\d{4}(?:_\d{4})*_", re.IGNORECASE)
LIVE_DATABASE = "lecturehub.sqlite"


def is_pre_migration_backup(name: str) -> bool:
    return bool(_PRE_MIGRATION.search(name))


def recording_session(app_directory: Path) -> str | None:
    """A recorder session that still holds its lock under <_lecturehub>/recordings (AGENTS §2.7), else None."""
    recordings = Path(app_directory) / "recordings"
    if not recordings.is_dir():
        return None
    from lecturehub.recorder.heartbeat import SessionLock

    for folder in recordings.iterdir():
        if (folder / ".recording.lock").exists() and not (folder / "stop.json").exists():
            try:
                with SessionLock(folder):
                    pass
            except RuntimeError:
                return folder.name
    return None


def _backup_sets(backup_dir: Path) -> list[dict]:
    """Each backup database with its -wal/-shm/-journal leftovers, newest first. Other files are not touched."""
    sets = []
    for path in backup_dir.iterdir():
        if path.suffix.lower() != ".sqlite" or not path.is_file() or path.is_symlink() \
                or path.name.lower() == LIVE_DATABASE:
            continue
        sidecars = [backup_dir / (path.name + suffix) for suffix in _SIDECARS]
        files = [path] + [p for p in sidecars if p.is_file() and not p.is_symlink()]
        stat = path.stat()
        sets.append({"name": path.name, "files": files, "mtime": stat.st_mtime,
                     "bytes": sum(p.stat().st_size for p in files), "pre_migration": is_pre_migration_backup(path.name)})
    sets.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    return sets


def _zip_path(backup_dir: Path, day: str) -> Path:
    path, n = backup_dir / f"old_backups_{day}.zip", 2
    while path.exists() or path.with_name(path.name + ".part").exists():
        path, n = backup_dir / f"old_backups_{day}_{n}.zip", n + 1
    return path


def prune_backups(backup_dir: str | Path, keep_latest: int = KEEP_LATEST, *, dry_run: bool = False,
                  today: str | None = None) -> dict:
    """Keep pre-migration backups and the newest ``keep_latest`` others; zip the rest, verify, then delete them.

    Only ``*.sqlite`` backups (and their -wal/-shm/-journal files) in ``backup_dir`` are considered; undo CSVs,
    earlier zips and anything else stay. The zip is written as ``.part``, checked (``testzip`` + every member's
    size against the original), renamed, and only then are the originals removed. Never overwrites a zip.
    """
    import zipfile

    backup_dir = Path(backup_dir)
    if backup_dir.name.lower() != "backup" or not backup_dir.is_dir() or backup_dir.is_symlink():
        raise ValueError(f"백업 폴더(_lecturehub\\backup)가 아닙니다: {backup_dir}")
    sets = _backup_sets(backup_dir)
    pre = [s for s in sets if s["pre_migration"]]
    others = [s for s in sets if not s["pre_migration"]]
    keep, old = others[:max(0, keep_latest)], others[max(0, keep_latest):]
    old.sort(key=lambda item: (item["mtime"], item["name"]))
    result = {"dry_run": dry_run, "backup_dir": str(backup_dir), "pre_migration": [s["name"] for s in pre],
              "kept_latest": [s["name"] for s in keep], "zipped": [s["name"] for s in old],
              "files": sum(len(s["files"]) for s in old), "bytes": sum(s["bytes"] for s in old),
              "zip": None, "zip_bytes": 0, "bytes_freed": 0, "errors": []}
    if dry_run or not old:
        return result
    day = today or datetime.now().astimezone().strftime("%Y%m%d")
    target = _zip_path(backup_dir, day)
    part = target.with_name(target.name + ".part")
    expected = {p.name: p.stat().st_size for s in old for p in s["files"]}
    try:
        with zipfile.ZipFile(part, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True,
                             strict_timestamps=False) as archive:
            for s in old:
                for path in s["files"]:
                    archive.write(path, arcname=path.name)
        with zipfile.ZipFile(part) as archive:
            bad = archive.testzip()
            sizes = {info.filename: info.file_size for info in archive.infolist()}
        if bad is not None or sizes != expected:
            raise OSError(f"압축 확인 실패({bad or '크기 불일치'}) — 원본을 지우지 않았습니다")
        part.rename(target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    result["zip"], result["zip_bytes"] = str(target), target.stat().st_size
    removed = 0
    for s in old:
        for path in s["files"]:
            try:
                path.unlink()
                removed += expected[path.name]
            except OSError as exc:  # stays on disk; it is also in the zip
                result["errors"].append(f"{path.name}: {exc}")
    result["bytes_freed"] = removed - result["zip_bytes"]
    return result


def auto_prune_backups(backup_dir: Path) -> dict | None:
    """After a new backup: keep the folder bounded. Skipped while recording; never raises (the backup already worked)."""
    try:
        if recording_session(Path(backup_dir).parent):
            return None
        return prune_backups(backup_dir)
    except Exception:  # noqa: BLE001 - retention must never break a migration or a backup
        return None


def make_backup(database: str | Path, label: str) -> Path:
    """A hand-made recovery point before a real-DB data change: backup\\<stem>_before_<label>_<local time>.sqlite.

    Same SQLite backup API and integrity check as the pre-migration backup; then the folder is pruned.
    """
    database = Path(database)
    if not database.is_file() or database.is_symlink():
        raise ValueError(f"DB 파일이 없습니다: {database}")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,40}", label):  # a letter first: never read as a schema version
        raise ValueError("이름표는 영문자로 시작하고 영문·숫자·_-로 41자 이내여야 합니다(예: notes1109).")
    backup_dir = database.resolve().parent / "backup"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    destination = backup_dir / f"{database.stem}_before_{label}_{stamp}.sqlite"
    with destination.open("xb"):
        pass
    source = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    backup = sqlite3.connect(destination)
    try:
        source.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("Database backup failed its integrity check")
    finally:
        backup.close()
        source.close()
    auto_prune_backups(backup_dir)
    return destination


def auto_upgrade_allowed() -> bool:
    """Installed builds and the public edition upgrade an older database by themselves (their users have no
    command line). The owner's source checkout keeps the deliberate, hand-run upgrade."""
    from . import edition, proc

    return proc.frozen() or edition.name() == "public"


def upgrade(path: str | Path) -> list[int]:
    """Bring an existing database up to this app's schema after an app update; returns the applied versions.

    Always takes an integrity-checked backup first (``migrate`` alone does so only before rebuilds). Nothing is
    done when the database is current; a newer database still raises SchemaTooNewError before any write.
    """
    connection = connect(path)
    try:
        current = check_schema_version(connection)
        if current >= known_schema_version():
            return []
        backup = _backup_before_rebuild(connection, current + 1) if current else None
        applied = migrate(connection)
    finally:
        connection.close()
    if backup is not None:
        auto_prune_backups(backup.parent)
    return applied


def init_db(path: str | Path) -> None:
    """Create the database parent directory and apply pending migrations."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        migrate(connection)
    finally:
        connection.close()
