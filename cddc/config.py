"""Routing maps + env knobs. Discord-agnostic (no `discord` import).

Two small lookup tables drive routing:
  CHANNEL_CATEGORY : Discord channel (parent) name -> category key
  CATEGORY_LANE    : category key -> lane name

Rename/remap channels here without touching dispatcher/worker code. Channel
names are matched case-insensitively; an unknown channel falls back to its own
lowercased name as the category, and an unknown category falls back to `raw`.
"""

from __future__ import annotations

import os

# Load cddc/.env BEFORE the module-level os.environ reads below - otherwise they
# see defaults (config is imported before bot.py could call load_dotenv).
# Guarded so the token-free sim still imports without python-dotenv installed.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

# The real server channels. general = teammate chat (bot ignores); status =
# global feed (flag/needs-human alerts + thread links). The rest are the
# challenge drop-categories. windows + deep_solver have NO channel - windows is
# reached via `!lane`, deep_solver via the hard-flag or self-escalation.
CHANNEL_CATEGORY: dict[str, str] = {
    "pwn": "pwn",
    "rev": "rev",
    "crypto": "crypto",
    "web": "web",
    "forensics": "forensics",
    "misc": "misc",
    "ai": "ai",
    "hardware": "hardware",
    "research": "research",
}

# Channels that are NOT challenge drop targets.
IGNORE_CHANNELS = {"general"}  # teammate chat - bot never dispatches here
STATUS_CHANNEL = "status"       # global feed for flag/needs-human alerts

# Category -> lane name (lanes live in cddc/lanes/__init__.py:LANES).
CATEGORY_LANE: dict[str, str] = {
    "pwn": "pwn",
    "rev": "rev",
    "crypto": "crypto",
    "web": "web",
    "forensics": "forensics",
    "misc": "misc",
    "ai": "ai",
    "hardware": "hw_research",
    "research": "research_run",
}

DEFAULT_LANE = "raw"

# Where !start saves downloaded distribution files (a per-challenge subdir is
# created under this). Gitignored. Override with CDDC_FILES_DIR.
DOWNLOAD_DIR = os.environ.get("CDDC_FILES_DIR", "_files")

# Seconds the dummy worker waits between scripted steps on the live bot. Slow
# enough to catch a run mid-flight and !steer / !status / !kill / !lane it.
# Override with CDDC_STEP_DELAY. (simulate.py builds workers with its own fast
# tick, so this only affects the live bot.)
STEP_DELAY = float(os.environ.get("CDDC_STEP_DELAY", "8"))

# Who gets pinged on a halt. "user" pings ALERT_USER_ID only (testing); flip to
# "everyone" for the tiered @everyone/@here behavior. `.split("#")` defends
# against an inline comment leaking into the value.
ALERT_MODE = os.environ.get("ALERT_MODE", "user").split("#")[0].strip().lower()
ALERT_USER_ID = os.environ.get("ALERT_USER_ID", "").split("#")[0].strip()


def _envs(name: str, default: str) -> str:
    return os.environ.get(name, default).split("#")[0].strip()


# --- real agent (DeepSeek, OpenAI-compatible) ------------------------------
# Worker kind: "dummy" (scripted, no key/cost) | "agent" (real DeepSeek loop).
WORKER_KIND = _envs("CDDC_WORKER", "dummy").lower()
DEEPSEEK_API_KEY = _envs("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _envs("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-chat = v4-flash non-thinking (cheapest churn). Override per taste.
CHURN_MODEL = _envs("CDDC_CHURN_MODEL", "deepseek-chat")
# Hard caps so a runaway loop can't burn money.
AGENT_MAX_STEPS = int(_envs("CDDC_AGENT_MAX_STEPS", "40"))
AGENT_MAX_TOKENS = int(_envs("CDDC_AGENT_MAX_TOKENS", "200000"))
SHELL_TIMEOUT = int(_envs("CDDC_SHELL_TIMEOUT", "30"))

def category_for_channel(channel_name: str) -> str:
    key = (channel_name or "").strip().lower().lstrip("#")
    return CHANNEL_CATEGORY.get(key, key)


def lane_for_category(category: str) -> str:
    return CATEGORY_LANE.get((category or "").strip().lower(), DEFAULT_LANE)


# --- env (read lazily; only bot.py needs the token) ----------------------
def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
