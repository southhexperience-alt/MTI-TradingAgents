"""A fixed grid of risk-guard inputs, for pinning guard.py's outcomes.

``tests/data/mti_guard_golden.json`` holds the guard's answers for this grid as
produced by the guard at commit 6e577c1, before the measurement changes. The
regression test recomputes them and requires the same answer for every case.
Regenerate only on a deliberate rule change: ``python -m tests.mti_guard_cases``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

from mti_pilot.config import MICRO_LIVE, SHADOW, PilotConfig
from mti_pilot.guard import BookView, OpenPosition, ReferencePrice, check
from mti_pilot.proposal import ExtractedDecision
from tradingagents.portfolio import PortfolioContext, Position

GOLDEN = Path(__file__).parent / "data" / "mti_guard_golden.json"
TICKER = "VOLV-B.ST"
DATE = "2026-10-06"

_BUY = {"final_rating": "Buy", "pm_direction": "BUY", "pm_structured": True,
        "price_target": 110.0, "time_horizon": "1-3 weeks", "trader_structured": True,
        "trader_action": "BUY", "entry_price": 100.0, "stop_loss": 95.0,
        "position_sizing_text": None, "rationale": "r"}

DECISIONS = {
    "buy": {},
    "overweight": {"final_rating": "Overweight"},
    "no-stop": {"stop_loss": None},
    "no-entry": {"entry_price": None},
    "stop-above-entry": {"stop_loss": 101.0},
    "target-below": {"price_target": 99.0},
    "stop-above-market": {"entry_price": 120.0, "stop_loss": 101.0},
    "free-text": {"trader_structured": False, "trader_action": None, "entry_price": None,
                  "stop_loss": None},
    "conflict": {"trader_action": "SELL"},
    "hold": {"final_rating": "Hold", "pm_direction": "HOLD", "trader_action": "HOLD"},
    "sell": {"final_rating": "Sell", "pm_direction": "SELL", "trader_action": "SELL"},
    "review": {"final_rating": "REVIEW", "pm_direction": None},
    "no-rationale": {"rationale": ""},
    "tight-stop": {"stop_loss": 99.9},
}

_LIMITS = {"pilot_capital": 10_000.0, "max_risk_per_trade": 100.0, "max_pilot_loss": 300.0}
CONFIGS = {
    "shadow-bare": (SHADOW, {}),
    "shadow-amounts": (SHADOW, _LIMITS),
    "live": (MICRO_LIVE, _LIMITS),
    "live-no-risk": (MICRO_LIVE, {"pilot_capital": 10_000.0, "max_pilot_loss": 300.0}),
}

BOOKS = {
    "empty": BookView(),
    "halted": BookView(halted=True, realized_pnl=-400.0),
    "open-other": BookView(realized_pnl=-120.0,
                           open_positions=(OpenPosition("x", "ERIC-B.ST", 20, 100.0, 95.0),)),
    "open-losing": BookView(open_positions=(OpenPosition("y", TICKER, 10, 130.0, 110.0),)),
}

PORTFOLIOS = {
    "none": None,
    "flat": PortfolioContext(),
    "losing": PortfolioContext(positions=[Position(ticker=TICKER, quantity=10, average_price=120.0)]),
    "winning": PortfolioContext(positions=[Position(ticker=TICKER, quantity=10, average_price=80.0)]),
    "unknown-cost": PortfolioContext(positions=[Position(ticker=TICKER, quantity=10)]),
}

REFERENCES = {
    "ok": ReferencePrice(100.5, DATE, "test prices"),
    "missing": ReferencePrice(None, None, None),
}


def cases():
    for (d, dc), (c, (mode, amounts)), (b, book), (p, portfolio), (r, ref) in itertools.product(
            DECISIONS.items(), CONFIGS.items(), BOOKS.items(), PORTFOLIOS.items(),
            REFERENCES.items()):
        yield (f"{d}|{c}|{b}|{p}|{r}",
               {"ticker": TICKER, "analysis_date": DATE,
                "decision": ExtractedDecision(**{**_BUY, **dc}), "reference": ref,
                "config": PilotConfig(mode=mode, **amounts), "mode": mode, "book": book,
                "portfolio": portfolio})


def outcome(kwargs) -> str:
    """Status and codes in the clear, and a digest of the whole result (messages, order)."""
    result = check(**kwargs)
    full = json.dumps(result.to_dict(), sort_keys=True, default=str)
    return " ".join([result.status, ",".join(result.blocker_codes()) or "-",
                     ",".join(result.warning_codes()) or "-",
                     hashlib.sha256(full.encode()).hexdigest()[:16]])


def compute() -> dict:
    return {case_id: outcome(kwargs) for case_id, kwargs in cases()}


if __name__ == "__main__":
    GOLDEN.parent.mkdir(exist_ok=True)
    data = compute()
    GOLDEN.write_text(json.dumps(data, sort_keys=True, indent=0) + "\n", encoding="utf-8")
    print(f"wrote {len(data)} cases to {GOLDEN}")
