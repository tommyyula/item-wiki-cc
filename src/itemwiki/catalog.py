"""SQLite catalog: the single source of truth for everything the tool knows about files."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    root        TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    n_files     INTEGER DEFAULT 0,
    n_bytes     INTEGER DEFAULT 0,
    n_new       INTEGER DEFAULT 0,
    n_changed   INTEGER DEFAULT 0,
    n_missing   INTEGER DEFAULT 0,
    n_errors    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY,
    root            TEXT NOT NULL,          -- scan root this file belongs to
    path            TEXT NOT NULL UNIQUE,   -- absolute path
    rel_path        TEXT NOT NULL,          -- path relative to root
    parent          TEXT NOT NULL,          -- rel_path of containing dir ('' = root)
    top_dir         TEXT NOT NULL,          -- first path component under root ('' = file at root)
    depth           INTEGER NOT NULL,
    name            TEXT NOT NULL,
    ext             TEXT NOT NULL,
    kind            TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    blocks          INTEGER,                -- st_blocks; 0 with size>0 => likely cloud-only placeholder
    cloud_only      INTEGER DEFAULT 0,
    in_archive_dir  INTEGER DEFAULT 0,      -- some ancestor dir looks like Archive/old/backup
    family          TEXT,                   -- version-family key (see filetypes.family_key)
    quick_hash      TEXT,                   -- blake2b(size + head 64K + tail 64K)
    full_hash       TEXT,                   -- blake2b(full content)
    first_scan      INTEGER,
    last_scan       INTEGER,
    status          TEXT DEFAULT 'present'  -- present | missing
);
CREATE INDEX IF NOT EXISTS ix_files_size   ON files(size);
CREATE INDEX IF NOT EXISTS ix_files_quick  ON files(quick_hash);
CREATE INDEX IF NOT EXISTS ix_files_full   ON files(full_hash);
CREATE INDEX IF NOT EXISTS ix_files_root   ON files(root, status);
CREATE INDEX IF NOT EXISTS ix_files_family ON files(family, ext);

CREATE TABLE IF NOT EXISTS errors (
    scan_id INTEGER, path TEXT, error TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    # DELETE journal (not WAL): the db may live on synced / network / VM-mounted folders.
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con
