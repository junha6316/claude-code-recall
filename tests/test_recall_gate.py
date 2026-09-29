# -*- coding: utf-8 -*-
"""Tests for scripts/recall-gate.py output shaping and the Jev relevance filter.

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load("recall_gate", os.path.join("scripts", "recall-gate.py"))
# recall.py's own printers produce the fixtures, so the parser is tested
# against the real stdout format rather than a hand-written copy of it.
recall = _load("recall_skill", os.path.join("skills", "recall", "recall.py"))

TERMS = ["fargate", "cost"]
KWS = ["fargate", "cost"]
PROMPT = "지난번에 fargate cost 줄이던 거 뭐였지"
RERUN = "python3 %s %s" % (shlex.quote(gate.RECALL), shlex.quote("fargate cost"))


def recall_output(n_threads, n_timeline, lines_per=4, line_len=100, tied=False,
                  heading_len=0, thread_line_lens=None):
    threads = [{
        "name": "thread-%02d" % i,
        # Halving scores keep rank 2 well outside the 20% ambiguity margin.
        "w": 10.0 if tied else 100.0 / (2 ** i),
        "d": 2, "t": 5, "last_date": "2026-09-%02d" % (i + 1), "via": set(),
        "current_state": "state of thread %02d" % i if i % 2 else None,
        "lines": ["T%02d-%d " % (i, j) + "x" * (thread_line_lens[i] if thread_line_lens else line_len)
                  for j in range(lines_per)],
        "path": "/home/u/.claude/work-timeline/threads/thread-%02d.md" % i,
    } for i in range(n_threads)]
    timeline = [(1.0, 2, 3, "2026-08-%02d" % (i % 28 + 1),
                 "10:00 project-%03d %s" % (i, "h" * heading_len),
                 ["L%03d-%d " % (i, j) + "y" * line_len for j in range(lines_per)])
                for i in range(n_timeline)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        recall.print_thread_hits(threads, TERMS)
        recall.print_timeline_hits(timeline, TERMS)
    return buf.getvalue().strip()


def medium():
    """12 blocks, just over the cap: shaping kicks in, but 8 full blocks fit."""
    out = recall_output(6, 6, lines_per=4, line_len=180)
    assert gate.ulen(out) > gate.CONTEXT_BUDGET
    return out


def blocks_of(out):
    """(kind, text) of each result block, as the gate parses them."""
    return [(k, "\n".join(lines).rstrip("\n"))
            for k, lines in gate.parse_items(out) if k in ("thread", "timeline")]


def is_full(ctx, text):
    return text in ctx


def is_collapsed(ctx, kind, text):
    """Header (+ ↳ path for threads) present, the block's own content lines absent."""
    lines = text.split("\n")
    kept = [lines[0]] + ([lines[-1]] if kind == "thread" else [])
    body = lines[1:-1] if kind == "thread" else lines[1:]
    own = [ln for ln in body if ln.strip() != "[current state]"]  # shared by blocks
    return all(ln in ctx for ln in kept) and all(ln not in ctx for ln in own)


class JevMock:
    """Local stand-in for the TypeSafe endpoint; records every request."""

    def __init__(self, respond):
        self.requests = []
        self.respond = respond  # body dict -> (status, bytes)
        mock_ = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw.decode("utf-8"))
                mock_.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": body,
                })
                status, payload = mock_.respond(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1/systemone" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def scored(scores):
    """Mock responder answering each question id with scores.get(id, 0.0)."""
    def respond(body):
        answers = {qid: {"type": "noul", "noul": scores.get(qid, 0.0)}
                   for qid in body["questions"]}
        return 200, json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()
    return respond


def env_without_key(**extra):
    env = {k: v for k, v in os.environ.items() if k != gate.JEV_KEY_ENV}
    env.update(extra)
    return env


class TriggerTest(unittest.TestCase):
    def test_non_trigger_prompt_no_output(self):
        with mock.patch.object(gate.subprocess, "run") as run:
            self.assertIsNone(gate.run({"prompt": "add a dark mode toggle to settings"}))
            run.assert_not_called()

    def test_run_injects_capped_context(self):
        big = recall_output(15, 15, lines_per=8, line_len=180)
        self.assertGreater(gate.ulen(big), gate.CONTEXT_BUDGET)
        fake = mock.Mock(stdout=big + "\n")
        with mock.patch.dict(os.environ, env_without_key(), clear=True), \
                mock.patch.object(gate.subprocess, "run", return_value=fake):
            out = gate.run({"prompt": PROMPT})
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        self.assertIn("--- recall result shortened", ctx)

    def assert_skipped(self, prompt):
        with mock.patch.object(gate.subprocess, "run") as run:
            self.assertIsNone(gate.run({"prompt": prompt}))
            run.assert_not_called()

    def recall_query(self, prompt):
        """The keyword string the gate passes to recall.py for prompt."""
        with mock.patch.dict(os.environ, env_without_key(), clear=True), \
                mock.patch.object(gate.subprocess, "run",
                                  return_value=mock.Mock(stdout="")) as run:
            self.assertIsNotNone(gate.run({"prompt": prompt}))
        return run.call_args[0][0][-1]

    def test_internal_summary_prompt_skipped(self):
        # The plugin's own claude -p prompts quote user prompts, triggers included.
        self.assert_skipped("[work-timeline-internal]\nBelow are the work sessions"
                            "\n- 10:02 통화 음성 파일 형식 고민했던거 기억나?")

    def test_agent_message_skipped(self):
        self.assert_skipped('Another Claude session sent a message:\n'
                            '<agent-message from="a1addb8352149c265">\n'
                            '[Subagent hand-back] 전에 했던 작업 결과입니다.')

    def test_trigger_only_inside_paste_skipped(self):
        self.assert_skipped('<pasted_content id="c40b">\n지난번에 이어서 CTranslate2 '
                            '최적화를 한다.\n</pasted_content id="c40b">\n')
        # Cut off before its closing tag: still pasted text to the end.
        self.assert_skipped('크레딧을 줬대\n<pasted_content id="90d7">\n'
                            'as we discussed last time')

    def test_before_doing_is_not_a_recall_cue(self):
        self.assert_skipped("오케이, 작업하기 전에 확인해야되는거 있어?")
        self.assert_skipped("terraform apply 실행하기전에 검토 할거 있나")
        # A time before now still is.
        self.assertEqual(self.recall_query("며칠 전에 fargate 비용 확인"), "며칠 fargate 비용 확인")

    def test_paste_dropped_from_keywords(self):
        q = self.recall_query('<pasted_content id="64db">\nAWS access key 평문 노출\n'
                              '</pasted_content id="64db">\n\n faster pymysql 기억나?')
        self.assertEqual(q, "faster pymysql")

    def test_url_and_path_reduced_to_last_segment(self):
        kw = gate.extract_keywords
        self.assertEqual(kw("https://github.com/yplabs-ltd/engineering-handbook 이거 기억나?"),
                         ["engineering-handbook"])
        self.assertEqual(kw("https://github.com/o/r/pull/6119/ 이거 기억나?"), ["6119"])
        self.assertEqual(kw("~/Projects/connecting/docs/sendbird-message-archive-design.md "
                            "이거 기억나?"), ["sendbird-message-archive-design"])
        # Only a token that starts as a URL or path; a slash inside a word stays a split.
        self.assertEqual(kw("dev/qa env-on 기억나?"), ["dev", "qa", "env-on"])


class ShapeTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, env_without_key(), clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_small_result_unchanged(self):
        out = recall_output(2, 3)
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertTrue(ctx.startswith("[recall enforcement hook]"))
        self.assertTrue(ctx.endswith("--- recall result ---\n" + out))
        self.assertNotIn("shortened", ctx)

    def test_empty_result_unchanged(self):
        ctx = gate.shape("", PROMPT, KWS)
        self.assertTrue(ctx.endswith("--- recall result ---\n(no result)"))

    def test_under_budget_unchanged(self):
        # More blocks than the rank policy keeps, but the whole result fits:
        # nothing is collapsed.
        out = recall_output(5, 7, lines_per=2, line_len=40)
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertTrue(ctx.endswith("--- recall result ---\n" + out))
        self.assertNotIn("shortened", ctx)

    def test_emoji_counts_as_two_units(self):
        # Claude Code counts an emoji ("🧠 Daily Summary") as 2 units, len() as 1.
        # This result fits by len() but not by UTF-16 units, so it must be shaped.
        out = recall_output(2, 12, lines_per=4, line_len=100).replace("y" * 100, "🧠" * 100)
        head = gate.shape("", PROMPT, KWS)[:-len("(no result)")]
        self.assertLessEqual(len(head) + len(out), gate.CONTEXT_BUDGET)
        self.assertGreater(gate.ulen(head + out), gate.CONTEXT_BUDGET)
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        self.assertIn("shortened", ctx)

    def test_footer_counts_add_up(self):
        # A huge ambiguity note leaves no room: picked blocks that are left out
        # entirely must be counted once (omitted), not also as collapsed.
        out = recall_output(5, 0, lines_per=2, line_len=40, tied=True)
        start = out.index(gate.AMBIGUOUS_MARKER)
        out = out[:start] + gate.AMBIGUOUS_MARKER + " " + "n" * 8600 + out[start + len(gate.AMBIGUOUS_MARKER):]
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        counts = re.search(r"(\d+) of (\d+) results are shown in full", ctx)
        assert counts is not None, ctx
        full, total = map(int, counts.groups())
        m = re.search(r"(\d+) picked results are collapsed", ctx)
        collapsed = int(m.group(1)) if m else 0
        m = re.search(r"Omitted entirely \(not even a header fit\): (.*)\.", ctx)
        omitted = sum(int(x) for x in re.findall(r"\d+", m.group(1))) if m else 0
        self.assertEqual(total, 5)
        self.assertLessEqual(full + collapsed + omitted, total)

    def test_large_result_capped(self):
        out = recall_output(15, 15, lines_per=4, line_len=110)
        self.assertGreater(gate.ulen(out), gate.CONTEXT_BUDGET)
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        blocks = blocks_of(out)
        threads = [t for k, t in blocks if k == "thread"]
        timeline = [t for k, t in blocks if k == "timeline"]
        for t in threads[:3] + timeline[:5]:
            self.assertTrue(is_full(ctx, t), t)
        for t in threads[3:]:
            self.assertTrue(is_collapsed(ctx, "thread", t), t)
            self.assertIn("↳ ", t.split("\n")[-1])
        # Section headers stay, in their original order.
        self.assertLess(ctx.index("=== Work threads"), ctx.index("=== Timeline search results"))
        self.assertIn(RERUN, ctx)
        self.assertIn(gate.TIMELINE_DIR + "/<date>.md", ctx)
        self.assertIn("Collapsed entries show only their header line", ctx)

    def test_full_text_before_trailing_headers(self):
        # All 8 rank slots fit only if lower-ranked headers give way.
        out = recall_output(15, 15, lines_per=6, line_len=110)
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        blocks = blocks_of(out)
        threads = [t for k, t in blocks if k == "thread"]
        timeline = [t for k, t in blocks if k == "timeline"]
        for t in threads[:3] + timeline[:5]:
            self.assertTrue(is_full(ctx, t), t)
        self.assertIn("8 of 30 results are shown in full", ctx)
        self.assertRegex(ctx, r"Omitted entirely \(not even a header fit\): \d+ ")
        self.assertNotIn("picked results are collapsed", ctx)

    def test_rank_order_kept_when_budget_tight(self):
        # Thread 1 cannot fit in full after thread 0; the small thread 2 must
        # not jump ahead of it. The timeline section is ranked on its own.
        out = recall_output(4, 2, lines_per=1, line_len=40,
                            thread_line_lens=[4500, 4200, 100, 100])
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        blocks = blocks_of(out)
        threads = [t for k, t in blocks if k == "thread"]
        timeline = [t for k, t in blocks if k == "timeline"]
        self.assertTrue(is_full(ctx, threads[0]))
        for t in threads[1:]:
            self.assertTrue(is_collapsed(ctx, "thread", t), t)
        for t in timeline:
            self.assertTrue(is_full(ctx, t), t)
        self.assertIn("2 picked results are collapsed because their full text did not fit.", ctx)

    def test_single_block_over_budget(self):
        out = recall_output(1, 0, lines_per=1, thread_line_lens=[12000])
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        thread = blocks_of(out)[0][1]
        self.assertTrue(is_collapsed(ctx, "thread", thread))
        self.assertIn("0 of 1 results are shown in full", ctx)
        self.assertIn("1 picked results are collapsed because their full text did not fit.", ctx)
        self.assertIn(RERUN, ctx)

    def test_rerun_command_is_shell_quoted(self):
        kws = ["$HOME", "설정"]
        ctx = gate.shape(medium(), "$HOME 설정 전에 어떻게 했지?", kws)
        line = next(ln for ln in ctx.split("\n") if ln.startswith("For the full text run: "))
        cmd = line[len("For the full text run: "):line.index(" — ")]
        self.assertTrue(cmd.startswith("python3 "), cmd)
        # Let a real shell parse the arguments, so $HOME expansion would show.
        argv = subprocess.run(["sh", "-c", "printf '%s\\n' " + cmd[len("python3 "):]],
                              capture_output=True, text=True).stdout.split("\n")[:-1]
        self.assertEqual(argv, [gate.RECALL, "$HOME 설정"])

    def test_cap_holds_with_huge_keyword(self):
        # A pasted blob (token, hash) can become a keyword; head and footer echo it.
        kws = ["a" * 9000]
        ctx = gate.shape(medium(), PROMPT, kws)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)

    def test_overflow_drops_trailing_blocks(self):
        out = recall_output(15, 200, lines_per=2, line_len=50, heading_len=60)
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        blocks = blocks_of(out)
        self.assertIn(blocks[0][1].split("\n")[0], ctx)
        self.assertNotIn(blocks[-1][1].split("\n")[0], ctx)
        self.assertRegex(ctx, r"Omitted entirely \(not even a header fit\): \d+ ")
        self.assertIn(RERUN, ctx)

    def test_ambiguity_note_kept_verbatim(self):
        out = recall_output(15, 15, lines_per=6, line_len=110, tied=True)
        start = out.index(gate.AMBIGUOUS_MARKER)
        note = out[start:out.index("\n\n", start)]
        self.assertIn("  4. thread-03", note)
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        self.assertIn(note, ctx)
        self.assertIn("AskUserQuestion", ctx)
        # All 4 candidates the note offers get full text, not just the first 3.
        threads = [t for k, t in blocks_of(out) if k == "thread"]
        for t in threads[:4]:
            self.assertTrue(is_full(ctx, t), t)
        self.assertIn("up to the first 4 threads", ctx)


class JevTest(unittest.TestCase):
    def use(self, respond=None, url=None, key="test-key"):
        """Point the gate at a mock (or a raw url) with the recall key set."""
        mock_ = None
        if respond is not None:
            mock_ = JevMock(respond)
            self.addCleanup(mock_.close)
            url = mock_.url
        p = mock.patch.object(gate, "JEV_URL", url)
        p.start()
        self.addCleanup(p.stop)
        env = env_without_key(**({gate.JEV_KEY_ENV: key} if key else {}))
        env["TYPESAFE_API_KEY"] = "generic-key-must-be-ignored"
        e = mock.patch.dict(os.environ, env, clear=True)
        e.start()
        self.addCleanup(e.stop)
        return mock_

    def assert_rank_order(self, ctx, out):
        threads = [t for k, t in blocks_of(out) if k == "thread"]
        for t in threads[:3]:
            self.assertTrue(is_full(ctx, t), t)
        for t in threads[3:]:
            self.assertTrue(is_collapsed(ctx, "thread", t), t)
        self.assertIn("picked by rank order", ctx)

    def test_request_shape_and_selection(self):
        jev = self.use(scored({"b1": 0.9, "b4": 0.8, "b5": 0.3, "b6": 0.29, "b8": 0.5}))
        out = medium()
        prompt = PROMPT + " " + "z" * 3000
        ctx = gate.shape(out, prompt, KWS)
        self.assertEqual(len(jev.requests), 1)
        req = jev.requests[0]
        self.assertEqual(req["path"], "/v1/systemone")
        self.assertEqual(req["authorization"], "Bearer test-key")
        self.assertEqual(req["content_type"], "application/json")
        body = req["body"]
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["state"], {"user_question": prompt[:2000]})
        blocks = blocks_of(out)
        self.assertEqual(sorted(body["questions"]), sorted("b%d" % n for n in range(len(blocks))))
        for n, (_, text) in enumerate(blocks):
            q = body["questions"]["b%d" % n]
            self.assertEqual(q["type"], "noul")
            self.assertEqual(q["instructions"]["passage"], text)
            self.assertEqual(q["instructions"]["question"], gate.JEV_QUESTION)
            self.assertEqual(set(q["criteria"]), {"true", "false"})
        # b0..b5 are threads, b6..b11 timeline hits; >= 0.3 gets full text.
        for n, (kind, text) in enumerate(blocks):
            if n in (1, 4, 5, 8):
                self.assertTrue(is_full(ctx, text), text)
            else:
                self.assertTrue(is_collapsed(ctx, kind, text), text)
        self.assertIn("4 of 12 results are shown in full, picked by Jev relevance", ctx)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)

    def test_jev_fills_by_score_when_budget_tight(self):
        # Room for three: b1-b3 outscore b0, so rank 1 (b0) is the one collapsed.
        self.use(scored({"b0": 0.35, "b1": 0.95, "b2": 0.9, "b3": 0.85}))
        p = mock.patch.object(gate, "CONTEXT_BUDGET", 4200)
        p.start()
        self.addCleanup(p.stop)
        out = medium()
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertLessEqual(gate.ulen(ctx), gate.CONTEXT_BUDGET)
        threads = [t for k, t in blocks_of(out) if k == "thread"]
        for t in threads[1:4]:
            self.assertTrue(is_full(ctx, t), t)
        self.assertTrue(is_collapsed(ctx, "thread", threads[0]))
        self.assertIn("3 of 12 results are shown in full, picked by Jev relevance", ctx)

    def test_zero_relevant_falls_back_to_rank(self):
        jev = self.use(scored({}))
        out = medium()
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertEqual(len(jev.requests), 1)
        self.assert_rank_order(ctx, out)
        self.assertIn("(Jev marked no result relevant; used rank order.)", ctx)

    def test_http_500_falls_back_to_rank(self):
        self.use(lambda body: (500, b'{"error": "boom"}'))
        out = medium()
        ctx = gate.shape(out, PROMPT, KWS)
        self.assert_rank_order(ctx, out)
        self.assertIn("(Jev relevance check failed: HTTP 500; used rank order.)", ctx)

    def test_missing_answers_falls_back_to_rank(self):
        self.use(lambda body: (200, b'{"model": "jev-1.13.0"}'))
        out = medium()
        ctx = gate.shape(out, PROMPT, KWS)
        self.assert_rank_order(ctx, out)
        self.assertIn("(Jev relevance check failed: unexpected response; used rank order.)", ctx)

    def test_connection_refused_falls_back_to_rank(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens on this port now
        self.use(url="http://127.0.0.1:%d/v1/systemone" % port)
        out = medium()
        ctx = gate.shape(out, PROMPT, KWS)
        self.assert_rank_order(ctx, out)
        self.assertIn("(Jev relevance check failed: ", ctx)
        self.assertIn("; used rank order.)", ctx)

    def test_timeout_falls_back_to_rank(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)  # the kernel accepts the connection; nothing ever answers
        self.addCleanup(s.close)
        self.use(url="http://127.0.0.1:%d/v1/systemone" % s.getsockname()[1])
        p = mock.patch.object(gate, "JEV_TIMEOUT", 0.3)
        p.start()
        self.addCleanup(p.stop)
        out = medium()
        ctx = gate.shape(out, PROMPT, KWS)
        self.assert_rank_order(ctx, out)
        self.assertIn("(Jev relevance check failed: timed out; used rank order.)", ctx)

    def test_key_unset_makes_no_request(self):
        jev = self.use(scored({"b5": 1.0}), key=None)
        out = medium()
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertEqual(jev.requests, [])
        self.assert_rank_order(ctx, out)
        self.assertNotIn("Jev", ctx)

    def test_small_result_makes_no_request(self):
        jev = self.use(scored({}))
        out = recall_output(2, 3)
        ctx = gate.shape(out, PROMPT, KWS)
        self.assertEqual(jev.requests, [])
        self.assertTrue(ctx.endswith("--- recall result ---\n" + out))


if __name__ == "__main__":
    unittest.main()
