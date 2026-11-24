"""Tokenomics Advisor: everything the feature runs, in one package.

api.py         -- /advisor/chats routes (called from lambda_entry.py).
worker.py      -- AdvisorWorker Lambda: async-invoked per chat message; claims
                  the pending reply, invokes the Advisor runtime, grounds the
                  answer and stores it in the AdvisorChats table.
chats.py       -- AdvisorChats table store; client.py -- Advisor runtime client.
grounding.py   -- renders figure references and verifies the answer.
data_tools.py  -- the read-only data tools (service/, kb.py, catalog.py) that
                  the agent runs in-process.
agent/         -- the Strands agent container, hosted on its own AgentCore Runtime.
"""

# The analysis windows every Advisor layer accepts (defined once; no imports here, so the
# runtime image and the worker Lambda can both import it).
WINDOWS = ("24h", "7d", "30d")
# Questions one chat may hold (the store enforces it; the agent bounds the history payload by it).
MAX_TURNS_PER_CHAT = 50
