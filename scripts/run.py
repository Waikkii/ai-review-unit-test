import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
commands = {
    "init": ["-m", "app.init_db"],
    "api": ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
    "worker": ["-m", "app.worker"],
    "test": ["-m", "pytest", "-q"],
}
if len(sys.argv) != 2 or sys.argv[1] not in commands:
    raise SystemExit("Usage: python scripts/run.py init|api|worker|test")
raise SystemExit(subprocess.call([sys.executable, *commands[sys.argv[1]]], cwd=root / "backend"))
