"""权限网关：用户 → 部门 → 对应 Dify 应用（按密级路由）+ 审计留痕。

为什么需要网关：Dify 社区版的知识库绑定在应用上，chat API 不能动态指定库。
所以权限隔离 = 一个密级一个应用，网关按用户身份选应用。
用户拿不到 app key，也无法直接访问 Dify，越权在网关上就被挡住。

启动：python scripts/gateway.py
调用：POST http://127.0.0.1:8090/ask  {"user": "zhang", "question": "..."}
审计：GET  http://127.0.0.1:8090/audit
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = "http://127.0.0.1"
PORT = 8090
ROOT = Path(__file__).resolve().parent.parent
TIERS = json.loads((ROOT / ".dify-tiered.json").read_text(encoding="utf-8"))
AUDIT_DB = ROOT / "out" / "gateway_audit.db"

# 用户 → 所属组 + 访问令牌。真实系统从 AD/LDAP/OA 同步，令牌由 SSO 签发。
# 这里用预共享令牌模拟：请求必须带 Authorization: Bearer <token>，
# 否则任何人都能伪造 user 字段绕过权限。
USERS = {
    "zhang": {"groups": ["compliance"], "name": "张三（合规部）",
              "token": "tk-zhang-7f3a9c2e"},
    "wang": {"groups": ["board"], "name": "王五（董事）",
             "token": "tk-wang-4b8d1f6a"},
    "li": {"groups": ["hr"], "name": "李四（人事部）",
           "token": "tk-li-9e2c5d7b"},
    "guest": {"groups": [], "name": "访客",
              "token": "tk-guest-1a3f8e4c"},
}
TOKEN_TO_USER = {v["token"]: u for u, v in USERS.items()}

# 每个知识库允许的组
TIER_GROUPS = {tier: set(cfg["groups"]) for tier, cfg in TIERS.items()}


def audit_init() -> None:
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(AUDIT_DB, timeout=10) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, user TEXT, group_names TEXT, action TEXT,
            query_hash TEXT, query_len INTEGER,
            routed_tier TEXT, allowed_tiers TEXT, blocked INTEGER, latency_ms INTEGER)""")


def audit_write(user: str, action: str, question: str, tier: str,
                allowed: list[str], blocked: bool, latency_ms: int) -> None:
    with sqlite3.connect(AUDIT_DB, timeout=10) as c:
        c.execute("""INSERT INTO audit (ts,user,group_names,action,query_hash,query_len,
                     routed_tier,allowed_tiers,blocked,latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (time.strftime("%Y-%m-%d %H:%M:%S"), user,
                   "|".join(USERS.get(user, {}).get("groups", [])), action,
                   hashlib.sha256(question.encode()).hexdigest()[:16], len(question),
                   tier, "|".join(allowed), int(blocked), latency_ms))


def allowed_tiers(user: str) -> list[str]:
    groups = set(USERS.get(user, {}).get("groups", []))
    return [t for t, gs in TIER_GROUPS.items() if groups & gs]


def call_dify(app_key: str, question: str) -> tuple[bool, str, list]:
    payload = json.dumps({"inputs": {}, "query": question, "response_mode": "blocking",
                          "conversation_id": "", "user": "gateway"}).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat-messages", data=payload,
                                 headers={"Authorization": f"Bearer {app_key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        return True, d.get("answer", ""), d.get("metadata", {}).get("retriever_resources", [])
    except urllib.error.HTTPError as e:
        return False, f"上游错误 {e.code}: {e.read().decode()[:200]}", []
    except Exception as e:
        return False, f"调用失败: {e}", []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # 静音默认日志
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/ask":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return

        # 身份只从令牌推导，绝不信任请求体里的 user 字段（防伪造）
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        user = TOKEN_TO_USER.get(token)
        if not user:
            t0 = time.time()
            audit_write("-", "auth_fail", "", "", [], True, int((time.time() - t0) * 1000))
            self._send(401, {"error": "缺少或无效的访问令牌"})
            return

        # 请求体若带 user 字段且与令牌不符，视为越权尝试
        claimed = body.get("user", "")
        if claimed and claimed != user:
            t0 = time.time()
            audit_write(user, "impersonation", "", "", [], True, int((time.time() - t0) * 1000))
            self._send(403, {"error": "令牌与声明身份不符"})
            return

        question = body.get("question", "").strip()
        if not question:
            self._send(400, {"error": "需要 question"})
            return

        t0 = time.time()
        tiers = allowed_tiers(user)
        if not tiers:
            audit_write(user, "ask", question, "", [], True, int((time.time() - t0) * 1000))
            self._send(403, {"error": "无任何知识库访问权限", "user": USERS[user]["name"]})
            return

        # 路由规则：按密级顺序（合规 > 人事），命中的第一个
        tier = tiers[0]
        cfg = TIERS[tier]
        ok, answer, refs = call_dify(cfg["app_key"], question)
        latency = int((time.time() - t0) * 1000)
        audit_write(user, "ask", question, tier, tiers, not ok, latency)

        if not ok:
            self._send(502, {"error": answer, "routed": tier})
            return

        self._send(200, {
            "user": USERS[user]["name"],
            "group_names": USERS[user]["groups"],
            "routed_tier": tier,
            "answer": answer,
            "sources": [{"doc": r.get("document_name"), "score": round(r.get("score", 0), 3)}
                        for r in refs[:5]],
            "latency_ms": latency,
        })

    def do_GET(self):
        if self.path != "/audit":
            self._send(404, {"error": "not found"})
            return
        with sqlite3.connect(AUDIT_DB, timeout=10) as c:
            rows = c.execute("""SELECT ts,user,group_names,routed_tier,allowed_tiers,blocked,latency_ms
                                FROM audit ORDER BY id DESC LIMIT 30""").fetchall()
        self._send(200, {"records": [
            {"ts": r[0], "user": r[1], "groups": r[2], "routed": r[3],
             "allowed": r[4], "blocked": bool(r[5]), "ms": r[6]} for r in rows]})


def main() -> None:
    audit_init()
    print(f"权限网关启动: http://127.0.0.1:{PORT}")
    print("用户:", {u: v["groups"] for u, v in USERS.items()})
    print("密级路由:", {t: sorted(g) for t, g in TIER_GROUPS.items()})
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
