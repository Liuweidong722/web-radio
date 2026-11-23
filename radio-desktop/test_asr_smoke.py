# -*- coding: utf-8 -*-
"""ASR 字幕端到端冒烟测试：启动桌面服务（含字幕后端）→ 连 WebSocket →
发送英文电台流 → 验证能收到识别/翻译文本。"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = int(os.environ.get("PORT") or 58900)
ASR_PORT = PORT + 1
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
PY = sys.executable
TEST_URL = "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"  # 英文语音，沙箱可连


async def run():
    # 1) 启动服务
    env = dict(os.environ, PORT=str(PORT), WEBRADIO_HEADLESS="1",
               PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([PY, APP], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    # 2) 等 HTTP 起来
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
        out = proc.stdout.read() if proc.stdout else ""
        print(out[:2000])
        proc.terminate()
        return 1

    import websockets
    got = []
    ws = None
    # 等字幕后端（模型加载约需几秒）就绪再连
    for _ in range(15):
        try:
            ws = await websockets.connect("ws://127.0.0.1:%d" % ASR_PORT)
            break
        except Exception:
            time.sleep(1.5)
    if ws is None:
        print("WS_CONNECT_REFUSED (ASR 后端未就绪)")
        out = proc.stdout.read() if proc.stdout else ""
        print(out[:2000])
        proc.terminate()
        return 3
    try:
        async with ws:
            await ws.send(json.dumps({"url": TEST_URL}))
            deadline = time.time() + 35
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                m = json.loads(msg)
                if m.get("type") == "final" and m.get("text"):
                    got.append(m)
                    print("FINAL en=%r zh=%r" % (m["text"][:80], (m.get("zh") or "")[:60]))
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
        print("ASR_SMOKE_PASS count=%d" % len(got))
        return 0
    print("ASR_SMOKE_FAIL no final text received")
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
