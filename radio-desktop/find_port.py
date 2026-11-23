import subprocess, time, re, os

EXE = r"C:\Users\Liu Weidong\WorkBuddy\2026-08-02-10-19-03\radio-desktop\dist\网络收音机.exe"
env = dict(os.environ)
env["PYTHONUNBUFFERED"] = "1"
p = subprocess.Popen([EXE], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, bufsize=1, env=env)
port = None
deadline = time.time() + 30
while time.time() < deadline:
    line = p.stdout.readline()
    if not line:
        if p.poll() is not None:
            print("EXE_EXITED", p.returncode)
            break
        continue
    print("EXE_OUT:", line.rstrip())
    m = re.search(r"127\.0\.0\.1:(\d+)", line)
    if m:
        port = int(m.group(1))
        break
if port:
    with open(r"C:\Users\Liu Weidong\WorkBuddy\2026-08-02-10-19-03\radio-desktop\.rb_port.txt", "w") as f:
        f.write(str(port))
    print("PORT_FOUND", port)
else:
    print("NO_PORT")
