"""RBForge public package.

Example:
    from RBForge import forge_tool, run_forged_tool
"""

from RBForge.forge_tool import forge_tool, run_forged_tool
from rbforge_core.version import RBFORGE_VERSION

__version__ = RBFORGE_VERSION

__all__ = ["__version__", "forge_tool", "run_forged_tool"]
