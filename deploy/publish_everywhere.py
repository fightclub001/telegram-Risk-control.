#!/usr/bin/env python3
"""
Publish runtime updates to both GitHub/Railway and the Ubuntu runtime with safeguards.

Workflow:
1) Optionally auto-commit only the selected runtime files.
2) Push the selected branch to origin (GitHub -> Railway auto deploy chain).
3) Ensure Ubuntu SSH tunnel is reachable.
4) Backup current Ubuntu runtime copies of the selected files.
5) Copy local runtime files to Ubuntu and install them.
6) Compile, restart services (or fall back to process rotation), and verify health.
7) Verify remote sha256 matches local sha256 for every synced file.
8) If remote install/restart/verify fails, restore the Ubuntu backup and re-check health.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_RUNTIME_FILES = [
    "main.py",
    "image_fuzzy_blocker.py",
    "semantic_ads.py",
    "railway_failover_runner.py",
    "deploy/release_guard.py",
]

DEFAULT_RESTART_SERVICES = [
    "telegram-risk-control.service",
    "telegram-risk-health.service",
]

DEFAULT_RESTART_PATTERNS = [
    "/opt/telegram-risk-control/app/main.py",
    "/opt/telegram-risk-control/app/deploy/ubuntu/telegram_risk_health.py",
]

DEFAULT_HEALTHCHECK_URL = "http://127.0.0.1:18080/status"


def _log(msg: str) -> None:
    print(f"[publish-everywhere] {msg}", flush=True)


def _run(
    command: list[str] | str,
    *,
    cwd: str | None = None,
    shell: bool = False,
    display_override: str | None = None,
) -> subprocess.CompletedProcess:
    display = display_override
    if display is None:
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


def _must(
    command: list[str] | str,
    *,
    cwd: str | None = None,
    shell: bool = False,
    display_override: str | None = None,
) -> subprocess.CompletedProcess:
    proc = _run(command, cwd=cwd, shell=shell, display_override=display_override)
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"command failed: {display_override or command}")
    return proc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_unique_basenames(runtime_files: list[str]) -> None:
    seen: dict[str, str] = {}
    collisions: list[str] = []
    for rel in runtime_files:
        base = Path(rel).name
        previous = seen.get(base)
        if previous and previous != rel:
            collisions.append(f"{previous} <-> {rel}")
        seen[base] = rel
    if collisions:
        raise SystemExit("duplicate runtime file basenames are unsupported:\n" + "\n".join(collisions))


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


def _git_path_status(workspace: Path, runtime_files: list[str]) -> list[str]:
    proc = _must(["git", "status", "--short", "--", *runtime_files], cwd=str(workspace))
    return [line.rstrip("\n") for line in (proc.stdout or "").splitlines() if line.strip()]


def _maybe_commit_runtime_files(workspace: Path, runtime_files: list[str], commit_message: str) -> bool:
    if not _git_path_status(workspace, runtime_files):
        _log("selected runtime files already match HEAD; no auto-commit needed")
        return False
    _must(["git", "add", "--", *runtime_files], cwd=str(workspace))
    _must(
        ["git", "commit", "--only", "-m", commit_message, "--", *runtime_files],
        cwd=str(workspace),
        display_override=f"git commit --only -m {shlex.quote(commit_message)} -- <runtime-files>",
    )
    return True


def _scp_to_tmp(workspace: Path, key_path: Path, local_files: list[Path], remote_temp_dir: str) -> None:
    _must(
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
            f"mkdir -p {shlex.quote(remote_temp_dir)}",
        ],
        cwd=str(workspace),
        display_override=f"ssh fightclub@127.0.0.1 mkdir -p {remote_temp_dir}",
    )

    for local_path in local_files:
        remote_path = f"{remote_temp_dir}/{local_path.name}"
        _must(
            [
                "scp",
                "-i",
                str(key_path),
                "-P",
                "10022",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=NUL",
                str(local_path),
                f"fightclub@127.0.0.1:{remote_path}",
            ],
            cwd=str(workspace),
            display_override=f"scp {local_path.name} -> {remote_path}",
        )


def _ssh(
    key_path: Path,
    remote_command: str,
    *,
    workspace: Path,
    display_override: str | None = None,
) -> subprocess.CompletedProcess:
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
        display_override=display_override,
    )


def _remote_python(
    key_path: Path,
    workspace: Path,
    script: str,
    *,
    display_override: str,
) -> subprocess.CompletedProcess:
    return _ssh(
        key_path,
        "python3 - <<'PY'\n" + script + "\nPY",
        workspace=workspace,
        display_override=display_override,
    )


def _remote_preflight(key_path: Path, workspace: Path, remote_app_dir: str) -> dict[str, object]:
    script = f"""
import json
import os
import subprocess
from pathlib import Path

app_dir = Path({remote_app_dir!r})
print(json.dumps({{
    "app_dir_exists": app_dir.exists(),
    "app_dir_writable": os.access(app_dir, os.W_OK),
    "sudo_nopasswd": subprocess.run(
        ["sudo", "-n", "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0,
}}, ensure_ascii=True))
"""
    proc = _remote_python(
        key_path,
        workspace,
        script,
        display_override="ssh fightclub@127.0.0.1 python preflight",
    )
    payload = (proc.stdout or "").strip().splitlines()
    if not payload:
        raise RuntimeError("remote preflight returned empty output")
    return json.loads(payload[-1])


def _wrap_bash(script: str, *, use_sudo: bool, sudo_password: str) -> str:
    if use_sudo:
        if sudo_password:
            return (
                f"printf '%s\\n' {shlex.quote(sudo_password)} | "
                f"sudo -S -p '' bash -lc {shlex.quote(script)}"
            )
        return f"sudo -n bash -lc {shlex.quote(script)}"
    return f"bash -lc {shlex.quote(script)}"


def _build_remote_install_script(
    runtime_files: list[str],
    *,
    remote_app_dir: str,
    remote_temp_dir: str,
    remote_backup_dir: str,
) -> str:
    lines = [
        "set -euo pipefail",
        f"mkdir -p {shlex.quote(remote_backup_dir)}",
    ]
    for rel in runtime_files:
        target = str(Path(remote_app_dir, rel)).replace("\\", "/")
        parent = str(Path(target).parent).replace("\\", "/")
        backup = str(Path(remote_backup_dir, rel)).replace("\\", "/")
        backup_parent = str(Path(backup).parent).replace("\\", "/")
        source = f"{remote_temp_dir}/{Path(rel).name}"
        lines.append(f"mkdir -p {shlex.quote(parent)}")
        lines.append(f"mkdir -p {shlex.quote(backup_parent)}")
        lines.append(
            f"if [ -f {shlex.quote(target)} ]; then cp -p {shlex.quote(target)} {shlex.quote(backup)}; fi"
        )
        lines.append(f"install -m 644 {shlex.quote(source)} {shlex.quote(target)}")
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
    return "\n".join(lines)


def _build_remote_restore_script(
    runtime_files: list[str],
    *,
    remote_app_dir: str,
    remote_backup_dir: str,
) -> str:
    lines = ["set -euo pipefail"]
    for rel in runtime_files:
        target = str(Path(remote_app_dir, rel)).replace("\\", "/")
        backup = str(Path(remote_backup_dir, rel)).replace("\\", "/")
        lines.append(
            f"if [ -f {shlex.quote(backup)} ]; then install -m 644 {shlex.quote(backup)} {shlex.quote(target)}; fi"
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
    return "\n".join(lines)


def _remote_restart(
    key_path: Path,
    workspace: Path,
    *,
    restart_services: list[str],
    restart_patterns: list[str],
    sudo_password: str,
    use_sudo: bool,
) -> None:
    if restart_services:
        service_cmd = "systemctl restart " + " ".join(shlex.quote(service) for service in restart_services)
        command = _wrap_bash(service_cmd, use_sudo=use_sudo, sudo_password=sudo_password)
        _ssh(
            key_path,
            command,
            workspace=workspace,
            display_override="ssh fightclub@127.0.0.1 restart systemd services",
        )
        return
    if not restart_patterns:
        _log("restart skipped: no services and no fallback patterns configured")
        return
    script = f"""
import os
import signal
import time
from pathlib import Path

patterns = {restart_patterns!r}
current_pid = os.getpid()
killed = []

for proc_dir in Path("/proc").iterdir():
    if not proc_dir.name.isdigit():
        continue
    pid = int(proc_dir.name)
    if pid == current_pid:
        continue
    try:
        raw = (proc_dir / "cmdline").read_bytes()
    except OSError:
        continue
    try:
        exe_path = os.readlink(proc_dir / "exe")
    except OSError:
        continue
    if not raw:
        continue
    cmdline = raw.replace(b"\\x00", b" ").decode("utf-8", errors="ignore")
    if not cmdline:
        continue
    exe_name = os.path.basename(exe_path)
    if not (exe_name.startswith("python") or exe_path == "/opt/telegram-risk-control/venv/bin/python"):
        continue
    if any(pattern in cmdline for pattern in patterns):
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append((pid, cmdline))
        except ProcessLookupError:
            pass

time.sleep(8)
for pid, cmdline in killed:
    print(f"killed {{pid}} {{cmdline}}")
"""
    _remote_python(
        key_path,
        workspace,
        script,
        display_override="ssh fightclub@127.0.0.1 rotate user-owned service processes",
    )


def _remote_healthcheck(
    key_path: Path,
    workspace: Path,
    *,
    healthcheck_url: str,
    timeout_sec: int,
    interval_sec: int,
) -> None:
    script = f"""
import json
import sys
import time
import urllib.error
import urllib.request

url = {healthcheck_url!r}
deadline = time.time() + {int(timeout_sec)}
interval_sec = max(1, int({int(interval_sec)}))
last = "no-response"

while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        data = json.loads(body)
        if bool(data.get("healthy", False)):
            print(body)
            sys.exit(0)
        last = body
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        last = f"{{type(exc).__name__}}: {{exc}}"
    time.sleep(interval_sec)

print(last, file=sys.stderr)
sys.exit(1)
"""
    _remote_python(
        key_path,
        workspace,
        script,
        display_override=f"ssh fightclub@127.0.0.1 wait for health {healthcheck_url}",
    )


def _remote_hashes(runtime_files: list[str], *, workspace: Path, key_path: Path, remote_app_dir: str) -> dict[str, str]:
    script = f"""
import hashlib
from pathlib import Path

targets = {runtime_files!r}
base = Path({remote_app_dir!r})
for rel in targets:
    path = base / rel
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{{rel}}\\t{{digest}}")
"""
    remote = _remote_python(
        key_path,
        workspace,
        script,
        display_override="ssh fightclub@127.0.0.1 compute remote sha256",
    )
    values: dict[str, str] = {}
    for line in (remote.stdout or "").splitlines():
        if "\t" not in line:
            continue
        rel, digest = line.split("\t", 1)
        values[rel.strip()] = digest.strip().lower()
    return values


def _verify_remote_hashes(workspace: Path, runtime_files: list[str], remote_hashes: dict[str, str]) -> None:
    local_hashes = {rel: _sha256(workspace / rel).lower() for rel in runtime_files}
    mismatches: list[str] = []
    for rel in runtime_files:
        local_digest = local_hashes.get(rel, "")
        remote_digest = remote_hashes.get(rel, "")
        if local_digest != remote_digest:
            mismatches.append(f"{rel}: local={local_digest} remote={remote_digest}")
    if mismatches:
        raise RuntimeError("remote hash mismatch:\n" + "\n".join(mismatches))


def main() -> int:
    parser = argparse.ArgumentParser(description="Commit/push and sync runtime files to Ubuntu with rollback")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--branch", default="main")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--skip-push", action="store_true")
    parser.add_argument("--auto-commit", action="store_true")
    parser.add_argument("--commit-message", default="Publish runtime updates to GitHub and Ubuntu")
    parser.add_argument("--skip-restart", action="store_true")
    parser.add_argument("--skip-healthcheck", action="store_true")
    parser.add_argument("--file", dest="files", action="append", help="runtime file relative path")
    parser.add_argument("--sudo-password", default=os.getenv("UBUNTU_SUDO_PASSWORD", ""))
    parser.add_argument("--remote-app-dir", default="/opt/telegram-risk-control/app")
    parser.add_argument("--remote-temp-root", default="/tmp/telegram-risk-publish")
    parser.add_argument("--remote-backup-root", default="/tmp/telegram-risk-code-backups")
    parser.add_argument("--healthcheck-url", default=DEFAULT_HEALTHCHECK_URL)
    parser.add_argument("--healthcheck-timeout-sec", type=int, default=120)
    parser.add_argument("--healthcheck-interval-sec", type=int, default=5)
    parser.add_argument(
        "--restart-service",
        dest="restart_services",
        action="append",
        default=None,
    )
    parser.add_argument(
        "--restart-pattern",
        dest="restart_patterns",
        action="append",
        default=None,
    )
    parser.add_argument("--allow-process-restart-fallback", action="store_true", default=True)
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    runtime_files = args.files[:] if args.files else list(DEFAULT_RUNTIME_FILES)
    local_paths = [workspace / rel for rel in runtime_files]
    missing = [str(path) for path in local_paths if not path.exists()]
    if missing:
        raise SystemExit(f"missing files: {', '.join(missing)}")
    _ensure_unique_basenames(runtime_files)

    key_path = Path(r"C:\Users\mgrtang\.ssh\fightclub_linux_ed25519")
    start_ssh_ps1 = Path(r"C:\Users\mgrtang\Desktop\linux\Start-Ubuntu-SSH.ps1")
    restart_services = list(args.restart_services) if args.restart_services is not None else list(DEFAULT_RESTART_SERVICES)
    restart_patterns = list(args.restart_patterns) if args.restart_patterns is not None else list(DEFAULT_RESTART_PATTERNS)

    dirty_paths = _git_path_status(workspace, runtime_files)
    if dirty_paths and not args.auto_commit and not args.skip_push:
        raise SystemExit(
            "selected runtime files differ from HEAD; use --auto-commit for stable dual-end deployment:\n"
            + "\n".join(dirty_paths)
        )
    if args.auto_commit:
        _maybe_commit_runtime_files(workspace, runtime_files, args.commit_message)

    if not args.skip_push:
        _must(["git", "push", args.remote, args.branch], cwd=str(workspace))

    _ensure_ssh_tunnel(workspace, start_ssh_ps1)

    preflight = _remote_preflight(key_path, workspace, args.remote_app_dir)
    install_use_sudo = not bool(preflight.get("app_dir_writable", False))
    sudo_nopasswd = bool(preflight.get("sudo_nopasswd", False))
    if install_use_sudo and not (args.sudo_password or sudo_nopasswd):
        raise SystemExit("remote app dir is not writable and no sudo credential path is available")

    restart_use_systemctl = False
    if restart_services and not args.skip_restart:
        if args.sudo_password or sudo_nopasswd:
            restart_use_systemctl = True
        elif args.allow_process_restart_fallback and restart_patterns:
            restart_services = []
        else:
            raise SystemExit("systemd restart requires sudo; provide UBUNTU_SUDO_PASSWORD or allow fallback patterns")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    remote_temp_dir = f"{args.remote_temp_root.rstrip('/')}/{timestamp}"
    remote_backup_dir = f"{args.remote_backup_root.rstrip('/')}/{timestamp}"

    _scp_to_tmp(workspace, key_path, local_paths, remote_temp_dir)

    install_script = _build_remote_install_script(
        runtime_files,
        remote_app_dir=args.remote_app_dir,
        remote_temp_dir=remote_temp_dir,
        remote_backup_dir=remote_backup_dir,
    )
    restore_script = _build_remote_restore_script(
        runtime_files,
        remote_app_dir=args.remote_app_dir,
        remote_backup_dir=remote_backup_dir,
    )

    try:
        _ssh(
            key_path,
            _wrap_bash(
                install_script,
                use_sudo=install_use_sudo,
                sudo_password=args.sudo_password if install_use_sudo else "",
            ),
            workspace=workspace,
            display_override="ssh fightclub@127.0.0.1 install runtime files",
        )

        if not args.skip_restart:
            _remote_restart(
                key_path,
                workspace,
                restart_services=restart_services if restart_use_systemctl else [],
                restart_patterns=restart_patterns if not restart_use_systemctl else [],
                sudo_password=args.sudo_password,
                use_sudo=restart_use_systemctl,
            )

        if not args.skip_healthcheck:
            _remote_healthcheck(
                key_path,
                workspace,
                healthcheck_url=args.healthcheck_url,
                timeout_sec=args.healthcheck_timeout_sec,
                interval_sec=args.healthcheck_interval_sec,
            )

        remote_hashes = _remote_hashes(
            runtime_files,
            workspace=workspace,
            key_path=key_path,
            remote_app_dir=args.remote_app_dir,
        )
        _verify_remote_hashes(workspace, runtime_files, remote_hashes)
    except Exception as exc:
        _log(f"remote publish failed, attempting rollback: {exc}")
        try:
            _ssh(
                key_path,
                _wrap_bash(
                    restore_script,
                    use_sudo=install_use_sudo,
                    sudo_password=args.sudo_password if install_use_sudo else "",
                ),
                workspace=workspace,
                display_override="ssh fightclub@127.0.0.1 restore runtime backup",
            )
            if not args.skip_restart:
                _remote_restart(
                    key_path,
                    workspace,
                    restart_services=restart_services if restart_use_systemctl else [],
                    restart_patterns=restart_patterns if not restart_use_systemctl else [],
                    sudo_password=args.sudo_password,
                    use_sudo=restart_use_systemctl,
                )
            if not args.skip_healthcheck:
                _remote_healthcheck(
                    key_path,
                    workspace,
                    healthcheck_url=args.healthcheck_url,
                    timeout_sec=args.healthcheck_timeout_sec,
                    interval_sec=args.healthcheck_interval_sec,
                )
        except Exception as rollback_exc:
            raise SystemExit(f"publish failed and rollback also failed: {rollback_exc}") from exc
        raise SystemExit(f"publish failed and Ubuntu runtime was rolled back: {exc}") from exc

    _log("sync verified successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
