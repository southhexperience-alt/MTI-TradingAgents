"""``mti-pilot``: the pilot's command line. It records; it never trades."""

from __future__ import annotations

import json
from datetime import datetime

import typer

from .config import CONFIG_TEMPLATE, config_path, load_config
from .ledger import LedgerError
from .workflow import ACTIVATION_PHRASE, REOPEN_PHRASE, STOCKHOLM, Pilot, WorkflowError

app = typer.Typer(help="MTI Swedish guarded pilot over TradingAgents. Records proposals and "
                       "Mats' manual trades; places no orders.", no_args_is_help=True)


def _pilot() -> Pilot:
    return Pilot(load_config())


def _fail(exc: Exception):
    typer.secho(f"Refused: {exc}", fg="red", err=True)
    raise typer.Exit(1)


def _show(data):
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


@app.command("init-config")
def init_config():
    """Write a configuration template with no amounts filled in."""
    path = config_path()
    if path.exists():
        _fail(WorkflowError(f"{path} already exists"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    path.chmod(0o600)
    typer.echo(f"Wrote {path}")


@app.command()
def status():
    """Pilot mode, limits, halt state, realized result and open positions."""
    pilot = _pilot()
    book = pilot.book()
    cfg = pilot.config
    _show({
        "configured_mode": cfg.mode,
        "micro_live_blockers": pilot.micro_live_blockers() if cfg.mode == "MICRO_LIVE" else [],
        "limits": cfg.limits(),
        "pilot_halted": book.halted,
        "realized_pnl": book.realized_pnl,
        "open_risk": book.open_risk,
        "open_positions": [vars(p) for p in book.open_positions],
        "ledger": str(pilot.ledger.path),
        "ledger_chain_ok": not pilot.ledger.verify(),
    })


@app.command()
def analyze(
    ticker: str,
    date: str = typer.Option(None, help="Analysis date YYYY-MM-DD (default: today)"),
    portfolio: str = typer.Option(None, help="Holdings JSON (upstream PortfolioContext format)"),
    analysts: str = typer.Option("market,social,news,fundamentals"),
):
    """Run TradingAgents on a Swedish ticker, record the proposal, risk-check it."""
    from tradingagents.portfolio import load_portfolio

    from .runner import run_analysis

    pilot = _pilot()
    date = date or datetime.now(STOCKHOLM).date().isoformat()
    try:
        book = load_portfolio(portfolio) if portfolio else None
        run_id, record, result = run_analysis(pilot, ticker, date, portfolio=book,
                                              selected_analysts=analysts.split(","))
    except (WorkflowError, LedgerError, ValueError) as exc:
        _fail(exc)
    _show({"run_id": run_id, "mode": record["mode"], "rating": record["decision"]["final_rating"],
           "status": result.status, "blockers": [vars(b) for b in result.blockers],
           "warnings": [vars(w) for w in result.warnings], "order": result.order,
           "benchmark": record["benchmark"], "report_dir": record["report_dir"]})


@app.command("list")
def list_runs():
    """Every recorded run and where it stands."""
    pilot = _pilot()
    events = pilot.ledger.events()
    for run_id in pilot.run_ids():
        p = pilot.proposal(run_id, events)
        typer.echo(f"{run_id}  {p['mode']:<10} {p['decision']['final_rating']:<11} "
                   f"{pilot.status(run_id, events)}")


@app.command()
def show(run_id: str):
    """All ledger events for one run."""
    _show(_pilot().ledger.run_events(run_id))


@app.command()
def approve(run_id: str, note: str = typer.Option("", help="Why")):
    """Record Mats' approval. It does not place an order; Mats does that in Avanza."""
    try:
        _show(_pilot().approve(run_id, note))
    except (WorkflowError, LedgerError) as exc:
        _fail(exc)


@app.command()
def reject(run_id: str, note: str = typer.Option("", help="Why")):
    """Record that Mats will not act on a proposal."""
    try:
        _show(_pilot().reject(run_id, note))
    except (WorkflowError, LedgerError) as exc:
        _fail(exc)


@app.command()
def execute(run_id: str, price: float = typer.Option(...), quantity: float = typer.Option(...),
            courtage: float = typer.Option(...), at: str = typer.Option(..., help="2026-10-07T09:05"),
            slippage: float = typer.Option(0.0, help="Estimated spread/slippage, in currency"),
            comment: str = typer.Option("")):
    """Record a buy Mats placed manually in Avanza."""
    try:
        _show(_pilot().record_execution(run_id, price=price, quantity=quantity, courtage=courtage,
                                        executed_at=at, slippage=slippage, comment=comment))
    except (WorkflowError, LedgerError) as exc:
        _fail(exc)


@app.command("close")
def close_trade(run_id: str, price: float = typer.Option(...), quantity: float = typer.Option(...),
                courtage: float = typer.Option(...), at: str = typer.Option(...),
                slippage: float = typer.Option(0.0), comment: str = typer.Option("")):
    """Record the exit Mats made manually in Avanza."""
    try:
        _show(_pilot().record_close(run_id, price=price, quantity=quantity, courtage=courtage,
                                    executed_at=at, slippage=slippage, comment=comment))
    except (WorkflowError, LedgerError) as exc:
        _fail(exc)


@app.command()
def evaluate(
    run_id: str = typer.Argument(None, help="Record one run's outcome as EVALUATED instead"),
    include_historical: bool = typer.Option(
        False, "--include-historical",
        help="Also show runs dated before they were made, in a separate section"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable report"),
):
    """Outcomes for every proposal: TRADEABLE, BLOCKED (per reason) and executed, apart."""
    from .measurement import build_report, render_report

    pilot = _pilot()
    if run_id:
        try:
            _show(pilot.evaluate(run_id))
        except (WorkflowError, LedgerError) as exc:
            _fail(exc)
        return
    report = build_report(pilot, include_historical=include_historical)
    if as_json:
        _show(report)
    else:
        typer.echo(render_report(report))


@app.command()
def correct(event_id: str, field: str, value: str, reason: str = typer.Option(...)):
    """Append a correction to an execution or exit; the original stays."""
    parsed = float(value) if field in ("price", "quantity", "courtage", "slippage") else value
    try:
        _show(_pilot().correct(event_id, {field: parsed}, reason))
    except (WorkflowError, LedgerError) as exc:
        _fail(exc)


@app.command("activate-micro-live")
def activate_micro_live():
    """Mats' explicit activation of MICRO_LIVE for the configured limits."""
    pilot = _pilot()
    _show(pilot.config.limits())
    phrase = typer.prompt(f"Type '{ACTIVATION_PHRASE}' to activate for these limits")
    try:
        _show(pilot.activate_micro_live(phrase))
    except (WorkflowError, LedgerError) as exc:
        _fail(exc)


@app.command("deactivate-micro-live")
def deactivate_micro_live(reason: str = typer.Option(...)):
    _show(_pilot().deactivate_micro_live(reason))


@app.command()
def reopen(reason: str = typer.Option(...)):
    """Reopen a halted pilot (needs a deliberately raised MAX_PILOT_LOSS)."""
    phrase = typer.prompt(f"Type '{REOPEN_PHRASE}' to reopen")
    try:
        _show(_pilot().reopen(phrase, reason))
    except (WorkflowError, LedgerError) as exc:
        _fail(exc)


@app.command()
def verify():
    """Check the ledger's hash chain."""
    problems = _pilot().ledger.verify()
    if problems:
        for p in problems:
            typer.secho(p, fg="red")
        raise typer.Exit(1)
    typer.echo("Ledger chain intact.")


if __name__ == "__main__":
    app()
