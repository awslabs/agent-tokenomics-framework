"""Advisor runtime settings (env-driven).

The ControlPlane stack injects the deployment values (model, guardrail, tables). The tunables
default here only; override one by setting its ADVISOR_* variable on the Advisor runtime.
"""

from __future__ import annotations

import os

import config
from advisor import MAX_TURNS_PER_CHAT, WINDOWS  # noqa: F401 -- WINDOWS is re-exported for the agent modules
from shared import price_map

AWS_REGION = config.AWS_REGION
# Default from the model-id single source (shared/price_map); env override wins.
ADVISOR_MODEL = os.environ.get("ADVISOR_MODEL", price_map.SONNET_5_5)
ADVISOR_GUARDRAIL_ID = os.environ.get("ADVISOR_GUARDRAIL_ID", "")
ADVISOR_GUARDRAIL_VERSION = os.environ.get("ADVISOR_GUARDRAIL_VERSION", "")

# Bounds (inner layers give up before the worker's 170 s read timeout).
# Per model call. A turn cut off here shows no partial answer (the worker retries it once asking
# for a shorter one); sized with ~2x headroom over a NARRATIVE_MAX_WORDS answer and its references.
MAX_OUTPUT_TOKENS = int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1000"))
# Word budget for an answer's narrative, stated in the (cached) system prompt and answer schema.
NARRATIVE_MAX_WORDS = int(os.environ.get("ADVISOR_NARRATIVE_MAX_WORDS", "120"))
MAX_TURNS = int(os.environ.get("ADVISOR_MAX_TURNS", "8"))
DEADLINE_SECONDS = float(os.environ.get("ADVISOR_DEADLINE_SECONDS", "150"))
# Max wait between streamed chunks of one model call. The final structured answer over a large
# tool context (plus the guardrail check) can pause longer than 20 s before streaming.
MODEL_READ_TIMEOUT_SECONDS = int(os.environ.get("ADVISOR_MODEL_READ_TIMEOUT_SECONDS", "60"))
# Per-call limit for one in-process data tool (advisor/data_tools.py).
TOOL_TIMEOUT_SECONDS = 8.0
MAX_MESSAGE_CHARS = 2000
# Question/answer pairs of the chat the agent sees (a sliding window over the seeded history).
HISTORY_WINDOW_TURNS = int(os.environ.get("ADVISOR_HISTORY_WINDOW_TURNS", "10"))
# Payload bound only: the worker sends every completed pair of the chat.
MAX_HISTORY_MESSAGES = 2 * MAX_TURNS_PER_CHAT
