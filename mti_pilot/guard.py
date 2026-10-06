"""MTI Risk Guard: decides, deterministically, whether a proposal may be traded.

It runs after TradingAgents has finished and changes nothing the agents wrote.
It reads the typed fields of the run (see ``proposal``), the market price the
run was made at, the caller's book, the pilot's ledger and configuration, and
returns TRADEABLE or BLOCKED with every reason. No LLM is consulted, so the
same inputs give the same answer.

Three rules cannot be configured away:

1. No averaging down: a BUY that adds to a position trading below its cost is
   blocked. There is no override.
2. No trade without defined risk: instrument, direction, entry, stop, maximum
   risk, rationale, analysis date and market-data source must all be present
   as fields, and must agree with each other.
3. The pilot loss cap: once realized loss reaches MAX_PILOT_LOSS the pilot is
   halted and nothing new is TRADEABLE until Mats reopens it. A trade whose
   risk, with the risk already open, could pass the cap is blocked as well.

Version 0.1 is long-only: a SELL is never a tradeable proposal. A sell signal
on a position the pilot holds is reported as a warning for Mats to weigh.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from tradingagents.portfolio import PortfolioContext

from .config import MICRO_LIVE, PilotConfig
from .proposal import ExtractedDecision

GUARD_VERSION = "0.1.0"
TRADEABLE = "TRADEABLE"
BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ReferencePrice:
    """The last close the analysis could see, and where it came from."""

    price: float | None
    date: str | None
    source: str | None


@dataclass(frozen=True)
class OpenPosition:
    run_id: str
    ticker: str
    quantity: float
    entry_price: float
    stop_loss: float | None

    @property
    def risk_amount(self) -> float:
        """Loss if the stop fills; an unknown stop counts the whole position at risk."""
        stop = self.stop_loss if self.stop_loss is not None else 0.0
        return max(0.0, (self.entry_price - stop) * self.quantity)

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity


@dataclass(frozen=True)
class BookView:
    """What the pilot's own ledger says: halt state, realized result, open trades."""

    halted: bool = False
    realized_pnl: float = 0.0
    open_positions: tuple[OpenPosition, ...] = ()

    @property
    def realized_loss(self) -> float:
        return max(0.0, -self.realized_pnl)

    @property
    def open_risk(self) -> float:
        return sum(p.risk_amount for p in self.open_positions)

    @property
    def committed_capital(self) -> float:
        return sum(p.notional for p in self.open_positions)


@dataclass
class Issue:
    code: str
    message: str


@dataclass
class GuardResult:
    status: str
    mode: str
    blockers: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    order: dict = field(default_factory=dict)
    guard_version: str = GUARD_VERSION

    @property
    def tradeable(self) -> bool:
        return self.status == TRADEABLE

    def blocker_codes(self) -> list[str]:
        return [b.code for b in self.blockers]

    def warning_codes(self) -> list[str]:
        return [w.code for w in self.warnings]

    def to_dict(self) -> dict:
        return asdict(self)


def _finite_positive(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def check(
    *,
    ticker: str,
    analysis_date: str | None,
    decision: ExtractedDecision,
    reference: ReferencePrice,
    config: PilotConfig,
    mode: str,
    book: BookView,
    portfolio: PortfolioContext | None,
) -> GuardResult:
    """Run every rule and collect every reason; the first failure does not hide the rest."""
    blockers: list[Issue] = []
    warnings: list[Issue] = []

    def block(code, message):
        blockers.append(Issue(code, message))

    def warn(code, message):
        warnings.append(Issue(code, message))

    ticker = (ticker or "").strip().upper()
    held = [p for p in book.open_positions if p.ticker == ticker]
    position = portfolio.position_in(ticker) if portfolio is not None else None

    # --- Rule 3: the pilot is halted -------------------------------------------------
    if book.halted:
        block("PILOT_HALTED", "the pilot is halted by its loss cap; Mats must reopen it")
    if mode == MICRO_LIVE:
        for reason in config.micro_live_config_blockers():
            block("MICRO_LIVE_NOT_CONFIGURED", reason)

    # --- Instrument and data ----------------------------------------------------------
    if not ticker:
        block("MISSING_INSTRUMENT", "no instrument")
    elif not config.instrument_allowed(ticker):
        block("INSTRUMENT_NOT_ALLOWED",
              f"{ticker} is outside the pilot's markets ({', '.join(config.allowed_suffixes)})")
    if not analysis_date:
        block("MISSING_ANALYSIS_DATE", "no analysis date")
    if not _finite_positive(reference.price) or not reference.date:
        block("MISSING_MARKET_DATA", "no market price on or before the analysis date")
    elif analysis_date and reference.date > analysis_date:
        block("LOOK_AHEAD_MARKET_DATA",
              f"reference price dated {reference.date} is after the analysis date {analysis_date}")
    if not reference.source:
        block("MISSING_MARKET_DATA_SOURCE", "the market data's source is not recorded")

    # --- Direction --------------------------------------------------------------------
    direction = decision.pm_direction
    if direction is None:
        block("RATING_UNREADABLE", f"the final rating {decision.final_rating!r} names no direction")
    if not decision.trader_structured:
        block("TRADER_FIELDS_NOT_STRUCTURED",
              "the Trader's proposal is not its typed output; entry and stop cannot be read as fields")
    elif direction is not None and decision.trader_action != direction:
        block("DIRECTION_CONFLICT",
              f"the Trader proposes {decision.trader_action} but the final rating "
              f"{decision.final_rating} means {direction}")
    if direction == "HOLD":
        block("NO_ACTIONABLE_DIRECTION", "the final rating is Hold: there is no trade to make")
    if direction == "SELL":
        block("SELL_NOT_SUPPORTED", "the v0.1 pilot is long-only; a SELL is not a tradeable proposal")
        if held or (position is not None and position.quantity > 0):
            warn("EXIT_SIGNAL_ON_OPEN_POSITION",
                 f"the analysis rates {ticker} {decision.final_rating} while a long position is open")

    if not decision.rationale.strip():
        block("MISSING_RATIONALE", "no rationale from the Trader or the Portfolio Manager")

    order: dict = {"instrument": ticker, "direction": direction, "currency": config.currency}

    if direction == "BUY":
        _check_buy(decision, reference, config, mode, book, portfolio, ticker, held, position,
                   order, block, warn)

    status = BLOCKED if blockers else TRADEABLE
    return GuardResult(status=status, mode=mode, blockers=blockers, warnings=warnings, order=order)


def _check_buy(decision, reference, config, mode, book, portfolio, ticker, held, position,
               order, block, warn):
    entry, stop, target = decision.entry_price, decision.stop_loss, decision.price_target
    order.update(entry_price=entry, stop_loss=stop, price_target=target,
                 reference_price=reference.price, reference_date=reference.date)

    # --- Rule 2: defined, consistent levels ---------------------------------------------
    if entry is None:
        block("MISSING_ENTRY", "no entry price")
    elif not _finite_positive(entry):
        block("INVALID_ENTRY", f"entry price {entry!r} is not a positive number")
    if stop is None:
        block("MISSING_STOP", "no stop-loss / invalidation level")
    elif not _finite_positive(stop):
        block("INVALID_STOP", f"stop-loss {stop!r} is not a positive number")
    levels_ok = _finite_positive(entry) and _finite_positive(stop)
    if levels_ok and stop >= entry:
        block("STOP_NOT_BELOW_ENTRY", f"a BUY's stop {stop} must be below its entry {entry}")
        levels_ok = False
    if target is not None and _finite_positive(entry) and not (
            _finite_positive(target) and target > entry):
        block("TARGET_NOT_ABOVE_ENTRY", f"a BUY's target {target} must be above its entry {entry}")
    if _finite_positive(stop) and _finite_positive(reference.price) and stop >= reference.price:
        block("STOP_ALREADY_BREACHED",
              f"the stop {stop} is at or above the last price {reference.price}")

    risk_per_unit = entry - stop if levels_ok else None
    order.update(risk_per_unit=risk_per_unit,
                 risk_pct_of_entry=risk_per_unit / entry if levels_ok else None)

    # --- Rule 1: no averaging down -------------------------------------------------------
    if mode == MICRO_LIVE and portfolio is None:
        block("PORTFOLIO_CONTEXT_MISSING",
              "MICRO_LIVE needs the current holdings (an empty book is fine) to rule out averaging down")
    costs = [(p.entry_price, p.quantity) for p in held]
    if position is not None and position.quantity < 0:
        block("SHORT_POSITION_UNSUPPORTED", f"the book is short {ticker}; the pilot is long-only")
    elif position is not None and position.quantity > 0:
        if position.average_price is None:
            block("POSITION_COST_UNKNOWN",
                  f"a position in {ticker} is held at an unknown cost; averaging down cannot be ruled out")
        else:
            costs.append((position.average_price, position.quantity))
    if costs:
        losing = [c for c, _ in costs if _finite_positive(reference.price) and reference.price < c]
        if losing or not _finite_positive(reference.price):
            block("AVERAGING_DOWN_DENIED",
                  f"{ticker} is held at a cost of {max(c for c, _ in costs)} and trades at "
                  f"{reference.price}: adding to a losing position is not allowed")
        else:
            warn("INCREASES_EXISTING_EXPOSURE", f"the BUY adds to an existing position in {ticker}")

    # --- Sizing: a defined maximum risk -------------------------------------------------
    max_risk = config.max_risk_per_trade
    if risk_per_unit is None:
        order.update(quantity=None, max_risk_amount=None, notional=None)
        return
    if max_risk is None:
        # SHADOW without amounts: risk is defined per unit and as a share of entry.
        order.update(quantity=None, max_risk_amount=None, notional=None,
                     max_risk_basis="per unit (no MAX_RISK_PER_TRADE configured)")
        if mode == MICRO_LIVE:
            block("MISSING_MAX_RISK", "MAX_RISK_PER_TRADE is not configured")
        return

    quantity = math.floor(max_risk / risk_per_unit)
    if config.pilot_capital is not None:
        free = config.pilot_capital - book.committed_capital
        quantity = min(quantity, math.floor(max(free, 0.0) / entry))
    risk_amount = round(quantity * risk_per_unit, 2)
    order.update(quantity=quantity, max_risk_amount=risk_amount,
                 notional=round(quantity * entry, 2),
                 max_risk_basis=f"MAX_RISK_PER_TRADE {max_risk} {config.currency}")
    if quantity < 1:
        block("POSITION_TOO_SMALL",
              "not one unit fits within the risk limit and the free pilot capital")
        return
    if config.max_pilot_loss is not None:
        exposure = book.realized_loss + book.open_risk + risk_amount
        if exposure > config.max_pilot_loss:
            block("WOULD_EXCEED_PILOT_LOSS_CAP",
                  f"realized loss {book.realized_loss:.2f} + open risk {book.open_risk:.2f} + "
                  f"this trade's risk {risk_amount:.2f} exceeds MAX_PILOT_LOSS {config.max_pilot_loss}")
