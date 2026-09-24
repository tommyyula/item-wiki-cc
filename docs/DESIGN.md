# Design

## Goal

Index a large local collection of mixed-media files, then reorganize it into a new folder structure. The tool proposes a plan, a human reviews it, and every step can be undone. Everything runs locally.

## Why not turbopuffer

turbopuffer is a storage and retrieval engine: vector search plus full-text search, with data on object storage. In this tool it would be only the **Index** layer. The closest open-source, local, embedded equivalent is **LanceDB**, which covers vector, FTS and hybrid search, stores data as columnar files on disk, needs no server, and can store blobs.

## Layers

| # | Layer | Responsibility | Tech |
|---|---|---|---|
| 1 | **Scanner** | Walk the tree, stat files, incremental re-scan, staged dedup | stdlib, SQLite |
| 2 | **Extractor** | Per-type plugins: docs → Markdown chunks; images → EXIF + optional caption; audio/video → ffmpeg keyframes + whisper transcript; archives/code → manifest only | Docling / markitdown, ffmpeg, whisper |
| 3 | **Embedder** | Multilingual text embeddings; image embeddings in a shared image-text space | bge-m3 / Qwen3-Embedding; SigLIP2 / jina-clip-v2 via Ollama or MLX |
| 4 | **Index** | Tables `files` and `chunks`, hybrid search | LanceDB |
| 5 | **Planner** | Target taxonomy in `taxonomy.yaml`. Classification cascade: rules → embedding similarity to folder prototypes → LLM for low-confidence cases. Output: plan rows `(src, dst, confidence, reason)` | YAML, LLM |
| 6 | **Executor** | Dry-run by default. Copy → verify hash → journal → (optionally) remove source. Undo from the journal. Never deletes without confirmation. | stdlib |

The SQLite catalog is the source of truth for file identity and state. LanceDB holds only derived data (chunks and vectors) and can be rebuilt from the catalog.

## Roadmap

- **Phase 0 (done):** scanner, dedup, inventory report, CSV export.
- **Phase 1:** text extraction for PDF / Office / Markdown, text embeddings, LanceDB, `itemwiki search "..."`.
- **Phase 2:** `taxonomy.yaml`, planner, review UI (Streamlit), executor with journal and undo.
- **Phase 3:** images, video and audio.

## Design notes

- **Cloud placeholders.** Dropbox / iCloud "online-only" files have `st_blocks == 0`. Reading them forces a download, so dedup and extraction skip them unless `--hydrate` is passed.
- **Dedup cost.** Only size-colliding files are read, and only quick-hash collisions are read in full. On a typical collection that is a small fraction of total bytes.
- **Version families.** File stems are normalized by stripping `v2`, `(1)`, `final`, `最终版`, `副本`, dates and similar markers. Files that share a family key are candidate old versions for the planner to skip.
- **Archive folders.** Folder names such as `Archive`, `Achieve` (a common typo), `old`, `backup`, `旧`, `归档` flag everything beneath them.
- **Privacy.** Catalogs and reports contain real file names, so they are git-ignored.
