from datetime import datetime

from scheduler.models import FixedBlock, ProfileSettings
from scheduler.nights import sleep_setup, wake_before, with_wake_limit


def test_wake_before_is_the_first_block_minus_the_wake_buffer():
    blocks = [FixedBlock(title="Lab", day=1, start_slot=40, end_slot=44),
              FixedBlock(title="Bus", day=1, start_slot=30, end_slot=34, buffer_before=False)]
    wake, reason = wake_before(blocks, ProfileSettings(wake_buffer_slots=4))
    assert wake == 26 and reason.startswith("'Bus' at 07:30 the next day")


def test_with_wake_limit_leaves_a_free_morning_alone():
    s = ProfileSettings()
    rule = s.default_sleep_rule(night=0)
    assert with_wake_limit(rule, [], s) == rule


def test_sleep_setup_gives_one_rule_per_night_and_keeps_this_morning_free():
    s = ProfileSettings()
    fixed, rules = sleep_setup([], [], 3, s, datetime(2026, 10, 5, 6, 0))
    assert [r.night for r in rules] == [0, 1, 2]
    assert [b.title for b in fixed] == ["Sleep (night before)", "Getting ready"]
