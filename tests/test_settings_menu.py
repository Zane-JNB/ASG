import pytest
from scheduler.db import connect, get_or_create_student, load_settings, load_tiers
from scheduler.preference_policy import POLICY, Tier
from scheduler.settings_menu import FIELD_UI, USER_FIELDS, run_settings_menu

@pytest.fixture
def conn(): return connect(":memory:")

@pytest.fixture
def sid(conn): return get_or_create_student(conn, "Zane")

def run(conn, sid, answers):
    it, shown = iter(answers), []
    run_settings_menu(conn, sid, ask=lambda p: (shown.append(p), next(it))[1], show=shown.append)
    return shown

def n(field): return str(USER_FIELDS.index(field) + 1)

def test_every_user_editable_field_has_menu_ui_and_internals_are_hidden():
    assert {k for k, p in POLICY.items() if p.user_editable} == set(FIELD_UI) == set(USER_FIELDS)
    assert not {"bedtime_penalty", "same_day_penalty", "sleep_target_penalty",
                "drop_deadline_multiplier"} & set(USER_FIELDS)

def test_list_shows_labels_values_tiers_and_hides_internals_until_asked(conn, sid):
    shown = run(conn, sid, ["i", "q"])
    first = [s for s in shown[:len(USER_FIELDS)]]
    assert any("Break time: 15 min  [learned automatically]" in s for s in first)
    assert not any("Bedtime drift weight" in s for s in first)
    assert any("Bedtime drift weight" in s for s in shown)

def test_claim_edit_and_return_keep_the_value(conn, sid):
    f = n("buffer_slots")
    shown = run(conn, sid, ["m", f, "e", f, "45", "a", f, "q"])
    assert any("kept at 15 min" in s for s in shown)
    assert any("Saved: Break time is now 45 min." in s for s in shown)
    assert any("automatic again, starting from 45 min" in s for s in shown)
    assert load_settings(conn, sid).buffer_slots == 3
    assert load_tiers(conn, sid)["buffer_slots"] == Tier.MODEL_LEARNED

def test_edit_of_model_owned_field_is_refused_before_asking_for_a_value(conn, sid):
    shown = run(conn, sid, ["e", n("buffer_slots"), "q"])
    assert any("Take ownership" in s for s in shown)
    assert not any("New value" in s for s in shown)
    assert load_settings(conn, sid).buffer_slots == 1

def test_non_claimable_field_cannot_be_switched(conn, sid):
    shown = run(conn, sid, ["a", n("default_sleep_min_slots"), "q"])
    assert any("can't be switched" in s for s in shown)
    assert load_tiers(conn, sid)["default_sleep_min_slots"] == Tier.USER

def test_invalid_input_reasks_and_enter_cancels(conn, sid):
    f = n("default_max_session_slots")
    shown = run(conn, sid, ["e", f, "abc", "", "q"])
    assert any("Invalid" in s for s in shown)
    assert load_settings(conn, sid).default_max_session_slots == 8

def test_valid_hours_and_none_rating_are_saved(conn, sid):
    run(conn, sid, ["e", n("default_max_session_slots"), "1.5",
                    "e", n("reminder_min_priority"), "3", "e", n("reminder_min_priority"), "none", "q"])
    s = load_settings(conn, sid)
    assert s.default_max_session_slots == 6 and s.reminder_min_priority is None

def test_validation_error_from_the_domain_layer_is_shown(conn, sid):
    f = n("default_preferred_bed")                      # claim, then try a bedtime past latest_bed
    shown = run(conn, sid, ["m", f, "e", f, "10:00", "q"])
    assert any("Invalid value" in s for s in shown)
    assert load_settings(conn, sid).default_preferred_bed == 92

def test_enter_at_the_settings_menu_gets_the_same_hint_as_every_menu(conn, sid):
    shown = run(conn, sid, ["", "q"])
    assert "Choose l, e, m, a, i, o or q." in shown
