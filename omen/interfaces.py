"""Shared Phase 1 interfaces.

These protocols are the contract between phases. Phase 2-7 implement against
them; do not change them without approval.
"""

from typing import Any, Protocol


class ProviderAdapter(Protocol):
    name: str

    def complete(self, messages: list[dict], *, stream: bool = False, **kwargs) -> Any: ...

    def health(self) -> dict: ...


class Tool(Protocol):
    name: str
    description: str
    permission_class: str  # read_only | low_risk_write | external_write | high_risk

    def schema(self) -> dict: ...

    def dry_run(self, **kwargs) -> dict: ...

    def execute(self, **kwargs) -> dict: ...


class MemoryStore(Protocol): ...


class Scheduler(Protocol): ...


class VoiceIO(Protocol): ...


class EventBus(Protocol): ...


class Notifier(Protocol): ...
