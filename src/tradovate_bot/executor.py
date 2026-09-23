"""Execute planned actions and produce the run report."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradovate_bot.config import Settings
from tradovate_bot.sync.reconciler import ActionType, PlannedAction
from tradovate_bot.tradovate.client import TradovateClient


@dataclass
class RunReport:
    mode: str
    job: str
    timestamp: str
    planned_actions: list[dict[str, Any]] = field(default_factory=list)
    executed: list[dict[str, Any]] = field(default_factory=list)
    placed_orders: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
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
            timestamp=datetime.now(UTC).isoformat(),
        )

        for action in actions:
            payload = _payload(action)
            report.planned_actions.append(payload)

            if not self._settings.writes_enabled:
                report.skipped.append({**payload, "skip_reason": "TRADING_MODE=dry_run"})
                continue

            if self._client is None or account is None:
                report.errors.append("Missing Tradovate client or account for execution")
                continue

            try:
                result = self._run_one(action, account)
            except Exception as exc:  # noqa: BLE001 - surface API failures in the report
                report.errors.append(f"{action.action.value}: {exc}")
                continue

            report.executed.append({**payload, "result": result})
            if action.action is ActionType.PLACE_BRACKET and action.plan is not None:
                report.placed_orders.append(
                    {
                        "tag": action.plan.trade.client_tag,
                        "symbol": action.symbol,
                        "side": action.plan.side.value if action.plan.side else None,
                        "entry": action.plan.entry,
                        "target": action.plan.target,
                        "stop_loss": action.plan.stop_loss,
                        "quantity": action.plan.trade.quantity,
                        "order_id": _order_id_from(result),
                    }
                )

        return report

    def _run_one(self, action: PlannedAction, account: dict[str, Any]) -> Any:
        assert self._client is not None
        if action.action is ActionType.PLACE_BRACKET:
            plan = action.plan
            assert plan is not None and plan.side is not None and plan.entry is not None
            assert plan.target is not None and plan.stop_loss is not None
            return self._client.place_bracket_order(
                account_spec=account["name"],
                account_id=account["id"],
                symbol=plan.trade.tradovate_symbol,
                action=plan.side.tradovate_action,
                exit_action=plan.side.exit_action,
                quantity=plan.trade.quantity,
                entry=plan.entry,
                target=plan.target,
                stop_loss=plan.stop_loss,
                custom_tag=plan.trade.client_tag,
            )
        if action.action is ActionType.CANCEL_ORDER:
            assert action.order_id is not None
            return self._client.cancel_order(action.order_id)
        if action.action is ActionType.FLATTEN_POSITION:
            assert action.symbol is not None
            return self._client.liquidate_position(
                account_id=account["id"],
                symbol=action.symbol,
            )
        raise ValueError(f"Unsupported action: {action.action}")


def _payload(action: PlannedAction) -> dict[str, Any]:
    return {
        "action": action.action.value,
        "reason": action.reason,
        "symbol": action.symbol,
        "order_id": action.order_id,
        "details": action.details,
    }


def _order_id_from(result: Any) -> int | None:
    if isinstance(result, dict):
        value = result.get("orderId") or result.get("id")
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def write_report(report: RunReport, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.timestamp.replace(":", "-")
    path = output_dir / f"{report.job}-{stamp}.json"
    path.write_text(report.to_json(), encoding="utf-8")
    return path