"""Run this project's development API/worker without touching shared services."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
python = root / ".venv" / "bin" / "python"
try:
    from dotenv import dotenv_values
except ModuleNotFoundError:
    if Path(sys.prefix) == root / ".venv":
        raise SystemExit("Install this project's pinned backend requirements first")
    # Preserve the documented convenience command while parsing quoted .env
    # values with the project's existing pinned dependency, never shell eval.
    raise SystemExit(subprocess.call([str(python), str(Path(__file__).resolve()), *sys.argv[1:]]))
environment = dict(os.environ)
for name in ("runtime.env", "integrations.env"):
    path = root / ".secrets" / name
    if path.exists():
        environment.update({key: value for key, value in dotenv_values(path, interpolate=False).items()
                            if value is not None})
for public_name, app_name in (("EMAIL_FROM", "SMTP_FROM"),
                             ("EMAIL_REPLY_TO", "SMTP_REPLY_TO"),
                             ("LLM_API_BASE_URL", "LLM_BASE_URL")):
    if public_name in environment:
        environment[app_name] = environment[public_name]
runtime = root / "runtime"
runtime.mkdir(exist_ok=True)
environment.update({"ENVIRONMENT": "development", "APP_URL": "http://localhost:5173",
                    "ALLOWED_ORIGINS": "http://localhost:5173,http://127.0.0.1:5173",
                    "DATABASE_URL": "sqlite:///" + str(runtime / "local.db"),
                    "REGISTRATION_ENABLED": "true", "SMTP_TLS_MODE": "none", "SMTP_STARTTLS": "false", "SMTP_SSL": "false",
                    "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "1025", "SMTP_FROM": "КОД.МЭТЧ <demo@example.org>",
                    "SMTP_USERNAME": "", "SMTP_PASSWORD": "",
                    "MAX_BOT_TOKEN": "", "MAX_BOT_USERNAME": "", "MAX_WEBHOOK_SECRET": "",
                    "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_BOT_USERNAME": "", "TELEGRAM_WEBHOOK_SECRET": ""})
mode = sys.argv[1] if len(sys.argv) > 1 else "api"
if mode == "api":
    command = [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8100", "--no-access-log"]
elif mode == "worker":
    command = [str(python), "-m", "app.worker"]
else:
    raise SystemExit("Usage: run_local.py api|worker")
raise SystemExit(subprocess.call(command, cwd=root / "backend", env=environment))
