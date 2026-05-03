#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

"""
macOS native deploy script for telegram-Risk-control.
Copies given files into runtime workspace, compiles Python, restarts launchd jobs,
performs health check and sha256 verification, with rollback on failure.
"""

DEFAULT_RUNTIME_FILES = [
    "main.py",
    "image_fuzzy_blocker.py",
    "semantic_ads.py",
    "railway_failover_runner.py",
    "deploy/release_guard.py",
]

HEALTH_URL = "http://127.0.0.1:18080/status"

def compute_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def compile_python_file(p: Path, pybin: Path):
    if p.suffix != ".py":
        return True
    try:
        subprocess.run([str(pybin), "-m", "py_compile", str(p)], check=True)
        return True
    except Exception as e:
        print(f"PyCompile failed for {p}: {e}", file=sys.stderr)
        return False

def restart_launch_agent(plist_path: Path):
    uid = os.getuid()
    try:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/ai.telegram-risk-control"], check=False)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], check=False)
        return True
    except Exception as e:
        print(f"Restart failed for {plist_path}: {e}", file=sys.stderr)
        return False

def health_check(url=HEALTH_URL, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                text = resp.read().decode(errors="ignore")
                if text:
                    try:
                        data = json.loads(text)
                        if isinstance(data, dict) and data.get("healthy") in (True, "true", 1, "1"):
                            return True
                    except Exception:
                        pass
                    if "healthy" in text.lower() and ("true" in text.lower() or "1" in text):
                        return True
        except Exception:
            pass
        time.sleep(1)
    return False

def main():
    parser = argparse.ArgumentParser(description="Mac deploy publish script for telegram-Risk-control.")
    parser.add_argument("--file", action="append", dest="files", default=[], help="Path to file to publish (relative to workspace). Can be repeated.")
    parser.add_argument("--workspace", required=False, help="Workspace root path. If omitted, auto-detect.")
    parser.add_argument("--skip-push", action="store_true", help="Skip git push step.")
    parser.add_argument("--skip-restart", action="store_true", help="Skip restart of launchd jobs.")
    parser.add_argument("--skip-healthcheck", action="store_true", help="Skip health check polling.")
    parser.add_argument("--auto-commit", action="store_true", help="Auto commit and push git changes before deployment.")
    parser.add_argument("--commit-message", default="Publish mac deployment", help="Git commit message if --auto-commit is used.")
    args = parser.parse_args()

    # workspace detection
    workspace = Path(args.workspace).resolve() if args.workspace else None
    if not workspace or not workspace.exists():
        cur = Path(__file__).resolve()
        root_found = None
        for _ in range(8):
            if (cur / ".git").exists():
                root_found = cur
                break
            cur = cur.parent
        workspace = (root_found if root_found else Path(__file__).resolve().parents[3])
    workspace = workspace.resolve()
    runtime_dir = workspace
    venv_python = Path(workspace) / "venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path("/usr/bin/python3")

    files_to_publish = args.files if args.files else DEFAULT_RUNTIME_FILES
    backup_root = Path("/tmp/telegram-risk-publish-backups") / time.strftime("%Y%m%d-%H%M%S")
    ensure_dir(backup_root)

    plist1 = Path.home() / "Library" / "LaunchAgents" / "ai.telegram-risk-control.plist"
    plist2 = Path.home() / "Library" / "LaunchAgents" / "ai.telegram-risk-health.plist"

    try:
        if args.auto_commit:
            subprocess.run(["git", "-C", str(workspace), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(workspace), "commit", "-m", args.commit_message], check=True)
            subprocess.run(["git", "-C", str(workspace), "push"], check=True)

        # copy files with backup
        for rel in files_to_publish:
            src = workspace / rel
            if not src.exists():
                raise FileNotFoundError(f"Source file not found: {src}")
            dest = runtime_dir / rel
            ensure_dir(dest.parent)
            if dest.exists():
                backup_path = backup_root / rel
                ensure_dir(backup_path.parent)
                shutil.copy2(dest, backup_path)
            shutil.copy2(src, dest)
            print(f"Copied {src} -> {dest}")

        # py_compile copied files
        for rel in files_to_publish:
            dest = runtime_dir / rel
            if dest.suffix == ".py":
                if not compile_python_file(dest, venv_python):
                    raise RuntimeError(f"Compilation failed for {dest}")

        # restart services
        if not args.skip_restart:
            if not plist1.exists():
                print(f"Warning: {plist1} not found; skipping restart of ai.telegram-risk-control.")
            else:
                if not restart_launch_agent(plist1):
                    raise RuntimeError("Failed to restart ai.telegram-risk-control")
            if not plist2.exists():
                print(f"Warning: {plist2} not found; skipping restart of ai.telegram-risk-health.")
            else:
                if not restart_launch_agent(plist2):
                    raise RuntimeError("Failed to restart ai.telegram-risk-health")

        # health check
        if not args.skip_healthcheck:
            if not health_check():
                raise RuntimeError("Health check failed to become healthy in time")

        # sha256 verify
        for rel in files_to_publish:
            src = workspace / rel
            dest = runtime_dir / rel
            if src.exists() and dest.exists():
                if compute_sha256(str(src)) != compute_sha256(str(dest)):
                    raise RuntimeError(f"SHA256 mismatch for {rel}")

        print("Deployment succeeded.")
        return 0
    except Exception as e:
        print(f"Deployment failed: {e}", file=sys.stderr)
        # rollback from backups
        try:
            for root, dirs, files in os.walk(backup_root):
                for f in files:
                    bpath = Path(root) / f
                    rel = bpath.relative_to(backup_root)
                    dest = runtime_dir / rel
                    ensure_dir(dest.parent)
                    shutil.copy2(bpath, dest)
            print("Rollback completed from backups.")
        except Exception as rb:
            print(f"Rollback failed: {rb}", file=sys.stderr)
        # restart after rollback
        if plist1.exists():
            restart_launch_agent(plist1)
        if plist2.exists():
            restart_launch_agent(plist2)
        return 1

if __name__ == "__main__":
    sys.exit(main())
