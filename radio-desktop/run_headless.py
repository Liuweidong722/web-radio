# 以无界面模式启动打包后的 exe（确保 WEBRADIO_HEADLESS 环境变量传入子进程）
import subprocess, sys, os

EXE = r"C:\Users\Liu Weidong\WorkBuddy\2026-08-02-10-19-03\radio-desktop\dist\网络收音机.exe"
env = dict(os.environ)
env["WEBRADIO_HEADLESS"] = "1"
p = subprocess.Popen([EXE], env=env)
print("headless exe pid =", p.pid)
p.wait()
print("exe exited code =", p.returncode)
