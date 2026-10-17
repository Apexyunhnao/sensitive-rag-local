"""从 Ollama registry 分块并发下载模型（国内 ollama pull 卡死时的替代方案）。

背景：`ollama pull` 在本网络长期 `part stalled`，但 registry 的 HTTP 接口直连正常，
单连接限速约 600 KB/s。16 并发 Range 分段可达 7 MB/s。

用法：
    python scripts/pull_ollama_model.py bge-m3
    python scripts/pull_ollama_model.py bge-m3 --tag latest --conn 16

完成后模型直接出现在 `ollama list` 里。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REGISTRY = "https://registry.ollama.ai/v2/library"
OLLAMA_MODELS = Path(os.path.expanduser("~")) / ".ollama" / "models"
BLOBS = OLLAMA_MODELS / "blobs"
MANIFESTS = OLLAMA_MODELS / "manifests" / "registry.ollama.ai" / "library"

CHUNK = 32 * 1024 * 1024      # 每块 32MB
RETRIES = 6


def fetch_manifest(model: str, tag: str) -> dict:
    url = f"{REGISTRY}/{model}/manifests/{tag}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.docker.distribution.manifest.v2+json"
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def blob_path(digest: str) -> Path:
    return BLOBS / digest.replace(":", "-")


def download_chunk(url: str, start: int, end: int, dest: Path) -> bytes:
    """下载一个 Range 片段，失败重试。返回数据本身（由调用方拼装）。"""
    last_err = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) == end - start + 1:
                return data
            last_err = f"长度不符 {len(data)} != {end - start + 1}"
        except Exception as e:
            last_err = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"块 {start}-{end} 失败: {last_err}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--tag", default="latest")
    ap.add_argument("--conn", type=int, default=16)
    args = ap.parse_args()

    model, tag = args.model, args.tag
    print(f"[manifest] {model}:{tag}")
    man = fetch_manifest(model, tag)

    BLOBS.mkdir(parents=True, exist_ok=True)
    layers = man.get("layers", [])
    cfg = man.get("config", {})
    all_layers = ([cfg] if cfg else []) + layers

    tmpdir = BLOBS / f".dl-{model.replace(':', '_')}"
    tmpdir.mkdir(exist_ok=True)

    for layer in all_layers:
        digest = layer["digest"]
        size = layer["size"]
        dest = blob_path(digest)
        if dest.exists() and dest.stat().st_size == size:
            print(f"  [skip] {digest[7:26]} {size/1024/1024:.1f}MB 已存在")
            continue

        print(f"  [get ] {digest[7:26]} {size/1024/1024:.1f}MB", flush=True)
        url = f"{REGISTRY}/{model}/blobs/{digest}"

        if size < 1024 * 1024:                      # 小文件直接拿
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            dest.write_bytes(data)
            print(f"        → {len(data)} 字节")
            continue

        # 大文件：切块并发
        ranges = [(s, min(s + CHUNK - 1, size - 1)) for s in range(0, size, CHUNK)]
        buf: dict[int, bytes] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.conn) as ex:
            futs = {ex.submit(download_chunk, url, s, e, dest): (i, s, e)
                    for i, (s, e) in enumerate(ranges)}
            for f in as_completed(futs):
                i, s, e = futs[f]
                buf[i] = f.result()
                done = sum(len(v) for v in buf.values())
                print(f"\r        {done/1024/1024:.0f}/{size/1024/1024:.0f} MB "
                      f"({done/max(time.time()-t0,0.1)/1024/1024:.1f} MB/s)", end="", flush=True)

        with open(dest, "wb") as f:
            for i in range(len(ranges)):
                f.write(buf[i])
        got = dest.stat().st_size
        print(f"\n        → {got} 字节 (期望 {size}) {'OK' if got == size else '❌ 不符'}")
        if got != size:
            sys.exit("blob 长度不符，终止")

    # 写 manifest
    mdir = MANIFESTS / model
    mdir.mkdir(parents=True, exist_ok=True)
    mfile = mdir / tag
    mfile.write_text(json.dumps(man, indent=2), encoding="utf-8")
    print(f"[done] manifest → {mfile}")
    print("验证: ollama list")


if __name__ == "__main__":
    main()
