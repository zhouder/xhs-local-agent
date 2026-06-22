from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    note_id: int | None = None


ALLOWED = {"status", "drafts", "approve", "reject", "pause", "resume", "publish_now"}


def parse_command(text: str) -> ParsedCommand:
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        raise ValueError("Command must start with /")
    name = parts[0][1:].lower()
    if name not in ALLOWED:
        raise ValueError("Unsupported command")
    needs_id = name in {"approve", "reject", "publish_now"}
    if needs_id and len(parts) != 2:
        raise ValueError("This command requires exactly one note_id")
    if not needs_id and len(parts) != 1:
        raise ValueError("This command takes no arguments")
    return ParsedCommand(name, int(parts[1]) if needs_id else None)
