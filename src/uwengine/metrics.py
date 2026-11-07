"""Prometheus text exposition without extra dependencies."""

from __future__ import annotations

import threading
from collections import defaultdict

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


def _labels(d: dict) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(d.items())) + "}" if d else ""


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.http = defaultdict(int)  # (route, status)
        self.decisions = defaultdict(int)  # (program, outcome, rules_version)
        self.whatif_scenarios = 0
        self.assist_calls = defaultdict(int)  # provider
        self.buckets = [0] * len(BUCKETS)
        self.dur_sum = 0.0
        self.dur_count = 0

    def observe_http(self, route: str, status: int, seconds: float) -> None:
        with self.lock:
            self.http[(route, str(status))] += 1
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    self.buckets[i] += 1
            self.dur_sum += seconds
            self.dur_count += 1

    def observe_decision(self, program: str, outcome: str, version: str) -> None:
        with self.lock:
            self.decisions[(program, outcome, version)] += 1

    def render(self) -> str:
        out: list[str] = []
        with self.lock:
            out += [
                "# HELP uw_http_requests_total HTTP requests by route and status.",
                "# TYPE uw_http_requests_total counter",
            ]
            out += [
                f"uw_http_requests_total{_labels({'route': r, 'status': s})} {v}"
                for (r, s), v in sorted(self.http.items())
            ]
            out += [
                "# HELP uw_decisions_total Decisions by program, outcome and rules version.",
                "# TYPE uw_decisions_total counter",
            ]
            out += [
                f"uw_decisions_total{_labels({'program': p, 'outcome': o, 'rules_version': rv})} {v}"
                for (p, o, rv), v in sorted(self.decisions.items())
            ]
            out += [
                "# HELP uw_whatif_scenarios_total What-if scenarios evaluated.",
                "# TYPE uw_whatif_scenarios_total counter",
                f"uw_whatif_scenarios_total {self.whatif_scenarios}",
            ]
            out += [
                "# HELP uw_assist_calls_total Summary and letter drafts by provider.",
                "# TYPE uw_assist_calls_total counter",
            ]
            out += [
                f"uw_assist_calls_total{_labels({'provider': p})} {v}" for p, v in sorted(self.assist_calls.items())
            ]
            out += [
                "# HELP uw_request_duration_seconds Request latency.",
                "# TYPE uw_request_duration_seconds histogram",
            ]
            for b, c in zip(BUCKETS, self.buckets, strict=True):
                out.append(f'uw_request_duration_seconds_bucket{{le="{b}"}} {c}')
            out.append(f'uw_request_duration_seconds_bucket{{le="+Inf"}} {self.dur_count}')
            out.append(f"uw_request_duration_seconds_sum {self.dur_sum:.6f}")
            out.append(f"uw_request_duration_seconds_count {self.dur_count}")
        return "\n".join(out) + "\n"
