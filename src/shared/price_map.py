"""Bedrock token->dollar price map (single source of truth for both planes).

Prices are USD per 1,000 tokens. Prompt caching splits input tokens into three
buckets that Bedrock bills at different rates:

  - input        : standard uncached input tokens
  - cache_write  : tokens written to cache (usually a small premium over input)
  - cache_read   : tokens read from cache (heavily discounted -> the savings)
  - output       : generated tokens (most expensive)

IMPORTANT (verified against Bedrock docs): when caching is on, the response's
`inputTokens` counts ONLY the non-cached input. Total input =
inputTokens + cacheReadInputTokens + cacheWriteInputTokens. The cost engine
must price the three buckets separately, never double-count.

These are approximate public list prices for demo/benchmark math. They are NOT
a billing source of truth -- adjust to your negotiated rates. Keep them here so
both planes compute identical dollars from identical token counts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000 tokens for each billing bucket."""

    input: float
    output: float
    cache_write: float
    cache_read: float


# ---- Model id single source of truth ---------------------------------------
# These inference-profile ids are the ONLY place model ids are written.
# Everything else imports them: the agent config (per-agent + router-judge
# defaults), the Advisor runtime settings, and the CDK Bedrock IAM scoping (via
# known_model_ids()). Change a model here and it changes everywhere -- a price
# entry below, the agents that default to it, and the IAM grant all follow.
# Frontier / capable tier. Claude Sonnet 4.5 is deprecated (Bedrock Legacy since
# ~Oct 2026, EOL 2027-04-08); Anthropic's named successor is Sonnet 5.5. We use
# the Global cross-Region inference profile (`global.` prefix; routes to
# commercial Regions worldwide, CloudWatch/CloudTrail still log in the source
# Region) because Global CRIS bills ~10% below the regional / `us.` rate, which
# the price entry below reflects.
SONNET_5_5 = "global.anthropic.claude-sonnet-5-5"  # frontier / capable tier (global CRIS)
# Mid / cheap tier. Claude Haiku 4.5 is deprecated (Haiku 5.5 launched 2026-10-07
# and is the default Haiku); 5.5 is ~10x cheaper and more capable. Unlike Sonnet
# 5.5 it DOES have a `us.` inference profile, so we keep the regional convention.
HAIKU_5_5 = "us.anthropic.claude-haiku-5-5"  # mid / cheap tier
NOVA_LITE = "us.amazon.nova-lite-v1:0"  # small tier (specialists' floor + the routing judge)

# Embedding model for the Advisor Knowledge Base. A Bedrock FOUNDATION-model id
# (not an `us.` inference profile, and not token-priced here), so it is kept out
# of PRICE_PER_1K / known_model_ids() -- but it lives here so EVERY model id in
# the solution has one home. Consumed by the CDK ControlPlaneStack to scope the
# KB role's bedrock:InvokeModel grant and to configure the KB's embedding config.
TITAN_EMBED_V2 = "amazon.titan-embed-text-v2:0"

# Model id (inference-profile id) -> price per 1K tokens.
# Source: https://aws.amazon.com/bedrock/pricing/ (on-demand, us-east-1). Each
# entry uses the rate for the profile its id routes through: Global CRIS for
# `global.` ids, the regional / geo rate for `us.` ids (~10% above Global).
PRICE_PER_1K: dict[str, ModelPrice] = {
    # Sonnet 5.5, Global CRIS: $2.00 / $10.00 per 1M in/out (regional is
    # $2.20 / $11.00). Cache write $2.50, cache read $0.10 per 1M (cache reads
    # were cut 50% on 2026-10-07, so they are now 0.05x input, not 0.1x).
    SONNET_5_5: ModelPrice(input=0.002, output=0.010, cache_write=0.0025, cache_read=0.0001),
    # Haiku 5.5, regional (`us.` profile): $0.11 / $0.55 per 1M in/out for prompts
    # <=100K tokens (the tier the specialists + summarizer run in); it steps up
    # to $0.55 / $2.75 above 100K. Cache write $0.1375, cache read $0.011 per 1M.
    HAIKU_5_5: ModelPrice(input=0.00011, output=0.00055, cache_write=0.0001375, cache_read=0.000011),
    # Nova Lite: $0.06 / $0.24 per 1M in/out; cache writes are free, cache read $0.015.
    NOVA_LITE: ModelPrice(input=0.00006, output=0.00024, cache_write=0.0, cache_read=0.000015),
}

# Fallback used when a model id is unknown, so cost math never crashes a demo.
_DEFAULT_PRICE = ModelPrice(input=0.003, output=0.015, cache_write=0.00375, cache_read=0.0003)


def known_model_ids() -> list[str]:
    """All model (inference-profile) ids this solution uses.

    Single source of truth shared by the agent (pricing) and the infra stack
    (Bedrock IAM scoping), so a model change is made in exactly one place.
    """
    return list(PRICE_PER_1K.keys())


def price_for(model_id: str) -> ModelPrice:
    """Return the price for a model id, falling back to a safe default."""
    return PRICE_PER_1K.get(model_id, _DEFAULT_PRICE)


def cost_usd(
    model_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Dollarize a single model call from its token buckets.

    Args map 1:1 to Bedrock Converse `usage` fields:
      input_tokens        -> usage.inputTokens (non-cached input when caching on)
      output_tokens       -> usage.outputTokens
      cache_read_tokens   -> usage.cacheReadInputTokens
      cache_write_tokens  -> usage.cacheWriteInputTokens
    """
    p = price_for(model_id)
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_tokens * p.cache_read
        + cache_write_tokens * p.cache_write
    ) / 1000.0
