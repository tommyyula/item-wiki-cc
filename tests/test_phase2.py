import csv
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from itemwiki import apply, catalog, dedup, planner, scanner  # noqa: E402
from itemwiki.filetypes import version_key  # noqa: E402


def write(p: Path, data: bytes, mtime: float | None = None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))


MAPPING = """# source\tdest\tflag\tnote
Customers\tCustomer
Customers/01-Acme\tCustomer/Acme
Leads\tCustomer\tFLAG\tleads merged into Customer
Junk\tSKIP\t\ttemp stuff
"""


class VersionKeyTests(unittest.TestCase):
    def test_strict(self):
        self.assertEqual(version_key("Deck v3.5"), version_key("Deck_v3.5.3 (1)"))
        self.assertEqual(version_key("Deck final"), "deck")
        from itemwiki.filetypes import version_tuple
        self.assertEqual(version_tuple("Deck-EN-Full-v3.5.3 (1)"), (3, 5, 3))
        self.assertIsNone(version_tuple("DIEV_0709"))
        self.assertLess(version_tuple("x v3.5"), version_tuple("x v3.5.3"))
        # dates are NOT version markers for auto-skip
        self.assertNotEqual(version_key("Report 20240301"), version_key("Report 20240302"))


class PlanApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.src, self.dst = t / "ITEM", t / "Wiki"
        big = os.urandom(200_000)
        now = time.time()
        write(self.src / "Customers/01-Acme/quote.xlsx", b"q" * 100)
        write(self.src / "Customers/01-Acme/Deck v1.pptx", b"1" * 500, now - 1000)
        write(self.src / "Customers/01-Acme/Deck v2.pptx", b"2" * 600, now)
        write(self.src / "Customers/01-Acme/Old/ancient.pdf", b"o" * 50)
        # date and version disagree -> keep both
        write(self.src / "Customers/01-Acme/Intro v3.5.3.pptx", b"x" * 300, now - 500)
        write(self.src / "Customers/01-Acme/Intro v3.5.pptx", b"y" * 400, now)
        write(self.src / "Customers/02-Beta/contract.pdf", big)
        write(self.src / "Leads/Beta/contract.pdf", big)                  # dup of the Customers copy
        write(self.src / "Leads/Acme/quote.xlsx", b"z" * 100)             # same dest name as Customers/01-Acme? no: different folder
        write(self.src / "Leads/01-Acme/note.txt", b"n" * 10)
        write(self.src / "Junk/tmp.txt", b"j" * 10)
        write(self.src / "Misc/unknown.txt", b"u" * 10)
        write(self.src / "Customers/01-Acme/empty.txt", b"")
        write(self.src / "Customers/02-Beta/daily 20240301.xlsx", b"a" * 70)
        write(self.src / "Customers/02-Beta/daily 20240302.xlsx", b"b" * 70)
        # Something already at destination
        write(self.dst / "Customer/02-Beta/daily 20240301.xlsx", b"a" * 70)
        self.mapping = t / "mapping.tsv"
        self.mapping.write_text(MAPPING)
        self.con = catalog.connect(t / "c.db")
        scanner.scan(self.con, str(self.src), progress=False)
        dedup.run(self.con, progress=False)
        self.root = str(self.src)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def plan(self):
        planner.build_plan(self.con, self.root, str(self.mapping), str(self.dst))
        return {r["rel_path"]: dict(r) for r in self.con.execute("SELECT * FROM plan")}

    def test_plan(self):
        p = self.plan()
        self.assertEqual(p["Customers/01-Acme/quote.xlsx"]["dst_rel"], "Customer/Acme/quote.xlsx")
        self.assertEqual(p["Customers/01-Acme/Deck v2.pptx"]["action"], "copy")
        self.assertTrue(p["Customers/01-Acme/Deck v1.pptx"]["reason"].startswith("older-version-of: Deck v2"))
        self.assertTrue(p["Customers/01-Acme/Old/ancient.pdf"]["reason"].startswith("archive-folder"))
        self.assertEqual(p["Customers/01-Acme/Intro v3.5.3.pptx"]["action"], "copy")
        self.assertEqual(p["Customers/01-Acme/Intro v3.5.pptx"]["action"], "copy")
        self.assertEqual(p["Customers/01-Acme/empty.txt"]["reason"], "empty")
        self.assertTrue(p["Junk/tmp.txt"]["reason"].startswith("rule: Junk"))
        self.assertEqual(p["Misc/unknown.txt"]["action"], "review")
        # duplicate: unflagged Customers copy wins over flagged Leads copy
        self.assertEqual(p["Customers/02-Beta/contract.pdf"]["action"], "copy")
        self.assertEqual(p["Leads/Beta/contract.pdf"]["reason"], "duplicate-of: Customers/02-Beta/contract.pdf")
        # Leads rule is flagged
        self.assertEqual(p["Leads/Acme/quote.xlsx"]["flag"], 1)
        # dated snapshots are both kept; one already exists at destination
        self.assertEqual(p["Customers/02-Beta/daily 20240302.xlsx"]["action"], "copy")
        self.assertEqual(p["Customers/02-Beta/daily 20240301.xlsx"]["reason"], "at-destination (same size)")

    def test_collision_rename(self):
        p = self.plan()
        a = p["Customers/01-Acme/quote.xlsx"]["dst_rel"]
        b = p["Leads/Acme/quote.xlsx"]["dst_rel"]
        self.assertNotEqual(a.lower(), b.lower())
        self.assertEqual(b, "Customer/Acme/quote (from Leads).xlsx")

    def test_apply(self):
        self.plan()
        log = Path(self.tmp.name) / "log" / "copy_log.csv"
        dry = apply.run(self.con, self.root, str(self.dst), "Customers", None, execute=False)
        self.assertFalse((self.dst / "Customer/Acme").exists())
        self.assertEqual(dry["to_copy"], 6)   # quote, Deck v2, 2 Intro, contract, daily 0302

        res = apply.run(self.con, self.root, str(self.dst), "Customers", str(log), execute=True)
        self.assertEqual((res["copied"], res["failed"], res["remaining"]), (6, 0, 0))
        self.assertEqual((self.dst / "Customer/Acme/Deck v2.pptx").read_bytes(), b"2" * 600)
        self.assertFalse(list(self.dst.rglob("*.itemwiki-part")))
        self.assertTrue((self.src / "Customers/01-Acme/Deck v1.pptx").exists())   # source untouched

        # Flagged batch is held back without --include-flagged
        res = apply.run(self.con, self.root, str(self.dst), "Leads", str(log), execute=True)
        self.assertEqual((res["copied"], res["held_flagged"]), (0, 2))
        res = apply.run(self.con, self.root, str(self.dst), "Leads", str(log), execute=True, include_flagged=True)
        self.assertEqual(res["copied"], 2)

        with open(log, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        actions = {r["action"] for r in rows}
        self.assertEqual(actions, {"copied", "skipped"})
        # Re-running is a no-op
        res = apply.run(self.con, self.root, str(self.dst), "Customers", str(log), execute=True)
        self.assertEqual(res["to_copy"], 0)


if __name__ == "__main__":
    unittest.main()
