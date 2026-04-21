#!/usr/bin/env python3
"""
Publish code to both GitHub/Railway and Ubuntu runtime with verification.

Workflow:
1) Push current branch to origin.
2) Ensure Ubuntu SSH tunnel is available.
3) Copy runtime files to Ubuntu /tmp.
4) Install them into /opt/telegram-risk-control/app.
5) Optionally compile/restart services.
6) Verify remote sha256 matches local sha256 for every synced file.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path


DEFAULT_RUNTIME_FILES = [
    "main.py",
    "image_fuzzy_blocker.py",
    "semantic_ads.py",
    "railway_failover_runner.py",
    "deploy/release_guard.py",
]


def _log(msg: str) -> None:
    print(f"[publish-everywhere] {msg}", flush=True)


def _run(command: list[str] | str, *, cwd: str | None = None, shell: bool = False) -> subprocess.CompletedProcess:
    display = command if isinstance(command, str) else " ".join(shlex.quote(part) for part in command)
    _log(f"run: {display}")
    return subprocess.run(
        command,
        cwd=cwd,
        shell=shell,
        check=False,
        text=True,
        capture_output=True,
    )


def _must(command: list[str] | str, *, cwd: str | None = None, shell: bool = False) -> subprocess.CompletedProcess:
    proc = _run(command, cwd=cwd, shell=shell)
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"command failed: {command}")
    return proc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_ssh_tunnel(workspace: Path, start_ssh_ps1: Path) -> None:
    _must(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(start_ssh_ps1),
            "-ForceRemote",
            "-TestOnly",
        ],
        cwd=str(workspace),
    )


def _scp_to_tmp(workspace: Path, key_path: Path, local_files: list[Path]) -> None:
    command = [
        "scp",
        "-i",
        str(key_path),
        "-P",
        "10022",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=NUL",
    ]
    command.extend(str(path) for path in local_files)
    command.append("fightclub@127.0.0.1:/tmp/")
    _must(command, cwd=str(workspace))


def _ssh(key_path: Path, remote_command: str, *, workspace: Path) -> subprocess.CompletedProcess:
    return _must(
        [
            "ssh",
            "-i",
            str(key_path),
            "-p",
            "10022",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=NUL",
            "fightclub@127.0.0.1",
            remote_command,
        ],
        cwd=str(workspace),
    )


def _build_remote_install_command(
    runtime_files: list[str],
    *,
    remote_app_dir: str,
    sudo_password: str,
    restart_services: list[str],
) -> str:
    lines: list[str] = []
    parent_dirs = sorted({str(Path(remote_app_dir, rel).parent).replace("\\", "/") for rel in runtime_files})
    for parent in parent_dirs:
        lines.append(f"echo {shlex.quote(sudo_password)} | sudo -S mkdir -p {shlex.quote(parent)}")
    for rel in runtime_files:
        basename = Path(rel).name
        target = str(Path(remote_app_dir, rel)).replace("\\", "/")
        lines.append(
            f"echo {shlex.quote(sudo_password)} | sudo -S install -m 644 /tmp/{shlex.quote(basename)} {shlex.quote(target)}"
        )

    py_targets = [
        str(Path(remote_app_dir, rel)).replace("\\", "/")
        for rel in runtime_files
        if rel.endswith(".py")
    ]
    if py_targets:
        lines.append(
            "/opt/telegram-risk-control/venv/bin/python -m py_compile "
            + " ".join(shlex.quote(item) for item in py_targets)
        )
    if restart_services:
        lines.append(
            "echo "
            + shlex.quote(sudo_password)
            + " | sudo -S systemctl restart "
            + " ".join(shlex.quote(service) for service in restart_services)
        )
    return " && ".join(lines)


def _remote_hashes(runtime_files: list[str], *, workspace: Path, key_path: Path, remote_app_dir: str) -> dict[str, str]:
    python = r"""python3 - <<'PY'
import hashlib
from pathlib import Path
targets = %s
base = Path(%r)
for rel in targets:
    path = base / rel
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{rel}\t{digest}")
PY"""
    remote = _ssh(key_path, python % (repr(runtime_files), remote_app_dir), workspace=workspace)
    values: dict[str, str] = {}
    for line in (remote.stdout or "").splitlines():
        if "\t" not in line:
            continue
        rel, digest = line.split("\t", 1)
        values[rel.strip()] = digest.strip().lower()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Push to origin and sync runtime files to Ubuntu")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--branch", default="main")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--skip-push", action="store_true")
    parser.add_argument("--skip-restart", action="store_true")
    parser.add_argument("--file", dest="files", action="append", help="runtime file relative path")
    parser.add_argument("--sudo-password", default=os.getenv("UBUNTU_SUDO_PASSWORD", ""))
    parser.add_argument(
        "--restart-service",
        dest="restart_services",
        action="append",
        default=["telegram-risk-control.service"],
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    runtime_files = args.files[:] if args.files else list(DEFAULT_RUNTIME_FILES)
    local_paths = [workspace / rel for rel in runtime_files]
    missing = [str(path) for path in local_paths if not path.exists()]
    if missing:
        raise SystemExit(f"missing files: {', '.join(missing)}")
    if not args.sudo_password:
        raise SystemExit("UBUNTU_SUDO_PASSWORD is required")

    key_path = Path(r"C:\Users\mgrtang\.ssh\fightclub_linux_ed25519")
    start_ssh_ps1 = Path(r"C:\Users\mgrtang\Desktop\linux\Start-Ubuntu-SSH.ps1")
    remote_app_dir = "/opt/telegram-risk-control/app"

    if not args.skip_push:
        _must(["git", "push", args.remote, args.branch], cwd=str(workspace))

    _ensure_ssh_tunnel(workspace, start_ssh_ps1)
    _scp_to_tmp(workspace, key_path, local_paths)

    remote_command = _build_remote_install_command(
        runtime_files,
        remote_app_dir=remote_app_dir,
        sudo_password=args.sudo_password,
        restart_services=[] if args.skip_restart else args.restart_services,
    )
    _ssh(key_path, remote_command, workspace=workspace)

    local_hashes = {rel: _sha256(workspace / rel).lower() for rel in runtime_files}
    remote_hashes = _remote_hashes(runtime_files, workspace=workspace, key_path=key_path, remote_app_dir=remote_app_dir)

    mismatches: list[str] = []
    for rel in runtime_files:
        local_digest = local_hashes.get(rel, "")
        remote_digest = remote_hashes.get(rel, "")
        if local_digest != remote_digest:
            mismatches.append(f"{rel}: local={local_digest} remote={remote_digest}")
    if mismatches:
        raise SystemExit("remote hash mismatch:\n" + "\n".join(mismatches))

    _log("sync verified successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
