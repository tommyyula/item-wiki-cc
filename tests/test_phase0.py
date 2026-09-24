import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from itemwiki import catalog, dedup, report, scanner  # noqa: E402
from itemwiki.filetypes import family_key, is_archive_dir  # noqa: E402


def write(p: Path, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


class FiletypeTests(unittest.TestCase):
    def test_family_key(self):
        same = ["Plan_v3", "Plan v1.2", "Plan (1)", "Plan final", "Plan-最终版", "Plan 20240315", "Plan copy",
                "Plan_v2_final", "Plan 副本"]
        self.assertEqual({family_key(s) for s in same}, {"plan"})
        self.assertNotEqual(family_key("Plan A"), family_key("Plan B"))

    def test_archive_dir(self):
        for n in ["Archive", "Achieve", "Archieve", "Archived", "old", "OLD FILES", "Old Version", "旧", "2023 Archive", "backup_2022", "_Archive_OldVersions"]:
            self.assertTrue(is_archive_dir(n), n)
        for n in ["Gold", "Holdings", "Archivist Notes", "Projects", "Bold Ideas"]:
            self.assertFalse(is_archive_dir(n), n)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ITEM"
        big = os.urandom(300_000)
        write(self.root / "Sales/deck_v1.pptx", b"a" * 1000)
        write(self.root / "Sales/deck_v2.pptx", b"b" * 1000)            # same size, different content
        write(self.root / "Sales/contract.pdf", big)
        write(self.root / "Archive/contract.pdf", big)                   # exact dup
        write(self.root / "Tech/sub/contract copy.pdf", big)             # exact dup
        write(self.root / "Tech/sub/photo.jpg", os.urandom(5000))
        write(self.root / "Tech/.DS_Store", b"x")                        # noise
        write(self.root / "node_modules/x.js", b"x")                     # excluded dir
        write(self.root / "readme.md", b"")                              # empty, at root
        self.con = catalog.connect(Path(self.tmp.name) / "c.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_scan_dedup_report_incremental(self):
        res = scanner.scan(self.con, str(self.root), progress=False)
        self.assertEqual(res["n_files"], 7)
        self.assertEqual(res["n_new"], 7)

        d = dedup.run(self.con, progress=False)
        self.assertEqual(d["quick_hashed"], 5)       # 2 pptx + 3 pdf share sizes
        self.assertEqual(d["full_hashed"], 3)        # only the pdfs share a quick hash
        groups = dedup.duplicate_groups(self.con)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0][2]), 3)

        r = self.con.execute("SELECT in_archive_dir, top_dir FROM files WHERE rel_path=?",
                             (os.path.join("Archive", "contract.pdf"),)).fetchone()
        self.assertEqual((r[0], r[1]), (1, "Archive"))

        md = report.build(self.con)
        for s in ["## Summary", "## Top-level folders", "Exact duplicates", "deck.pptx", "Archive"]:
            self.assertIn(s, md)

        # Incremental: modify one file, delete one, add one.
        time.sleep(0.01)
        write(self.root / "Sales/deck_v2.pptx", b"c" * 2000)
        (self.root / "Tech/sub/photo.jpg").unlink()
        write(self.root / "Tech/new.txt", b"hi")
        res = scanner.scan(self.con, str(self.root), progress=False)
        self.assertEqual((res["n_new"], res["n_changed"], res["n_missing"]), (1, 1, 1))
        h = self.con.execute("SELECT quick_hash FROM files WHERE name='deck_v2.pptx'").fetchone()[0]
        self.assertIsNone(h)  # hash invalidated on change

        out = Path(self.tmp.name) / "f.csv"
        self.assertEqual(report.export_csv(self.con, str(out)), 7)


if __name__ == "__main__":
    unittest.main()
