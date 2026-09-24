"""Phase 0 scanner: walk a tree, stat every file, upsert into the catalog. Never reads file content."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

from .filetypes import DEFAULT_EXCLUDE_DIRS, classify, family_key, is_archive_dir, is_noise

BATCH = 2000


def _walk(root: str, exclude_dirs: set[str], errors: list[tuple[str, str]]):
    """Yield (abs_path, stat) for every regular file. Uses os.scandir; does not follow symlinks."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError as e:
            errors.append((d, f"{type(e).__name__}: {e}"))
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        # Keep .app / .photoslibrary style bundles opaque? For now descend; excluded names skipped.
                        if entry.name not in exclude_dirs:
                            stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False) or is_noise(entry.name):
                        continue
                    yield entry.path, entry.stat(follow_symlinks=False)
                except OSError as e:
                    errors.append((entry.path, f"{type(e).__name__}: {e}"))


def scan(con: sqlite3.Connection, root: str, exclude_dirs: set[str] | None = None,
         progress: bool = True) -> dict:
    root = os.path.abspath(root).rstrip(os.sep) or os.sep
    if not os.path.isdir(root):
        raise SystemExit(f"not a directory: {root}")
    exclude = set(DEFAULT_EXCLUDE_DIRS) | set(exclude_dirs or ())

    t0 = time.time()
    scan_id = con.execute("INSERT INTO scans(root, started_at) VALUES (?, ?)", (root, t0)).lastrowid

    # Existing state for incremental comparison: path -> (size, mtime)
    known = {r["path"]: (r["size"], r["mtime"])
             for r in con.execute("SELECT path, size, mtime FROM files WHERE root = ?", (root,))}

    errors: list[tuple[str, str]] = []
    stats = dict(n_files=0, n_bytes=0, n_new=0, n_changed=0)
    rows_new, rows_changed, rows_same = [], [], []
    last_print = 0.0

    def flush():
        if rows_new:
            con.executemany(
                """INSERT INTO files(root, path, rel_path, parent, top_dir, depth, name, ext, kind, size, mtime,
                       blocks, cloud_only, in_archive_dir, family, first_scan, last_scan, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'present')""", rows_new)
        if rows_changed:
            # Content may have changed: drop hashes so dedup recomputes them.
            con.executemany(
                """UPDATE files SET size=?, mtime=?, blocks=?, cloud_only=?, quick_hash=NULL, full_hash=NULL,
                       last_scan=?, status='present' WHERE path=?""", rows_changed)
        if rows_same:
            con.executemany("UPDATE files SET last_scan=?, blocks=?, cloud_only=?, status='present' WHERE path=?",
                            rows_same)
        con.commit()
        rows_new.clear(); rows_changed.clear(); rows_same.clear()

    prefix_len = len(root) + 1
    for path, st in _walk(root, exclude, errors):
        rel = path[prefix_len:]
        parts = rel.split(os.sep)
        name = parts[-1]
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:          # no extension, or dotfile like ".env"
            stem, ext = name, ""
        size, mtime = st.st_size, st.st_mtime
        blocks = getattr(st, "st_blocks", None)
        cloud_only = int(bool(size > 0 and blocks == 0))
        stats["n_files"] += 1
        stats["n_bytes"] += size

        prev = known.get(path)
        if prev is None:
            in_arch = int(any(is_archive_dir(p) for p in parts[:-1]))
            rows_new.append((root, path, rel, os.sep.join(parts[:-1]), parts[0] if len(parts) > 1 else "",
                             len(parts) - 1, name, ext.lower(), classify(ext), size, mtime, blocks, cloud_only,
                             in_arch, family_key(stem), scan_id, scan_id))
            stats["n_new"] += 1
        elif prev != (size, mtime):
            rows_changed.append((size, mtime, blocks, cloud_only, scan_id, path))
            stats["n_changed"] += 1
        else:
            rows_same.append((scan_id, blocks, cloud_only, path))

        if len(rows_new) + len(rows_changed) + len(rows_same) >= BATCH:
            flush()
        if progress and time.time() - last_print > 2:
            last_print = time.time()
            print(f"\r  scanned {stats['n_files']:,} files, {stats['n_bytes'] / 1e9:,.2f} GB", end="",
                  file=sys.stderr, flush=True)
    flush()
    if progress:
        print(file=sys.stderr)
    refresh_derived(con, root)

    n_missing = con.execute("UPDATE files SET status='missing' WHERE root=? AND last_scan<? AND status='present'",
                            (root, scan_id)).rowcount
    con.executemany("INSERT INTO errors(scan_id, path, error) VALUES (?,?,?)",
                    [(scan_id, p, e) for p, e in errors])
    con.execute("""UPDATE scans SET finished_at=?, n_files=?, n_bytes=?, n_new=?, n_changed=?, n_missing=?, n_errors=?
                   WHERE id=?""",
                (time.time(), stats["n_files"], stats["n_bytes"], stats["n_new"], stats["n_changed"], n_missing,
                 len(errors), scan_id))
    con.commit()
    return dict(scan_id=scan_id, root=root, n_missing=n_missing, n_errors=len(errors),
                seconds=round(time.time() - t0, 1), **stats)


def refresh_derived(con: sqlite3.Connection, root: str) -> int:
    """Recompute heuristic columns (kind, archive flag, family) so rule changes apply to old catalogs."""
    updates = []
    for r in con.execute("SELECT id, rel_path, name, ext, kind, in_archive_dir, family FROM files WHERE root=?",
                         (root,)):
        parts = r["rel_path"].split(os.sep)
        stem = r["name"][: -(len(r["ext"]) + 1)] if r["ext"] else r["name"]
        new = (classify(r["ext"]), int(any(is_archive_dir(p) for p in parts[:-1])), family_key(stem))
        if new != (r["kind"], r["in_archive_dir"], r["family"]):
            updates.append((*new, r["id"]))
    con.executemany("UPDATE files SET kind=?, in_archive_dir=?, family=? WHERE id=?", updates)
    con.commit()
    return len(updates)
