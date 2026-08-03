"""The CLI front end.

Same flow as the HTTP API, so these focus on argument handling, exit codes, and
the fact that nothing prints a token.
"""
from __future__ import annotations

import json
import time

import pytest

from codex_gateway import login, oauth
from codex_gateway.oauth import AuthError

from .conftest import make_jwt, response


@pytest.fixture
def signed_in(fresh_token):
    oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
    return fresh_token


class TestStatus:
    def test_exit_1_when_not_signed_in(self, capsys):
        assert login.main(["--status"]) == 1
        assert json.loads(capsys.readouterr().out)["authenticated"] is False

    def test_exit_0_when_signed_in(self, capsys, signed_in):
        assert login.main(["--status"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["authenticated"] is True
        assert body["account_id"] == "acct-123"

    def test_does_not_print_the_token(self, capsys, signed_in):
        login.main(["--status"])
        out = capsys.readouterr().out
        assert signed_in not in out
        assert "r1" not in out


class TestLogout:
    def test_clears_credentials(self, capsys, signed_in):
        assert login.main(["--logout"]) == 0
        assert "cleared" in capsys.readouterr().out
        assert oauth.credential_status()["authenticated"] is False

    def test_reports_when_nothing_stored(self, capsys):
        assert login.main(["--logout"]) == 0
        assert "nothing stored" in capsys.readouterr().out


class TestImport:
    def _write_cli(self, payload):
        path = oauth.codex_cli_auth_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def test_imports_and_persists(self, capsys, fresh_token):
        self._write_cli({"tokens": {"access_token": fresh_token, "refresh_token": "r1"}})
        assert login.main(["--import"]) == 0
        assert "imported" in capsys.readouterr().out
        assert oauth.read_tokens()["tokens"]["access_token"] == fresh_token

    def test_exit_1_when_nothing_to_import(self, capsys):
        assert login.main(["--import"]) == 1
        assert "no usable tokens" in capsys.readouterr().err

    def test_expired_cli_tokens_are_not_imported(self, capsys, expired_token):
        self._write_cli({"tokens": {"access_token": expired_token, "refresh_token": "r1"}})
        assert login.main(["--import"]) == 1


class TestDeviceLogin:
    def test_prints_code_and_url_then_saves(self, capsys, stub_post, fresh_token,
                                            monkeypatch):
        monkeypatch.setattr(oauth.time, "sleep", lambda _: None)
        stub_post.queue.append(response(200, {"user_code": "ABCD-1234",
                                              "device_auth_id": "dev-1",
                                              "interval": 3}))
        stub_post.queue.append(response(200, {"authorization_code": "c",
                                              "code_verifier": "v"}))
        stub_post.queue.append(response(200, {"access_token": fresh_token,
                                              "refresh_token": "r1"}))

        assert login.main([]) == 0
        out = capsys.readouterr().out
        assert "ABCD-1234" in out
        assert oauth.VERIFICATION_URI in out
        assert "signed in" in out
        assert fresh_token not in out
        assert oauth.read_tokens()["tokens"]["access_token"] == fresh_token

    def test_warns_when_no_refresh_token_was_issued(self, capsys, stub_post,
                                                    fresh_token, monkeypatch):
        monkeypatch.setattr(oauth.time, "sleep", lambda _: None)
        stub_post.queue.append(response(200, {"user_code": "A", "device_auth_id": "d"}))
        stub_post.queue.append(response(200, {"authorization_code": "c",
                                              "code_verifier": "v"}))
        stub_post.queue.append(response(200, {"access_token": fresh_token}))

        assert login.main([]) == 0
        assert "cannot be renewed" in capsys.readouterr().err

    def test_auth_error_exits_1_with_the_code(self, capsys, monkeypatch):
        def _throttled(**kwargs):
            raise AuthError("throttled by OpenAI", code="rate_limited")

        monkeypatch.setattr(oauth, "start_device_login", _throttled)
        assert login.main([]) == 1
        err = capsys.readouterr().err
        assert "rate_limited" in err
        assert "throttled by OpenAI" in err

    def test_ctrl_c_exits_130(self, capsys, monkeypatch):
        def _interrupt(**kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(oauth, "start_device_login", _interrupt)
        assert login.main([]) == 130
        assert "aborted" in capsys.readouterr().err

    def test_prints_the_account_id(self, capsys, stub_post, monkeypatch):
        monkeypatch.setattr(oauth.time, "sleep", lambda _: None)
        token = make_jwt(exp=time.time() + 3600, account_id="acct-xyz")
        stub_post.queue.append(response(200, {"user_code": "A", "device_auth_id": "d"}))
        stub_post.queue.append(response(200, {"authorization_code": "c",
                                              "code_verifier": "v"}))
        stub_post.queue.append(response(200, {"access_token": token,
                                              "refresh_token": "r"}))
        login.main([])
        assert "acct-xyz" in capsys.readouterr().out


class TestArgParsing:
    def test_modes_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            login.main(["--status", "--import"])

    def test_unknown_flag_is_rejected(self):
        with pytest.raises(SystemExit):
            login.main(["--nonsense"])
