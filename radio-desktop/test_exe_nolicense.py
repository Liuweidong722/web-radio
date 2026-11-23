"""验证打包后的 exe 已彻底移除付费解锁：
- 首页不再包含 unlockOverlay / 付费解锁 / 激活 等字样
- /api/license 与 /pay_qr.png 不再存在（应 404）
- 电台列表可正常初始化（首页含站点渲染相关标记）
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "网络收音机.exe")
PORT = int(os.environ.get("PORT") or 59500)


def req(path, method="GET", body=None):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "ignore") if e.fp else "")
    except Exception as e:  # noqa: BLE001
        return 0, repr(e)


def main():
    if not os.path.isfile(EXE):
        print("EXE_NOT_FOUND", EXE)
        return 1
    env = dict(os.environ, PORT=str(PORT), WEBRADIO_HEADLESS="1")
    proc = subprocess.Popen([EXE], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=1).read()
                up = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
        if not up:
            print("EXE_NOT_UP")
            return 1

        results = []
        s, b = req("/")
        for kw in ["unlockOverlay", "付费解锁", "激活", "RadioLicense", "pay_qr"]:
            results.append(("index WITHOUT '%s'" % kw, kw not in b, ""))
        results.append(("index ok", s == 200, "status=%d len=%d" % (s, len(b))))
        results.append(("index still has app", "hls.min.js" in b and "电视伴音" in b, ""))

        s, _ = req("/api/license")
        results.append(("/api/license removed (404)", s == 404, "status=%d" % s))
        s, _ = req("/pay_qr.png")
        results.append(("/pay_qr.png removed (404)", s == 404, "status=%d" % s))
        s, b = req("/api/config")
        j = json.loads(b) if b else {}
        results.append(("/api/config still works", s == 200 and j.get("ok") is True, b))

        allok = True
        for name, ok, extra in results:
            print(("  PASS " if ok else "  FAIL ") + name + ((" | " + extra) if extra else ""))
            allok = allok and ok
        print("EXE_NOLICENSE_PASS" if allok else "EXE_NOLICENSE_FAIL")
        return 0 if allok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _port_free(port):
    """端口必须空闲：否则可能连到上一个没退干净的实例，测试结果会骗人。"""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


if __name__ == "__main__":
    if not _port_free(PORT):
        print("PORT_BUSY", PORT)
        sys.exit(2)
    sys.exit(main())
