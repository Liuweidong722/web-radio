# -*- coding: utf-8 -*-
"""
实时字幕后端（不碰麦克风）
- 接收电台流 URL（WebSocket 客户端发来）
- 用 ffmpeg(imageio_ffmpeg) 把流解码成 16k 单声道 PCM
- 喂给本地 Vosk 模型做离线流式识别（英文）
- final 结果经前端翻译为中文
- 把 {type,text,zh} 推回客户端显示

设计要点（性能相关）：
- 声音来自电台流本身，与麦克风无关。
- **模型懒加载**：WebSocket 服务先监听，Vosk 模型在第一个字幕请求时才加载，
  这样不用字幕的用户启动/操作电台完全不受影响（不占内存、不抢 CPU）。
- **断开即回收**：客户端断开（切台/关字幕/关页面）立刻杀掉 ffmpeg 解码进程，
  否则解码进程会一直跑下去，越切台越吃 CPU，表现为「越用越慢」。
- 会话数上限，避免同时开多个 ffmpeg 把机器吃满。
- 解码进程用低于正常的优先级，保证播放与界面响应优先。
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.request

try:
    from vosk import Model, KaldiRecognizer
except Exception:  # noqa: BLE001
    Model = None
    KaldiRecognizer = None

MODEL = None
MODEL_PATH = None
_LOAD_LOCK = threading.Lock()

# 同时允许的字幕会话数（够用即可，防止多个 ffmpeg 抢 CPU）
MAX_SESSIONS = 2
_SESS_LOCK = threading.Lock()
_SESS_COUNT = 0

# Windows：让解码进程以「低于正常」优先级运行
try:
    _LOW_PRIO = subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
except Exception:  # noqa: BLE001
    _LOW_PRIO = 0


def _log(msg):
    """写调试日志到 TEMP/asr_err.log（不依赖控制台，方便打包后排查）。"""
    try:
        with open(os.path.join(os.environ.get("TEMP", "C:/tmp"),
                               "asr_err.log"), "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:  # noqa: BLE001
        pass


def ffmpeg_path():
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:  # noqa: BLE001
        pass
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            import glob
            hits = (glob.glob(os.path.join(meipass, "ffmpeg*.exe"))
                    + glob.glob(os.path.join(meipass, "ffmpeg*")))
            if hits:
                return hits[0]
        exe = os.path.dirname(sys.executable)
        for cand in (os.path.join(exe, "ffmpeg.exe"), os.path.join(exe, "ffmpeg")):
            if os.path.isfile(cand):
                return cand
    return "ffmpeg"


def find_model():
    global MODEL_PATH
    if MODEL_PATH and os.path.isdir(MODEL_PATH):
        return MODEL_PATH
    candidates = []
    env = os.environ.get("VOSK_MODEL")
    if env:
        candidates.append(env)
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "models"))
        exe = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe, "models"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(here, "models"),
        os.path.join(os.path.dirname(here), "models"),
        os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries",
                     "python", "envs", "radio-sub", "models"),
    ]
    for base in candidates:
        if not base or not os.path.isdir(base):
            continue
        for d in sorted(os.listdir(base)):
            if d.startswith("vosk-model"):
                MODEL_PATH = os.path.join(base, d)
                return MODEL_PATH
    _log("[ASR] 模型路径未找到，候选：%s" % candidates)
    return None


def load_model():
    """加载 Vosk 模型（只加载一次）。返回 True/False。"""
    global MODEL
    if MODEL is not None:
        return True
    if Model is None:
        _log("[ASR] vosk 未导入（Model is None）——可能是打包时 .dll 未随 vosk 包一起打包")
        return False
    with _LOAD_LOCK:
        if MODEL is not None:
            return True
        p = find_model()
        if not p:
            return False
        try:
            MODEL = Model(p)
        except Exception as e:  # noqa: BLE001
            _log("[ASR] 模型加载失败：%r" % e)
            return False
        _log("[ASR] 模型加载成功：%s" % p)
    return True


def translate_en_zh(text):
    """Google 免费翻译接口（保留备用；实际翻译在前端做）。失败返回空串。"""
    if not text:
        return ""
    try:
        q = urllib.parse.quote(text)
        url = ("https://translate.googleapis.com/translate_a/single"
               "?client=gtx&sl=en&tl=zh-CN&dt=t&q=" + q)
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        parts = []
        for seg in data[0]:
            if seg and seg[0]:
                parts.append(seg[0])
        return "".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def open_pcm(url, proxy_base):
    """打开解码进程，直连优先，失败回退 proxy。返回 Popen 或抛错。

    注意：这是阻塞函数，调用方应放进线程池执行，避免卡住事件循环。
    """
    attempts = [url]
    if proxy_base:
        attempts.append(proxy_base + urllib.parse.quote(url, safe=""))
    last_err = ""
    for i, inp in enumerate(attempts):
        cmd = [ffmpeg_path(), "-nostdin", "-loglevel", "error",
               "-i", inp, "-ar", "16000", "-ac", "1", "-f", "s16le", "-"]
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if _LOW_PRIO:
            kwargs["creationflags"] = _LOW_PRIO
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            continue
        # 给一点时间判断是否真的连上了
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            return proc  # 仍存活 → 视为连接成功
        err = b""
        try:
            err = proc.stderr.read().decode("utf-8", "ignore")[:400]
        except Exception:  # noqa: BLE001
            pass
        last_err = err or ("ffmpeg exited code %d" % proc.returncode)
        _close_proc(proc)
        if i < len(attempts) - 1:
            continue
        raise RuntimeError("解码失败：" + last_err)
    raise RuntimeError("解码失败：" + last_err)


def _close_proc(proc):
    """确保解码进程退出，并释放管道（防止句柄/进程泄漏）。"""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=1)
        except Exception:  # noqa: BLE001
            pass
    for pipe in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if pipe:
                pipe.close()
        except Exception:  # noqa: BLE001
            pass


async def asr_session(ws, url, proxy_base=None):
    global _SESS_COUNT
    if MODEL is None:
        await ws.send(json.dumps({"type": "error", "msg": "ASR 模型未加载"}))
        return

    # 并发闸门：避免多个 ffmpeg 同时抢 CPU（UI 卡顿的主因之一）
    with _SESS_LOCK:
        if _SESS_COUNT >= MAX_SESSIONS:
            busy = True
        else:
            _SESS_COUNT += 1
            busy = False
    if busy:
        await ws.send(json.dumps({"type": "error", "msg": "字幕通道已满，请稍后再试"}))
        return

    loop = asyncio.get_running_loop()
    proc = None
    watcher = None
    try:
        try:
            proc = await loop.run_in_executor(None, open_pcm, url, proxy_base)
        except Exception as e:  # noqa: BLE001
            await ws.send(json.dumps({"type": "error", "msg": str(e)}))
            return

        rec = KaldiRecognizer(MODEL, 16000)
        rec.SetWords(False)

        async def _watch_disconnect():
            """客户端断开（切台/关字幕/关页面）时立刻杀掉解码进程。

            否则一个安静的流（长时间没有识别结果）会让读循环一直阻塞，
            ffmpeg 永不退出 → 切几次台后 CPU 被一堆 ffmpeg 吃满 → 越用越慢。
            """
            try:
                await ws.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            await loop.run_in_executor(None, _close_proc, proc)

        watcher = loop.create_task(_watch_disconnect())

        while True:
            raw = await loop.run_in_executor(None, proc.stdout.read, 4096)
            if not raw:
                break
            if rec.AcceptWaveform(raw):
                res = json.loads(rec.Result())
                text = res.get("text", "").strip()
                if text:
                    await ws.send(json.dumps({"type": "final", "text": text}))
            else:
                p = json.loads(rec.PartialResult())
                pt = p.get("partial", "").strip()
                if pt:
                    await ws.send(json.dumps({"type": "partial", "text": pt}))
        # 收尾
        try:
            res = json.loads(rec.FinalResult())
            text = res.get("text", "").strip()
            if text:
                await ws.send(json.dumps({"type": "final", "text": text}))
            await ws.send(json.dumps({"type": "end"}))
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send(json.dumps({"type": "error", "msg": str(e)}))
        except Exception:  # noqa: BLE001
            pass
    finally:
        if watcher is not None:
            watcher.cancel()
        await loop.run_in_executor(None, _close_proc, proc)
        with _SESS_LOCK:
            _SESS_COUNT = max(0, _SESS_COUNT - 1)


async def _handler(ws):
    loop = asyncio.get_running_loop()
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=10)
        cfg = json.loads(msg)
        url = cfg.get("url", "")
        proxy = cfg.get("proxy")
        if not url:
            await ws.send(json.dumps({"type": "error", "msg": "缺少 url"}))
            return
        # 懒加载：第一个字幕请求才把模型载入内存（不用字幕的人不受影响）
        if MODEL is None:
            ok = await loop.run_in_executor(None, load_model)
            if not ok:
                await ws.send(json.dumps({"type": "error", "msg": "ASR 模型未加载"}))
                return
        await asr_session(ws, url, proxy)
    except asyncio.TimeoutError:
        try:
            await ws.send(json.dumps({"type": "error", "msg": "等待 url 超时"}))
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send(json.dumps({"type": "error", "msg": str(e)}))
        except Exception:  # noqa: BLE001
            pass


def make_server(host, port):
    """返回 websockets 服务协程；需在 asyncio 事件循环里 run_until_complete。

    模型不在启动时加载（懒加载），故这里永远返回服务对象。
    """
    import websockets
    print("[ASR] 字幕 WebSocket 监听 ws://%s:%d（模型在首次使用时加载）" % (host, port))
    return websockets.serve(_handler, host, port)


def run_server_thread(host, port, stop_ev=None):
    """在独立线程里跑 ASR WebSocket 服务（模型懒加载，启动几乎零开销）。"""
    import websockets  # noqa: F401
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _serve():
        # websockets.serve 是同步构造，必须在运行中的事件循环里调用
        async with websockets.serve(_handler, host, port):
            print("[ASR] 字幕服务已就绪 ws://%s:%d（模型在首次使用时加载）" % (host, port))
            await loop.create_future()   # 永远运行，直到线程/进程退出

    try:
        loop.run_until_complete(_serve())
    except Exception as e:  # noqa: BLE001
        try:
            with open(os.path.join(os.environ.get("TEMP", "C:/tmp"),
                                    "asr_err.log"), "a", encoding="utf-8") as f:
                import traceback
                f.write("ASR thread error: %r\n" % e)
                traceback.print_exc(file=f)
        except Exception:
            pass
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass
