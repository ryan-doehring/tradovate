from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class NewsPause:
    """A bracket cancelled for a news blackout that should be re-placed after reopen."""

    event_id: str
    paused_at: datetime

    def is_stale(self, now: datetime, *, ttl_minutes: int) -> bool:
        return now - self.paused_at > timedelta(minutes=ttl_minutes)

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "paused_at": self.paused_at.isoformat()}

    @classmethod
    def from_dict(cls, data: object) -> NewsPause | None:
        """Parse a stored pause. Timestamp-less (legacy) entries are dropped as stale."""
        if not isinstance(data, dict):
            return None
        event_id = data.get("event_id")
        paused_at_raw = data.get("paused_at")
        if not event_id or not paused_at_raw:
            return None
        try:
            paused_at = datetime.fromisoformat(str(paused_at_raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if paused_at.tzinfo is None:
            paused_at = paused_at.replace(tzinfo=timezone.utc)
        return cls(event_id=str(event_id), paused_at=paused_at)


@dataclass
class BotState:
    """Tracks brackets paused for news so they can be re-placed after the reopen window."""

    paused_for_news: dict[str, NewsPause] = field(default_factory=dict)

    def pause_for_news(
        self, client_tag: str, event_id: str, *, now: datetime | None = None
    ) -> None:
        self.paused_for_news[client_tag] = NewsPause(
            event_id=event_id,
            paused_at=now or datetime.now(timezone.utc),
        )

    def clear_pause(self, client_tag: str) -> None:
        self.paused_for_news.pop(client_tag, None)

    def paused_event_id(self, client_tag: str) -> str | None:
        pause = self.paused_for_news.get(client_tag)
        return pause.event_id if pause else None

    def prune_stale_pauses(self, now: datetime, *, ttl_minutes: int) -> list[str]:
        """Drop pauses that outlived their event so the bracket can be re-placed.

        Returns the client tags that were cleared, so callers can report them.
        """
        stale = [
            tag
            for tag, pause in self.paused_for_news.items()
            if pause.is_stale(now, ttl_minutes=ttl_minutes)
        ]
        for tag in stale:
            self.paused_for_news.pop(tag, None)
        return sorted(stale)

    @property
    def paused_tags(self) -> set[str]:
        return set(self.paused_for_news.keys())

    def to_dict(self) -> dict:
        return {
            "paused_for_news": {
                tag: pause.to_dict() for tag, pause in self.paused_for_news.items()
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> BotState:
        pauses: dict[str, NewsPause] = {}
        for tag, raw in (data.get("paused_for_news") or {}).items():
            pause = NewsPause.from_dict(raw)
            if pause is not None:
                pauses[str(tag)] = pause
        return cls(paused_for_news=pauses)


def load_state(path: Path) -> BotState:
    if not path.exists():
        return BotState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt state file must not block trading; start clean and let the
        # reconciler re-place anything the sheet still wants.
        return BotState()
    return BotState.from_dict(data)


def save_state(path: Path, state: BotState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")

