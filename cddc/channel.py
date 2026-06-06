"""The Channel seam - the ONLY way the control plane talks to the outside.

`bot.py` implements this against a Discord thread; `ConsoleChannel` (here)
implements it against stdout + a scripted queue. Because the whole control
plane only ever touches this protocol, `simulate.py` exercises everything with
zero Discord token.

Nothing here imports `discord`.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Callable, Protocol, runtime_checkable


def _chunk(s: str, n: int = 1990) -> list[str]:
    return [s[i : i + n] for i in range(0, len(s), n)] or [""]


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


class WebhookChannel:
    """Channel that narrates to a Discord thread via a WEBHOOK url (no bot token,
    no gateway) and takes steering from a LOCAL source, not Discord `!steer`.

    This is the shared on-site + fleet reporting path (handoff phase 6). Every
    agent - your remote fleet OR a teammate's local on-site agent - narrates
    through its own webhook identity (own display name) into the challenge
    thread, so the whole team sees status in Discord. They differ only in how
    they're STEERED: remote operators use the bot's `!steer`; an on-site
    operator steers their own agent locally via `push_steer()` (typed into their
    own console). The Worker code is identical either way - it just folds
    whatever `drain_steer()` returns. Same worker, same tools, same skills; only
    the seat changes.

    Discord-agnostic: posts are plain HTTPS POSTs, so this module STILL never
    imports discord. `sender` is injectable so simulate.py exercises it with no
    network and no real webhook.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        thread_id: int | None = None,
        username: str | None = None,
        sender: Callable[[str], None] | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.thread_id = thread_id
        self.username = username
        self._sender = sender or self._http_post
        self._pending: list[str] = []  # local (on-site) steers
        self._answers: list[str] = []  # local answers to ask()
        self.posts: list[str] = []     # narration history, for assertions

    # --- worker -> humans (out via the webhook) ---------------------------
    async def post(self, content: str) -> None:
        self.posts.append(content)
        for piece in _chunk(content):
            await asyncio.to_thread(self._sender, piece)

    # --- humans -> worker (in from the LOCAL operator) --------------------
    def push_steer(self, text: str) -> None:
        """On-site operator steering (local console/CLI) -> folded by the worker.
        The local-seat analogue of the bot's `!steer`."""
        self._pending.append(text)

    def drain_steer(self) -> list[str]:
        out, self._pending = self._pending, []
        return out

    def push_answer(self, text: str) -> None:
        """Local answer to a worker `ask()` (on-site, instead of a Discord reply)."""
        self._answers.append(text)

    async def ask(self, prompt: str, timeout: float = 60.0, default: str = "") -> str:
        await self.post(f"[ask] {prompt}")
        return self._answers.pop(0) if self._answers else default

    # --- the actual webhook POST (stdlib only; never imports discord) -----
    def _http_post(self, content: str) -> None:
        payload: dict = {"content": content}
        if self.username:
            payload["username"] = self.username
        url = self.webhook_url
        if self.thread_id is not None:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}thread_id={self.thread_id}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "cddc-agent"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15).close()  # noqa: S310
