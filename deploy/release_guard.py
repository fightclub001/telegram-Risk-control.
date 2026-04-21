#!/usr/bin/env python3
"""
Release guard for admin-state durability.

Workflow:
1) Snapshot local admin-state files.
2) Execute release command.
3) Verify local and remote state consistency.
4) Auto-restore snapshot + re-push state bundle if verification fails.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_FILES = (
    "config.json",
    "image_fuzzy_blocks.json",
    "semantic_ads/semantic_ads.db",
)


def _log(msg: str) -> None:
    print(f"[release-guard] {msg}", flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_manifest(data_dir: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for rel in STATE_FILES:
        abs_path = data_dir / rel
        if not abs_path.exists():
            continue
        stat = abs_path.stat()
        files[rel] = {
            "size": int(stat.st_size),
            "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            "sha256": _sha256(abs_path),
        }
    return {
        "schema": 1,
        "generated_at_ns": time.time_ns(),
        "files": files,
    }


def _snapshot_state(data_dir: Path, backup_dir: Path) -> tuple[Path, dict[str, Any]]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = backup_dir / f"admin-state-{ts}.zip"
    manifest = _build_manifest(data_dir)

    with zipfile.ZipFile(snapshot_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in sorted(manifest["files"].keys()):
            zf.write(data_dir / rel, arcname=rel)

    _log(f"snapshot created: {snapshot_path}")
    return snapshot_path, manifest


def _restore_snapshot(snapshot_path: Path, data_dir: Path) -> dict[str, Any]:
    restored = []
    with zipfile.ZipFile(snapshot_path, "r") as zf:
        raw_manifest = json.loads(zf.read("_manifest.json").decode("utf-8"))
        files = raw_manifest.get("files", {})
        for rel in sorted(files.keys()):
            if rel not in STATE_FILES:
                continue
            target = data_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_bytes(zf.read(rel))
            os.replace(temp, target)
            mtime_ns = int(files[rel].get("mtime_ns", 0) or 0)
            if mtime_ns > 0:
                try:
                    os.utime(target, ns=(mtime_ns, mtime_ns))
                except OSError:
                    pass
            restored.append(rel)
    _log(f"snapshot restored files: {', '.join(restored) if restored else '(none)'}")
    return _build_manifest(data_dir)


def _request_json(url: str, token: str, timeout_sec: int) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _request_put_bytes(url: str, token: str, payload: bytes, timeout_sec: int, content_type: str) -> dict[str, Any]:
    req = urllib.request.Request(url, data=payload, method="PUT")
    req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        body = resp.read()
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "invalid_json_response"}


def _build_bundle_bytes(data_dir: Path) -> bytes:
    manifest = _build_manifest(data_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in sorted(manifest["files"].keys()):
            zf.write(data_dir / rel, arcname=rel)
    return buf.getvalue()


def _push_bundle(state_bundle_url: str, token: str, data_dir: Path, timeout_sec: int) -> bool:
    payload = _build_bundle_bytes(data_dir)
    rsp = _request_put_bytes(
        state_bundle_url,
        token,
        payload,
        timeout_sec,
        content_type="application/zip",
    )
    return bool(rsp.get("ok"))


def _verify_remote(
    local_manifest: dict[str, Any],
    state_manifest_url: str,
    token: str,
    timeout_sec: int,
    retries: int,
    retry_interval_sec: int,
) -> tuple[bool, str]:
    local_files = local_manifest.get("files", {})
    for attempt in range(1, retries + 1):
        try:
            remote = _request_json(state_manifest_url, token, timeout_sec)
            remote_files = remote.get("files", {}) if isinstance(remote, dict) else {}
            mismatches = []
            for rel, meta in local_files.items():
                remote_meta = remote_files.get(rel)
                if not isinstance(remote_meta, dict):
                    mismatches.append(f"{rel}:missing")
                    continue
                if str(remote_meta.get("sha256", "")) != str(meta.get("sha256", "")):
                    mismatches.append(f"{rel}:sha256_mismatch")
            if not mismatches:
                return True, "ok"
            reason = ", ".join(mismatches)
        except urllib.error.HTTPError as exc:
            reason = f"http_{exc.code}"
        except Exception as exc:
            reason = f"{type(exc).__name__}:{exc}"

        if attempt < retries:
            _log(f"remote verify attempt {attempt}/{retries} failed: {reason}, retrying...")
            time.sleep(retry_interval_sec)
            continue
        return False, reason
    return False, "unknown"


def _run_command(raw_command: str) -> int:
    _log(f"running command: {raw_command}")
    if os.name == "nt":
        proc = subprocess.run(raw_command, shell=True)
    else:
        proc = subprocess.run(shlex.split(raw_command), shell=False)
    return int(proc.returncode)


def _derive_state_urls(config_sync_url: str) -> tuple[str, str]:
    base = config_sync_url.rstrip("/")
    if base.endswith("/config"):
        base = base[:-7]
    return f"{base}/state-manifest", f"{base}/state-bundle"


def main() -> int:
    parser = argparse.ArgumentParser(description="Release guard with snapshot/verify/rollback")
    parser.add_argument("--data-dir", default=os.getenv("DATA_DIR", "/opt/telegram-risk-control/data"))
    parser.add_argument(
        "--backup-dir",
        default=os.getenv("RELEASE_BACKUP_DIR", ""),
        help="default: <data-dir>/backups/release-guard",
    )
    parser.add_argument("--deploy-cmd", required=True, help="release command")
    parser.add_argument("--rollback-cmd", default="", help="optional command after restore")
    parser.add_argument("--config-sync-token", default=os.getenv("CONFIG_SYNC_TOKEN", ""))
    parser.add_argument("--state-manifest-url", default=os.getenv("STATE_SYNC_MANIFEST_URL", ""))
    parser.add_argument("--state-bundle-url", default=os.getenv("STATE_SYNC_BUNDLE_URL", ""))
    parser.add_argument("--config-sync-url", default=os.getenv("CONFIG_SYNC_URL", ""))
    parser.add_argument("--remote-timeout-sec", type=int, default=8)
    parser.add_argument("--verify-retries", type=int, default=6)
    parser.add_argument("--verify-retry-interval-sec", type=int, default=5)
    parser.add_argument("--skip-remote-verify", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        _log(f"data dir not found: {data_dir}")
        return 2

    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else data_dir / "backups" / "release-guard"
    state_manifest_url = args.state_manifest_url
    state_bundle_url = args.state_bundle_url

    if args.config_sync_url and (not state_manifest_url or not state_bundle_url):
        inferred_manifest_url, inferred_bundle_url = _derive_state_urls(args.config_sync_url)
        state_manifest_url = state_manifest_url or inferred_manifest_url
        state_bundle_url = state_bundle_url or inferred_bundle_url

    snapshot_path, before_manifest = _snapshot_state(data_dir, backup_dir)
    before_files = set(before_manifest.get("files", {}).keys())
    if not before_files:
        _log("warning: snapshot is empty, no admin-state files detected")

    release_code = _run_command(args.deploy_cmd)
    if release_code != 0:
        _log(f"deploy command failed with code {release_code}, starting rollback")
        _restore_snapshot(snapshot_path, data_dir)
        if args.rollback_cmd:
            _run_command(args.rollback_cmd)
        if state_bundle_url and args.config_sync_token:
            _push_bundle(state_bundle_url, args.config_sync_token, data_dir, args.remote_timeout_sec)
        return release_code

    after_manifest = _build_manifest(data_dir)
    after_files = set(after_manifest.get("files", {}).keys())
    if before_files - after_files:
        _log(f"post-deploy local verification failed: missing files {sorted(before_files - after_files)}")
        _restore_snapshot(snapshot_path, data_dir)
        if args.rollback_cmd:
            _run_command(args.rollback_cmd)
        if state_bundle_url and args.config_sync_token:
            _push_bundle(state_bundle_url, args.config_sync_token, data_dir, args.remote_timeout_sec)
        return 3

    if not args.skip_remote_verify:
        if not state_manifest_url or not state_bundle_url or not args.config_sync_token:
            _log("remote verify enabled but missing state url/token config")
            _restore_snapshot(snapshot_path, data_dir)
            if args.rollback_cmd:
                _run_command(args.rollback_cmd)
            return 4

        if not _push_bundle(state_bundle_url, args.config_sync_token, data_dir, args.remote_timeout_sec):
            _log("failed to push local state bundle to remote, starting rollback")
            _restore_snapshot(snapshot_path, data_dir)
            if args.rollback_cmd:
                _run_command(args.rollback_cmd)
            _push_bundle(state_bundle_url, args.config_sync_token, data_dir, args.remote_timeout_sec)
            return 5

        ok, reason = _verify_remote(
            after_manifest,
            state_manifest_url,
            args.config_sync_token,
            args.remote_timeout_sec,
            args.verify_retries,
            args.verify_retry_interval_sec,
        )
        if not ok:
            _log(f"remote verify failed: {reason}, starting rollback")
            _restore_snapshot(snapshot_path, data_dir)
            if args.rollback_cmd:
                _run_command(args.rollback_cmd)
            _push_bundle(state_bundle_url, args.config_sync_token, data_dir, args.remote_timeout_sec)
            return 6

    _log(f"release succeeded, snapshot kept at: {snapshot_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
