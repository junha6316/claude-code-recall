# -*- coding: utf-8 -*-
"""Tests for the file line ranges skills/recall/recall.py attaches to timeline hits.

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "recall_skill_spans", os.path.join(ROOT, "skills", "recall", "recall.py"))
recall = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recall)


class BlockSpanTest(unittest.TestCase):
    def test_gives_first_and_last_file_line_per_block(self):
        # The H1 and the blank line after it belong to no block; the final newline adds no line.
        content = "# 2026-08-01\n\n## 10:00 a\nx\n\n## 10:15 b\ny\n"
        self.assertEqual(recall.block_spans(content), [(3, 5), (6, 7)])
        self.assertEqual(recall.block_spans(content.rstrip("\n")), [(3, 5), (6, 7)])

    def test_no_headings_gives_empty_list(self):
        self.assertEqual(recall.block_spans("# 2026-08-01\nfoo\n"), [])

    def test_none_when_block_count_differs_from_split_blocks(self):
        # Only splitlines() breaks on U+2028, so the '## ' after it starts a block in
        # split_blocks alone.
        content = "## a\nfoo ## b\n"
        self.assertEqual(len(recall.split_blocks(content)), 2)
        self.assertIsNone(recall.block_spans(content))


class TimelineHitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        p = mock.patch.object(recall, "TIMELINE_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def printed(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data.encode("utf-8"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            recall.print_timeline_hits(recall.search_timeline(["fargate"], 10), ["fargate"])
        return path, buf.getvalue()

    def test_crlf_file_gets_same_line_numbers_as_sed(self):
        path, out = self.printed(
            "2026-08-01.md",
            "# 2026-08-01\r\n\r\n## 10:00 a\r\nfargate cost\r\n\r\n## 10:15 b\r\nfargate spot\r\n")
        self.assertIn("    ↳ %s (lines 3-5)\n" % path, out)
        self.assertIn("    ↳ %s (lines 6-7)\n" % path, out)
        with open(path, "rb") as f:
            raw = f.read().split(b"\n")  # count lines the way sed does
        self.assertEqual(raw[3 - 1], "## 10:00 a\r".encode())
        self.assertEqual(raw[6 - 1], "## 10:15 b\r".encode())

    def test_prints_only_the_file_when_spans_do_not_line_up(self):
        path, out = self.printed("2026-08-02.md", "## a\nfargate ## b\nfargate\n")
        self.assertIn("    ↳ %s\n" % path, out)
        self.assertNotIn("(lines ", out)


if __name__ == "__main__":
    unittest.main()
