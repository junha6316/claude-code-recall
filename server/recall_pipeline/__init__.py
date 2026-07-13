# Server-side port of the local work-timeline pipeline (scripts/).
# Ported as a separate module — the local scripts are untouched (see plan:
# "로컬 그대로 둠"). Key differences from the local scripts:
#   - No module-level path globals: every function takes a TenantContext.
#   - LLM calls go through the Anthropic API (llm.complete), replacing BOTH
#     local run_claude copies (work-timeline.py:264 and rollup.py:85).
#   - Failure semantics fixed: a failed summary window does NOT advance the
#     cursor (the local pipeline fail-forwards to an ai-title fallback).
#   - No hook/debounce/self-noise re-entry logic (server has no Stop hooks).
