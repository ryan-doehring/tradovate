from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradovate_bot.config import Settings, TradingMode
from tradovate_bot.sync.reconciler import ActionType, PlannedAction
from tradovate_bot.tradovate.client import TradovateClient


@dataclass
class RunReport:
    mode: str
    job: str
    timestamp: str
    planned_actions: list[dict[str, Any]] = field(default_factory=list)
    executed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


class ActionExecutor:
    def __init__(self, settings: Settings, client: TradovateClient | None = None) -> None:
        self._settings = settings
        self._client = client

    def execute(
        self,
        actions: list[PlannedAction],
        *,
        job: str,
        account: dict[str, Any] | None = None,
    ) -> RunReport:
        report = RunReport(
            mode=self._settings.trading_mode.value,
            job=job,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        for action in actions:
            payload = {
                "action": action.action.value,
                "reason": action.reason,
                "symbol": action.symbol,
                "order_id": action.order_id,
                "details": action.details,
            }
            report.planned_actions.append(payload)

            if not self._settings.writes_enabled:
                report.skipped.append({**payload, "skip_reason": "TRADING_MODE=dry_run"})
                continue

            if self._client is None or account is None:
                report.errors.append("Missing Tradovate client or account for live execution")
                continue

            try:
                result = self._run_one(action, account)
                report.executed.append({**payload, "result": result})
            except Exception as exc:  # noqa: BLE001 - surface API failures in report
                report.errors.append(f"{action.action.value}: {exc}")

        return report

    def _run_one(self, action: PlannedAction, account: dict[str, Any]) -> Any:
        assert self._client is not None
        if action.action == ActionType.PLACE_BRACKET:
            assert action.trade is not None
            return self._client.place_bracket_order(
                account_spec=account["name"],
                account_id=account["id"],
                trade=action.trade,
                custom_tag=action.trade.client_tag,
            )
        if action.action == ActionType.CANCEL_ORDER:
            assert action.order_id is not None
            return self._client.cancel_order(action.order_id)
        if action.action == ActionType.FLATTEN_POSITION:
            assert action.symbol is not None
            return self._client.liquidate_position(
                account_id=account["id"],
                symbol=action.symbol,
            )
        raise ValueError(f"Unsupported action: {action.action}")


def write_report(report: RunReport, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{report.job}-{report.timestamp.replace(':', '-')}.json"
    path.write_text(report.to_json(), encoding="utf-8")
    return path
