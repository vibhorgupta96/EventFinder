"""Mac-local launchd lifecycle wrapper for the isolated EventFinder service."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.eventfinder.app"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = Path.home() / "Library" / "Logs" / "EventFinder"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS / f"{LABEL}.plist"


def _venv_python() -> Path:
    return PROJECT_ROOT / ".venv" / "bin" / "python"


def _plist() -> dict:
    python = _venv_python()
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", str(python), "-m", "eventfinder.main"],
        "WorkingDirectory": str(PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_DIR / "stdout.log"),
        "StandardErrorPath": str(LOG_DIR / "stderr.log"),
        "EnvironmentVariables": {"PATH": f"{PROJECT_ROOT / '.venv' / 'bin'}:/usr/bin:/bin"},
    }


def _domain_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def install() -> None:
    if not _venv_python().exists():
        raise RuntimeError("Missing .venv. Run `make install` before starting EventFinder.")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as stream:
        plistlib.dump(_plist(), stream, sort_keys=False)
    print(f"Installed {PLIST_PATH}")


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], text=True, check=check, capture_output=True)


def start() -> None:
    install()
    _launchctl("bootout", _domain_target(), check=False)
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH))
    _launchctl("kickstart", "-k", _domain_target())
    print(f"Started {LABEL}; dashboard: http://127.0.0.1:8766")


def stop() -> None:
    result = _launchctl("bootout", _domain_target(), check=False)
    if result.returncode:
        print(f"{LABEL} was not loaded")
    else:
        print(f"Stopped {LABEL}")


def restart() -> None:
    start()


def status() -> None:
    result = _launchctl("print", _domain_target(), check=False)
    print(result.stdout or result.stderr or f"{LABEL} is not loaded")
    print(f"Logs: {LOG_DIR}")


def logs() -> None:
    for path in (LOG_DIR / "stdout.log", LOG_DIR / "stderr.log"):
        print(f"\n== {path} ==")
        if path.exists():
            print("".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-100:]), end="")
        else:
            print("(no log yet)")


def main() -> None:
    parser = argparse.ArgumentParser(description="EventFinder launchd lifecycle")
    parser.add_argument("command", choices=("start", "stop", "restart", "status", "logs"))
    args = parser.parse_args()
    globals()[args.command]()


if __name__ == "__main__":
    main()
