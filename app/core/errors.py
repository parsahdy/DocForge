"""
Error taxonomy. The useful question about a failure: would trying again help?

PermanentError: no, the file itself is the problem (corrupt, encrypted, empty).
TransientError: maybe, the environment is the problem (rate limit, timeout).
Stage 1 only records the code. Stage 2 uses the difference to decide on retries.
"""

from __future__ import annotations


class PermanentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TransientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code