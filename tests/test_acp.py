"""ACP launcher configuration and stdio-safety tests.

The maintained Codex adapter owns the wire protocol.  These tests cover the
gateway-specific boundary: provider injection, command selection, and the
guarantee that the launcher writes nothing to ACP stdout before ``exec``.
"""

from __future__ import annotations

import json

import pytest

from codex_gateway import acp
from codex_gateway import __main__ as package_main


@pytest.fixture(autouse=True)
def isolated_acp_environment(monkeypatch):
    """Keep launcher tests independent of a developer's editor setup."""
    for name in (
        "CODEX_ACP_BIN",
        "CODEX_ACP_PACKAGE",
        "CODEX_CONFIG",
        "CODEX_GATEWAY_MODEL",
        "CODEX_GATEWAY_URL",
        "CODEX_MODELS",
        "DEFAULT_AUTH_REQUEST",
        "MODEL_PROVIDER",
        "NO_BROWSER",
    ):
        monkeypatch.delenv(name, raising=False)


class TestCodexConfig:
    def test_builds_keyless_responses_provider(self):
        config = acp.codex_config({})

        assert config["model"] == "gpt-5.6-sol"
        assert config["model_provider"] == "codex-gateway"
        assert config["model_providers"]["codex-gateway"] == {
            "name": "Codex Gateway",
            "base_url": "http://127.0.0.1:8085/v1",
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": False,
        }

    def test_uses_explicit_url_and_model(self):
        config = acp.codex_config(
            {
                "CODEX_GATEWAY_URL": "https://gateway.example.test/codex/v1/",
                "CODEX_GATEWAY_MODEL": "gpt-custom",
            }
        )
        assert config["model"] == "gpt-custom"
        assert (
            config["model_providers"]["codex-gateway"]["base_url"]
            == "https://gateway.example.test/codex/v1"
        )

    def test_accepts_gateway_base_without_v1(self):
        config = acp.codex_config(
            {
                "CODEX_GATEWAY_URL": "http://localhost:8085",
            }
        )
        assert (
            config["model_providers"]["codex-gateway"]["base_url"]
            == "http://localhost:8085"
        )

    def test_falls_back_to_first_catalogue_model(self):
        config = acp.codex_config({"CODEX_MODELS": " model-a, model-b "})
        assert config["model"] == "model-a"

    def test_preserves_unrelated_config_and_provider_tuning(self):
        original = {
            "model": "old-model",
            "model_reasoning_effort": "high",
            "model_providers": {
                "other": {"base_url": "https://other.example/v1"},
                "codex-gateway": {
                    "request_max_retries": 7,
                    "base_url": "https://stale.example/v1",
                },
            },
        }
        config = acp.codex_config(
            {
                "CODEX_CONFIG": json.dumps(original),
                "CODEX_GATEWAY_MODEL": "new-model",
            }
        )

        assert config["model_reasoning_effort"] == "high"
        assert (
            config["model_providers"]["other"] == original["model_providers"]["other"]
        )
        provider = config["model_providers"]["codex-gateway"]
        assert provider["request_max_retries"] == 7
        assert provider["base_url"] == "http://127.0.0.1:8085/v1"
        assert config["model"] == "new-model"

    @pytest.mark.parametrize("value", ["not json", "[]", "null"])
    def test_rejects_invalid_codex_config(self, value):
        with pytest.raises(acp.AcpConfigError):
            acp.codex_config({"CODEX_CONFIG": value})

    @pytest.mark.parametrize(
        "url",
        [
            "localhost:8085/v1",
            "file:///tmp/gateway",
            "http://user:secret@localhost:8085/v1",
            "http://localhost:8085/v1?token=secret",
            "http://localhost:8085/v1#fragment",
            "http://bad host:8085/v1",
            "http://[invalid/v1",
            "http://localhost:99999/v1",
            " ",
        ],
    )
    def test_rejects_invalid_gateway_url(self, url):
        with pytest.raises(acp.AcpConfigError):
            acp.codex_config({"CODEX_GATEWAY_URL": url})


class TestAdapterCommand:
    def test_explicit_binary_wins(self, monkeypatch):
        monkeypatch.setattr(acp.shutil, "which", lambda _name: "/ignored")
        assert acp.adapter_command({"CODEX_ACP_BIN": "/opt/bin/my-codex-acp"}) == [
            "/opt/bin/my-codex-acp"
        ]

    def test_falls_back_to_pinned_npx_package(self, monkeypatch):
        monkeypatch.setattr(
            acp.shutil,
            "which",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )
        assert acp.adapter_command({}) == [
            "/usr/bin/npx",
            "--yes",
            "@agentclientprotocol/codex-acp@1.1.9",
        ]

    def test_does_not_auto_select_an_unversioned_adapter(self, monkeypatch):
        def which(name):
            return {
                "codex-acp": "/old/bin/codex-acp",
                "npx": "/usr/bin/npx",
            }.get(name)

        monkeypatch.setattr(acp.shutil, "which", which)
        assert acp.adapter_command({}) == [
            "/usr/bin/npx",
            "--yes",
            "@agentclientprotocol/codex-acp@1.1.9",
        ]

    def test_reports_missing_adapter_and_node(self, monkeypatch):
        monkeypatch.setattr(acp.shutil, "which", lambda _name: None)
        with pytest.raises(acp.AcpConfigError, match="Node.js 20"):
            acp.adapter_command({})


class TestEntryPoint:
    def test_execs_adapter_with_pristine_stdout(self, monkeypatch, capsys):
        captured = {}

        def fake_exec(file, args, env):
            captured.update(file=file, args=args, env=env)

        monkeypatch.setenv("CODEX_ACP_BIN", "/opt/bin/codex-acp")
        monkeypatch.setenv("CODEX_GATEWAY_MODEL", "gpt-test")
        monkeypatch.setattr(acp.os, "execvpe", fake_exec)

        assert acp.main([]) == 0
        output = capsys.readouterr()
        assert output.out == ""
        assert output.err == ""
        assert captured["file"] == "/opt/bin/codex-acp"
        assert captured["args"] == ["/opt/bin/codex-acp"]
        config = json.loads(captured["env"]["CODEX_CONFIG"])
        assert config["model"] == "gpt-test"
        assert captured["env"]["MODEL_PROVIDER"] == "codex-gateway"
        auth = json.loads(captured["env"]["DEFAULT_AUTH_REQUEST"])
        assert auth == {
            "methodId": "gateway",
            "_meta": {
                "gateway": {
                    "baseUrl": "http://127.0.0.1:8085/v1",
                    "headers": {},
                    "providerName": "Codex Gateway",
                },
            },
        }
        assert captured["env"]["NO_BROWSER"] == "1"

    def test_configuration_errors_only_use_stderr(self, monkeypatch, capsys):
        monkeypatch.setenv("CODEX_ACP_BIN", "/opt/bin/codex-acp")
        monkeypatch.setenv("CODEX_CONFIG", "not-json")

        assert acp.main([]) == 2
        output = capsys.readouterr()
        assert output.out == ""
        assert "CODEX_CONFIG is not valid JSON" in output.err

    def test_os_error_only_uses_stderr(self, monkeypatch, capsys):
        monkeypatch.setenv("CODEX_ACP_BIN", "/missing/codex-acp")

        def fail_exec(*_args):
            raise FileNotFoundError("not found")

        monkeypatch.setattr(acp.os, "execvpe", fail_exec)
        assert acp.main([]) == 127
        output = capsys.readouterr()
        assert output.out == ""
        assert "cannot start ACP adapter" in output.err

    def test_main_command_dispatches_acp_subcommand(self, monkeypatch):
        seen = []
        monkeypatch.setattr(acp, "main", lambda args: seen.append(args) or 23)
        assert package_main.main(["acp"]) == 23
        assert seen == [[]]

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_main_command_has_discoverable_help(self, flag, capsys):
        assert package_main.main([flag]) == 0
        output = capsys.readouterr()
        assert output.err == ""
        assert "usage: codex-gateway" in output.out
        assert "--port" in output.out

    def test_main_command_rejects_unknown_arguments(self, capsys):
        assert package_main.main(["unknown"]) == 2
        assert "usage: codex-gateway" in capsys.readouterr().err

    def test_main_command_serves_on_default_port_without_flag(self, monkeypatch):
        seen = []
        monkeypatch.setattr(package_main, "run", lambda port: seen.append(port) or 0)
        assert package_main.main([]) == 0
        assert seen == [None]

    @pytest.mark.parametrize("args", [["--port", "9000"], ["--port=9000"]])
    def test_main_command_accepts_port_flag(self, args, monkeypatch):
        seen = []
        monkeypatch.setattr(package_main, "run", lambda port: seen.append(port) or 0)
        assert package_main.main(args) == 0
        assert seen == [9000]

    @pytest.mark.parametrize("args", [
        ["--port"],
        ["--port", "abc"],
        ["--port", "0"],
        ["--port", "70000"],
        ["--port", "9000", "extra"],
        ["--port=9000", "extra"],
    ])
    def test_main_command_rejects_bad_port(self, args, monkeypatch, capsys):
        monkeypatch.setattr(package_main, "run",
                            lambda port: pytest.fail("must not serve"))
        assert package_main.main(args) == 2
        assert "usage: codex-gateway" in capsys.readouterr().err
