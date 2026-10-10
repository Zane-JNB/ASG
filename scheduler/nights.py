"""Sleep for a plan window: one rule per night, each ending by the next morning's first class or
commute minus the wake buffer, and this morning's sleep and getting-ready time as fixed blocks."""
from datetime import datetime
from scheduler.models import FixedBlock, ProfileSettings, SleepRule
from scheduler.units import MINUTES_PER_SLOT, SLOTS_PER_DAY, next_slot, slot_to_time


def wake_before(day_blocks: list[FixedBlock], settings: ProfileSettings) -> tuple[int, str] | None:
    """(latest wake-up within that day, why) from the day's first class or commute minus the
    wake buffer, or None if nothing is on that day. The one wake-up rule, for every morning."""
    if not day_blocks:
        return None
    first = min(day_blocks, key=lambda b: b.start_slot)
    reason = f"'{first.title}' at {slot_to_time(first.start_slot)} the next day"
    if settings.wake_buffer_slots:
        reason += f" (minus your {settings.wake_buffer_slots * MINUTES_PER_SLOT} min wake-up buffer)"
    return first.start_slot - settings.wake_buffer_slots, reason


def with_wake_limit(rule: SleepRule, next_day: list[FixedBlock], settings: ProfileSettings) -> SleepRule:
    """Cap one night's sleep at the next day's wake-up (never before its earliest bedtime), and
    say why. Unchanged if nothing is on that day."""
    limit = wake_before(next_day, settings)
    if limit is None:
        return rule
    wake, reason = limit
    return rule.model_copy(update={"latest_wake": max(wake + SLOTS_PER_DAY, rule.earliest_bed),
                                   "latest_wake_reason": reason})


def sleep_setup(fixed: list[FixedBlock], morning_after: list[FixedBlock], num_days: int,
                settings: ProfileSettings, now: datetime) -> tuple[list[FixedBlock], list[SleepRule]]:
    """(fixed blocks with this morning's sleep added, one sleep rule per night). The one place
    sleep is set up for a plan:
      - this morning: if it is still sleep time, "Sleep (night before)" runs from 00:00 to the
        wake-up (the usual one, or the first class/commute minus the wake buffer), and "Getting
        ready" keeps the rest of the wake buffer free (also when the plan starts inside it);
      - tonight: bedtime is never before now;
      - every night: ends by the next day's first class/commute minus the wake buffer, and says
        so. The last night's next day (the morning after the window) is only looked at.
    The solver keeps the wake buffer after every night's sleep."""
    start_day, start_slot = divmod(next_slot(now), SLOTS_PER_DAY)
    sleep_rules = [settings.default_sleep_rule(night=n) for n in range(num_days)]
    rule0 = sleep_rules[0]
    today = [b for b in fixed if b.day == 0]
    wake = rule0.preferred_bed + rule0.length_slots - SLOTS_PER_DAY
    limit = wake_before(today, settings)
    wake = max(0, min(wake, limit[0]) if limit else wake)
    if start_day == 0:
        added = []
        if wake > start_slot:  # still morning-sleep hours: keep them free
            added.append(FixedBlock(title="Sleep (night before)", day=0, start_slot=0, end_slot=wake))
        # the normal buffer after a block makes up the rest of the wake buffer; never past the
        # day's first block (a class just after midnight leaves no room for it)
        ready_end = min([wake + settings.wake_buffer_slots - settings.buffer_slots] + [b.start_slot for b in today])
        ready_start = max(wake, start_slot)
        if ready_start < ready_end <= SLOTS_PER_DAY:
            added.append(FixedBlock(title="Getting ready", day=0, start_slot=ready_start, end_slot=ready_end))
        fixed = fixed + added
    # tonight's bedtime cannot be earlier than now
    bed = max(rule0.earliest_bed, next_slot(now))
    sleep_rules[0] = rule0.model_copy(update={"earliest_bed": bed,
                                              "preferred_bed": max(rule0.preferred_bed, bed),
                                              "latest_bed": max(rule0.latest_bed, bed)})
    blocks = fixed + morning_after
    return fixed, [with_wake_limit(rule, [b for b in blocks if b.day == rule.night + 1], settings)
                   for rule in sleep_rules]
