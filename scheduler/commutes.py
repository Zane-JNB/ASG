from datetime import timedelta

from scheduler.models import Commute, FixedBlock, PlanAnchor, ScheduleWarning
from scheduler.units import MINUTES_PER_SLOT, clock_range, time_to_minutes


def commute_to_block(commute: Commute, day_index: int) -> FixedBlock:
    """The commute as a fixed block, widened to whole slots. Tasks need no buffer before it."""
    start = time_to_minutes(commute.start_time)
    end_slot = -(-(start + commute.length_minutes) // MINUTES_PER_SLOT)  # ceiling division
    return FixedBlock(title=commute.title, day=day_index, start_slot=start // MINUTES_PER_SLOT,
                      end_slot=end_slot, buffer_before=False)


def expand_commutes(commutes: list[Commute], anchor: PlanAnchor) -> list[FixedBlock]:
    """Every commute on every day of the anchor window it runs on."""
    days = [anchor.start_date + timedelta(days=i) for i in range(anchor.num_days)]
    return [commute_to_block(c, i) for c in commutes for i, d in enumerate(days) if c.runs_on(d)]


def commute_overlaps(others: list[FixedBlock], commute_blocks: list[FixedBlock]) -> list[tuple[FixedBlock, FixedBlock]]:
    """(commute, other) pairs that share time; each commute-vs-commute pair is listed once."""
    return [(c, o) for i, c in enumerate(commute_blocks)
            for o in others + commute_blocks[i + 1:] if c.overlaps(o)]


def overlap_warnings(pairs: list[tuple[FixedBlock, FixedBlock]], anchor: PlanAnchor) -> list[ScheduleWarning]:
    return [
        ScheduleWarning.soft("commute_overlap", (
            f"{anchor.start_date + timedelta(days=c.day):%a %d %b}: "
            f"{c.title} {clock_range(c.start_slot, c.end_slot)} "
            f"overlaps {o.title} {clock_range(o.start_slot, o.end_slot)} (both kept; tasks avoid both)"))
        for c, o in pairs
    ]
