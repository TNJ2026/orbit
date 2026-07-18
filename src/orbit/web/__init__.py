"""The production composition root for the new Runtime.

`web/app.py` wires ports to adapters and owns the process lifecycle. It holds
no state machine, no routing decision, no planner policy and no SQL — those
belong to `orbit.workflow`.
"""

from .app import RuntimeComposition, create_app

__all__ = ["RuntimeComposition", "create_app"]
