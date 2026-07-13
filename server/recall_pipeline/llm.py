# -*- coding: utf-8 -*-
"""Anthropic API backend for pipeline summaries.

Replaces BOTH local run_claude copies (work-timeline.py:264-289 and
work-timeline-rollup.py:85-105 — the review found they are separate
implementations, not one choke point). No headless `claude -p`, so the
rc=1 concurrency failures and the CCRECALL_INTERNAL re-entry guard are gone.

Errors raise LLMError — callers decide whether to stop the cursor (timeline,
threads) or skip and retry next run (consolidate synthesis).
"""
import anthropic

_client = None

# Optional pre-call hook — the server's build worker installs a per-tenant
# budget check here (raises LLMError when the daily cap is hit, which stops
# the cursor exactly like any other LLM failure and resumes next run/day).
# Phase 1 runs builds one-at-a-time, so a module-level hook is sufficient.
before_call = None


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    """Raised by a before_call budget hook — inherits LLMError so every
    pipeline stage treats it as a retryable stop, not a crash."""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
    return _client


def complete(prompt: str, *, model: str, timeout: float = 180.0,
             max_tokens: int = 8192) -> str:
    """Single prompt → text completion. Raises LLMError on API failure or
    empty output (empty means the window would silently lose its summary)."""
    if before_call is not None:
        before_call()
    try:
        response = _get_client().with_options(timeout=timeout).messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AnthropicError as e:
        # Catch the SDK root (APIError/connection/timeout/status + client-side
        # errors like a missing key), not just APIError. consolidate.synthesize
        # only catches LLMError; a raw AnthropicError escaping here would abort
        # run_consolidate before it saves the syntheses that already succeeded.
        raise LLMError("anthropic API call failed: %s" % e) from e
    text = "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not text:
        raise LLMError("anthropic API returned no text (stop_reason=%s)"
                       % response.stop_reason)
    return text
