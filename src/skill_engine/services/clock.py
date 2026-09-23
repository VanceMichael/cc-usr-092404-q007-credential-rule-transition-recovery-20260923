"""可替换的时钟，便于把系统固定到“下月生效日”。"""

from dataclasses import dataclass
from datetime import date, datetime, timezone


class Clock:
    def today(self) -> date:
        return date.today()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FixedClock(Clock):
    fixed_today: date
    fixed_now: datetime | None = None

    def today(self) -> date:
        return self.fixed_today

    def now(self) -> datetime:
        return self.fixed_now or datetime(
            self.fixed_today.year, self.fixed_today.month, self.fixed_today.day, tzinfo=timezone.utc
        )
