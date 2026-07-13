# Phase 1: single-tenant end-to-end server.
#   ingest (token auth, byte-offset append) → debounced build (recall_pipeline)
#   → recall MCP server (streamable HTTP).
# Multi-tenant hardening (accounts, isolation, OAuth, locks) is Phase 2.
