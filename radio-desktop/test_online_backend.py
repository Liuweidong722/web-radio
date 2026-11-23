# -*- coding: utf-8 -*-
"""验证在线自托管后端 asr_server.py：启动 → HTTP 起来 → WS 字幕收到英文 final。"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = r"C:\Users\Liu Weidong\.workbuddy\binaries\python\envs\radio-sub\Scripts\python.exe"
PORT = 8800
ASR_PORT = PORT + 1
TEST_URL = "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"


async def run():
    env = dict(os.environ, PORT=str(PORT), PYTHONUNBUFFERED="1")
    srv_log = os.path.join(HERE, "online_srv.log")
    with open(srv_log, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen([PY, os.path.join(HERE, "asr_server.py"),
                                 "--host", "127.0.0.1", "--port", str(PORT)],
                                env=env, stdout=lf, stderr=subprocess.STDOUT)
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=1)
            up = True
            break
        except Exception:
            if proc.poll() is not None:
                print("SERVER_EXITED", proc.returncode)
                break
            time.sleep(0.5)
    if not up:
        print("SERVER_NOT_UP")
        try:
            with open(srv_log, encoding="utf-8") as f:
                print(f.read()[:2000])
        except Exception:
            pass
        proc.terminate()
        return 1

    # 确认首页注入了 __WS__
    html = urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=5).read().decode("utf-8", "ignore")
    print("index injects __WS__:", "__WS__" in html)

    import websockets
    got = []
    ws = None
    for _ in range(20):
        try:
            ws = await websockets.connect("ws://127.0.0.1:%d" % ASR_PORT)
            break
        except Exception:
            time.sleep(1.5)
    if ws is None:
        print("WS_REFUSED")
        proc.terminate()
        return 3
    try:
        async with ws:
            await ws.send(json.dumps({"url": TEST_URL}))
            deadline = time.time() + 70
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                m = json.loads(msg)
                if m.get("type") == "final" and m.get("text"):
                    got.append(m["text"])
                    print("FINAL en=%r" % m["text"][:80])
                elif m.get("type") == "error":
                    print("ASR_ERROR", m.get("msg"))
                if len(got) >= 3:
                    break
    except Exception as e:
        print("WS_FAIL", repr(e)[:200])
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    if got:
        print("ONLINE_BACKEND_PASS count=%d" % len(got))
        return 0
    print("ONLINE_BACKEND_FAIL")
    return 2


if __name__ == "__main__":
    import threading
    def _hard_exit():
        print("HARD_TIMEOUT forcing exit")
        os._exit(7)
    threading.Timer(150, _hard_exit).start()
    sys.exit(asyncio.run(run()))
