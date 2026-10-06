# MTI TradingAgents — Swedish Live Pilot v0.1

Ett tunt svenskt risk-, logg- och arbetsflödeslager ovanpå
[TradingAgents](https://github.com/TauricResearch/TradingAgents) v0.6.0
(`1394a3f72aa4393e1a98f51b382434c4b4c2d972`).

**Systemet lägger aldrig order.** Det föreslår. Mats beslutar och lägger
eventuell order manuellt i Avanza. Ingen inloggning, inget Avanza-API, inget
BankID, inga order-anrop finns i koden.

```
TradingAgents (oförändrad)  →  .ST-aktie, ^OMXSPI som benchmark
        ↓ final_state
mti_pilot.proposal   typade fält ur Trader/PM (exakt round-trip, aldrig prosa)
        ↓
ledger: PROPOSED     sparas INNAN guard och innan utfall är känt
        ↓
mti_pilot.guard      deterministisk: TRADEABLE eller BLOCKED + alla skäl
        ↓
ledger: RISK_CHECKED
        ↓  (endast MICRO_LIVE, endast Mats)
USER_APPROVED → MANUALLY_EXECUTED → CLOSED → EVALUATED (mot ^OMXSPI, efter kostnader)
```

## Vad som återanvänds från upstream

Analytiker, bull/bear-debatt, Trader (strukturerat `TraderProposal` med entry/stop),
riskdebatt, Portfolio Manager (`PortfolioDecision` med rating/target),
point-in-time-data (`load_ohlcv`, `as_of`-fönster), `benchmark_map` +
`resolve_benchmark`, settlement/alpha (`fetch_returns`), memory log, rapporter,
checkpointing, `PortfolioContext`, provider-/modellabstraktioner.

Ändrat i upstream: **en rad** i `default_config.py` (`.ST → ^OMXSPI`) samt
paketering i `pyproject.toml`. Allt annat MTI-specifikt ligger i `mti_pilot/`.

## Installation

```bash
uv venv ~/.venvs/mti-tradingagents --python 3.13
VIRTUAL_ENV=~/.venvs/mti-tradingagents uv pip install -e ".[dev]"
```

Virtuell miljö, konfiguration och ledger ligger medvetet **utanför Dropbox**.
API-nycklar (t.ex. `OPENAI_API_KEY`) läggs i `~/.config/<projekt>/env` med
`chmod 600`, aldrig i repot.

## Konfiguration

`mti-pilot init-config` skriver `~/.config/mti-tradingagents/pilot.toml`
(eller `$MTI_PILOT_CONFIG`). Inga belopp har standardvärden:

| Nyckel | Betydelse |
|---|---|
| `MODE` | `SHADOW` (standard) eller `MICRO_LIVE` |
| `PILOT_CAPITAL` | pilotkapital — **ej beslutat** |
| `MAX_RISK_PER_TRADE` | max förlust om stop fylls — **ej beslutat** |
| `MAX_PILOT_LOSS` | absolut förlusttak för piloten — **ej beslutat** |
| `ALLOWED_SUFFIXES` | `[".ST"]` |
| `DATA_DIR` | ledger och körningar, standard `~/.mti-tradingagents/pilot` |

Okända nycklar (t.ex. felstavade) är fel, inte tysta.

## Lägen

- **SHADOW** — standard. Kräver inga belopp. Förslag loggas och utvärderas men kan
  inte godkännas för handel. Maxrisk definieras per aktie och i % av entry.
- **MICRO_LIVE** — kräver alla tre beloppen, inbördes konsistenta
  (`MAX_RISK_PER_TRADE ≤ MAX_PILOT_LOSS ≤ PILOT_CAPITAL`), **och** att Mats kört
  `mti-pilot activate-micro-live` och skrivit `JAG AKTIVERAR MICRO_LIVE` för exakt
  de beloppen. Ändras ett belopp krävs ny aktivering. Saknas något vägrar
  analysen att starta (ingen tyst nedgradering till SHADOW). MICRO_LIVE betyder
  fortfarande: systemet föreslår → Mats handlar manuellt.

## Riskvaktens regler (`mti_pilot/guard.py`)

1. **Ingen averaging down.** BUY i ett instrument som hålls (enligt portföljfilen
   eller pilotens egen ledger) under anskaffningskurs → `AVERAGING_DOWN_DENIED`.
   Okänd anskaffningskurs → `POSITION_COST_UNKNOWN`. Ingen override finns.
   MICRO_LIVE kräver portföljfil (tom bok går bra) → annars `PORTFOLIO_CONTEXT_MISSING`.
2. **Ingen affär utan definierad risk.** Kräver instrument, riktning, entry, stop,
   maxrisk, motivering, analysdatum, marknadspris + källa — som *fält*. Trader-fält
   godtas bara om texten är exakt upstreams rendering av `TraderProposal`
   (annars `TRADER_FIELDS_NOT_STRUCTURED`). Motsägelser blockeras:
   stop ≥ entry, target ≤ entry, stop ≥ senaste pris, Trader-riktning ≠ PM-rating,
   referenspris daterat efter analysdatum.
3. **Absolut förlusttak.** Realiserad nettoförlust (efter courtage och slippage)
   ≥ `MAX_PILOT_LOSS` → `PILOT_HALTED`; inget nytt blir TRADEABLE och inget kan
   godkännas. Dessutom blockeras en affär om realiserad förlust + öppen risk +
   affärens risk skulle passera taket. Återöppning: `mti-pilot reopen` med frasen
   `JAG ÅTERÖPPNAR PILOTEN`, ett skäl, och ett medvetet höjt `MAX_PILOT_LOSS`
   (och därmed ny aktivering).

v0.1 är **long-only**: SELL blir aldrig TRADEABLE; en säljsignal på en öppen
position visas som varning `EXIT_SIGNAL_ON_OPEN_POSITION`. Hold blir BLOCKED
(`NO_ACTIONABLE_DIRECTION`).

Storlek i MICRO_LIVE: `antal = floor(min(MAX_RISK_PER_TRADE / (entry − stop),
fritt kapital / entry))`.

## Beslutslogg (`mti_pilot/ledger.py`)

`ledger.jsonl`: append-only, hashkedjad (varje rad bär föregående rads sha256).
Ingen update/delete finns. `mti-pilot verify` hittar ändrade, borttagna eller
omkastade rader, och en bruten kedja stoppar alla nya skrivningar. Varje körnings
fulla state skrivs en gång till `runs/<run_id>/state.json` (exklusiv skapning) och
dess sha256 lagras i PROPOSED. Fel rättas med `mti-pilot correct` (CORRECTION-post
för execution/exit-fält); rekommendationer och riskbedömningar kan inte korrigeras.

PROPOSED innehåller bl.a.: run_id, tidpunkt, ticker, analysdatum, `is_historical`,
TradingAgents-version, upstream-commit, MTI-version, fork-commit, modeller/providers,
analytiker, referenspris med datum och källa, slutrating, trader- och PM-text,
typade fält, benchmark, portfölj.

## Kommandon

```bash
mti-pilot status
mti-pilot analyze VOLV-B.ST                     # dagens datum
mti-pilot analyze VOLV-B.ST --date 2026-09-04   # historiskt (is_historical=true)
mti-pilot list
mti-pilot show <run_id>
mti-pilot approve <run_id> --note "..."         # registrerar godkännande, lägger ingen order
mti-pilot execute <run_id> --price 345.2 --quantity 10 --courtage 39 --slippage 2 --at 2026-10-07T09:05
mti-pilot close <run_id> --price 352.0 --quantity 10 --courtage 39 --at 2026-10-14T15:30
mti-pilot evaluate <run_id>                     # trade: netto vs ^OMXSPI; annars shadow via upstream settlement
mti-pilot verify
```

## Synka upstream

```bash
git fetch upstream --tags
git merge <ny-upstream-tag>     # på en egen branch; konflikter bör bara kunna uppstå i benchmark_map/pyproject
```

## Kända begränsningar

- `^OMXSPI` är ett **prisindex**, medan Yahoos aktiekurser (`auto_adjust=True`) är
  utdelningsjusterade. Över ett ex-datum blir alpha därför något för positiv.
  `^OMXSBGI` (bruttoindex) finns på Yahoo och kan övervägas.
- Historiska körningar ser utdelningsjusterade prisnivåer (justerade med
  utdelningar efter analysdatum). Live-körningar påverkas inte. Historiska
  förslag märks `is_historical` och ska inte räknas som framåtriktad evidens.
- Hashkedjan bevisar integritet lokalt men kan skrivas om i sin helhet av den
  som har filen. Förankra kedjans huvud externt (t.ex. git-commit) för starkare bevis.
- Delstängningar stöds inte i v0.1. Benchmark för genomförda affärer mäts
  stängning-till-stängning på fyllnadsdagarna.
- Socialt sentiment (StockTwits/Reddit) och SEC-data är tunna eller saknas för
  svenska bolag; nyheter och fundamenta kommer från Yahoo.
