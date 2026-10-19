"""Human-in-the-loop approval agent.

Detect uncertainty, pause, request human approval, resume with validated context, and record
everything in a tamper-evident audit trail.
"""

from . import agent, approvals, audit, checkpoint, core, uncertainty
from .agent import *  # noqa: F403
from .approvals import *  # noqa: F403
from .audit import *  # noqa: F403
from .checkpoint import *  # noqa: F403
from .core import *  # noqa: F403
from .uncertainty import *  # noqa: F403

__version__ = "0.1.0"

__all__ = sorted(
    {
        *agent.__all__,
        *approvals.__all__,
        *audit.__all__,
        *checkpoint.__all__,
        *core.__all__,
        *uncertainty.__all__,
    }
)
