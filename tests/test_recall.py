# -*- coding: utf-8 -*-
"""Tests for skills/recall/recall.py: term matching, and the file line ranges it
attaches to timeline hits.

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


class TermMatchTest(unittest.TestCase):
    def test_short_latin_term_matches_whole_words_and_plural(self):
        text = "setup settings dataset reset set sets set-up set을 offset"
        self.assertEqual(recall.count_term(text, "set"), 4)  # set, sets, set(-up), set(을)
        self.assertEqual(recall.count_term("ai detail maintain ai-generated", "ai"), 2)
        self.assertEqual(recall.count_term("2026-08-22 2022 220", "22"), 1)

    def test_longer_and_korean_terms_stay_substrings(self):
        self.assertEqual(recall.count_term("fargate-spot fargatespot", "fargate"), 2)
        self.assertEqual(recall.count_term("autoscaling autoscaler", "autoscal"), 2)
        self.assertEqual(recall.count_term("게이트웨이 장애", "게이트웨"), 1)

    def test_generic_short_word_no_longer_outranks_the_topic(self):
        # "fargate" is common here and "set" is rare, so "set" weighs most. As a
        # substring, "setup"/"settings" counted as "set" and put the noise first.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        files = {"noise.md": "# Tooling\n\n" + "- setup settings dataset reset\n" * 20,
                 "fargate.md": "# Fargate scaling\n\n- set fargate autoscaling target\n"}
        for i in range(3):
            files["cost%d.md" % i] = "# Fargate cost %d\n\n- fargate autoscaling 비용\n" % i
        for name, body in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(body)
        with mock.patch.object(recall, "THREADS_DIR", d):
            hits = recall.search_threads(["set", "fargate", "autoscaling"], 10, {})
        names = [h["name"] for h in hits]
        self.assertEqual(names[0], "Fargate scaling")
        self.assertNotIn("Tooling", names)


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
