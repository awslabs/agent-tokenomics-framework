"""Cross-plane shared contracts (flags schema + model price map).

Both the application plane (agent) and the control plane import from here so
the two planes never drift on the feature-flag shape or the token->dollar
prices. This is the single source of truth for the "connection contract".
"""
