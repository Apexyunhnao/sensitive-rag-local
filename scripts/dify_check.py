"""复用已登录的浏览器 profile，检查 Dify 登录态并截图。"""
from playwright.sync_api import sync_playwright

PROFILE = "C:/Users/31619/AppData/Local/hermes/cache/dify-browser"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(PROFILE, channel="msedge", headless=True,
                                               viewport={"width": 1440, "height": 900})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("http://127.0.0.1/apps", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    print("URL:", page.url)
    print("标题:", page.title())
    # 登录态判断：cookie 里有 access_token
    cookies = ctx.cookies()
    names = [c["name"] for c in cookies]
    print("关键 cookie:", [n for n in names if "token" in n.lower() or "csrf" in n.lower()])
    page.screenshot(path="E:/Projects/sensitive-rag-demo/out/dify_check.png", full_page=False)
    print("截图: out/dify_check.png")
    ctx.close()
