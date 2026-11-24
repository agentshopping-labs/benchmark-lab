#!/usr/bin/env python3
"""Unit tests for runner/bench.py.

Stdlib unittest only; all network calls are mocked. The end-to-end CLI wiring is
covered by a --dry-run subprocess test so no provider endpoint is ever contacted.
"""
from __future__ import annotations

import gzip
import io
import json
import pathlib
import subprocess
import sys
import unittest
import urllib.error

HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE.parent / "runner"
sys.path.insert(0, str(RUNNER))

import bench  # noqa: E402

DETECTION = {
    "block_keywords": ["captcha", "access denied"],
    "min_markdown_chars": {"ecommerce": 200, "saas": 200,
                           "docs": 150, "sandbox": 100},
}


class FakeHeaders:
    def __init__(self, mapping=None):
        self._mapping = mapping or {}

    def get(self, key, default=None):
        return self._mapping.get(key, default)


class FakeResponse:
    def __init__(self, raw, status=200, headers=None):
        self._raw = raw
        self.status = status
        self.headers = FakeHeaders(headers)

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPureFunctions(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        values = [10, 20, 30, 40, 50]
        self.assertEqual(bench.percentile(values, 50), 30)
        self.assertEqual(bench.percentile(values, 95), 50)
        self.assertIsNone(bench.percentile([], 50))

    def test_get_path(self):
        payload = {"a": {"b": [{"c": 7}]}}
        self.assertEqual(bench.get_path(payload, "a.b.0.c"), 7)
        self.assertIs(bench.get_path(payload, "__body__"), payload)

    def test_word_count(self):
        self.assertEqual(bench.word_count("one two three"), 3)
        self.assertEqual(bench.word_count(""), 0)

    def test_stats_null_handling(self):
        self.assertIsNone(bench._stats([None, None]))
        result = bench._stats([1, None, 3])
        self.assertEqual(result["mean"], 2)
        self.assertEqual(result["null_count"], 1)

    def test_class_counts(self):
        records = [
            {"error_class": None, "block_reason": None, "success": True},
            {"error_class": "timeout", "block_reason": None, "success": False},
            {"error_class": None, "block_reason": "block_page", "success": False},
        ]
        counts = bench._class_counts(records)
        self.assertEqual(counts, {"success": 1, "timeout": 1, "block_page": 1})


class TestIsBlock(unittest.TestCase):
    def test_keyword_hit(self):
        self.assertEqual(bench.is_block("x" * 300 + " CAPTCHA here",
                                        {"page_type": "ecommerce"}, DETECTION),
                         "block_page")

    def test_below_threshold(self):
        self.assertEqual(bench.is_block("short",
                                        {"page_type": "docs"}, DETECTION),
                         "block_page")

    def test_empty(self):
        self.assertEqual(bench.is_block(None, {"page_type": "docs"}, DETECTION),
                         "empty")

    def test_valid(self):
        self.assertIsNone(bench.is_block("x" * 250,
                                         {"page_type": "ecommerce"}, DETECTION))


class TestRequestBuilding(unittest.TestCase):
    def test_get_query_encoding(self):
        provider = {
            "endpoint": "https://api.example.com/v1/",
            "request": {"method": "GET",
                        "query": {"apikey": "{KEY}", "url": "{URL}"}},
        }
        req = bench.make_request(provider, "https://target.com/page", "secret")
        self.assertEqual(req.full_url,
                         "https://api.example.com/v1/?apikey=secret&url=https%3A%2F%2F"
                         "target.com%2Fpage")
        self.assertIsNone(req.data)

    def test_post_body_fill(self):
        provider = {
            "endpoint": "https://api.example.com/v2/scrape",
            "request": {"method": "POST",
                        "headers": {"Authorization": "Bearer {KEY}"},
                        "body": {"url": "{URL}", "formats": ["markdown"]}},
        }
        req = bench.make_request(provider, "https://t.co/x", "k")
        self.assertEqual(req.headers["Authorization"], "Bearer k")
        self.assertEqual(json.loads(req.data),
                         {"url": "https://t.co/x", "formats": ["markdown"]})

    def test_basic_auth(self):
        provider = {
            "endpoint": "https://api.example.com/v1/extract",
            "request": {"method": "POST", "basic_auth": True,
                        "headers": {"Content-Type": "application/json"},
                        "body": {"url": "{URL}"}},
        }
        req = bench.make_request(provider, "https://t.co/x", "abc")
        import base64
        self.assertEqual(req.headers["Authorization"],
                         "Basic " + base64.b64encode(b"abc:").decode())

    def test_fill_recursive(self):
        node = {"a": {"u": "{URL}"}, "b": ["{URL}", "keep"]}
        bench._fill(node, "https://x")
        self.assertEqual(node["a"]["u"], "https://x")
        self.assertEqual(node["b"], ["https://x", "keep"])


class TestHeaderCapture(unittest.TestCase):
    def test_extract_allowlist(self):
        headers = FakeHeaders({
            "X-Request-Credits": "10", "X-Request-Cost": "1",
            "Concurrency-Remaining": "42", "X-Other": "skip"})
        out = bench.capture_response_headers(
            headers, ["X-Request-Credits", "X-Request-Cost",
                      "Concurrency-Remaining", "Missing-One"])
        self.assertEqual(out, {"X-Request-Credits": "10",
                               "X-Request-Cost": "1",
                               "Concurrency-Remaining": "42"})

    def test_empty_without_config(self):
        self.assertEqual(
            bench.capture_response_headers(FakeHeaders({"X": "y"}), None),
            {})

    def test_call_once_records_captured_headers(self):
        provider = {
            "endpoint": "https://api.example.com/v2",
            "markdown_path": "data.markdown",
            "capture_headers": ["X-Request-Credits"],
            "request": {"method": "POST",
                        "headers": {"Authorization": "Bearer {KEY}"},
                        "body": {"url": "{URL}"}}}
        payload = json.dumps({"data": {"markdown": "x" * 10}}).encode()
        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = lambda req, timeout: FakeResponse(
            payload, headers={"X-Request-Credits": "5"})
        try:
            result = bench.call_once(provider, {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["captured_headers"],
                         {"X-Request-Credits": "5"})

    def test_call_once_empty_headers_unconfigured(self):
        payload = json.dumps({"data": {"markdown": "x"}}).encode()
        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = lambda req, timeout: FakeResponse(payload)
        try:
            result = bench.call_once(
                {"endpoint": "https://api.example.com",
                 "markdown_path": "data.markdown",
                 "request": {"method": "POST", "body": {"url": "{URL}"}}},
                {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["captured_headers"], {})


class TestCallOnce(unittest.TestCase):
    def _provider(self, markdown_path="data.markdown"):
        return {
            "endpoint": "https://api.example.com/v2",
            "markdown_path": markdown_path,
            "request": {"method": "POST",
                        "headers": {"Authorization": "Bearer {KEY}"},
                        "body": {"url": "{URL}"}},
        }

    def test_success_json_path(self):
        payload = json.dumps({"data": {"markdown": "x" * 300}}).encode()
        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = lambda req, timeout: FakeResponse(payload)
        try:
            result = bench.call_once(self._provider(),
                                     {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["error_class"])
        self.assertEqual(result["markdown"], "x" * 300)

    def test_success_body_path(self):
        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = lambda req, timeout: FakeResponse(b"plain markdown")
        try:
            result = bench.call_once(self._provider("__body__"),
                                     {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["markdown"], "plain markdown")

    def test_gzip_response(self):
        body = gzip.compress(json.dumps({"data": {"markdown": "g" * 250}}).encode())
        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = lambda req, timeout: FakeResponse(
            body, headers={"Content-Encoding": "gzip"})
        try:
            result = bench.call_once(self._provider(),
                                     {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["markdown"], "g" * 250)

    def test_parse_error(self):
        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = lambda req, timeout: FakeResponse(b"not json")
        try:
            result = bench.call_once(self._provider(),
                                     {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["error_class"], "parse_error")
        self.assertIsNone(result["markdown"])

    def test_bare_dict_wrapped_for_index_path(self):
        payload = json.dumps({"url": "u", "markdown": "x" * 300}).encode()
        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = lambda req, timeout: FakeResponse(payload)
        try:
            result = bench.call_once(self._provider("0.markdown"),
                                     {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertIsNone(result["error_class"])
        self.assertEqual(result["markdown"], "x" * 300)

    def test_is_success_rejects_parse_error(self):
        self.assertFalse(bench.is_success(
            {"status": 200, "error_class": "parse_error"}, None))
        self.assertFalse(bench.is_success(
            {"status": 200, "error_class": None}, "block_page"))
        self.assertTrue(bench.is_success(
            {"status": 200, "error_class": None}, None))

    def test_http_error(self):
        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", FakeHeaders(),
                io.BytesIO(b'{"error":"rate limit"}'))

        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = fake_urlopen
        try:
            result = bench.call_once(self._provider(),
                                     {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["status"], 429)
        self.assertEqual(result["error_class"], "http_error")

    def test_429_surfaces_retry_after(self):
        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests",
                FakeHeaders({"Retry-After": "15"}), io.BytesIO(b"{}"))

        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = fake_urlopen
        try:
            result = bench.call_once(self._provider(),
                                     {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["retry_after"], 15.0)

    def test_timeout(self):
        def fake_urlopen(req, timeout):
            raise TimeoutError()

        original = bench.urllib.request.urlopen
        bench.urllib.request.urlopen = fake_urlopen
        try:
            result = bench.call_once(self._provider(),
                                     {"url": "https://t"}, "k", 30)
        finally:
            bench.urllib.request.urlopen = original
        self.assertEqual(result["error_class"], "timeout")
        self.assertIsNone(result["status"])


class TestRatePacer(unittest.TestCase):
    def test_disabled_when_zero(self):
        pacer = bench.RatePacer(0)
        for _ in range(50):
            pacer.wait()  # must never block

    def test_blocks_over_cap_within_window(self):
        now = [1000.0]
        slept = []
        pacer = bench.RatePacer(
            10, clock=lambda: now[0], sleeper=lambda s: slept.append(s))
        for _ in range(10):
            pacer.wait()
        self.assertEqual(slept, [])
        pacer.wait()  # 11th call must block
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 60.0)

    def test_window_expiry_frees_slot(self):
        now = [1000.0]
        pacer = bench.RatePacer(
            2, clock=lambda: now[0], sleeper=lambda s: now.__setitem__(0, now[0] + s))
        pacer.wait(); pacer.wait()
        now[0] += 61  # window slid past
        pacer.wait()  # no block, slot freed

    def test_rate_retry_delay_prefers_retry_after(self):
        self.assertEqual(bench.rate_retry_delay({"retry_after": 12}, 0), 12)
        self.assertEqual(bench.rate_retry_delay({"retry_after": None}, 0), 5)
        self.assertEqual(bench.rate_retry_delay({"retry_after": None}, 2), 20)
        self.assertEqual(bench.rate_retry_delay({"retry_after": None}, 6), 60)

    def test_call_with_rate_retry_retries_then_succeeds(self):
        pacer = bench.RatePacer(0)
        calls = []

        def caller(provider, page, key, timeout):
            calls.append(1)
            if len(calls) < 3:
                return {"status": 429, "retry_after": 1, "error_class": "http_error"}
            return {"status": 200, "retry_after": None, "error_class": None}

        delays = []
        result, attempts = bench.call_with_rate_retry(
            {}, {}, "k", 30, 5, pacer, sleep=delays.append, caller=caller)
        self.assertEqual(result["status"], 200)
        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [1, 1])

    def test_call_with_rate_retry_exhausts_budget(self):
        pacer = bench.RatePacer(0)
        caller = lambda *a: {"status": 429, "retry_after": None,
                             "error_class": "http_error"}
        result, attempts = bench.call_with_rate_retry(
            {}, {}, "k", 30, 2, pacer, sleep=lambda s: None, caller=caller)
        self.assertEqual(result["status"], 429)
        self.assertEqual(attempts, 2)


class TestCliDryRun(unittest.TestCase):
    def test_dry_run(self):
        proc = subprocess.run(
            [sys.executable, str(RUNNER / "bench.py"), "--round", "test",
             "--dry-run", "--limit", "2"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dry run complete", proc.stdout)
        self.assertNotIn("success=", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
