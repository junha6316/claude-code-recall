# -*- coding: utf-8 -*-
"""R2 persistence layer (S3-compatible; works against R2, MinIO, moto).

Cloudflare Containers have ephemeral disks (snapshots not broadly rolled out),
so R2 is the only durable store. Concurrency model: **one active container
instance per tenant** (the fronting Worker routes each tenant to its own
Durable Object → same instance), so the local disk is the cache-of-record
while awake and R2 is the recovery point across sleeps:

  - cold start (tenant dir absent locally) → pull the tenant prefix from R2
  - after each ingest append              → push that transcript file
  - after each build                      → push changed outputs (timeline md,
                                            threads, registry, cursors, budgets)

Push is whole-file (objects are MB-scale). Pull trusts R2 blindly on cold
start; while warm, local wins — R2 is never read back over live local state.

Disabled entirely when RECALL_R2_BUCKET is unset (Phase 1 local mode keeps
working with zero config).
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from recall_pipeline.context import TenantContext

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:      # boto3 only needed when R2 is configured
    boto3 = None
    BotoConfig = None

# Cold-start restores download the whole tenant prefix (thousands of transcript
# objects); sequential GETs made wake-up O(corpus) in wall-clock (2026-07-18,
# 40+ min observed). Bounded fan-out keeps it minutes.
PULL_WORKERS = int(os.environ.get("RECALL_PULL_WORKERS", "8"))


@dataclass
class R2Config:
    bucket: str
    endpoint_url: str
    access_key: str
    secret_key: str

    @classmethod
    def from_env(cls) -> "R2Config | None":
        bucket = os.environ.get("RECALL_R2_BUCKET")
        if not bucket:
            return None
        return cls(
            bucket=bucket,
            endpoint_url=os.environ["RECALL_R2_ENDPOINT"],
            access_key=os.environ["RECALL_R2_ACCESS_KEY"],
            secret_key=os.environ["RECALL_R2_SECRET_KEY"],
        )


class R2Sync:
    """Blocking S3 client wrapper — call via asyncio.to_thread from the app."""

    def __init__(self, cfg: R2Config):
        if boto3 is None:
            raise RuntimeError("boto3 is required when RECALL_R2_BUCKET is set")
        self.cfg = cfg
        self._s3 = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.access_key,
            aws_secret_access_key=cfg.secret_key,
            # R2 ignores region; boto3 wants one.
            region_name="auto",
            # pull_tenant downloads with PULL_WORKERS threads; the default pool
            # (10) would serialize them at the connection layer.
            config=BotoConfig(max_pool_connections=PULL_WORKERS + 2),
        )

    # ---- keys ----

    @staticmethod
    def _key(tenant_id: str, relpath: str) -> str:
        return "tenants/%s/%s" % (tenant_id, relpath)

    @staticmethod
    def _rel(tenant_id: str, key: str) -> str:
        return key[len("tenants/%s/" % tenant_id):]

    # ---- push ----

    def push_file(self, tenant_id: str, ctx: TenantContext, relpath: str):
        """Upload one file under the tenant's data root (relpath is
        data_root-relative, e.g. 'projects/<dir>/<sid>.jsonl')."""
        local = os.path.join(ctx.data_root, relpath)
        self._s3.upload_file(local, self.cfg.bucket, self._key(tenant_id, relpath))

    def push_outputs(self, tenant_id: str, ctx: TenantContext):
        """Upload everything a build can change: work-timeline/** plus the
        tenant-root state/budget json files. mtime-based skip keeps re-pushes
        cheap; the manifest of previously pushed mtimes lives locally (it is
        itself rebuilt on cold pull, so a lost manifest just means one full
        re-push, never data loss)."""
        manifest_path = os.path.join(ctx.data_root, ".r2_push_manifest.json")
        import json
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

        pushed = 0
        for relpath in self._output_relpaths(ctx):
            local = os.path.join(ctx.data_root, relpath)
            try:
                mtime = os.path.getmtime(local)
            except OSError:
                continue
            if manifest.get(relpath) == mtime:
                continue
            self._s3.upload_file(local, self.cfg.bucket, self._key(tenant_id, relpath))
            manifest[relpath] = mtime
            pushed += 1

        tmp = manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        os.replace(tmp, manifest_path)
        return pushed

    @staticmethod
    def _output_relpaths(ctx: TenantContext):
        roots = [ctx.output_dir]
        for root in roots:
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    if name.endswith(".tmp"):
                        continue
                    full = os.path.join(dirpath, name)
                    yield os.path.relpath(full, ctx.data_root)
        for name in ("state.json", "llm_budget.json", "ingest_budget.json"):
            if os.path.exists(os.path.join(ctx.data_root, name)):
                yield name

    # ---- pull ----

    @staticmethod
    def _warm_marker(ctx: TenantContext) -> str:
        return os.path.join(ctx.data_root, ".r2_warm")

    def pull_tenant(self, tenant_id: str, ctx: TenantContext) -> int:
        """Full restore of the tenant prefix into the local data root.
        Called on cold start only (local dir absent/empty) — while warm the
        local disk is authoritative and R2 is never read back.

        The warm-marker is written only after *all* objects have downloaded, so
        a pull that dies partway (network drop, disk full) leaves the tenant
        cold: the next request re-runs the full restore instead of serving a
        half-populated tree. Downloads are whole-file overwrites, so retrying is
        idempotent."""
        paginator = self._s3.get_paginator("list_objects_v2")
        prefix = "tenants/%s/" % tenant_id
        keys = [obj["Key"]
                for page in paginator.paginate(Bucket=self.cfg.bucket, Prefix=prefix)
                for obj in page.get("Contents", [])]

        def fetch(key):
            local = os.path.join(ctx.data_root, self._rel(tenant_id, key))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            self._s3.download_file(self.cfg.bucket, key, local)

        # Concurrent downloads: boto3 clients are thread-safe, writes land in
        # distinct files. Any failure propagates and the warm-marker is not
        # written, preserving the all-or-cold contract below.
        with ThreadPoolExecutor(max_workers=PULL_WORKERS) as pool:
            for _ in pool.map(fetch, keys):
                pass
        os.makedirs(ctx.data_root, exist_ok=True)   # empty tenant (0 objects)
        with open(self._warm_marker(ctx), "w", encoding="utf-8") as f:
            f.write("")
        return len(keys)

    def is_cold(self, ctx: TenantContext) -> bool:
        """Cold = this disk has not yet completed an R2 restore for the tenant.
        Keyed on the warm-marker (written by pull_tenant on full success) rather
        than the presence of data dirs, so a partial restore reads as cold and a
        genuinely empty tenant is pulled only once. The marker lives on the same
        ephemeral disk as the data it guards, so a disk wipe clears both together
        (never a marker without its data)."""
        return not os.path.exists(self._warm_marker(ctx))

    # ---- delete ----

    def delete_tenant(self, tenant_id: str) -> int:
        """Erase every object under the tenant prefix (self-serve deletion)."""
        paginator = self._s3.get_paginator("list_objects_v2")
        prefix = "tenants/%s/" % tenant_id
        count = 0
        for page in paginator.paginate(Bucket=self.cfg.bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self._s3.delete_objects(Bucket=self.cfg.bucket, Delete={"Objects": keys})
                count += len(keys)
        return count
