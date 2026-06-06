"""The Channel seam - the ONLY way the control plane talks to the outside.

`bot.py` implements this against a Discord thread; `ConsoleChannel` (here)
implements it against stdout + a scripted queue. Because the whole control
plane only ever touches this protocol, `simulate.py` exercises everything with
zero Discord token.

Nothing here imports `discord`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Channel(Protocol):
    """Three operations, deliberately tiny.

    - `post`  : worker -> humans (narration). async; may hit the network.
    - `drain_steer`: humans -> worker (steering). NON-blocking - returns the
      pending steers and clears them. Plain thread chatter never lands here;
      only explicit `!steer` text does.
    - `ask`   : worker -> humans -> worker (a blocking question with a timeout
      and a default, e.g. the stall "race?" prompt).
    """

    async def post(self, content: str) -> None: ...

    def drain_steer(self) -> list[str]: ...

    async def ask(self, prompt: str, timeout: float = 60.0, default: str = "") -> str: ...


class ConsoleChannel:
    """Channel impl for `simulate.py` - prints, captures, serves scripted input.

    The harness injects steers with `inject_steer()` (simulating an operator
    typing `!steer ...`) and the worker drains them. `ask` answers come from a
    scripted FIFO; when it's empty, `ask` returns the `default` (simulating a
    timeout with no operator reply). Everything posted is captured in `posts`
    so the harness can assert on narration.
    """

    def __init__(
        self,
        name: str = "console",
        *,
        ask_answers: list[str] | None = None,
        echo: bool = True,
    ) -> None:
        self.name = name
        self.echo = echo
        self._pending: list[str] = []
        self._ask_answers: list[str] = list(ask_answers or [])
        self.posts: list[str] = []  # full narration history, for assertions

    # --- worker -> humans -------------------------------------------------
    async def post(self, content: str) -> None:
        self.posts.append(content)
        if self.echo:
            print(f"[{self.name}] {content}")

    # --- humans -> worker -------------------------------------------------
    def inject_steer(self, msg: str) -> None:
        """Harness-side: queue a steer as if an operator typed `!steer msg`."""
        self._pending.append(msg)

    def drain_steer(self) -> list[str]:
        out, self._pending = self._pending, []
        return out

    # --- worker -> humans -> worker --------------------------------------
    async def ask(self, prompt: str, timeout: float = 60.0, default: str = "") -> str:
        await self.post(f"[ask] {prompt}")
        if self._ask_answers:
            ans = self._ask_answers.pop(0)
            await self.post(f"   -> (scripted reply) {ans!r}")
            return ans
        await self.post(f"   -> (timeout - default) {default!r}")
        return default
