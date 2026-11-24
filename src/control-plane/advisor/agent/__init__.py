"""Tokenomics Advisor agent -- a Strands agent hosted on its own AgentCore Runtime.

entrypoint.py -- BedrockAgentCoreApp (/invocations + /ping); one Advisor turn.
agent.py      -- builds the model + agent per invocation and runs it.
prompts.py    -- role, rules and the compact pattern index.
tools.py      -- @tool wrappers over the in-process, read-only data tools
                 (advisor/data_tools.py) that record every figure returned.
settings.py   -- env-driven settings injected by the ControlPlane stack.

The agent never states numbers itself: it cites tool figures by reference and
the control plane renders + verifies them (advisor/grounding.py).
"""
