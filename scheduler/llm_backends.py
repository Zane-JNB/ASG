import json
import os


def _anthropic_call(system_prompt: str, user_message: str, tool_name: str, tool_schema: dict) -> dict:
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        system=system_prompt,
        tools=[{"name": tool_name, "description": tool_schema.get("description", ""),
                "input_schema": tool_schema}],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user_message}],
    )
    block = next(b for b in response.content if b.type == "tool_use")
    return block.input


def _groq_call(system_prompt: str, user_message: str, tool_name: str, tool_schema: dict) -> dict:
    # Groq's API is OpenAI-compatible -- the `openai` package works, pointed at Groq's base_url.
    # Free tier, no credit card required: https://console.groq.com
    from openai import OpenAI
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=os.environ["GROQ_API_KEY"])
    model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")  #  -- Groq decommissioned
    # llama-3.1-8b-instant and llama-3.3-70b-versatile in Aug 2026; gpt-oss-120b follows
    # the tool schema more reliably than the smaller 20b. Groq's free-tier catalog
    # churns -- override with GROQ_MODEL if this one stops working too.
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            tools=[{
                "type": "function",
                "function": {"name": tool_name, "description": tool_schema.get("description", ""),
                            "parameters": tool_schema},
            }],
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
    except Exception as e:
        status_code = getattr(e, "status_code", None)
        if status_code is None:
            raise
        body = getattr(e, "body", None)
        body = body if isinstance(body, dict) else {}
        code = body.get("code") or body.get("error", {}).get("code")
        print("### DEBUG: extracted code =", repr(code), file=__import__("sys").stderr)
        if status_code == 404:
            raise RuntimeError(
                f"Groq model '{model}' isn't available -- it may have been decommissioned "
                "(Groq's free-tier catalog changes often). Check current models at "
                "https://console.groq.com/docs/models and set the GROQ_MODEL env var to override."
            ) from e
        if status_code == 400 and code == "tool_use_failed":
            return {
                "summary": (
                    "The model's response didn't match the expected proposal format "
                    "this time (can happen with faster/smaller models) -- no changes "
                    "proposed. Try again, or set GROQ_MODEL to a stronger model."
                ),
                "proposals": [],
            }
        print("### DEBUG: fell through both conditions, re-raising ###", file=__import__("sys").stderr)
        raise
    call = response.choices[0].message.tool_calls[0]
    return json.loads(call.function.arguments)

def _fake_call(system_prompt: str, user_message: str, tool_name: str, tool_schema: dict) -> dict:
    """A tiny offline heuristic -- NOT a real preference-change engine. It exists purely so the
    rest of the app (db writes, main.py flow, UI) can be built and tested with zero network
    calls and zero cost, before you have any API key at all. Replace with a real backend
    once you're evaluating actual reflection-understanding quality.
    """
    text = user_message.lower()
    proposals = []
    if any(w in text for w in ("rushed", "no break", "no time", "back to back")):
        proposals.append({"field": "buffer_slots", "direction": "increase", "magnitude": "medium",
                          "reason": "mentioned feeling rushed or lacking breaks (fake backend)"})
    if any(w in text for w in ("tired", "exhausted", "didn't sleep", "not enough sleep")):
        proposals.append({"field": "sleep_target_penalty", "direction": "increase", "magnitude": "small",
                          "reason": "mentioned tiredness or insufficient sleep (fake backend)"})
    return {"summary": "Offline fake-backend response -- no real LLM call was made.",
            "proposals": proposals}


_BACKENDS = {"anthropic": _anthropic_call, "groq": _groq_call, "fake": _fake_call}
# _BACKENDS = {"groq": _groq_call, "fake": _fake_call}


def call_llm(system_prompt: str, user_message: str, tool_name: str, tool_schema: dict) -> dict:
    """Dispatch to whichever backend LLM_BACKEND names (env var; defaults to 'fake' -- free,
    offline, no key needed). Set LLM_BACKEND=groq (with GROQ_API_KEY) or LLM_BACKEND=anthropic
    (with ANTHROPIC_API_KEY) to use a real model.
    """
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend not in _BACKENDS:
        raise ValueError(f"unknown LLM_BACKEND '{backend}' (choose from: {', '.join(_BACKENDS)})")
    return _BACKENDS[backend](system_prompt, user_message, tool_name, tool_schema)

def _anthropic_vision_call(system_prompt: str, user_text: str, image_base64: str, media_type: str,
                           tool_name: str, tool_schema: dict) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    content = [{"type": "text", "text": user_text}]
    if media_type == "application/pdf":
        content.insert(0, {"type": "document",
                           "source": {"type": "base64", "media_type": media_type, "data": image_base64}})
    else:
        content.insert(0, {"type": "image",
                           "source": {"type": "base64", "media_type": media_type, "data": image_base64}})

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2048,
        system=system_prompt,
        tools=[{"name": tool_name, "description": tool_schema.get("description", ""),
                "input_schema": tool_schema}],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": content}],
    )
    block = next(b for b in response.content if b.type == "tool_use")
    return block.input


def _groq_vision_call(system_prompt: str, user_text: str, image_base64: str, media_type: str,
                      tool_name: str, tool_schema: dict) -> dict:
    if media_type == "application/pdf":
        raise ValueError(
            "Groq's vision model doesn't support PDF input directly (images only, per their docs). "
            "Convert the PDF's first page to an image first, or use LLM_BACKEND=anthropic for PDFs."
        )
    from openai import OpenAI
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=os.environ["GROQ_API_KEY"])
    model = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")  # Groq's only vision model as of writing
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_base64}"}},
            ]},
        ],
        tools=[{
            "type": "function",
            "function": {"name": tool_name, "description": tool_schema.get("description", ""),
                        "parameters": tool_schema},
        }],
        tool_choice={"type": "function", "function": {"name": tool_name}},
    )
    call = response.choices[0].message.tool_calls[0]
    return json.loads(call.function.arguments)


def _fake_vision_call(system_prompt: str, user_text: str, image_base64: str, media_type: str,
                      tool_name: str, tool_schema: dict) -> dict:
    """Offline stub -- can't actually read the image. Returns an empty result so the extraction
    pipeline (review screen, db writes) can be built and tested with zero cost and no real
    document, before you're ready to spend even Groq's free-tier rate limit on it.
    """
    return {"weekly_patterns": [], "tasks": []}


_VISION_BACKENDS = {"anthropic": _anthropic_vision_call, "groq": _groq_vision_call, "fake": _fake_vision_call}


def call_vision_llm(system_prompt: str, user_text: str, image_base64: str, media_type: str,
                    tool_name: str, tool_schema: dict) -> dict:
    """Same LLM_BACKEND-driven dispatch as call_llm, but for image/PDF input. 'anthropic' and
    'groq' both work for images; only 'anthropic' currently handles PDFs (see _groq_vision_call).
    """
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend not in _VISION_BACKENDS:
        raise ValueError(f"unknown LLM_BACKEND '{backend}' (choose from: {', '.join(_VISION_BACKENDS)})")
    return _VISION_BACKENDS[backend](system_prompt, user_text, image_base64, media_type, tool_name, tool_schema)