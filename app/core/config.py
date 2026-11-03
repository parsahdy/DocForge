"""
Application settings (decision D12): every limit and endpoint is configuration.

Secrets have no default on purpose: a missing secret should stop the process at
startup, not at the first request.
"""