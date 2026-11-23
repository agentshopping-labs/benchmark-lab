#!/usr/bin/env python3
"""AgentShopping benchmark runner v1.

Single-threaded, rate-limited, pure HTTP calls to provider APIs. No headless
browser, no concurrent calls, no outbound traffic beyond the configured
provider endpoints. Every action is guarded by a STOP file / BENCH_KILL env,
per-call timeout and inter-call delay.

Stdlib only; tiktoken is optional. If tiktoken is unavailable, token fields
are null and the run metadata says so, rather than substituting a guess.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import gzip
import hashlib
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

LAB_ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_env():
    env_path = LAB_ROOT / ".env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_json(relative):
    return json.loads((LAB_ROOT / relative).read_text(encoding="utf-8"))


def killed():
    return os.environ.get("BENCH_KILL") == "1" or (LAB_ROOT / "STOP").exists()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_path(value, dotted):
    if dotted == "__body__":
        return value
    current = value
    for part in dotted.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def percentile(sorted_values, pct):
    if not sorted_values:
        return None
    rank = max(1, -(-pct * len(sorted_values) // 100))  # nearest rank, ceil
    return sorted_values[min(rank, len(sorted_values)) - 1]


def word_count(text):
    return len(text.split())


def make_request(provider, url, key):
    spec = provider["request"]
    method = spec.get("method", "GET")
    endpoint = provider["endpoint"]
    headers = {k: v.replace("{KEY}", key) for k, v in spec.get("headers", {}).items()}
    body = None
    if method == "GET":
        query = {k: v.replace("{KEY}", key).replace("{URL}", url)
                 for k, v in spec["query"].items()}
        endpoint = endpoint + "?" + urllib.parse.urlencode(query)
    else:
        payload = json.loads(json.dumps(spec.get("body", {})))
        _fill(payload, url)
        body = json.dumps(payload).encode("utf-8")
    if spec.get("basic_auth"):
        token = base64.b64encode(f"{key}:".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(endpoint, data=body, headers=headers, method=method)
    return request


def _fill(node, url):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                node[key] = value.replace("{URL}", url)
            else:
                _fill(value, url)
    elif isinstance(node, list):
        for pos, item in enumerate(node):
            if isinstance(item, str):
                node[pos] = item.replace("{URL}", url)
            else:
                _fill(item, url)


def call_once(provider, page, key, timeout):
    request = make_request(provider, page["url"], key)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read() or b""
        if error.headers.get("Content-Encoding") == "gzip" and raw:
            raw = gzip.decompress(raw)
        status = error.code
    except TimeoutError:
        return {"status": None, "error_class": "timeout", "markdown": None,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except urllib.error.URLError as error:
        if "timed out" in str(error.reason):
            return {"status": None, "error_class": "timeout", "markdown": None,
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        return {"status": None, "error_class": "network", "markdown": None,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    latency_ms = round((time.monotonic() - started) * 1000)

    markdown = None
    error_class = None
    try:
        if provider["markdown_path"] == "__body__":
            markdown = raw.decode("utf-8", errors="replace")
            parsed = None
        else:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            markdown = get_path(parsed, provider["markdown_path"])
    except (ValueError, KeyError, IndexError, TypeError):
        error_class = "parse_error"
        markdown = None
    if status != 200:
        error_class = "http_error"
    return {"status": status, "error_class": error_class, "markdown": markdown,
            "latency_ms": latency_ms, "raw_bytes": len(raw)}


def is_block(markdown, page, detection):
    if not markdown:
        return "empty"
    lowered = markdown.lower()
    for keyword in detection["block_keywords"]:
        if keyword in lowered:
            return "block_page"
    threshold = detection["min_markdown_chars"][page["page_type"]]
    if len(markdown) < threshold:
        return "block_page"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", required=True, help="round label, e.g. r1")
    parser.add_argument("--providers", default="", help="comma-separated provider ids")
    parser.add_argument("--limit", type=int, default=0, help="max corpus pages (0 = all)")
    parser.add_argument("--indices", default="", help="comma-separated corpus indices, e.g. 1,5")
    parser.add_argument("--max-errors", type=int, default=3,
                        help="abort after this many consecutive quota/network errors")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between calls")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env()
    provider_config = load_json("configs/providers.json")["providers"]
    detection = load_json("configs/detection.json")
    pages = load_json("corpus/corpus.json")["pages"]
    if args.indices.strip():
        wanted = {int(x) for x in args.indices.split(",") if x.strip()}
        pages = [p for p in pages if p["index"] in wanted]
    if args.limit:
        pages = pages[:args.limit]
    if not pages:
        raise SystemExit("no corpus pages selected")
    selected = [p for p in provider_config
                if not args.providers or p["id"] in args.providers.split(",")]

    try:
        import tiktoken
        encoder = tiktoken.get_encoding("cl100k_base")
        tokenizer_meta = {"name": "tiktoken", "encoding": "cl100k_base"}
    except ImportError:
        encoder = None
        tokenizer_meta = {"name": None, "note": "tiktoken unavailable; token fields null"}

    run_id = f"{args.round}-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')}"
    run_dir = LAB_ROOT / "runs" / run_id
    if not args.dry_run:
        for provider in selected:
            (run_dir / "raw" / provider["id"]).mkdir(parents=True, exist_ok=True)

    meta = {"run_id": run_id, "started": utc_now(), "round": args.round,
            "dry_run": args.dry_run, "tokenizer": tokenizer_meta,
            "providers": [p["id"] for p in selected], "page_count": len(pages),
            "delay_seconds": args.delay, "timeout_seconds": args.timeout}
    if not args.dry_run:
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    summaries = {}
    consecutive_errors = 0
    for provider in selected:
        key = os.environ.get(provider["key_env"], "")
        if not key and not args.dry_run:
            raise SystemExit(f"missing {provider['key_env']} in .env")
        records = []
        for page in pages:
            if killed():
                raise SystemExit("kill switch detected; stopping")
            if args.dry_run:
                print(f"[dry] {provider['id']} <- {page['url']}")
                continue
            result = call_once(provider, page, key, args.timeout)
            markdown = result.pop("markdown")
            block_reason = is_block(markdown, page, detection) if not result["error_class"] else None
            success = result["status"] == 200 and not block_reason
            tokens = None
            if markdown and encoder:
                tokens = len(encoder.encode(markdown))
            response_sha256 = (hashlib.sha256(markdown.encode("utf-8")).hexdigest()
                               if markdown else None)
            record = {
                "index": page["index"], "url": page["url"],
                "page_type": page["page_type"], "anti_bot": page["anti_bot"],
                **result,
                "response_sha256": response_sha256,
                "block_reason": block_reason,
                "success": success,
                "markdown_chars": len(markdown) if markdown else 0,
                "markdown_words": word_count(markdown) if markdown else 0,
                "markdown_tokens": tokens,
            }
            raw_dir = run_dir / "raw" / provider["id"]
            (raw_dir / f"{page['index']:02d}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            if markdown:
                (raw_dir / f"{page['index']:02d}.md").write_text(markdown, encoding="utf-8")
            records.append(record)
            print(f"{provider['id']} {page['index']:02d} success={success} "
                  f"status={record['status']} latency={record['latency_ms']}ms")
            if success:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= args.max_errors:
                    raise SystemExit(
                        f"circuit breaker: {consecutive_errors} consecutive failures "
                        f"(quota/network/block) — run aborted to protect samples")
            time.sleep(args.delay)

        if records:
            latencies = sorted(r["latency_ms"] for r in records)
            summaries[provider["id"]] = {
                "calls": len(records),
                "successes": sum(1 for r in records if r["success"]),
                "success_rate": round(sum(1 for r in records if r["success"]) / len(records), 4),
                "failure_classes": _class_counts(records),
                "latency_ms": {"p50": percentile(latencies, 50),
                               "p95": percentile(latencies, 95),
                               "min": latencies[0], "max": latencies[-1]},
                "markdown_tokens": _stats([r["markdown_tokens"] for r in records]),
                "markdown_chars": _stats([r["markdown_chars"] for r in records]),
                "markdown_words": _stats([r["markdown_words"] for r in records]),
            }

    if not args.dry_run:
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": run_id, "finished": utc_now(), "summaries": summaries},
                       indent=2, ensure_ascii=False),
            encoding="utf-8")
        # Public-safe copy: metrics + hashes only, markdown bodies excluded.
        metrics_dir = LAB_ROOT / "metrics" / run_id
        metrics_dir.mkdir(parents=True, exist_ok=True)
        (metrics_dir / "meta.json").write_text(
            (run_dir / "meta.json").read_text(encoding="utf-8"), encoding="utf-8")
        (metrics_dir / "summary.json").write_text(
            (run_dir / "summary.json").read_text(encoding="utf-8"), encoding="utf-8")
        for json_file in (run_dir / "raw").rglob("*.json"):
            target = metrics_dir / "raw" / json_file.parent.name / json_file.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json_file.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"run complete: {run_dir}")
        print(f"public metrics copy: {metrics_dir}")
    else:
        print("dry run complete; no calls made")


def _class_counts(records):
    counts = {}
    for record in records:
        reason = record["error_class"] or record["block_reason"]
        if record["success"]:
            reason = "success"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _stats(values):
    numeric = [v for v in values if v is not None]
    if not numeric:
        return None
    return {"min": min(numeric), "max": max(numeric),
            "mean": round(sum(numeric) / len(numeric), 2),
            "null_count": len(values) - len(numeric)}


if __name__ == "__main__":
    main()
