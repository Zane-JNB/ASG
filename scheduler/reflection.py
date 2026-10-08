from dataclasses import fields
from typing import Literal

import annotated_types
from pydantic import BaseModel, Field, ValidationError, model_validator

from scheduler.models import ProfileSettings
from scheduler.preference_policy import MODEL_DELTAS  

ADJUSTABLE_FIELDS: dict[str, dict[str, int]] = MODEL_DELTAS   


class PreferenceChangeProposal(BaseModel):
    field: str
    direction: Literal["increase", "decrease"]
    magnitude: Literal["small", "medium", "large"]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def field_must_be_adjustable(self):
        if self.field not in ADJUSTABLE_FIELDS:
            raise ValueError(
                f"'{self.field}' is not an adjustable setting "
                f"(allowed: {', '.join(ADJUSTABLE_FIELDS)})"
            )
        return self

class ReflectionResult(BaseModel):
    summary: str
    proposals: list[PreferenceChangeProposal] = Field(default_factory=list)

def _lower_bound(field_name: str) -> int:
    """The smallest value ProfileSettings' own Field(...) constraint allows for this field."""
    for constraint in ProfileSettings.model_fields[field_name].metadata:
        if isinstance(constraint, annotated_types.Gt):
            return constraint.gt + 1
        if isinstance(constraint, annotated_types.Ge):
            return constraint.ge
    return 0

def apply_proposal(settings: ProfileSettings, proposal: PreferenceChangeProposal) -> ProfileSettings:
    """Apply one proposal's bounded delta, clamped to this field's valid range."""
    delta = ADJUSTABLE_FIELDS[proposal.field][proposal.magnitude]
    current = getattr(settings, proposal.field)
    new_value = current + delta if proposal.direction == "increase" else current - delta
    new_value = max(new_value, _lower_bound(proposal.field))
    upper = _upper_bound(proposal.field)
    if upper is not None:
        new_value = min(new_value, upper)

    data = settings.model_dump()
    data[proposal.field] = new_value
    return ProfileSettings(**data)

def apply_all(settings: ProfileSettings, proposals: list[PreferenceChangeProposal]) -> ProfileSettings:
    for proposal in proposals:
        settings = apply_proposal(settings, proposal)
    return settings

def build_system_prompt(fields: list[str] | None = None) -> str:
    shown = ADJUSTABLE_FIELDS if fields is None else {n: ADJUSTABLE_FIELDS[n] for n in fields}
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

def propose_preference_changes(reflection_text: str, client=None, allowed_fields = None) -> ReflectionResult:
    """Ask the LLM to propose bounded preference changes from a reflection.

    client is an optional Anthropic-SDK-shaped override (exposing .messages.create(...)),
    used mainly for testing. When omitted, this dispatches through llm_backends.call_llm,
    which reads LLM_BACKEND from the environment -- defaulting to a free offline stub so
    development doesn't require an API key at all (see llm_backends.py).
    """
    if allowed_fields is not None and not allowed_fields:  # nothing learnable -> skip the API call
        return ReflectionResult(summary="", proposals=[])
    
    from scheduler.llm_backends import _anthropic_call, call_llm
    args = dict(system_prompt=build_system_prompt(allowed_fields), user_message=reflection_text,
                tool_name="propose_preference_changes", tool_schema=_tool_schema(allowed_fields))
    raw = _anthropic_call(**args, client=client) if client is not None else call_llm(**args)

    # validate each proposal individually -- one hallucinated/locked field shouldn't
    # discard every other, otherwise valid, proposal in the same response
    proposals = []
    for item in raw.get("proposals", []):
        try:
            proposals.append(PreferenceChangeProposal(**item))
        except ValidationError:
            continue

    return ReflectionResult(summary=raw.get("summary", ""), proposals=proposals)
    
def _upper_bound(field_name: str) -> int | None:  
    for constraint in ProfileSettings.model_fields[field_name].metadata:
        if isinstance(constraint, annotated_types.Lt):
            return constraint.lt - 1
        if isinstance(constraint, annotated_types.Le):
            return constraint.le
    return None
    