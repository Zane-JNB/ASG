import pytest

from scheduler.llm_backends import call_llm


SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}


def test_default_backend_is_fake_and_needs_no_key(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    result = call_llm("system", "I felt rushed today", "tool", SCHEMA)
    assert "summary" in result
    assert "proposals" in result


def test_fake_backend_detects_rushed_keyword(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    result = call_llm("system", "It felt so rushed, no breaks", "tool", SCHEMA)
    fields = [p["field"] for p in result["proposals"]]
    assert "buffer_slots" in fields


def test_fake_backend_detects_tired_keyword(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    result = call_llm("system", "I'm so exhausted lately", "tool", SCHEMA)
    fields = [p["field"] for p in result["proposals"]]
    assert "sleep_target_penalty" in fields


def test_fake_backend_no_matches_returns_no_proposals(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    result = call_llm("system", "Everything was fine today, nothing to report", "tool", SCHEMA)
    assert result["proposals"] == []


def test_unknown_backend_raises_clear_error(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "not-a-real-backend")
    with pytest.raises(ValueError, match="unknown LLM_BACKEND"):
        call_llm("system", "text", "tool", SCHEMA)


def test_explicit_fake_backend_via_env(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "fake")
    result = call_llm("system", "text", "tool", SCHEMA)
    assert result["summary"].startswith("Offline fake-backend response")


def test_groq_backend_wraps_decommissioned_model_error(monkeypatch):
    """A 404 model_not_found from Groq (e.g. a decommissioned model) should surface as
    a clear RuntimeError pointing at GROQ_MODEL/console.groq.com, not a raw SDK traceback.
    """
    import httpx
    from openai import NotFoundError

    monkeypatch.setenv("LLM_BACKEND", "groq")
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


def test_groq_backend_degrades_gracefully_on_tool_schema_mismatch(monkeypatch):
    """When the model's tool call doesn't match our schema (wrong field names, missing
    fields), Groq rejects it server-side with a 400/tool_use_failed. That should come
    back as zero proposals, not crash the whole reflection. Body shape here is the
    UNWRAPPED one Groq actually sends (no outer "error" key) -- confirmed via a live
    failure, not assumed.
    """
    import httpx
    from openai import BadRequestError

    monkeypatch.setenv("LLM_BACKEND", "groq")
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

    result = call_llm("system", "text", "tool", SCHEMA)
    assert result["proposals"] == []
    assert "didn't match" in result["summary"]


def test_groq_backend_degrades_gracefully_on_wrapped_error_shape_too(monkeypatch):
    """Some SDK/proxy paths wrap the error body in an outer "error" key instead of
    sending it flat. Both shapes must work -- this guards the wrapped one specifically,
    since test_groq_backend_degrades_gracefully_on_tool_schema_mismatch above already
    covers the real unwrapped shape.
    """
    import httpx
    from openai import BadRequestError

    monkeypatch.setenv("LLM_BACKEND", "groq")
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

    result = call_llm("system", "text", "tool", SCHEMA)
    assert result["proposals"] == []
    assert "didn't match" in result["summary"]


def test_groq_backend_reraises_other_bad_request_errors(monkeypatch):
    """A 400 that ISN'T the tool-schema-mismatch case (bad key format, etc.) should still
    surface normally -- only tool_use_failed gets the soft-degrade treatment.
    """
    import httpx
    from openai import BadRequestError

    monkeypatch.setenv("LLM_BACKEND", "groq")
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
    monkeypatch.setenv("LLM_BACKEND", "groq")
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

    result = call_llm("system", "text", "tool", SCHEMA)
    assert result["proposals"] == []
    assert "didn't match" in result["summary"]


def test_groq_backend_reraises_non_api_errors_untouched(monkeypatch):
    """An exception with no status_code at all (network failure, bad key format thrown
    before any HTTP call, etc.) must pass straight through, not get swallowed.
    """
    monkeypatch.setenv("LLM_BACKEND", "groq")
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

    monkeypatch.setenv("LLM_BACKEND", "groq")
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