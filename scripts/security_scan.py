from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{20,}|api[_-]?key\s*=\s*['\"][^'\"]{8,}|bearer\s+[A-Za-z0-9._-]{20,})", re.IGNORECASE)
FORBIDDEN_PATH_PARTS = {".env", ".env.bak", "data/", "logs/", "screenshots/", ".db"}


def git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    staged = [line.strip() for line in git(["diff", "--cached", "--name-only"]).splitlines() if line.strip()]
    problems: list[str] = []
    for path in staged:
        folded = path.replace("\\", "/").casefold()
        if any(part in folded for part in FORBIDDEN_PATH_PARTS):
            problems.append(f"forbidden staged path: {path}")
            continue
        full = ROOT / path
        if full.exists() and full.is_file() and full.suffix.lower() in {".py", ".md", ".yaml", ".yml", ".html", ".css", ".js", ".txt"}:
            text = full.read_text(encoding="utf-8", errors="ignore")
            if SECRET_RE.search(text):
                problems.append(f"possible secret in: {path}")
            thinking_fixture = "private reasoning " + "should not be shown"
            if thinking_fixture in text and not path.startswith("tests/"):
                problems.append(f"thinking fixture leaked outside tests: {path}")
    for local in [".env", ".env.bak", "data/xhs_agent.db"]:
        if (ROOT / local).exists() and local in staged:
            problems.append(f"local sensitive file staged: {local}")
    if problems:
        print("Security scan FAILED")
        for problem in problems:
            print(" - " + problem)
        return 1
    print(f"Security scan OK; staged_files={len(staged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
