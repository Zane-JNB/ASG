import pytest

from scheduler.llm_backends import BadModelOutput, call_llm


SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}

# Every test here drives the real Groq code through a stand-in OpenAI client (no key, no network).
pytestmark = pytest.mark.usefixtures("real_groq_code")


def test_groq_backend_wraps_decommissioned_model_error(monkeypatch):
    """A 404 model_not_found from Groq (e.g. a decommissioned model) should surface as
    a clear RuntimeError pointing at GROQ_MODEL/console.groq.com, not a raw SDK traceback.
    """
    import httpx
    from openai import NotFoundError

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("GROQ_MODEL", "some-decommissioned-model")

    class FakeCompletions:
        def create(self, **kwargs):
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            response = httpx.Response(404, request=request, json={"error": {"message": "not found"}})
            raise NotFoundError("model not found", response=response, body=None)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = FakeChat()

    monkeypatch.setattr("openai.OpenAI", FakeClient)

    with pytest.raises(RuntimeError, match="some-decommissioned-model.*console.groq.com"):
        call_llm("system", "text", "tool", SCHEMA)


def test_groq_backend_raises_bad_model_output_on_tool_schema_mismatch(monkeypatch):
    """When the model's tool call doesn't match our schema (wrong field names, missing
    fields), Groq rejects it server-side with a 400/tool_use_failed. That must raise
    BadModelOutput (a RuntimeError), not pretend the model proposed nothing. Body shape here is the
    UNWRAPPED one Groq actually sends (no outer "error" key) -- confirmed via a live
    failure, not assumed.
    """
    import httpx
    from openai import BadRequestError

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")

    class FakeCompletions:
        def create(self, **kwargs):
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            body = {"message": "Tool call validation failed",
                    "type": "invalid_request_error", "code": "tool_use_failed"}
            response = httpx.Response(400, request=request, json=body)
            raise BadRequestError("tool call validation failed", response=response, body=body)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = FakeChat()

    monkeypatch.setattr("openai.OpenAI", FakeClient)

    with pytest.raises(BadModelOutput, match="didn't match"):
        call_llm("system", "text", "tool", SCHEMA)


def test_groq_backend_raises_bad_model_output_on_wrapped_error_shape_too(monkeypatch):
    """Some SDK/proxy paths wrap the error body in an outer "error" key instead of
    sending it flat. Both shapes must work -- this guards the wrapped one specifically,
    since test_groq_backend_raises_bad_model_output_on_tool_schema_mismatch above already
    covers the real unwrapped shape.
    """
    import httpx
    from openai import BadRequestError

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")

    class FakeCompletions:
        def create(self, **kwargs):
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            body = {"error": {"message": "Tool call validation failed",
                              "type": "invalid_request_error", "code": "tool_use_failed"}}
            response = httpx.Response(400, request=request, json=body)
            raise BadRequestError("tool call validation failed", response=response, body=body)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = FakeChat()

    monkeypatch.setattr("openai.OpenAI", FakeClient)

    with pytest.raises(BadModelOutput, match="didn't match"):
        call_llm("system", "text", "tool", SCHEMA)


def test_groq_backend_reraises_other_bad_request_errors(monkeypatch):
    """A 400 that ISN'T the tool-schema-mismatch case (bad key format, etc.) should still
    surface normally -- only tool_use_failed becomes BadModelOutput.
    """
    import httpx
    from openai import BadRequestError

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")

    class FakeCompletions:
        def create(self, **kwargs):
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            body = {"message": "invalid api key format",
                    "type": "invalid_request_error", "code": "invalid_api_key"}
            response = httpx.Response(400, request=request, json=body)
            raise BadRequestError("invalid api key format", response=response, body=body)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = FakeChat()

    monkeypatch.setattr("openai.OpenAI", FakeClient)

    with pytest.raises(BadRequestError):
        call_llm("system", "text", "tool", SCHEMA)


def test_groq_backend_handles_error_via_duck_typing_not_isinstance(monkeypatch):
    """Regression guard: the handler must work off status_code/body attributes, not
    `except SpecificErrorClass`, since exact class matching proved unreliable on at
    least one real machine even when the class identity checked out manually. A plain
    exception that merely duck-types status_code/body must be handled identically to
    a real openai.BadRequestError.
    """
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")

    class FakeAPIError(Exception):
        """Deliberately NOT a subclass of any openai exception."""
        def __init__(self, status_code, body):
            super().__init__("fake api error")
            self.status_code = status_code
            self.body = body

    class FakeCompletions:
        def create(self, **kwargs):
            raise FakeAPIError(400, {"code": "tool_use_failed"})  # unwrapped, matching real Groq responses

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = FakeChat()

    monkeypatch.setattr("openai.OpenAI", FakeClient)

    with pytest.raises(BadModelOutput, match="didn't match"):
        call_llm("system", "text", "tool", SCHEMA)


def test_groq_backend_reraises_non_api_errors_untouched(monkeypatch):
    """An exception with no status_code at all (network failure, bad key format thrown
    before any HTTP call, etc.) must pass straight through, not get swallowed.
    """
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")

    class FakeCompletions:
        def create(self, **kwargs):
            raise ConnectionError("network is down")

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = FakeChat()

    monkeypatch.setattr("openai.OpenAI", FakeClient)

    with pytest.raises(ConnectionError, match="network is down"):
        call_llm("system", "text", "tool", SCHEMA)

def test_groq_vision_call_is_deterministic_and_sends_the_image(monkeypatch):
    import json
    from types import SimpleNamespace
    from scheduler.llm_backends import call_vision_llm

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    seen = {}

    class FakeCompletions:
        def create(self, **kwargs):
            seen.update(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps({"weekly_patterns": []})))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = FakeChat()

    monkeypatch.setattr("openai.OpenAI", FakeClient)

    result = call_vision_llm("sys", "read this", "QUJD", "image/png", "extract", SCHEMA)

    assert result == {"weekly_patterns": []}
    assert seen["temperature"] == 0  # same image should give the same extraction
    image_part = seen["messages"][1]["content"][1]
    assert image_part["image_url"]["url"] == "data:image/png;base64,QUJD"
    assert seen["tool_choice"]["function"]["name"] == "extract"  # forces structured output

def test_model_ids_have_defaults_and_env_overrides(monkeypatch):
    from scheduler import llm_backends as lb
    for var in ("GROQ_MODEL", "GROQ_VISION_MODEL"):
        monkeypatch.delenv(var, raising=False)
    assert (lb.groq_model(), lb.groq_vision_model()) == ("openai/gpt-oss-120b", "qwen/qwen3.8-27b")
    monkeypatch.setenv("GROQ_MODEL", "g"); monkeypatch.setenv("GROQ_VISION_MODEL", "v")
    assert (lb.groq_model(), lb.groq_vision_model()) == ("g", "v")


def test_groq_is_the_only_provider():
    from scheduler import llm_backends as lb
    assert lb.PROVIDER == "groq"
    assert list(lb._BACKENDS) == list(lb._VISION_BACKENDS) == ["groq"]


def test_injected_client_paths_use_the_central_groq_models(monkeypatch):
    """reflection and schedule_extraction take an OpenAI-shaped client; it must get the
    model from llm_backends, not a hardcoded ID. No real call: the client is a stub."""
    import json
    from types import SimpleNamespace
    from scheduler.reflection import propose_preference_changes
    from scheduler.schedule_extraction import extract_schedule
    monkeypatch.setenv("GROQ_MODEL", "text-model")
    monkeypatch.setenv("GROQ_VISION_MODEL", "vision-model")
    seen = []
    def client_returning(data):
        def create(**kwargs):
            seen.append(kwargs["model"])
            call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(data)))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    propose_preference_changes("fine", client=client_returning({"summary": "", "proposals": []}))
    extract_schedule(b"x", "image/png", client=client_returning({}))
    assert seen == ["text-model", "vision-model"]


class _StatusError(Exception):
    """Duck-types an openai APIStatusError: just a status_code."""
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.body = None


def _groq_raising(monkeypatch, status_code):
    class FakeCompletions:
        def create(self, **kwargs):
            raise _StatusError(status_code)
    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr("openai.OpenAI", FakeClient)


def test_groq_vision_wraps_decommissioned_model_error(monkeypatch):
    from scheduler.llm_backends import call_vision_llm
    _groq_raising(monkeypatch, 404)
    monkeypatch.setenv("GROQ_VISION_MODEL", "retired-vision")
    with pytest.raises(RuntimeError, match="retired-vision.*GROQ_VISION_MODEL"):
        call_vision_llm("system", "text", "QUJD", "image/png", "extract", SCHEMA)


@pytest.mark.parametrize("vision", [False, True])
def test_groq_rate_limit_is_a_clear_runtime_error(monkeypatch, vision):
    from scheduler.llm_backends import call_vision_llm
    _groq_raising(monkeypatch, 429)
    with pytest.raises(RuntimeError, match="rate limit"):
        if vision:
            call_vision_llm("system", "text", "QUJD", "image/png", "extract", SCHEMA)
        else:
            call_llm("system", "text", "tool", SCHEMA)


def test_groq_missing_key_is_a_clear_runtime_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        call_llm("system", "text", "tool", SCHEMA)


def _groq_replying(monkeypatch, tool_calls):
    from types import SimpleNamespace
    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls))])
    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = SimpleNamespace(completions=FakeCompletions())
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr("openai.OpenAI", FakeClient)


@pytest.mark.parametrize("tool_calls", [None, []])
@pytest.mark.parametrize("vision", [False, True])
def test_groq_reply_without_a_tool_call_is_a_clear_runtime_error(monkeypatch, tool_calls, vision):
    from scheduler.llm_backends import call_vision_llm
    _groq_replying(monkeypatch, tool_calls)
    env_var = "GROQ_VISION_MODEL" if vision else "GROQ_MODEL"
    with pytest.raises(BadModelOutput, match=f"structured answer.*{env_var}"):
        if vision:
            call_vision_llm("system", "text", "QUJD", "image/png", "extract", SCHEMA)
        else:
            call_llm("system", "text", "tool", SCHEMA)


@pytest.mark.parametrize("arguments", ["not json", "[1, 2]"])
def test_groq_tool_call_with_bad_arguments_is_bad_model_output(monkeypatch, arguments):
    from types import SimpleNamespace
    _groq_replying(monkeypatch, [SimpleNamespace(function=SimpleNamespace(arguments=arguments))])
    with pytest.raises(BadModelOutput):
        call_llm("system", "text", "tool", SCHEMA)


def test_groq_vision_tool_use_failed_is_bad_model_output(monkeypatch):
    from scheduler.llm_backends import call_vision_llm
    class ToolUseFailed(Exception):
        status_code = 400
        body = {"code": "tool_use_failed"}
    class FakeClient:
        def __init__(self, *a, **kw):
            def create(**kwargs):
                raise ToolUseFailed()
            self.chat = type("Chat", (), {"completions": type("C", (), {"create": staticmethod(create)})()})()
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr("openai.OpenAI", FakeClient)
    with pytest.raises(BadModelOutput, match="GROQ_VISION_MODEL"):
        call_vision_llm("system", "text", "QUJD", "image/png", "extract", SCHEMA)


def test_rate_limit_is_not_bad_model_output(monkeypatch):
    _groq_raising(monkeypatch, 429)
    with pytest.raises(RuntimeError) as info:
        call_llm("system", "text", "tool", SCHEMA)
    assert not isinstance(info.value, BadModelOutput)


def test_groq_error_body_with_a_string_error_still_maps_to_a_clear_message(monkeypatch):
    class StringBodyError(Exception):
        status_code = 429
        body = {"error": "Rate limit exceeded"}  # not a dict: must not crash the handler
    class FakeCompletions:
        def create(self, **kwargs):
            raise StringBodyError("429")
    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr("openai.OpenAI", FakeClient)
    with pytest.raises(RuntimeError, match="rate limit"):
        call_llm("system", "text", "tool", SCHEMA)


def test_tests_run_offline_whatever_the_shell_has():
    """conftest removes the key, so a real GROQ_API_KEY in the shell can never reach a test."""
    import os
    assert "GROQ_API_KEY" not in os.environ


@pytest.mark.parametrize("vision", [False, True])
def test_groq_rejected_key_is_a_clear_runtime_error(monkeypatch, vision):
    from scheduler.llm_backends import call_vision_llm
    _groq_raising(monkeypatch, 401)
    with pytest.raises(RuntimeError, match="rejected GROQ_API_KEY.*console.groq.com/keys"):
        if vision:
            call_vision_llm("system", "text", "QUJD", "image/png", "extract", SCHEMA)
        else:
            call_llm("system", "text", "tool", SCHEMA)


@pytest.mark.parametrize("body", [
    {"error": {"message": "Limit reached on tokens per minute (TPM)", "code": "rate_limit_exceeded"}},
    {"message": "Limit reached on tokens per minute (TPM)", "code": "rate_limit_exceeded"},
])
def test_groq_rate_limit_shows_which_limit_groq_says_was_hit(monkeypatch, body):
    class LimitError(Exception):
        status_code = 429
    error = LimitError("429"); error.body = body
    class FakeCompletions:
        def create(self, **kwargs):
            raise error
    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr("openai.OpenAI", FakeClient)
    with pytest.raises(RuntimeError, match=r"rate limit.*Groq says: .*tokens per minute \(TPM\)"):
        call_llm("system", "text", "tool", SCHEMA)


def test_rate_limit_without_a_body_keeps_the_plain_message(monkeypatch):
    _groq_raising(monkeypatch, 429)
    with pytest.raises(RuntimeError) as info:
        call_llm("system", "text", "tool", SCHEMA)
    assert str(info.value) == "Groq's free-tier rate limit was hit. Wait a minute and try again."
