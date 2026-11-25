# Agent Tokenomics Skill

A lightweight [Agent Skill](https://agentskills.io) that guides developers on token-cost
best practices for agentic AI on Amazon Bedrock / AgentCore / Strands Agents, directly
in the IDE. It is the Developer Tools building block of the Agent Tokenomics Framework.

**Scope — Optimization pillar only:** prompt-cache placement, system-prompt
optimization, context management, intra-session tool result caching, model routing, and
request batching. Governance and Observability are not covered yet.

What it does:
- **Advise:** answers tokenomics questions with the fix, the quality risk, and how to verify.
- **Review:** on request, scans your agent code against a checklist and returns
  prioritized findings with `file:line` evidence.

It runs entirely in your IDE; no code or traces leave your machine beyond what your IDE's
model already sees.

## Layout

```
agent-tokenomics/
├── SKILL.md                       # entry point (instructions + priority table)
└── references/                    # loaded on demand
    ├── prompt-caching.md
    ├── system-prompt.md
    ├── context-management.md
    ├── tool-result-caching.md
    ├── model-routing.md
    ├── request-batching.md
    └── code-review-checklist.md
```

## Install

### Claude Code

```bash
# Personal (all projects)
cp -r skills/agent-tokenomics ~/.claude/skills/

# Or project-scoped (commit it with your repo)
mkdir -p .claude/skills && cp -r skills/agent-tokenomics .claude/skills/
```

Restart Claude Code. The skill is picked up automatically when you ask about token cost,
or invoke it explicitly with `/agent-tokenomics`.

### Kiro

```bash
# Workspace-scoped
mkdir -p .kiro/skills && cp -r skills/agent-tokenomics .kiro/skills/

# Or user-scoped (all workspaces)
cp -r skills/agent-tokenomics ~/.kiro/skills/
```

Kiro loads it when a request matches the skill description.

### Other skill-compatible tools (Cursor, Codex, …)

Copy the `agent-tokenomics/` folder into the tool's skills directory; the format follows
the open Agent Skills spec.

## Try it

- "Why is my agent's `cacheReadInputTokens` always zero?"
- "My input tokens grow every turn with AgentCore Memory on — what should I do?"
- "Should I route my orchestrator to Haiku for simple steps?"
- "Review this repo's agents for token waste."

## Roadmap

Governance (budget ceilings, circuit breakers, delegation slicing, dynamic `max_tokens`),
Observability (attribution, anomaly detection, cache hit-rate, cost-per-outcome), and
SDK-specific guides beyond Strands.
