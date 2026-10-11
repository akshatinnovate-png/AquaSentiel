"""AquaSentinel detection and prediction engine.

Dependency-free pure Python so that the *same* code runs in:
  * AWS Lambda (no layers, no packaging beyond a zip)
  * the local development server (`local/server.py`)
  * the offline evaluation harness (`eval/run_eval.py`)
  * the unit tests (`tests/`)

Nothing in this package imports boto3, numpy or any third-party module.
"""

from engine.model import Reading, Signal, Alert, Severity, ModuleResult  # noqa: F401
from engine.state import DeviceState  # noqa: F401
from engine.config import SiteConfig, DEFAULT_SITE  # noqa: F401
from engine.pipeline import Engine  # noqa: F401

__all__ = [
    "Reading",
    "Signal",
    "Alert",
    "Severity",
    "ModuleResult",
    "DeviceState",
    "SiteConfig",
    "DEFAULT_SITE",
    "Engine",
]
__version__ = "1.0.0"
