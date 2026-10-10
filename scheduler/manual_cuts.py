class CutState:   
    """durations: {task index: planned slots}. sizes_fn(index, remaining_slots) -> chunk sizes."""

    def __init__(self, durations: dict[int, int], sizes_fn):
        self.durations = dict(durations)
        self.sizes_fn = sizes_fn
        self.lost: dict[int, int] = {}          # total slots cut per task so far
        self._undo: list[dict[int, int]] = []   # snapshots, one per successful edit

    def remaining(self, i: int) -> int:
        return self.durations[i] - self.lost.get(i, 0)

    def _commit(self, i: int, slots: int) -> None:
        self._undo.append(dict(self.lost))      # snapshot BEFORE changing, so undo is exact
        self.lost[i] = self.lost.get(i, 0) + slots

    def _check(self, i: int) -> int:
        if i not in self.durations:
            raise ValueError("no such task")
        left = self.remaining(i)
        if left == 0:
            raise ValueError("that task is already fully cut")
        return left

    def drop(self, i: int) -> int:
        left = self._check(i)
        self._commit(i, left)
        return left

    def cut_chunks(self, i: int, k: int) -> int:
        self._check(i)
        sizes = self.sizes_fn(i, self.remaining(i))   # chunks of what is LEFT, not the original
        if not 1 <= k <= len(sizes):
            raise ValueError(f"cut between 1 and {len(sizes)} session(s)")
        lost = sum(sizes[-k:])
        self._commit(i, lost)
        return lost

    def reduce(self, i: int, slots: int) -> int:
        left = self._check(i)
        if not 1 <= slots < left:
            raise ValueError(f"reduce by 1 to {left - 1} slots (to remove it all, use drop)")
        self._commit(i, slots)
        return slots

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.lost = self._undo.pop()
        return True

    @property
    def touched(self) -> bool:
        return bool(self.lost)