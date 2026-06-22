import pytest

from app.services.commands import parse_command


@pytest.mark.parametrize("text,name", [("/status", "status"), ("/drafts", "drafts"), ("/pause", "pause"), ("/resume", "resume")])
def test_simple_commands(text, name):
    assert parse_command(text).name == name


@pytest.mark.parametrize("text", ["approve 1", "/unknown", "/approve", "/pause 1", "/approve nope"])
def test_invalid_commands(text):
    with pytest.raises((ValueError, TypeError)):
        parse_command(text)


def test_publish_command_parses_but_does_not_execute():
    parsed = parse_command("/publish_now 42")
    assert parsed.note_id == 42
