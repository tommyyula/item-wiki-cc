"""Phase 2 executor: carry out one batch of the plan.

Safety rules:
  * dry-run unless execute=True
  * copy only — sources are never modified or removed
  * never overwrites: an existing destination file is left alone and recorded
  * copies to '<dst>.itemwiki-part', verifies size + head/tail hash, then renames into place
  * rows whose mapping rule is FLAGged are held back unless include_flagged=True
  * resumable: each row's status is stored, so a time-boxed run can be continued
"""
from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import sys
import time

from .dedup import quick_hash

LOG_HEADER = ["time", "source", "destination", "action", "reason", "size_bytes"]


def _log_writer(path: str):
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8")
    w = csv.writer(fh)
    if new:
        w.writerow(LOG_HEADER)
    return fh, w


def run(con: sqlite3.Connection, root: str, dest_root: str, batch: str | None, log_path: str | None,
        execute: bool = False, include_flagged: bool = False, max_seconds: float | None = None,
        show: int = 20) -> dict:
    root = os.path.abspath(root)
    q = "SELECT * FROM plan WHERE root=? AND status='planned'"
    args: list = [root]
    if batch:
        q += " AND top_dir=?"; args.append(batch)
    rows = [dict(r) for r in con.execute(q + " ORDER BY rel_path", args)]
    copies = [r for r in rows if r["action"] == "copy" and (include_flagged or not r["flag"])]
    held = [r for r in rows if r["action"] == "copy" and r["flag"] and not include_flagged]
    skips = [r for r in rows if r["action"] == "skip"]
    reviews = [r for r in rows if r["action"] == "review"]
    summary = dict(batch=batch or "(all)", to_copy=len(copies), to_copy_bytes=sum(r["size"] for r in copies),
                   held_flagged=len(held), skips=len(skips), reviews=len(reviews), execute=execute)

    if not execute:
        for r in copies[:show]:
            print(f"  COPY  {r['rel_path']}\n     -> {r['dst_rel']}")
        if len(copies) > show:
            print(f"  … {len(copies) - show:,} more")
        return summary

    fh, log = _log_writer(log_path)
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    done = failed = existed = 0
    copied_bytes = 0
    try:
        # Record skips once, so the log is the complete story for the batch.
        for r in skips:
            log.writerow([now(), r["rel_path"], "", "skipped", r["reason"], r["size"]])
        con.executemany("UPDATE plan SET status='done', done_at=? WHERE file_id=?",
                        [(time.time(), r["file_id"]) for r in skips])
        con.commit()

        for i, r in enumerate(copies):
            if max_seconds and time.time() - t0 > max_seconds:
                summary["stopped_early"] = True
                break
            src = os.path.join(root, r["rel_path"])
            dst = os.path.join(dest_root, r["dst_rel"])
            status, action, reason = "done", "copied", ""
            try:
                if os.path.exists(dst):
                    same = os.path.getsize(dst) == r["size"]
                    action, reason = "skipped", "destination exists" + (" (same size)" if same else " (DIFFERENT size)")
                    existed += 1
                    if not same:
                        status = "failed"
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    part = dst + ".itemwiki-part"
                    shutil.copy2(src, part)
                    if os.path.getsize(part) != r["size"] or quick_hash(part, r["size"]) != quick_hash(src, r["size"]):
                        os.replace(part, dst + ".itemwiki-bad")
                        raise OSError("verification failed")
                    os.replace(part, dst)
                    done += 1
                    copied_bytes += r["size"]
            except OSError as e:
                status, action, reason = "failed", "failed", f"{type(e).__name__}: {e}"
                failed += 1
            log.writerow([now(), r["rel_path"], r["dst_rel"], action, reason, r["size"]])
            con.execute("UPDATE plan SET status=?, done_at=? WHERE file_id=?", (status, time.time(), r["file_id"]))
            if i % 200 == 0:
                con.commit(); fh.flush()
                print(f"\r  copied {done:,}/{len(copies):,} ({copied_bytes / 1e9:,.2f} GB)", end="",
                      file=sys.stderr, flush=True)
        print(file=sys.stderr)
    finally:
        con.commit()
        fh.close()
    remaining = con.execute(
        "SELECT COUNT(*) FROM plan WHERE root=? AND status='planned' AND action='copy'"
        + (" AND top_dir=?" if batch else "") + ("" if include_flagged else " AND flag=0"),
        [root] + ([batch] if batch else [])).fetchone()[0]
    summary.update(copied=done, copied_bytes=copied_bytes, existed=existed, failed=failed, remaining=remaining,
                   seconds=round(time.time() - t0, 1))
    return summary
