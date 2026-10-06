"""The pilot's workflow over the ledger: modes, approval, manual execution, outcome.

PROPOSED -> RISK_CHECKED (TRADEABLE | BLOCKED) -> USER_APPROVED ->
MANUALLY_EXECUTED -> CLOSED -> EVALUATED

Each step is a separate act. Approving records only that Mats approved; it
never implies that an order was placed. Executions and exits are what Mats
reports after doing them himself in Avanza. Nothing here can place, change or
cancel an order.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import ledger as L
from .config import MICRO_LIVE, SHADOW, PilotConfig
from .guard import BookView, GuardResult, OpenPosition
from .measurement import benchmark_of, hypothetical_outcome, trade_outcome, trade_result

STOCKHOLM = ZoneInfo("Europe/Stockholm")
ACTIVATION_PHRASE = "JAG AKTIVERAR MICRO_LIVE"
REOPEN_PHRASE = "JAG ÅTERÖPPNAR PILOTEN"

_LIFECYCLE = (L.PROPOSED, L.RISK_CHECKED, L.USER_APPROVED, L.USER_REJECTED,
              L.MANUALLY_EXECUTED, L.CLOSED, L.EVALUATED)
_CORRECTABLE = {L.MANUALLY_EXECUTED: {"price", "quantity", "courtage", "slippage",
                                      "executed_at", "comment"},
                L.CLOSED: {"price", "quantity", "courtage", "slippage", "executed_at", "comment"}}


class WorkflowError(RuntimeError):
    """A step the workflow does not allow from where the run stands."""


class MicroLiveBlocked(WorkflowError):
    """MICRO_LIVE is configured but may not run."""


def parse_time(value: str) -> datetime:
    """A timestamp Mats typed; without a zone it is Stockholm time."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise WorkflowError(f"not a timestamp: {value!r} (use e.g. 2026-10-07T09:05)") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=STOCKHOLM)
    return moment


def _positive(name: str, value, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WorkflowError(f"{name} must be a number, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise WorkflowError(f"{name} must be {'non-negative' if allow_zero else 'positive'}, got {value}")
    return float(value)


class Pilot:
    def __init__(self, config: PilotConfig, ledger: L.Ledger | None = None, today=None, now=None):
        self.config = config
        self.ledger = ledger or L.Ledger(config.ledger_path)
        self._today = today or (lambda: datetime.now(STOCKHOLM).date().isoformat())
        self._now = now or (lambda: datetime.now(UTC))

    # --- Reading the ledger ------------------------------------------------------------

    def _corrected(self, events: list[dict]) -> dict[str, dict]:
        """Each event's payload with later corrections applied; the events stay as written."""
        payloads = {e["event_id"]: dict(e["payload"]) for e in events}
        for e in events:
            if e["type"] == L.CORRECTION:
                payloads[e["payload"]["target_event_id"]].update(e["payload"]["changes"])
        return payloads

    def _last(self, events, run_id, types) -> dict | None:
        found = [e for e in events if e["run_id"] == run_id and e["type"] in types]
        return found[-1] if found else None

    def status(self, run_id: str, events: list[dict] | None = None) -> str:
        events = self.ledger.events() if events is None else events
        last = self._last(events, run_id, _LIFECYCLE)
        if last is None:
            raise WorkflowError(f"no run {run_id} in the ledger")
        if last["type"] == L.RISK_CHECKED:
            return last["payload"]["status"]
        return last["type"]

    def proposal(self, run_id: str, events: list[dict] | None = None) -> dict:
        events = self.ledger.events() if events is None else events
        event = self._last(events, run_id, {L.PROPOSED})
        if event is None:
            raise WorkflowError(f"no run {run_id} in the ledger")
        return event["payload"]

    def risk_check(self, run_id: str, events: list[dict] | None = None) -> dict:
        events = self.ledger.events() if events is None else events
        event = self._last(events, run_id, {L.RISK_CHECKED})
        if event is None:
            raise WorkflowError(f"run {run_id} has not been risk-checked")
        return event["payload"]

    def run_ids(self) -> list[str]:
        return [e["run_id"] for e in self.ledger.events() if e["type"] == L.PROPOSED]

    def book(self, events: list[dict] | None = None) -> BookView:
        events = self.ledger.events() if events is None else events
        payloads = self._corrected(events)
        executions = {e["run_id"]: (e, payloads[e["event_id"]])
                      for e in events if e["type"] == L.MANUALLY_EXECUTED}
        closes = {e["run_id"]: payloads[e["event_id"]] for e in events if e["type"] == L.CLOSED}
        realized, open_positions = 0.0, []
        for run_id, (_, execution) in executions.items():
            if run_id in closes:
                realized += trade_result(execution, closes[run_id])["net_pnl"]
            else:
                open_positions.append(OpenPosition(
                    run_id=run_id, ticker=execution["ticker"], quantity=execution["quantity"],
                    entry_price=execution["price"], stop_loss=execution.get("stop_loss")))
        halt_events = [e for e in events if e["type"] in (L.PILOT_HALTED, L.PILOT_REOPENED)]
        halted = bool(halt_events) and halt_events[-1]["type"] == L.PILOT_HALTED
        cap = self.config.max_pilot_loss
        if cap is not None and max(0.0, -realized) >= cap:
            halted = True
        return BookView(halted=halted, realized_pnl=round(realized, 2),
                        open_positions=tuple(open_positions))

    # --- Modes ----------------------------------------------------------------------------

    def micro_live_activated(self, events: list[dict] | None = None) -> bool:
        """Whether Mats activated MICRO_LIVE for exactly the limits configured now."""
        events = self.ledger.events() if events is None else events
        toggles = [e for e in events if e["type"] in (L.MICRO_LIVE_ACTIVATED, L.MICRO_LIVE_DEACTIVATED)]
        if not toggles or toggles[-1]["type"] != L.MICRO_LIVE_ACTIVATED:
            return False
        return toggles[-1]["payload"].get("limits") == self.config.limits()

    def micro_live_blockers(self) -> list[str]:
        """Why MICRO_LIVE cannot run now; empty when it can."""
        if self.config.mode != MICRO_LIVE:
            return [f"MODE is {self.config.mode}"]
        blockers = self.config.micro_live_config_blockers()
        if not blockers and not self.micro_live_activated():
            blockers.append("MICRO_LIVE has not been activated by Mats for the configured limits")
        return blockers

    def run_mode(self) -> str:
        """The mode a new analysis runs in; a MICRO_LIVE that may not run is refused, not downgraded."""
        if self.config.mode == SHADOW:
            return SHADOW
        blockers = self.micro_live_blockers()
        if blockers:
            raise MicroLiveBlocked("MICRO_LIVE is blocked: " + "; ".join(blockers))
        return MICRO_LIVE

    def activate_micro_live(self, confirmation: str) -> dict:
        if self.config.mode != MICRO_LIVE:
            raise WorkflowError("set MODE = \"MICRO_LIVE\" in the pilot configuration first")
        blockers = self.config.micro_live_config_blockers()
        if blockers:
            raise MicroLiveBlocked("MICRO_LIVE cannot be activated: " + "; ".join(blockers))
        if confirmation != ACTIVATION_PHRASE:
            raise WorkflowError(f"activation needs the exact phrase: {ACTIVATION_PHRASE}")
        return self.ledger.append(L.MICRO_LIVE_ACTIVATED,
                                  {"limits": self.config.limits(), "confirmation": confirmation})

    def deactivate_micro_live(self, reason: str) -> dict:
        return self.ledger.append(L.MICRO_LIVE_DEACTIVATED, {"reason": reason})

    # --- The loss cap ---------------------------------------------------------------------

    def check_halt(self) -> bool:
        """Record PILOT_HALTED once realized loss has reached the cap; returns the halt state."""
        events = self.ledger.events()
        book = self.book(events)
        cap = self.config.max_pilot_loss
        halt_events = [e for e in events if e["type"] in (L.PILOT_HALTED, L.PILOT_REOPENED)]
        recorded = bool(halt_events) and halt_events[-1]["type"] == L.PILOT_HALTED
        if cap is not None and book.realized_loss >= cap and not recorded:
            self.ledger.append(L.PILOT_HALTED, {"realized_loss": book.realized_loss,
                                                "max_pilot_loss": cap})
        return book.halted

    def reopen(self, confirmation: str, reason: str) -> dict:
        book = self.book()
        if not book.halted:
            raise WorkflowError("the pilot is not halted")
        cap = self.config.max_pilot_loss
        if cap is not None and book.realized_loss >= cap:
            raise WorkflowError(
                f"realized loss {book.realized_loss:.2f} is still at or above MAX_PILOT_LOSS {cap}: "
                "reopening needs a deliberately raised MAX_PILOT_LOSS in the configuration")
        if confirmation != REOPEN_PHRASE:
            raise WorkflowError(f"reopening needs the exact phrase: {REOPEN_PHRASE}")
        if not reason.strip():
            raise WorkflowError("reopening needs a reason")
        return self.ledger.append(L.PILOT_REOPENED, {"reason": reason, "confirmation": confirmation,
                                                     "realized_loss": book.realized_loss,
                                                     "max_pilot_loss": cap})

    # --- Recording a run ------------------------------------------------------------------

    def record_proposal(self, record: dict) -> dict:
        return self.ledger.append(L.PROPOSED, record, run_id=record["run_id"])

    def record_risk_check(self, run_id: str, result: GuardResult) -> dict:
        return self.ledger.append(L.RISK_CHECKED, result.to_dict(), run_id=run_id)

    # --- Mats' decisions ------------------------------------------------------------------

    def approve(self, run_id: str, note: str = "") -> dict:
        events = self.ledger.events()
        status = self.status(run_id, events)
        if status != "TRADEABLE":
            raise WorkflowError(f"run {run_id} is {status}; only a TRADEABLE proposal can be approved")
        check = self.risk_check(run_id, events)
        if check["mode"] != MICRO_LIVE:
            raise WorkflowError("a SHADOW proposal is an observation, not an order; it cannot be approved")
        self.run_mode()  # MICRO_LIVE must still be allowed now
        proposal = self.proposal(run_id, events)
        if proposal.get("is_historical") or proposal.get("created_on") != self._today():
            raise WorkflowError("only a proposal made today on today's data can be approved")
        book = self.book(events)
        if book.halted:
            raise WorkflowError("the pilot is halted; nothing can be approved until Mats reopens it")
        order = check["order"]
        if any(p.ticker == order["instrument"] for p in book.open_positions):
            raise WorkflowError(f"the pilot already holds {order['instrument']}; analyse again")
        cfg = self.config
        if book.realized_loss + book.open_risk + order["max_risk_amount"] > cfg.max_pilot_loss:
            raise WorkflowError("approving now could take the pilot past MAX_PILOT_LOSS")
        if book.committed_capital + order["notional"] > cfg.pilot_capital:
            raise WorkflowError("the pilot capital no longer covers this order")
        return self.ledger.append(L.USER_APPROVED, {"note": note, "approved_order": order},
                                  run_id=run_id)

    def reject(self, run_id: str, note: str = "") -> dict:
        status = self.status(run_id)
        if status not in ("TRADEABLE", "BLOCKED", L.USER_APPROVED):
            raise WorkflowError(f"run {run_id} is {status}; it can no longer be rejected")
        return self.ledger.append(L.USER_REJECTED, {"note": note}, run_id=run_id)

    def record_execution(self, run_id: str, *, price: float, quantity: float, courtage: float,
                         executed_at: str, slippage: float = 0.0, comment: str = "") -> dict:
        """Record the buy Mats placed himself; it does not place anything."""
        events = self.ledger.events()
        status = self.status(run_id, events)
        if status != L.USER_APPROVED:
            raise WorkflowError(f"run {run_id} is {status}; an execution follows an approval")
        price, quantity = _positive("price", price), _positive("quantity", quantity)
        courtage = _positive("courtage", courtage, allow_zero=True)
        slippage = _positive("slippage", slippage, allow_zero=True)
        moment = parse_time(executed_at)
        approved = self._last(events, run_id, {L.USER_APPROVED})
        if moment > self._now() + timedelta(minutes=5):
            raise WorkflowError("the execution time is in the future")
        if moment < datetime.fromisoformat(approved["ts"]):
            raise WorkflowError("the execution time is before the approval")
        order = approved["payload"]["approved_order"]
        warnings = []
        if quantity > (order.get("quantity") or 0):
            warnings.append("QUANTITY_ABOVE_APPROVED")
        stop = order.get("stop_loss")
        if stop is not None and price <= stop:
            warnings.append("FILL_AT_OR_BELOW_STOP")
        if stop is not None and (price - stop) * quantity > (self.config.max_risk_per_trade or 0):
            warnings.append("EXECUTION_EXCEEDS_RISK_LIMIT")
        return self.ledger.append(L.MANUALLY_EXECUTED, {
            "ticker": order["instrument"], "price": price, "quantity": quantity,
            "courtage": courtage, "slippage": slippage, "executed_at": moment.isoformat(),
            "comment": comment, "stop_loss": stop, "warnings": warnings,
        }, run_id=run_id)

    def record_close(self, run_id: str, *, price: float, quantity: float, courtage: float,
                     executed_at: str, slippage: float = 0.0, comment: str = "") -> dict:
        """Record the exit Mats made himself; then apply the loss cap."""
        events = self.ledger.events()
        status = self.status(run_id, events)
        if status != L.MANUALLY_EXECUTED:
            raise WorkflowError(f"run {run_id} is {status}; only an executed trade can be closed")
        execution_event = self._last(events, run_id, {L.MANUALLY_EXECUTED})
        execution = self._corrected(events)[execution_event["event_id"]]
        price, quantity = _positive("price", price), _positive("quantity", quantity)
        if quantity != execution["quantity"]:
            raise WorkflowError(f"v0.1 closes whole positions: {execution['quantity']} units are open")
        moment = parse_time(executed_at)
        if moment < datetime.fromisoformat(execution["executed_at"]):
            raise WorkflowError("the exit time is before the entry")
        if moment > self._now() + timedelta(minutes=5):
            raise WorkflowError("the exit time is in the future")
        close = {"price": price, "quantity": quantity,
                 "courtage": _positive("courtage", courtage, allow_zero=True),
                 "slippage": _positive("slippage", slippage, allow_zero=True),
                 "executed_at": moment.isoformat(), "comment": comment}
        event = self.ledger.append(L.CLOSED, {**close, "result": trade_result(execution, close)},
                                   run_id=run_id)
        self.check_halt()
        return event

    def correct(self, target_event_id: str, changes: dict, reason: str) -> dict:
        """Append a correction to a recorded execution or exit; the original stays as written."""
        events = self.ledger.events()
        target = next((e for e in events if e["event_id"] == target_event_id), None)
        if target is None:
            raise WorkflowError(f"no event {target_event_id}")
        allowed = _CORRECTABLE.get(target["type"])
        if allowed is None:
            raise WorkflowError(f"a {target['type']} event cannot be corrected; "
                                "recommendations and checks stand as recorded")
        if not changes or set(changes) - allowed:
            raise WorkflowError(f"correctable fields: {', '.join(sorted(allowed))}")
        if not reason.strip():
            raise WorkflowError("a correction needs a reason")
        clean = {}
        for key, value in changes.items():
            if key in ("price", "quantity"):
                clean[key] = _positive(key, value)
            elif key in ("courtage", "slippage"):
                clean[key] = _positive(key, value, allow_zero=True)
            elif key == "executed_at":
                clean[key] = parse_time(value).isoformat()
            else:
                clean[key] = str(value)
        event = self.ledger.append(L.CORRECTION, {"target_event_id": target_event_id,
                                                  "target_type": target["type"],
                                                  "changes": clean, "reason": reason},
                                   run_id=target["run_id"])
        self.check_halt()
        return event

    # --- Outcome --------------------------------------------------------------------------

    def evaluate(self, run_id: str, *, get_closes=None) -> dict:
        """Score one run against its benchmark and record the result as EVALUATED.

        A closed trade is scored on what Mats actually got, net of costs. A run
        that was never executed is scored hypothetically, exactly as the report
        does (``measurement.hypothetical_outcome``). ``mti-pilot evaluate``
        without a run reports every run instead, and records nothing.
        """
        if get_closes is None:
            from tradingagents.dataflows.vendors.yahoo.market import get_closes
        events = self.ledger.events()
        status = self.status(run_id, events)
        proposal = self.proposal(run_id, events)
        benchmark = benchmark_of(proposal)
        if status == L.EVALUATED:
            last_eval = self._last(events, run_id, {L.EVALUATED})
            corrected_after = any(e["type"] == L.CORRECTION and e["run_id"] == run_id
                                  and e["seq"] > last_eval["seq"] for e in events)
            if not corrected_after:
                raise WorkflowError(f"run {run_id} is already evaluated")
            status = L.CLOSED if self._last(events, run_id, {L.CLOSED}) else "SHADOW"

        if status == L.CLOSED:
            payloads = self._corrected(events)
            execution = payloads[self._last(events, run_id, {L.MANUALLY_EXECUTED})["event_id"]]
            close = payloads[self._last(events, run_id, {L.CLOSED})["event_id"]]
            payload = trade_outcome(execution, close, benchmark, get_closes)
        elif status in ("TRADEABLE", "BLOCKED", L.USER_REJECTED, "SHADOW"):
            payload = {"kind": "shadow", **hypothetical_outcome(proposal, get_closes),
                       "guard_status": self.risk_check(run_id, events)["status"],
                       "final_rating": proposal["decision"]["final_rating"],
                       "is_historical": proposal["is_historical"]}
        else:
            raise WorkflowError(f"run {run_id} is {status}; nothing to evaluate yet")
        if payload["state"] != "ok":
            raise WorkflowError(f"{payload['reason']}; evaluate later")
        return self.ledger.append(L.EVALUATED, payload, run_id=run_id)
