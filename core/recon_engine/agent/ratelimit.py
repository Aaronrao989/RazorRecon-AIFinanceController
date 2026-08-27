"""Shared rate limiter for ALL agent Groq calls in a batch.

The agent makes one model call per reasoning step, so a tight loop can burst
calls back-to-back. This limiter enforces a minimum interval between calls
(derived from RPM_CAP) so the whole batch stays under Groq's 30 RPM free-tier
limit — by construction, not by luck. It also records call timestamps so we can
report the peak RPM actually observed (proof we respected the limit).

Thread-safe: the backend may run batches on worker threads.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .. import config


class RateLimiter:
    def __init__(self, min_interval_seconds: float | None = None, rpm_cap: int | None = None):
        self.min_interval = (
            min_interval_seconds
            if min_interval_seconds is not None
            else config.AGENT_MIN_INTERVAL_SECONDS
        )
        self.rpm_cap = rpm_cap if rpm_cap is not None else config.RPM_CAP
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._timestamps: deque[float] = deque()  # times of granted calls (last 60s)
        self.total_calls = 0

    def acquire(self) -> None:
        """Block until it is safe to make the next call, then record it."""
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_call = now
            self._timestamps.append(now)
            self.total_calls += 1
            # Drop timestamps older than 60s.
            cutoff = now - 60.0
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()

    def peak_rpm(self) -> int:
        """Highest number of calls observed within any trailing 60s window."""
        with self._lock:
            times = list(self._timestamps)
        if not times:
            return 0
        # Sliding window over recorded timestamps.
        peak = 0
        j = 0
        for i in range(len(times)):
            while times[i] - times[j] > 60.0:
                j += 1
            peak = max(peak, i - j + 1)
        return peak

    def stats(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "peak_rpm": self.peak_rpm(),
            "rpm_cap": self.rpm_cap,
            "min_interval_seconds": round(self.min_interval, 3),
        }
