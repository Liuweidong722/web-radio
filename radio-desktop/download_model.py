# -*- coding: utf-8 -*-
"""稳健下载 Vosk 模型 zip（带重试 + 校验）。"""
import os
import sys
import time
import urllib.request
import zipfile

URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODELDIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.expanduser("~"), ".workbuddy", "binaries", "python",
    "envs", "radio-sub", "models")
os.makedirs(MODELDIR, exist_ok=True)
zip_path = os.path.join(MODELDIR, "model.zip")


def download():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(1, 7):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length", "0"))
                done = 0
                with open(zip_path, "wb") as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                print("downloaded %d bytes (expected %d)" % (done, total))
                return done
        except Exception as e:  # noqa: BLE001
            print("attempt %d failed: %s" % (attempt, e))
            time.sleep(2)
    return 0


def main():
    # 若已存在坏文件先删
    if os.path.exists(zip_path) and not zipfile.is_zipfile(zip_path):
        os.remove(zip_path)
    n = download()
    if n == 0:
        print("DOWNLOAD_FAILED")
        return
    if not zipfile.is_zipfile(zip_path):
        print("BAD_ZIP")
        return
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(MODELDIR)
    found = [d for d in os.listdir(MODELDIR) if d.startswith("vosk-model")]
    print("EXTRACTED", found)
    try:
        os.remove(zip_path)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
