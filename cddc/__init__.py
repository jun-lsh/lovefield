"""CDDC 2026 agent stack - phase 1: orchestration scaffolding + control plane.

The control plane (dispatcher / worker / lanes / channel / registry) is fully
Discord-agnostic and exercised by `simulate.py` with no token. `bot.py` is the
only module that imports `discord`.
"""
