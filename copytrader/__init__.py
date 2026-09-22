"""copytrader - paper-first multi-chain memecoin copy-trading engine.

The core (monitoring, analysis, signal logic, risk management, paper
execution, ledger) uses ONLY the Python standard library (urllib, json,
sqlite3, tomllib, ...). Live on-chain execution is an isolated, opt-in
module that lazily imports web3/eth-account and is disabled by default.

Nothing in this package ever reads, stores, or logs a private key from
the core; the private key is only ever read from an environment variable
inside copytrader.executor_live, and only when live mode is explicitly
enabled by the operator.
"""

__version__ = "0.1.0"
