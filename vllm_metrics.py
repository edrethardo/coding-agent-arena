"""Model-side metrics for a benchmark round, scraped from vLLM's Prometheus endpoint.

Why this exists: the agent makes the HTTP call to vLLM itself, so the runner only ever saw
wall-clock time. Wall-clock cannot distinguish "the model was slow" from "the agent spent
twenty minutes thinking between calls", and it cannot see the numbers the serving docs use
to explain *why* a config is fast -- prefix-cache hit rate and speculative-decode
acceptance. Those are the difference between a result and an anecdote.

vLLM's counters are monotonic and server-wide, so a round is measured as the delta between
two snapshots. Server-wide means this is only valid while nothing else is using the
endpoint -- which the GPU lock guarantees, and `exclusive` records rather than assumes.
"""
import urllib.request

ENDPOINT = "http://127.0.0.1:8000/metrics"

COUNTERS = [
    "vllm:prompt_tokens_total", "vllm:generation_tokens_total",
    "vllm:time_to_first_token_seconds_sum", "vllm:time_to_first_token_seconds_count",
    "vllm:inter_token_latency_seconds_sum", "vllm:inter_token_latency_seconds_count",
    "vllm:e2e_request_latency_seconds_sum", "vllm:e2e_request_latency_seconds_count",
    "vllm:spec_decode_num_accepted_tokens_total", "vllm:spec_decode_num_draft_tokens_total",
    "vllm:spec_decode_num_drafts_total",
    "vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total",
    "vllm:num_preemptions_total", "vllm:request_success_total",
]
GAUGES = ["vllm:num_requests_running", "vllm:kv_cache_usage_perc"]


def snapshot(url=ENDPOINT, timeout=15):
    """Current value of every counter we care about. Labels are summed across engines."""
    out = {k: 0.0 for k in COUNTERS + GAUGES}
    try:
        raw = urllib.request.urlopen(url, timeout=timeout).read().decode()
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}
    for line in raw.splitlines():
        if line.startswith("#") or not line.startswith("vllm:"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name in out:
            try:
                out[name] += float(line.rsplit(" ", 1)[1])
            except (ValueError, IndexError):
                pass
    return out


def delta(before, after, wall_s):
    """Derive the round's model-side numbers from two snapshots.

    Rates are per second of *wall* time for the round, which for a single-request-at-a-time
    agent is the honest denominator: it includes the gaps where the agent was thinking and
    the model idle, and those gaps are real cost.
    """
    if "_error" in before or "_error" in after:
        return {"metrics_error": before.get("_error") or after.get("_error")}

    # These counters are monotonic for the lifetime of one server process. If any of them
    # went backwards, the server restarted between the two snapshots -- the engine is
    # socket-activated and idle-stops, and the host has rebooted mid-run once already.
    # Every derived figure is then meaningless, and only some of them look it: a reset
    # yields prompt_tokens = -995000 (obvious) alongside ttft_mean_s = 1.0 and
    # prefix_cache_hit_rate = 1.0 (entirely plausible, entirely wrong). Refuse to report
    # rather than publish a number that cannot be told apart from a real one.
    went_back = sorted(k for k in COUNTERS if after.get(k, 0) < before.get(k, 0))
    if went_back:
        return {"metrics_error": "vllm counter reset between the round's two snapshots -- "
                                 "the server restarted mid-round; model-side numbers for "
                                 "this round are unrecoverable",
                "counters_reset": went_back,
                "reset_detected_on": went_back[0]}

    d = {k: after.get(k, 0) - before.get(k, 0) for k in COUNTERS}
    gen = d["vllm:generation_tokens_total"]
    prompt = d["vllm:prompt_tokens_total"]
    reqs = d["vllm:time_to_first_token_seconds_count"]
    drafts = d["vllm:spec_decode_num_drafts_total"]
    accepted = d["vllm:spec_decode_num_accepted_tokens_total"]
    draft_toks = d["vllm:spec_decode_num_draft_tokens_total"]
    cq = d["vllm:prefix_cache_queries_total"]
    ch = d["vllm:prefix_cache_hits_total"]

    def per(n, x):
        return round(n / x, 3) if x else None

    return {
        "prompt_tokens": int(prompt),              # real tokenizer counts, not chars//4
        "generation_tokens": int(gen),
        "requests": int(reqs),
        "decode_tok_s_wall": per(gen, wall_s),
        "ttft_mean_s": per(d["vllm:time_to_first_token_seconds_sum"], reqs),
        "e2e_mean_s": per(d["vllm:e2e_request_latency_seconds_sum"],
                          d["vllm:e2e_request_latency_seconds_count"]),
        "inter_token_ms": (per(d["vllm:inter_token_latency_seconds_sum"],
                               d["vllm:inter_token_latency_seconds_count"]) or 0) * 1000 or None,
        # acceptance: the number the serving docs use to explain throughput (2.56 vs 2.38)
        "accepted_per_draft_step": per(accepted, drafts),
        "draft_acceptance_rate": per(accepted, draft_toks),
        # prefix cache: production runs 85.8%, and it decides whether TTFT means anything
        "prefix_cache_hit_rate": per(ch, cq),
        "preemptions": int(d["vllm:num_preemptions_total"]),
        "kv_cache_usage_end": after.get("vllm:kv_cache_usage_perc"),
        # server-wide counters are only attributable to us if we were the only client
        "exclusive": before.get("vllm:num_requests_running", 0) == 0,
    }

def stack_fingerprint(url=None, timeout=15):
    """Welcher Serving-Stack laeuft gerade -- nicht nur welches Modell.

    Derselbe Modellname kann auf zwei verschiedenen Staecks laufen: dem Container
    (ghcr.io/syv-ai, vLLM 0.27.1 + Patches, DFlash2) oder der venv-Variante. Ein
    `vllm-switch` bringt die zweite hoch, und im Protokoll stuende weiterhin
    "Qwen3.8-27B-Instruct". Gemeldet von der Box-Session am 2026-09-09, bevor es uns
    getroffen hat: Runde 1 auf dem einen, Runden 2-7 auf dem anderen Stack.

    `vllm:cache_config_info` traegt die Unterscheidung als Labels -- kein Container-Log
    noetig. `kv_cache_size_tokens` ist der Wert, an dem die beiden auseinandergehen.
    """
    import re
    import urllib.request
    url = url or ENDPOINT
    try:
        raw = urllib.request.urlopen(url, timeout=timeout).read().decode()
    except Exception as exc:
        return {"stack_error": f"{type(exc).__name__}: {exc}"}
    m = re.search(r"^vllm:cache_config_info\{([^}]*)\}", raw, re.M)
    if not m:
        return {"stack_error": "vllm:cache_config_info nicht exportiert"}
    labels = dict(re.findall(r'([a-z_]+)="([^"]*)"', m.group(1)))
    keep = ("kv_cache_size_tokens", "cache_dtype", "block_size",
            "enable_prefix_caching", "gpu_memory_utilization",
            "kv_cache_max_concurrency", "num_gpu_blocks")
    return {k: labels[k] for k in keep if k in labels}
