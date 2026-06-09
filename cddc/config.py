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
# created under this). Gitignored. Override with CDDC_FILES_DIR. expanduser so a
# `~/...` value resolves to your home (Python does NOT expand ~ like the shell -
# without this it makes a literal `~` directory) - and a WSL-native home path
# bind-mounts into docker far more cleanly than a /mnt/c path.
DOWNLOAD_DIR = os.path.expanduser(os.environ.get("CDDC_FILES_DIR", "_files"))

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


# --- real agent (pluggable provider) ---------------------------------------
# Worker kind: "dummy" (scripted, no key/cost) | "agent" (real LLM loop).
WORKER_KIND = _envs("CDDC_WORKER", "dummy").lower()
# Provider for the real loop: "deepseek" | "claude" | "codex". bot.py builds the
# matching client and falls back to dummy workers if its key is missing.
AGENT_PROVIDER = _envs("CDDC_PROVIDER", "deepseek").lower()
# The ESCALATION tier: what !escalate respawns (the two-tier bridge). For now
# "agent" = a LIGHT specialist (same model as triage but the specialist doctrine,
# the ctf skills library, and a deeper budget). Flip to "harness" later for the
# heavy Claude Code CLI specialist in the box (once the docker layers land - that
# tier reaches the pwn/rev/etc. tools). Triage always stays CDDC_WORKER.
SPECIALIST_KIND = _envs("CDDC_SPECIALIST_KIND", "agent").lower()
# The DEEP tier: what `!escalate deep` respawns - the heaviest brain, on the
# deep_solver lane/doctrine. Defaults to "harness" (the Claude Code box agent) so
# the ladder is real: flash triage -> DeepSeek-pro specialist -> Claude deep. Needs
# the harness prereqs (claude/codex login, tmux). Set CDDC_DEEP_KIND=agent on a host
# without the harness to keep `deep` as a DeepSeek-pro on the deep_solver lane.
DEEP_KIND = _envs("CDDC_DEEP_KIND", "harness").lower()

# DeepSeek (OpenAI-compatible).
DEEPSEEK_API_KEY = _envs("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _envs("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-v4-flash: $0.14/$0.28 per M (cache hits 98% off). The old deepseek-chat
# / deepseek-reasoner aliases DEPRECATE 2026-07-24, so use the v4 IDs directly.
# CHURN_THINKING toggles V4 reasoning mode (SAME per-token rate, just more tokens)
# - ON by default since the DeepSeek tier IS triage, which writes the difficulty
# report and shouldn't rabbit-hole. Flip off for a dumb high-volume churn tier.
CHURN_MODEL = _envs("CDDC_CHURN_MODEL", "deepseek-v4-flash")
# The ESCALATION tier's model (specialist / deep / race) - a stronger brain than
# triage's flash. Defaults to v4-pro (~3x flash's price, still cents/solve); set to
# deepseek-v4-flash if pro isn't enabled on your account. Only used when the
# escalation kind is "agent" (deepseek); the "harness" kind ignores it.
SPECIALIST_MODEL = _envs("CDDC_SPECIALIST_MODEL", "deepseek-v4-pro")

# --- Claude Code headless harness (worker kind "cc") ------------------------
# Drive ONE `claude` CLI per tier, swapping only the model BACKEND. DeepSeek ships
# an official Anthropic-compatible endpoint, so the same CLI runs on DeepSeek for the
# cheap tiers and on the Anthropic SUBSCRIPTION (Opus 4.8) for the deep tier.
# Set CDDC_WORKER / CDDC_SPECIALIST_KIND / CDDC_DEEP_KIND = "cc" to use this path.
CC_DEEPSEEK_ANTHROPIC_URL = _envs("CDDC_CC_DEEPSEEK_URL", "https://api.deepseek.com/anthropic")
CC_FLASH_MODEL = _envs("CDDC_CC_FLASH_MODEL", "deepseek-v4-flash")  # triage tier
CC_PRO_MODEL = _envs("CDDC_CC_PRO_MODEL", "deepseek-v4-pro")        # specialist tier
# Backstop wall-clock cap (seconds) on a single TRIAGE turn: a flash turn that ignores
# "assess, don't grind" still stops and halts for the operator. 0 = no cap. Solver
# tiers (specialist/deep) run uncapped - the operator controls them via !steer/!kill.
CC_TRIAGE_TURN_SECS = int(_envs("CDDC_CC_TRIAGE_TURN_SECS", "300"))


def cc_tier_for(role: str, lane_name: str) -> str:
    """Map a (role, lane) to a CC tier: triage | specialist | deep."""
    if lane_name == "deep_solver":
        return "deep"
    return "specialist" if (role or "").strip().lower() == "specialist" else "triage"


def cc_profile(tier: str) -> tuple[dict, dict]:
    """(env_profile, secret_env) for a CC tier's `claude` spawn.

    env_profile -> non-secret `-e K=V` flags (base url, model ids, effort); secret_env
    -> `-e K` flags whose VALUE rides in the spawn's own env, never on the argv.

    SAFETY INVARIANT (do not break): we NEVER set ANTHROPIC_API_KEY anywhere. The deep
    tier returns EMPTY env, so the CLI falls through to the mounted ~/.claude
    SUBSCRIPTION login -> Opus 4.8, with NO metered Anthropic-API billing. The cheap
    tiers authenticate to DeepSeek (ANTHROPIC_AUTH_TOKEN = the DeepSeek key) against
    DeepSeek's endpoint - DeepSeek billing only.
    """
    if tier == "deep":
        return {}, {}  # no anthropic env -> mounted subscription -> Opus 4.8 (no API burn)
    model = CC_PRO_MODEL if tier == "specialist" else CC_FLASH_MODEL
    env = {
        "ANTHROPIC_BASE_URL": CC_DEEPSEEK_ANTHROPIC_URL,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": CC_PRO_MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": CC_PRO_MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": CC_FLASH_MODEL,
        "CLAUDE_CODE_SUBAGENT_MODEL": CC_FLASH_MODEL,
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
    }
    secret = {"ANTHROPIC_AUTH_TOKEN": DEEPSEEK_API_KEY} if DEEPSEEK_API_KEY else {}
    return env, secret
CHURN_THINKING = _envs("CDDC_CHURN_THINKING", "1").lower() not in ("0", "false", "no", "")

# Claude (Anthropic Messages API).
ANTHROPIC_API_KEY = _envs("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = _envs("ANTHROPIC_BASE_URL", "")  # empty -> SDK default
CLAUDE_MODEL = _envs("CDDC_CLAUDE_MODEL", "claude-opus-4-8")
CLAUDE_MAX_TOKENS = int(_envs("CDDC_CLAUDE_MAX_TOKENS", "8000"))

# Codex (OpenAI, OpenAI-compatible).
OPENAI_API_KEY = _envs("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _envs("OPENAI_BASE_URL", "https://api.openai.com/v1")
CODEX_MODEL = _envs("CDDC_CODEX_MODEL", "gpt-5-codex")

# --- harness worker (CDDC_WORKER=harness) ----------------------------------
# Runs the FULL CLI coding agent (claude code / codex) inside the sandbox
# container, driven from the host via libtmux (see harness.py / harness_worker.py)
# instead of our own model tool-loop. The bot's env must carry the matching key
# (ANTHROPIC_API_KEY for claude, OPENAI_API_KEY for codex) - it is passed THROUGH
# to the container, never baked into the image. The HOST needs `tmux` installed.
HARNESS_CLI = _envs("CDDC_HARNESS_CLI", "claude").lower()  # "claude" | "codex"
# Per-CLI launch command run in the container (operators add flags per version).
# claude/codex run unattended, so they need their auto-approve flags - it is the
# sandbox container that provides isolation, not the CLI's own permission prompt.
CLAUDE_CLI_CMD = _envs("CDDC_CLAUDE_CLI_CMD", "claude --dangerously-skip-permissions")
CODEX_CLI_CMD = _envs("CDDC_CODEX_CLI_CMD", "codex --full-auto")
# Comma-separated tmux key names sent (after the gate renders) to clear each
# CLI's startup prompt. claude's trust-folder prompt DEFAULTS to "Yes, I trust
# this folder", so a bare "Enter" accepts it - any arrow moves OFF the correct
# option (Up even wraps 1->2). codex --full-auto usually needs nothing.
CLAUDE_STARTUP_KEYS = _envs("CDDC_CLAUDE_STARTUP_KEYS", "Enter")
CODEX_STARTUP_KEYS = _envs("CDDC_CODEX_STARTUP_KEYS", "")
# Run the CLI as this container user ("" = root). Set to a non-root user (e.g.
# "ctf") if your CLI refuses to run as root (claude --dangerously-skip-permissions
# does). The Dockerfile creates a `ctf` user for this.
HARNESS_USER = _envs("CDDC_HARNESS_USER", "")
HARNESS_POLL = float(_envs("CDDC_HARNESS_POLL", "5"))          # screen-poll seconds
HARNESS_MAX_MINUTES = float(_envs("CDDC_HARNESS_MAX_MINUTES", "20"))  # wall-clock cap
# Compose the CLI's noisy TUI output into clean 1-line Discord updates via the
# cheap churn model (the cheap model narrating the deep agent), every N seconds.
# Off / no DeepSeek key -> post the cleaned screen deltas raw.
HARNESS_SUMMARIZE = _envs("CDDC_HARNESS_SUMMARIZE", "1").lower() not in ("0", "false", "no", "")
HARNESS_SUMMARIZE_SECS = float(_envs("CDDC_HARNESS_SUMMARIZE_SECS", "20"))
# Share the host's claude/codex login (~/.claude, ~/.claude.json, ~/.codex) into
# the container so the CLIs don't get stuck on login. On by default.
HARNESS_SHARE_CREDS = _envs("CDDC_HARNESS_SHARE_CREDS", "1").lower() not in ("0", "false", "no", "")
# Candidate flags announce but DON'T halt the agent by default (the CLI keeps
# working after printing a guess). Flip on for the classic validation halt.
HARNESS_HALT_ON_FLAG = _envs("CDDC_HARNESS_HALT_ON_FLAG", "0").lower() in ("1", "true", "yes")
# Maintained blacklist of fake flags to ignore (comma-separated), on top of the
# built-in placeholders and the prompt's own example tokens.
FLAG_BLACKLIST = [f.strip() for f in _envs("CDDC_FLAG_BLACKLIST", "").split(",") if f.strip()]
# Hard caps so a runaway loop can't burn money.
AGENT_MAX_STEPS = int(_envs("CDDC_AGENT_MAX_STEPS", "40"))
AGENT_MAX_TOKENS = int(_envs("CDDC_AGENT_MAX_TOKENS", "200000"))
SHELL_TIMEOUT = int(_envs("CDDC_SHELL_TIMEOUT", "30"))
# Agent posts a consolidated checkpoint rollup every N steps (0 disables).
AGENT_CHECKPOINT = int(_envs("CDDC_AGENT_CHECKPOINT", "8"))
# When triage escalates, the respawned specialist gets this multiple of the
# base step budget (a specialist is meant to grind, not triage).
ESCALATION_BUDGET_MULT = float(_envs("CDDC_ESCALATION_BUDGET_MULT", "3"))
# Test/observe knob: when set, `!escalate` refuses to respawn a specialist - the
# triage agent stays put (use `!deny` to keep it grinding). For testing triage in
# isolation before the specialist/deep tiers (and their docker layers) are ready.
DISABLE_HANDOFF = _envs("CDDC_DISABLE_HANDOFF", "0").lower() in ("1", "true", "yes")

# Where run_shell executes: "local" (host, no isolation) | "docker" (per-challenge
# ctf-sandbox container, workdir bind-mounted). Crypto/web/research keep working
# host-side without Docker on "local"; flip to "docker" to safely run untrusted
# pwn/rev binaries. Requires the bot to run as a docker-capable user.
CDDC_SANDBOX = _envs("CDDC_SANDBOX", "local").lower()
CDDC_SANDBOX_IMAGE = _envs("CDDC_SANDBOX_IMAGE", "ctf-sandbox")
# The ai lane is torch-heavy, so it uses a SEPARATE image (built `--target ai`),
# keeping the lean default image free of the torch tax on every rebuild. Other
# lanes use CDDC_SANDBOX_IMAGE.
CDDC_SANDBOX_IMAGE_AI = _envs("CDDC_SANDBOX_IMAGE_AI", "ctf-sandbox:ai")


def sandbox_image_for_lane(lane_name: str) -> str:
    return CDDC_SANDBOX_IMAGE_AI if lane_name == "ai" else CDDC_SANDBOX_IMAGE
# SELinux bind-mount relabel flag ("z" shared / "Z" private). EMPTY by default -
# it's a no-op on WSL/Docker Desktop and only needed on SELinux-enforcing hosts
# (Fedora/RHEL), where you'd set CDDC_SANDBOX_MOUNT_FLAG=Z so the container can
# read the mount.
CDDC_SANDBOX_MOUNT_FLAG = _envs("CDDC_SANDBOX_MOUNT_FLAG", "").strip().lstrip(":")

# Docker-OUT-of-docker: host daemon socket bound into the challenge box so the agent
# inside can stand up service containers (a `docker compose up` challenge, a target
# box). It is host-root. NOTE: the bot now runs ONE persistent box per challenge that
# every tier SHARES and takes over on handoff (task #12), and you cannot add a socket
# to a running container - so the dispatcher gives that box the socket UNIFORMLY
# whenever CDDC_DOCKER_SOCK is set (see Dispatcher._build_box), not role-gated.
# docker_sock_for_role() below keeps the older role-gating ONLY for the standalone
# tryout helper (a throwaway single-worker box that never hands off).
# Set CDDC_DOCKER_SOCK="" to disable the socket everywhere; CDDC_TRIAGE_SOCKET=1 arms
# triage in the tryout helper (the "exception" knob, off by default).
DOCKER_SOCK = _envs("CDDC_DOCKER_SOCK", "/var/run/docker.sock")
TRIAGE_SOCKET = _envs("CDDC_TRIAGE_SOCKET", "0").lower() in ("1", "true", "yes")
# Pass the host GPU into the sandbox (docker --gpus all) so the ai lane's CUDA
# torch can use it. Needs the NVIDIA driver + nvidia-container-toolkit on the host;
# off by default (most hosts have no GPU, and the flag errors without the toolkit).
CDDC_SANDBOX_GPU = _envs("CDDC_SANDBOX_GPU", "0").lower() in ("1", "true", "yes")
# Shared decompiler service (pyghidra-mcp). Agents reach it over a docker network:
# set CDDC_SANDBOX_NETWORK to the network the `cddc-decompiler` container is on, and
# the sandbox joins it + gets CDDC_DECOMPILER_URL so the `dc` client can find it.
# Empty network -> no decompiler wiring (dc will just fail to connect).
CDDC_SANDBOX_NETWORK = _envs("CDDC_SANDBOX_NETWORK", "")
CDDC_DECOMPILER_URL = _envs("CDDC_DECOMPILER_URL", "http://cddc-decompiler:8000/mcp")


def docker_sock_for_role(role: str) -> str:
    """Socket path this role's sandbox should bind, or "" for none.

    Role-gated (triage withheld unless CDDC_TRIAGE_SOCKET). Used only by the
    standalone `tryout` helper now - the bot's shared per-challenge box gets the
    socket uniformly (see the DOCKER_SOCK note above / Dispatcher._build_box)."""
    if not DOCKER_SOCK:
        return ""
    if (role or "").strip().lower() == "triage" and not TRIAGE_SOCKET:
        return ""
    return DOCKER_SOCK

# --- web_search / read_url tools (provider-agnostic) -----------------------
# Default DuckDuckGo (no key, works for every teammate out of the box); set to
# "serper" + a key for Google-quality at fleet scale, or "none" to disable the
# tool. read_url extraction uses Jina Reader (r.jina.ai) - no key required, a key
# only lifts rate limits. Mirrors the AGENT_PROVIDER seam; see cddc/search.py.
WEB_SEARCH_PROVIDER = _envs("CDDC_WEB_SEARCH", "ddg").lower()
SERPER_API_KEY = _envs("SERPER_API_KEY", "")
JINA_API_KEY = _envs("JINA_API_KEY", "")
WEB_SEARCH_RESULTS = int(_envs("CDDC_WEB_SEARCH_RESULTS", "5"))


def category_for_channel(channel_name: str) -> str:
    key = (channel_name or "").strip().lower().lstrip("#")
    return CHANNEL_CATEGORY.get(key, key)


def lane_for_category(category: str) -> str:
    return CATEGORY_LANE.get((category or "").strip().lower(), DEFAULT_LANE)


# --- env (read lazily; only bot.py needs the token) ----------------------
def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
