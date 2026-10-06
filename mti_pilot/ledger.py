"""The pilot's decision ledger: append-only, hash-chained JSON lines.

Every event is written once and never changed. Each line carries the hash of
the line before it, so an edited or deleted line breaks the chain and
``verify`` reports where. A mistake is fixed by appending a CORRECTION event
that names the event it corrects; the original stays as it was written.

There is deliberately no update or delete method.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from tradingagents.dataflows.files import locked

# Event types. A run moves PROPOSED -> RISK_CHECKED (TRADEABLE or BLOCKED) ->
# USER_APPROVED -> MANUALLY_EXECUTED -> CLOSED -> EVALUATED; USER_REJECTED ends it.
PROPOSED = "PROPOSED"
RISK_CHECKED = "RISK_CHECKED"
USER_APPROVED = "USER_APPROVED"
USER_REJECTED = "USER_REJECTED"
MANUALLY_EXECUTED = "MANUALLY_EXECUTED"
CLOSED = "CLOSED"
EVALUATED = "EVALUATED"
CORRECTION = "CORRECTION"
# Pilot-level events, not tied to one run.
MICRO_LIVE_ACTIVATED = "MICRO_LIVE_ACTIVATED"
MICRO_LIVE_DEACTIVATED = "MICRO_LIVE_DEACTIVATED"
PILOT_HALTED = "PILOT_HALTED"
PILOT_REOPENED = "PILOT_REOPENED"

EVENT_TYPES = frozenset({
    PROPOSED, RISK_CHECKED, USER_APPROVED, USER_REJECTED, MANUALLY_EXECUTED, CLOSED,
    EVALUATED, CORRECTION, MICRO_LIVE_ACTIVATED, MICRO_LIVE_DEACTIVATED, PILOT_HALTED,
    PILOT_REOPENED,
})

GENESIS = "0" * 64


class LedgerError(RuntimeError):
    """The ledger refused a write, or found itself tampered with."""


def _digest(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def append(self, event_type: str, payload: dict, run_id: str | None = None) -> dict:
        """Append one event, chained to the last; the chain is checked before writing."""
        if event_type not in EVENT_TYPES:
            raise LedgerError(f"unknown event type {event_type!r}")
        with locked(self.path):
            events = self.events()
            problems = verify_events(events)
            if problems:
                raise LedgerError("ledger chain is broken; nothing appended: " + "; ".join(problems))
            if event_type == PROPOSED and any(
                    e["type"] == PROPOSED and e["run_id"] == run_id for e in events):
                raise LedgerError(f"run {run_id} is already recorded; a recommendation is written once")
            record = {
                "seq": len(events) + 1,
                "event_id": uuid.uuid4().hex,
                "ts": utc_now(),
                "type": event_type,
                "run_id": run_id,
                "payload": payload,
                "prev_hash": events[-1]["hash"] if events else GENESIS,
            }
            record["hash"] = _digest(record)
            line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
            return record

    def verify(self) -> list[str]:
        return verify_events(self.events())

    def run_events(self, run_id: str) -> list[dict]:
        return [e for e in self.events() if e["run_id"] == run_id]


def verify_events(events: list[dict]) -> list[str]:
    """Every break in the chain: a changed line, a removed or reordered one."""
    problems, prev = [], GENESIS
    for index, event in enumerate(events, 1):
        if event.get("seq") != index:
            problems.append(f"line {index}: sequence {event.get('seq')} out of order")
        if event.get("prev_hash") != prev:
            problems.append(f"line {index}: does not follow the line before it")
        if _digest(event) != event.get("hash"):
            problems.append(f"line {index}: content does not match its hash")
        prev = event.get("hash")
    return problems


def write_once(path: str | Path, data: dict) -> str:
    """Write a JSON file that must not already exist; returns its sha256."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError as exc:
        raise LedgerError(f"{path} already exists; a recorded run is never rewritten") from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
