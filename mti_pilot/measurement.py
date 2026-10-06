"""Measuring what the proposals were worth: hypothetically for all, actually for trades.

Every PROPOSED run gets a hypothetical outcome, whatever the guard said and
whether or not Mats acted: from the close on the reference date the analysis
saw, over the run's holding horizon in the stock's own sessions, against the
benchmark over the same dates. Executed trades get their actual result, net of
costs, separately. The groups are reported side by side and never averaged
together, and runs dated before they were made are kept out of the headline
figures altogether (see ``build_report``).

Both legs are total return: the stock's closes come from upstream's
``get_closes`` (Yahoo, ``auto_adjust=True``, so dividends are reinvested) and
``^OMXSBGI`` is a gross index. Both are read from one fetch made at evaluation
time, so a dividend paid inside the window is in the return; the reference
price recorded at analysis time is kept beside it for comparison only.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LEGACY_BENCHMARK = "^OMXSPI"     # what .ST proposals recorded before the switch to ^OMXSBGI
BENCHMARK_BASIS = {"^OMXSBGI": "total_return", "^OMXSPI": "price"}
MIN_OBSERVATIONS = 30
HISTORICAL_LABEL = "Påverkas av modellens träningsdata – inte giltigt som träffsäkerhetsmått"
_SIGN = {"BUY": 1, "SELL": -1}
_STOCKHOLM = ZoneInfo("Europe/Stockholm")


def benchmark_of(proposal: dict) -> str:
    """The benchmark a proposal is measured against; records without one predate the field."""
    return proposal.get("benchmark") or LEGACY_BENCHMARK


def benchmark_basis(benchmark: str) -> str:
    return BENCHMARK_BASIS.get(benchmark, "unknown")


def _by_date(closes) -> list[tuple[date, float]]:
    """Closes as (calendar day in the market's own zone, value), usable values only, in order."""
    rows = []
    for stamp, value in zip(closes.index, closes.values, strict=True):
        value = float(value)
        if math.isfinite(value) and value > 0:
            rows.append((stamp.date() if hasattr(stamp, "date") else stamp, value))
    return sorted(rows)


def _asof(rows, day):
    """The last close on or before ``day``, or None."""
    found = [v for d, v in rows if d <= day]
    return found[-1] if found else None


def _risk_pct(direction: str | None, entry, stop) -> float | None:
    """Stop distance as a share of entry, when entry and stop define a risk for the direction."""
    if entry is None or stop is None or not entry or entry <= 0:
        return None
    if direction == "BUY" and stop < entry:
        return (entry - stop) / entry
    if direction == "SELL" and stop > entry:
        return (stop - entry) / entry
    return None


def hypothetical_outcome(proposal: dict, get_closes) -> dict:
    """What the proposal would have returned over its horizon. Never a trade: no costs."""
    benchmark = benchmark_of(proposal)
    decision = proposal.get("decision") or {}
    direction = decision.get("pm_direction")
    horizon = int(proposal.get("holding_period_days") or 5)
    market = proposal.get("market_data") or {}
    out = {"run_id": proposal["run_id"], "ticker": proposal["ticker"], "benchmark": benchmark,
           "benchmark_basis": benchmark_basis(benchmark), "hypothetical": True,
           "direction": direction, "horizon_days": horizon,
           "reference_date": market.get("reference_date"),
           "reference_price_recorded": market.get("reference_price")}
    if not out["reference_date"]:
        return {**out, "state": "unavailable", "reason": "no reference price was recorded"}

    ref_day = date.fromisoformat(out["reference_date"])
    start = (ref_day - timedelta(days=7)).isoformat()
    end = (ref_day + timedelta(days=round(horizon * 7 / 5) + 10)).isoformat()
    stock = _by_date(get_closes(proposal["ticker"], start, end))
    bench = _by_date(get_closes(benchmark, start, end))

    entry_index = max((i for i, (d, _) in enumerate(stock) if d <= ref_day), default=None)
    if entry_index is None:
        return {**out, "state": "unavailable", "reason": "no close on or before the reference date"}
    if len(stock) <= entry_index + horizon:
        return {**out, "state": "pending", "reason": "the horizon has not traded yet"}
    window = stock[entry_index:entry_index + horizon + 1]
    (entry_day, entry_close), (exit_day, exit_close) = window[0], window[-1]
    if not bench or bench[0][0] > entry_day:
        return {**out, "state": "unavailable", "reason": f"no {benchmark} close at the entry"}
    if bench[-1][0] < exit_day:
        return {**out, "state": "pending", "reason": f"{benchmark} has not closed on the exit day"}

    raw = exit_close / entry_close - 1
    bench_return = _asof(bench, exit_day) / _asof(bench, entry_day) - 1
    alpha = raw - bench_return
    sign = _SIGN.get(direction)
    risk = _risk_pct(direction, decision.get("entry_price"), decision.get("stop_loss"))
    directional = sign * raw if sign else None
    breached = None
    if risk is not None:
        later = [v for _, v in window[1:]]
        breached = (min(later) <= entry_close * (1 - risk) if sign == 1
                    else max(later) >= entry_close * (1 + risk))
    return {**out, "state": "ok", "entry_date": entry_day.isoformat(),
            "exit_date": exit_day.isoformat(), "entry_close": entry_close, "exit_close": exit_close,
            "raw_return": raw, "benchmark_return": bench_return, "alpha": alpha,
            "directional_return": directional,
            "directional_alpha": sign * alpha if sign else None,
            # Risk/reward only where entry and stop defined a risk for the direction.
            "risk_pct": risk, "r_multiple": directional / risk if risk else None,
            "stop_breached_on_close": breached,
            "method": "close on the reference date to the close horizon_days sessions later; "
                      "benchmark over the same dates; no costs"}


def trade_result(execution: dict, close: dict) -> dict:
    """Realized result of a closed trade, after courtage and estimated slippage."""
    quantity = execution["quantity"]
    gross = (close["price"] - execution["price"]) * quantity
    costs = execution["courtage"] + execution["slippage"] + close["courtage"] + close["slippage"]
    net = gross - costs
    invested = execution["price"] * quantity
    return {"gross_pnl": round(gross, 2), "costs": round(costs, 2), "net_pnl": round(net, 2),
            "net_return": net / invested if invested else None}


def _stockholm_day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(_STOCKHOLM).date()


def trade_outcome(execution: dict, close: dict, benchmark: str, get_closes) -> dict:
    """A closed trade's actual result against the benchmark between the fill days' closes."""
    entry_day, exit_day = _stockholm_day(execution["executed_at"]), _stockholm_day(close["executed_at"])
    bench = _by_date(get_closes(benchmark, (entry_day - timedelta(days=7)).isoformat(),
                                (exit_day + timedelta(days=1)).isoformat()))
    result = trade_result(execution, close)
    out = {"kind": "trade", "hypothetical": False, "benchmark": benchmark,
           "benchmark_basis": benchmark_basis(benchmark), **result,
           "benchmark_window": [entry_day.isoformat(), exit_day.isoformat()],
           "method": "benchmark close on the entry fill day to close on the exit fill day"}
    if not bench or bench[0][0] > entry_day or bench[-1][0] < exit_day:
        return {**out, "state": "pending", "reason": f"{benchmark} has no close for the exit day yet"}
    bench_return = _asof(bench, exit_day) / _asof(bench, entry_day) - 1
    return {**out, "state": "ok", "benchmark_return": bench_return,
            "alpha_net": result["net_return"] - bench_return}


# --- The report ---------------------------------------------------------------------------

def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def summarize(outcomes: list[dict]) -> dict:
    """One group's figures; n counts only measured observations, pending ones are listed apart."""
    ok = [o for o in outcomes if o["state"] == "ok"]
    directional = [o for o in ok if o.get("directional_alpha") is not None]
    with_risk = [o for o in ok if o.get("r_multiple") is not None]
    summary = {
        "n": len(ok),
        "pending": sum(o["state"] == "pending" for o in outcomes),
        "unavailable": sum(o["state"] == "unavailable" for o in outcomes),
        "mean_raw_return": _mean(o.get("raw_return") for o in ok),
        "mean_alpha": _mean(o.get("alpha") for o in ok),
        "n_directional": len(directional),
        "mean_directional_return": _mean(o["directional_return"] for o in directional),
        "mean_directional_alpha": _mean(o["directional_alpha"] for o in directional),
        "directional_hit_rate": (sum(o["directional_alpha"] > 0 for o in directional)
                                 / len(directional)) if directional else None,
        "n_with_risk": len(with_risk),
        "mean_r_multiple": _mean(o["r_multiple"] for o in with_risk),
        "stop_breached_share": (sum(bool(o["stop_breached_on_close"]) for o in with_risk)
                                / len(with_risk)) if with_risk else None,
    }
    summary["warning"] = (f"n={summary['n']} < {MIN_OBSERVATIONS}: för få observationer för slutsatser"
                          if summary["n"] < MIN_OBSERVATIONS else None)
    return summary


def summarize_trades(outcomes: list[dict]) -> dict:
    ok = [o for o in outcomes if o["state"] == "ok"]
    summary = {
        "n": len(ok),
        "pending": sum(o["state"] == "pending" for o in outcomes),
        "open": sum(o["state"] == "open" for o in outcomes),
        "total_net_pnl": round(sum(o["net_pnl"] for o in ok), 2) if ok else None,
        "mean_net_return": _mean(o["net_return"] for o in ok),
        "mean_alpha_net": _mean(o["alpha_net"] for o in ok),
        "hit_rate_vs_benchmark": (sum(o["alpha_net"] > 0 for o in ok) / len(ok)) if ok else None,
    }
    summary["warning"] = (f"n={summary['n']} < {MIN_OBSERVATIONS}: för få observationer för slutsatser"
                          if summary["n"] < MIN_OBSERVATIONS else None)
    return summary


def _section(rows: list[dict], trades: list[dict]) -> dict:
    by_status = defaultdict(list)
    by_blocker = defaultdict(list)
    for row in rows:
        by_status[row["guard_status"]].append(row["outcome"])
        for code in row["blockers"]:
            by_blocker[code].append(row["outcome"])
    section = {
        "tradeable_hypothetical": summarize(by_status["TRADEABLE"]),
        "blocked_hypothetical": summarize(by_status["BLOCKED"]),
        "blocked_by_reason": {code: summarize(v) for code, v in sorted(by_blocker.items())},
        "executed_trades": summarize_trades(trades),
    }
    if by_status["UNCHECKED"]:
        section["unchecked_hypothetical"] = summarize(by_status["UNCHECKED"])
    return section


class _RangeCache:
    """One fetch per symbol over the widest range asked for, then sliced."""

    def __init__(self, get_closes, requests: list[tuple[str, str, str]]):
        self._get, self._ranges, self._data = get_closes, {}, {}
        for symbol, start, end in requests:
            lo, hi = self._ranges.get(symbol, (start, end))
            self._ranges[symbol] = (min(lo, start), max(hi, end))

    def __call__(self, symbol, start, end):
        if symbol not in self._data:
            lo, hi = self._ranges.get(symbol, (start, end))
            self._data[symbol] = self._get(symbol, min(lo, start), max(hi, end))
        series = self._data[symbol]
        days = [s.date() if hasattr(s, "date") else s for s in series.index]
        keep = [date.fromisoformat(start) <= d < date.fromisoformat(end) for d in days]
        return series[keep]


def _windows(proposal: dict) -> list[tuple[str, str, str]]:
    ref = (proposal.get("market_data") or {}).get("reference_date")
    if not ref:
        return []
    horizon = int(proposal.get("holding_period_days") or 5)
    day = date.fromisoformat(ref)
    start = (day - timedelta(days=7)).isoformat()
    end = (day + timedelta(days=round(horizon * 7 / 5) + 10)).isoformat()
    return [(proposal["ticker"], start, end), (benchmark_of(proposal), start, end)]


def build_report(pilot, get_closes=None, include_historical: bool = False) -> dict:
    """Outcomes for every PROPOSED run, grouped; nothing is written to the ledger.

    Runs analysed on a date before they were made (``is_historical``) are left
    out of the headline section: the models may have been trained on what came
    after that date. With ``include_historical`` they are shown in a section of
    their own, never pooled with the rest.
    """
    if get_closes is None:
        from tradingagents.dataflows.vendors.yahoo.market import get_closes
    events = pilot.ledger.events()
    proposals = [pilot.proposal(run_id, events) for run_id in pilot.run_ids()]
    closes = _RangeCache(get_closes, [w for p in proposals for w in _windows(p)])
    payloads = pilot._corrected(events)
    rows, trades = {True: [], False: []}, {True: [], False: []}
    for proposal in proposals:
        run_id = proposal["run_id"]
        check = next((e["payload"] for e in reversed(events)
                      if e["run_id"] == run_id and e["type"] == "RISK_CHECKED"), None)
        historical = bool(proposal.get("is_historical"))
        rows[historical].append({
            "run_id": run_id,
            "guard_status": check["status"] if check else "UNCHECKED",
            "blockers": [b["code"] for b in check["blockers"]] if check else [],
            "outcome": hypothetical_outcome(proposal, closes),
        })
        executed = pilot._last(events, run_id, {"MANUALLY_EXECUTED"})
        if executed is None:
            continue
        closed = pilot._last(events, run_id, {"CLOSED"})
        if closed is None:
            trades[historical].append({"run_id": run_id, "state": "open"})
            continue
        trades[historical].append({"run_id": run_id, **trade_outcome(
            payloads[executed["event_id"]], payloads[closed["event_id"]],
            benchmark_of(proposal), get_closes)})
    report = {"main": _section(rows[False], trades[False]),
              "excluded_historical_runs": len(rows[True]),
              "runs": {"main": rows[False], "historical": rows[True] if include_historical else []}}
    if include_historical:
        report["historical"] = {"label": HISTORICAL_LABEL,
                                **_section(rows[True], trades[True])}
    return report


# --- Rendering ------------------------------------------------------------------------------

def _pct(value) -> str:
    return "–" if value is None else f"{value:+.2%}"


def _group_lines(title: str, s: dict) -> list[str]:
    lines = [f"{title}: n={s['n']} (väntar: {s['pending']}, saknar data: {s['unavailable']})"]
    if s["n"]:
        lines.append(f"    utfall {_pct(s['mean_raw_return'])}, alpha {_pct(s['mean_alpha'])}")
        if s["n_directional"]:
            hit = f"{s['directional_hit_rate']:.0%}"
            lines.append(f"    i riktningen (n={s['n_directional']}): utfall "
                         f"{_pct(s['mean_directional_return'])}, alpha "
                         f"{_pct(s['mean_directional_alpha'])}, alpha > 0 i {hit}")
        if s["n_with_risk"]:
            lines.append(f"    risk/reward (n={s['n_with_risk']}): snitt "
                         f"{s['mean_r_multiple']:+.2f} R, stop passerad på stängning i "
                         f"{s['stop_breached_share']:.0%}")
    if s["warning"]:
        lines.append(f"    VARNING: {s['warning']}")
    return lines


def _trade_lines(s: dict) -> list[str]:
    lines = [f"c) Faktiskt genomförda affärer: n={s['n']} (öppna: {s['open']}, väntar: {s['pending']})"]
    if s["n"]:
        lines.append(f"    netto {s['total_net_pnl']:+.2f}, snitt {_pct(s['mean_net_return'])} "
                     f"efter kostnader, alpha {_pct(s['mean_alpha_net'])}, "
                     f"slog benchmark i {s['hit_rate_vs_benchmark']:.0%}")
    if s["warning"]:
        lines.append(f"    VARNING: {s['warning']}")
    return lines


def _render_section(section: dict) -> list[str]:
    lines = _group_lines("a) TRADEABLE – hypotetiskt", section["tradeable_hypothetical"])
    lines += _group_lines("b) BLOCKED – hypotetiskt, totalt", section["blocked_hypothetical"])
    for code, s in section["blocked_by_reason"].items():
        lines += ["  " + line for line in _group_lines(f"b) {code}", s)]
    if "unchecked_hypothetical" in section:
        lines += _group_lines("Ej riskkontrollerade – hypotetiskt", section["unchecked_hypothetical"])
    lines += _trade_lines(section["executed_trades"])
    return lines


def render_report(report: dict) -> str:
    lines = ["MTI-pilot – utvärdering",
             "Hypotetiska utfall är inga affärer: inga kostnader, ingen fyllnad.",
             "Ett förslag med flera blockeringsorsaker räknas under varje orsak; "
             "grupperna snittas aldrig ihop.",
             "", "Live-körningar (huvudsiffror)"]
    lines += _render_section(report["main"])
    excluded = report["excluded_historical_runs"]
    if "historical" in report:
        lines += ["", f"Historiska körningar – {report['historical']['label']}"]
        lines += _render_section(report["historical"])
    elif excluded:
        lines += ["", f"{excluded} historisk(a) körning(ar) utesluten(a); visa med --include-historical."]
    return "\n".join(lines)
