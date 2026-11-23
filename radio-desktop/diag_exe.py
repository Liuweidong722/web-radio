# -*- coding: utf-8 -*-
"""诊断打包后 exe 的 Vosk 模型路径解析：启动 exe（无界面）→ 等待 ASR 线程写
日志 → 打印真实 MEIPASS 与候选路径。"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(os.environ.get("TEMP", "C:/tmp"), "asr_err.log")
EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ["USERPROFILE"], "AppData", "Local", "Temp", "rb_build6", "dist", "网络收音机.exe")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 58960

# 清掉旧日志
try:
    os.remove(LOG)
except Exception:
    pass

env = dict(os.environ, PORT=str(PORT), WEBRADIO_HEADLESS="1", PYTHONUNBUFFERED="1")
print("[diag] launching", EXE)
proc = subprocess.Popen([EXE], env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1)

# 等 ASR 线程跑 find_model（最多 ~15s）
for _ in range(30):
    if os.path.isfile(LOG) and os.path.getsize(LOG) > 0:
        break
    if proc.poll() is not None:
        print("[diag] exe exited early code=%s" % proc.returncode)
        break
    time.sleep(0.5)

time.sleep(2)
try:
    proc.kill()
except Exception:
    pass

print("===== asr_err.log =====")
try:
    with open(LOG, "r", encoding="utf-8") as f:
        print(f.read())
except Exception as e:
    print("(no log: %s)" % e)
