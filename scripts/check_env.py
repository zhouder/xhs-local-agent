from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 12):
        print("ERROR: Python 3.12+ is required.")
        return 1
    missing = [name for name in ["fastapi", "sqlalchemy", "httpx", "playwright", "yaml"] if importlib.util.find_spec(name) is None]
    if missing:
        print("ERROR: missing dependencies: " + ", ".join(missing))
        return 1
    print("Dependencies: OK")
    print(f".env exists: {(ROOT / '.env').exists()}")
    from app.database import init_db

    init_db()
    print("Database init: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
