"""Start the platform:  python server.py   then open http://localhost:8000

Settings are read from the environment or from a .env file next to this script (see .env.example)."""
import os

import uvicorn

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_env(path: str) -> None:
    """Tiny .env reader (KEY=value lines); real environment variables win."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():                      # empty value = leave the setting unset
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    load_env(os.path.join(ROOT, ".env"))          # must run before the app is imported
    uvicorn.run("app.main:app", host=os.environ.get("OMR_HOST", "127.0.0.1"), port=int(os.environ.get("OMR_PORT", "8000")))
