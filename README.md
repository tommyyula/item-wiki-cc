# item-wiki-cc

A local tool for indexing and reorganizing a large personal file collection (~900 GB, mixed documents / images / video / audio, Chinese + English). Everything runs on your machine.

```
scan → extract → embed → index → plan → execute
  └──────── SQLite catalog (single source of truth) ────────┘
```

See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture and roadmap.

## Status: Phase 0 — inventory (stdlib only, read-only)

Phase 0 never modifies, moves, or deletes your files. It only reads metadata, plus file content for duplicate hashing.

| Command | What it does |
|---|---|
| `itemwiki scan <folder>` | Walks the tree and records path, size, mtime, kind, version family and archive-folder flag. Reruns are incremental. |
| `itemwiki dedup` | Finds exact duplicates in three stages: size → head+tail hash → full hash. It only reads files whose size matches another file's, and skips cloud-only placeholders unless you pass `--hydrate`. |
| `itemwiki report --out reports/x.md` | Writes a Markdown inventory: top-level folders, kinds, largest files, archive-style folders, duplicates, version families. |
| `itemwiki export files.csv` | Exports the full file list (UTF-8 with BOM, so Excel opens Chinese names correctly). |

## Phase 2 — plan and copy (copy-only, one batch at a time)

| Command | What it does |
|---|---|
| `itemwiki plan --root SRC --mapping map.tsv --dest DST --out-dir DIR --name X` | Gives every file one action: `copy` (with a destination), `skip` (with a reason) or `review`. Writes `X.csv` (full list) and `X.md` (summary). Changes nothing on disk. |
| `itemwiki apply --root SRC --dest DST --batch <top-folder>` | Dry run: shows what would be copied. |
| `… apply … --execute --log copy_log.csv [--max-seconds 150]` | Copies the batch and appends each file to the log. Resumable. |

**Mapping file** (TSV): `source_prefix  dest_prefix|SKIP  [FLAG]  [note]`. The longest matching source prefix wins, and the rest of the path is kept under `dest_prefix`. `FLAG` marks a proposed rule that isn't confirmed yet: `apply` holds those files back unless you pass `--include-flagged`. See [examples/mapping.example.tsv](examples/mapping.example.tsv).

**Skip rules, in order:**

1. Empty files.
2. `SKIP` rules in the mapping.
3. Files under Archive/Archieve/Achieve/OLD/backup-style folders.
4. Exact duplicates. One canonical copy is kept, preferring: a confirmed rule, a shallower path, then the newer file.
5. Older versions in the same folder. A file is skipped only when its version number (v3.5 < v3.5.3) and its date agree that it is older. Date-suffixed files (such as daily reports) are never auto-skipped.

**Executor safety:**

- Copy only; sources are never touched.
- Never overwrites an existing file.
- Copies to a `.itemwiki-part` file, verifies size and a head/tail hash, then renames it into place.
- Every copied and skipped file is written to the CSV log.

## Quick start

```bash
cd item-wiki-cc
PYTHONPATH=src python3 -m itemwiki scan ~/Dropbox/ITEM
PYTHONPATH=src python3 -m itemwiki dedup
PYTHONPATH=src python3 -m itemwiki report --out reports/ITEM.md
# or: pip install -e . && itemwiki scan ...
```

The catalog is stored at `./data/catalog.db` by default. Override it with `--db` or `ITEMWIKI_DB`. Both `data/` and `reports/` are git-ignored because they contain real file names.

> SQLite needs a real local filesystem. If the catalog sits on a network, FUSE or VM-shared mount, you may get `disk I/O error`. In that case, point `--db` at a local path.

## First run: `~/Dropbox/ITEM` (2026-09-24)

67,262 files, 304 GB. The scan took 11 s; dedup hashed 50k size-colliding files (about 86 GB) in about 4 min. It found 7,100 exact-duplicate groups (46 GB reclaimable) and 5,881 files (48.7 GB) inside Archive/old-style folders.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
