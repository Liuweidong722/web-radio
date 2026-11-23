import os, time

base = "C:/Users/Liu Weidong/WorkBuddy/2026-08-02-10-19-03/radio-desktop/dist"
d = os.path.join(base, "网络收音机.exe")
bak = d + ".bak"
new = os.path.join(base, "网络收音机_new.exe")

for i in range(4):
    try:
        if os.path.exists(bak):
            try: os.remove(bak)
            except Exception: pass
        os.rename(d, bak)
        print("renamed old -> .bak")
        break
    except Exception as e:
        print("rename attempt", i, "failed:", e)
        time.sleep(3)
else:
    print("RENAME_FAILED")
    raise SystemExit(1)

for i in range(4):
    try:
        os.replace(new, d)
        print("REPLACED new -> 网络收音机.exe", os.path.getsize(d))
        break
    except Exception as e:
        print("replace attempt", i, "failed:", e)
        time.sleep(3)
else:
    print("REPLACE_FAILED")
    raise SystemExit(1)

try:
    os.remove(bak)
    print("cleaned .bak")
except Exception as e:
    print("bak cleanup:", e)
