"""Create private runtime secrets once. Never replace existing credentials."""
from pathlib import Path
import os
import secrets
root = Path(__file__).resolve().parents[1]
folder = root / ".secrets"
folder.mkdir(mode=0o700, exist_ok=True)
path = folder / "runtime.env"
values = {"POSTGRES_PASSWORD": secrets.token_hex(32), "DEMO_PASSWORD": secrets.token_urlsafe(24),
          "SEED_DEMO": "true", "REGISTRATION_ENABLED": "false", "RELEASE_VERSION": "0.1.0", "FSP_PORT": "8094",
          "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32), "MAX_WEBHOOK_SECRET": secrets.token_urlsafe(32),
          "SMTP_STARTTLS": "true", "SMTP_SSL": "false"}
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("Runtime file already exists; no changes")
else:
    with os.fdopen(fd, "w") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")
    print("Created private runtime file; secret values not displayed")

# A clean source checkout contains only the public contract. Compose requires
# this private file even while external integrations are deliberately disabled.
# An owner's existing handoff is never replaced, including on repeat startup.
integrations = folder / "integrations.env"
try:
    fd = os.open(integrations, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("Integration file already exists; no changes")
else:
    with os.fdopen(fd, "w") as stream:
        stream.write((root / "config" / "integrations.env.example").read_text())
    print("Created private integration template; external services remain unconfigured")
