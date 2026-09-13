"""Arrival preparation: reshape a download without touching the original."""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from acq import prepare  # noqa: E402


def snapshot(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prep-")
        self.arrivals = os.path.join(self.tmp, "intake", "Manual")
        self.out = os.path.join(self.tmp, "intake", "Prepared")
        os.makedirs(self.arrivals)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def loose_images(self, name="Some Series v01", count=3):
        folder = os.path.join(self.arrivals, name)
        os.makedirs(folder)
        for i in range(1, count + 1):
            with open(os.path.join(folder, f"{i:03d}.jpg"), "wb") as fh:
                fh.write(f"page{i}".encode())
        return folder

    def zip_of_zips(self, name="Bundle.zip"):
        inner = os.path.join(self.tmp, "inner.zip")
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("Ch0001/001.jpg", b"img")
        path = os.path.join(self.arrivals, name)
        with zipfile.ZipFile(path, "w") as z:
            z.write(inner, "Some Series ch1.zip")
            z.write(inner, "Some Series ch2.zip")
        return path


class TestPlan(Base):
    def test_archive_of_chapter_folders_is_already_right(self):
        path = os.path.join(self.arrivals, "Ready.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Ch0001/001.jpg", b"img")
            z.writestr("Ch0002/001.jpg", b"img")
        decision = prepare.plan(path)
        self.assertEqual(decision["action"], prepare.READY)
        self.assertEqual(decision["chapter_dirs"], 2)

    def test_loose_images_are_grouped(self):
        decision = prepare.plan(self.loose_images())
        self.assertEqual(decision["action"], prepare.GROUP_IMAGES)
        self.assertEqual(decision["images"], 3)

    def test_packed_archives_are_extracted(self):
        decision = prepare.plan(self.zip_of_zips())
        self.assertEqual(decision["action"], prepare.EXTRACT_NESTED)
        self.assertEqual(decision["nested_archives"], 2)

    def test_rar_is_unsupported_not_a_crash(self):
        path = os.path.join(self.arrivals, "Thing.rar")
        with open(path, "wb") as fh:
            fh.write(b"Rar!\x1a\x07\x00")
        decision = prepare.plan(path)
        self.assertEqual(decision["action"], prepare.UNSUPPORTED)
        self.assertIn("RAR", decision["reason"])

    def test_unreadable_archive_is_reported(self):
        path = os.path.join(self.arrivals, "Broken.zip")
        with open(path, "wb") as fh:
            fh.write(b"not really a zip")
        decision = prepare.plan(path)
        self.assertEqual(decision["action"], prepare.UNSUPPORTED)
        self.assertIn("cannot be read", decision["reason"])


class TestApply(Base):
    def test_dry_run_writes_nothing(self):
        folder = self.loose_images()
        before = snapshot(self.arrivals)
        result = prepare.prepare(folder, self.out, apply=False)
        self.assertTrue(result["outputs"])
        self.assertFalse(result["applied"])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(snapshot(self.arrivals), before)

    def test_grouping_builds_chapter_folders_and_keeps_the_original(self):
        folder = self.loose_images(count=4)
        before = snapshot(self.arrivals)
        result = prepare.prepare(folder, self.out, apply=True)
        self.assertTrue(result["applied"])
        built = result["outputs"][0]["output"]
        self.assertTrue(os.path.isfile(built))
        with zipfile.ZipFile(built) as z:
            names = z.namelist()
        self.assertEqual(len(names), 4)
        self.assertTrue(all(n.startswith("Ch0001/") for n in names), names)
        self.assertEqual(snapshot(self.arrivals), before, "the arrival itself must be untouched")
        self.assertFalse(any(n.endswith(".partial") for n in os.listdir(self.out)))

    def test_chapter_number_comes_from_the_folder_name(self):
        folder = self.loose_images(name="Some Series ch 12")
        prepare.prepare(folder, self.out, apply=True)
        built = os.path.join(self.out, "Some Series ch 12.zip")
        with zipfile.ZipFile(built) as z:
            self.assertTrue(all(n.startswith("Ch0012/") for n in z.namelist()))

    def test_extraction_lifts_each_archive_out(self):
        bundle = self.zip_of_zips()
        before = snapshot(self.arrivals)
        result = prepare.prepare(bundle, self.out, apply=True)
        written = sorted(os.listdir(self.out))
        self.assertEqual(written, ["Some Series ch1.zip", "Some Series ch2.zip"])
        for name in written:
            with zipfile.ZipFile(os.path.join(self.out, name)) as z:
                self.assertEqual(z.namelist(), ["Ch0001/001.jpg"])
        self.assertEqual(snapshot(self.arrivals), before)
        self.assertTrue(all(o["written"] for o in result["outputs"]))

    def test_reapplying_does_not_overwrite(self):
        bundle = self.zip_of_zips()
        prepare.prepare(bundle, self.out, apply=True)
        first = snapshot(self.out)
        again = prepare.prepare(bundle, self.out, apply=True)
        self.assertEqual(snapshot(self.out), first)
        self.assertTrue(all(not o["written"] for o in again["outputs"]))

    def test_a_traversing_member_is_refused(self):
        """A member named ../x must never be written outside the target."""
        inner = os.path.join(self.tmp, "inner.zip")
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("Ch0001/001.jpg", b"img")
        hostile = os.path.join(self.arrivals, "Hostile.zip")
        with zipfile.ZipFile(hostile, "w") as z:
            z.write(inner, "../escaped.zip")
            z.write(inner, "fine.zip")
        prepare.prepare(hostile, self.out, apply=True)
        self.assertEqual(os.listdir(self.out), ["fine.zip"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "intake", "escaped.zip")))


if __name__ == "__main__":
    unittest.main()
