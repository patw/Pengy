"""The credential/error contract for a failed turn.

Long-standing bug (found 2026-09-14, in every release up to 1.8.4): a failed turn
printed ``Error: ...`` to **stdout** and the process exited **0**.  Consequences:

* ``pengy-cli --output json`` produced unparseable output with a success status,
  so anything parsing it (ralph's loop, shell pipelines) saw a silent failure;
* the raw SDK text told users to set ``OPENAI_API_KEY`` — an environment variable
  Pengy never reads, so following the advice changed nothing.

The contract these tests pin down:

* credential failures become a ``CredentialError`` carrying Pengy's own
  instructions (``/apikey``, ``/baseurl``, ``/config``, the settings file,
  the Settings page);
* human-readable errors go to **stderr**;
* ``--output json`` keeps **stdout** parseable — a valid error document;
* single-shot mode exits **2** for credential problems, **1** for other errors,
  **0** on success; interactive mode keeps going and still exits 0.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from pengy.cli import main as cli_main
from pengy.core import llm_client
from pengy.core.llm_client import CredentialError, LLMClient


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeStatusError(Exception):
    """Stands in for openai.AuthenticationError (name + status_code)."""

    def __init__(self, status_code=401, message="Unauthorized"):
        super().__init__(message)
        self.status_code = status_code


_FakeAuthenticationError = type("AuthenticationError", (_FakeStatusError,), {})


def _raise_missing_credentials():
    """The exact client-side error the OpenAI SDK raises with no key at all."""
    raise Exception(
        "Missing credentials. Please pass an `api_key`, `workload_identity`, "
        "`admin_api_key`, or set the `OPENAI_API_KEY` or `OPENAI_ADMIN_KEY` "
        "environment variable."
    )


class TestDetection:
    def test_status_code_401_and_403(self):
        assert llm_client._looks_like_credential_problem(_FakeStatusError(401))
        assert llm_client._looks_like_credential_problem(_FakeStatusError(403))

    def test_exception_class_name(self):
        assert llm_client._looks_like_credential_problem(_FakeAuthenticationError())

    def test_sdk_missing_credentials_message(self):
        exc = Exception(
            "Missing credentials. Please pass an `api_key`, ... or set the "
            "`OPENAI_API_KEY` environment variable."
        )
        assert llm_client._looks_like_credential_problem(exc)

    def test_common_server_phrasings(self):
        for text in (
            "Incorrect API key provided: sk-xxx",
            "invalid_api_key",
            "Authentication failed",
            "You didn't provide an API key",
            "Unauthorized",
        ):
            assert llm_client._looks_like_credential_problem(Exception(text)), text

    @pytest.mark.parametrize(
        "text",
        [
            "Connection refused",
            "Request timed out after 300s",
            "model `gpt-4o` does not exist",
            "429 Too Many Requests",
        ],
    )
    def test_other_errors_are_not_credential_problems(self, text):
        assert not llm_client._looks_like_credential_problem(Exception(text))


class TestHelpText:
    def test_help_names_the_real_configuration_commands(self):
        text = llm_client.credential_help("https://api.openai.com/v1")
        for needle in (
            "/apikey",
            "/baseurl",
            "/model",
            "/config",
            "settings.json",
            "/settings",
        ):
            assert needle in text, needle

    def test_help_explains_env_vars_are_not_used(self):
        """The raw SDK advice is actively wrong for Pengy — say so."""
        text = llm_client.credential_help("https://api.openai.com/v1")
        assert "OPENAI_API_KEY" in text
        assert "NOT used" in text

    def test_help_mentions_the_endpoint_and_local_models(self):
        text = llm_client.credential_help("http://localhost:11434/v1")
        assert "http://localhost:11434/v1" in text
        assert "Ollama" in text

    def test_help_shows_the_effective_config_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PENGY_CONFIG_DIR", str(tmp_path))
        assert str(tmp_path / "settings.json") in llm_client.credential_help("http://x/v1")


class TestTranslation:
    def test_credential_error_is_typed_and_carries_an_exit_code(self):
        translated = llm_client._translate_api_error(_FakeStatusError(401), "http://x/v1")
        assert isinstance(translated, CredentialError)
        assert translated.kind == "credentials"
        assert translated.exit_code == 2

    def test_generic_errors_pass_through_untouched(self):
        original = RuntimeError("Connection refused")
        assert llm_client._translate_api_error(original, "http://x/v1") is original

    def test_already_translated_errors_are_not_rewrapped(self):
        once = CredentialError("already friendly")
        assert llm_client._translate_api_error(once, "http://x/v1") is once


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A PengyCLI with an isolated config dir and no saving."""
    monkeypatch.setenv("PENGY_CONFIG_DIR", str(tmp_path))
    instance = cli_main.PengyCLI(no_save=True)
    return instance


def _patch_chat(monkeypatch, behaviour):
    """Replace LLMClient.chat with a generator-producing behaviour(behaviour=exc|list)."""

    def fake_chat(self, messages, **kwargs):
        if isinstance(behaviour, BaseException):
            raise behaviour
        yield from behaviour

    monkeypatch.setattr(LLMClient, "chat", fake_chat)


def _final_response(text="hello"):
    return [
        {
            "type": "final_response",
            "content": text,
            "message": {"role": "assistant", "content": text},
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }
    ]


class TestSingleShotContract:
    def test_missing_credentials_exit_2_with_help_on_stderr(
        self, cli, monkeypatch, capsys
    ):
        _patch_chat(
            monkeypatch,
            llm_client.CredentialError(
                llm_client.credential_help("https://api.openai.com/v1")
            ),
        )
        with pytest.raises(SystemExit) as exc:
            cli.run_single_shot("hello")
        assert exc.value.code == 2

        captured = capsys.readouterr()
        assert "/apikey" in captured.err
        assert "OPENAI_API_KEY" in captured.err
        # the error must NOT be on stdout
        assert "Error" not in captured.out
        assert "/apikey" not in captured.out

    def test_sdk_style_error_is_translated_end_to_end(self, cli, monkeypatch, capsys):
        """Even a raw SDK error (bypassing translation) must exit non-zero."""
        _patch_chat(monkeypatch, Exception("Missing credentials. Please pass an `api_key`"))
        with pytest.raises(SystemExit) as exc:
            cli.run_single_shot("hello")
        # untranslated text is not a CredentialError, so this is a generic failure
        assert exc.value.code == 1
        assert "Missing credentials" in capsys.readouterr().err

    def test_generic_error_exits_1_with_stderr_only(self, cli, monkeypatch, capsys):
        _patch_chat(monkeypatch, RuntimeError("Connection refused"))
        with pytest.raises(SystemExit) as exc:
            cli.run_single_shot("hello")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Connection refused" in captured.err
        assert "Connection refused" not in captured.out

    def test_success_exits_zero(self, cli, monkeypatch):
        _patch_chat(monkeypatch, _final_response("hi there"))
        cli.run_single_shot("hello")  # must not raise SystemExit
        assert cli._turn_error is None

    def test_json_mode_emits_a_valid_error_document(self, cli, monkeypatch, capsys):
        cli._output_mode = "json"
        _patch_chat(
            monkeypatch,
            llm_client.CredentialError(
                llm_client.credential_help("https://api.openai.com/v1")
            ),
        )
        with pytest.raises(SystemExit) as exc:
            cli.run_single_shot("hello")
        assert exc.value.code == 2

        captured = capsys.readouterr()
        payload = json.loads(captured.out)  # must parse — that is the whole point
        assert payload["error"]["type"] == "credentials"
        assert "/apikey" in payload["error"]["message"]

    def test_json_mode_success_still_emits_content_and_usage(self, cli, monkeypatch, capsys):
        cli._output_mode = "json"
        _patch_chat(monkeypatch, _final_response("hi there"))
        cli.run_single_shot("hello")  # no SystemExit
        payload = json.loads(capsys.readouterr().out)
        assert payload["content"] == "hi there"
        assert payload["usage"]["total_tokens"] == 3

    def test_report_turn_error_alone_does_not_exit(self, cli, capsys):
        """Interactive mode must survive a failed turn."""
        cli._report_turn_error(RuntimeError("boom"))
        captured = capsys.readouterr()
        assert "boom" in captured.err
        assert cli._turn_error is not None  # recorded, but no SystemExit raised

    def test_missing_model_exits_nonzero_with_a_pick_a_model_hint(
        self, cli, monkeypatch, capsys
    ):
        """The default model is empty on purpose (a local server ships none).

        Single-shot mode must say how to choose one and exit 1 -- a config
        problem, not a credentials one -- instead of sending ``model: ""`` and
        reporting whatever the endpoint replies as the assistant's answer.
        """
        from pengy.core.config import DEFAULTS

        _patch_chat(
            monkeypatch,
            llm_client.ConfigError(
                llm_client.no_model_help(DEFAULTS["base_url"])
            ),
        )
        with pytest.raises(SystemExit) as exc:
            cli.run_single_shot("hello")
        assert exc.value.code == 1

        captured = capsys.readouterr()
        assert "No model is selected" in captured.err
        assert "/models" in captured.err
        assert captured.out == ""  # a failed turn never claims stdout


class TestHandlerIsolation:
    """The CLI's error paths must never write to stdout in json mode."""

    def test_print_stderr_uses_stderr(self, cli, capsys):
        cli._print_stderr("only on stderr")
        captured = capsys.readouterr()
        assert captured.err.strip() == "only on stderr"
        assert captured.out == ""
