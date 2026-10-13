from __future__ import annotations

import json
import math
import queue
import subprocess
import threading
import time
from typing import Any

from .models import UsageSample
from .tokens import balance


class ProviderError(RuntimeError):
    """A sanitized provider or protocol failure."""


def _optional_int(value: object, field: str, *, positive: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderError(f"invalid {field}")
    if (positive and value <= 0) or (not positive and value < 0):
        raise ProviderError(f"invalid {field}")
    return value


def _optional_epoch(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError(f"invalid {field}")
    number = float(value)
    if not (number == number and abs(number) != float("inf")):
        raise ProviderError(f"invalid {field}")
    return number


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProviderError(f"invalid {field}")
    return value


def parse_usage(result: object, timestamp: float) -> list[UsageSample]:
    """Decode only the rate-limit fields the estimator persists."""
    if not isinstance(result, dict):
        raise ProviderError("invalid rate-limit response")

    reset_summary = result.get("rateLimitResetCredits")
    if reset_summary is None:
        reset_count = None
    elif isinstance(reset_summary, dict):
        reset_count = _optional_int(reset_summary.get("availableCount"), "reset count")
        if reset_count is None:
            raise ProviderError("invalid reset count")
    else:
        raise ProviderError("invalid reset count")

    bucket_map = result.get("rateLimitsByLimitId")
    if isinstance(bucket_map, dict) and bucket_map:
        buckets = list(bucket_map.items())
    else:
        legacy = result.get("rateLimits")
        if not isinstance(legacy, dict):
            raise ProviderError("missing rate limits")
        buckets = [("codex", legacy)]

    samples: list[UsageSample] = []
    for map_key, snapshot in buckets:
        if not isinstance(map_key, str) or not map_key or not isinstance(snapshot, dict):
            raise ProviderError("invalid rate-limit bucket")
        payload_id = snapshot.get("limitId")
        limit_id = _optional_string(payload_id, "limit id") if payload_id is not None else map_key
        limit_name = _optional_string(snapshot.get("limitName"), "limit name")
        for window_name in ("primary", "secondary"):
            window = snapshot.get(window_name)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise ProviderError("invalid rate-limit window")
            used = window.get("usedPercent")
            if isinstance(used, bool) or not isinstance(used, (int, float)):
                raise ProviderError("invalid used percent")
            used_percent = float(used)
            if not (used_percent == used_percent and 0.0 <= used_percent <= 100.0):
                raise ProviderError("invalid used percent")
            samples.append(
                UsageSample(
                    timestamp=float(timestamp),
                    used_percent=used_percent,
                    limit_id=limit_id,
                    window=window_name,
                    window_minutes=_optional_int(window.get("windowDurationMins"), "window duration", positive=True),
                    resets_at=_optional_epoch(window.get("resetsAt"), "reset timestamp"),
                    model=None,
                    banked_reset_count=reset_count,
                    limit_name=limit_name,
                )
            )
    if not samples:
        raise ProviderError("rate-limit response has no windows")
    return samples


class CodexProvider:
    supports_token_telemetry = True

    def __init__(self, codex_bin: str = "codex", timeout: float = 20) -> None:
        if not isinstance(codex_bin, str) or not codex_bin:
            raise ValueError("codex_bin must be a non-empty string")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be positive")
        self.credit_balance = None
        self.codex_bin = codex_bin
        self.timeout = float(timeout)
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[str | None] | None = None
        self._reader: threading.Thread | None = None
        self._next_id = 1

    def __enter__(self) -> CodexProvider:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _start(self) -> None:
        try:
            process = subprocess.Popen(
                [self.codex_bin, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except (OSError, ValueError) as exc:
            raise ProviderError("unable to start Codex app server") from exc
        self._process = process
        self._messages = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, args=(process, self._messages), daemon=True)
        self._reader.start()
        try:
            self._request(
                "initialize",
                {"clientInfo": {"name": "codex-usage-estimator", "version": "0.1.0"}},
            )
            self._notify("initialized")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _read_stdout(process: subprocess.Popen[str], messages: queue.Queue[str | None]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise ProviderError("Codex app server is unavailable")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ProviderError("Codex app server connection failed") from exc

    def _notify(self, method: str) -> None:
        self._send({"method": method})

    def _request(self, method: str, params: object) -> object:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("Codex app server request timed out")
            assert self._messages is not None
            try:
                line = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise ProviderError("Codex app server request timed out") from exc
            if line is None:
                raise ProviderError("Codex app server closed the connection")
            try:
                response = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ProviderError("Codex app server returned invalid JSON") from exc
            if not isinstance(response, dict) or response.get("id") != request_id:
                continue
            if "error" in response:
                raise ProviderError("Codex app server RPC request failed")
            if "result" not in response:
                raise ProviderError("Codex app server returned an invalid response")
            return response["result"]

    def sample(self) -> list[UsageSample]:
        if self._process is None:
            self._start()
        try:
            result = self._request("account/rateLimits/read", None)
            samples = parse_usage(result, time.time())
            snapshot = result.get('rateLimits') or {}
            # Only the ordinary Codex bucket establishes paid-credit spending.
            buckets = result.get('rateLimitsByLimitId')
            for key, value in (buckets.items() if isinstance(buckets, dict) else []):
                if isinstance(value, dict) and (value.get('limitId') or key) == 'codex':
                    snapshot = value
                    break
            if not isinstance(snapshot, dict) or snapshot.get('limitId', 'codex') != 'codex':
                snapshot = {}
            credits = snapshot.get('credits') or {}
            self.credit_balance = balance(credits.get('balance')) if isinstance(credits, dict) else None
            return samples
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self._process
        reader = self._reader
        self._process = None
        self._messages = None
        self._reader = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
        if reader is not None:
            reader.join(timeout=1)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
