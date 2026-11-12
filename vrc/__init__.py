"""Independent replay of the tests attached to a change proposal."""

__version__ = "0.3.0"

from .receipt import Receipt, derive_verdict
from .replay import ReplayOutcome, replay

__all__ = ["Receipt", "ReplayOutcome", "derive_verdict", "replay"]
