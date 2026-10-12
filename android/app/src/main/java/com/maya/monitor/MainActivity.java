package com.maya.monitor;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
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
import java.text.NumberFormat;
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
    private TextView configPill, equity, pnl, state, haltReasons, scanNote, footer, setupError;
    private LinearLayout position, stats, trades, scan;
    private View scanCard, stateDot;
    private Button halt, connect;

    // The design's colour tokens (res/values/colors.xml), resolved once.
    private int cPrimary, cSecondary, cHint, cPositive, cNegative, cWarning;

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
        stateDot = findViewById(R.id.stateDot);
        haltReasons = findViewById(R.id.haltReasons);
        scanNote = findViewById(R.id.scanNote);
        connect = findViewById(R.id.connect);
        position = findViewById(R.id.position);
        stats = findViewById(R.id.stats);
        trades = findViewById(R.id.trades);
        scan = findViewById(R.id.scan);
        scanCard = findViewById(R.id.scanCard);
        footer = findViewById(R.id.footer);
        setupError = findViewById(R.id.setupError);
        halt = findViewById(R.id.halt);

        cPrimary = getColor(R.color.text_primary);
        cSecondary = getColor(R.color.text_secondary);
        cHint = getColor(R.color.text_hint);
        cPositive = getColor(R.color.positive);
        cNegative = getColor(R.color.negative);
        cWarning = getColor(R.color.warning);

        ((EditText) findViewById(R.id.host)).setText(prefs.getString("host", ""));
        ((EditText) findViewById(R.id.port)).setText(prefs.getString("port", "8787"));
        ((EditText) findViewById(R.id.token)).setText(prefs.getString("token", ""));

        connect.setOnClickListener(v -> onConnect());
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
        if (TextUtils.isEmpty(port)) port = "8787";
        final String p = port;
        connect.setEnabled(false);
        connect.setText("Connecting…");

        // Probe the *authenticated* status endpoint before committing to the
        // dashboard, so a wrong token reports itself here instead of as a
        // dashboard that only ever says "Unauthorized". Settings are saved
        // only once they work.
        io.execute(() -> {
            try {
                request(base(host, p), token, "/api/status", "GET", null);
                ui.post(() -> {
                    prefs.edit().putString("host", host).putString("port", p)
                            .putString("token", token).apply();
                    resetConnectButton();
                    showDashboard();
                });
            } catch (Exception e) {
                ui.post(() -> {
                    resetConnectButton();
                    showSetup("Could not reach the agent at " + host + ":" + p
                            + "\n\n" + friendly(e)
                            + "\n\nCheck the agent is running with MAYA_API_HOST=0.0.0.0 "
                            + "and that both devices are on the same Wi-Fi.");
                });
            }
        });
    }

    private void resetConnectButton() {
        connect.setEnabled(true);
        connect.setText("Connect");
    }

    private String text(int id) {
        return ((EditText) findViewById(id)).getText().toString().trim();
    }

    // -- networking -------------------------------------------------------- //
    private static String base(String host, String port) {
        return "http://" + host + ":" + port;
    }

    /** A request with the saved connection settings. */
    private String request(String path, String method, String body) throws Exception {
        return request(base(prefs.getString("host", ""), prefs.getString("port", "8787")),
                prefs.getString("token", ""), path, method, body);
    }

    private static String request(String base, String token, String path, String method,
                                  String body) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(base + path).openConnection();
        try {
            c.setRequestMethod(method);
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            c.setRequestProperty("Authorization", "Bearer " + token);
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
                JSONObject status = new JSONObject(request("/api/status", "GET", null));
                JSONObject data = status.getJSONObject("data");
                JSONArray recent = new JSONObject(
                        request("/api/trades?limit=6", "GET", null)).getJSONArray("data");
                ui.post(() -> render(data, recent));
            } catch (Exception e) {
                ui.post(() -> {
                    footer.setTextColor(cWarning);
                    footer.setText(friendly(e));
                });
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

            // The scanner agent trades whichever coin it picks, so its
            // configured symbol would be misleading here.
            String subject = "scanner".equals(cfg.optString("agent"))
                    ? "scanner" : cfg.getString("symbol");
            configPill.setText(cfg.getString("mode") + " · " + cfg.getString("venue")
                    + " · " + subject + " · " + cfg.getString("timeframe")
                    + " · " + cfg.getInt("leverage") + "x");

            renderEquity(acc);
            renderPosition(d.isNull("position") ? null : d.getJSONObject("position"));
            renderStats(d.getJSONObject("run"), acc);
            renderTrades(recent);
            renderScan(d.optJSONArray("scan"), cfg);

            boolean halted = "halted".equals(acc.getString("state"));
            halt.setEnabled(!halted);
            halt.setAlpha(halted ? 0.5f : 1f);
            halt.setText(halted ? "System halted" : "Halt trading");

            footer.setTextColor(cHint);
            footer.setText("Updated "
                    + new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date())
                    + " · " + d.optInt("candles_processed") + " candles");
        } catch (Exception e) {
            footer.setTextColor(cWarning);
            footer.setText("Bad response: " + e.getMessage());
        }
    }

    private void renderEquity(JSONObject acc) throws Exception {
        double delta = acc.getDouble("pnl_usdt");
        equity.setText(money(acc.getDouble("equity")));
        pnl.setText(signedMoney(delta) + "  ("
                + String.format(Locale.US, "%+.2f", acc.getDouble("pnl_pct")) + "%) this run");
        pnl.setTextColor(delta > 0 ? cPositive : delta < 0 ? cNegative : cSecondary);

        String st = acc.getString("state");
        int color = "halted".equals(st) ? cNegative : "cooldown".equals(st) ? cWarning : cPositive;
        state.setText(st.toUpperCase(Locale.US));
        state.setTextColor(color);
        stateDot.getBackground().mutate().setTint(color);

        JSONArray reasons = acc.optJSONArray("halt_reasons");
        if (reasons == null || reasons.length() == 0) {
            haltReasons.setVisibility(View.GONE);
        } else {
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < reasons.length(); i++) {
                if (i > 0) sb.append(" · ");
                sb.append(reasons.getString(i));
            }
            haltReasons.setText(sb);
            haltReasons.setVisibility(View.VISIBLE);
        }
    }

    private void renderPosition(JSONObject p) throws Exception {
        position.removeAllViews();
        if (p == null) {
            position.addView(note("Flat — no open position"));
            return;
        }
        TextView head = text(p.getString("direction").toUpperCase(Locale.US) + "  "
                + p.getString("symbol") + " · " + p.getInt("leverage") + "x", 16, cPrimary, true);
        head.setPadding(0, 0, 0, dp(4));
        position.addView(head);
        position.addView(metric("Entry", price(p.getDouble("entry_price")), cPrimary));
        position.addView(metric(p.getBoolean("moved_to_breakeven") ? "Stop (breakeven)" : "Stop",
                price(p.getDouble("stop_loss")), cNegative));
        position.addView(metric("Target", price(p.getDouble("take_profit")), cPositive));
        position.addView(metric("Size", String.format(Locale.US, "%.5g", p.getDouble("quantity"))
                + " · " + money(p.getDouble("notional")), cPrimary));
        position.addView(metric("Setup", p.getString("setup"), cPrimary));
    }

    private void renderStats(JSONObject run, JSONObject acc) throws Exception {
        stats.removeAllViews();
        int n = run.optInt("trades", 0);
        stats.addView(metric("Trades", String.valueOf(n), cPrimary));
        if (n > 0) {
            double net = run.optDouble("total_pnl_usdt", 0);
            stats.addView(metric("Win rate",
                    String.format(Locale.US, "%.1f%%", run.optDouble("win_rate", 0)), cPrimary));
            stats.addView(metric("PnL (net)", signedMoney(net), net >= 0 ? cPositive : cNegative));
            stats.addView(metric("Fees paid", money(run.optDouble("total_fees", 0)), cWarning));
            stats.addView(metric("Profit factor", run.isNull("profit_factor")
                    ? "∞ (no losses)"
                    : String.format(Locale.US, "%.2f", run.optDouble("profit_factor")), cPrimary));
        }
        stats.addView(metric("Trades today", String.valueOf(acc.getInt("trades_today")), cPrimary));
        stats.addView(metric("Loss streak", String.valueOf(acc.getInt("consecutive_losses")), cPrimary));
    }

    private void renderTrades(JSONArray recent) throws Exception {
        trades.removeAllViews();
        if (recent.length() == 0) {
            trades.addView(note("None yet"));
            return;
        }
        for (int i = 0; i < recent.length(); i++) {
            JSONObject t = recent.getJSONObject(i);
            double v = t.getDouble("pnl_usdt");
            LinearLayout row = row();
            row.addView(chip(t.getString("direction").toUpperCase(Locale.US)));
            TextView setup = text(t.getString("setup_name"), 13, cPrimary, false);
            setup.setPadding(dp(10), 0, dp(8), 0);
            row.addView(setup, weighted());
            row.addView(text(signedMoney(v), 13, v >= 0 ? cPositive : cNegative, true));
            trades.addView(row);
        }
    }

    private void renderScan(JSONArray scanned, JSONObject cfg) throws Exception {
        // Scanner agent only: each coin from the latest scan and its fate.
        if (scanned == null) {
            scanCard.setVisibility(View.GONE);
            return;
        }
        scanCard.setVisibility(View.VISIBLE);
        scanNote.setText(String.format(Locale.US, "Fees %.2f%% round trip · needs net R:R ≥ %.1f",
                cfg.optDouble("round_trip_fee_pct", 0), cfg.optDouble("min_net_rr", 0)));
        scan.removeAllViews();
        for (int i = 0; i < Math.min(scanned.length(), 10); i++) {
            JSONObject c = scanned.getJSONObject(i);
            boolean trade = "trade".equals(c.optString("status"));
            LinearLayout row = row();
            row.addView(fixed(text(c.optString("coin"), 13, cPrimary, true), 56));
            row.addView(fixed(text(c.isNull("direction") ? "–" : c.optString("direction"),
                    13, cSecondary, false), 48));
            TextView rr = text(c.isNull("setup") ? "" : String.format(Locale.US, "%.2f",
                    c.optDouble("net_rr", 0)), 13, cPrimary, false);
            rr.setGravity(Gravity.END);
            row.addView(fixed(rr, 40));
            TextView status = text(trade ? "TRADE" : c.optString("status"), 13,
                    trade ? cPositive : cSecondary, trade);
            status.setPadding(dp(12), 0, 0, 0);
            row.addView(status, weighted());
            scan.addView(row);
        }
    }

    // -- row builders (the Figma "Android" components) ---------------------- //
    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                getResources().getDisplayMetrics()));
    }

    private TextView text(String s, int sp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        t.setSingleLine(true);
        t.setEllipsize(TextUtils.TruncateAt.END);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private TextView note(String s) {
        TextView t = text(s, 14, cSecondary, false);
        t.setPadding(0, dp(2), 0, dp(4));
        return t;
    }

    private LinearLayout row() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(6), 0, dp(6));
        return row;
    }

    private static LinearLayout.LayoutParams weighted() {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
    }

    private View fixed(TextView t, int widthDp) {
        t.setLayoutParams(new LinearLayout.LayoutParams(dp(widthDp),
                ViewGroup.LayoutParams.WRAP_CONTENT));
        return t;
    }

    /** "Android/Metric row": label left, value right (coloured by meaning). */
    private LinearLayout metric(String label, String value, int valueColor) {
        LinearLayout row = row();
        row.setPadding(0, dp(5), 0, dp(5));
        row.addView(text(label, 14, cSecondary, false), weighted());
        TextView v = text(value, 14, valueColor, true);
        v.setEllipsize(TextUtils.TruncateAt.MIDDLE);
        v.setPadding(dp(12), 0, 0, 0);
        row.addView(v);
        return row;
    }

    /** Direction chip on a trade row. */
    private TextView chip(String s) {
        TextView t = text(s, 10, cSecondary, true);
        t.setBackgroundResource(R.drawable.pill_raised);
        t.setPadding(dp(8), dp(2), dp(8), dp(2));
        return t;
    }

    private static final NumberFormat GROUPED = NumberFormat.getNumberInstance(Locale.US);

    private static String grouped(double v, int decimals) {
        synchronized (GROUPED) {
            GROUPED.setMinimumFractionDigits(decimals);
            GROUPED.setMaximumFractionDigits(decimals);
            return GROUPED.format(v);
        }
    }

    /** {@code $1,036.42} / {@code -$4.12} — same as the iOS app. */
    private static String money(double v) {
        return (v < 0 ? "-$" : "$") + grouped(Math.abs(v), 2);
    }

    /** PnL, where the sign is the point: {@code +$36.42}. */
    private static String signedMoney(double v) {
        return v > 0 ? "+" + money(v) : money(v);
    }

    /** More decimals for cheap coins, so a $0.0123 stop doesn't read as 0.01. */
    private static String price(double v) {
        return grouped(v, v >= 1000 ? 1 : v >= 1 ? 2 : 5);
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
                                    "{\"reason\":\"halted from Android app\"}");
                            ui.post(this::refresh);
                        } catch (Exception e) {
                            ui.post(() -> {
                                halt.setEnabled(true);
                                halt.setText("Halt trading");
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
