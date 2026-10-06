"""Pilot configuration: mode and the money limits Mats sets himself.

None of the amounts has a default. A missing amount is not zero and not
"unlimited": it means the decision has not been made, and MICRO_LIVE stays
blocked until it is. SHADOW needs none of them.

The file lives outside the repository and outside Dropbox by default
(``~/.config/mti-tradingagents/pilot.toml``), since it holds amounts of
Mats' own money. ``MTI_PILOT_CONFIG`` points elsewhere.
"""

from __future__ import annotations

import copy
import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SHADOW = "SHADOW"
MICRO_LIVE = "MICRO_LIVE"
MODES = (SHADOW, MICRO_LIVE)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "mti-tradingagents" / "pilot.toml"
DEFAULT_DATA_DIR = Path.home() / ".mti-tradingagents" / "pilot"

_AMOUNT_KEYS = ("PILOT_CAPITAL", "MAX_RISK_PER_TRADE", "MAX_PILOT_LOSS")
_KNOWN_KEYS = {"MODE", "CURRENCY", "ALLOWED_SUFFIXES", "DATA_DIR", *_AMOUNT_KEYS}

# Swedish macro headlines added to upstream's US-centric global news queries.
SWEDISH_NEWS_QUERIES = [
    "Riksbank interest rate decision Sweden inflation krona",
    "Stockholm stock exchange OMX Swedish economy",
]

CONFIG_TEMPLATE = """\
# MTI TradingAgents pilot configuration.
# Amounts are in CURRENCY. Leave an amount out until it is decided:
# a missing amount keeps MICRO_LIVE blocked. SHADOW needs none of them.

MODE = "SHADOW"            # SHADOW or MICRO_LIVE
CURRENCY = "SEK"
ALLOWED_SUFFIXES = [".ST"]

# PILOT_CAPITAL =
# MAX_RISK_PER_TRADE =
# MAX_PILOT_LOSS =
"""


class PilotConfigError(ValueError):
    """The pilot configuration file cannot be used as written."""


@dataclass(frozen=True)
class PilotConfig:
    mode: str = SHADOW
    pilot_capital: float | None = None
    max_risk_per_trade: float | None = None
    max_pilot_loss: float | None = None
    currency: str = "SEK"
    allowed_suffixes: tuple[str, ...] = (".ST",)
    data_dir: Path = field(default=DEFAULT_DATA_DIR)

    def limits(self) -> dict:
        """The three amounts, as recorded when MICRO_LIVE is activated."""
        return {"pilot_capital": self.pilot_capital,
                "max_risk_per_trade": self.max_risk_per_trade,
                "max_pilot_loss": self.max_pilot_loss,
                "currency": self.currency}

    def micro_live_config_blockers(self) -> list[str]:
        """What in the configuration itself keeps MICRO_LIVE from running."""
        blockers = []
        names = {"pilot_capital": "PILOT_CAPITAL", "max_risk_per_trade": "MAX_RISK_PER_TRADE",
                 "max_pilot_loss": "MAX_PILOT_LOSS"}
        for attr, key in names.items():
            if getattr(self, attr) is None:
                blockers.append(f"{key} is not configured")
        if blockers:
            return blockers
        if self.max_risk_per_trade > self.pilot_capital:
            blockers.append("MAX_RISK_PER_TRADE exceeds PILOT_CAPITAL")
        if self.max_pilot_loss > self.pilot_capital:
            blockers.append("MAX_PILOT_LOSS exceeds PILOT_CAPITAL")
        if self.max_risk_per_trade > self.max_pilot_loss:
            blockers.append("MAX_RISK_PER_TRADE exceeds MAX_PILOT_LOSS: one trade could pass the cap")
        return blockers

    def instrument_allowed(self, ticker: str) -> bool:
        return any(ticker.upper().endswith(s.upper()) for s in self.allowed_suffixes)

    @property
    def ledger_path(self) -> Path:
        return Path(self.data_dir) / "ledger.jsonl"


def _amount(key: str, value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotConfigError(f"{key} must be a number, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise PilotConfigError(f"{key} must be a positive amount, got {value!r}")
    return float(value)


def parse_config(data: dict) -> PilotConfig:
    """Validate a parsed TOML mapping; unknown keys are an error, not ignored."""
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        raise PilotConfigError(f"unknown configuration keys: {', '.join(sorted(unknown))}")
    mode = str(data.get("MODE", SHADOW)).upper()
    if mode not in MODES:
        raise PilotConfigError(f"MODE must be one of {', '.join(MODES)}, got {data.get('MODE')!r}")
    suffixes = data.get("ALLOWED_SUFFIXES", [".ST"])
    if not isinstance(suffixes, list) or not suffixes or not all(
            isinstance(s, str) and s.startswith(".") for s in suffixes):
        raise PilotConfigError("ALLOWED_SUFFIXES must be a non-empty list like [\".ST\"]")
    data_dir = Path(os.path.expanduser(data.get("DATA_DIR") or os.environ.get("MTI_PILOT_DATA_DIR")
                                       or DEFAULT_DATA_DIR))
    return PilotConfig(
        mode=mode,
        pilot_capital=_amount("PILOT_CAPITAL", data.get("PILOT_CAPITAL")),
        max_risk_per_trade=_amount("MAX_RISK_PER_TRADE", data.get("MAX_RISK_PER_TRADE")),
        max_pilot_loss=_amount("MAX_PILOT_LOSS", data.get("MAX_PILOT_LOSS")),
        currency=str(data.get("CURRENCY", "SEK")),
        allowed_suffixes=tuple(suffixes),
        data_dir=data_dir,
    )


def config_path() -> Path:
    return Path(os.environ.get("MTI_PILOT_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


def load_config(path: str | Path | None = None) -> PilotConfig:
    """Read the pilot configuration; with no file, the pilot is SHADOW with no amounts."""
    path = Path(path).expanduser() if path else config_path()
    if not path.exists():
        return parse_config({})
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PilotConfigError(f"pilot configuration {path} is not readable: {exc}") from exc
    return parse_config(data)


def tradingagents_config(pilot: PilotConfig, base: dict | None = None) -> dict:
    """The upstream config a pilot run uses: upstream defaults, plus the pilot's own files.

    The pilot keeps its own memory log and results, so its decisions and their
    settlement against ^OMXSPI never mix with other TradingAgents use. Swedish
    macro queries are added to upstream's, not substituted for them.
    """
    from tradingagents.default_config import build_default_config

    cfg = copy.deepcopy(base) if base is not None else build_default_config()
    data_dir = Path(pilot.data_dir)
    cfg["results_dir"] = str(data_dir / "tradingagents_results")
    cfg["memory_log_path"] = str(data_dir / "trading_memory.md")
    queries = list(cfg.get("global_news_queries") or [])
    cfg["global_news_queries"] = queries + [q for q in SWEDISH_NEWS_QUERIES if q not in queries]
    return cfg
