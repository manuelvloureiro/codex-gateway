"""Catalogue discovery: the parts that decide what lands in CODEX_MODELS.

The app-server call itself needs the Codex CLI and a live login, so it is not
exercised here. What is exercised is everything that shapes its answer into
config: a wrong effort name or a stray hidden model goes straight into `.env`
and from there into what clients are allowed to pick.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "discover_models.py"


def _load():
    """Import the script by path; `scripts/` is deliberately not a package."""
    spec = importlib.util.spec_from_file_location("discover_models", SCRIPT)
    assert spec and spec.loader, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["discover_models"] = module
    spec.loader.exec_module(module)
    return module


discover = _load()


def model(**overrides):
    base = {
        "id": "gpt-5.6-sol",
        "displayName": "GPT-5.6-Sol",
        "hidden": False,
        "isDefault": True,
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Fast"},
            {"reasoningEffort": "high", "description": "Deeper"},
        ],
    }
    base.update(overrides)
    return base


class TestEfforts:
    def test_unwraps_the_catalogue_objects(self):
        # The catalogue returns {"reasoningEffort": ..., "description": ...};
        # the wire wants the bare name.
        assert discover._efforts(model()) == ["low", "high"]

    def test_drops_ultra(self):
        # `ultra` is an app-server behaviour (it delegates tasks), and
        # /responses answers "Invalid value: 'ultra'".
        entry = model(supportedReasoningEfforts=[
            {"reasoningEffort": "max"}, {"reasoningEffort": "ultra"},
        ])
        assert discover._efforts(entry) == ["max"]

    def test_accepts_bare_strings(self):
        assert discover._efforts(model(supportedReasoningEfforts=["low"])) == ["low"]

    @pytest.mark.parametrize("value", [None, [], [{}]])
    def test_degenerate_inputs(self, value):
        assert discover._efforts(model(supportedReasoningEfforts=value)) == []


class TestEnvLine:
    """`--env` output is pasted into .env, so its shape is load-bearing."""

    def test_is_a_comma_separated_assignment(self, capsys, monkeypatch):
        entries = [model(id="gpt-5.6-sol"), model(id="gpt-5.4-mini")]
        monkeypatch.setattr(discover, "_read_catalogue", lambda hidden: entries)
        monkeypatch.setattr(sys, "argv", ["discover_models.py", "--env"])
        assert discover.main() == 0
        line = capsys.readouterr().out.strip()
        assert line == "CODEX_MODELS=gpt-5.6-sol,gpt-5.4-mini"
        assert " " not in line   # a space would split into a bogus model id

    def test_hidden_models_stay_out_by_default(self, capsys, monkeypatch):
        # codex-auto-review is callable but deliberately not offered to users.
        entries = [model(id="gpt-5.6-sol"),
                   model(id="codex-auto-review", hidden=True)]
        monkeypatch.setattr(discover, "_read_catalogue", lambda hidden: entries)
        monkeypatch.setattr(sys, "argv", ["discover_models.py", "--env"])
        discover.main()
        assert capsys.readouterr().out.strip() == "CODEX_MODELS=gpt-5.6-sol"

    def test_include_hidden_keeps_them(self, capsys, monkeypatch):
        entries = [model(id="gpt-5.6-sol"),
                   model(id="codex-auto-review", hidden=True)]
        monkeypatch.setattr(discover, "_read_catalogue", lambda hidden: entries)
        monkeypatch.setattr(sys, "argv",
                            ["discover_models.py", "--env", "--include-hidden"])
        discover.main()
        assert "codex-auto-review" in capsys.readouterr().out


class TestFailureModes:
    def test_empty_catalogue_is_an_error_not_an_empty_env_line(self, monkeypatch):
        # CODEX_MODELS= with nothing after it serves an empty catalogue, which
        # makes every model unreachable. Fail loudly instead.
        monkeypatch.setattr(discover, "_read_catalogue", lambda hidden: [])
        monkeypatch.setattr(sys, "argv", ["discover_models.py", "--env"])
        assert discover.main() == 1

    def test_discovery_failure_is_reported(self, monkeypatch):
        def boom(hidden):
            raise RuntimeError("model/list failed")
        monkeypatch.setattr(discover, "_read_catalogue", boom)
        monkeypatch.setattr(sys, "argv", ["discover_models.py"])
        assert discover.main() == 1
