"""Task 13: patch generation and verification.

A generated patch is only a proposal. It becomes a verified fix only after it
applied cleanly in an isolated copy, the original failing test passes, no
previously passing test fails and every other mandatory check passes. The
original repository is never modified; a human decides whether to accept.
"""

from app.patching.models import (
    PatchExecution,
    PatchLimits,
    PatchProposal,
    PatchStatus,
    PatchValidationResult,
    PatchVerificationResult,
    VerificationCheck,
)
from app.patching.service import PatchingService

__all__ = [
    "PatchExecution",
    "PatchLimits",
    "PatchProposal",
    "PatchStatus",
    "PatchValidationResult",
    "PatchVerificationResult",
    "PatchingService",
    "VerificationCheck",
]
