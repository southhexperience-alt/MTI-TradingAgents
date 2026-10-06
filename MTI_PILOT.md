# MTI TradingAgents — Swedish Live Pilot v0.1

Ett tunt svenskt risk-, logg- och arbetsflödeslager ovanpå
[TradingAgents](https://github.com/TauricResearch/TradingAgents) v0.6.0
(`1394a3f72aa4393e1a98f51b382434c4b4c2d972`).

**Systemet lägger aldrig order.** Det föreslår. Mats beslutar och lägger
eventuell order manuellt i Avanza. Ingen inloggning, inget Avanza-API, inget
BankID, inga order-anrop finns i koden.

```
TradingAgents (oförändrad)  →  .ST-aktie, ^OMXSBGI som benchmark
        ↓ final_state
mti_pilot.proposal   typade fält ur Trader/PM (exakt round-trip, aldrig prosa)
        ↓
ledger: PROPOSED     sparas INNAN guard och innan utfall är känt
        ↓
mti_pilot.guard      deterministisk: TRADEABLE eller BLOCKED + alla skäl
        ↓
ledger: RISK_CHECKED
        ↓  (endast MICRO_LIVE, endast Mats)
USER_APPROVED → MANUALLY_EXECUTED → CLOSED → EVALUATED (mot ^OMXSBGI, efter kostnader)
```

## Vad som återanvänds från upstream

Analytiker, bull/bear-debatt, Trader (strukturerat `TraderProposal` med entry/stop),
riskdebatt, Portfolio Manager (`PortfolioDecision` med rating/target),
point-in-time-data (`load_ohlcv`, `as_of`-fönster), `benchmark_map` +
`resolve_benchmark`, settlement/alpha (`fetch_returns`), memory log, rapporter,
checkpointing, `PortfolioContext`, provider-/modellabstraktioner.

Ändrat i upstream: **en rad** i `default_config.py` (`.ST → ^OMXSBGI`) samt
paketering i `pyproject.toml`. Allt annat MTI-specifikt ligger i `mti_pilot/`.

## Benchmark: `^OMXSBGI` (totalavkastning)

`.ST`-aktier jämförs mot **OMX Stockholm Benchmark_GI** (`^OMXSBGI`), ett
bruttoindex där utdelningar återinvesteras. Aktiekurserna kommer från upstreams
`get_closes` (Yahoo, `auto_adjust=True`), alltså också utdelningsjusterade.
Båda benen är därmed totalavkastning, och jämförelsen är konsekvent: en aktie
som delar ut får inte en artificiell nackdel mot ett index som inte gör det.
Med det tidigare prisindexet `^OMXSPI` kom aktiens utdelning med men inte
indexets, så alpha blev systematiskt för positiv. Under det senaste året gav
`^OMXSBGI` 7,2 procentenheter mer än `^OMXSPI`. Ungefär 2,7 av dem kom under
utdelningssäsongen mars–juni, medan jul–okt gav bara 0,2. Resten beror på att
indexen innehåller olika bolag (se nedan), inte på utdelningar.

Kontroll 2026-10-06: `^OMXSBGI` gav 251 dagliga rader 2025-10-06–2026-10-06,
exakt samma handelsdagar som VOLV-B, ERIC-B, INVE-B, ATCO-A och SEB-A (.ST).
Bruttokaraktären syns i data: den största dagliga skillnaden mot prisindexet
(+0,51 %) inföll 2026-03-25, samma dag som SEB:s ex-utdelningsdag.

Observera att Benchmark-indexet omfattar de största och mest omsatta bolagen,
inte hela börsen. För pilotens storbolag är det relevant; Yahoo saknar ett
fungerande bruttoindex för hela börsen (`^OMXSGI` ger inga data).

Varje PROPOSED-post sparar `benchmark` och `benchmark_basis`, så ett framtida
byte går att spåra. Poster som saknar fältet utvärderas mot `^OMXSPI`, som var
benchmark när de skrevs.

## Utvärdering (`mti-pilot evaluate`)

`mti-pilot evaluate` räknar utfall för **alla** PROPOSED-poster och skriver
ingenting till ledgern. `mti-pilot evaluate <run_id>` utvärderar en körning och
sparar resultatet som en EVALUATED-post.

**Hypotetiskt utfall** (alla förslag, också blockerade och de som aldrig
handlades): från stängningskursen på referensdagen, alltså den kurs analysen
såg, till stängningen `holding_period_days` (5) handelsdagar senare i aktiens
egen kalender. Benchmark mäts över exakt samma datum, och alpha = aktiens
avkastning − benchmarkens. Inga kostnader eller fyllnader ingår; utfallen är
märkta `hypothetical: true` och är inga affärer. Båda benen läses ur en och
samma justerade kursserie som hämtas vid utvärderingen. Det sparade
referenspriset visas bredvid, men används inte i beräkningen: delas en
utdelning ut inom fönstret räknar Yahoo om tidigare kurser, och då skulle
utdelningen falla bort ur avkastningen.

- *I riktningen*: BUY/Overweight räknas +1, SELL/Underweight −1. Hold har
  ingen riktning och syns bara i råutfallet.
- *Risk/reward* räknas bara när entry och stop finns och ger en risk i
  riktningen. Stopavståndet i procent av entry blir 1 R, och utfallet uttrycks i
  R. Vi visar också om någon stängningskurs i fönstret passerade stopnivån.
  Saknas entry eller stop redovisas bara utfall och alpha i riktningen.

**Grupperna snittas aldrig ihop:**

- a) TRADEABLE – hypotetiskt (även de som sedan handlades, så att signalen
  mäts oberoende av Mats val)
- b) BLOCKED – hypotetiskt, totalt och per blockeringsorsak. Ett förslag med
  flera orsaker räknas under varje orsak.
- c) Faktiskt genomförda affärer – verkligt netto efter courtage och slippage,
  mot benchmark mellan fyllnadsdagarnas stängningar.

Varje grupp visar `n` (mätta observationer) samt hur många som väntar på att
horisonten ska gå ut och hur många som saknar data. När n < 30 visas
`VARNING: n=… < 30`.

**Historiska körningar utesluts som standard.** En analys daterad före dagen
den gjordes (`is_historical`) kan påverkas av att modellerna tränats på vad
som hände efteråt. Den mäter därför inte träffsäkerhet. Med
`--include-historical` visas sådana körningar i en egen sektion märkt
*"Påverkas av modellens träningsdata – inte giltigt som träffsäkerhetsmått"*.
De räknas aldrig in i huvudsiffrorna.

**25 observationer räcker inte för beslut.** Med fem dagars horisont är
spridningen i alpha stor jämfört med den skillnad vi letar efter. Några dussin
utfall kan se bra eller dåliga ut av ren slump, och femdagarsfönster för
samma aktie överlappar och är inte oberoende. Beslut om belopp eller
MICRO_LIVE bör vänta tills varje relevant grupp är väl över 30, helst
betydligt fler och spridda över olika marknadslägen.

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
mti-pilot evaluate                              # rapport över alla förslag, grupperade; skriver inget
mti-pilot evaluate --include-historical         # + historiska körningar i egen sektion
mti-pilot evaluate <run_id>                     # sparar EVALUATED för en körning
mti-pilot verify
```

## Synka upstream

```bash
git fetch upstream --tags
git merge <ny-upstream-tag>     # på en egen branch; konflikter bör bara kunna uppstå i benchmark_map/pyproject
```

## Kända begränsningar

- `^OMXSBGI` omfattar Benchmark-bolagen, inte hela börsen. En pilot som
  handlar småbolag skulle mätas mot fel universum.
- Upstreams egen memory log räknar utfall mot den benchmark som gäller när
  utfallet beräknas, inte den som sparades vid analysen. MTI:s utvärdering
  använder alltid den benchmark som sparades på posten.
- Hypotetiska utfall bygger på stängningskurser. Stopp som utlöstes intradag
  syns inte, och fyllnad till exakt stängningskurs är en förenkling.
- Historiska körningar ser utdelningsjusterade prisnivåer (justerade med
  utdelningar efter analysdatum). Live-körningar påverkas inte. Historiska
  förslag märks `is_historical` och ska inte räknas som framåtriktad evidens.
- Hashkedjan bevisar integritet lokalt men kan skrivas om i sin helhet av den
  som har filen. Förankra kedjans huvud externt (t.ex. git-commit) för starkare bevis.
- Delstängningar stöds inte i v0.1. Benchmark för genomförda affärer mäts
  stängning-till-stängning på fyllnadsdagarna.
- Socialt sentiment (StockTwits/Reddit) och SEC-data är tunna eller saknas för
  svenska bolag; nyheter och fundamenta kommer från Yahoo.
