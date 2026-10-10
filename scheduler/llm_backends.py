import json
import os

# Model IDs live only here. Each can be overridden by the env var its getter reads.
# Groq's free-tier catalog churns (llama-3.1-8b-instant and llama-3.3-70b-versatile were
# decommissioned in Aug 2026); gpt-oss-120b follows the tool schema more reliably than the 20b.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GROQ_VISION_MODEL = "qwen/qwen3.8-27b"  # Groq's only vision model as of writing

# Groq is the only provider for now. Other providers join the tables at the bottom of this file
# at the model comparison (4.1); a task/depth-based router (4.0) then replaces this constant.
PROVIDER = "groq"


def groq_model() -> str:
    return os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)


def groq_vision_model() -> str:
    return os.environ.get("GROQ_VISION_MODEL", DEFAULT_GROQ_VISION_MODEL)


class BadModelOutput(RuntimeError):
    """The model answered, but not in the forced tool format (Groq's tool_use_failed, plain text
    instead of a tool call, or arguments that aren't JSON). Retrying or another path may work;
    a rate limit, key or model error is a plain RuntimeError and stops at once."""


def is_backend_failure(e: Exception) -> bool:
    """Failures a student can hit with working code: a retired model, rate limit, missing key
    (RuntimeError), bad model output (ValueError, incl. pydantic/JSON), network (OSError),
    a backend package not installed (ImportError), or any API/SDK error. Duck-typed on
    status_code and on the SDK's package name. Anything else (TypeError, AttributeError,
    IndexError...) is a bug and should not be hidden. Entry points use this to report, not crash."""
    if isinstance(e, (RuntimeError, ValueError, OSError, ImportError)):
        return True
    if getattr(e, "status_code", None) is not None:
        return True
    return type(e).__module__.split(".")[0] in ("openai", "httpx")


def _groq_client():
    # Groq's API is OpenAI-compatible -- the `openai` package works, pointed at Groq's base_url.
    # Free tier, no credit card required: https://console.groq.com
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("Groq needs GROQ_API_KEY set (free key: https://console.groq.com).")
    from openai import OpenAI
    return OpenAI(base_url="https://api.groq.com/openai/v1", api_key=key)


def _groq_error_detail(e: Exception) -> str:
    """Groq's own explanation from the error body (e.g. which rate limit: requests, tokens or
    daily), or "" when there is none. The body can be flat, wrapped in "error", or a string."""
    body = getattr(e, "body", None)
    if not isinstance(body, dict):
        return ""
    inner = body.get("error")
    if isinstance(inner, dict):
        return str(inner.get("message") or "")
    if isinstance(inner, str):
        return inner
    return str(body.get("message") or "")


def _groq_status_error(e: Exception, model: str, env_var: str) -> RuntimeError | None:
    """A clear RuntimeError for Groq failures a student can act on, else None (caller re-raises).
    Duck-typed on status_code on purpose (see test_groq_backend_handles_error_via_duck_typing...)."""
    status_code = getattr(e, "status_code", None)
    if status_code == 401:
        return RuntimeError(
            "Groq rejected GROQ_API_KEY (invalid or revoked). Make a new key at "
            "https://console.groq.com/keys, set it, and reopen your terminal.")
    if status_code == 429:
        detail = _groq_error_detail(e)
        return RuntimeError("Groq's free-tier rate limit was hit. Wait a minute and try again."
                            + (f" Groq says: {detail}" if detail else ""))
    if status_code == 404:
        return RuntimeError(
            f"Groq model '{model}' isn't available -- it may have been decommissioned "
            "(Groq's free-tier catalog changes often). Check current models at "
            f"https://console.groq.com/docs/models and set the {env_var} env var to override.")
    return None


def _bad_output_error(e: Exception, model: str, env_var: str) -> BadModelOutput | None:
    """BadModelOutput for Groq's 400 tool_use_failed (body flat or wrapped in "error"), else None."""
    if getattr(e, "status_code", None) != 400:
        return None
    body = getattr(e, "body", None)
    body = body if isinstance(body, dict) else {}
    inner = body.get("error")  # can be a dict, a plain string, or missing
    code = body.get("code") or (inner.get("code") if isinstance(inner, dict) else None)
    if code != "tool_use_failed":
        return None
    return BadModelOutput(
        f"Groq model '{model}' didn't match the expected answer format this time. "
        f"Try again, or set the {env_var} env var to a stronger model.")


def _groq_request(model: str, env_var: str, messages: list[dict], tool_name: str, tool_schema: dict,
                  client=None, **options) -> dict:
    """One forced tool call to Groq: the tool's arguments, or a clear error a student can act on.
    env_var is the one that overrides `model`. client: optional OpenAI-SDK-shaped stand-in."""
    if client is None:
        client = _groq_client()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=[{
                "type": "function",
                "function": {"name": tool_name, "description": tool_schema.get("description", ""),
                             "parameters": tool_schema},
            }],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            **options,
        )
    except Exception as e:
        clear = _groq_status_error(e, model, env_var) or _bad_output_error(e, model, env_var)
        if clear is None:
            raise
        raise clear from e
    return _tool_call_arguments(response, model, env_var)


def _groq_call(system_prompt: str, user_message: str, tool_name: str, tool_schema: dict,
               client=None) -> dict:
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}]
    return _groq_request(groq_model(), "GROQ_MODEL", messages, tool_name, tool_schema, client)


def _tool_call_arguments(response, model: str, env_var: str) -> dict:
    """The forced tool call's arguments. A model can still reply with plain text instead
    (tool_calls None or empty); say so clearly rather than failing on a None index."""
    tool_calls = response.choices[0].message.tool_calls if response.choices else None
    if not tool_calls:
        raise BadModelOutput(
            f"Groq model '{model}' replied without the structured answer this time. "
            f"Try again, or set the {env_var} env var to a stronger model.")
    try:
        args = json.loads(tool_calls[0].function.arguments)
    except (TypeError, ValueError) as e:
        raise BadModelOutput(f"Groq model '{model}' sent an answer that isn't valid JSON. Try again.") from e
    if not isinstance(args, dict):
        raise BadModelOutput(f"Groq model '{model}' sent an answer in the wrong shape. Try again.")
    return args


_BACKENDS = {"groq": _groq_call}


def call_llm(system_prompt: str, user_message: str, tool_name: str, tool_schema: dict, client=None) -> dict:
    """Send a text prompt to the current provider (Groq; needs GROQ_API_KEY). A client stand-in
    is OpenAI-SDK shaped, so it always runs the Groq code (tests inject one; no real call)."""
    backend = _BACKENDS[PROVIDER] if client is None else _groq_call
    return backend(system_prompt, user_message, tool_name, tool_schema, client=client)


def _groq_vision_call(system_prompt: str, user_text: str, image_base64: str, media_type: str,
                      tool_name: str, tool_schema: dict, client=None) -> dict:
    if media_type == "application/pdf":
        raise ValueError(
            "Groq's vision model doesn't support PDF input directly (images only, per their docs). "
            "Convert the PDF's pages to images first (import does this for you)."
        )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_base64}"}},
        ]},
    ]
    return _groq_request(groq_vision_model(), "GROQ_VISION_MODEL", messages, tool_name, tool_schema, client,
                         temperature=0)


_VISION_BACKENDS = {"groq": _groq_vision_call}


def call_vision_llm(system_prompt: str, user_text: str, image_base64: str, media_type: str,
                    tool_name: str, tool_schema: dict, client=None) -> dict:
    """Same as call_llm, but for image input. Groq's vision model takes images only, not PDFs
    (see _groq_vision_call); PDFs go through pdf_extraction instead."""
    backend = _VISION_BACKENDS[PROVIDER] if client is None else _groq_vision_call
    return backend(system_prompt, user_text, image_base64, media_type, tool_name, tool_schema, client=client)


NONE_WORDS = {"", "none", "null", "n/a", "na", "-"}  # what models write for "nothing"


def as_items(key: str, value, marker: str = "title") -> list:
    """A list key's value as a list. Also accepts what models send for "none" (null, {}, "",
    "N/A"...), one item on its own (recognised by its `marker` key), and a list sent as JSON text.
    Each entry is then validated on its own by the caller, so a bad entry is dropped and the rest
    kept. Anything else (a number, true/false, other text, an object without the marker) raises
    BadModelOutput: the answer can't be trusted, so the caller fails it (and warns) instead of crashing."""
    sent = type(value).__name__
    if isinstance(value, str):
        if value.strip().lower() in NONE_WORDS:
            return []
        try:
            value = json.loads(value)
        except ValueError:
            raise BadModelOutput(f"The model sent '{key}' in the wrong shape ({sent}). Try again.") from None
    if value is None or value == {}:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and marker in value:
        return [value]
    raise BadModelOutput(f"The model sent '{key}' in the wrong shape ({sent}). Try again.")
