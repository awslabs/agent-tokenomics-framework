"""Benchmarking: async worker Lambda + the multi-turn scenario corpus.

worker.py       -- the async-invoked Lambda that runs the N x M A/B loop.
scenarios.json  -- the fixed 5x5 multi-turn conversation corpus (subset to N x M).

The benchmark handler + load generator live under handlers/ and the metric
aggregation under lib/, following the same convention as the other features.
"""
