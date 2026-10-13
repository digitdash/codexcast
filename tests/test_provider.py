import math
import os
from pathlib import Path
import stat
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from codex_usage.models import UsageSample
from codex_usage.provider import CodexProvider, ProviderError, parse_usage


class UsageSampleTests(unittest.TestCase):
    def test_derives_clamped_remaining_percent_and_bucket_key(self):
        sample = UsageSample(timestamp=10.0, used_percent=25.5, limit_id="spark", window="secondary")

        self.assertEqual(sample.remaining_percent, 74.5)
        self.assertEqual(sample.key, ("spark", "secondary"))

    def test_rejects_invalid_required_values(self):
        invalid = (
            {"timestamp": math.nan, "used_percent": 1.0},
            {"timestamp": 1.0, "used_percent": math.inf},
            {"timestamp": 1.0, "used_percent": -0.1},
            {"timestamp": 1.0, "used_percent": 100.1},
        )

        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                UsageSample(**kwargs)

    def test_rejects_invalid_optional_values(self):
        invalid = (
            {"window_minutes": 0},
            {"resets_at": math.inf},
            {"model": ""},
            {"banked_reset_count": -1},
            {"banked_reset_count": True},
            {"limit_name": 3},
        )

        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaises((TypeError, ValueError)):
                UsageSample(timestamp=1.0, used_percent=1.0, **extra)


class ParseUsageTests(unittest.TestCase):
    def test_parses_all_windows_from_all_populated_buckets(self):
        result = {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "primary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 2000},
                    "secondary": None,
                    "credits": {"hasCredits": True, "unlimited": False, "balance": "99.50"},
                    "individualLimit": {"remainingPercent": 7},
                },
                "spark": {
                    "limitId": "spark",
                    "limitName": None,
                    "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": None},
                    "secondary": {"usedPercent": 4, "windowDurationMins": None, "resetsAt": 2100},
                },
            },
            "rateLimits": {"primary": {"usedPercent": 99}},
            "rateLimitResetCredits": {"availableCount": 1, "credits": [{"id": "secret"}]},
            "unknown": {"private": "discard me"},
        }

        samples = parse_usage(result, timestamp=1000.0)

        self.assertEqual(
            samples,
            [
                UsageSample(1000.0, 25.0, "codex", "primary", 10080, 2000.0, None, 1, "Codex"),
                UsageSample(1000.0, 0.0, "spark", "primary", 300, None, None, 1, None),
                UsageSample(1000.0, 4.0, "spark", "secondary", None, 2100.0, None, 1, None),
            ],
        )

    def test_falls_back_to_legacy_bucket_when_multi_bucket_is_empty(self):
        result = {
            "rateLimitsByLimitId": {},
            "rateLimits": {
                "limitId": "legacy",
                "limitName": "Legacy quota",
                "primary": {"usedPercent": 7, "windowDurationMins": None, "resetsAt": None},
            },
            "rateLimitResetCredits": None,
        }

        self.assertEqual(
            parse_usage(result, 12.0),
            [UsageSample(12.0, 7.0, "legacy", "primary", None, None, None, None, "Legacy quota")],
        )

    def test_uses_bucket_map_key_when_payload_limit_id_is_missing(self):
        result = {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 3, "windowDurationMins": 300, "resetsAt": 50}}
            },
            "rateLimits": {},
        }

        self.assertEqual(parse_usage(result, 1.0)[0].limit_id, "codex")

    def test_zero_reset_count_is_distinct_from_null(self):
        base = {"rateLimits": {"primary": {"usedPercent": 0}}}

        zero = parse_usage({**base, "rateLimitResetCredits": {"availableCount": 0}}, 1.0)[0]
        null = parse_usage({**base, "rateLimitResetCredits": None}, 1.0)[0]

        self.assertEqual(zero.banked_reset_count, 0)
        self.assertIsNone(null.banked_reset_count)

    def test_never_treats_spending_credits_or_quota_name_as_model_or_resets(self):
        sample = parse_usage(
            {
                "rateLimits": {
                    "limitName": "GPT-5 quota",
                    "credits": {"hasCredits": True, "balance": "200"},
                    "primary": {"usedPercent": 2},
                }
            },
            1.0,
        )[0]

        self.assertIsNone(sample.model)
        self.assertIsNone(sample.banked_reset_count)
        self.assertEqual(sample.limit_name, "GPT-5 quota")

    def test_rejects_malformed_or_missing_usage_without_echoing_payload(self):
        bad_results = (
            {},
            {"rateLimits": {}},
            {"rateLimits": {"primary": {"usedPercent": None}}},
            {"rateLimits": {"primary": {"usedPercent": "25"}}},
            {"rateLimits": {"primary": {"usedPercent": 101}}},
            {"rateLimitsByLimitId": {"codex": "secret-value"}, "rateLimits": {}},
            {"rateLimits": {"primary": {"usedPercent": 5}}, "rateLimitResetCredits": {"availableCount": "1"}},
        )

        for result in bad_results:
            with self.subTest(result=result), self.assertRaises(ProviderError) as raised:
                parse_usage(result, 1.0)
            self.assertNotIn("secret-value", str(raised.exception))


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import sys
import time

mode = os.environ.get("FAKE_CODEX_MODE", "success")
if mode == "startup_eof":
    raise SystemExit(0)
if mode == "startup_timeout":
    time.sleep(30)
    raise SystemExit(0)

initialize = json.loads(sys.stdin.readline())
if mode == "auth_error":
    print(json.dumps({"id": initialize["id"], "error": {"code": -32000, "message": "token SUPERSECRET rejected", "data": {"auth": "SUPERSECRET"}}}), flush=True)
    time.sleep(30)
    raise SystemExit(0)
print(json.dumps({"id": initialize["id"], "result": {"serverInfo": {"name": "fake"}}}), flush=True)
json.loads(sys.stdin.readline())
while True:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    request = json.loads(line)
    if mode == "read_eof":
        raise SystemExit(0)
    if mode == "read_timeout":
        time.sleep(30)
        raise SystemExit(0)
    print(json.dumps({"method": "account/rateLimits/updated", "params": {"ignored": True}}), flush=True)
    print(json.dumps({"id": request["id"], "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 2000}}}}), flush=True)
'''


class CodexProviderProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.fake_codex = Path(self.temp_dir.name) / "codex"
        self.fake_codex.write_text(textwrap.dedent(FAKE_CODEX))
        self.fake_codex.chmod(self.fake_codex.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initializes_once_and_reuses_process_for_reads(self):
        with patch.dict(os.environ, {"FAKE_CODEX_MODE": "success"}):
            with CodexProvider(str(self.fake_codex), timeout=1) as provider:
                first = provider.sample()
                second = provider.sample()

        self.assertEqual(first[0].used_percent, 12.0)
        self.assertEqual(second[0].limit_id, "codex")

    def test_sanitizes_rpc_auth_errors_and_cleans_up_startup(self):
        with patch.dict(os.environ, {"FAKE_CODEX_MODE": "auth_error"}):
            provider = CodexProvider(str(self.fake_codex), timeout=1)
            with self.assertRaisesRegex(ProviderError, "RPC") as raised:
                provider.sample()

        self.assertNotIn("SUPERSECRET", str(raised.exception))
        self.assertIsNone(provider._process)

    def test_reports_timeout_and_eof_and_cleans_up(self):
        for mode in ("startup_timeout", "startup_eof", "read_timeout", "read_eof"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"FAKE_CODEX_MODE": mode}):
                provider = CodexProvider(str(self.fake_codex), timeout=0.1)
                with self.assertRaises(ProviderError):
                    provider.sample()
                self.assertIsNone(provider._process)

    def test_close_is_idempotent_and_sample_can_restart_after_close(self):
        with patch.dict(os.environ, {"FAKE_CODEX_MODE": "success"}):
            provider = CodexProvider(str(self.fake_codex), timeout=1)
            provider.sample()
            provider.close()
            provider.close()
            restarted = provider.sample()
            provider.close()

        self.assertEqual(restarted[0].used_percent, 12.0)


if __name__ == "__main__":
    unittest.main()
