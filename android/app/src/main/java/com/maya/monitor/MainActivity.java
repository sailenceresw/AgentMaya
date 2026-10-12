package com.maya.monitor;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Read-mostly client for a running Maya agent.
 *
 * <p>This app deliberately does <em>not</em> contain a trading engine. The
 * agent needs an uninterrupted event loop and a stable network, and Android
 * suspends background processes — a suspended process holding a leveraged
 * position is a position with nobody managing its stop. So the engine stays on
 * a machine that can actually host it and this is a window onto it.
 *
 * <p>The only state-changing call it can make is HALT, which moves the system
 * to a safer state. There is no "resume" and no way to open a trade from here,
 * by design: a phone left on a table should not be able to start trading.
 *
 * <p>Networking is plain {@link HttpURLConnection} on a small executor rather
 * than a library, so the APK carries no third-party dependencies.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "maya";
    private static final long POLL_MS = 3000;
    /** Short: the phone is on a radio and a stalled request must not wedge the poll. */
    private static final int TIMEOUT_MS = 5000;

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());

    private SharedPreferences prefs;
    private View setup, dashboard;
    private TextView configPill, equity, pnl, state, position, stats, trades, footer, setupError;
    private TextView scan;
    private View scanCard;
    private Button halt;

    private final Runnable poll = new Runnable() {
        @Override public void run() {
            refresh();
            ui.postDelayed(this, POLL_MS);
        }
    };

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        setup = findViewById(R.id.setup);
        dashboard = findViewById(R.id.dashboard);
        configPill = findViewById(R.id.configPill);
        equity = findViewById(R.id.equity);
        pnl = findViewById(R.id.pnl);
        state = findViewById(R.id.state);
        position = findViewById(R.id.position);
        stats = findViewById(R.id.stats);
        trades = findViewById(R.id.trades);
        scan = findViewById(R.id.scan);
        scanCard = findViewById(R.id.scanCard);
        footer = findViewById(R.id.footer);
        setupError = findViewById(R.id.setupError);
        halt = findViewById(R.id.halt);

        ((EditText) findViewById(R.id.host)).setText(prefs.getString("host", ""));
        ((EditText) findViewById(R.id.port)).setText(prefs.getString("port", "8787"));
        ((EditText) findViewById(R.id.token)).setText(prefs.getString("token", ""));

        findViewById(R.id.connect).setOnClickListener(v -> onConnect());
        findViewById(R.id.settings).setOnClickListener(v -> showSetup(null));
        halt.setOnClickListener(v -> confirmHalt());

        if (TextUtils.isEmpty(prefs.getString("host", ""))) showSetup(null);
        else showDashboard();
    }

    @Override protected void onResume() {
        super.onResume();
        // Only poll while visible: a 3s poll in the background would drain the
        // battery for information nobody is looking at.
        if (dashboard.getVisibility() == View.VISIBLE) ui.post(poll);
    }

    @Override protected void onPause() {
        super.onPause();
        ui.removeCallbacks(poll);
    }

    @Override protected void onDestroy() {
        super.onDestroy();
        io.shutdownNow();
    }

    // -- panels ----------------------------------------------------------- //
    private void showSetup(String error) {
        ui.removeCallbacks(poll);
        setup.setVisibility(View.VISIBLE);
        dashboard.setVisibility(View.GONE);
        if (error == null) {
            setupError.setVisibility(View.GONE);
        } else {
            setupError.setText(error);
            setupError.setVisibility(View.VISIBLE);
        }
    }

    private void showDashboard() {
        setup.setVisibility(View.GONE);
        dashboard.setVisibility(View.VISIBLE);
        ui.removeCallbacks(poll);
        ui.post(poll);
    }

    private void onConnect() {
        String host = text(R.id.host), port = text(R.id.port), token = text(R.id.token);
        if (TextUtils.isEmpty(host) || TextUtils.isEmpty(token)) {
            showSetup("Host and token are both required.");
            return;
        }
        prefs.edit().putString("host", host).putString("port", port)
                .putString("token", token).apply();

        // Probe before committing to the dashboard, so a typo reports itself
        // here instead of as a silently empty screen.
        io.execute(() -> {
            try {
                request("/api/health", "GET", null, false);
                ui.post(this::showDashboard);
            } catch (Exception e) {
                ui.post(() -> showSetup("Could not reach the agent at " + base()
                        + "\n\n" + friendly(e)
                        + "\n\nCheck the agent is running with MAYA_API_HOST=0.0.0.0 "
                        + "and that both devices are on the same network."));
            }
        });
    }

    private String text(int id) {
        return ((EditText) findViewById(id)).getText().toString().trim();
    }

    // -- networking -------------------------------------------------------- //
    private String base() {
        return "http://" + prefs.getString("host", "") + ":"
                + prefs.getString("port", "8787");
    }

    private String request(String path, String method, String body, boolean auth)
            throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(base() + path).openConnection();
        try {
            c.setRequestMethod(method);
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            if (auth) {
                c.setRequestProperty("Authorization",
                        "Bearer " + prefs.getString("token", ""));
            }
            if (body != null) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                try (OutputStream os = c.getOutputStream()) {
                    os.write(body.getBytes(StandardCharsets.UTF_8));
                }
            }
            int code = c.getResponseCode();
            if (code == 401) throw new Exception("Unauthorized — the API token is wrong.");
            InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            StringBuilder sb = new StringBuilder();
            if (in != null) {
                try (BufferedReader r = new BufferedReader(
                        new InputStreamReader(in, StandardCharsets.UTF_8))) {
                    for (String line; (line = r.readLine()) != null; ) sb.append(line);
                }
            }
            if (code >= 400) throw new Exception("HTTP " + code + ": " + sb);
            return sb.toString();
        } finally {
            c.disconnect();
        }
    }

    private void refresh() {
        io.execute(() -> {
            try {
                JSONObject status = new JSONObject(request("/api/status", "GET", null, true));
                JSONObject data = status.getJSONObject("data");
                JSONArray recent = new JSONObject(
                        request("/api/trades?limit=6", "GET", null, true)).getJSONArray("data");
                ui.post(() -> render(data, recent));
            } catch (Exception e) {
                ui.post(() -> footer.setText(friendly(e)));
            }
        });
    }

    private static String friendly(Exception e) {
        String m = e.getMessage();
        if (m == null || m.isEmpty()) return e.getClass().getSimpleName();
        if (m.contains("ECONNREFUSED") || m.contains("failed to connect")) {
            return "Connection refused — is the agent running?";
        }
        if (m.contains("timed out")) return "Timed out — check the IP address.";
        return m;
    }

    // -- rendering --------------------------------------------------------- //
    private void render(JSONObject d, JSONArray recent) {
        try {
            JSONObject cfg = d.getJSONObject("config");
            JSONObject acc = d.getJSONObject("account");

            configPill.setText(cfg.getString("mode") + " · " + cfg.getString("venue")
                    + " · " + cfg.getString("symbol") + " " + cfg.getString("timeframe")
                    + " · " + cfg.getInt("leverage") + "x");

            double delta = acc.getDouble("pnl_usdt");
            equity.setText(money(acc.getDouble("equity")));
            pnl.setText((delta >= 0 ? "+" : "") + money(delta)
                    + " (" + String.format(Locale.US, "%.2f", acc.getDouble("pnl_pct")) + "%)");
            pnl.setTextColor(delta > 0 ? 0xFF3FB950 : delta < 0 ? 0xFFF85149 : 0xFF8B9BB0);

            String st = acc.getString("state");
            state.setText(st.toUpperCase(Locale.US));
            state.setTextColor("halted".equals(st) ? 0xFFF85149
                    : "cooldown".equals(st) ? 0xFFD29922 : 0xFF3FB950);

            if (d.isNull("position")) {
                position.setText("Flat — no open position");
                position.setTextColor(0xFF8B9BB0);
            } else {
                JSONObject p = d.getJSONObject("position");
                position.setTextColor(0xFFE6EDF3);
                position.setText(
                        p.getString("direction").toUpperCase(Locale.US) + " "
                                + p.getString("symbol") + "  " + p.getInt("leverage") + "x\n"
                                + "Entry   " + fmt(p.getDouble("entry_price")) + "\n"
                                + "Stop    " + fmt(p.getDouble("stop_loss"))
                                + (p.getBoolean("moved_to_breakeven") ? "  (breakeven)" : "") + "\n"
                                + "Target  " + fmt(p.getDouble("take_profit")) + "\n"
                                + "Size    " + p.getDouble("quantity")
                                + "   " + money(p.getDouble("notional")) + " notional\n"
                                + "Setup   " + p.getString("setup"));
            }

            JSONObject run = d.getJSONObject("run");
            int n = run.optInt("trades", 0);
            stats.setText(n == 0
                    ? "No trades yet this run\nTrades today  " + acc.getInt("trades_today")
                      + "\nLoss streak   " + acc.getInt("consecutive_losses")
                    : "Trades        " + n
                      + "\nWin rate      " + run.optDouble("win_rate", 0) + "%"
                      + "\nPnL (net)     " + money(run.optDouble("total_pnl_usdt", 0))
                      + "\nFees paid     " + money(run.optDouble("total_fees", 0))
                      + "\nProfit factor " + (run.isNull("profit_factor")
                              ? "∞ (no losses)" : run.opt("profit_factor"))
                      + "\nTrades today  " + acc.getInt("trades_today")
                      + "\nLoss streak   " + acc.getInt("consecutive_losses"));

            if (recent.length() == 0) {
                trades.setText("None yet");
                trades.setTextColor(0xFF8B9BB0);
            } else {
                StringBuilder sb = new StringBuilder();
                for (int i = 0; i < recent.length(); i++) {
                    JSONObject t = recent.getJSONObject(i);
                    if (i > 0) sb.append('\n');
                    sb.append(String.format(Locale.US, "%-5s %-22s %8s",
                            t.getString("direction"),
                            truncate(t.getString("setup_name"), 22),
                            money(t.getDouble("pnl_usdt"))));
                }
                trades.setTextColor(0xFFE6EDF3);
                trades.setText(sb.toString());
            }

            // Scanner agent only: each coin from the latest scan and its fate.
            JSONArray scanned = d.optJSONArray("scan");
            if (scanned == null) {
                scanCard.setVisibility(View.GONE);
            } else {
                scanCard.setVisibility(View.VISIBLE);
                StringBuilder sb = new StringBuilder(String.format(Locale.US,
                        "fees %.2f%% round trip · net R:R ≥ %s\n",
                        cfg.optDouble("round_trip_fee_pct", 0), cfg.opt("min_net_rr")));
                for (int i = 0; i < Math.min(scanned.length(), 10); i++) {
                    JSONObject c = scanned.getJSONObject(i);
                    String status = c.optString("status");
                    sb.append('\n').append(String.format(Locale.US, "%-6s %-5s %5s  %s",
                            truncate(c.optString("coin"), 6),
                            c.isNull("direction") ? "-" : c.optString("direction"),
                            c.isNull("setup") ? "" : String.format(Locale.US, "%.2f",
                                    c.optDouble("net_rr", 0)),
                            "trade".equals(status) ? "TRADE" : truncate(status, 28)));
                }
                scan.setText(sb.toString());
            }

            boolean halted = "halted".equals(st);
            halt.setEnabled(!halted);
            halt.setText(halted ? "System halted" : "HALT TRADING");

            footer.setText("updated "
                    + new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date())
                    + " · " + d.optInt("candles_processed") + " candles");
        } catch (Exception e) {
            footer.setText("Bad response: " + e.getMessage());
        }
    }

    private static String truncate(String s, int n) {
        return s.length() <= n ? s : s.substring(0, n - 1) + "…";
    }

    private static String money(double v) {
        return (v < 0 ? "-$" : "$") + String.format(Locale.US, "%.2f", Math.abs(v));
    }

    private static String fmt(double v) {
        return String.format(Locale.US, "%.2f", v);
    }

    // -- halt --------------------------------------------------------------- //
    private void confirmHalt() {
        new AlertDialog.Builder(this)
                .setTitle("Halt trading?")
                .setMessage("The agent will stop opening new positions.\n\n"
                        + "This does NOT close an open position — do that on the exchange.")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Halt", (dlg, w) -> {
                    halt.setEnabled(false);
                    halt.setText("Halting…");
                    io.execute(() -> {
                        try {
                            request("/api/halt", "POST",
                                    "{\"reason\":\"halted from Android app\"}", true);
                            ui.post(this::refresh);
                        } catch (Exception e) {
                            ui.post(() -> {
                                halt.setEnabled(true);
                                halt.setText("HALT TRADING");
                                new AlertDialog.Builder(this)
                                        .setTitle("Halt failed")
                                        .setMessage(friendly(e))
                                        .setPositiveButton("OK", null)
                                        .show();
                            });
                        }
                    });
                })
                .show();
    }
}
