# -*- coding: utf-8 -*-
"""Tenant store — SQLite locally, D1 in production (same SQL dialect).

Replaces Phase 1's tenants.json. Tokens are stored as SHA-256 hashes; the
plaintext token is shown once at creation. Schema is deliberately D1-portable
(D1 *is* SQLite): the same CREATE TABLE runs unchanged via wrangler migrations.

CLI:
  python -m recall_server.tenants add junha --tz Asia/Seoul --lang Korean
  python -m recall_server.tenants list
  python -m recall_server.tenants revoke <tenant_id>
"""
from __future__ import annotations

import os
import json
import hashlib
import secrets
import sqlite3
import argparse
from zoneinfo import ZoneInfo

from recall_pipeline.context import TenantContext

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   TEXT PRIMARY KEY,
    tz          TEXT NOT NULL DEFAULT 'UTC',
    lang        TEXT NOT NULL DEFAULT 'English',
    model       TEXT,
    backfill_floor TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at  TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash  TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(tenant_id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at  TEXT
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TenantStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- auth path (hot) ----

    def resolve(self, token: str, data_root: str) -> "tuple[str, TenantContext] | None":
        """token → (tenant_id, ctx), or None. Row lookup by hash — O(1)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT t.* FROM tokens k JOIN tenants t ON t.tenant_id = k.tenant_id "
                "WHERE k.token_hash = ? AND k.revoked_at IS NULL AND t.deleted_at IS NULL",
                (_hash(token),)).fetchone()
        if row is None:
            return None
        ctx = TenantContext(
            data_root=os.path.join(data_root, row["tenant_id"]),
            tz=ZoneInfo(row["tz"]),
            summary_lang=row["lang"],
        )
        if row["model"]:
            ctx.model = row["model"]
        if row["backfill_floor"]:
            ctx.backfill_floor = row["backfill_floor"]
        return row["tenant_id"], ctx

    # ---- management ----

    def add(self, tenant_id: str, tz: str = "UTC", lang: str = "English",
            model: str | None = None, backfill_floor: str | None = None) -> str:
        """Create tenant + one token. Returns the plaintext token (shown once)."""
        token = "rcl_" + secrets.token_hex(24)
        with self._conn() as c:
            c.execute("INSERT INTO tenants (tenant_id, tz, lang, model, backfill_floor) "
                      "VALUES (?, ?, ?, ?, ?)",
                      (tenant_id, tz, lang, model, backfill_floor))
            c.execute("INSERT INTO tokens (token_hash, tenant_id) VALUES (?, ?)",
                      (_hash(token), tenant_id))
        return token

    def mark_deleted(self, tenant_id: str):
        """Soft-delete the account and revoke its tokens (data wipe is the
        caller's job — local dir + R2 prefix)."""
        with self._conn() as c:
            c.execute("UPDATE tenants SET deleted_at = datetime('now') "
                      "WHERE tenant_id = ? AND deleted_at IS NULL", (tenant_id,))
            c.execute("UPDATE tokens SET revoked_at = datetime('now') "
                      "WHERE tenant_id = ? AND revoked_at IS NULL", (tenant_id,))

    def list(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT tenant_id, tz, lang, created_at, deleted_at FROM tenants")]

    # ---- one-time migration from Phase 1 tenants.json ----

    def import_json(self, path: str) -> int:
        """Import Phase 1 tenants.json ({token: {tenant_id, tz, lang, ...}}).
        Keeps the existing plaintext tokens working (stores their hashes)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return 0
        n = 0
        with self._conn() as c:
            for token, t in raw.items():
                exists = c.execute("SELECT 1 FROM tenants WHERE tenant_id = ?",
                                   (t["tenant_id"],)).fetchone()
                if not exists:
                    c.execute("INSERT INTO tenants (tenant_id, tz, lang, model, backfill_floor) "
                              "VALUES (?, ?, ?, ?, ?)",
                              (t["tenant_id"], t.get("tz", "UTC"), t.get("lang", "English"),
                               t.get("model"), t.get("backfill_floor")))
                c.execute("INSERT OR IGNORE INTO tokens (token_hash, tenant_id) VALUES (?, ?)",
                          (_hash(token), t["tenant_id"]))
                n += 1
        return n


def main():
    ap = argparse.ArgumentParser(prog="recall_server.tenants")
    ap.add_argument("--db", default=os.environ.get("RECALL_TENANTS_DB")
                    or os.path.expanduser("~/recall-data/tenants.db"))
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add")
    p.add_argument("tenant_id")
    p.add_argument("--tz", default="UTC")
    p.add_argument("--lang", default="English")
    p.add_argument("--model")
    p.add_argument("--backfill-floor")

    sub.add_parser("list")

    p = sub.add_parser("revoke")
    p.add_argument("tenant_id")

    p = sub.add_parser("import-json")
    p.add_argument("path")

    args = ap.parse_args()
    store = TenantStore(args.db)
    if args.command == "add":
        token = store.add(args.tenant_id, tz=args.tz, lang=args.lang,
                          model=args.model, backfill_floor=args.backfill_floor)
        print("token (shown once): %s" % token)
    elif args.command == "list":
        for row in store.list():
            print(row)
    elif args.command == "revoke":
        store.mark_deleted(args.tenant_id)
        print("revoked %s" % args.tenant_id)
    elif args.command == "import-json":
        print("imported %d token(s)" % store.import_json(args.path))


if __name__ == "__main__":
    main()
