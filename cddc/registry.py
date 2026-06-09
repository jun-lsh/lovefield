"""Registry - active workers per thread. MANY agents per challenge, kept dumb.

A thread maps to a *list* of workers from day one (usually 1, realistically 1-3:
one on-site + a fleet churner, a couple more for hard ones). No scheduling /
load-balancing / fairness - just a short list. Modelling it as a list now is the
seam so phase 5 racers and phase 6 on-site agents join the same challenge with
no retrofit.

`!status` lists the thread's workers; `!steer`/`!pause`/`!kill` broadcast to all
(or target one by name). Nothing here imports `discord`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .worker import Worker

if TYPE_CHECKING:
    from .sandbox import Sandbox


class Registry:
    def __init__(self) -> None:
        self.active: dict[int, list[Worker]] = {}
        # The ONE shared sandbox box per challenge (task #12). It outlives the
        # individual workers: created on first use, reused by every worker on the
        # thread (escalation / race / lane reroute), released only at challenge end.
        self.boxes: dict[int, "Sandbox"] = {}

    def get_box(self, thread_id: int, factory: Callable[[], "Sandbox"]) -> "Sandbox":
        """Return the challenge's shared box, creating it (via `factory`) on first
        use. Every worker on the thread gets the SAME object, so a handoff reuses
        the live container instead of recreating it. Synchronous and free of any
        await between the check and the store, so concurrent dispatch (a race
        fan-out) can't make two boxes. The container itself is launched lazily by
        the worker's start() (idempotent), not here."""
        box = self.boxes.get(thread_id)
        if box is None:
            box = factory()
            self.boxes[thread_id] = box
        return box

    async def release_box(self, thread_id: int) -> None:
        """Destroy and forget the challenge's box (challenge end: !kill / !solved).
        Best-effort; a no-op if there was never a box for the thread."""
        box = self.boxes.pop(thread_id, None)
        if box is not None:
            await box.release()

    def add(self, thread_id: int, worker: Worker) -> None:
        self.active.setdefault(thread_id, []).append(worker)

    def workers(self, thread_id: int) -> list[Worker]:
        return list(self.active.get(thread_id, []))

    def by_name(self, thread_id: int, name: str) -> Worker | None:
        for w in self.active.get(thread_id, []):
            if w.name == name:
                return w
        return None

    def remove(self, thread_id: int, worker: Worker) -> None:
        lst = self.active.get(thread_id)
        if lst and worker in lst:
            lst.remove(worker)
        if lst is not None and not lst:
            del self.active[thread_id]

    def broadcast(
        self,
        thread_id: int,
        fn: Callable[[Worker], None],
        *,
        target: str | None = None,
    ) -> list[Worker]:
        """Apply `fn` to every worker on the thread, or just `target` by name.

        Returns the workers acted on (so callers can report what happened).
        """
        hit: list[Worker] = []
        for w in self.active.get(thread_id, []):
            if target is None or w.name == target:
                fn(w)
                hit.append(w)
        return hit
