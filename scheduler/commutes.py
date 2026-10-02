from datetime import date, timedelta
from scheduler.calendar_utils import weekday_name
from scheduler.models import (
    Commute, FixedBlock, MINUTES_PER_SLOT, PlanAnchor, ScheduleWarning, SLOTS_PER_DAY, slot_to_time,)

def commute_to_block(commute: Commute, day_index: int) -> FixedBlock:
    h, m = commute.start_time.split(":")
    start_min = int(h) * 60 + int(m)
    start_slot = start_min // MINUTES_PER_SLOT
    end_slot = -(-(start_min + commute.length_minutes) // MINUTES_PER_SLOT)  # ceiling division
    return FixedBlock(title=commute.title, day=day_index, start_slot=start_slot,
                      end_slot=end_slot, buffer_before=False)

def expand_commute(commute: Commute, anchor: PlanAnchor) -> list[FixedBlock]:
    blocks = []
    for day_index in range(anchor.num_days):
        current = anchor.start_date + timedelta(days=day_index)
        if commute.recurring:
            if weekday_name(current) != commute.weekday:
                continue
            if commute.end_date and current > date.fromisoformat(commute.end_date):
                continue
            if current.isoformat() in commute.skip_dates:
                continue
        elif current.isoformat() != commute.date:
            continue
        blocks.append(commute_to_block(commute, day_index))
    return blocks

def expand_commutes(commutes: list[Commute], anchor: PlanAnchor) -> list[FixedBlock]:
    blocks = []
    for commute in commutes:
        blocks.extend(expand_commute(commute, anchor))
    return blocks

def _span(b: FixedBlock) -> tuple[int, int]:
    return b.day * SLOTS_PER_DAY + b.start_slot, b.day * SLOTS_PER_DAY + b.end_slot

def commute_overlaps(others: list[FixedBlock], commute_blocks: list[FixedBlock]): 
    pairs = []
    for i, c in enumerate(commute_blocks):
        cs, ce = _span(c)
        for o in others + commute_blocks[i + 1:]:  # each commute-vs-commute pair once
            os_, oe = _span(o)
            if cs < oe and os_ < ce:
                pairs.append((c, o))
    return pairs

def _clock(b: FixedBlock) -> str:  # NEW
    return f"{slot_to_time(b.start_slot)}-{slot_to_time(b.end_slot % SLOTS_PER_DAY)}"

def overlap_warnings(pairs, anchor: PlanAnchor) -> list[ScheduleWarning]: 
    return [
        ScheduleWarning(
            severity="soft", kind="commute_overlap",
            message=(f"{anchor.start_date + timedelta(days=c.day):%a %d %b}: {c.title} {_clock(c)} "
                     f"overlaps {o.title} {_clock(o)} (both kept; tasks avoid both)"),
        )
        for c, o in pairs
    ]