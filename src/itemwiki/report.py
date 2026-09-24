"""Markdown inventory report built purely from the catalog."""
from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
from collections import Counter, defaultdict

from .dedup import duplicate_groups
from .filetypes import VERSIONED_EXTS, is_archive_dir

BATCH_SERIES_MIN = 12


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return str(n)


def day(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _table(headers, rows) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(esc(str(c)) for c in r) + " |" for r in rows]
    return out


def latest_root(con: sqlite3.Connection) -> str | None:
    r = con.execute("SELECT root FROM scans WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return r["root"] if r else None


def build(con: sqlite3.Connection, root: str | None = None, top_n: int = 25, label: str | None = None) -> str:
    root = root or latest_root(con)
    if not root:
        return "No scans in catalog yet. Run `itemwiki scan <folder>` first.\n"
    root = os.path.abspath(root)
    files = [dict(r) for r in con.execute("SELECT * FROM files WHERE root=? AND status='present'", (root,))]
    scan = con.execute("SELECT * FROM scans WHERE root=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (root,)).fetchone()
    total = sum(f["size"] for f in files) or 1
    L: list[str] = []
    L += [f"# Inventory report — `{label or root}`", "",
          f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} · last scan #{scan['id']} on {day(scan['finished_at'])} "
          f"({scan['finished_at'] - scan['started_at']:.0f}s)", ""]

    # --- Summary
    cloud = [f for f in files if f["cloud_only"]]
    empty = [f for f in files if f["size"] == 0]
    arch = [f for f in files if f["in_archive_dir"]]
    groups = duplicate_groups(con, root)
    wasted = sum(size * (len(p) - 1) for _, size, p in groups)
    hashed_candidates = con.execute(
        "SELECT COUNT(*) FROM files WHERE root=? AND status='present' AND quick_hash IS NOT NULL", (root,)).fetchone()[0]
    n_dirs = len({f["parent"] for f in files})
    L += ["## Summary", ""]
    L += _table(["Metric", "Value"], [
        ("Files", f"{len(files):,}"),
        ("Folders containing files", f"{n_dirs:,}"),
        ("Total size", human(total)),
        ("Files inside Archive/old/backup-style folders", f"{len(arch):,} ({human(sum(f['size'] for f in arch))})"),
        ("Exact-duplicate groups", f"{len(groups):,} — reclaimable {human(wasted)}"
         + ("" if hashed_candidates else " (run `itemwiki dedup` first)")),
        ("Cloud-only placeholders (not on disk)", f"{len(cloud):,} ({human(sum(f['size'] for f in cloud))})"),
        ("Empty files", f"{len(empty):,}"),
        ("Scan errors", f"{scan['n_errors']:,}"),
    ])
    L.append("")

    # --- Top-level folders
    by_top: dict[str, list] = defaultdict(list)
    for f in files:
        by_top[f["top_dir"] or "(files at root)"].append(f)
    rows = []
    for name, fs in sorted(by_top.items(), key=lambda kv: -sum(f["size"] for f in kv[1])):
        sz = sum(f["size"] for f in fs)
        kinds = Counter()
        for f in fs:
            kinds[f["kind"]] += f["size"]
        mix = ", ".join(f"{k} {v * 100 / (sz or 1):.0f}%" for k, v in kinds.most_common(3))
        flag = "archive?" if is_archive_dir(name) else ""
        rows.append((name, f"{len(fs):,}", human(sz), f"{sz * 100 / total:.1f}%", mix,
                     day(min(f["mtime"] for f in fs)) + " → " + day(max(f["mtime"] for f in fs)), flag))
    L += ["## Top-level folders", ""]
    L += _table(["Folder", "Files", "Size", "% size", "Content mix (by size)", "Modified range", "Flag"], rows)
    L.append("")

    # --- Second-level folders (largest)
    by_second: dict[str, list] = defaultdict(list)
    for f in files:
        parts = f["rel_path"].split(os.sep)
        if len(parts) > 2:
            by_second[os.sep.join(parts[:2])].append(f)
    rows = sorted(((k, len(v), sum(f["size"] for f in v), max(f["mtime"] for f in v)) for k, v in by_second.items()),
                  key=lambda r: -r[2])[:top_n]
    L += [f"## Largest second-level folders (top {top_n})", ""]
    L += _table(["Folder", "Files", "Size", "Last modified"], [(k, f"{n:,}", human(s), day(m)) for k, n, s, m in rows])
    L.append("")

    # --- By kind / extension
    kind_c, kind_n = Counter(), Counter()
    ext_c, ext_n = Counter(), Counter()
    for f in files:
        kind_c[f["kind"]] += f["size"]; kind_n[f["kind"]] += 1
        e = f["ext"] or "(none)"
        ext_c[e] += f["size"]; ext_n[e] += 1
    L += ["## By kind", ""]
    L += _table(["Kind", "Files", "Size", "% size"],
                [(k, f"{kind_n[k]:,}", human(s), f"{s * 100 / total:.1f}%") for k, s in kind_c.most_common()])
    L += ["", f"## Top extensions by count (top {top_n})", ""]
    L += _table(["Ext", "Files", "Size"], [(e, f"{n:,}", human(ext_c[e])) for e, n in ext_n.most_common(top_n)])
    L.append("")

    # --- Largest files
    L += [f"## Largest files (top {top_n})", ""]
    L += _table(["File", "Size", "Modified"],
                [(f["rel_path"], human(f["size"]), day(f["mtime"]))
                 for f in sorted(files, key=lambda f: -f["size"])[:top_n]])
    L.append("")

    # --- Archive-style folders
    arch_dirs: dict[str, list] = defaultdict(list)
    for f in arch:
        parts = f["rel_path"].split(os.sep)[:-1]
        for i, p in enumerate(parts):
            if is_archive_dir(p):
                arch_dirs[os.sep.join(parts[:i + 1])].append(f); break
    if arch_dirs:
        L += ["## Archive / old / backup-style folders", "",
              "Candidates to skip. Matched by folder name (includes the common typo *Achieve*).", ""]
        L += _table(["Folder", "Files", "Size"],
                    [(k, f"{len(v):,}", human(sum(f['size'] for f in v)))
                     for k, v in sorted(arch_dirs.items(), key=lambda kv: -sum(f['size'] for f in kv[1]))[:top_n * 2]])
        L.append("")

    # --- Duplicates
    L += [f"## Exact duplicates (top {top_n} groups by reclaimable size)", ""]
    if groups:
        rows = []
        for _, size, paths in groups[:top_n]:
            rows.append((f"{len(paths)} × {human(size)}", human(size * (len(paths) - 1)),
                         "<br>".join(paths[:4]) + (f"<br>… +{len(paths) - 4} more" if len(paths) > 4 else "")))
        L += _table(["Copies", "Reclaimable", "Paths"], rows)
    else:
        L.append("_None found (or `itemwiki dedup` not run yet)._")
    L.append("")

    # --- Version families: same folder, same ext, names differ only by version/date/copy markers.
    fam: dict[tuple, list] = defaultdict(list)
    for f in files:
        if f["ext"] in VERSIONED_EXTS and f["family"]:
            fam[(f["parent"], f["family"], f["ext"])].append(f)
    fams, batch_series = [], 0
    for k, v in fam.items():
        if len({f["name"].lower() for f in v}) < 2:
            continue
        if len(v) > BATCH_SERIES_MIN:          # e.g. 600 generated DN_Import_*.xlsx: a data series, not versions
            batch_series += 1
            continue
        fams.append((k, v))
    # Rank by bytes held by the non-newest members (what skipping old versions would save).
    def stale_bytes(v):
        v = sorted(v, key=lambda f: -f["mtime"])
        return sum(f["size"] for f in v[1:])
    fams.sort(key=lambda kv: -stale_bytes(kv[1]))
    total_stale = sum(stale_bytes(v) for _, v in fams)
    L += [f"## Version families (top {top_n} by size of older versions)", "",
          f"{len(fams):,} groups of files in the same folder whose names differ only by version/date/copy markers; "
          f"older members hold {human(total_stale)}. Newest listed first — older ones are skip candidates. "
          f"({batch_series:,} large generated series with >{BATCH_SERIES_MIN} members excluded.)", ""]
    rows = []
    for (parent, key, ext), v in fams[:top_n]:
        v = sorted(v, key=lambda f: -f["mtime"])
        rows.append((f"{parent or '.'}/ **{key}.{ext}**", len(v), human(stale_bytes(v)),
                     "<br>".join(f"{f['name']} ({day(f['mtime'])}, {human(f['size'])})" for f in v[:5])
                     + (f"<br>… +{len(v) - 5} more" if len(v) > 5 else "")))
    L += _table(["Folder / family", "Files", "Older versions", "Members (newest first)"], rows) if rows else ["_None._"]
    L.append("")

    errs = con.execute("SELECT path, error FROM errors WHERE scan_id=? LIMIT 20", (scan["id"],)).fetchall()
    if errs:
        L += ["## Scan errors (first 20)", ""]
        L += _table(["Path", "Error"], [(e["path"], e["error"]) for e in errs])
        L.append("")
    return "\n".join(L)


def export_csv(con: sqlite3.Connection, out_path: str, root: str | None = None) -> int:
    root = os.path.abspath(root) if root else latest_root(con)
    cols = ["rel_path", "top_dir", "name", "ext", "kind", "size", "mtime", "cloud_only", "in_archive_dir",
            "family", "full_hash"]
    rows = con.execute(f"SELECT {', '.join(cols)} FROM files WHERE root=? AND status='present' ORDER BY rel_path",
                       (root,)).fetchall()
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel reads Chinese correctly
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            r = list(r)
            r[6] = dt.datetime.fromtimestamp(r[6]).strftime("%Y-%m-%d %H:%M")
            w.writerow(r)
    return len(rows)
