from scheduler.calendar_utils import overlap_lines
from scheduler.models import Commute, FixedBlock, PlanAnchor, ScheduleWarning
from scheduler.units import MINUTES_PER_SLOT, time_to_minutes


def commute_to_block(commute: Commute, day_index: int) -> FixedBlock:
    """The commute as a fixed block, widened to whole slots. Tasks need no buffer before it."""
    start = time_to_minutes(commute.start_time)
    end_slot = -(-(start + commute.length_minutes) // MINUTES_PER_SLOT)  # ceiling division
    return FixedBlock(title=commute.title, day=day_index, start_slot=start // MINUTES_PER_SLOT,
                      end_slot=end_slot, buffer_before=False)


def expand_commutes(commutes: list[Commute], anchor: PlanAnchor) -> list[FixedBlock]:
    """Every commute on every day of the anchor window it runs on."""
    return [commute_to_block(c, i) for c in commutes for i, d in enumerate(anchor.dates) if c.runs_on(d)]


def commute_overlaps(others: list[FixedBlock], commute_blocks: list[FixedBlock]) -> list[tuple[FixedBlock, FixedBlock]]:
    """(commute, other) pairs that share time; each commute-vs-commute pair is listed once."""
    return [(c, o) for i, c in enumerate(commute_blocks)
            for o in others + commute_blocks[i + 1:] if c.overlaps(o)]


def overlap_warnings(pairs: list[tuple[FixedBlock, FixedBlock]], anchor: PlanAnchor) -> list[ScheduleWarning]:
    """Soft warnings for commutes that share time with something: both are kept."""
    return [ScheduleWarning.soft("commute_overlap", f"{line} (both kept; tasks avoid both)")
            for line in overlap_lines(anchor.start_date, pairs)]
