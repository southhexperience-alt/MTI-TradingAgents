"""MTI Swedish guarded pilot: a thin layer over TradingAgents.

TradingAgents analyses and proposes. This package adds only what the pilot
needs on top: Swedish market configuration, a deterministic risk guard, the
pilot's mode and loss cap, an append-only decision ledger, and the record of
trades Mats places by hand. It never talks to a broker: every order is placed
manually, outside this code.
"""

MTI_VERSION = "0.1.0"

# The upstream release this fork is based on, pinned so every recorded
# recommendation names the code that produced it.
UPSTREAM_REPO = "TauricResearch/TradingAgents"
UPSTREAM_VERSION = "v0.6.0"
UPSTREAM_COMMIT = "1394a3f72aa4393e1a98f51b382434c4b4c2d972"
