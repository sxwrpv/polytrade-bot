"""Operator fence tool: list and clear BUY fences (copy_open_claims).

Deliberately a CLI, not an API endpoint, like broadcast.py. The logic lives in
backend/core/fences.py because the Docker image ships backend/ but not
scripts/, so production runs it inside the app container:
    docker compose exec app python -m backend.core.fences list --evidence

From a checkout (repo root, .env loaded automatically):
    .venv/bin/python scripts/fences.py list
    .venv/bin/python scripts/fences.py list --evidence
    .venv/bin/python scripts/fences.py clear <claim_id>          # dry run
    .venv/bin/python scripts/fences.py clear <claim_id> --yes
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.fences import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
