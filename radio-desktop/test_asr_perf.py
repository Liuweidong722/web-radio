"""验证字幕后端的性能修复：
1) 模型懒加载：启动后（未开字幕）不加载 Vosk 模型
2) 断开即回收：WebSocket 断开后 ffmpeg 解码进程必须在数秒内退出（不再累积吃 CPU）
3) 回收后服务仍可用：第二次连接还能正常出字幕
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

try:
    import websockets
except ImportError:
    print("NO_WEBSOCKETS")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PORT = int(os.environ.get("PORT") or 59600)
TEST_URL = "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"
TEMP = os.environ.get("TEMP", "C:/tmp")
ERRLOG = os.path.join(TEMP, "asr_err.log")


def ffmpeg_alive():
    """当前 ffmpeg.exe 进程数（<0 表示查询失败）。"""
    try:
        raw = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=25).stdout
    except Exception:
        return -1
    if raw is None:
        return -1
    out = raw.decode("gbk", "ignore") if isinstance(raw, bytes) else raw
    return sum(1 for ln in out.splitlines() if "ffmpeg" in ln.lower())


def errlog_text():
    try:
        return open(ERRLOG, encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""


async def one_session(got_target, timeout_s):
    """连一次字幕后端，等待识别结果，然后断开。返回收到的 final 数。"""
    got = 0
    async with websockets.connect("ws://127.0.0.1:%d" % (PORT + 1)) as ws:
        await ws.send(json.dumps({"url": TEST_URL}))
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "final":
                got += 1
                if got >= got_target:
                    break
    return got


async def run():
    # 清空日志，便于判断本次运行是否加载过模型
    try:
        open(ERRLOG, "w", encoding="utf-8").close()
    except Exception:
        pass

    env = dict(os.environ, PORT=str(PORT), WEBRADIO_HEADLESS="1")
    logf = open(os.path.join(HERE, "leak_srv.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([PY, os.path.join(HERE, "app.py")], env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    results = []
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
        results.append(("HTTP 服务已启动", up, ""))
        if not up:
            results.append(("（终止）服务未起来", False, ""))
            return report(results)

        # 1) 懒加载：此时还没人用字幕，日志里不该有「模型加载成功」
        time.sleep(1.0)
        lazy_ok = "模型加载成功" not in errlog_text()
        results.append(("模型懒加载（不用字幕就不加载）", lazy_ok,
                        "errlog 含模型加载=" + str(not lazy_ok)))

        base_ff = ffmpeg_alive()

        # 2) 第一次会话：出 1 条 final
        got1 = await one_session(1, 45)
        results.append(("第一次连接收到字幕", got1 >= 1, "final=%d" % got1))
        loaded = "模型加载成功" in errlog_text()
        results.append(("首次使用时才加载模型", loaded, ""))

        # 3) 断开后 ffmpeg 必须回收
        alive = None
        for _ in range(20):
            alive = ffmpeg_alive()
            if alive <= base_ff:
                break
            await asyncio.sleep(0.5)
        results.append(("断开后解码进程已回收", alive <= base_ff,
                        "ffmpeg=%s (基线 %s)" % (alive, base_ff)))

        # 4) 第二次连接仍可用（确认回收没把服务弄坏）
        got2 = await one_session(1, 45)
        results.append(("回收后服务仍可用", got2 >= 1, "final=%d" % got2))

        alive2 = None
        for _ in range(20):
            alive2 = ffmpeg_alive()
            if alive2 <= base_ff:
                break
            await asyncio.sleep(0.5)
        results.append(("第二次断开后也已回收", alive2 <= base_ff,
                        "ffmpeg=%s" % alive2))
        return report(results)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except Exception:
            proc.kill()
        logf.close()


def report(results):
    allok = True
    for name, ok, extra in results:
        print(("  PASS " if ok else "  FAIL ") + name + ((" | " + extra) if extra else ""))
        allok = allok and ok
    print("ASR_PERF_PASS" if allok else "ASR_PERF_FAIL")
    return 0 if allok else 1


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
    sys.exit(asyncio.run(run()))
