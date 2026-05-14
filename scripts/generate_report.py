from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import LabConfig, load_config


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_delta(before: float, after: float, as_percent: bool = False) -> str:
    if as_percent:
        return f"{(after - before) * 100:.2f} pp"
    if before == 0:
        return "n/a"
    change = ((after - before) / before) * 100
    return f"{change:+.1f}%"


def _build_cache_comparison(config: LabConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    queries = load_queries()
    with_cache_cfg = config.model_copy(deep=True)
    with_cache_cfg.cache.enabled = True

    without_cache_cfg = config.model_copy(deep=True)
    without_cache_cfg.cache.enabled = False

    with_cache = run_simulation(with_cache_cfg, queries).to_report_dict()
    without_cache = run_simulation(without_cache_cfg, queries).to_report_dict()
    return without_cache, with_cache


def _redis_observation(config: LabConfig) -> tuple[str, str]:
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(config.cache.redis_url, decode_responses=True)
        client.ping()
        key_count = sum(1 for _ in client.scan_iter("rl:cache:*"))
        client.close()
        return "reachable", f"Redis reachable at {config.cache.redis_url}; keys matching rl:cache:* = {key_count}."
    except Exception as exc:
        return "unavailable", f"Redis not reachable from this environment ({type(exc).__name__})."


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()

    metrics: dict[str, Any] = json.loads(Path(args.metrics).read_text())
    config = load_config(args.config)
    without_cache, with_cache = _build_cache_comparison(config)
    redis_status, redis_note = _redis_observation(config)

    availability = float(metrics.get("availability", 0.0))
    p95 = float(metrics.get("latency_p95_ms", 0.0))
    p99 = float(metrics.get("latency_p99_ms", 0.0))
    fallback_rate = float(metrics.get("fallback_success_rate", 0.0))
    cache_hit_rate = float(metrics.get("cache_hit_rate", 0.0))
    recovery_time = float(metrics.get("recovery_time_ms", 0.0) or 0.0)

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture Summary",
        "",
        "The gateway applies layered reliability controls in this order: cache lookup, circuit-breaker guarded provider routing, and static fallback if all providers fail.",
        "Route reasons are explicit (e.g. primary:<provider>, fallback:<provider>, cache_hit:<score>) so failures are diagnosable from logs and metrics.",
        "",
        "```",
        "User Request",
        "    |",
        "    v",
        "[ReliabilityGateway]",
        "    |",
        "    +--> [Cache check] --hit--> return cached",
        "    |",
        "    +--> [CircuitBreaker: primary] --> Provider A",
        "    |",
        "    +--> [CircuitBreaker: backup] --> Provider B",
        "    |",
        "    v",
        "[Static fallback message]",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Why this value |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Trips quickly on persistent failures while tolerating single transient errors |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Allows provider cooldown before half-open probes |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | Single successful probe is enough to close in this simulation |",
        f"| cache TTL | {config.cache.ttl_seconds} | Balances freshness and hit rate for repeated FAQ-like prompts |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | High enough to reduce semantic false hits (e.g., year-sensitive queries) |",
        f"| load_test requests | {config.load_test.requests} | Provides enough volume to exercise circuits and caching behavior |",
        "",
        "## 3. SLO Definitions",
        "",
        "| SLI | SLO Target | Actual | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {_fmt_pct(availability)} | {'yes' if availability >= 0.99 else 'no'} |",
        f"| Latency P95 | < 2500 ms | {p95:.2f} ms | {'yes' if p95 < 2500 else 'no'} |",
        f"| Latency P99 | < 600 ms | {p99:.2f} ms | {'yes' if p99 < 600 else 'no'} |",
        f"| Fallback success rate | >= 90% | {_fmt_pct(fallback_rate)} | {'yes' if fallback_rate >= 0.90 else 'no'} |",
        f"| Cache hit rate | >= 10% | {_fmt_pct(cache_hit_rate)} | {'yes' if cache_hit_rate >= 0.10 else 'no'} |",
        f"| Recovery time | < 6000 ms | {recovery_time:.2f} ms | {'yes' if recovery_time < 6000 else 'no'} |",
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key == "scenarios":
            continue
        lines.append(f"| {key} | {value} |")
    lines += [
        "",
        "## 5. Chaos Scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    for key, value in metrics.get("scenarios", {}).items():
        expected = {
            "primary_timeout_100": "Primary opens quickly; backup serves most traffic",
            "primary_flaky_50": "Circuit oscillates with mixed primary/fallback",
            "all_healthy": "Mostly primary responses, minimal fallback",
            "cache_stale_candidate": "False-hit guard blocks year-mismatch cache reuse",
        }.get(key, "Scenario-specific reliability expectation")
        observed = "Matched expected pattern in run metrics" if value == "pass" else "Did not match expected pattern"
        lines.append(f"| {key} | {expected} | {observed} | {value} |")

    lines += [
        "",
        "## 6. Cache Comparison (Without vs With Cache)",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
        (
            f"| latency_p50_ms | {without_cache['latency_p50_ms']} | {with_cache['latency_p50_ms']} "
            f"| {_fmt_delta(float(without_cache['latency_p50_ms']), float(with_cache['latency_p50_ms']))} |"
        ),
        (
            f"| latency_p95_ms | {without_cache['latency_p95_ms']} | {with_cache['latency_p95_ms']} "
            f"| {_fmt_delta(float(without_cache['latency_p95_ms']), float(with_cache['latency_p95_ms']))} |"
        ),
        (
            f"| estimated_cost | {without_cache['estimated_cost']} | {with_cache['estimated_cost']} "
            f"| {_fmt_delta(float(without_cache['estimated_cost']), float(with_cache['estimated_cost']))} |"
        ),
        (
            f"| cache_hit_rate | {without_cache['cache_hit_rate']} | {with_cache['cache_hit_rate']} "
            f"| {_fmt_delta(float(without_cache['cache_hit_rate']), float(with_cache['cache_hit_rate']), as_percent=True)} |"
        ),
        "",
        "## 7. Redis Shared Cache",
        "",
        "Shared cache is important for horizontally scaled deployments because one instance can reuse entries written by another, reducing duplicate provider calls and cost.",
        "SharedRedisCache stores query/response pairs in Redis hashes with TTL and reuses the same privacy and false-hit guardrails as memory cache.",
        "",
        f"Redis connectivity during report generation: {redis_status}. {redis_note}",
        "",
        "Redis verification command:",
        "```bash",
        "docker compose exec redis redis-cli KEYS \"rl:cache:*\"",
        "```",
        "",
        "Shared-state evidence source: test_shared_state_across_instances in tests/test_redis_cache.py.",
        "",
        "## 8. Failure Analysis",
        "",
        "Remaining weakness: circuit breaker state is process-local, so multi-instance deployments can still over-hit a failing provider until every instance independently opens.",
        "A production fix is to store circuit counters/state in Redis (atomic INCR + EXPIRE), so open/half-open/closed decisions are shared across instances.",
        "",
        "## 9. Next Steps",
        "",
        "1. Move circuit state to Redis for multi-instance coordination.",
        "2. Add concurrent load simulation using configured concurrency and compare throughput.",
        "3. Export metrics to Prometheus and add alerts for SLO breaches.",
    ]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
