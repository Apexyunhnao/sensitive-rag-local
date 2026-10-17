"""打开 Dify 浏览器窗口并等待登录，登录后保存会话状态。

用法：python3 dify_login.py
登录一次后，dify-state.json 可复用，后续自动化无需再登录。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = "C:/Users/31619/AppData/Local/hermes/cache/dify-browser"
STATE = Path("E:/Projects/sensitive-rag-demo/dify-state.json")


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE,
            channel="msedge",
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("http://127.0.0.1/", wait_until="domcontentloaded")
        print("已打开 Dify，当前地址:", page.url, flush=True)

        if "signin" in page.url:
            print(">>> 请在打开的窗口里登录（邮箱+密码），登录后脚本自动继续 <<<", flush=True)
        else:
            print("看起来已登录", flush=True)

        # 等 URL 离开 signin 页
        deadline = time.time() + 300
        while time.time() < deadline:
            if "signin" not in page.url:
                break
            time.sleep(1)

        if "signin" in page.url:
            print("❌ 5 分钟内未完成登录，退出", flush=True)
            ctx.close()
            sys.exit(1)

        print("✓ 登录成功，地址:", page.url, flush=True)
        time.sleep(2)
        ctx.storage_state(path=str(STATE))
        print(f"✓ 会话已保存到 {STATE}", flush=True)
        print("可以关闭这个窗口了（或留着，脚本会自己关）", flush=True)
        time.sleep(3)
        ctx.close()


if __name__ == "__main__":
    main()
