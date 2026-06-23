from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ai.factory import create_provider_from_profile
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.security import redact_secrets
from app.services.provider_registry import ProviderRegistry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="")
    args = parser.parse_args()
    init_db()
    settings = get_settings()
    with SessionLocal() as db:
        registry = ProviderRegistry(db, settings)
        registry.initialize()
        row = registry.get_by_name(args.provider) if args.provider else registry.get_default()
        if not row:
            print("ERROR: provider not found")
            return 1
        try:
            adapter = create_provider_from_profile(row, settings)
            ok = adapter.test_connection()
        except Exception as exc:
            print(f"Provider {row.name}: FAILED - {redact_secrets(str(exc))[:300]}")
            return 1
        print(f"Provider {row.name}: {'OK' if ok else 'FAILED'}")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
