package com.webradio.app;

import android.annotation.SuppressLint;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import android.app.Activity;
import android.webkit.JavascriptInterface;
import androidx.webkit.WebViewAssetLoader;

import java.io.File;

/**
 * 网络收音机 · 安卓壳
 * 用 WebViewAssetLoader 把 assets/web 以 https://appassets.androidplatform.net 提供，
 * 这样 Service Worker / IndexedDB / 媒体接口都在安全上下文中可用。
 */
public class MainActivity extends Activity {

    private WebView webView;

    /** 页面内 audio 播放状态 → 前台服务（保活 + 通知栏） */
    public class Bridge {
        @JavascriptInterface
        public void onPlaying(String title) {
            runOnUiThread(() -> PlaybackService.start(MainActivity.this,
                    title == null || title.isEmpty() ? "正在收听" : title));
        }

        @JavascriptInterface
        public void onStopped() {
            runOnUiThread(() -> PlaybackService.stop(MainActivity.this));
        }
    }

    private final Bridge bridge = new Bridge();

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        webView.setBackgroundColor(0xFF0F1226);
        setContentView(webView);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        // 2 = MIXED_CONTENT_COMPATIBILITY：允许 http 音流在 https 页面里播
        s.setMixedContentMode(2);
        s.setSupportZoom(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        final WebViewAssetLoader assetLoader = new WebViewAssetLoader.Builder()
                .setDomain("appassets.androidplatform.net")
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        webView.addJavascriptInterface(bridge, "AndroidBridge");

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return assetLoader.shouldInterceptRequest(request.getUrl());
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                // 钩住页面内 <audio>：播放→前台服务，暂停/停→关服务
                view.evaluateJavascript(
                        "(function(){try{"
                        + "if(window.__androidHooked)return;window.__androidHooked=1;"
                        + "var audios=function(){return document.getElementsByTagName('audio')};"
                        + "function bind(){var as=audios();for(var i=0;i<as.length;i++){"
                        + "  if(as[i].__h)continue;as[i].__h=1;"
                        + "  as[i].addEventListener('play',function(){"
                        + "    var t=(document.getElementById('nowTitle')||{}).textContent||'网络收音机';"
                        + "    try{AndroidBridge.onPlaying(t);}catch(e){}"
                        + "  });"
                        + "  as[i].addEventListener('pause',function(){"
                        + "    try{AndroidBridge.onStopped();}catch(e){}"
                        + "  });"
                        + "}}bind();"
                        + "new MutationObserver(bind).observe(document.documentElement,"
                        + "{childList:true,subtree:true});"
                        + "}catch(e){}})();", null);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String host = uri.getHost() == null ? "" : uri.getHost();
                // 应用内页面继续交给 WebView；外部链接交给系统浏览器
                if ("appassets.androidplatform.net".equals(host)) {
                    return false;
                }
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW, uri);
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(i);
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "无法打开链接", Toast.LENGTH_SHORT).show();
                }
                return true;
            }
        });

        // 导出录音：blob:/https 下载落到系统 Download 目录
        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                try {
                    String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
                    if (name == null || name.isEmpty()) name = "recording.mp3";
                    DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                    req.setMimeType(mimeType);
                    req.addRequestHeader("User-Agent", userAgent);
                    req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                    DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                    if (dm != null) {
                        dm.enqueue(req);
                        Toast.makeText(MainActivity.this, "已开始下载：" + name, Toast.LENGTH_SHORT).show();
                    }
                } catch (Exception e) {
                    // blob URL 无法走 DownloadManager：提示用录音库内试听/导出
                    Toast.makeText(MainActivity.this, "请在录音库中使用「导出」按钮", Toast.LENGTH_SHORT).show();
                }
            }
        });

        PlaybackService.setListener(null);
        webView.loadUrl("https://appassets.androidplatform.net/assets/web/index.html");
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            // 退到后台而不是直接杀进程，尽量保住播放
            moveTaskToBack(true);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        // 不暂停 WebView，保证切后台继续放音
        if (webView != null) webView.onResume();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) webView.onResume();
    }

    @Override
    protected void onDestroy() {
        PlaybackService.setListener(null);
        if (webView != null) {
            webView.loadUrl("about:blank");
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    /** 供 JS 桥（可选）：当前版本通过 MediaSession 由 Service 维护通知。 */
    public static boolean isEmulator() {
        return Build.FINGERPRINT.startsWith("generic")
                || Build.FINGERPRINT.startsWith("unknown");
    }

    static File appWebRoot(Context ctx) {
        return new File(ctx.getApplicationInfo().sourceDir);
    }
}
