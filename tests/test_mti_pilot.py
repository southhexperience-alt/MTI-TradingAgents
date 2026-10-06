"""MTI Swedish guarded pilot: benchmark, risk guard, modes, ledger and manual workflow."""

from __future__ import annotations

import ast
import copy
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from mti_pilot import ledger as L
from mti_pilot.config import (
    MICRO_LIVE,
    SHADOW,
    PilotConfig,
    PilotConfigError,
    parse_config,
    tradingagents_config,
)
from mti_pilot.guard import BLOCKED, TRADEABLE, BookView, OpenPosition, ReferencePrice, check
from mti_pilot.proposal import ExtractedDecision, extract_decision, parse_trader_plan
from mti_pilot.workflow import (
    ACTIVATION_PHRASE,
    REOPEN_PHRASE,
    STOCKHOLM,
    MicroLiveBlocked,
    Pilot,
    WorkflowError,
)
from tests.test_graph_end_to_end import offline  # noqa: F401  (upstream's offline vendors)
from tradingagents.agents import schemas
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory.settlement import resolve_benchmark
from tradingagents.portfolio import PortfolioContext, Position

pytestmark = pytest.mark.unit

TICKER = "VOLV-B.ST"
TODAY = datetime.now(STOCKHOLM).date().isoformat()
REF = ReferencePrice(100.5, TODAY, "test prices")
LIMITS = {"pilot_capital": 10_000.0, "max_risk_per_trade": 100.0, "max_pilot_loss": 300.0}


def _decision(**changes) -> ExtractedDecision:
    base = {"final_rating": "Buy", "pm_direction": "BUY", "pm_structured": True,
            "price_target": 110.0, "time_horizon": "1-3 weeks", "trader_structured": True,
            "trader_action": "BUY", "entry_price": 100.0, "stop_loss": 95.0,
            "position_sizing_text": None, "rationale": "Trend and earnings support a buy."}
    base.update(changes)
    return ExtractedDecision(**base)


def _config(tmp_path, mode=SHADOW, **amounts) -> PilotConfig:
    return PilotConfig(mode=mode, data_dir=tmp_path / "pilot", **amounts)


def _check(config, decision=None, mode=None, book=None, portfolio=None, reference=REF,
           ticker=TICKER):
    return check(ticker=ticker, analysis_date=TODAY, decision=decision or _decision(),
                 reference=reference, config=config, mode=mode or config.mode,
                 book=book or BookView(), portfolio=portfolio)


def _live_pilot(tmp_path, **amounts) -> Pilot:
    pilot = Pilot(_config(tmp_path, MICRO_LIVE, **{**LIMITS, **amounts}))
    pilot.activate_micro_live(ACTIVATION_PHRASE)
    return pilot


_EMPTY_BOOK = PortfolioContext()


def _record(pilot: Pilot, decision=None, portfolio=_EMPTY_BOOK, ticker=TICKER,
            reference=REF, historical=False) -> str:
    """Record a proposal and its guard result as the runner does, without a model."""
    decision = decision or _decision()
    run_id = f"{TODAY}_{ticker}_{len(pilot.run_ids()):08d}"
    pilot.record_proposal({
        "run_id": run_id, "created_on": pilot._today(), "ticker": ticker,
        "analysis_date": TODAY, "is_historical": historical, "mode": pilot.run_mode(),
        "benchmark": resolve_benchmark(ticker, DEFAULT_CONFIG), "decision": decision.to_dict(),
    })
    result = check(ticker=ticker, analysis_date=TODAY, decision=decision, reference=reference,
                   config=pilot.config, mode=pilot.run_mode(), book=pilot.book(),
                   portfolio=portfolio)
    pilot.record_risk_check(run_id, result)
    return run_id


def _now() -> str:
    return datetime.now(STOCKHOLM).isoformat()


# 1. Swedish benchmark -------------------------------------------------------------------

def test_a_stockholm_ticker_is_measured_against_omxspi():
    for ticker in ("VOLV-B.ST", "ERIC-B.ST", "INVE-B.ST", "ATCO-A.ST", "SEB-A.ST", "volv-b.st"):
        assert resolve_benchmark(ticker, DEFAULT_CONFIG) == "^OMXSPI"
    # The new suffix does not capture its neighbours.
    assert resolve_benchmark("7203.T", DEFAULT_CONFIG) == "^N225"
    assert resolve_benchmark("NOVN.SW", DEFAULT_CONFIG) == "^SSMI"
    assert resolve_benchmark("AAPL", DEFAULT_CONFIG) == "SPY"


def test_the_pilot_keeps_upstream_config_and_its_own_files(tmp_path):
    cfg = tradingagents_config(_config(tmp_path), base=copy.deepcopy(DEFAULT_CONFIG))
    assert cfg["benchmark_map"][".ST"] == "^OMXSPI"
    assert cfg["memory_log_path"].startswith(str(tmp_path))
    assert set(DEFAULT_CONFIG["global_news_queries"]) < set(cfg["global_news_queries"])


# 2. A Swedish ticker through the whole TradingAgents graph --------------------------------

@pytest.fixture
def scripted_graph(monkeypatch, request):
    """Upstream's offline, scripted end-to-end graph, built the way the pilot builds it."""
    request.getfixturevalue("offline")
    from tests import test_graph_end_to_end as e2e
    from tradingagents.graph import trading_graph

    def factory(structured):
        model = e2e.ScriptedModel(structured=structured)
        monkeypatch.setattr(trading_graph, "create_tier_client",
                            lambda config, tier, **k: e2e._Client(model))
        return lambda analysts, config: trading_graph.TradingAgentsGraph(analysts, config=config)
    return factory


def _ta_config(pilot):
    cfg = tradingagents_config(pilot.config, base=copy.deepcopy(DEFAULT_CONFIG))
    cfg["data_cache_dir"] = str(Path(pilot.config.data_dir) / "cache")
    return cfg


def test_a_swedish_ticker_goes_through_the_analysis_and_is_recorded(tmp_path, monkeypatch,
                                                                   scripted_graph):
    from mti_pilot.runner import run_analysis
    from tests import test_graph_end_to_end as e2e

    monkeypatch.setitem(e2e.STRUCTURED, schemas.TraderProposal, schemas.TraderProposal(
        action=schemas.TraderAction.BUY, reasoning="Breakout above resistance.",
        entry_price=100.0, stop_loss=95.0))
    pilot = Pilot(_config(tmp_path))
    run_id, record, result = run_analysis(
        pilot, "volv-b.st", e2e.TRADE_DATE, ta_config=_ta_config(pilot),
        graph_factory=scripted_graph(structured=True),
        reference_fn=lambda t, d, c: ReferencePrice(100.5, d, "test prices"))

    assert record["ticker"] == TICKER and record["benchmark"] == "^OMXSPI"
    assert record["decision"]["final_rating"] == "Overweight"
    assert record["decision"]["entry_price"] == 100.0 and record["decision"]["stop_loss"] == 95.0
    assert record["versions"]["upstream_commit"].startswith("1394a3f")
    assert record["is_historical"] is True
    assert result.status == TRADEABLE, result.blockers
    assert [e["type"] for e in pilot.ledger.events()] == [L.PROPOSED, L.RISK_CHECKED]
    assert Path(record["state_file"]).is_file()
    # Upstream's own memory log holds the decision, to settle it against ^OMXSPI.
    from tradingagents.memory import TradingMemoryLog
    entries = TradingMemoryLog({"memory_log_path": _ta_config(pilot)["memory_log_path"]}).load_entries()
    assert [(e["ticker"], e["rating"]) for e in entries] == [(TICKER, "Overweight")]


def test_a_free_text_trader_answer_is_never_mined_for_prices(tmp_path, scripted_graph):
    from mti_pilot.runner import run_analysis
    from tests import test_graph_end_to_end as e2e

    pilot = Pilot(_config(tmp_path))
    _, record, result = run_analysis(
        pilot, TICKER, e2e.TRADE_DATE, ta_config=_ta_config(pilot),
        graph_factory=scripted_graph(structured=False),
        reference_fn=lambda t, d, c: ReferencePrice(100.5, d, "test prices"))
    assert result.status == BLOCKED
    assert "TRADER_FIELDS_NOT_STRUCTURED" in result.blocker_codes()
    assert record["decision"]["entry_price"] is None


def test_typed_fields_are_read_only_from_upstreams_exact_rendering():
    proposal = schemas.TraderProposal(action="Buy", reasoning="r\n\nmulti", entry_price=189.5,
                                      stop_loss=172.0, position_sizing="2% of book")
    text = schemas.render_trader_proposal(proposal)
    assert parse_trader_plan(text) == proposal
    assert parse_trader_plan(text.replace("189.5", "about 189")) is None
    assert parse_trader_plan("Buy at 189.5 with a stop at 172.") is None
    state = {"final_rating": "Buy", "trader_investment_plan": text,
             "final_trade_decision": schemas.render_pm_decision(schemas.PortfolioDecision(
                 rating="Sell", executive_summary="s", investment_thesis="t"))}
    # A PM rendering naming another rating than the run's is not the decision made.
    assert extract_decision(state).pm_structured is False


# 3-5. No trade without defined, consistent risk ------------------------------------------

def test_a_buy_without_a_stop_is_blocked(tmp_path):
    result = _check(_config(tmp_path), _decision(stop_loss=None))
    assert result.status == BLOCKED and "MISSING_STOP" in result.blocker_codes()


def test_a_buy_without_an_entry_is_blocked(tmp_path):
    result = _check(_config(tmp_path), _decision(entry_price=None))
    assert result.status == BLOCKED and "MISSING_ENTRY" in result.blocker_codes()


def test_a_live_buy_without_a_risk_amount_is_blocked(tmp_path):
    cfg = _config(tmp_path, MICRO_LIVE, pilot_capital=10_000.0, max_pilot_loss=300.0)
    result = _check(cfg, portfolio=PortfolioContext())
    assert result.status == BLOCKED
    assert {"MISSING_MAX_RISK", "MICRO_LIVE_NOT_CONFIGURED"} <= set(result.blocker_codes())


@pytest.mark.parametrize("changes, code", [
    ({"stop_loss": 100.0}, "STOP_NOT_BELOW_ENTRY"),
    ({"stop_loss": 104.0}, "STOP_NOT_BELOW_ENTRY"),
    ({"price_target": 99.0}, "TARGET_NOT_ABOVE_ENTRY"),
    ({"entry_price": 120.0, "stop_loss": 101.0}, "STOP_ALREADY_BREACHED"),
    ({"entry_price": -5.0}, "INVALID_ENTRY"),
    ({"stop_loss": float("nan")}, "INVALID_STOP"),
    ({"trader_action": "SELL"}, "DIRECTION_CONFLICT"),
    ({"rationale": "  "}, "MISSING_RATIONALE"),
], ids=["stop-at-entry", "stop-above-entry", "target-below-entry", "stop-above-market",
        "negative-entry", "nan-stop", "trader-vs-pm", "no-rationale"])
def test_contradictory_values_are_blocked(tmp_path, changes, code):
    result = _check(_config(tmp_path), _decision(**changes))
    assert result.status == BLOCKED and code in result.blocker_codes()


def test_missing_market_data_or_its_source_is_blocked(tmp_path):
    cfg = _config(tmp_path)
    assert "MISSING_MARKET_DATA" in _check(cfg, reference=ReferencePrice(None, None, "x")).blocker_codes()
    assert "MISSING_MARKET_DATA_SOURCE" in _check(cfg, reference=ReferencePrice(100.5, TODAY, None)).blocker_codes()
    later = ReferencePrice(100.5, "2999-01-01", "x")
    assert "LOOK_AHEAD_MARKET_DATA" in _check(cfg, reference=later).blocker_codes()


def test_hold_sell_and_foreign_tickers_are_never_tradeable(tmp_path):
    cfg = _config(tmp_path)
    assert "NO_ACTIONABLE_DIRECTION" in _check(cfg, _decision(
        final_rating="Hold", pm_direction="HOLD", trader_action="HOLD")).blocker_codes()
    sell = _check(cfg, _decision(final_rating="Sell", pm_direction="SELL", trader_action="SELL"),
                  portfolio=PortfolioContext(positions=[Position(ticker=TICKER, quantity=5, average_price=90)]))
    assert "SELL_NOT_SUPPORTED" in sell.blocker_codes()
    assert "EXIT_SIGNAL_ON_OPEN_POSITION" in sell.warning_codes()
    assert "INSTRUMENT_NOT_ALLOWED" in _check(cfg, ticker="AAPL").blocker_codes()
    assert "RATING_UNREADABLE" in _check(cfg, _decision(final_rating="REVIEW", pm_direction=None)).blocker_codes()


# 6. No averaging down -------------------------------------------------------------------

def test_adding_to_a_losing_position_is_denied(tmp_path):
    cfg = _config(tmp_path)
    losing = PortfolioContext(positions=[Position(ticker=TICKER, quantity=10, average_price=120.0)])
    result = _check(cfg, portfolio=losing)
    assert result.status == BLOCKED and "AVERAGING_DOWN_DENIED" in result.blocker_codes()

    ledger_held = BookView(open_positions=(OpenPosition("r", TICKER, 10, 130.0, 110.0),))
    assert "AVERAGING_DOWN_DENIED" in _check(cfg, book=ledger_held).blocker_codes()

    unknown = PortfolioContext(positions=[Position(ticker=TICKER, quantity=10)])
    assert "POSITION_COST_UNKNOWN" in _check(cfg, portfolio=unknown).blocker_codes()


def test_adding_to_a_winning_position_is_flagged_not_denied(tmp_path):
    winning = PortfolioContext(positions=[Position(ticker=TICKER, quantity=10, average_price=80.0)])
    result = _check(_config(tmp_path), portfolio=winning)
    assert result.status == TRADEABLE
    assert result.warning_codes() == ["INCREASES_EXISTING_EXPOSURE"]


def test_live_needs_the_book_to_rule_out_averaging_down(tmp_path):
    cfg = _config(tmp_path, MICRO_LIVE, **LIMITS)
    assert "PORTFOLIO_CONTEXT_MISSING" in _check(cfg, portfolio=None).blocker_codes()
    assert _check(cfg, portfolio=PortfolioContext()).status == TRADEABLE


# 7-8. The pilot loss cap ----------------------------------------------------------------

def test_reaching_max_pilot_loss_halts_the_pilot(tmp_path):
    pilot = _live_pilot(tmp_path)
    for exit_price in (79.0, 79.0):   # 20 units x 5 risk = 100 risk; each loses ~420
        run_id = _record(pilot)
        if pilot.status(run_id) != TRADEABLE:
            break
        pilot.approve(run_id)
        pilot.record_execution(run_id, price=100.0, quantity=20, courtage=1.0, executed_at=_now())
        pilot.record_close(run_id, price=exit_price, quantity=20, courtage=1.0, executed_at=_now())
    book = pilot.book()
    assert book.realized_loss >= LIMITS["max_pilot_loss"] and book.halted
    assert [e["type"] for e in pilot.ledger.events()].count(L.PILOT_HALTED) == 1


def test_a_halted_pilot_makes_nothing_tradeable_until_reopened(tmp_path):
    pilot = _live_pilot(tmp_path)
    run_id = _record(pilot)
    pilot.approve(run_id)
    pilot.record_execution(run_id, price=100.0, quantity=20, courtage=1.0, executed_at=_now())
    pending = _record(pilot)     # TRADEABLE before the loss lands
    pilot.record_close(run_id, price=80.0, quantity=20, courtage=1.0, executed_at=_now())

    after = _record(pilot)
    assert pilot.status(after) == BLOCKED
    assert "PILOT_HALTED" in pilot.risk_check(after)["blockers"][0]["code"]
    with pytest.raises(WorkflowError, match="halted"):
        pilot.approve(pending)
    with pytest.raises(WorkflowError, match="raised MAX_PILOT_LOSS"):
        pilot.reopen(REOPEN_PHRASE, "reviewed")

    # Only a deliberately raised cap, a fresh activation and the phrase reopen it.
    raised = Pilot(_config(tmp_path, MICRO_LIVE, **{**LIMITS, "max_pilot_loss": 1_000.0}))
    with pytest.raises(WorkflowError, match="exact phrase"):
        raised.reopen("yes", "reviewed")
    raised.reopen(REOPEN_PHRASE, "reviewed the losses, cap raised")
    with pytest.raises(MicroLiveBlocked, match="not been activated"):
        raised.run_mode()
    raised.activate_micro_live(ACTIVATION_PHRASE)
    assert raised.status(_record(raised)) == TRADEABLE


def test_a_trade_that_could_pass_the_cap_is_blocked(tmp_path):
    pilot = _live_pilot(tmp_path, max_pilot_loss=150.0)
    first = _record(pilot)
    pilot.approve(first)
    pilot.record_execution(first, price=100.0, quantity=20, courtage=0.0, executed_at=_now())
    second = _record(pilot, ticker="ERIC-B.ST")
    assert "WOULD_EXCEED_PILOT_LOSS_CAP" in [b["code"] for b in pilot.risk_check(second)["blockers"]]


# 9-10. Modes -----------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["pilot_capital", "max_risk_per_trade", "max_pilot_loss"])
def test_micro_live_is_blocked_while_a_limit_is_missing(tmp_path, missing):
    amounts = {k: v for k, v in LIMITS.items() if k != missing}
    pilot = Pilot(_config(tmp_path, MICRO_LIVE, **amounts))
    with pytest.raises(MicroLiveBlocked, match=missing.upper()):
        pilot.run_mode()
    with pytest.raises(MicroLiveBlocked):
        pilot.activate_micro_live(ACTIVATION_PHRASE)

    from mti_pilot.runner import run_analysis

    def no_graph(*a, **k):
        raise AssertionError("no model may run while MICRO_LIVE is blocked")
    with pytest.raises(MicroLiveBlocked):
        run_analysis(pilot, TICKER, TODAY, graph_factory=no_graph)


def test_micro_live_needs_mats_activation_for_the_configured_limits(tmp_path):
    pilot = Pilot(_config(tmp_path, MICRO_LIVE, **LIMITS))
    with pytest.raises(MicroLiveBlocked, match="not been activated"):
        pilot.run_mode()
    with pytest.raises(WorkflowError, match="exact phrase"):
        pilot.activate_micro_live("ok")
    pilot.activate_micro_live(ACTIVATION_PHRASE)
    assert pilot.run_mode() == MICRO_LIVE
    changed = Pilot(_config(tmp_path, MICRO_LIVE, **{**LIMITS, "max_risk_per_trade": 200.0}))
    with pytest.raises(MicroLiveBlocked):
        changed.run_mode()


def test_shadow_runs_without_any_amount(tmp_path):
    pilot = Pilot(_config(tmp_path))
    assert pilot.run_mode() == SHADOW
    run_id = _record(pilot, portfolio=None)
    check = pilot.risk_check(run_id)
    assert check["status"] == TRADEABLE and check["mode"] == SHADOW
    assert check["order"]["quantity"] is None and check["order"]["risk_per_unit"] == 5.0
    with pytest.raises(WorkflowError, match="SHADOW proposal"):
        pilot.approve(run_id)


def test_config_amounts_have_no_defaults_and_typos_are_errors(tmp_path):
    cfg = parse_config({})
    assert cfg.mode == SHADOW and cfg.limits()["max_pilot_loss"] is None
    with pytest.raises(PilotConfigError, match="unknown"):
        parse_config({"MAX_PILOT_LOS": 100})
    with pytest.raises(PilotConfigError, match="positive"):
        parse_config({"MAX_PILOT_LOSS": 0})
    cfg = parse_config({"MODE": "MICRO_LIVE", "PILOT_CAPITAL": 1000, "MAX_RISK_PER_TRADE": 500,
                        "MAX_PILOT_LOSS": 200})
    assert any("one trade could pass the cap" in b for b in cfg.micro_live_config_blockers())


# 11. Approval is not execution ------------------------------------------------------------

def test_approval_does_not_record_an_execution(tmp_path):
    pilot = _live_pilot(tmp_path)
    run_id = _record(pilot)
    pilot.approve(run_id, "looks right")
    assert pilot.status(run_id) == L.USER_APPROVED
    assert L.MANUALLY_EXECUTED not in [e["type"] for e in pilot.ledger.events()]
    assert pilot.book().open_positions == ()
    with pytest.raises(WorkflowError, match="only an executed trade"):
        pilot.record_close(run_id, price=101, quantity=20, courtage=1, executed_at=_now())

    event = pilot.record_execution(run_id, price=100.2, quantity=20, courtage=1.0, slippage=0.5,
                                   executed_at=_now(), comment="limit order filled")
    assert event["payload"]["price"] == 100.2 and pilot.status(run_id) == L.MANUALLY_EXECUTED
    assert pilot.book().open_positions[0].quantity == 20


def test_only_todays_live_proposals_can_be_approved(tmp_path):
    pilot = _live_pilot(tmp_path)
    old = _record(pilot, historical=True)
    with pytest.raises(WorkflowError, match="made today"):
        pilot.approve(old)
    tomorrow = Pilot(pilot.config, pilot.ledger, today=lambda: "2999-01-01")
    with pytest.raises(WorkflowError, match="made today"):
        tomorrow.approve(_record(pilot))


# 12. No broker or order path ---------------------------------------------------------------

_PILOT_DIR = Path(__file__).resolve().parents[1] / "mti_pilot"
_NETWORK_MODULES = {"requests", "httpx", "urllib", "urllib3", "http", "socket", "aiohttp",
                    "websocket", "websockets", "selenium", "playwright", "yfinance", "curl_cffi"}
_BROKER_WORDS = ("avanza", "bankid", "broker", "place_order", "submit_order", "cancel_order",
                 "modify_order", "send_order")


def test_the_pilot_has_no_network_or_broker_code():
    for path in _PILOT_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported.add(node.module.split(".")[0])
            if isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name.lower())
        assert not imported & _NETWORK_MODULES, (path.name, imported & _NETWORK_MODULES)
        assert not [n for n in names for w in _BROKER_WORDS if w in n], path.name


def test_the_whole_live_path_runs_with_the_network_refused(tmp_path):
    """conftest refuses every socket; a broker call anywhere in the path would fail here."""
    pilot = _live_pilot(tmp_path)
    run_id = _record(pilot)
    pilot.approve(run_id)
    pilot.record_execution(run_id, price=100.0, quantity=20, courtage=1.0, executed_at=_now())
    pilot.record_close(run_id, price=104.0, quantity=20, courtage=1.0, executed_at=_now())
    assert pilot.status(run_id) == L.CLOSED


# 13. History is never rewritten ------------------------------------------------------------

def test_a_recorded_recommendation_cannot_be_overwritten(tmp_path):
    pilot = Pilot(_config(tmp_path))
    run_id = _record(pilot)
    with pytest.raises(L.LedgerError, match="already recorded"):
        pilot.record_proposal({"run_id": run_id, "decision": {"final_rating": "Sell"}})
    target = tmp_path / "state.json"
    L.write_once(target, {"a": 1})
    with pytest.raises(L.LedgerError, match="never rewritten"):
        L.write_once(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 1}

    proposed = pilot.ledger.events()[0]
    with pytest.raises(WorkflowError, match="cannot be corrected"):
        pilot.correct(proposed["event_id"], {"price": 1.0}, "try")
    assert not hasattr(pilot.ledger, "update") and not hasattr(pilot.ledger, "delete")


def test_a_tampered_ledger_is_detected_and_refuses_new_writes(tmp_path):
    pilot = Pilot(_config(tmp_path))
    _record(pilot)
    path = pilot.ledger.path
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["payload"]["decision"]["final_rating"] = "Sell"
    path.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n", encoding="utf-8")
    assert pilot.ledger.verify()
    with pytest.raises(L.LedgerError, match="chain is broken"):
        _record(pilot)


def test_a_correction_is_appended_and_the_original_kept(tmp_path):
    pilot = _live_pilot(tmp_path)
    run_id = _record(pilot)
    pilot.approve(run_id)
    executed = pilot.record_execution(run_id, price=100.0, quantity=20, courtage=1.0,
                                      executed_at=_now())
    before = pilot.ledger.path.read_text(encoding="utf-8")
    pilot.correct(executed["event_id"], {"courtage": 39.0}, "Avanza note showed 39 kr")
    after = pilot.ledger.path.read_text(encoding="utf-8")
    assert after.startswith(before) and not pilot.ledger.verify()
    assert pilot.ledger.run_events(run_id)[-2]["payload"]["courtage"] == 1.0
    pilot.record_close(run_id, price=110.0, quantity=20, courtage=1.0, executed_at=_now())
    assert pilot.ledger.run_events(run_id)[-1]["payload"]["result"]["costs"] == 40.0


# 14. Results are measured against the Swedish benchmark ------------------------------------

def test_a_closed_trade_is_evaluated_net_of_costs_against_omxspi(tmp_path):
    pilot = _live_pilot(tmp_path)
    run_id = _record(pilot)
    pilot.approve(run_id)
    pilot.record_execution(run_id, price=100.0, quantity=20, courtage=1.0, slippage=1.0,
                           executed_at=_now())
    pilot.record_close(run_id, price=110.0, quantity=20, courtage=1.0, slippage=1.0,
                       executed_at=_now())
    asked = []

    def closes(symbol, start, end):
        asked.append(symbol)
        day = pd.Timestamp(TODAY, tz="Europe/Stockholm")
        return pd.Series([1000.0, 1010.0], index=[day - pd.Timedelta(days=1), day])

    payload = pilot.evaluate(run_id, get_closes=closes)["payload"]
    assert asked == ["^OMXSPI"] and payload["benchmark"] == "^OMXSPI"
    assert payload["net_pnl"] == 196.0                       # 200 gross - 4 costs
    assert payload["benchmark_return"] == pytest.approx(0.0)  # same-day fills
    assert payload["alpha_net"] == pytest.approx(196.0 / 2000.0)
    assert pilot.status(run_id) == L.EVALUATED
    with pytest.raises(WorkflowError, match="already evaluated"):
        pilot.evaluate(run_id, get_closes=closes)


def test_a_shadow_proposal_is_evaluated_with_upstream_settlement_against_omxspi(tmp_path):
    pilot = Pilot(_config(tmp_path))
    run_id = _record(pilot)
    seen = {}

    def fetch_returns(ticker, date, days, benchmark):
        seen.update(ticker=ticker, benchmark=benchmark)
        return 0.02, 0.005, days, "2026-10-13"

    payload = pilot.evaluate(run_id, fetch_returns=fetch_returns)["payload"]
    assert seen == {"ticker": TICKER, "benchmark": "^OMXSPI"}
    assert payload["kind"] == "shadow" and payload["alpha_return"] == 0.005
    with pytest.raises(WorkflowError, match="not traded yet"):
        pilot.evaluate(_record(pilot), fetch_returns=lambda *a, **k: (None, None, None, None))


# Live data, run only on request ------------------------------------------------------------

@pytest.mark.integration
def test_yahoo_serves_swedish_prices_and_omxspi_point_in_time():
    from tradingagents.dataflows.vendors.yahoo.market import get_closes
    from tradingagents.memory.settlement import fetch_returns

    for symbol in ("VOLV-B.ST", "ERIC-B.ST", "INVE-B.ST", "ATCO-A.ST", "SEB-A.ST", "^OMXSPI"):
        assert len(get_closes(symbol, "2026-09-01", "2026-09-15")) > 5, symbol
    raw, alpha, days, resolved = fetch_returns("VOLV-B.ST", "2026-09-01", 5, benchmark="^OMXSPI")
    assert days == 5 and resolved > "2026-09-01" and alpha is not None
