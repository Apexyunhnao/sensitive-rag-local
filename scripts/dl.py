"""多线程分段下载器 —— 绕过单连接限速。

用法：
    python dl.py <url> <输出文件> [线程数]

支持断点续传（记录 .part 状态），中断后重跑会接着下。
"""
import os
import sys
import threading
import time
import urllib.request

CHUNK = 8 * 1024 * 1024


def head_info(url):
    import requests

    r = requests.head(url, timeout=20, allow_redirects=True)
    size = int(r.headers.get("Content-Length", 0))
    return size, r.headers.get("Accept-Ranges", "")


def download(url, out, threads=8):
    import requests

    size, ranges = head_info(url)
    if size == 0:
        print("无法获取文件大小")
        return False
    if os.path.exists(out) and os.path.getsize(out) == size:
        print(f"已存在完整文件：{out}")
        return True

    # 预分配
    with open(out, "wb") as f:
        f.truncate(size)

    # 断点状态
    state_path = out + ".state"
    done = set()
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                done = {int(x) for x in f.read().split() if x.strip()}
            print(f"续传：已完成 {len(done)} 块")
        except Exception:
            done = set()

    chunks = [(i, min(i + CHUNK, size)) for i in range(0, size, CHUNK)]
    todo = [(i, s, e) for i, (s, e) in enumerate(chunks) if i not in done]
    print(f"文件 {size/1024/1024:.0f} MB，共 {len(chunks)} 块，待下 {len(todo)} 块，线程 {threads}")

    lock = threading.Lock()
    downloaded = [0]
    t0 = time.time()
    stop = [False]

    def worker():
        while not stop[0]:
            with lock:
                if not todo:
                    return
                idx, s, e = todo.pop(0)
            for attempt in range(6):
                try:
                    req = requests.get(
                        url, headers={"Range": f"bytes={s}-{e-1}"}, timeout=90, stream=True
                    )
                    req.raise_for_status()
                    data = req.content
                    with open(out, "r+b") as f:
                        f.seek(s)
                        f.write(data)
                    with lock:
                        downloaded[0] += len(data)
                        with open(state_path, "a") as sf:
                            sf.write(f"{idx}\n")
                    break
                except Exception as ex:
                    if attempt == 5:
                        print(f"\n块 {idx} 失败: {ex}")
                        stop[0] = True
                        return
                    time.sleep(1 + attempt)

    ths = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for t in ths:
        t.start()

    while any(t.is_alive() for t in ths):
        time.sleep(3)
        el = time.time() - t0
        sp = downloaded[0] / el / 1024 if el else 0
        pct = downloaded[0] / size * 100
        eta = (size - downloaded[0]) / (downloaded[0] / el) / 60 if downloaded[0] else 0
        print(f"\r  {pct:5.1f}%  {downloaded[0]/1024/1024:7.0f}MB  {sp:6.0f}KB/s  剩余约 {eta:4.0f} 分钟", end="", flush=True)
    print()
    ok = os.path.getsize(out) == size
    if ok and os.path.exists(state_path):
        os.remove(state_path)
    print("完成" if ok else "未完成（重跑可续传）")
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    download(sys.argv[1], sys.argv[2], n)
