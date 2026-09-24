"""Staged duplicate detection: size -> quick hash (head+tail) -> full hash.

Only files whose size collides with another file are ever read, and only files whose
quick hash also collides are read in full. Cloud-only placeholders are skipped unless
hydrate=True (reading them would force a download).
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import time

CHUNK = 64 * 1024
MIN_SIZE = 1  # ignore empty files; they are reported separately


def quick_hash(path: str, size: int) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(size.to_bytes(8, "little"))
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > 2 * CHUNK:
            f.seek(size - CHUNK)
            h.update(f.read(CHUNK))
    return h.hexdigest()


def full_hash(path: str) -> str:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _hash_stage(con, rows, fn, column, progress, label):
    done, t, updates = 0, time.time(), []
    for r in rows:
        try:
            updates.append((fn(r), r["id"]))
        except OSError as e:
            con.execute("INSERT INTO errors(scan_id, path, error) VALUES (NULL, ?, ?)",
                        (r["path"], f"{label}: {type(e).__name__}: {e}"))
        done += 1
        if len(updates) >= 500:
            con.executemany(f"UPDATE files SET {column}=? WHERE id=?", updates); con.commit(); updates.clear()
        if progress and time.time() - t > 2:
            t = time.time()
            print(f"\r  {label}: {done:,}/{len(rows):,}", end="", file=sys.stderr, flush=True)
    con.executemany(f"UPDATE files SET {column}=? WHERE id=?", updates)
    con.commit()
    if progress and rows:
        print(f"\r  {label}: {done:,}/{len(rows):,}", file=sys.stderr)


def run(con: sqlite3.Connection, root: str | None = None, hydrate: bool = False, progress: bool = True) -> dict:
    where = "status='present' AND size>=?" + ("" if hydrate else " AND cloud_only=0")
    args: list = [MIN_SIZE]
    if root:
        where += " AND root=?"; args.append(root)

    # Stage 1: quick hash for files sharing a size.
    rows = con.execute(f"""
        SELECT id, path, size FROM files
        WHERE {where} AND quick_hash IS NULL
          AND size IN (SELECT size FROM files WHERE {where} GROUP BY size HAVING COUNT(*) > 1)""",
                       args + args).fetchall()
    _hash_stage(con, rows, lambda r: quick_hash(r["path"], r["size"]), "quick_hash", progress, "quick hash")
    n_quick = len(rows)

    # Stage 2: full hash for files sharing a quick hash.
    rows = con.execute(f"""
        SELECT id, path, size FROM files
        WHERE {where} AND full_hash IS NULL AND quick_hash IS NOT NULL
          AND quick_hash IN (SELECT quick_hash FROM files WHERE {where} AND quick_hash IS NOT NULL
                             GROUP BY quick_hash HAVING COUNT(*) > 1)""", args + args).fetchall()
    _hash_stage(con, rows, lambda r: full_hash(r["path"]), "full_hash", progress, "full hash")
    return dict(quick_hashed=n_quick, full_hashed=len(rows))


def duplicate_groups(con: sqlite3.Connection, root: str | None = None):
    """Return list of (full_hash, size, [paths]) sorted by wasted bytes desc."""
    where, args = "status='present' AND full_hash IS NOT NULL", []
    if root:
        where += " AND root=?"; args.append(root)
    groups: dict[str, dict] = {}
    for r in con.execute(f"SELECT full_hash, size, rel_path FROM files WHERE {where} ORDER BY rel_path", args):
        g = groups.setdefault(r["full_hash"], {"size": r["size"], "paths": []})
        g["paths"].append(r["rel_path"])
    out = [(h, g["size"], g["paths"]) for h, g in groups.items() if len(g["paths"]) > 1]
    out.sort(key=lambda x: x[1] * (len(x[2]) - 1), reverse=True)
    return out
