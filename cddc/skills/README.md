# Agent skills / alignment files

The per-agent **alignment layer**. An agent's system prompt is composed at
runtime (see `cddc/agent_worker.py:load_system`) by stacking four parts:

```
common.md            neutral facts for EVERY agent (tools, submit rules, narrate)
   +
env.md               current-environment facts (bare-metal win, no sage) - SWAPPABLE
   +
roles/<role>.md      the role doctrine: how hard / how long to push
   +
lanes/<lane>.md      this skill's playbook (optional)
```

The key split: **role doctrine and lane playbook are separate**, and the
"move fast, triage only" bias lives ONLY in `roles/triage.md`. A specialist
(`roles/specialist.md`) is never poisoned by it - it's told it has budget and
should grind methodically. `common.md` and `env.md` carry no speed/depth bias at
all.

## The pieces

- **`common.md`** - true for everyone, no bias. Tools, submitting, narration.
- **`env.md`** - facts about the box we run on *right now* (no Sage, no debugger,
  no web_search yet). A swappable snippet: when the tool container lands, edit
  THIS file, not the doctrine.
- **`roles/triage.md`** - the fast, cheap first-pass bot: recon, classify,
  cheap-win, then escalate. Anti-rabbit-hole hard rule lives here.
- **`roles/specialist.md`** - the deep solver: hypothesis-test loops, build real
  artifacts, checkpoint, don't bail early.
- **`lanes/<lane>.md`** - per-skill checklist (crypto, rev, pwn, web, forensics,
  ai, misc, ...). Lane names are the keys in `cddc/lanes/__init__.py:LANES`.

## Which role an agent gets

Today: a lane whose `default_mode` is `specialist` (e.g. `deep_solver`,
`windows`) loads `roles/specialist.md`; everything else loads `roles/triage.md`
(thin-triage-always). Escalation / `!lane` onto a specialist lane flips the role.
That mapping lives in `dispatcher.py`.

## How to edit (teammates)

- Change behaviour for every agent -> `common.md`.
- Change current-box facts (tools arrived, sage available) -> `env.md`.
- Change how a *role* pushes -> `roles/<role>.md`.
- Change a *skill's* approach -> `lanes/<lane>.md` (create it if missing -
  optional).

No Python changes needed. Files are plain markdown, read fresh on each `!start`.

## Conventions

- **Pure ASCII** (the Windows console chokes on unicode/emoji).
- Keep files SHORT and concrete - ordered checklists beat essays, and the model
  follows "do this, then this" better than prose.
- Don't repeat across files - `common.md` already carries the universal stuff;
  a lane file should only add what's specific to that skill.
- A missing `roles/<role>.md` or `lanes/<lane>.md` degrades gracefully (the
  loader just skips it).
