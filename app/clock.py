"""可测试的时钟：测试可通过接口推进时间以确定性验证同意过期。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    """单调推进的时钟；now() 始终返回带 UTC 时区的时间。"""

    def __init__(self) -> None:
        self._offset: timedelta = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self._offset

    def advance(self, seconds: int) -> datetime:
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        self._offset += timedelta(seconds=seconds)
        return self.now()

    def reset(self) -> None:
        self._offset = timedelta(0)
