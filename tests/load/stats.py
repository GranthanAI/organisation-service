"""Latency statistics for the load scripts."""

import math


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = max(0, min(len(sorted_values) - 1, math.ceil(p / 100 * len(sorted_values)) - 1))
    return sorted_values[k]


def summarise(seconds: list[float], errors: int = 0) -> dict:
    values = sorted(seconds)
    return {"n": len(values), "errors": errors, "p50_ms": round(percentile(values, 50) * 1000, 2),
            "p95_ms": round(percentile(values, 95) * 1000, 2), "p99_ms": round(percentile(values, 99) * 1000, 2),
            "max_ms": round((values[-1] if values else 0) * 1000, 2)}


def line(label: str, summary: dict) -> str:
    return (f"{label:<46} n={summary['n']:<5} err={summary['errors']:<3} p50={summary['p50_ms']:>7} ms  "
            f"p95={summary['p95_ms']:>7} ms  p99={summary['p99_ms']:>7} ms  max={summary['max_ms']:>7} ms")
