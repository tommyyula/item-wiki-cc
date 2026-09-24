"""itemwiki command line. Phase 0: scan / dedup / report / export. Read-only on your files."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, apply, catalog, dedup, planner, report, scanner

DEFAULT_DB = os.environ.get("ITEMWIKI_DB", str(Path.cwd() / "data" / "catalog.db"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="itemwiki", description="Local multimodal file indexer / organizer")
    p.add_argument("--db", default=DEFAULT_DB, help=f"catalog path (default: {DEFAULT_DB}; env ITEMWIKI_DB)")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="walk a folder and record metadata (reads no file content)")
    s.add_argument("root")
    s.add_argument("--exclude", action="append", default=[], help="extra directory name to skip (repeatable)")

    d = sub.add_parser("dedup", help="hash size-colliding files to find exact duplicates")
    d.add_argument("--root")
    d.add_argument("--hydrate", action="store_true", help="also read cloud-only placeholders (triggers download)")

    r = sub.add_parser("report", help="write a Markdown inventory report")
    r.add_argument("--root")
    r.add_argument("--out", default="-", help="output file (default stdout)")
    r.add_argument("--top", type=int, default=25)
    r.add_argument("--label", help="display name for the root in the report header")

    e = sub.add_parser("export", help="export the file list as CSV")
    e.add_argument("out")
    e.add_argument("--root")

    pl = sub.add_parser("plan", help="build a copy plan from a folder mapping (changes nothing on disk)")
    pl.add_argument("--root", required=True, help="scanned source root")
    pl.add_argument("--mapping", required=True, help="TSV: source_prefix<TAB>dest_prefix|SKIP[<TAB>FLAG[<TAB>note]]")
    pl.add_argument("--dest", help="destination root (used to detect files already there)")
    pl.add_argument("--out-dir", default="reports", help="where to write <name>.csv and <name>.md")
    pl.add_argument("--name", default="plan")

    ap = sub.add_parser("apply", help="execute one batch of the plan (dry-run unless --execute)")
    ap.add_argument("--root", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--batch", help="source top-level folder to run (omit = all batches)")
    ap.add_argument("--log", help="CSV journal to append to (required with --execute)")
    ap.add_argument("--execute", action="store_true", help="actually copy")
    ap.add_argument("--include-flagged", action="store_true", help="also copy rows whose rule is FLAGged")
    ap.add_argument("--max-seconds", type=float, help="stop after this long; rerun to resume")

    a = p.parse_args(argv)
    con = catalog.connect(a.db)
    prog = not a.quiet

    if a.cmd == "scan":
        res = scanner.scan(con, a.root, set(a.exclude), progress=prog)
        print(json.dumps(res, ensure_ascii=False))
    elif a.cmd == "dedup":
        res = dedup.run(con, os.path.abspath(a.root) if a.root else None, hydrate=a.hydrate, progress=prog)
        print(json.dumps(res))
    elif a.cmd == "report":
        md = report.build(con, a.root, top_n=a.top, label=a.label)
        if a.out == "-":
            sys.stdout.write(md)
        else:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(md, encoding="utf-8")
            print(f"wrote {a.out}")
    elif a.cmd == "plan":
        res = planner.build_plan(con, a.root, a.mapping, a.dest)
        csv_path, md_path = planner.write_outputs(con, a.root, a.out_dir, a.name)
        print(json.dumps(res))
        print(f"wrote {csv_path}\nwrote {md_path}")
    elif a.cmd == "apply":
        if a.execute and not a.log:
            p.error("--execute requires --log")
        res = apply.run(con, a.root, a.dest, a.batch, a.log, execute=a.execute,
                        include_flagged=a.include_flagged, max_seconds=a.max_seconds)
        print(json.dumps(res, ensure_ascii=False))
    elif a.cmd == "export":
        n = report.export_csv(con, a.out, a.root)
        print(f"wrote {n:,} rows to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
