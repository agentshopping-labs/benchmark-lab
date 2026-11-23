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
