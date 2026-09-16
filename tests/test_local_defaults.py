"""The local-first defaults, and what happens when they cannot be used.

Pengy's audience runs its own model server, so the default endpoint is Ollama's
OpenAI-compatible port (which needs no API key) and there is **deliberately no
default model**: a local server ships none of its own -- a fresh ``ollama list``
is empty -- so naming one would be a lie that fails on the user's first message.

These tests pin the consequences of that choice:

* an unset model produces Pengy's own "pick a model" instructions, and does so
  *before* any request is attempted, instead of forwarding ``model: ""`` and
  showing whatever the endpoint says about it;
* an endpoint that never answers names the URL, and points at ``ollama serve`` /
  ``/baseurl`` when the endpoint is a local one (with a local default, the
  likeliest first-run failure is "your own server is not running");
* the OpenAI SDK's refusal to build a client with an empty key -- "Missing
  credentials... or set the `OPENAI_API_KEY` ... environment variable", which is
  the message the 1.8.4 work removed -- cannot reach a user of that default.
"""

from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from pengy.core import llm_client
from pengy.core.config import DEFAULTS
from pengy.core.llm_client import (
    NO_KEY_PLACEHOLDER,
    ConfigError,
    LLMClient,
    is_local_endpoint,
    no_model_help,
    unreachable_help,
)


def _connection_error() -> APIConnectionError:
    """The SDK error for a refused connection or an unreachable host."""
    request = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    return APIConnectionError(request=request)


class TestDefaultsAreLocalAndKeyless:
    def test_default_endpoint_is_a_local_server_not_a_hosted_api(self):
        assert DEFAULTS["base_url"] == "http://127.0.0.1:11434/v1"
        assert "openai" not in DEFAULTS["base_url"].lower()
        # A local server needs no key, and asking for one is the first thing that
        # makes a new user think they have to pay for something.
        assert DEFAULTS["api_key"] == ""

    def test_there_is_no_default_model(self):
        assert DEFAULTS["model"] == ""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:11434/v1",
        "http://127.0.0.1:8080/v1",
        "http://localhost:11434/v1",
        "http://0.0.0.0:11434/v1",
        "http://[::1]:11434/v1",
    ])
    def test_local_endpoints_are_recognised(self, url):
        assert is_local_endpoint(url)

    @pytest.mark.parametrize("url", [
        "https://api.openai.com/v1",
        "https://api.groq.com/openai/v1",
        "http://192.168.1.50:11434/v1",
        "",
    ])
    def test_remote_endpoints_are_not_local(self, url):
        assert not is_local_endpoint(url)


class TestNoModelSelected:
    def test_help_says_how_to_choose_one(self):
        help_text = no_model_help("http://127.0.0.1:11434/v1")
        assert "http://127.0.0.1:11434/v1" in help_text
        for expected in ("/models", "/model ", "ollama pull", "Fetch Models"):
            assert expected in help_text, expected

    def test_chat_refuses_before_sending_anything(self, monkeypatch):
        """No request may be attempted -- the endpoint never gets to answer."""
        def explode(self):  # pragma: no cover - must not be reached
            raise AssertionError("the SDK client must not be built without a model")

        monkeypatch.setattr(LLMClient, "client", property(explode))
        client = LLMClient(base_url=DEFAULTS["base_url"], api_key="", model="")
        with pytest.raises(ConfigError) as exc:
            next(client.chat([{"role": "user", "content": "hi"}]))
        assert "No model is selected" in str(exc.value)
        assert exc.value.kind == "config"

    def test_whitespace_model_counts_as_unset(self):
        client = LLMClient(base_url=DEFAULTS["base_url"], api_key="", model="   ")
        with pytest.raises(ConfigError):
            next(client.chat([{"role": "user", "content": "hi"}]))

    def test_an_explicit_model_still_works(self, monkeypatch):
        """Per-tab / per-request model selection must survive the guard."""
        sentinel = RuntimeError("reached the request")

        class _Completions:
            def __init__(self):
                self.called_with = None

            def create(self, **kwargs):
                self.called_with = kwargs
                raise sentinel

        class _Client:
            def __init__(self):
                self.completions = _Completions()
                self.chat = type("_Chat", (), {"completions": self.completions})()

        fake = _Client()
        monkeypatch.setattr(LLMClient, "client", property(lambda self: fake))

        client = LLMClient(base_url=DEFAULTS["base_url"], api_key="", model="")
        with pytest.raises(RuntimeError) as exc:
            next(client.chat([{"role": "user", "content": "hi"}], model="llama3.2"))
        assert exc.value is sentinel
        assert fake.completions.called_with["model"] == "llama3.2"


class TestUnreachableEndpoint:
    def test_local_endpoint_gets_server_starting_advice(self):
        text = unreachable_help("http://127.0.0.1:11434/v1", "Connection error.")
        assert "Nothing answered at http://127.0.0.1:11434/v1" in text
        assert "ollama serve" in text
        assert "/baseurl" in text

    def test_remote_endpoint_is_not_told_about_ollama(self):
        text = unreachable_help("https://api.example.com/v1", "Connection error.")
        assert "Could not reach https://api.example.com/v1" in text
        assert "ollama" not in text.lower()

    def test_connection_errors_are_translated(self):
        translated = llm_client._translate_api_error(
            _connection_error(), "http://127.0.0.1:11434/v1"
        )
        assert "Nothing answered at" in str(translated)
        assert "ollama serve" in str(translated)

    def test_timeouts_get_the_same_treatment(self):
        request = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
        translated = llm_client._translate_api_error(
            APITimeoutError(request=request), "http://127.0.0.1:11434/v1"
        )
        assert "Nothing answered at" in str(translated)

    def test_other_errors_are_left_alone(self):
        original = ValueError("something else")
        assert llm_client._translate_api_error(original, DEFAULTS["base_url"]) is original


class TestEmptyKeyDoesNotReachTheSdk:
    def test_client_builds_with_no_key_configured(self):
        """The SDK would raise 'Missing credentials ... or set OPENAI_API_KEY'."""
        client = LLMClient(base_url=DEFAULTS["base_url"], api_key="", model="llama3.2")
        assert client.client is not None
        assert client.client.api_key == NO_KEY_PLACEHOLDER

    def test_a_configured_key_is_used_verbatim(self):
        client = LLMClient(base_url=DEFAULTS["base_url"], api_key="sk-real", model="x")
        assert client.client.api_key == "sk-real"
