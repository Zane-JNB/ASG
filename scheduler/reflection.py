"""The LLM's side of a reflection: it proposes a direction and a magnitude per setting, never a
value. Turning a proposal into a number is preferences' job."""
from pydantic import BaseModel, Field, ValidationError, model_validator

from scheduler.llm_backends import as_items, call_llm
from scheduler.preference_policy import MODEL_DELTAS, Direction, Magnitude


class PreferenceChangeProposal(BaseModel):
    field: str
    direction: Direction
    magnitude: Magnitude
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def field_must_be_adjustable(self):
        if self.field not in MODEL_DELTAS:
            raise ValueError(
                f"'{self.field}' is not an adjustable setting "
                f"(allowed: {', '.join(MODEL_DELTAS)})"
            )
        return self


class ReflectionResult(BaseModel):
    summary: str
    proposals: list[PreferenceChangeProposal] = Field(default_factory=list)


def build_system_prompt(fields: list[str] | None = None) -> str:
    shown = MODEL_DELTAS if fields is None else {n: MODEL_DELTAS[n] for n in fields}
    field_lines = "\n".join(
        f"- {name} (deltas: small={d['small']}, medium={d['medium']}, large={d['large']})"
        for name, d in shown.items()
    )
    return (
        "You help tune a student's schedule-generation preferences based on their reflection "
        "on how their day or week went.\n"
        "You may ONLY propose changes to these fields, each as an increase or decrease by a "
        "small, medium, or large bucket -- you never choose the exact number:\n"
        f"{field_lines}\n\n"
        "Propose 0 to 3 changes. Only propose a change when the reflection clearly supports it -- "
        "when in doubt, propose nothing for that concern. Each proposal needs a one-sentence "
        "reason grounded in what the student actually wrote, not a generic justification.\n\n"
        "Each proposal MUST use exactly these four JSON keys -- do not rename or substitute "
        "any of them:\n"
        '  "field": one of the field names listed above, spelled exactly as shown\n'
        '  "direction": exactly "increase" or "decrease"\n'
        '  "magnitude": exactly "small", "medium", or "large"\n'
        '  "reason": a one-sentence string\n'
        "Example of one correctly-formatted proposal:\n"
        '  {"field": "buffer_slots", "direction": "increase", "magnitude": "medium", '
        '"reason": "mentioned feeling rushed with no breaks between activities"}\n'
        'Never use "change", "size", "amount", or any other alternate name for these keys.'
    )


def _tool_schema(fields):
    schema = ReflectionResult.model_json_schema()
    if fields is not None:
        schema["$defs"]["PreferenceChangeProposal"]["properties"]["field"]["enum"] = list(fields)
    return schema


def propose_preference_changes(reflection_text: str, client=None,
                               allowed_fields: list[str] | None = None) -> ReflectionResult:
    """Ask the LLM to propose bounded preference changes from a reflection.

    Goes through llm_backends.call_llm (Groq; needs GROQ_API_KEY). client: optional
    OpenAI-SDK-shaped stand-in (exposing .chat.completions.create(...)), for testing.
    """
    if allowed_fields is not None and not allowed_fields:  # nothing learnable -> skip the API call
        return ReflectionResult(summary="", proposals=[])

    raw = call_llm(system_prompt=build_system_prompt(allowed_fields), user_message=reflection_text,
                   tool_name="propose_preference_changes", tool_schema=_tool_schema(allowed_fields), client=client)

    # validate each proposal individually -- one hallucinated/locked field shouldn't
    # discard every other, otherwise valid, proposal in the same response
    # null / "none" / JSON text are read loosely; an unreadable shape raises BadModelOutput
    proposals = []
    for item in as_items("proposals", raw.get("proposals"), marker="field"):
        try:
            proposals.append(PreferenceChangeProposal(**item))
        except (ValidationError, TypeError):  # TypeError: an entry that isn't an object
            continue

    summary = raw.get("summary")
    return ReflectionResult(summary=summary if isinstance(summary, str) else "", proposals=proposals)
