"""只做一件事：把 SYSTEM 提示词写进正确的编辑器（contenteditable），然后发布。"""
from __future__ import annotations

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

        # 确认模型与知识库仍在
        print("模型今天显示:", pg.get_by_text("qwen2.5:7b", exact=False).first.is_visible())
        print("知识库已关联:", pg.get_by_text("敏感", exact=True).first.is_visible())

        editors = pg.locator('[contenteditable="true"]').all()
        print(f"contenteditable 数量: {len(editors)}")
        # 第一个是 SYSTEM 提示词编辑器（页面最上方的编辑区）
        ed = editors[0]
        box = ed.bounding_box()
        print("提示词编辑器位置:", box)

        ed.click()
        pg.wait_for_timeout(500)
        ed.fill(PROMPT)
        pg.wait_for_timeout(400)
        # 手打变量，触发 Dify 的变量识别
        pg.keyboard.type("{{#context#}}")
        pg.wait_for_timeout(1500)

        pg.screenshot(path="E:/Projects/sensitive-rag-demo/out/prompt_written.png")

        # 发布
        pg.get_by_role("button", name="发布").first.click()
        pg.wait_for_timeout(3500)
        pg.screenshot(path="E:/Projects/sensitive-rag-demo/out/published2.png")
        print("已发布")

        ctx.close()


if __name__ == "__main__":
    main()
