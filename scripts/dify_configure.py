"""一次性完成：切模型 → 关联知识库 → 写提示词 → 发布。

关键：Dify 的界面状态在页面刷新后回到已发布的版本，所有改动必须同一会话里做完并点「发布」。
"""
from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

PROFILE = "C:/Users/31619/AppData/Local/hermes/cache/dify-browser"
APP_ID = "e7349827-f654-4f60-8473-1068a786ada6"
CONFIG_URL = f"http://127.0.0.1/app/{APP_ID}/configuration"

PROMPT = (
    "你是一个港股公告审阅助手。根据以下资料回答问题，只用资料中已有的信息，"
    "资料里没有就说\"未提及\"。回答简洁，末尾标注来源文档名。\n\n资料：\n"
)


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="msedge", headless=True,
            viewport={"width": 1600, "height": 1000})
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(CONFIG_URL, wait_until="domcontentloaded")
        pg.wait_for_timeout(4500)

        # 1) 切模型
        pg.get_by_text("DeepSeek V4.1 Flash", exact=False).first.click()
        pg.wait_for_timeout(1800)
        pg.get_by_text("qwen2.5:7b", exact=False).first.click()
        pg.wait_for_timeout(2000)
        print("1) 模型 → qwen2.5:7b")

        # 2) 关联知识库
        pg.get_by_text("添加", exact=True).nth(1).click()
        pg.wait_for_timeout(2000)
        pg.get_by_text("敏感", exact=True).first.click()
        pg.wait_for_timeout(800)
        try:
            pg.get_by_text("确定", exact=True).first.click(timeout=8000)
        except Exception:
            pg.mouse.click(942, 582)       # 兜底：确定按钮坐标
        pg.wait_for_timeout(2000)
        print("2) 知识库 → 敏感")

        # 3) 写提示词
        ta = pg.locator("textarea").first
        ta.click()
        ta.fill(PROMPT)
        pg.wait_for_timeout(500)
        # 插入 context 变量（手打会触发 Dify 的变量识别，fill 不会）
        pg.keyboard.type("{{#context#}}")
        pg.wait_for_timeout(1500)
        print("3) 提示词已写入（含 context 变量）")

        pg.screenshot(path="E:/Projects/sensitive-rag-demo/out/before_publish.png")

        # 4) 发布
        pub = pg.get_by_role("button", name="发布").first
        pub.click()
        pg.wait_for_timeout(3500)
        pg.screenshot(path="E:/Projects/sensitive-rag-demo/out/after_publish.png")
        print("4) 已点发布")

        ctx.close()


if __name__ == "__main__":
    main()
