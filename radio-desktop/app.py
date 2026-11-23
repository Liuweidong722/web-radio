# -*- coding: utf-8 -*-
"""
网络收音机 · 桌面版
- 内置本地 HTTP 服务：托管 web-radio 前端 + 流代理（绕过 CORS / 防盗链）
- 服务端录音引擎：直链原始码流透传落盘；HLS 轮询下载分片并解复用为标准 AAC
- 支持自动分段、录音库管理（列表 / 试听 / 重命名 / 删除 / 打开目录）
"""

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 注意：实时字幕后端（asr）改为「懒导入」——它在 import 时就会拉起 vosk
# （较重），放到后台线程里再导入，主窗口启动更快。

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    WEB_DIR = os.path.join(sys._MEIPASS, "web-radio")          # noqa: SLF001
else:
    WEB_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "web-radio"))


def default_rec_dir():
    """录音目录：优先 %USERPROFILE%\\Music\\网络收音机录音"""
    home = os.path.expanduser("~")
    for sub in ("Music", "音乐"):
        p = os.path.join(home, sub)
        if os.path.isdir(p):
            return os.path.join(p, "网络收音机录音")
    return os.path.join(home, "网络收音机录音")


CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "网络收音机")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
def load_config():
    """读取用户配置的录音目录（若有效则覆盖默认值）"""
    global REC_DIR
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            d = cfg.get("rec_dir")
            if d and isinstance(d, str):
                d = os.path.abspath(os.path.expanduser(d))
                if os.path.isdir(d) or _mkdir_ok(d):
                    REC_DIR = d
    except Exception:
        pass


def _mkdir_ok(d):
    try:
        os.makedirs(d, exist_ok=True)
        return True
    except Exception:
        return False


def save_config():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"rec_dir": REC_DIR}, f, ensure_ascii=False)
    except Exception:
        pass


REC_DIR = default_rec_dir()
load_config()
os.makedirs(REC_DIR, exist_ok=True)

AUDIO_EXT = (".mp3", ".aac", ".m4a", ".ts", ".ogg", ".wav")
MIME_MAP = {".mp3": "audio/mpeg", ".aac": "audio/aac", ".m4a": "audio/mp4",
            ".ts": "video/mp2t", ".ogg": "audio/ogg", ".wav": "audio/wav"}


def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def safe_name(s, limit=60):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s or "录音")).strip()
    return (s[:limit] or "录音")


def http_get(url, timeout=20, stream=False):
    req = urllib.request.Request(
        url, headers={"User-Agent": CHROME_UA, "Referer": url, "Accept": "*/*",
                      "Icy-MetaData": "0"})
    return urllib.request.urlopen(req, timeout=timeout)


# ==============================================================
#  MPEG-TS -> ADTS AAC 解复用
#  HLS 分片常为 188 字节 TS 包；直接拼接得到的 .ts 很多播放器打不开。
#  这里抽出音频 PES 负载（ADTS 帧自带同步头），拼接即为标准 .aac。
# ==============================================================
class TsDemuxer:
    def __init__(self):
        self.pmt_pid = -1
        self.audio_pid = -1
        self.audio_type = -1

    @property
    def ext(self):
        if self.audio_type in (0x03, 0x04):
            return ".mp3"
        return ".aac"

    def feed(self, buf: bytes):
        """返回抽取出的音频裸流；若不是 TS 则原样返回。"""
        if not buf:
            return b""
        # 已经是裸 ADTS / ID3 打头的 AAC 分片
        if buf[0] == 0xFF and (buf[1] & 0xF0) == 0xF0:
            if self.audio_type < 0:
                self.audio_type = 0x0F
            return buf
        if buf[:3] == b"ID3":
            if self.audio_type < 0:
                self.audio_type = 0x0F
            return buf
        if buf[0] != 0x47:
            return buf

        out = bytearray()
        n = len(buf)
        off = 0
        while off + 188 <= n:
            if buf[off] != 0x47:
                break
            pusi = bool(buf[off + 1] & 0x40)
            pid = ((buf[off + 1] & 0x1F) << 8) | buf[off + 2]
            afc = (buf[off + 3] >> 4) & 0x03
            p = off + 4
            if afc == 2:
                off += 188
                continue
            if afc == 3:
                p += buf[p] + 1
            if p >= off + 188:
                off += 188
                continue

            if pid == 0 and self.pmt_pid < 0:
                q = p + (buf[p] + 1 if pusi else 0)
                if q + 12 < off + 188:
                    sec_len = ((buf[q + 1] & 0x0F) << 8) | buf[q + 2]
                    end = min(q + 3 + sec_len - 4, off + 188)
                    i = q + 8
                    while i + 3 < end:
                        prog = (buf[i] << 8) | buf[i + 1]
                        pidv = ((buf[i + 2] & 0x1F) << 8) | buf[i + 3]
                        if prog != 0:
                            self.pmt_pid = pidv
                            break
                        i += 4
            elif pid == self.pmt_pid and self.audio_pid < 0:
                q = p + (buf[p] + 1 if pusi else 0)
                if q + 12 < off + 188:
                    sec_len = ((buf[q + 1] & 0x0F) << 8) | buf[q + 2]
                    end = min(q + 3 + sec_len - 4, off + 188)
                    pi_len = ((buf[q + 10] & 0x0F) << 8) | buf[q + 11]
                    i = q + 12 + pi_len
                    while i + 4 < end:
                        stype = buf[i]
                        epid = ((buf[i + 1] & 0x1F) << 8) | buf[i + 2]
                        es_len = ((buf[i + 3] & 0x0F) << 8) | buf[i + 4]
                        if stype in (0x0F, 0x11, 0x03, 0x04):
                            self.audio_pid = epid
                            self.audio_type = stype
                            break
                        i += 5 + es_len
            elif self.audio_pid >= 0 and pid == self.audio_pid:
                q = p
                if pusi and q + 9 < off + 188 and buf[q:q + 3] == b"\x00\x00\x01":
                    q += 9 + buf[q + 8]
                if q < off + 188:
                    out += buf[q:off + 188]
            off += 188
        return bytes(out)


# ==============================================================
#  录音引擎
# ==============================================================
class Recorder:
    def __init__(self):
        self.lock = threading.RLock()
        self.recording = False
        self.thread = None
        self.stop_flag = threading.Event()
        self.url = ""
        self.station = ""
        self.seg_minutes = 0
        self.bytes = 0
        self.started = 0.0
        self.files = []          # 本次录制产生的文件名
        self.error = ""
        self._fp = None
        self._cur_path = ""
        self._seg_index = 1
        self._seg_started = 0.0
        self._ext = ".mp3"

    # ---------- 状态 ----------
    def status(self):
        with self.lock:
            return {
                "recording": self.recording,
                "url": self.url,
                "station": self.station,
                "bytes": self.bytes,
                "elapsed": int(time.time() - self.started) if self.recording else 0,
                "file": os.path.basename(self._cur_path) if self._cur_path else "",
                "files": list(self.files),
                "seg": self.seg_minutes,
                "error": self.error,
            }

    # ---------- 文件轮转 ----------
    def _open_new_file(self):
        self._close_file()
        base = safe_name(self.station)
        ts = time.strftime("%Y%m%d-%H%M%S")
        suffix = "_P%02d" % self._seg_index if self.seg_minutes else ""
        name = "%s_%s%s%s" % (base, ts, suffix, self._ext)
        self._cur_path = os.path.join(REC_DIR, name)
        self._fp = open(self._cur_path, "wb")
        self._seg_started = time.time()
        self._seg_index += 1
        with self.lock:
            self.files.append(name)

    def _close_file(self):
        if self._fp:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                pass
            self._fp = None
            # 空文件直接清掉
            if self._cur_path and os.path.isfile(self._cur_path) \
                    and os.path.getsize(self._cur_path) == 0:
                try:
                    os.remove(self._cur_path)
                    name = os.path.basename(self._cur_path)
                    with self.lock:
                        if name in self.files:
                            self.files.remove(name)
                except Exception:
                    pass

    @staticmethod
    def _trim_to_frame(data, ext):
        """直播流是从中间接入的，开头往往是半帧数据。
        丢掉首个完整帧同步头之前的字节，保证文件头干净、播放器能立刻识别。"""
        if ext == ".aac":
            for i in range(min(len(data) - 2, 8192)):
                if data[i] == 0xFF and (data[i + 1] & 0xF6) == 0xF0:
                    return data[i:]
            return data
        if ext == ".mp3":
            for i in range(min(len(data) - 4, 8192)):
                if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
                    continue
                ver = (data[i + 1] >> 3) & 0x03     # 01 = 保留值
                layer = (data[i + 1] >> 1) & 0x03   # 00 = 保留值
                br = (data[i + 2] >> 4) & 0x0F      # 0000 自由 / 1111 非法
                sr = (data[i + 2] >> 2) & 0x03      # 11 = 保留值
                if ver == 1 or layer == 0 or br in (0, 15) or sr == 3:
                    continue
                return data[i:]
            return data
        return data

    def _write(self, data):
        if not data:
            return
        if self._fp is None:
            self._open_new_file()
            data = self._trim_to_frame(data, self._ext)
            if not data:
                return
        self._fp.write(data)
        with self.lock:
            self.bytes += len(data)
        if self.seg_minutes and (time.time() - self._seg_started) >= self.seg_minutes * 60:
            self._fp.flush()
            self._open_new_file()

    # ---------- 直链透传 ----------
    def _run_direct(self):
        low = self.url.lower().split("?")[0]
        self._ext = ".aac" if low.endswith(".aac") else (".ogg" if low.endswith(".ogg") else ".mp3")
        try:
            resp = http_get(self.url, timeout=25)
            ct = (resp.headers.get("Content-Type") or "").lower()
            if "aac" in ct:
                self._ext = ".aac"
            elif "ogg" in ct:
                self._ext = ".ogg"
            elif "mpeg" in ct or "mp3" in ct:
                self._ext = ".mp3"
            while not self.stop_flag.is_set():
                chunk = resp.read(16384)
                if not chunk:
                    break
                self._write(chunk)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.error = "录制中断：%s" % e

    # ---------- HLS 轮询下载 ----------
    def _run_hls(self):
        demux = TsDemuxer()
        seen = []
        seen_set = set()
        first = True
        idle = 0
        base_url = self.url
        while not self.stop_flag.is_set():
            try:
                with http_get(base_url, timeout=15) as r:
                    text = r.read().decode("utf-8", "ignore")
                    real = r.geturl()
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.error = "播放列表拉取失败：%s" % e
                if self.stop_flag.wait(2):
                    break
                continue

            lines = [l.strip() for l in text.splitlines() if l.strip()]
            # 主播放列表（master）→ 取第一条子列表
            if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
                sub = None
                for i, l in enumerate(lines):
                    if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                        sub = lines[i + 1]
                        break
                if sub:
                    base_url = urllib.parse.urljoin(real, sub)
                    continue

            target = 6.0
            for l in lines:
                if l.startswith("#EXT-X-TARGETDURATION"):
                    try:
                        target = float(l.split(":")[1])
                    except Exception:
                        pass
            segs = [urllib.parse.urljoin(real, l) for l in lines if not l.startswith("#")]
            if first:
                # 首次只从最后一片开始录：其余历史分片全部标记为「已见」，
                # 否则下一轮轮询会把播放列表里的整段缓存回补下来（录出几倍时长）
                for s in segs[:-1]:
                    seen_set.add(s)
                    seen.append(s)
                segs = segs[-1:]
                first = False
            new = [s for s in segs if s not in seen_set]
            if not new:
                idle += 1
                if idle > 40:
                    with self.lock:
                        self.error = "长时间无新分片，直播流可能已中断"
                    break
            else:
                idle = 0
            for s in new:
                if self.stop_flag.is_set():
                    break
                seen_set.add(s)
                seen.append(s)
                if len(seen) > 400:
                    old = seen.pop(0)
                    seen_set.discard(old)
                try:
                    with http_get(s, timeout=20) as sr:
                        raw = sr.read()
                    data = demux.feed(raw)
                    if self._fp is None:
                        self._ext = demux.ext
                    self._write(data if data else raw)
                except Exception:
                    continue
            if self.stop_flag.wait(max(1.0, target * 0.5)):
                break

    def _run(self):
        try:
            if re.search(r"\.m3u8($|\?)", self.url, re.I):
                self._run_hls()
            else:
                self._run_direct()
        finally:
            self._close_file()
            with self.lock:
                self.recording = False

    # ---------- 控制 ----------
    def start(self, url, station, seg):
        with self.lock:
            if self.recording:
                return False, "已有录音进行中"
            self.recording = True
            self.url = url
            self.station = station or "录音"
            self.seg_minutes = max(0, int(seg or 0))
            self.bytes = 0
            self.started = time.time()
            self.files = []
            self.error = ""
            self._cur_path = ""
            self._seg_index = 1
            self._ext = ".mp3"
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True, ""

    def stop(self):
        self.stop_flag.set()
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=8)
        self._close_file()
        with self.lock:
            self.recording = False
            files = list(self.files)
        return files


REC = Recorder()


# ==============================================================
#  HTTP 服务
# ==============================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/proxy":
            return self._proxy(parsed.query)
        if path == "/api/config":
            return self._json({"ok": True, "rec_dir": REC_DIR})
        if path == "/api/rec/status":
            return self._json(REC.status())
        if path == "/api/rec/list":
            return self._json({"ok": True, "items": self._list_recs(), "dir": REC_DIR})
        if path.startswith("/rec/"):
            return self._serve_rec(urllib.parse.unquote(path[5:]))
        if path in ("/", "/index.html"):
            return self._serve_index()
        return super().do_GET()

    def do_POST(self):
        global REC_DIR
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/rec/start":
            b = self._body()
            ok, err = REC.start(b.get("url", ""), b.get("name", ""), b.get("seg", 0))
            return self._json({"ok": ok, "error": err})
        if path == "/api/rec/stop":
            files = REC.stop()
            return self._json({"ok": True, "files": files})
        if path == "/api/rec/delete":
            name = os.path.basename(self._body().get("name", ""))
            p = os.path.join(REC_DIR, name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/rec/rename":
            b = self._body()
            src = os.path.join(REC_DIR, os.path.basename(b.get("from", "")))
            dst = os.path.join(REC_DIR, safe_name(os.path.basename(b.get("to", "")), 120))
            try:
                if not os.path.isfile(src):
                    raise FileNotFoundError("原文件不存在")
                if os.path.exists(dst):
                    raise FileExistsError("同名文件已存在")
                os.rename(src, dst)
                return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/rec/open":
            try:
                if sys.platform.startswith("win"):
                    os.startfile(REC_DIR)  # noqa: S606
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", REC_DIR])
                else:
                    subprocess.Popen(["xdg-open", REC_DIR])
            except Exception:
                pass
            return self._json({"ok": True})
        if path == "/api/config":
            b = self._body()
            if REC.recording:
                return self._json({"ok": False, "error": "正在录音中，无法更改路径"})
            d = os.path.abspath(os.path.expanduser(b.get("rec_dir", "")))
            if not d:
                return self._json({"ok": False, "error": "路径为空"})
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": "无法创建目录：" + str(e)})
            REC_DIR = d
            save_config()
            return self._json({"ok": True, "rec_dir": REC_DIR})
        if path == "/api/config/pick":
            return self._json({"ok": False, "error": "请通过窗口选择文件夹"})
        return self._json({"ok": False, "error": "unknown api"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    # ---------- 实现 ----------
    def _list_recs(self):
        items = []
        try:
            for n in os.listdir(REC_DIR):
                p = os.path.join(REC_DIR, n)
                if not os.path.isfile(p):
                    continue
                ext = os.path.splitext(n)[1].lower()
                if ext not in AUDIO_EXT:
                    continue
                stt = os.stat(p)
                items.append({
                    "name": n, "size": stt.st_size, "mtime": int(stt.st_mtime),
                    "mime": MIME_MAP.get(ext, "application/octet-stream"),
                    "station": n.split("_")[0],
                })
        except Exception:
            pass
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    def _serve_rec(self, name):
        p = os.path.join(REC_DIR, os.path.basename(name))
        if not os.path.isfile(p):
            return self.send_error(404)
        ext = os.path.splitext(p)[1].lower()
        size = os.path.getsize(p)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                code = 206
        end = min(end, size - 1)
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", MIME_MAP.get(ext, "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(65536, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except Exception:
            pass

    def _serve_index(self):
        try:
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                data = f.read()
        except Exception:
            return self.send_error(404)
        inject = ("<script>"
                  "window.__DESKTOP__=true;"
                  "window.__API__='';"
                  "window.__PROXY__='http://127.0.0.1:%d/proxy?url=';"
                  "window.__WS__='ws://127.0.0.1:%d';"
                  "window.__ASR_PROXY__='http://127.0.0.1:%d/proxy?url=';"
                  "</script>" % (PORT, PORT + 1, PORT)).encode("utf-8")
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
            with http_get(target, timeout=20) as r:
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


class DesktopApi:
    """暴露给前端（pywebview JS 桥）的本机能力：选择/打开录音文件夹"""
    def pick_folder(self):
        try:
            import webview
            if webview.windows:
                paths = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
                if paths:
                    return paths[0] if isinstance(paths, (list, tuple)) else paths
        except Exception:  # noqa: BLE001
            pass
        return None

    def open_folder(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(REC_DIR)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", REC_DIR])
            else:
                subprocess.Popen(["xdg-open", REC_DIR])
        except Exception:  # noqa: BLE001
            pass
        return None

    def get_config(self):
        return {"ok": True, "rec_dir": REC_DIR}


PORT = int(os.environ.get("PORT") or 0) or find_free_port()


def _start_asr_backend(port):
    """后台线程里启动字幕后端（懒导入 asr，避免拖慢启动）。"""
    try:
        import asr
        asr.run_server_thread("127.0.0.1", port)
    except Exception as e:  # noqa: BLE001
        print("[ASR] 字幕后端启动失败：%r" % e)


def main():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # 实时字幕 WebSocket 后端（端口 = HTTP + 1）；模型在首次使用时才加载
    threading.Thread(target=_start_asr_backend,
                     args=(PORT + 1,), daemon=True).start()
    url = "http://127.0.0.1:%d/" % PORT

    headless = os.environ.get("WEBRADIO_HEADLESS", "").strip().lower() in ("1", "true", "yes")
    if headless:
        # 无界面模式：只跑本地 HTTP 服务（适合服务器 / 容器内部署、自动化测试）
        print("网络收音机（无界面模式）已启动：%s" % url)
        print("录音目录：%s" % REC_DIR)
        print("按 Ctrl+C 退出。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        try:
            REC.stop()
        except Exception:
            pass
        httpd.shutdown()
        return

    try:
        import webview
        win = webview.create_window("网络收音机 · 录制版", url=url,
                                    width=1040, height=760, min_size=(760, 560),
                                    js_api=DesktopApi())

        def on_closing():
            try:
                REC.stop()
            except Exception:
                pass
        try:
            win.events.closing += on_closing
        except Exception:
            pass
        webview.start()
    except Exception:
        # 没有 pywebview 时退化为「以应用模式打开 Edge」，体验接近独立窗口
        opened = False
        for exe in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ):
            if os.path.isfile(exe):
                subprocess.Popen([exe, "--app=" + url,
                                  "--window-size=1040,780",
                                  "--autoplay-policy=no-user-gesture-required"])
                opened = True
                break
        if not opened:
            import webbrowser
            webbrowser.open(url)
        print("网络收音机已启动：%s" % url)
        print("录音目录：%s" % REC_DIR)
        print("关闭此窗口即退出程序。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    try:
        REC.stop()
    except Exception:
        pass
    httpd.shutdown()


if __name__ == "__main__":
    main()
