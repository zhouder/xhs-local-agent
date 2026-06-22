from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("app.main:app", host=str(settings.app["host"]), port=int(settings.app["port"]))


if __name__ == "__main__":
    main()
