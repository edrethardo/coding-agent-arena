"""Span attributes for the benchmark sweeps, and a tracing setup that stays out of the
measurement path.

Two things this exists for.

**Attribution.** A sweep over serving configs or agent rows produces one indistinguishable
pile of spans unless the identity of the run is written onto every span at the time it is
created. Phoenix groups, filters and compares by recorded attributes; nothing can be
re-attributed afterwards. So `attrs()` is not a convenience, it is the difference between a
comparable dataset and a wasted lock window.

**Not contaminating the numbers.** `common.setup_tracing()` registers with `batch=False`,
which exports each span synchronously as it ends. That is right for the interactive eval --
traces show up immediately -- but a benchmark measuring TTFT and tok/s must not put an HTTP
round-trip to Phoenix between its calls. `setup()` here uses a batching processor and
flushes at the end instead.
"""
import os
from contextlib import contextmanager

from opentelemetry import trace

PREFIX = "arena"  # joined with "_", never "." -- see the note in attrs()


def setup(project_name, endpoint=None):
    """Register Phoenix with a BATCHING span processor. Returns the tracer provider.

    Call `provider.force_flush()` before the process exits or the tail of the run is lost --
    that is the price of keeping the exporter off the hot path.
    """
    from phoenix.otel import register
    from openinference.instrumentation.openai import OpenAIInstrumentor

    endpoint = endpoint or os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    provider = register(
        project_name=project_name,
        endpoint=f"{endpoint}/v1/traces",
        auto_instrument=False,
        batch=True,          # <-- the one difference from common.setup_tracing()
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    return provider


def attrs(config=None, spec=None, ctx=None, kv_dtype=None, dflash_tokens=None,
          max_model_len=None, depth_tier=None, cache_state=None, harness=None,
          model=None, game=None, round_no=None, **extra):
    """Build the `arena_*` attribute dict.

    **Underscores, not dots, and this is load-bearing.** Measured against this Phoenix on
    2026-09-08: a dotted `arena.config` is nested into a single `attributes.arena` object,
    and `SpanQuery().where("attributes['arena.config'] == ...")` then matches **0 spans and
    raises nothing**. The flat `arena_config` becomes its own column and filters correctly.
    A silent empty result set is the worst available failure here -- it reads as "the run
    produced nothing" rather than as "the query is wrong".

    Omitted values are left out, not written as None -- Phoenix filters on presence, so a
    None would create a bucket meaning "unknown" and one meaning "absent" and merge them.

    `cache_state` is 'cold' or 'warm': with an 85.8% prefix-cache hit rate in production, a
    timing without this field cannot be interpreted at all.
    """
    out = {}
    for key, val in (("config", config), ("spec", spec), ("ctx", ctx),
                     ("kv_dtype", kv_dtype), ("dflash_tokens", dflash_tokens),
                     ("max_model_len", max_model_len), ("depth_tier", depth_tier),
                     ("cache_state", cache_state), ("harness", harness),
                     ("model", model), ("game", game), ("round", round_no)):
        if val is not None:
            out[f"{PREFIX}_{key}"] = val
    for key, val in extra.items():
        if val is not None:
            out[f"{PREFIX}_{key}"] = val
    return out


def measurements(ctx_tokens_in=None, ctx_tokens_out=None, ttft_s=None,
                 decode_tok_s=None, accepted_per_step=None, ladder_rung=None,
                 tests_passed=None, tests_total=None):
    """The numbers. `accepted_per_step` matters more than it looks: acceptance is what the
    serving docs use to explain *why* a config is fast (2.56 vs 2.38; 14.19 vs 3.81), so a
    trace without it records the effect and hides the cause.
    """
    return attrs(ctx_tokens_in=ctx_tokens_in, ctx_tokens_out=ctx_tokens_out,
                 ttft_s=ttft_s, decode_tok_s=decode_tok_s,
                 accepted_per_step=accepted_per_step, ladder_rung=ladder_rung,
                 tests_passed=tests_passed, tests_total=tests_total)


@contextmanager
def span(name, tracer=None, **attributes):
    """A span carrying `arena_*` attributes, marked ERROR on exception.

    Nesting mirrors the experiment: sweep -> config -> task/round -> call, so the trace tree
    in Phoenix reads as the experimental design.
    """
    tracer = tracer or trace.get_tracer(__name__)
    with tracer.start_as_current_span(name) as sp:
        for key, val in attrs(**attributes).items():
            sp.set_attribute(key, val)
        try:
            yield sp
        except Exception as exc:
            sp.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            sp.record_exception(exc)
            raise
