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
