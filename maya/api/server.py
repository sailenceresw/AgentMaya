"""Layer 7 — read-mostly monitoring API plus a phone-friendly dashboard.

The spec lists Telegram/dashboard as the Layer 7 interface; until now L7 was
structured logging only, which means the only way to see what the agent is
doing is to watch a terminal. This exposes the same state over HTTP so a
phone (or the Android client in ``android/``) can show it.

**The agent does not run here.** This is a window onto a running agent, served
from the process that is actually trading. That separation is deliberate: the
engine needs a stable host and an uninterrupted event loop, which a phone
cannot provide (Android suspends background processes, and a suspended process
holding a leveraged position is a position with no stop management).

Deliberately built on :mod:`http.server` from the standard library — adding
FastAPI/uvicorn to a trading process for a status page is dependency weight
with its own failure modes, and this serves a handful of small JSON documents.

Safety properties, since this puts a trading system on a network socket:

* **One** state-changing endpoint exists (``POST /api/halt``) and it only ever
  moves the system to a *safer* state. There is no "resume", no "open trade",
  and no way to change risk limits. The worst a stolen token can do is stop
  the agent trading.
* A bearer token is required for everything except ``/api/health``. If no
  token is configured the server refuses to start rather than defaulting to
  open.
* Binds to ``127.0.0.1`` unless told otherwise, so it is not on the LAN by
  accident.
* Runs as a daemon thread and never raises into the trading loop.
"""
from __future__ import annotations

import http.server
import json
import secrets
import socket
import threading
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checking
    from maya.journal.store import TradeJournal
    from maya.nodes import TradingNodes

from maya.config import Settings


def build_snapshot(settings: Settings, nodes: "TradingNodes",
                   journal: "TradeJournal", processed: int = 0) -> dict[str, Any]:
    """Everything the dashboard needs, in one document.

    One endpoint rather than several so the phone makes a single request per
    refresh — it is on a mobile radio, and the values must be mutually
    consistent (equity and open position read at different instants can
    disagree).
    """
    acc = nodes.risk.account
    pos = nodes.execution.position
    run = journal.metrics(session_only=True)
    lifetime = journal.metrics()

    position: dict[str, Any] | None = None
    if pos is not None:
        position = {
            "symbol": pos.symbol,
            "direction": pos.direction,
            "setup": pos.setup_name,
            "entry_price": pos.entry_price,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "quantity": pos.quantity,
            "notional": pos.notional,
            "leverage": pos.leverage,
            "moved_to_breakeven": pos.moved_to_breakeven,
            "confluence_score": pos.confluence_score,
            "rr_planned": pos.rr_planned,
        }

    from maya.fees import FeeModel

    fees = FeeModel.for_settings(settings)
    equity_delta = acc.equity - acc.start_equity
    snapshot = {
        "config": {
            "mode": settings.mode.value,
            "venue": settings.venue.value,
            "symbol": settings.symbol,
            "timeframe": settings.timeframe,
            "leverage": settings.leverage,
            "sizing_mode": settings.sizing_mode,
            "margin_per_trade_usd": settings.margin_per_trade_usd,
            "risk_per_trade_usd": settings.risk_per_trade_usd,
            # What one round trip costs, so the phone can judge any target.
            "round_trip_fee_pct": round(fees.worst_round_trip() * 100, 4),
            "min_net_rr": settings.min_net_rr,
        },
        "account": {
            "equity": round(acc.equity, 2),
            "start_equity": round(acc.start_equity, 2),
            "pnl_usdt": round(equity_delta, 2),
            "pnl_pct": round(equity_delta / acc.start_equity * 100, 3) if acc.start_equity else 0.0,
            "state": acc.state.value,
            "halt_reasons": list(acc.halt_reasons),
            "open_positions": acc.open_positions,
            "trades_today": acc.trades_today,
            "consecutive_losses": acc.consecutive_losses,
        },
        "position": position,
        "run": run,
        "lifetime": lifetime,
        "candles_processed": processed,
    }
    # Scanner agent only: the latest scan, best first, with each coin's fate.
    scan = getattr(nodes, "last_scan", None)
    if scan is not None:
        snapshot["scan"] = [
            {
                "coin": c.market.coin,
                "setup": c.intent.setup_name if c.intent else None,
                "direction": c.intent.direction if c.intent else None,
                "net_rr": round(c.net_rr, 2),
                "fee_share_pct": round(c.fee_share * 100, 1) if c.intent else None,
                "desk": c.desk_verdict,
                "status": "trade" if c.tradable else c.rejected,
            }
            for c in scan
        ]
    return snapshot


class MonitorServer:
    """Threaded HTTP server exposing the snapshot, trade list and halt switch."""

    def __init__(self, settings: Settings, snapshot_fn: Callable[[], dict[str, Any]],
                 trades_fn: Callable[[int], list[dict[str, Any]]],
                 halt_fn: Callable[[str], None]) -> None:
        self.settings = settings
        self.snapshot_fn = snapshot_fn
        self.trades_fn = trades_fn
        self.halt_fn = halt_fn
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        token = settings.api_token
        if not token:
            raise ValueError(
                "MAYA_API_TOKEN is not set. The monitoring API controls a trading "
                "system, so it refuses to run unauthenticated. Generate one with: "
                "python -c \"import secrets; print(secrets.token_urlsafe(24))\""
            )
        self.token = token

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        handler = _make_handler(self)
        try:
            self._httpd = http.server.ThreadingHTTPServer(
                (self.settings.api_host, self.settings.api_port), handler)
        except OSError as exc:
            # A dashboard that cannot bind must never stop the agent trading.
            logger.error("monitoring API could not bind {}:{} — continuing without "
                         "it: {}", self.settings.api_host, self.settings.api_port, exc)
            return

        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="maya-monitor-api", daemon=True)
        self._thread.start()

        host = self.settings.api_host
        logger.info("Monitoring API on http://{}:{}", host, self.settings.api_port)
        if host in ("0.0.0.0", "::"):
            lan = _lan_ip()
            logger.warning(
                "API is bound to ALL interfaces — anything on your network can "
                "reach it. Open the dashboard at http://{}:{}/?token=<MAYA_API_TOKEN>",
                lan or "<your-ip>", self.settings.api_port)
        else:
            logger.info("Bound to loopback only. Set MAYA_API_HOST=0.0.0.0 to reach "
                        "it from your phone on the same network.")

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self.settings.api_port


def _lan_ip() -> str | None:
    """Best-effort LAN address, for printing a reachable URL."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _make_handler(server: MonitorServer):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MayaMonitor/1.0"

        # -- plumbing --------------------------------------------------- #
        def log_message(self, fmt: str, *args: Any) -> None:
            # BaseHTTPRequestHandler logs every request to stderr, which would
            # drown the trading log at a 2s dashboard poll.
            logger.debug("api {}", fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _authorized(self, query: dict[str, str]) -> bool:
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else query.get("token", "")
            # Constant-time compare: this token is the only thing standing
            # between the network and the halt switch.
            return bool(supplied) and secrets.compare_digest(supplied, server.token)

        # -- routes ------------------------------------------------------ #
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            query = dict(
                part.split("=", 1) for part in parsed.query.split("&")
                if "=" in part
            )
            route = parsed.path.rstrip("/") or "/"

            if route == "/api/health":
                # Unauthenticated on purpose: lets the phone app tell "wrong
                # address" apart from "wrong token" without leaking anything.
                return self._json(200, {"ok": True, "service": "maya-monitor"})

            if not self._authorized(query):
                return self._json(401, {"ok": False, "error": "unauthorized"})

            try:
                if route in ("/", "/dashboard"):
                    return self._send(200, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
                if route == "/api/status":
                    return self._json(200, {"ok": True, "data": server.snapshot_fn()})
                if route == "/api/trades":
                    limit = int(query.get("limit", "20"))
                    return self._json(200, {"ok": True, "data": server.trades_fn(limit)})
            except Exception as exc:  # noqa: BLE001 - never 500 silently
                logger.warning("monitoring API error on {}: {}", route, exc)
                return self._json(500, {"ok": False, "error": str(exc)})

            return self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            query = dict(
                part.split("=", 1) for part in parsed.query.split("&")
                if "=" in part
            )
            if not self._authorized(query):
                return self._json(401, {"ok": False, "error": "unauthorized"})

            if parsed.path.rstrip("/") == "/api/halt":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    reason = (json.loads(raw or b"{}").get("reason")
                              or "halted from dashboard")
                except json.JSONDecodeError:
                    reason = "halted from dashboard"
                server.halt_fn(str(reason))
                logger.critical("HALT requested via monitoring API: {}", reason)
                return self._json(200, {"ok": True, "data": server.snapshot_fn()})

            return self._json(404, {"ok": False, "error": "not found"})

    return Handler


# --------------------------------------------------------------------------- #
# Dashboard. Inlined rather than shipped as a static file so the running agent
# has no filesystem dependency, and phone-first because that is where it is
# read: single column, large touch targets, dark by default.
# --------------------------------------------------------------------------- #
DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0f14">
<title>Maya</title>
<style>
  :root{
    --bg:#0b0f14; --card:#141b24; --line:#223041; --fg:#e6edf3; --dim:#8b9bb0;
    --up:#3fb950; --down:#f85149; --warn:#d29922; --accent:#58a6ff;
  }
  @media (prefers-color-scheme: light){
    :root{--bg:#f5f7fa;--card:#fff;--line:#dde3ea;--fg:#111820;--dim:#5c6b7f}
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:16px/1.45 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif;
       padding:16px;padding-bottom:calc(16px + env(safe-area-inset-bottom))}
  header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
  h1{font-size:18px;margin:0;letter-spacing:.3px}
  .pill{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:14px 16px;margin-bottom:12px}
  .equity{font-size:34px;font-weight:650;letter-spacing:-.5px;margin:2px 0}
  .delta{font-size:15px;font-weight:600}
  .up{color:var(--up)} .down{color:var(--down)} .warn{color:var(--warn)}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .kv{display:flex;justify-content:space-between;gap:12px;padding:5px 0;font-size:14px}
  .kv span:first-child{color:var(--dim)}
  .kv span:last-child{font-variant-numeric:tabular-nums;text-align:right}
  h2{font-size:12px;text-transform:uppercase;letter-spacing:.9px;color:var(--dim);
     margin:0 0 8px;font-weight:600}
  .state{display:inline-block;padding:3px 10px;border-radius:6px;font-size:13px;font-weight:600}
  .running{background:rgba(63,185,80,.15);color:var(--up)}
  .halted{background:rgba(248,81,73,.15);color:var(--down)}
  .cooldown{background:rgba(210,153,34,.15);color:var(--warn)}
  button{width:100%;padding:15px;border-radius:12px;border:1px solid var(--down);
         background:transparent;color:var(--down);font-size:16px;font-weight:600;
         cursor:pointer;-webkit-tap-highlight-color:transparent}
  button:active{background:rgba(248,81,73,.12)}
  button:disabled{opacity:.4;cursor:default}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--dim);font-weight:500;padding-bottom:6px}
  td{padding:7px 0;border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
  .flat{color:var(--dim);text-align:center;padding:14px 0;font-size:14px}
  footer{color:var(--dim);font-size:12px;text-align:center;margin-top:14px}
</style></head><body>
<header>
  <h1>Maya</h1>
  <span class="pill" id="cfg">connecting…</span>
</header>
<div id="app"></div>
<footer id="foot"></footer>
<script>
const qs = new URLSearchParams(location.search);
const token = qs.get('token') || '';
const money = n => (n<0?'-':'') + '$' + Math.abs(n).toFixed(2);
const el = document.getElementById('app');

async function api(path, opts={}){
  const r = await fetch(path, {...opts, headers:{'Authorization':'Bearer '+token}});
  if(r.status === 401) throw new Error('Unauthorized — check the token in the URL');
  const j = await r.json();
  if(!j.ok) throw new Error(j.error || 'request failed');
  return j.data;
}

function render(d, trades){
  const a = d.account, p = d.position, c = d.config;
  document.getElementById('cfg').textContent =
    `${c.mode} · ${c.venue} · ${c.symbol} ${c.timeframe} · ${c.leverage}x`;
  const cls = a.pnl_usdt > 0 ? 'up' : a.pnl_usdt < 0 ? 'down' : '';
  const sign = a.pnl_usdt > 0 ? '+' : '';

  let html = `<div class="card">
    <h2>Equity</h2>
    <div class="equity">${money(a.equity)}</div>
    <div class="delta ${cls}">${sign}${money(a.pnl_usdt)} (${sign}${a.pnl_pct.toFixed(2)}%)</div>
    <div class="kv" style="margin-top:10px"><span>System</span>
      <span><span class="state ${a.state}">${a.state.toUpperCase()}</span></span></div>
    ${a.halt_reasons.length ? `<div class="kv"><span>Reason</span>
      <span class="down">${a.halt_reasons.join(', ')}</span></div>` : ''}
  </div>`;

  html += p ? `<div class="card">
    <h2>Open position</h2>
    <div class="kv"><span>${p.direction.toUpperCase()} ${p.symbol}</span>
      <span>${p.leverage}x · ${p.setup}</span></div>
    <div class="grid" style="margin-top:6px">
      <div class="kv"><span>Entry</span><span>${p.entry_price.toFixed(2)}</span></div>
      <div class="kv"><span>Size</span><span>${p.quantity}</span></div>
      <div class="kv"><span>Stop</span><span class="down">${p.stop_loss.toFixed(2)}</span></div>
      <div class="kv"><span>Target</span><span class="up">${p.take_profit.toFixed(2)}</span></div>
      <div class="kv"><span>Notional</span><span>${money(p.notional)}</span></div>
      <div class="kv"><span>Breakeven</span><span>${p.moved_to_breakeven?'yes':'no'}</span></div>
    </div></div>`
    : `<div class="card"><h2>Open position</h2><div class="flat">Flat — no open position</div></div>`;

  const r = d.run;
  html += `<div class="card"><h2>This run</h2>
    <div class="grid">
      <div class="kv"><span>Trades</span><span>${r.trades||0}</span></div>
      <div class="kv"><span>Win rate</span><span>${r.trades?r.win_rate+'%':'—'}</span></div>
      <div class="kv"><span>PnL</span><span>${r.trades?money(r.total_pnl_usdt):'—'}</span></div>
      <div class="kv"><span>Profit factor</span><span>${r.trades?r.profit_factor:'—'}</span></div>
      <div class="kv"><span>Trades today</span><span>${a.trades_today}</span></div>
      <div class="kv"><span>Loss streak</span><span>${a.consecutive_losses}</span></div>
    </div></div>`;

  if(trades && trades.length){
    html += `<div class="card"><h2>Recent trades</h2><table>
      <tr><th>Setup</th><th>Side</th><th style="text-align:right">PnL</th></tr>
      ${trades.map(t=>`<tr><td>${t.setup_name}</td><td>${t.direction}</td>
        <td style="text-align:right" class="${t.pnl_usdt>=0?'up':'down'}">
        ${money(t.pnl_usdt)}</td></tr>`).join('')}
    </table></div>`;
  }

  const halted = a.state === 'halted';
  html += `<div class="card"><button id="halt" ${halted?'disabled':''}>
    ${halted?'System halted':'HALT TRADING'}</button>
    <div class="flat" style="padding-top:10px">Halting stops new trades. It does not
    close an open position — do that on the exchange.</div></div>`;

  el.innerHTML = html;
  const btn = document.getElementById('halt');
  if(btn && !halted) btn.onclick = async () => {
    if(!confirm('Halt trading? The agent will stop opening new positions.')) return;
    btn.disabled = true; btn.textContent = 'Halting…';
    try{ await api('/api/halt', {method:'POST',
      body: JSON.stringify({reason:'halted from dashboard'})}); await tick(); }
    catch(e){ alert(e.message); btn.disabled = false; }
  };
}

async function tick(){
  try{
    const [d, trades] = await Promise.all([api('/api/status'), api('/api/trades?limit=8')]);
    render(d, trades);
    document.getElementById('foot').textContent =
      'updated ' + new Date().toLocaleTimeString() + ' · ' + d.candles_processed + ' candles';
  }catch(e){
    document.getElementById('foot').textContent = e.message;
    if(!el.innerHTML) el.innerHTML = `<div class="card"><div class="flat down">${e.message}</div></div>`;
  }
}
tick(); setInterval(tick, 3000);
</script></body></html>
"""
