#!/usr/bin/env python3
"""Bootstrap the smc-analyst runtime environment.

Creates an isolated virtualenv for the skill, installs the pinned
requirements, verifies the imports and records a stamp, so that later runs
return immediately. It is cross-platform (Windows ``Scripts/`` vs POSIX
``bin/``), uses only the standard library, and is safe to run repeatedly.

Usage:
    python setup.py            # create/update the environment if needed
    python setup.py --check    # report status only, never install (exit 2 if not ready)
    python setup.py --force    # delete and rebuild the environment
    python setup.py --json     # add a final machine-readable JSON status line

Environment variables:
    SMC_ANALYST_VENV   Override the virtualenv location (default: <skill-dir>/.venv)

Exit codes: 0 ready, 1 error, 2 not ready (--check only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
from datetime import datetime, timezone
from pathlib import Path

MIN_PYTHON = (3, 12)
STAMP_NAME = ".smc-setup.json"
# Import names checked after install (they can differ from pip distribution names).
REQUIRED_IMPORTS = ["pandas", "numpy", "yfinance", "ccxt", "fastapi", "uvicorn", "websockets"]

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
REQUIREMENTS = SCRIPTS_DIR / "requirements.txt"


def venv_dir() -> Path:
    override = os.environ.get("SMC_ANALYST_VENV")
    return Path(override).expanduser().resolve() if override else SKILL_DIR / ".venv"


def venv_python(vdir: Path) -> Path:
    if os.name == "nt":
        return vdir / "Scripts" / "python.exe"
    return vdir / "bin" / "python"


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def read_stamp(vdir: Path) -> dict | None:
    try:
        return json.loads((vdir / STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(f"[smc-analyst setup] {msg}", flush=True)


def is_ready(vdir: Path) -> tuple[bool, str]:
    py = venv_python(vdir)
    if not py.exists():
        return False, "virtualenv not found"
    stamp = read_stamp(vdir)
    if not stamp:
        return False, "setup stamp missing (previous install incomplete)"
    if stamp.get("requirements_sha256") != requirements_hash():
        return False, "requirements.txt changed since last install"
    return True, "environment is ready"


def verify_imports(py: Path) -> None:
    code = "import " + ", ".join(REQUIRED_IMPORTS)
    subprocess.run([str(py), "-c", code], check=True, capture_output=True, text=True)


def build(vdir: Path, force: bool, quiet: bool) -> None:
    if force and vdir.exists():
        log(f"removing existing environment at {vdir}", quiet)
        shutil.rmtree(vdir)

    py = venv_python(vdir)
    if not py.exists():
        log(f"creating virtualenv at {vdir}", quiet)
        venv.EnvBuilder(with_pip=True, clear=vdir.exists()).create(vdir)

    log("installing pinned requirements (first run can take a few minutes)", quiet)
    # Generous timeout/retries: large wheels (pandas, numpy) on slow links can
    # exceed pip's default 15 s read timeout. A partial install leaves no
    # stamp, so the next run retries automatically.
    cmd = [str(py), "-m", "pip", "install", "--disable-pip-version-check",
           "--no-input", "--timeout", "120", "--retries", "10", "-r", str(REQUIREMENTS)]
    if quiet:
        cmd.append("--quiet")
    subprocess.run(cmd, check=True)

    log("verifying imports", quiet)
    verify_imports(py)

    stamp = {
        "requirements_sha256": requirements_hash(),
        "python_version": subprocess.run(
            [str(py), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            check=True, capture_output=True, text=True).stdout.strip(),
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (vdir / STAMP_NAME).write_text(json.dumps(stamp, indent=2), encoding="utf-8")


def emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bootstrap the smc-analyst Python environment.")
    ap.add_argument("--check", action="store_true", help="report status only; do not install")
    ap.add_argument("--force", action="store_true", help="delete and rebuild the environment")
    ap.add_argument("--json", action="store_true", help="print a final JSON status line")
    ap.add_argument("--quiet", action="store_true", help="suppress progress messages")
    args = ap.parse_args(argv)

    vdir = venv_dir()
    base = {"venv": str(vdir), "venv_python": str(venv_python(vdir)), "skill_dir": str(SKILL_DIR)}

    if sys.version_info < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        have = ".".join(map(str, sys.version_info[:3]))
        log(f"Python {need}+ required (found {have})", args.quiet)
        emit({**base, "status": "error",
              "message": f"Python {need}+ is required, but this is Python {have}. "
                         f"Install Python {need} or newer from https://www.python.org/downloads/ "
                         f"and re-run setup.py with it (e.g. 'py -3.12' on Windows, 'python3.12' elsewhere)."},
             args.json)
        return 1

    if not REQUIREMENTS.exists():
        emit({**base, "status": "error", "message": f"missing {REQUIREMENTS}"}, args.json)
        return 1

    ready, reason = is_ready(vdir)
    if args.check:
        log(reason, args.quiet)
        emit({**base, "status": "ready" if ready else "not_ready", "message": reason}, args.json)
        return 0 if ready else 2

    if ready and not args.force:
        log(reason + " (nothing to do)", args.quiet)
        emit({**base, "status": "ready", "message": reason, "changed": False}, args.json)
        return 0

    try:
        log(f"setup needed: {reason}" if not args.force else "forced rebuild", args.quiet)
        build(vdir, args.force, args.quiet)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[-1:] if exc.stderr else []
        msg = "dependency installation or import check failed"
        if detail:
            msg += f": {detail[0]}"
        msg += (". See the pip output above. Check your internet connection and re-run "
                "setup.py (already-downloaded packages are reused).")
        log(msg, False)
        emit({**base, "status": "error", "message": msg}, args.json)
        return 1
    except OSError as exc:
        msg = f"could not create the virtualenv: {exc}"
        log(msg, False)
        emit({**base, "status": "error", "message": msg}, args.json)
        return 1

    log("environment is ready", args.quiet)
    emit({**base, "status": "ready", "message": "environment installed", "changed": True}, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
