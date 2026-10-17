"""完整配置：模型 + 知识库 + 提示词 + 发布更新，完成后重新加载验证是否真的保存。"""
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

        # 1) 模型
        if not pg.get_by_text("qwen2.5:7b", exact=False).first.is_visible():
            pg.get_by_text("DeepSeek V4.1 Flash", exact=False).first.click()
            pg.wait_for_timeout(1800)
            pg.get_by_text("qwen2.5:7b", exact=False).first.click()
            pg.wait_for_timeout(2000)
        print("1) 模型 qwen2.5:7b")

        # 2) 知识库
        if not pg.get_by_text("敏感", exact=True).first.is_visible():
            pg.get_by_text("添加", exact=True).nth(1).click()
            pg.wait_for_timeout(2000)
            pg.get_by_text("敏感", exact=True).first.click()
            pg.wait_for_timeout(800)
            try:
                pg.get_by_text("确定", exact=True).first.click(timeout=8000)
            except Exception:
                pg.mouse.click(942, 582)
            pg.wait_for_timeout(2000)
        print("2) 知识库 敏感")

        # 3) 提示词（contenteditable）
        ed = pg.locator('[contenteditable="true"]').first
        ed.click()
        pg.wait_for_timeout(400)
        ed.fill(PROMPT)
        pg.wait_for_timeout(400)
        pg.keyboard.type("{{#context#}}")
        pg.wait_for_timeout(1500)
        print("3) 提示词已写入")

        pg.screenshot(path="E:/Projects/sensitive-rag-demo/out/cfg_before_publish.png")

        # 4) 发布 → 发布更新
        pg.get_by_role("button", name="发布").first.click()
        pg.wait_for_timeout(2000)
        pg.screenshot(path="E:/Projects/sensitive-rag-demo/out/cfg_menu.png")
        try:
            pg.get_by_text("发布更新", exact=False).first.click(timeout=8000)
            print("4) 已点发布更新")
        except Exception as e:
            print("4) 找不到发布更新:", str(e)[:100])
        pg.wait_for_timeout(4000)
        pg.screenshot(path="E:/Projects/sensitive-rag-demo/out/cfg_after_publish.png")

        # 5) 重新加载验证
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(5000)
        model_ok = pg.get_by_text("qwen2.5:7b", exact=False).first.is_visible()
        kb_ok = pg.get_by_text("敏感", exact=True).first.is_visible()
        body = pg.locator('[contenteditable="true"]').first.inner_text()
        prompt_ok = "港股公告审阅助手" in body
        print("\n=== 重新加载后验证 ===")
        print("模型 qwen2.5:7b:", model_ok)
        print("知识库 敏感:", kb_ok)
        print("提示词已保存:", prompt_ok, "|", body[:60].replace("\n", " "))
        pg.screenshot(path="E:/Projects/sensitive-rag-demo/out/cfg_verify.png")
        ctx.close()


if __name__ == "__main__":
    main()
