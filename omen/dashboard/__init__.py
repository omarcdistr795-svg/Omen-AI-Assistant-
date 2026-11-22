"""Phase 6 — local HUD-style dashboard.

Reads and acts only through existing public Omen interfaces. No model loading,
no bypass of policy/approval/audit, local-only by default.
"""

from .server import DashboardServer

__all__ = ["DashboardServer"]
