"""Re-export for pip installability: ``import olivetin_mcp`` works the same as ``import main``."""

from main import *  # noqa: F401,F403
from main import cfg, mcp  # noqa: F401  — explicit names for entry-point use
