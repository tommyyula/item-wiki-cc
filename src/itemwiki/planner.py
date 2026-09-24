"""Phase 2 planner: turn the catalog + a folder mapping into a reviewable copy plan.

Every present file gets exactly one action:
  copy    -> dst_rel is where it will go (flag=1 means its mapping rule still needs human confirmation)
  skip    -> reason says why (empty / rule / archive-folder / duplicate-of / older-version-of / at-destination)
  review  -> no rule matched, or the destination holds a different file with the same name

Nothing on disk is changed here. See apply.py for execution.
"""
from __future__ import annotations

import csv
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass

from .filetypes import is_archive_dir, version_key, version_tuple

SKIP = "SKIP"

PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan (
    file_id   INTEGER PRIMARY KEY,
    root      TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    top_dir   TEXT NOT NULL,
    size      INTEGER NOT NULL,
    action    TEXT NOT NULL,          -- copy | skip | review
    reason    TEXT,
    rule      TEXT,                   -- source prefix of the mapping rule applied
    rule_dst  TEXT,                   -- destination prefix of that rule
    flag      INTEGER DEFAULT 0,      -- 1 = rule marked as needing confirmation
    dst_rel   TEXT,                   -- destination path relative to dest root
    status    TEXT DEFAULT 'planned', -- planned | done | failed
    done_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_plan_top ON plan(root, top_dir, action);
"""


@dataclass
class Rule:
    src: str       # source prefix (folder path relative to root, '/'-separated)
    dst: str       # destination prefix, or SKIP
    flag: bool
    note: str


def load_mapping(path: str) -> list[Rule]:
    rules = []
    with open(path, encoding="utf-8-sig") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = [c.strip() for c in line.rstrip("\n").split("\t")]
            if len(cols) < 2 or not cols[0]:
                raise ValueError(f"{path}:{n}: need at least 'source<TAB>dest'")
            flag = len(cols) > 2 and cols[2].upper() in ("FLAG", "Y", "YES", "1", "?")
            rules.append(Rule(cols[0].strip("/"), cols[1].strip("/"), flag, cols[3] if len(cols) > 3 else ""))
    return rules


class Mapper:
    """Longest-prefix match on whole path components."""

    def __init__(self, rules: list[Rule]):
        self.by_src = {r.src: r for r in rules}

    def match(self, rel_dir_parts: list[str]) -> Rule | None:
        for i in range(len(rel_dir_parts), 0, -1):
            r = self.by_src.get("/".join(rel_dir_parts[:i]))
            if r:
                return r
        return None

    def dest(self, rule: Rule, rel_path: str) -> str:
        rest = rel_path[len(rule.src):].lstrip("/")
        return f"{rule.dst}/{rest}" if rule.dst else rest


def _archive_ancestor(parts: list[str]) -> str | None:
    for i, p in enumerate(parts):
        if is_archive_dir(p):
            return "/".join(parts[:i + 1])
    return None


def build_plan(con: sqlite3.Connection, root: str, mapping_path: str, dest_root: str | None = None) -> dict:
    con.executescript(PLAN_SCHEMA)
    root = os.path.abspath(root)
    mapper = Mapper(load_mapping(mapping_path))
    files = [dict(r) for r in con.execute(
        "SELECT id, rel_path, top_dir, name, ext, size, mtime, full_hash FROM files WHERE root=? AND status='present'",
        (root,))]
    if not files:
        raise SystemExit(f"no files in catalog for {root}; run `itemwiki scan` first")

    plan: dict[int, dict] = {}

    def put(f, action, reason=None, rule=None, dst=None):
        plan[f["id"]] = dict(file_id=f["id"], root=root, rel_path=f["rel_path"], top_dir=f["top_dir"],
                             size=f["size"], action=action, reason=reason, rule=rule.src if rule else None,
                             rule_dst=rule.dst if rule else None,
                             flag=int(bool(rule and rule.flag)), dst_rel=dst)

    # Pass 1: structural rules (empty / mapping / archive / unmapped)
    for f in files:
        parts = f["rel_path"].split("/")
        rule = mapper.match(parts[:-1])
        if f["size"] == 0:
            put(f, "skip", "empty", rule)
        elif rule and rule.dst == SKIP:
            put(f, "skip", f"rule: {rule.src}" + (f" ({rule.note})" if rule.note else ""), rule)
        elif (arch := _archive_ancestor(parts[:-1])):
            put(f, "skip", f"archive-folder: {arch}", rule)
        elif rule is None:
            put(f, "review", "unmapped folder", None)
        else:
            put(f, "copy", None, rule, mapper.dest(rule, f["rel_path"]))

    by_id = {f["id"]: f for f in files}

    # Pass 2: exact duplicates -> keep one canonical copy
    groups: dict[str, list] = defaultdict(list)
    for f in files:
        if f["full_hash"] and plan[f["id"]]["action"] != "skip":
            groups[f["full_hash"]].append(f)
    n_dup = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        # Prefer: mapped copy over review, confirmed rule over flagged, shallower path, newer, alphabetical.
        members.sort(key=lambda f: (plan[f["id"]]["action"] != "copy", plan[f["id"]]["flag"],
                                    f["rel_path"].count("/"), -f["mtime"], f["rel_path"]))
        keep = members[0]
        for f in members[1:]:
            p = plan[f["id"]]
            p.update(action="skip", reason=f"duplicate-of: {keep['rel_path']}", dst_rel=None)
            n_dup += 1

    # Pass 3: older versions in the same folder (explicit version/copy markers only, never date suffixes)
    fams: dict[tuple, list] = defaultdict(list)
    for f in files:
        if plan[f["id"]]["action"] == "copy" and f["ext"]:
            parent = f["rel_path"].rsplit("/", 1)[0] if "/" in f["rel_path"] else ""
            stem = f["name"][: -(len(f["ext"]) + 1)]
            fams[(parent, version_key(stem), f["ext"])].append(f)
    n_old = 0
    for members in fams.values():
        if len(members) < 2 or len({m["name"].lower() for m in members}) < 2:
            continue
        members.sort(key=lambda f: -f["mtime"])
        newest = members[0]
        v_new = version_tuple(newest["name"][: -(len(newest["ext"]) + 1)])
        for f in members[1:]:
            v = version_tuple(f["name"][: -(len(f["ext"]) + 1)])
            # Skip only when date and version number agree it is older; otherwise keep both.
            if (v is None and v_new is None) or (v is not None and v_new is not None and v < v_new):
                plan[f["id"]].update(action="skip", reason=f"older-version-of: {newest['name']}", dst_rel=None)
                n_old += 1

    # Pass 4: destination collisions between different sources, then what's already at the destination
    seen: dict[str, int] = {}
    # Deterministic: confirmed rules first, then path order, so the "main" source keeps the plain name.
    for p in sorted(plan.values(), key=lambda p: (p["flag"], p["rel_path"])):
        fid = p["file_id"]
        if p["action"] != "copy":
            continue
        key = p["dst_rel"].lower()   # macOS volumes are usually case-insensitive
        if key in seen:
            stem, dot, ext = p["dst_rel"].rpartition(".")
            if not dot or "/" in ext:
                stem, ext = p["dst_rel"], ""
            p["dst_rel"] = f"{stem} (from {p['top_dir']})" + (f".{ext}" if ext else "")
            key = p["dst_rel"].lower()
            n = 2
            while key in seen:
                p["dst_rel"] = f"{stem} (from {p['top_dir']} {n})" + (f".{ext}" if ext else "")
                key = p["dst_rel"].lower(); n += 1
        seen[key] = fid
    if dest_root:
        for p in plan.values():
            if p["action"] != "copy":
                continue
            target = os.path.join(dest_root, p["dst_rel"])
            try:
                st = os.stat(target)
            except FileNotFoundError:
                continue
            if st.st_size == by_id[p["file_id"]]["size"]:
                p.update(action="skip", reason="at-destination (same size)")
            else:
                p.update(action="review", reason="destination has a different file with this name")

    con.execute("DELETE FROM plan WHERE root=?", (root,))
    con.executemany(
        """INSERT INTO plan(file_id, root, rel_path, top_dir, size, action, reason, rule, rule_dst, flag, dst_rel)
           VALUES (:file_id, :root, :rel_path, :top_dir, :size, :action, :reason, :rule, :rule_dst, :flag, :dst_rel)""",
        list(plan.values()))
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('plan_built_at', ?)", (str(time.time()),))
    con.commit()
    counts = defaultdict(int)
    for p in plan.values():
        counts[p["action"]] += 1
    return dict(files=len(plan), duplicates_skipped=n_dup, older_versions_skipped=n_old, **counts)


# ---------------------------------------------------------------- reporting

def _h(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:,.0f} {u}" if u == "B" else f"{n:,.1f} {u}"
        n /= 1024
    return str(n)


def _reason_kind(reason: str | None) -> str:
    return (reason or "").split(":")[0].split(" (")[0] or "-"


def _tbl(headers, rows):
    esc = lambda s: str(s).replace("|", "\\|")
    return ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"] + \
           ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]


def write_outputs(con: sqlite3.Connection, root: str, out_dir: str, name: str = "plan") -> tuple[str, str]:
    root = os.path.abspath(root)
    os.makedirs(out_dir, exist_ok=True)
    rows = [dict(r) for r in con.execute("SELECT * FROM plan WHERE root=? ORDER BY rel_path", (root,))]
    csv_path = os.path.join(out_dir, f"{name}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["batch", "action", "flag", "size_bytes", "source", "destination", "reason", "rule"])
        for r in rows:
            w.writerow([r["top_dir"], r["action"], "FLAG" if r["flag"] else "", r["size"], r["rel_path"],
                        r["dst_rel"] or "", r["reason"] or "", r["rule"] or ""])

    tot = defaultdict(lambda: [0, 0])
    for r in rows:
        k = r["action"] if r["action"] != "skip" else f"skip: {_reason_kind(r['reason'])}"
        tot[k][0] += 1; tot[k][1] += r["size"]
    L = [f"# Copy plan — {name}", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')} · {len(rows):,} files. "
         "Nothing has been copied yet. Full detail: the CSV next to this file (filter by `batch`).", "",
         "## Totals", ""]
    L += _tbl(["Action", "Files", "Size"], [(k, f"{n:,}", _h(s)) for k, (n, s) in
                                             sorted(tot.items(), key=lambda kv: (kv[0] != 'copy', -kv[1][1]))])

    # Per batch (source top-level folder)
    bt: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in rows:
        k = r["action"] if r["action"] != "skip" else "skip"
        if r["action"] == "copy" and r["flag"]:
            k = "copy (flagged)"
        bt[r["top_dir"] or "(root)"][k][0] += 1
        bt[r["top_dir"] or "(root)"][k][1] += r["size"]
    L += ["", "## Batches (one per source top-level folder)", ""]
    cell = lambda d, k: f"{d[k][0]:,} / {_h(d[k][1])}" if d[k][0] else "—"
    L += _tbl(["Batch", "Copy", "Copy (flagged rule)", "Skip", "Review"],
              [(b, cell(d, "copy"), cell(d, "copy (flagged)"), cell(d, "skip"), cell(d, "review"))
               for b, d in sorted(bt.items(), key=lambda kv: -sum(v[1] for v in kv[1].values()))])

    # Rules: where each source folder goes
    rt = defaultdict(lambda: [0, 0, None, 0])
    for r in rows:
        if r["rule"]:
            x = rt[r["rule"]]
            x[2] = r["rule_dst"]; x[3] |= r["flag"]
            if r["action"] == "copy":
                x[0] += 1; x[1] += r["size"]
    flagged = [(k, v) for k, v in rt.items() if v[3] and v[0]]
    if flagged:
        L += ["", "## Rules that need your confirmation (FLAG)", "",
              "These have a proposed destination but were marked uncertain in the mapping.", ""]
        L += _tbl(["Source folder", "→ Proposed destination", "Files to copy", "Size"],
                  [(k, (v[2] or "").rstrip("/"), f"{v[0]:,}", _h(v[1]))
                   for k, v in sorted(flagged, key=lambda kv: -kv[1][1])])

    unm = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["action"] == "review":
            parent = r["rel_path"].rsplit("/", 1)[0] if "/" in r["rel_path"] else "(root)"
            key = "/".join(parent.split("/")[:2])
            unm[f"{key} — {r['reason']}"][0] += 1; unm[f"{key} — {r['reason']}"][1] += r["size"]
    if unm:
        L += ["", "## Needs a decision (review)", ""]
        L += _tbl(["Folder — why", "Files", "Size"],
                  [(k, f"{n:,}", _h(s)) for k, (n, s) in sorted(unm.items(), key=lambda kv: -kv[1][1])[:60]])

    # Destination tree (depth 3)
    dt = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["action"] == "copy":
            k = "/".join(r["dst_rel"].split("/")[:-1][:3]) or "(dest root)"
            dt[k][0] += 1; dt[k][1] += r["size"]
    L += ["", "## Destination folders (top 60 by size)", ""]
    L += _tbl(["Destination", "Files", "Size"],
              [(k, f"{n:,}", _h(s)) for k, (n, s) in sorted(dt.items(), key=lambda kv: -kv[1][1])[:60]])

    # Biggest skips, so the user can sanity check that nothing important is dropped
    for kind, title in (("older-version-of", "Largest older-version skips"),
                        ("duplicate-of", "Largest duplicate skips"),
                        ("archive-folder", "Largest archive-folder skips")):
        sk = [r for r in rows if r["action"] == "skip" and (r["reason"] or "").startswith(kind)]
        if sk:
            sk.sort(key=lambda r: -r["size"])
            L += ["", f"## {title} (top 20 of {len(sk):,}, {_h(sum(r['size'] for r in sk))})", ""]
            L += _tbl(["Skipped file", "Size", "Reason"], [(r["rel_path"], _h(r["size"]), r["reason"]) for r in sk[:20]])

    md_path = os.path.join(out_dir, f"{name}.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return csv_path, md_path
