"""拿 Dify 登录态（token + csrf + 全部 cookie）存 JSON，供其他脚本调用 API 用。

Dify 的 CSRF 校验依赖 session cookie，只带 access_token 不够。
"""
import json
import pathlib

from playwright.sync_api import sync_playwright

PROFILE = "C:/Users/31619/AppData/Local/hermes/cache/dify-browser"
OUT = pathlib.Path("E:/Projects/sensitive-rag-demo/.dify-auth.json")

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(PROFILE, channel="msedge", headless=True)
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("http://127.0.0.1/apps", wait_until="domcontentloaded")
    pg.wait_for_timeout(1500)
    cookies = ctx.cookies()
    ctx.close()

jar = {c["name"]: c["value"] for c in cookies}
data = {
    "access_token": jar.get("access_token", ""),
    "csrf_token": jar.get("csrf_token", ""),
    "cookies": {c["name"]: c["value"] for c in cookies if c["domain"] in ("127.0.0.1", "localhost", "")},
}
OUT.write_text(json.dumps(data), encoding="utf-8")
print("已保存:", OUT)
print("cookie 数:", len(data["cookies"]), "| 名称:", list(data["cookies"].keys()))
