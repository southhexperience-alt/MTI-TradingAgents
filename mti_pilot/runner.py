"""One pilot analysis: run TradingAgents unchanged, record what it said, then guard it.

The recommendation is written to the ledger before the guard runs and before
any outcome can be known. The full run state is written once to its own file,
whose hash the ledger keeps, so the ledger proves what the system said then.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import tradingagents
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.memory.settlement import resolve_benchmark
from tradingagents.portfolio import PortfolioContext

from . import MTI_VERSION, UPSTREAM_COMMIT, UPSTREAM_REPO, UPSTREAM_VERSION
from .config import tradingagents_config
from .guard import GuardResult, ReferencePrice, check
from .ledger import write_once
from .measurement import benchmark_basis
from .proposal import extract_decision
from .workflow import Pilot, WorkflowError

DEFAULT_ANALYSTS = ("market", "social", "news", "fundamentals")
REFERENCE_SOURCE = ("yfinance Ticker.history (auto_adjust=True) via upstream load_ohlcv; "
                    "last unfilled row on or before the analysis date")
_STATE_KEYS = ("company_of_interest", "trade_date", "instrument_context", "portfolio_context",
               "market_report", "sentiment_report", "news_report", "fundamentals_report",
               "investment_plan", "trader_investment_plan", "final_trade_decision", "final_rating",
               "past_context", "memory_note")


def reference_price(ticker: str, analysis_date: str, ta_config: dict) -> ReferencePrice:
    """The last close the analysis could have seen, from upstream's point-in-time loader."""
    import pandas as pd

    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    try:
        with run_config(ta_config):
            rows = load_ohlcv(ticker, analysis_date, fill_gaps=False)
    except (NoMarketDataError, VendorUnavailableError, ValueError):
        return ReferencePrice(None, None, None)
    if rows is None or rows.empty:
        return ReferencePrice(None, None, None)
    rows = rows.assign(Date=pd.to_datetime(rows["Date"], errors="coerce"))
    rows = rows.dropna(subset=["Date", "Close"])
    rows = rows[rows["Date"] <= pd.to_datetime(analysis_date)].sort_values("Date")
    if rows.empty:
        return ReferencePrice(None, None, None)
    last = rows.iloc[-1]
    return ReferencePrice(float(last["Close"]), last["Date"].strftime("%Y-%m-%d"), REFERENCE_SOURCE)


def code_commit() -> str | None:
    """The fork's own commit the run used, when it runs from a git checkout."""
    try:
        root = Path(__file__).resolve().parents[1]
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5, check=True)
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5, check=True)
        return out.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None


def run_analysis(
    pilot: Pilot,
    ticker: str,
    analysis_date: str,
    *,
    selected_analysts=DEFAULT_ANALYSTS,
    portfolio: PortfolioContext | None = None,
    ta_config: dict | None = None,
    graph_factory=None,
    reference_fn=reference_price,
) -> tuple[str, dict, GuardResult]:
    """Analyse ``ticker`` as of ``analysis_date`` and record proposal and guard result.

    Returns ``(run_id, record, guard_result)``. A MICRO_LIVE that may not run
    raises before any model is called.
    """
    mode = pilot.run_mode()
    ticker = normalize_symbol(ticker)
    if not pilot.config.instrument_allowed(ticker):
        raise WorkflowError(f"{ticker} is outside the pilot's markets "
                            f"({', '.join(pilot.config.allowed_suffixes)})")
    if graph_factory is None:
        from tradingagents.graph.trading_graph import TradingAgentsGraph as graph_factory
    ta_config = ta_config or tradingagents_config(pilot.config)
    pilot.check_halt()

    graph = graph_factory(list(selected_analysts), config=ta_config)
    final_state, signal = graph.propagate(ticker, analysis_date, portfolio=portfolio)
    created = datetime.now(UTC)
    run_id = f"{analysis_date}_{ticker}_{uuid.uuid4().hex[:8]}"

    reference = reference_fn(ticker, analysis_date, ta_config)
    decision = extract_decision(final_state)
    try:
        report_dir = str(graph.save_reports(final_state, ticker))
    except Exception as exc:  # reports are for reading; the ledger is the record
        report_dir = f"not written: {type(exc).__name__}: {exc}"

    benchmark = resolve_benchmark(ticker, ta_config)
    data_dir = Path(pilot.config.data_dir)
    state = {k: final_state.get(k) for k in _STATE_KEYS}
    state["investment_debate_history"] = (final_state.get("investment_debate_state") or {}).get("history")
    state["risk_debate_history"] = (final_state.get("risk_debate_state") or {}).get("history")
    state_file = data_dir / "runs" / run_id / "state.json"
    state_sha = write_once(state_file, state)

    record = {
        "run_id": run_id,
        "created_at": created.isoformat(timespec="seconds"),
        "created_on": pilot._today(),
        "ticker": ticker,
        "analysis_date": analysis_date,
        "is_historical": is_historical(analysis_date),
        "mode": mode,
        "versions": {"tradingagents": tradingagents.__version__, "upstream_repo": UPSTREAM_REPO,
                     "upstream_version": UPSTREAM_VERSION, "upstream_commit": UPSTREAM_COMMIT,
                     "mti": MTI_VERSION, "code_commit": code_commit()},
        "run_settings": graph.run_settings(),
        # Recorded per run, so a later change of benchmark stays traceable.
        "benchmark": benchmark,
        "benchmark_basis": benchmark_basis(benchmark),
        "holding_period_days": ta_config.get("holding_period_days"),
        "market_data": {"reference_price": reference.price, "reference_date": reference.date,
                        "source": reference.source,
                        "vendors": dict(ta_config.get("data_vendors") or {})},
        "signal": signal,
        "decision": decision.to_dict(),
        "trader_proposal_text": final_state.get("trader_investment_plan", ""),
        "final_decision_text": final_state.get("final_trade_decision", ""),
        "portfolio": portfolio.model_dump() if portfolio is not None else None,
        "state_file": str(state_file),
        "state_sha256": state_sha,
        "report_dir": report_dir,
    }
    pilot.record_proposal(record)

    result = check(ticker=ticker, analysis_date=analysis_date, decision=decision,
                   reference=reference, config=pilot.config, mode=mode, book=pilot.book(),
                   portfolio=portfolio)
    pilot.record_risk_check(run_id, result)
    return run_id, record, result
