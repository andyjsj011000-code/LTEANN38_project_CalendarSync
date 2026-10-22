"""Run a local synthetic CalendarSync beta; no school credentials required."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--prepare-only", action="store_true", help="Create private test accounts without starting a server")
    parser.add_argument("--current-python", action="store_true", help="Use an already prepared Python environment")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required")
    if not 1024 <= args.port <= 65535:
        parser.error("Use a port between 1024 and 65535")
    launch_dir = Path(__file__).resolve().parent
    source_dir = launch_dir if (launch_dir / "web_service").is_dir() else launch_dir.parent
    data_dir = (args.data_dir or launch_dir / "var" / "data").resolve()
    if not args.current_python:
        venv_dir = launch_dir / "var" / "venv"
        executable = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not executable.exists():
            print("Preparing a private Python environment...", flush=True)
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        requirements = launch_dir / "requirements-beta.txt"
        marker = venv_dir / "calendarsync-requirements.txt"
        wanted = requirements.read_text(encoding="utf-8")
        if not marker.exists() or marker.read_text(encoding="utf-8") != wanted:
            print("Installing beta dependencies (first run needs internet)...", flush=True)
            subprocess.run([str(executable), "-m", "pip", "install", "-r", str(requirements)], check=True)
            marker.write_text(wanted, encoding="utf-8")
        # Re-enter with the private interpreter; avoid changing global packages.
        command = [str(executable), str(Path(__file__).resolve()), "--current-python", "--port", str(args.port), "--data-dir", str(data_dir)]
        if args.no_browser:
            command.append("--no-browser")
        if args.prepare_only:
            command.append("--prepare-only")
        return subprocess.call(command)
    missing = [name for name in ("fastapi", "cryptography", "uvicorn") if importlib.util.find_spec(name) is None]
    if missing:
        parser.error("Missing packages: " + ", ".join(missing) + ". Run without --current-python to install privately.")
    sys.path.insert(0, str(source_dir))
    from web_service.beta import prepare_beta
    origin = f"http://127.0.0.1:{args.port}"
    config, invitations = prepare_beta(data_dir, origin)
    print("\nCalendarSync BETA - synthetic Teams examples, separate test accounts", flush=True)
    print("Open: " + origin, flush=True)
    print("Use a different code for each tester; sharing a code shares that account.", flush=True)
    for index, invitation in enumerate(invitations, 1):
        print(f"Tester {index}: {invitation['code']}", flush=True)
    print("Data stays in: " + str(data_dir), flush=True)
    print("Keep this window open. Stop with Ctrl+C.\n", flush=True)
    if args.prepare_only:
        return 0
    os.environ["CALENDARSYNC_BETA_CONFIG"] = str(config)
    if not args.no_browser:
        timer = threading.Timer(2, webbrowser.open, args=(origin,))
        timer.daemon = True
        timer.start()
    import uvicorn
    uvicorn.run("web_service.beta:create_beta_app", factory=True, host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print("Could not start CalendarSync beta: " + str(error), file=sys.stderr)
        raise SystemExit(1)
