"""What a Handler promises about being run more than once.

All that is left of the durable-execution vocabulary. The job, lease and
timer records — and the command and event catalogs that went with them —
belonged to the event-sourced engine and were deleted with it. This one
outlived it because it is a property of the *Handler*, not of the engine:
whether re-running an attempt is safe is the Handler's claim, and the
LangGraph adapter reads it to decide what it may replay.
"""

from __future__ import annotations

from enum import Enum


class ExecutionSafety(str, Enum):
    REPLAY_SAFE = "replay_safe"
    UNKNOWN_ON_LEASE_LOSS = "unknown_on_lease_loss"
