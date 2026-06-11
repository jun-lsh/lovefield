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

import shutil
from collections.abc import Callable
from typing import TYPE_CHECKING

from .worker import Worker

if TYPE_CHECKING:
    from .sandbox import Sandbox


class Registry:
    def __init__(self) -> None:
        self.active: dict[int, list[Worker]] = {}
        # Boxes keyed by SCOPE string. Normally "<thread_id>" - the one shared box per
        # challenge (task #12), reused by escalation / lane reroute. A race fan-out adds
        # ISOLATED per-racer boxes keyed "<thread_id>-r<i>" so the racers don't clobber
        # each other. All a thread's boxes are released together at challenge end.
        self.boxes: dict[str, "Sandbox"] = {}

    def get_box(self, scope, factory: Callable[[], "Sandbox"]) -> "Sandbox":
        """Return the box for `scope` (str: "<thread>" shared, or "<thread>-r<i>" for a
        racer), creating it (via `factory`) on first use. Workers sharing a scope get the
        SAME object, so a handoff reuses the live container. Synchronous and free of any
        await between check and store, so concurrent dispatch can't make two boxes. The
        container is launched lazily by the worker's start() (idempotent), not here."""
        key = str(scope)
        box = self.boxes.get(key)
        if box is None:
            box = factory()
            self.boxes[key] = box
        return box

    async def release_box(self, thread_id: int) -> None:
        """Destroy and forget ALL of the challenge's boxes (challenge end: !kill /
        !solved): the shared box AND any race instances. Reclaims racer workdir copies.
        Best-effort; a no-op if the thread never had a box."""
        prefix = f"{thread_id}-r"
        keys = [k for k in self.boxes if k == str(thread_id) or k.startswith(prefix)]
        for k in keys:
            box = self.boxes.pop(k, None)
            if box is None:
                continue
            await box.release()
            if "-r" in k:  # racer boxes own a throwaway workdir COPY - reclaim it
                wd = getattr(box, "host_workdir", "")
                if wd:
                    shutil.rmtree(wd, ignore_errors=True)

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
