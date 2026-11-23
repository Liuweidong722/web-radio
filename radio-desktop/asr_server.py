# -*- coding: utf-8 -*-
"""
可独立部署的「网络收音机」后端（用于在线版实时字幕）
- 托管 web-radio 前端（静态文件）
- /proxy：流代理（绕过 CORS / 防盗链）
- /ws/asr：实时字幕 WebSocket（Vosk 离线识别，声音来自电台流本身，不碰麦克风）

运行（需先安装依赖）：
    pip install vosk websockets imageio-ffmpeg
    # Vosk 英文模型放到 ./models/vosk-model-* 或设置环境变量 VOSK_MODEL
    python asr_server.py --host 0.0.0.0 --port 8000

前端字幕会连 ws(s)://<host>:<port+1>。也可在静态页用 ?asr=ws://host:port+1 手动指定。
"""
import argparse
import json
import os
import re
import sys
import threading
import http.server
import urllib.parse
import urllib.request

import asr

HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    WEB_DIR = os.path.join(sys._MEIPASS, "web-radio")
else:
    WEB_DIR = os.path.abspath(os.path.join(HERE, "..", "web-radio"))

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/proxy":
            return self._proxy(parsed.query)
        if parsed.path == "/hls":
            return self._hls_proxy(parsed.query)
        if parsed.path in ("/", "/index.html"):
            return self._serve_index()
        return super().do_GET()

    def _hls_proxy(self, query):
        """递归 HLS 代理（与桌面端 app.py 同逻辑），用于在线自托管后端。"""
        params = urllib.parse.parse_qs(query)
        host = self.headers.get("Host") or "127.0.0.1:%d" % (
            self.server.server_address[1])
        base = "http://%s/hls" % host
        CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

        seg_url = params.get("seg", [""])[0]
        if seg_url:
            if not seg_url.startswith(("http://", "https://")):
                return self.send_error(400, "invalid seg")
            try:
                rng = self.headers.get("Range")
                req = urllib.request.Request(
                    seg_url, headers={"User-Agent": CHROME_UA, "Referer": seg_url,
                                      "Accept": "*/*", "Range": rng} if rng else
                    {"User-Agent": CHROME_UA, "Referer": seg_url, "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type",
                                 r.headers.get("Content-Type", "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:  # noqa: BLE001
                try:
                    self.send_error(502, "hls seg error: %s" % e)
                except Exception:
                    pass
            return

        pl_url = params.get("u", [""])[0]
        if not pl_url or not pl_url.startswith(("http://", "https://")):
            return self.send_error(400, "missing or invalid url")
        try:
            req = urllib.request.Request(
                pl_url, headers={"User-Agent": CHROME_UA, "Referer": pl_url,
                                 "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode("utf-8", "ignore")
                real = r.geturl()
        except Exception as e:  # noqa: BLE001
            try:
                self.send_error(502, "hls playlist error: %s" % e)
            except Exception:
                pass
            return

        out = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#EXT-X-STREAM-INF"):
                out.append(line)
                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    sub = urllib.parse.urljoin(real, lines[i + 1])
                    out.append("%s?u=%s" % (base, urllib.parse.quote(sub, safe="")))
                    i += 2
                    continue
            elif line.startswith("#EXT-X-MEDIA"):
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    uri = urllib.parse.urljoin(real, m.group(1))
                    line = (line[:m.start(1)]
                            + ("%s?u=%s" % (base, urllib.parse.quote(uri, safe="")))
                            + line[m.end(1):])
            elif line and not line.startswith("#"):
                seg = urllib.parse.urljoin(real, line)
                out.append("%s?seg=%s" % (base, urllib.parse.quote(seg, safe="")))
                i += 1
                continue
            out.append(line)
            i += 1
        body = ("\n".join(out) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self):
        try:
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                data = f.read()
        except Exception:
            return self.send_error(404)
        # 注入字幕后端地址（ws 与 http 同 host，端口 +1）
        ws_proto = "wss" if self.server.server_https else "ws"
        host = self.headers.get("Host", "127.0.0.1")
        # 去掉可能带的端口，避免重复
        host = host.split(":")[0]
        asr_port = self.server.server_asr_port
        inject = ("<script>window.__WS__='%s://%s:%d';"
                  "window.__PROXY__='/proxy?url=';</script>"
                  % (ws_proto, host, asr_port)).encode("utf-8")
        idx = data.lower().find(b"</head>")
        data = data[:idx] + inject + data[idx:] if idx != -1 else inject + data
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy(self, query):
        params = urllib.parse.parse_qs(query)
        target = params.get("url", [""])[0]
        if not target or not target.startswith(("http://", "https://")):
            return self.send_error(400, "missing or invalid url")
        try:
            req = urllib.request.Request(
                target, headers={"User-Agent": CHROME_UA, "Referer": target,
                                  "Accept": "*/*", "Icy-MetaData": "0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type",
                                 r.headers.get("Content-Type", "application/octet-stream"))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as e:  # noqa: BLE001
            try:
                self.send_error(502, "proxy error: %s" % e)
            except Exception:
                pass

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-asr", action="store_true", help="不启动字幕后端")
    args = ap.parse_args()

    if not args.no_asr:
        # 字幕 WebSocket 端口 = HTTP 端口 + 1
        asr_port = args.port + 1
        threading.Thread(target=asr.run_server_thread,
                         args=(args.host, asr_port), daemon=True).start()
    else:
        asr_port = args.port + 1

    import http.server as _hs
    httpd = _hs.ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.server_asr_port = asr_port
    httpd.server_https = False
    url = "http://%s:%d/" % (args.host, args.port)
    print("网络收音机后端已启动：%s" % url)
    print("字幕 WebSocket：ws://%s:%d" % (args.host, asr_port))
    print("按 Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
