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
import math
import secrets
import socket
import threading
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import unquote, urlparse

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checking
    from maya.journal.store import TradeJournal
    from maya.nodes import TradingNodes

from maya.config import RiskLimits, Settings


MAX_HALT_BODY = 4096       # bytes accepted by POST /api/halt
MAX_HALT_REASON = 200      # characters of the halt reason that are kept


def json_safe(value: Any) -> Any:
    """Replace non-finite floats with None so the payload is valid JSON.

    Python serialises inf/NaN as `Infinity`/`NaN`, which browsers and the
    Android app reject outright; one such number anywhere in a snapshot made
    the whole dashboard unreadable."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


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
            # "scanner" when maya.scan_run is driving (any coin), else "single".
            "agent": "scanner" if getattr(nodes, "last_scan", None) is not None else "single",
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
            # For "how close to each limit" read-outs (Telegram /limits).
            "day_start_equity": round(acc.day_start_equity, 2),
            "week_start_equity": round(acc.week_start_equity, 2),
            "cooldown_until_ts": acc.cooldown_until_ts,
        },
        # The hard limits themselves, read from RiskLimits so a read-out can
        # never disagree with what the risk manager enforces.
        "limits": {
            "max_trades_per_day": RiskLimits.MAX_TRADES_PER_DAY,
            "max_consecutive_losses": RiskLimits.MAX_CONSECUTIVE_LOSSES,
            "cooldown_minutes": RiskLimits.COOLDOWN_MINUTES,
            "daily_loss_limit_pct": RiskLimits.DAILY_LOSS_LIMIT_PCT,
            "weekly_loss_limit_pct": RiskLimits.WEEKLY_LOSS_LIMIT_PCT,
            "max_concurrent_positions": RiskLimits.MAX_CONCURRENT_POSITIONS,
        },
        "position": position,
        "run": run,
        "lifetime": lifetime,
        "candles_processed": processed,
    }
    # Scanner agent only: the latest scan, best first, with each coin's fate.
    scan = getattr(nodes, "last_scan", None)
    if scan is not None:
        snapshot["scan_ts"] = getattr(nodes, "last_scan_ts", 0)
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
                 halt_fn: Callable[[str], None],
                 chart_fn: Callable[[str, int], dict[str, Any]] | None = None) -> None:
        self.settings = settings
        self.snapshot_fn = snapshot_fn
        self.trades_fn = trades_fn
        self.halt_fn = halt_fn
        # Read-only drawing payload (maya.viz.chart). Optional so a server
        # built without it keeps working; /api/chart is then a 404.
        self.chart_fn = chart_fn
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
            # drown the trading log at a 2s dashboard poll. The request line
            # carries "?token=..." when the dashboard is opened from a link,
            # so the token is scrubbed before anything is logged.
            logger.debug("api {}", (fmt % args).replace(server.token, "<token>"))

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
            self._send(status, json.dumps(json_safe(payload), allow_nan=False).encode(),
                       "application/json")

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
                if route == "/api/chart" and server.chart_fn is not None:
                    # Bounded so a typo cannot make the trading process draw
                    # thousands of bars on every dashboard refresh.
                    bars = max(20, min(int(query.get("bars", "150")), 300))
                    tf = unquote(query.get("tf", "base"))
                    coin = unquote(query.get("coin", "")) or None
                    try:
                        data = (server.chart_fn(tf, bars, coin) if coin
                                else server.chart_fn(tf, bars))
                    except ValueError as exc:
                        return self._json(400, {"ok": False, "error": str(exc)})
                    return self._json(200, {"ok": True, "data": data})
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
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                # The body is only a short reason. Refuse anything large rather
                # than read an unbounded amount into the trading process.
                if not 0 <= length <= MAX_HALT_BODY:
                    return self._json(413, {"ok": False, "error": "body too large"})
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    reason = (json.loads(raw or b"{}").get("reason")
                              or "halted from dashboard")
                except (json.JSONDecodeError, AttributeError):
                    reason = "halted from dashboard"
                # The halt still happens; only the stored text is bounded.
                server.halt_fn(str(reason)[:MAX_HALT_REASON])
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
  canvas{width:100%;height:280px;display:block}
  .tfs{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
  .tfs a{font-size:12px;padding:5px 11px;border-radius:999px;border:1px solid var(--line);
         color:var(--dim);text-decoration:none}
  .tfs a.on{color:var(--fg);border-color:var(--accent)}
  .notes{font-size:12px;color:var(--dim);margin-top:6px}
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
// Every string from data goes through esc() before it reaches innerHTML: coin
// names come from the venue's public list and halt reasons from a request, so
// unescaped they could run script on a page that holds the token.
const esc = v => String(v ?? '').replace(/[&<>"']/g,
  ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const el = document.getElementById('app');

async function api(path, opts={}){
  const r = await fetch(path, {...opts, headers:{'Authorization':'Bearer '+token}});
  if(r.status === 401) throw new Error('Unauthorized — check the token in the URL');
  const j = await r.json();
  if(!j.ok) throw new Error(j.error || 'request failed');
  return j.data;
}

let chartTf = 'base';

function chartCard(ch){
  if(!ch) return '';
  const tfs = (ch.available || []).map((t, i) => {
    const key = i === 0 ? 'base' : t;
    return `<a href="#" data-tf="${esc(key)}" class="${key === chartTf ? 'on' : ''}">${esc(t)}</a>`;
  }).join('');
  const dr = ch.drawings || {};
  const notes = [];
  if(dr.formation) notes.push(dr.formation.replace(/_/g, ' '));
  (dr.patterns || []).forEach(p => notes.push(`${p.name.replace(/_/g, ' ')} (${p.status})`));
  (dr.breakouts || []).forEach(b => notes.push(`breakout ${b.direction}: ${b.kind.replace(/_/g, ' ')}`));
  return `<div class="card"><h2>Chart — ${esc(ch.symbol)} ${esc(ch.timeframe)}</h2><div class="tfs">${tfs}</div>
    <canvas id="chart"></canvas>
    <div class="notes">${notes.length ? esc(notes.join(' · ')) : 'no pattern on this chart'}
    — drawings are readings, not trade signals.</div></div>`;
}

function drawChart(ch){
  const cv = document.getElementById('chart');
  if(!cv || !ch || !ch.candles.length) return;
  const css = getComputedStyle(document.documentElement);
  const col = k => css.getPropertyValue(k).trim();
  const UP = col('--up'), DN = col('--down'), DIM = col('--dim'), AC = col('--accent'), WR = col('--warn');
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  const rows = ch.candles, n = rows.length, padR = 54;
  const idx = {}; rows.forEach((r, i) => idx[r[0]] = i);
  const step = n > 1 ? rows[1][0] - rows[0][0] : 60000;
  const xi = i => (i + 0.5) * (W - padR) / n;
  const xt = t => xi(t in idx ? idx[t] : (t - rows[0][0]) / step);
  let lo = Math.min(...rows.map(r => r[3])), hi = Math.max(...rows.map(r => r[2]));
  const pos = ch.position;
  if(pos) [pos.entry, pos.stop_loss, pos.take_profit].forEach(v => { lo = Math.min(lo, v); hi = Math.max(hi, v); });
  const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
  const y = p => H - (p - lo) / (hi - lo) * H;
  const inView = p => p >= lo && p <= hi;
  const label = (txt, py, c) => { g.fillStyle = c; g.font = '10px system-ui'; g.fillText(txt, W - padR + 3, py + 3); };
  const seg = (pts, c, w, dash) => { g.strokeStyle = c; g.lineWidth = w; g.setLineDash(dash || []);
    g.beginPath(); pts.forEach(([t, v], k) => k ? g.lineTo(xt(t), y(v)) : g.moveTo(xt(t), y(v))); g.stroke(); g.setLineDash([]); };

  (ch.zones || []).forEach(z => {
    g.globalAlpha = 0.08 + 0.2 * z.strength; g.fillStyle = z.kind === 'support' ? UP : DN;
    g.fillRect(0, y(z.upper), W - padR, Math.max(y(z.lower) - y(z.upper), 1)); g.globalAlpha = 1;
    if(inView(z.center)) label(`${z.kind[0].toUpperCase()}x${z.touches}`, y(z.center), z.kind === 'support' ? UP : DN);
  });
  const bw = Math.max((W - padR) / n * 0.65, 1);
  rows.forEach((r, i) => {
    const [, o, h, l, c] = r, x = xi(i); g.strokeStyle = g.fillStyle = c >= o ? UP : DN;
    g.lineWidth = 1; g.beginPath(); g.moveTo(x, y(h)); g.lineTo(x, y(l)); g.stroke();
    g.fillRect(x - bw / 2, y(Math.max(o, c)), bw, Math.max(Math.abs(y(o) - y(c)), 1));
  });
  const dr = ch.drawings || {};
  if(dr.fib) Object.entries(dr.fib.levels).forEach(([k, v]) => {
    if(!inView(v)) return; seg([[rows[0][0], v], [rows[n - 1][0], v]], WR, 0.7, [2, 4]); label('fib ' + k, y(v), WR);
  });
  if(dr.support_line) seg(dr.support_line.points, UP, 1.6);
  if(dr.resistance_line) seg(dr.resistance_line.points, DN, 1.6);
  (dr.patterns || []).forEach(p => {
    const c = p.direction === 'bullish' ? UP : DN;
    seg(p.points, c, 1.2, [5, 3]);
    if(p.neckline) seg(p.neckline, c, 1, [8, 3, 2, 3]);
    const [t, v] = p.points[p.points.length - 1];
    g.fillStyle = c; g.font = '11px system-ui'; g.fillText(p.name.replace(/_/g, ' '), Math.min(xt(t), W - padR - 110), y(v) - 6);
  });
  if(pos) [['entry', pos.entry, AC], ['SL', pos.stop_loss, DN], ['TP', pos.take_profit, UP]].forEach(([k, v, c]) => {
    seg([[rows[0][0], v], [rows[n - 1][0], v]], c, 1.2); label(k, y(v), c);
  });
  (dr.breakouts || []).forEach(b => {
    if(!inView(b.level)) return; const x = xi(n - 1), py = y(b.level);
    g.fillStyle = AC; g.beginPath();
    if(b.direction === 'up'){ g.moveTo(x, py - 14); g.lineTo(x - 5, py - 6); g.lineTo(x + 5, py - 6); }
    else { g.moveTo(x, py + 14); g.lineTo(x - 5, py + 6); g.lineTo(x + 5, py + 6); }
    g.fill();
  });
  if(dr.formation){ g.fillStyle = AC; g.font = 'bold 11px system-ui'; g.fillText(dr.formation.replace(/_/g, ' '), 6, 14); }
  label(rows[n - 1][4].toFixed(2), y(rows[n - 1][4]), DIM);
}

function render(d, trades, ch){
  const a = d.account, p = d.position, c = d.config;
  document.getElementById('cfg').textContent =
    `${c.mode} · ${c.venue} · ${c.agent === 'scanner' ? 'scanner' : c.symbol} ${c.timeframe} · ${c.leverage}x`;
  const cls = a.pnl_usdt > 0 ? 'up' : a.pnl_usdt < 0 ? 'down' : '';
  const sign = a.pnl_usdt > 0 ? '+' : '';

  let html = `<div class="card">
    <h2>Equity</h2>
    <div class="equity">${money(a.equity)}</div>
    <div class="delta ${cls}">${sign}${money(a.pnl_usdt)} (${sign}${a.pnl_pct.toFixed(2)}%)</div>
    <div class="kv" style="margin-top:10px"><span>System</span>
      <span><span class="state ${esc(a.state)}">${esc(a.state.toUpperCase())}</span></span></div>
    ${a.halt_reasons.length ? `<div class="kv"><span>Reason</span>
      <span class="down">${esc(a.halt_reasons.join(', '))}</span></div>` : ''}
  </div>`;

  html += p ? `<div class="card">
    <h2>Open position</h2>
    <div class="kv"><span>${esc(p.direction.toUpperCase())} ${esc(p.symbol)}</span>
      <span>${esc(p.leverage)}x · ${esc(p.setup)}</span></div>
    <div class="grid" style="margin-top:6px">
      <div class="kv"><span>Entry</span><span>${p.entry_price.toFixed(2)}</span></div>
      <div class="kv"><span>Size</span><span>${esc(p.quantity)}</span></div>
      <div class="kv"><span>Stop</span><span class="down">${p.stop_loss.toFixed(2)}</span></div>
      <div class="kv"><span>Target</span><span class="up">${p.take_profit.toFixed(2)}</span></div>
      <div class="kv"><span>Notional</span><span>${money(p.notional)}</span></div>
      <div class="kv"><span>Breakeven</span><span>${p.moved_to_breakeven?'yes':'no'}</span></div>
    </div></div>`
    : `<div class="card"><h2>Open position</h2><div class="flat">Flat — no open position</div></div>`;

  html += chartCard(ch);

  const r = d.run;
  html += `<div class="card"><h2>This run</h2>
    <div class="grid">
      <div class="kv"><span>Trades</span><span>${r.trades||0}</span></div>
      <div class="kv"><span>Win rate</span><span>${r.trades?r.win_rate+'%':'—'}</span></div>
      <div class="kv"><span>PnL</span><span>${r.trades?money(r.total_pnl_usdt):'—'}</span></div>
      <div class="kv"><span>Profit factor</span><span>${!r.trades?'—':(r.profit_factor ?? '∞ (no losses)')}</span></div>
      <div class="kv"><span>Trades today</span><span>${a.trades_today}</span></div>
      <div class="kv"><span>Loss streak</span><span>${a.consecutive_losses}</span></div>
    </div></div>`;

  if(trades && trades.length){
    html += `<div class="card"><h2>Recent trades</h2><table>
      <tr><th>Coin</th><th>Setup</th><th>Side</th><th style="text-align:right">PnL</th></tr>
      ${trades.map(t=>`<tr><td>${esc((t.symbol || '').replace(/USDT$/, ''))}</td>
        <td>${esc((t.setup_name || '').replace(/_/g, ' '))}</td><td>${esc(t.direction)}</td>
        <td style="text-align:right" class="${t.pnl_usdt>=0?'up':'down'}">
        ${money(t.pnl_usdt)}</td></tr>`).join('')}
    </table></div>`;
  }

  const halted = a.state === 'halted';
  const lifts = halted && a.halt_reasons.length &&
    a.halt_reasons.every(r => /^(daily|weekly) loss limit/.test(r));
  const note = !halted ? `Halting stops new trades. It does not close an open position;
    do that on the exchange.` : lifts ? `Stopped by a loss limit. It lifts by itself when
    the next day (00:00 UTC) or week starts.` : `Halted. A person must check and restart Maya.`;
  html += `<div class="card"><button id="halt" ${halted?'disabled':''}>
    ${halted?'System halted':'HALT TRADING'}</button>
    <div class="flat" style="padding-top:10px">${note}</div></div>`;

  el.innerHTML = html;
  drawChart(ch);
  document.querySelectorAll('.tfs a').forEach(t => t.onclick = e => {
    e.preventDefault(); chartTf = t.dataset.tf; tick();
  });
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
    // The chart is optional: an older server without /api/chart, or a
    // timeframe with too few bars yet, must not blank the rest of the page.
    const [d, trades, ch] = await Promise.all([api('/api/status'), api('/api/trades?limit=8'),
      api('/api/chart?tf=' + encodeURIComponent(chartTf) + '&bars=120').catch(() => null)]);
    render(d, trades, ch);
    document.getElementById('foot').textContent =
      'updated ' + new Date().toLocaleTimeString() + ' · ' + d.candles_processed + ' candles';
  }catch(e){
    document.getElementById('foot').textContent = e.message;
    if(!el.innerHTML) el.innerHTML = `<div class="card"><div class="flat down">${esc(e.message)}</div></div>`;
  }
}
tick(); setInterval(tick, 3000);
</script></body></html>
"""
