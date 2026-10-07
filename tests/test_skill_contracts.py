"""Check metadata, packaged resources and executable documentation examples."""
import argparse
import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted(ROOT.glob("*/v*/SKILL.md"))


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.parent.name)
def test_skill_metadata_and_local_links(path):
    text = path.read_text()
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert metadata["name"] == path.parent.parent.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
    assert isinstance(metadata["description"], str) and metadata["description"].strip()
    assert str(metadata["version"]) == (path.parent / "VERSION").read_text().strip()
    assert path.parent.name == f"v{metadata['version']}"
    assert metadata["platforms"] == ["linux"]
    assert isinstance(metadata["metadata"]["hermes"]["tags"], list)
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        if not target.startswith(("https://", "http://", "#")):
            assert (path.parent / target).is_file()


def documented_commands():
    for path in SKILLS:
        for line in path.read_text().splitlines():
            if line.startswith("/opt/hermes/.venv/bin/python3 "):
                words = shlex.split(line)
                yield pytest.param(path, words, id=f"{path.parent.parent.name}: {' '.join(words[2:])}")


@pytest.mark.parametrize("path,words", list(documented_commands()))
def test_documented_commands_parse_without_running(path, words, health, updater, monkeypatch, capsys):
    script_path = Path(words[1])
    assert script_path.parts[-3] == path.parent.parent.name
    assert (path.parent / "scripts" / script_path.name).is_file()
    if path.parent.parent.name == "docker-updater":
        parser = updater.build_parser()
    else:
        # Capture the real parser before main loads config or runs any command.
        parsers = []
        class ParserCaptured(Exception):
            pass
        def capture(parser, *args, **kwargs):
            parsers.append(parser)
            raise ParserCaptured
        with monkeypatch.context() as patch:
            patch.setattr(argparse.ArgumentParser, "parse_args", capture)
            with pytest.raises(ParserCaptured):
                health.main()
        parser = parsers[0]
    if words[2:] == ["--help"]:
        with pytest.raises(SystemExit) as result:
            parser.parse_args(words[2:])
        assert result.value.code == 0
    else:
        args = parser.parse_args(words[2:])
        assert args.command == words[2]
