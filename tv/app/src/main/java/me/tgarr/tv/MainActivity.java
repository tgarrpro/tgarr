package me.tgarr.tv;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * tgarr on the television: the Media Center of the tgarr at home, full screen, driven by the
 * remote control. All of tgarr is in the page; this is the frame around it - it finds the
 * server on the home network (or is told its address once), remembers it, and hands the
 * remote's keys to the page: the arrows and OK as they are, Back as "back".
 *
 * A television that was not unlocked with the PIN is shown the Media Center and nothing
 * else - that is tgarr's own rule, not this app's.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "tgarr", KEY = "server";
    private static final int PORT = 8765;

    private FrameLayout root;
    private WebView web;
    private View setup;
    private View fullScreen;                         // a film playing full screen
    private WebChromeClient.CustomViewCallback fullScreenDone;
    private TextView status;
    private EditText address;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile boolean looking;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#191624"));
        setContentView(root);
        String server = prefs().getString(KEY, "");
        if (server.isEmpty()) showSetup(null); else open(server);
    }

    private SharedPreferences prefs() { return getSharedPreferences(PREFS, MODE_PRIVATE); }

    // ---- the page -------------------------------------------------------------------------------

    private void open(final String server) {
        if (web == null) {
            web = new WebView(this);
            WebSettings s = web.getSettings();
            s.setJavaScriptEnabled(true);
            s.setDomStorageEnabled(true);                       // "carry on" and the reader's place are kept there
            s.setMediaPlaybackRequiresUserGesture(false);
            s.setUserAgentString(s.getUserAgentString() + " tgarr-tv/1.0");
            web.setBackgroundColor(Color.parseColor("#191624"));
            web.setFocusable(true);
            web.setFocusableInTouchMode(true);
            web.setWebViewClient(new WebViewClient() {
                @Override
                public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                    // the page stays at home: a link that leads elsewhere is not followed
                    String host = r.getUrl().getHost(), mine = Uri.parse(prefs().getString(KEY, "")).getHost();
                    return host == null || mine == null || !host.equalsIgnoreCase(mine);
                }

                @Override
                public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                    if (r.isForMainFrame()) {
                        showSetup("tgarr at " + prefs().getString(KEY, "") + " does not answer. Is it running? Has its address changed?");
                    }
                }
            });
            web.setWebChromeClient(new WebChromeClient() {
                @Override
                public void onShowCustomView(View view, CustomViewCallback done) {
                    if (fullScreen != null) { done.onCustomViewHidden(); return; }
                    fullScreen = view;
                    fullScreenDone = done;
                    root.addView(view, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
                    web.setVisibility(View.GONE);
                }

                @Override
                public void onHideCustomView() { leaveFullScreen(); }
            });
            root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }
        if (setup != null) { root.removeView(setup); setup = null; }
        web.setVisibility(View.VISIBLE);
        web.loadUrl(server + "/mediacenter?tv=1");
        web.requestFocus();
    }

    private void leaveFullScreen() {
        if (fullScreen == null) return;
        root.removeView(fullScreen);
        fullScreen = null;
        if (fullScreenDone != null) fullScreenDone.onCustomViewHidden();
        fullScreenDone = null;
        web.setVisibility(View.VISIBLE);
        web.requestFocus();
    }

    // ---- the keys of the remote ---------------------------------------------------------------------

    @Override
    public boolean onKeyDown(int key, KeyEvent e) {
        if (key == KeyEvent.KEYCODE_BACK && setup == null && web != null) {
            if (fullScreen != null) { leaveFullScreen(); return true; }
            if (web.canGoBack()) { web.goBack(); return true; }
        }
        if (key == KeyEvent.KEYCODE_MENU && setup == null) { askToChange(); return true; }
        return super.onKeyDown(key, e);
    }

    private void askToChange() {
        new AlertDialog.Builder(this)
                .setTitle("tgarr")
                .setMessage("Showing " + prefs().getString(KEY, "") + ".")
                .setPositiveButton("Another address", (d, w) -> showSetup(null))
                .setNeutralButton("Load again", (d, w) -> { if (web != null) web.reload(); })
                .setNegativeButton("Close", null)
                .show();
    }

    @Override
    protected void onPause() { super.onPause(); if (web != null) web.onPause(); }

    @Override
    protected void onResume() { super.onResume(); if (web != null) web.onResume(); }

    @Override
    protected void onDestroy() { looking = false; if (web != null) web.destroy(); super.onDestroy(); }

    // ---- where tgarr is -------------------------------------------------------------------------------

    private int dp(int v) { return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics()); }

    private void showSetup(String why) {
        if (web != null) web.setVisibility(View.GONE);
        if (setup != null) root.removeView(setup);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setPadding(dp(48), dp(32), dp(48), dp(32));

        TextView title = new TextView(this);
        title.setText("tgarr");
        title.setTextColor(Color.parseColor("#229ED9"));
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 44);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setGravity(Gravity.CENTER);
        box.addView(title);

        status = new TextView(this);
        status.setTextColor(Color.parseColor("#f3eefc"));
        status.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
        status.setGravity(Gravity.CENTER);
        status.setPadding(0, dp(18), 0, dp(18));
        box.addView(status);

        address = new EditText(this);
        address.setSingleLine(true);
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setImeOptions(EditorInfo.IME_ACTION_GO);
        address.setHint("192.168.1.20:8765");
        address.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        address.setText(prefs().getString(KEY, "").replace("http://", ""));
        address.setOnEditorActionListener((v, action, ev) -> { tryTyped(); return true; });
        box.addView(address, new LinearLayout.LayoutParams(dp(420), ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout row = new LinearLayout(this);
        row.setGravity(Gravity.CENTER);
        row.setPadding(0, dp(14), 0, 0);
        Button openIt = new Button(this);
        openIt.setText("Open");
        openIt.setOnClickListener(v -> tryTyped());
        Button find = new Button(this);
        find.setText("Look for it again");
        find.setOnClickListener(v -> look());
        row.addView(find);
        row.addView(openIt);
        box.addView(row);

        setup = box;
        root.addView(box, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        if (why != null) { status.setText(why); find.requestFocus(); } else look();
    }

    /** What was typed, as an address: "192.168.1.20", "192.168.1.20:8765", "http://nas:8765/". */
    static String normalise(String typed) {
        String t = typed == null ? "" : typed.trim();
        if (t.isEmpty()) return "";
        if (!t.matches("(?i)^https?://.*")) t = "http://" + t;
        Uri u = Uri.parse(t);
        if (u.getHost() == null || u.getHost().isEmpty()) return "";
        int port = u.getPort() > 0 ? u.getPort() : ("https".equalsIgnoreCase(u.getScheme()) ? -1 : PORT);
        return u.getScheme().toLowerCase() + "://" + u.getHost() + (port > 0 ? ":" + port : "");
    }

    private void tryTyped() {
        final String server = normalise(address.getText().toString());
        if (server.isEmpty()) { status.setText("That is not an address. For example: 192.168.1.20"); return; }
        status.setText("Asking " + server + " …");
        new Thread(() -> {
            final boolean ok = isTgarr(server, 4000);
            ui.post(() -> {
                if (setup == null) return;
                if (ok) use(server);
                else status.setText("Nothing that is tgarr answers at " + server + ".\n(tgarr 0.5.11 or later; the address is the one you open in a browser.)");
            });
        }).start();
    }

    private void use(String server) {
        looking = false;
        prefs().edit().putString(KEY, server).apply();
        open(server);
    }

    /** Is that tgarr? Its home-screen manifest says so, and is given without a PIN. */
    static boolean isTgarr(String server, int timeout) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(server + "/manifest.webmanifest").openConnection();
            c.setConnectTimeout(timeout);
            c.setReadTimeout(timeout);
            c.setInstanceFollowRedirects(false);
            if (c.getResponseCode() != 200) return false;
            InputStream in = c.getInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[2048];
            int n;
            while ((n = in.read(buf)) > 0 && out.size() < 8192) out.write(buf, 0, n);
            return out.toString("UTF-8").contains("\"tgarr\"");
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** This television's own addresses on the home network (never one that is public). */
    static List<String> ownAddresses() {
        List<String> out = new ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) out.add(a.getHostAddress());
                }
            }
        } catch (Exception ignored) { }
        return out;
    }

    /** The 254 neighbours of an address: 192.168.1.37 -> 192.168.1.1 … 192.168.1.254, itself left out. */
    static List<String> neighbours(String own) {
        List<String> out = new ArrayList<>();
        int dot = own.lastIndexOf('.');
        if (dot < 0) return out;
        String net = own.substring(0, dot + 1);
        for (int i = 1; i <= 254; i++) {
            String a = net + i;
            if (!a.equals(own)) out.add(a);
        }
        return out;
    }

    /** Look for tgarr on the home network: every neighbour is asked, a few dozen at a time. */
    private void look() {
        if (looking) return;
        final List<String> own = ownAddresses();
        if (own.isEmpty()) { status.setText("This television is not on a home network. Type tgarr's address:"); address.requestFocus(); return; }
        looking = true;
        status.setText("Looking for tgarr on the home network …");
        new Thread(() -> {
            final AtomicReference<String> found = new AtomicReference<>();
            ExecutorService pool = Executors.newFixedThreadPool(48);
            for (String me : own) {
                for (final String a : neighbours(me)) {
                    pool.execute(() -> {
                        if (!looking || found.get() != null) return;
                        String server = "http://" + a + ":" + PORT;
                        if (isTgarr(server, 700)) found.compareAndSet(null, server);
                    });
                }
            }
            pool.shutdown();
            try { pool.awaitTermination(40, TimeUnit.SECONDS); } catch (InterruptedException ignored) { }
            pool.shutdownNow();
            final String server = found.get();
            ui.post(() -> {
                boolean was = looking;
                looking = false;
                if (setup == null || !was) return;
                if (server != null) use(server);
                else { status.setText("tgarr was not found on the home network (port " + PORT + ").\nType the address you open it at in a browser:"); address.requestFocus(); }
            });
        }).start();
    }
}
