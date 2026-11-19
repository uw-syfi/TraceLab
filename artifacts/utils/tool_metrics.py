"""Raw-call aggregate expressions; callers keep cohort, names, grouping, and rates."""


def tool_aggregate_sql(*, latency: str = "eff", error: str = "is_error") -> dict[str, str]:
    """Use trusted SQL expressions; keep null versus zero for no positive timing separate."""
    return {
        "calls": "count(*)",
        "errors": f"count(*) FILTER (WHERE {error} = TRUE)",
        "successes": f"count(*) FILTER (WHERE {error} = FALSE)",
        "unknown_outcomes": f"count(*) FILTER (WHERE {error} IS NULL)",
        "positive_latency_calls": f"count(*) FILTER (WHERE {latency} > 0)",
        "missing_latency_calls": f"count(*) FILTER (WHERE {latency} IS NULL)",
        "nonpositive_latency_calls": f"count(*) FILTER (WHERE {latency} <= 0)",
        "latency_sum": f"sum({latency}) FILTER (WHERE {latency} > 0)",
        "latency_sum_zero": f"sum(CASE WHEN {latency} > 0 THEN {latency} ELSE 0 END)",
    }
