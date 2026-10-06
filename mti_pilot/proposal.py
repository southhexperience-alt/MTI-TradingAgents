"""Reading the machine-readable fields out of a finished TradingAgents run.

The Trader and the Portfolio Manager answer in typed schemas upstream
(``TraderProposal``, ``PortfolioDecision``), which are rendered to markdown
before they reach the run's state. Their numbers are taken back only when the
text is exactly what upstream's renderer produces for some schema instance:
the text is parsed, rendered again, and must match byte for byte. When a model
fell back to free text, that check fails and the fields count as missing.
Prose is never mined for a price.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from pydantic import ValidationError

from tradingagents.agents.rating import RATING_REVIEW
from tradingagents.agents.schemas import (
    PortfolioDecision,
    TraderProposal,
    render_pm_decision,
    render_trader_proposal,
)

_TRADER_RE = re.compile(
    r"\A\*\*Action\*\*: (?P<action>Buy|Hold|Sell)\n\n"
    r"\*\*Reasoning\*\*: (?P<reasoning>.*)\n\n"
    r"\*\*Entry Price\*\*: (?P<entry>[^\n]*)\n\n"
    r"\*\*Stop Loss\*\*: (?P<stop>[^\n]*)\n\n"
    r"\*\*Position Sizing\*\*: (?P<sizing>.*)\n\n"
    r"FINAL TRANSACTION PROPOSAL: \*\*(?:BUY|HOLD|SELL)\*\*\Z",
    re.DOTALL,
)

_PM_RE = re.compile(
    r"\A\*\*Rating\*\*: (?P<rating>\w+)\n\n"
    r"\*\*Executive Summary\*\*: (?P<summary>.*?)\n\n"
    r"\*\*Investment Thesis\*\*: (?P<thesis>.*)\n\n"
    r"\*\*Price Target\*\*: (?P<target>[^\n]*)\n\n"
    r"\*\*Time Horizon\*\*: (?P<horizon>[^\n]*)\Z",
    re.DOTALL,
)

_NOT_PROVIDED = "not provided"

# What each final rating asks for. Overweight and Underweight lean a direction
# without being a hold, as upstream's Trader is told to read them.
DIRECTION_OF_RATING = {"Buy": "BUY", "Overweight": "BUY", "Hold": "HOLD",
                       "Underweight": "SELL", "Sell": "SELL"}


def _number(text: str) -> float | None:
    if text == _NOT_PROVIDED:
        return None
    return float(text)


def parse_trader_plan(text: str) -> TraderProposal | None:
    """The Trader's typed proposal, or None when the text is not its exact rendering."""
    match = _TRADER_RE.match(text or "")
    if not match:
        return None
    try:
        sizing = match["sizing"]
        proposal = TraderProposal(
            action=match["action"], reasoning=match["reasoning"],
            entry_price=_number(match["entry"]), stop_loss=_number(match["stop"]),
            position_sizing=None if sizing == _NOT_PROVIDED else sizing,
        )
    except (ValueError, ValidationError):
        return None
    return proposal if render_trader_proposal(proposal) == text else None


def parse_pm_decision(text: str) -> PortfolioDecision | None:
    """The Portfolio Manager's typed decision, or None when the text is not its exact rendering."""
    match = _PM_RE.match(text or "")
    if not match:
        return None
    try:
        horizon = match["horizon"]
        decision = PortfolioDecision(
            rating=match["rating"], executive_summary=match["summary"],
            investment_thesis=match["thesis"], price_target=_number(match["target"]),
            time_horizon=None if horizon == _NOT_PROVIDED else horizon,
        )
    except (ValueError, ValidationError):
        return None
    return decision if render_pm_decision(decision) == text else None


@dataclass(frozen=True)
class ExtractedDecision:
    """The fields the risk guard reads, and where each came from."""

    final_rating: str
    pm_direction: str | None
    pm_structured: bool
    price_target: float | None
    time_horizon: str | None
    trader_structured: bool
    trader_action: str | None
    entry_price: float | None
    stop_loss: float | None
    position_sizing_text: str | None
    rationale: str

    def to_dict(self) -> dict:
        return asdict(self)


def extract_decision(final_state: dict) -> ExtractedDecision:
    rating = final_state.get("final_rating") or RATING_REVIEW
    trader = parse_trader_plan(final_state.get("trader_investment_plan", ""))
    pm = parse_pm_decision(final_state.get("final_trade_decision", ""))
    # A PM decision whose rendering names another rating than the state's is
    # not the decision that was made.
    if pm is not None and pm.rating.value != rating:
        pm = None
    rationale_parts = [trader.reasoning.strip() if trader else "",
                       pm.investment_thesis.strip() if pm else ""]
    return ExtractedDecision(
        final_rating=rating,
        pm_direction=DIRECTION_OF_RATING.get(rating),
        pm_structured=pm is not None,
        price_target=pm.price_target if pm else None,
        time_horizon=pm.time_horizon if pm else None,
        trader_structured=trader is not None,
        trader_action=trader.action.value.upper() if trader else None,
        entry_price=trader.entry_price if trader else None,
        stop_loss=trader.stop_loss if trader else None,
        position_sizing_text=trader.position_sizing if trader else None,
        rationale="\n\n".join(p for p in rationale_parts if p),
    )
