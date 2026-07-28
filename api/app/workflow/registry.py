"""Frozen adapter-construction and clock seams."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.sources.ats.contracts import ATSAdapter

AdapterFactory = Callable[[], ATSAdapter]


@dataclass(frozen=True, slots=True)
class AdapterRegistry:
    """Exactly the two approved zero-argument adapter factories."""

    greenhouse: AdapterFactory
    lever: AdapterFactory


class UTCClock(Protocol):
    def now(self) -> datetime:
        """Return an aware zero-offset datetime."""
        ...
