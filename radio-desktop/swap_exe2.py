import os, time
TMP = os.path.join(os.environ["USERPROFILE"], "AppData", "Local", "Temp", "rb_build3", "dist", "网络收音机.exe")
DST = r"C:/Users/Liu Weidong/WorkBuddy/2026-08-02-10-19-03/radio-desktop/dist/网络收音机.exe"
NEW = DST[:-4] + "_new.exe"
# 1) 复制为新名（避免占用锁）
for i in range(3):
    try:
        with open(TMP, "rb") as fsrc, open(NEW, "wb") as fdst:
            while True:
                b = fsrc.read(1024 * 1024)
                if not b:
                    break
                fdst.write(b)
        print("copied new ->", NEW, os.path.getsize(NEW))
        break
    except Exception as e:
        print("copy attempt", i, "failed:", e)
        time.sleep(2)
else:
    raise SystemExit("COPY_FAILED")
# 2) 旧文件改名 aside
bak = DST + ".bak2"
for i in range(3):
    try:
        if os.path.exists(bak):
            os.remove(bak)
        if os.path.exists(DST):
            os.rename(DST, bak)
        print("renamed old -> .bak2")
        break
    except Exception as e:
        print("rename attempt", i, "failed:", e)
        time.sleep(2)
else:
    raise SystemExit("RENAME_FAILED")
# 3) 新文件顶上
for i in range(3):
    try:
        os.replace(NEW, DST)
        print("REPLACED ->", DST, os.path.getsize(DST))
        break
    except Exception as e:
        print("replace attempt", i, "failed:", e)
        time.sleep(2)
else:
    raise SystemExit("REPLACE_FAILED")
try:
    os.remove(bak)
    print("cleaned .bak2")
except Exception as e:
    print("bak cleanup:", e)
