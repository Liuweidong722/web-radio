# -*- coding: utf-8 -*-
"""
exe 冒烟测试：
1. 启动打包后的 网络收音机.exe（frozen 模式）
2. 自动探测其监听端口（窗口 exe 不输出端口）
3. 校验：首页注入 __DESKTOP__、hls.min.js 可取、proxy 可用
4. 用本地字节流做服务端录制（直链路径），验证 start->status->stop->list->落盘->rename->delete 全链路
"""
import subprocess, socket, time, sys, os, json, threading, urllib.request, urllib.error

EXE = r"C:\Users\Liu Weidong\WorkBuddy\2026-08-02-10-19-03\radio-desktop\dist\网络收音机.exe"
REC_DIR = os.path.join(os.path.expanduser("~"), "Music", "网络收音机录音")
LOCAL_PORT = 50888  # 本地测试流端口

MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00])  # 合法 MP3 帧同步头，_trim 会保留


def start_local_stream():
    """本地 HTTP 服务：GET /stream 持续吐 MP3 帧字节；GET /ping 返回 ok。"""
    import http.server
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/ping"):
                self.send_response(200); self.send_header("Content-Type","text/plain")
                self.end_headers(); self.wfile.write(b"ok"); return
            if self.path.startswith("/stream"):
                self.send_response(200)
                self.send_header("Content-Type","audio/mpeg")
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(MP3_FRAME * 64)  # ~256B/次，模拟码流
                        time.sleep(0.01)
                except Exception:
                    pass
                return
        def log_message(self,*a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def listening_ports():
    """用 Get-NetTCPConnection 拿到本机所有 LISTEN 端口（快且准）。"""
    try:
        import subprocess as _sp
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetTCPConnection -State Listen | Select-Object -ExpandProperty LocalPort"],
            capture_output=True, text=True, timeout=15).stdout
        ports = set()
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                ports.add(int(line))
        return sorted(ports)
    except Exception:
        return []


def find_exe_port(timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in listening_ports():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.8) as r:
                    txt = r.read(3000).decode("utf-8", "ignore")
                if "__DESKTOP__" in txt:
                    return port
            except Exception:
                pass
        time.sleep(0.5)
    return None


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return r.status, r.read()


def post(port, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    start_local_stream()
    # 等本地流就绪
    for _ in range(20):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{LOCAL_PORT}/ping", timeout=2) as r:
                if r.read() == b"ok": break
        except Exception:
            pass
        time.sleep(0.2)

    env_port = os.environ.get("PORT")
    if env_port:
        port = int(env_port)
        proc = None
        print("[1/6] 使用外部已启动的 exe，端口 =", port)
    else:
        print("[1/6] 启动 exe ...")
        proc = subprocess.Popen([EXE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("      exe pid =", proc.pid)
        port = find_exe_port()
        if not port:
            print("FAIL: 未探测到 exe 监听端口")
            proc.terminate(); sys.exit(1)
        print("      exe 端口 =", port)

    ok = True
    def check(label, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), label)
        if not cond: ok = False

    # 2) 首页注入
    try:
        st, body = get(port, "/")
        txt = body.decode("utf-8", "ignore")
        check("首页注入 __DESKTOP__", st == 200 and "__DESKTOP__" in txt and "网络收音机" in txt)
    except Exception as e:
        check("首页注入 __DESKTOP__ (异常: %s)" % e, False)

    # 3) hls.min.js
    try:
        st, body = get(port, "/lib/hls.min.js")
        check("hls.min.js 可取 (%d 字节)" % len(body), st == 200 and len(body) > 100000)
    except Exception as e:
        check("hls.min.js (异常: %s)" % e, False)

    # 4) proxy
    try:
        st, body = get(port, "/proxy?url=http://127.0.0.1:%d/ping" % LOCAL_PORT)
        check("proxy 代理本地流 (body=%r)" % body[:10], st == 200 and b"ok" in body)
    except Exception as e:
        check("proxy (异常: %s)" % e, False)

    # 5) 录音：直链本地流
    stream_url = "http://127.0.0.1:%d/stream" % LOCAL_PORT
    try:
        st, j = post(port, "/api/rec/start", {"url": stream_url, "name": "冒烟测试台", "seg": 0})
        check("录制 start 返回 ok", st == 200 and j.get("ok") is True)
        time.sleep(3)
        st, body = get(port, "/api/rec/status")
        j = json.loads(body.decode("utf-8"))
        check("status 显示 recording 且 bytes>0 (bytes=%s)" % j.get("bytes"),
              j.get("recording") is True and j.get("bytes", 0) > 0)
        st, j = post(port, "/api/rec/stop", {})
        files = j.get("files", [])
        check("stop 返回文件列表 (files=%s)" % files, st == 200 and len(files) >= 1)
        # 落盘校验
        on_disk = [f for f in files if os.path.isfile(os.path.join(REC_DIR, f))]
        check("录音文件已落盘 (%s)" % on_disk, len(on_disk) >= 1)
        # list
        st, body = get(port, "/api/rec/list")
        j = json.loads(body.decode("utf-8"))
        names = [it["name"] for it in j.get("items", [])]
        check("list 含录音 (%s)" % names[:3], any(n in names for n in files))
        test_file = on_disk[0] if on_disk else (files[0] if files else None)
    except Exception as e:
        check("录制流程 (异常: %s)" % e, False)
        test_file = None

    # 6) rename + delete
    if test_file:
        try:
            st, j = post(port, "/api/rec/rename", {"from": test_file, "to": "冒烟重命名.mp3"})
            check("rename ok", j.get("ok") is True)
            renamed = "冒烟重命名.mp3"
            st, j = post(port, "/api/rec/delete", {"name": renamed})
            check("delete ok", j.get("ok") is True)
            check("删除后文件消失", not os.path.isfile(os.path.join(REC_DIR, renamed)))
        except Exception as e:
            check("rename/delete (异常: %s)" % e, False)

    # 7) 存储路径配置接口
    try:
        st, body = get(port, "/api/config")
        j = json.loads(body.decode("utf-8"))
        default_dir = j.get("rec_dir", "")
        check("config GET 返回 rec_dir (=%s)" % default_dir, st == 200 and j.get("ok") is True and bool(default_dir))
        # 改为临时目录
        import tempfile
        new_dir = os.path.join(tempfile.gettempdir(), "rb_cfg_test_%d" % int(time.time()))
        st, j = post(port, "/api/config", {"rec_dir": new_dir})
        check("config POST 设置新路径 ok (=%s)" % new_dir, st == 200 and j.get("ok") is True and j.get("rec_dir") == new_dir)
        # 验证生效：在该路径录制应落盘到新目录
        st, j = post(port, "/api/rec/start", {"url": stream_url, "name": "路径配置测试", "seg": 0})
        time.sleep(2)
        st, j = post(port, "/api/rec/stop", {})
        new_files = j.get("files", [])
        landed = [f for f in new_files if os.path.isfile(os.path.join(new_dir, f))]
        check("新路径下录音落盘 (%s)" % landed, len(landed) >= 1)
        # 清理新目录里的测试文件
        for f in landed:
            try: os.remove(os.path.join(new_dir, f))
            except Exception: pass
        # 还原默认路径
        st, j = post(port, "/api/config", {"rec_dir": default_dir})
        check("config POST 还原默认路径 ok", st == 200 and j.get("ok") is True and j.get("rec_dir") == default_dir)
    except Exception as e:
        check("存储路径配置 (异常: %s)" % e, False)

    if proc is not None:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    print("\n==== 结果:", "ALL PASS" if ok else "SOME FAILED", "====")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
