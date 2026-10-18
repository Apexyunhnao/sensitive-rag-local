"""飞书权限探测：确认凭据/权限够不够，并生成「部门 id → 密级」映射模板。

为什么需要它：权限是一层层叠的，缺任何一层都只报一个含糊的错。
  取 token        → 凭据对不对
  /contact/v3/departments  → 有没有 contact:department.base:readonly
  /contact/v3/users/{id}   → 有没有 contact:contact.base:readonly + contact:user.department:readonly
                              （只有后者才返回 department_ids）

用法：
  python scripts/feishu_probe.py                 # token + 部门列表 + 生成 depmap 模板
  python scripts/feishu_probe.py --phone 138xxxx # 额外验证：手机号 → 该人的 department_ids
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from feishu_bridge import APP_ID, APP_SECRET, FEISHU_BASE, MOCK, OUT  # noqa: E402


def post(path: str, payload: dict, token: str = "") -> dict:
    req = urllib.request.Request(
        f"{FEISHU_BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def get(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{FEISHU_BASE}{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def tenant_token() -> str:
    d = post("/open-apis/auth/v3/tenant_access_token/internal",
             {"app_id": APP_ID, "app_secret": APP_SECRET})
    if d.get("code") != 0:
        raise SystemExit(f"✗ 取 token 失败 code={d.get('code')} msg={d.get('msg')}\n"
                         f"  常见原因：app_secret 填错 / 应用未发布")
    return d["tenant_access_token"]


def list_departments(tok: str) -> list[dict]:
    """一级 + 二级部门（小公司两级够了；更多层改成递归）。"""
    out: list[dict] = []
    for parent in ["0"]:
        try:
            d = get(f"/open-apis/contact/v3/departments?page_size=50"
                    f"&parent_department_id={parent}"
                    f"&department_id_type=open_department_id", tok)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"✗ 列部门失败 HTTP {e.code}：{e.read().decode()[:200]}\n"
                             f"  需要权限 contact:department.base:readonly") from None
        if d.get("code") != 0:
            raise SystemExit(f"✗ 列部门失败 code={d.get('code')} msg={d.get('msg')}\n"
                             f"  需要权限 contact:department.base:readonly")
        for it in d.get("data", {}).get("items", []):
            out.append({"id": it.get("open_department_id") or it.get("department_id"),
                        "name": it.get("name", ""), "level": 1})
    # 二级
    for dep in list(out):
        try:
            d = get(f"/open-apis/contact/v3/departments?page_size=50"
                    f"&parent_department_id={dep['id']}"
                    f"&department_id_type=open_department_id", tok)
            for it in d.get("data", {}).get("items", []):
                out.append({"id": it.get("open_department_id") or it.get("department_id"),
                            "name": "  └ " + it.get("name", ""), "level": 2})
        except Exception:  # noqa: BLE001
            pass
    return out


def user_departments(tok: str, phone: str) -> tuple[str, list[str]]:
    """手机号 → open_id → department_ids（验证权限链是否完整）。"""
    d = post("/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id",
             {"mobiles": [phone]}, tok)
    if d.get("code") != 0:
        return "", [f"batch_get_id 失败 code={d.get('code')} {d.get('msg')}（需 contact:user.id:readonly）"]
    users = d.get("data", {}).get("user_list", [])
    oid = (users[0].get("user_id") or "") if users else ""
    if not oid:
        return "", ["该手机号不在通讯录（或不在应用通讯录权限范围内）"]
    u = get(f"/open-apis/contact/v3/users/{oid}?user_id_type=open_id", tok)
    if u.get("code") != 0:
        return oid, [f"查用户失败 code={u.get('code')} {u.get('msg')}（需 contact:contact.base:readonly）"]
    user = u.get("data", {}).get("user", {})
    deps = user.get("department_ids") or []
    if not deps:
        return oid, ["department_ids 为空 —— 缺 contact:user.department:readonly"]
    return oid, deps


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="", help="用手机号验证某人的部门能否查到")
    a = ap.parse_args()

    if MOCK:
        print("✗ 没读到凭据。先在 " + str(ROOT / ".env") + " 里填 FEISHU_APP_ID / FEISHU_APP_SECRET")
        sys.exit(1)

    print("1) 取 tenant_access_token …")
    tok = tenant_token()
    print(f"   ✓ 成功（{len(tok)} 字符）")

    print("\n2) 列部门 …")
    deps = list_departments(tok)
    if not deps:
        print("   ⚠ 没查到部门（应用通讯录权限范围可能是空的）")
    for d in deps:
        print(f"   {d['id']}  {d['name']}")

    tmpl = OUT / "feishu_depmap.template.json"
    guess = {}
    for d in deps:
        n = d["name"].replace("└", "").strip()
        if any(k in n for k in ["合规", "法务", "内审", "审计", "风控"]):
            guess[d["id"]] = ["compliance"]
        elif any(k in n for k in ["人事", "人力", "HR", "行政"]):
            guess[d["id"]] = ["hr"]
    tmpl.write_text(json.dumps(guess or {d["id"]: [] for d in deps},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n   已生成映射模板: {tmpl}")
    print(f"   我的猜测（按名字猜的，必须你核对）: {json.dumps(guess, ensure_ascii=False)}")

    if a.phone:
        print(f"\n3) 用手机号 {a.phone} 验证部门查询 …")
        oid, info = user_departments(tok, a.phone)
        print(f"   open_id: {oid or '(未取到)'}")
        print(f"   department_ids: {info}")
        missing = [i for i in info if isinstance(i, str) and ("缺" in i or "失败" in i)]
        if missing:
            print("   ⚠ 权限链不完整 —— 上面的提示就是要补的权限")
        else:
            print("   ✓ 权限链完整，身份映射可以按部门走")

    print("\n下一步：把模板复制成 out/feishu_depmap.json 并填好密级，然后 python scripts/feishu_bridge.py --ws")


if __name__ == "__main__":
    main()
