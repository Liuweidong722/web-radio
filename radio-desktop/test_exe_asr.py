# -*- coding: utf-8 -*-
"""对打包后的 exe 验证实时字幕（模型已打进 exe）。"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = int(os.environ.get("PORT") or 58920)
ASR_PORT = PORT + 1
EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "网络收音机.exe")
TEST_URL = "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"


async def run():
    env = dict(os.environ, PORT=str(PORT), WEBRADIO_HEADLESS="1", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([EXE], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=1)
            up = True
            break
        except Exception:
            if proc.poll() is not None:
                print("EXE_EXITED", proc.returncode)
                break
            time.sleep(0.5)
    if not up:
        print("EXE_NOT_UP")
        proc.terminate()
        return 1

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
            deadline = time.time() + 75
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                m = json.loads(msg)
                if m.get("type") == "final" and m.get("text"):
                    got.append(m)
                    print("FINAL en=%r" % m["text"][:90])
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
        print("EXE_ASR_PASS count=%d" % len(got))
        return 0
    print("EXE_ASR_FAIL")
    return 2


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
