import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from scheduler.models import ProfileSettings
from scheduler.preferences import stepped_value
from scheduler.preference_policy import MODEL_DELTAS
from scheduler.reflection import (
    PreferenceChangeProposal, ReflectionResult, propose_preference_changes,
)


class FakeClient:
    """OpenAI-SDK-shaped stand-in (what Groq's client looks like): replies with one tool call."""
    def __init__(self, arguments: dict):
        self.chat = SimpleNamespace(completions=self)
        call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(arguments)))
        self._response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])

    def create(self, **kwargs):
        return self._response


def make_client(summary: str, proposals: list[dict]) -> FakeClient:
    return FakeClient({"summary": summary, "proposals": proposals})


def test_locked_fields_are_rejected():
    locked = {"presence_bonus", "sleep_min_penalty", "default_sleep_min_slots",
              "default_earliest_bed", "default_latest_bed"}
    assert locked.isdisjoint(MODEL_DELTAS)
    for field in locked:
        with pytest.raises(ValidationError):
            PreferenceChangeProposal(field=field, direction="decrease", magnitude="large", reason="x")


def test_adjustable_field_is_accepted():
    p = PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                 magnitude="small", reason="felt rushed")
    assert p.field == "buffer_slots"


def test_reason_cannot_be_empty():
    with pytest.raises(ValidationError):
        PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                 magnitude="small", reason="")


def test_stepped_value_increase():
    s = ProfileSettings()
    assert stepped_value(s, "buffer_slots", "increase", "medium") == s.buffer_slots + MODEL_DELTAS["buffer_slots"]["medium"]


def test_stepped_value_decrease():
    s = ProfileSettings(same_day_penalty=3000)
    assert stepped_value(s, "same_day_penalty", "decrease", "small") == 3000 - MODEL_DELTAS["same_day_penalty"]["small"]


def test_stepped_value_clamps_at_lower_bound():
    s = ProfileSettings(buffer_slots=1)  # delta of 4, would go negative
    assert stepped_value(s, "buffer_slots", "decrease", "large") == 0  # ge=0: clamped there, not negative


def test_stepped_value_clamps_gt_zero_field_at_one():
    s = ProfileSettings(sleep_target_penalty=100)  # would go to -2400
    assert stepped_value(s, "sleep_target_penalty", "decrease", "large") == 1  # gt=0: 1 is the smallest valid int


def test_the_reflection_module_holds_no_settings_arithmetic():
    """The LLM only proposes (direction + magnitude); the numbers are preferences' job."""
    import scheduler.reflection as reflection
    assert not hasattr(reflection, "apply_proposal") and not hasattr(reflection, "_bounds")






def test_propose_preference_changes_parses_valid_response():
    client = make_client(
        "Felt rushed between tasks.",
        [{"field": "buffer_slots", "direction": "increase", "magnitude": "medium",
          "reason": "mentioned feeling rushed"}],
    )
    result = propose_preference_changes("I felt rushed today", client=client)
    assert isinstance(result, ReflectionResult)
    assert len(result.proposals) == 1
    assert result.proposals[0].field == "buffer_slots"


def test_propose_preference_changes_drops_invalid_proposals_but_keeps_valid_ones():
    client = make_client(
        "Mixed reflection.",
        [
            {"field": "buffer_slots", "direction": "increase", "magnitude": "small", "reason": "valid"},
            {"field": "sleep_min_penalty", "direction": "decrease", "magnitude": "large", "reason": "locked field"},
        ],
    )
    result = propose_preference_changes("some reflection", client=client)
    assert len(result.proposals) == 1
    assert result.proposals[0].field == "buffer_slots"


def test_propose_preference_changes_handles_zero_proposals():
    client = make_client("Nothing notable to change.", [])
    result = propose_preference_changes("today was fine", client=client)
    assert result.proposals == []
    assert result.summary == "Nothing notable to change."

def test_system_prompt_names_exact_required_keys():
    """Regression guard: gpt-oss-120b has repeatedly used wrong key names ("change"/
    "size" instead of "direction"/"magnitude", and dropped "reason") when calling the
    tool. The prompt must explicitly spell out the four required keys and warn against
    the specific wrong names it's been using.
    """
    from scheduler.reflection import build_system_prompt
    prompt = build_system_prompt()
    for required_key in ('"direction"', '"magnitude"', '"reason"'):
        assert required_key in prompt
    for wrong_key in ('"change"', '"size"', '"amount"'):
        assert wrong_key in prompt  # named explicitly as WRONG, but must be mentioned

GOOD = {"field": "buffer_slots", "direction": "increase", "magnitude": "small", "reason": "felt rushed"}


@pytest.mark.parametrize("sent", [None, "none", {}, ""])
def test_no_proposals_in_any_none_shape_is_an_empty_result(sent):
    result = propose_preference_changes("fine week", client=FakeClient({"summary": "ok", "proposals": sent}))
    assert result.proposals == [] and result.summary == "ok"


def test_entries_that_are_not_objects_are_skipped_and_good_ones_kept():
    result = propose_preference_changes("x", client=make_client("ok", ["more buffer please", 3, GOOD]))
    assert [p.field for p in result.proposals] == ["buffer_slots"]


def test_one_proposal_on_its_own_or_as_json_text_is_read():
    assert len(propose_preference_changes("x", client=make_client("ok", GOOD)).proposals) == 1
    assert len(propose_preference_changes("x", client=make_client("ok", json.dumps([GOOD]))).proposals) == 1


def test_unreadable_proposals_shape_is_a_clear_backend_failure():
    from scheduler.llm_backends import BadModelOutput, is_backend_failure
    with pytest.raises(BadModelOutput) as e:
        propose_preference_changes("x", client=FakeClient({"summary": "ok", "proposals": 5}))
    assert is_backend_failure(e.value)


def test_non_text_summary_becomes_empty():
    assert propose_preference_changes("x", client=FakeClient({"summary": None, "proposals": []})).summary == ""
