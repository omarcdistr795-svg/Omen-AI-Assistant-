"""Phase 3 core tools: web, files, local data, shell, git, system and rollback.

Every tool in this package declares a permission class and is routed through the
Phase 1 ToolRegistry (policy engine -> approval gate -> audit log). No tool here
calls a side-effecting API directly from model text.
"""

from .base import CoreTool, CoreToolError, PathPolicy, PathPolicyError, truncate
from .connectors import Connector, ConnectorRegistry, MockConnector
from .register import CORE_TOOL_NAMES, register_core_tools

__all__ = [
    "CoreTool",
    "CoreToolError",
    "PathPolicy",
    "PathPolicyError",
    "truncate",
    "Connector",
    "ConnectorRegistry",
    "MockConnector",
    "CORE_TOOL_NAMES",
    "register_core_tools",
]
