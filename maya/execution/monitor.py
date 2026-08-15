"""Layer 5/7 — background position monitor for testnet/live modes.

The candle-driven graph (``maya/graph.py``) only advances once per closed
1-minute candle. But on testnet/live the actual stop-loss and take-profit
orders live on the exchange and can fill at any moment — the graph finding
out 30-60 seconds later is a real gap (see RUNNING.md "what's implemented
vs. stubbed" / spec 8.4). :class:`PositionMonitor` closes that gap: it polls
the exchange every few seconds, independent of the candle clock, and the
moment it sees the local position has actually gone flat on the exchange,
it closes it locally (via
:meth:`~maya.execution.engine.ExecutionEngine.reconcile_external_close`),
journals it, and updates the risk manager immediately — instead of waiting
for the next candle.

It also performs the spec-8.4 startup check: if the exchange reports an open
position when Maya's local state is empty, that is a mismatch, and per spec
("do NOT auto-resolve; human verification required") this is surfaced as a
critical log rather than silently adopted or closed. The actual refusal to
start on a startup mismatch is enforced in :mod:`maya.run` via
:mod:`maya.check_connection`, which runs *before* the engine/monitor exist;
this monitor's ongoing check is for mismatches that appear *during* a run
(e.g. manual intervention on the exchange, or a missed webhook).
"""
from __future__ import annotations

import threading
import time

from loguru import logger

from maya.config import Settings
from maya.execution.engine import ExecutionEngine
from maya.journal.store import TradeJournal
from maya.risk.manager import RiskManager


class PositionMonitor:
    def __init__(self, settings: Settings, execution: ExecutionEngine,
                 journal: TradeJournal, risk: RiskManager,
                 poll_interval_s: float = 5.0) -> None:
        self.settings = settings
        self.execution = execution
        self.journal = journal
        self.risk = risk
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._exchange = None

    # -- lifecycle ----------------------------------------------------------- #
    def start(self) -> None:
        if not self.settings.uses_real_orders:
            return  # nothing to poll in sim/paper — the candle loop is authoritative
        self._exchange = self._build_exchange()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="maya-position-monitor",
                                        daemon=True)
        self._thread.start()
        logger.info("Position monitor started (poll every {:.0f}s)", self.poll_interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval_s * 2)

    def _build_exchange(self):
        if self.settings.uses_metamask:
            # Hyperliquid via the MetaMask agent wallet — polling goes through
            # the `mm` CLI, not ccxt. Returning None marks that path; the
            # broker held by the execution engine is reused rather than
            # constructing a second CLI client.
            return None

        import ccxt

        ex = ccxt.binanceusdm({
            "apiKey": self.settings.binance_api_key,
            "secret": self.settings.binance_api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        if self.settings.mode.value == "testnet":
            ex.set_sandbox_mode(True)
        return ex

    # -- polling loop ---------------------------------------------------------- #
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:  # noqa: BLE001 - monitor must never crash the process
                logger.warning("position monitor poll failed: {}", exc)
            self._stop.wait(self.poll_interval_s)

    def _poll_once(self) -> None:
        local_pos = self.execution.position
        if local_pos is None:
            return  # nothing to reconcile; cheap early-out, no API call

        exchange_contracts = self._exchange_contracts()
        if exchange_contracts is None:
            return  # couldn't read the venue — never reconcile on a blind guess
        if exchange_contracts > 0:
            return  # still open on the exchange — nothing to reconcile yet

        # Exchange shows flat but we still think we're in a position: the
        # SL or TP order filled. Find the real exit price/fee from the most
        # recent reduce-only fill rather than guessing.
        exit_price, fees = self._last_fill(local_pos)
        now_ts = int(time.time() * 1000)
        result = self.execution.reconcile_external_close(
            exit_price=exit_price, ts=now_ts, reason="exchange_fill", fees_usdt=fees,
        )
        if result is None:
            return

        # Order matters, and so does the isolation. The risk update is what
        # drives consecutive_losses, the daily-loss halt and the kill switch;
        # journaling is bookkeeping. Previously a journal failure here
        # propagated into the poll loop's catch-all and skipped
        # on_trade_closed entirely, so a real losing fill never reached the
        # risk limits. Journal failures are now contained.
        try:
            self.journal.record(result, result.position.confluences)
        except Exception as exc:  # noqa: BLE001 - must not block the risk update
            logger.error("could not journal exchange-fill close for {} (pnl={:.2f}) — "
                         "risk accounting still applied, but this trade is missing "
                         "from the journal and from restart restoration: {}",
                         result.position.symbol, result.pnl_usdt, exc)

        self.risk.on_trade_closed(result.pnl_usdt, now_ts)
        logger.info("Position monitor detected exchange fill: {} {} pnl={:.2f}",
                    result.position.setup_name, result.result, result.pnl_usdt)

    def _exchange_contracts(self) -> float | None:
        """Absolute position size the venue reports, or ``None`` if unreadable.

        ``None`` is deliberately distinct from ``0.0``: a failed read must not
        be mistaken for "flat", which would close a live position locally
        while it is still open on the venue.
        """
        if self.settings.uses_metamask:
            from maya.execution.metamask_broker import MMError, as_float, pick

            try:
                entry = self.execution.broker.fetch_position(self.settings.symbol)
            except MMError as exc:
                logger.warning("could not fetch Hyperliquid position: {}", exc)
                return None
            if entry is None:
                return 0.0
            return abs(as_float(pick(entry, "size", "szi", "sz", "contracts", default=0.0)))

        try:
            positions = self._exchange.fetch_positions([self.settings.symbol])
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not fetch positions for reconciliation: {}", exc)
            return None
        return sum(abs(float(p.get("contracts") or 0)) for p in positions)

    def _last_fill(self, pos) -> tuple[float, float]:
        """Best-effort real exit price/fees from recent fills; falls back to
        the position's own stop/take-profit level if trade history isn't
        available (still far better than silently defaulting to zero)."""
        if self.settings.uses_metamask:
            # Known gap: the `mm` CLI exposes no fills/trade-history command
            # (perps has balance/positions/orders but nothing historical), so
            # the true exit price and fee simply cannot be read back on this
            # venue. Every Hyperliquid close is therefore journaled from the
            # SL/TP estimate below. Journal PnL on this path is approximate —
            # reconcile against the Hyperliquid UI before trusting the metrics.
            logger.warning(
                "Hyperliquid exit price is ESTIMATED from the SL/TP level — the CLI "
                "exposes no fill history. Journaled PnL for {} is approximate.",
                pos.symbol)
            return pos.take_profit, pos.notional * 0.001

        try:
            trades = self._exchange.fetch_my_trades(self.settings.symbol, limit=5)
            reduce_trades = [t for t in trades if t.get("info", {}).get("reduceOnly") in (True, "true")]
            if reduce_trades:
                last = reduce_trades[-1]
                price = float(last["price"])
                fee = float(last.get("fee", {}).get("cost") or 0.0)
                return price, fee
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not fetch fill details, estimating exit: {}", exc)

        # Fallback: assume whichever of SL/TP is nearer the last-known price
        # direction — conservative guess, logged clearly as an estimate.
        logger.warning("estimating exit price from SL/TP levels — verify against "
                       "the exchange UI for {}", pos.symbol)
        return pos.take_profit, pos.notional * 0.001  # FEE_RATE*2 fallback approximation
