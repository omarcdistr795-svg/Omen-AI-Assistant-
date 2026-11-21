"""Phase 5 — briefings and automation.

Everything in this package is additive: it plugs into the interfaces the earlier
phases left (scheduler, notifier, event bus, audit log, resource layer, voice and
the Phase 1 tool registry) without rewriting them.

Local-first by default. No dashboard, no cloud calls, no new secrets.
"""

from .alerts import BuildAlerts
from .briefings import BriefingService, MockWeatherSource
from .data import AutomationDataStore
from .bundle import AUTOMATION_TOOL_NAMES, AutomationBundle, register_automation_tools
from .contextual import ContextualReminders
from .guardian import DeadlineGuardian
from .notifier import Notifier
from .scheduler import Scheduler

__all__ = [
    "AUTOMATION_TOOL_NAMES",
    "AutomationBundle",
    "AutomationDataStore",
    "BuildAlerts",
    "BriefingService",
    "ContextualReminders",
    "DeadlineGuardian",
    "MockWeatherSource",
    "Notifier",
    "Scheduler",
    "register_automation_tools",
]
