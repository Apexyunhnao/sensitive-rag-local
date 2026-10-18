"""飞书接入层：事件回调 → 立即返回 → 异步处理 → 回消息。

为什么必须异步（实测数据，不是猜测）：
  飞书事件回调要求秒级响应，超时它会重试，用户就会收到重复回复。
  本机实测链路：新问题 12.2s（qwen2.5:7b 完整生成答案），
                已预热过的相似问题 0.5~1.2s。
  ⇒ 文本问答也必须异步，不能只在 OCR 场景才异步。

三种保护：
  1. 立即 200：回调里只做「验签 + 去重 + 入队」，不碰模型
  2. event_id 幂等：飞书重试同一条事件时直接丢弃（sqlite 主键，原子）
  3. 身份只从 open_id 查：绝不信任消息体里自报的部门

两种运行模式：
  mock（默认，无需凭据）：open_id → 部门 从 out/feishu_users.json 读；回复写 out/feishu_replies.jsonl
  real：需要 FEISHU_APP_ID / FEISHU_APP_SECRET，走飞书开放平台 open.feishu.cn

用法：
  python scripts/feishu_bridge.py --selftest        # 端到端自测（不需要飞书凭据）
  python scripts/feishu_bridge.py --port 8091       # 起服务
  python scripts/feishu_bridge.py --load            # 只看身份映射和配置状态

飞书后台配置（接真环境时）：
  事件订阅地址 → http://<内网地址>:8091/feishu/event
  订阅事件     → im.message.receive_v1（接收消息）、contact.user.deleted_v3（离职吊销）
  权限         → im:message、contact:user.base:readonly
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
DB = OUT / "feishu_bridge.db"
USERS_FILE = OUT / "feishu_users.json"
REPLY_LOG = OUT / "feishu_replies.jsonl"

GATEWAY = "http://127.0.0.1:8090/ask"
FEISHU_BASE = "https://open.feishu.cn"

APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
VERIFY_TOKEN = os.environ.get("FEISHU_VERIFY_TOKEN", "").strip()
MOCK = not (APP_ID and APP_SECRET)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── 身份映射 ────────────────────────────────────────────────────────────────
# mock 模式读这个文件；真实模式忽略它，改从飞书通讯录查。
DEFAULT_USERS = {
    "ou_mock_zhang": {"name": "张三（合规部）", "groups": ["compliance"]},
    "ou_mock_li": {"name": "李四（人事部）", "groups": ["hr"]},
    "ou_mock_guest": {"name": "访客", "groups": []},
}

# 离职名单：真实模式由 contact.user.deleted_v3 事件写入，这里做兜底
REVOKED_FILE = OUT / "feishu_revoked.json"


def load_local_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    USERS_FILE.write_text(json.dumps(DEFAULT_USERS, ensure_ascii=False, indent=2), encoding="utf-8")
    return DEFAULT_USERS


def load_revoked() -> set[str]:
    if REVOKED_FILE.exists():
        try:
            return set(json.loads(REVOKED_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return set()


def revoke(open_id: str) -> None:
    s = load_revoked()
    s.add(open_id)
    REVOKED_FILE.write_text(json.dumps(sorted(s), ensure_ascii=False, indent=2), encoding="utf-8")


# ── 去重（幂等的真身）────────────────────────────────────────────────────────

def db_init() -> None:
    with sqlite3.connect(DB, timeout=10) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS seen_events (
            event_id TEXT PRIMARY KEY, ts REAL, open_id TEXT, kind TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event_id TEXT,
            open_id TEXT, user_name TEXT, tier TEXT, blocked INTEGER,
            latency_ms INTEGER, answer_len INTEGER, error TEXT)""")


def claim_event(event_id: str, open_id: str, kind: str) -> bool:
    """返回 True = 第一次见到，可以处理；False = 重复事件，丢弃。"""
    try:
        with sqlite3.connect(DB, timeout=10) as c:
            c.execute("INSERT INTO seen_events (event_id, ts, open_id, kind) VALUES (?,?,?,?)",
                      (event_id, time.time(), open_id, kind))
        return True
    except sqlite3.IntegrityError:
        return False


def log_reply(event_id: str, open_id: str, user_name: str, tier: str,
              blocked: bool, latency_ms: int, answer_len: int, error: str = "") -> None:
    with sqlite3.connect(DB, timeout=10) as c:
        c.execute("""INSERT INTO replies (ts,event_id,open_id,user_name,tier,blocked,
                     latency_ms,answer_len,error) VALUES (?,?,?,?,?,?,?,?,?)""",
                  (time.time(), event_id, open_id, user_name, tier, int(blocked),
                   latency_ms, answer_len, error))


# ── 飞书客户端（real 模式）──────────────────────────────────────────────────

class FeishuClient:
    """tenant_access_token + 通讯录查询 + 回复消息。token 缓存到过期前 5 分钟。"""

    def __init__(self) -> None:
        self._token = ""
        self._exp = 0.0
        self._lock = threading.Lock()

    def tenant_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._exp:
                return self._token
            body = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode()
            req = urllib.request.Request(
                f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            if d.get("code") != 0:
                raise RuntimeError(f"取 token 失败 code={d.get('code')} msg={d.get('msg')}")
            self._token = d["tenant_access_token"]
            self._exp = time.time() + max(int(d.get("expire", 7200)) - 300, 60)
            return self._token

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(f"{FEISHU_BASE}{path}",
                                     headers={"Authorization": f"Bearer {self.tenant_token()}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{FEISHU_BASE}{path}", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.tenant_token()}",
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    def user_departments(self, open_id: str) -> list[str]:
        """open_id → 部门名列表。需要 contact:user.base:readonly。

        注意：飞书返回的是 department_ids（数字），要拿名字得再查 /contact/v3/departments/{id}。
        这里先返回 ids，映射到 groups 由上层配置决定（部门 id → 密级组）。
        """
        d = self._get(f"/open-apis/contact/v3/users/{open_id}?user_id_type=open_id")
        if d.get("code") != 0:
            raise RuntimeError(f"查用户失败 code={d.get('code')} msg={d.get('msg')}")
        return [str(x) for x in d.get("data", {}).get("user", {}).get("department_ids", [])]

    def reply_text(self, message_id: str, text: str) -> dict:
        """在原消息下回复（比主动发消息更自然，也会带上引用关系）。"""
        return self._post(
            f"/open-apis/im/v1/messages/{message_id}/reply",
            {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)})


# ── 核心处理 ────────────────────────────────────────────────────────────────

CLIENT = FeishuClient() if not MOCK else None
LOCAL_USERS = load_local_users()
TASKS: "queue.Queue[dict]" = queue.Queue()


def resolve_identity(open_id: str) -> tuple[str, list[str], str]:
    """open_id → (显示名, 密级组列表, 错误说明)。

    绝不从消息体里读部门：员工能改的字段一律不参与权限判断。
    """
    if open_id in load_revoked():
        return "", [], "账号已离职，权限已吊销"
    if MOCK:
        u = LOCAL_USERS.get(open_id)
        if not u:
            return "", [], "不在员工名册（mock）"
        return u.get("name", open_id), list(u.get("groups", [])), ""
    try:
        deps = CLIENT.user_departments(open_id)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001
        return "", [], f"查通讯录失败: {e}"
    if not deps:
        return "", [], "该用户没有部门归属"
    # 部门 id → 密级组 的映射表（接真环境时按公司组织架构填）
    dep_map = _load_dep_map()
    groups: list[str] = []
    for dep in deps:
        groups.extend(dep_map.get(dep, []))
    if not groups:
        return open_id[:10], [], f"部门 {deps} 未映射到任何密级"
    return open_id[:10], sorted(set(groups)), ""


def _load_dep_map() -> dict[str, list[str]]:
    f = ROOT / ".feishu_depmap.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def call_gateway(groups: list[str], question: str) -> tuple[int, dict]:
    """组 → 令牌。真实环境令牌应由 SSO 签发，这里用组名换本地令牌（最小改动）。"""
    tokens = {"compliance": "tk-zhang-7f3a9c2e", "board": "tk-wang-4b8d1f6a",
              "hr": "tk-li-9e2c5d7b"}
    token = next((tokens[g] for g in groups if g in tokens), "tk-guest-1a3f8e4c")
    body = json.dumps({"question": question}).encode()
    req = urllib.request.Request(GATEWAY, data=body,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {"error": "上游返回非 JSON"}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"网关不可达: {e}"}


def format_answer(name: str, data: dict) -> str:
    ans = (data.get("answer") or "").strip()
    tier = data.get("routed_tier", "")
    srcs = data.get("sources") or []
    lines = [f"{name}，我在「{tier}」里查到：", "", ans]
    if srcs:
        lines += ["", "来源：" + "、".join(
            f"{s['doc']}({s['score']})" for s in srcs[:3])]
    return "\n".join(lines)


def deliver(open_id: str, message_id: str, text: str) -> None:
    """mock 模式写日志；real 模式回消息。"""
    if MOCK:
        with REPLY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "open_id": open_id,
                                "message_id": message_id, "text": text},
                               ensure_ascii=False) + "\n")
        return
    try:
        r = CLIENT.reply_text(message_id, text)  # type: ignore[union-attr]
        if r.get("code") != 0:
            print(f"[warn] 回复失败 code={r.get('code')} msg={r.get('msg')}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 回复异常: {e}")


def worker() -> None:
    while True:
        task = TASKS.get()
        try:
            handle_task(task)
        except Exception as e:  # noqa: BLE001
            print(f"[error] 处理异常 event={task.get('event_id')}: {e}")
        finally:
            TASKS.task_done()


def handle_task(task: dict) -> None:
    event_id = task["event_id"]
    open_id = task["open_id"]
    message_id = task["message_id"]
    question = task.get("text", "").strip()
    t0 = time.time()

    name, groups, why = resolve_identity(open_id)
    if why:
        deliver(open_id, message_id, f"无法回答：{why}。请联系管理员开通权限。")
        log_reply(event_id, open_id, name or open_id[:10], "", True,
                  int((time.time() - t0) * 1000), 0, why)
        return

    if not question:
        deliver(open_id, message_id, "我只处理文字问题，图片和文件还在做。")
        log_reply(event_id, open_id, name, "", True, int((time.time() - t0) * 1000), 0, "empty")
        return

    status, data = call_gateway(groups, question)
    latency = int((time.time() - t0) * 1000)

    if status == 401:
        text = "身份校验失败（令牌无效）。"
    elif status == 403:
        text = f"{name}，你没有可访问的知识库。{data.get('error', '')}"
    elif status != 200:
        text = f"查询失败（{status}）：{data.get('error', '未知错误')[:120]}"
    else:
        text = format_answer(name, data)

    deliver(open_id, message_id, text)
    log_reply(event_id, open_id, name, data.get("routed_tier", ""),
              status != 200, latency, len(text), "" if status == 200 else str(status))


# ── 事件解析 ────────────────────────────────────────────────────────────────

def parse_event(body: dict) -> dict | None:
    """飞书 2.0 事件 → 统一任务。不是消息事件就返回 None。"""
    header = body.get("header") or {}
    event = body.get("event") or {}
    etype = header.get("event_type") or body.get("type") or ""

    if etype == "im.message.receive_v1":
        msg = event.get("message") or {}
        sender = (event.get("sender") or {}).get("sender_id") or {}
        open_id = sender.get("open_id", "")
        message_id = msg.get("message_id", "")
        try:
            content = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            content = {}
        text = content.get("text", "")
        # 群聊里去掉 @机器人 的前缀
        for m in msg.get("mentions") or []:
            key = m.get("key", "")
            if key:
                text = text.replace(key, "")
        return {"kind": "message", "event_id": header.get("event_id", ""),
                "open_id": open_id, "message_id": message_id,
                "text": text.strip(), "chat_type": msg.get("chat_type", "")}

    if etype == "contact.user.deleted_v3":
        u = event.get("object") or event.get("user") or {}
        return {"kind": "user_deleted", "event_id": header.get("event_id", ""),
                "open_id": u.get("open_id", ""), "message_id": "", "text": ""}

    return None


# ── HTTP 层 ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "mode": "mock" if MOCK else "real",
                             "queued": TASKS.qsize()})
        elif self.path == "/stats":
            with sqlite3.connect(DB, timeout=10) as c:
                n_seen = c.execute("SELECT count(*) FROM seen_events").fetchone()[0]
                rows = c.execute("""SELECT ts,user_name,tier,blocked,latency_ms
                                    FROM replies ORDER BY id DESC LIMIT 20""").fetchall()
            self._send(200, {"events_seen": n_seen, "recent": [
                {"ts": r[0], "user": r[1], "tier": r[2], "blocked": bool(r[3]),
                 "ms": r[4]} for r in rows]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/feishu/event":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return

        # 1. URL 校验握手：必须原样回 challenge
        if body.get("type") == "url_verification":
            if VERIFY_TOKEN and body.get("token") != VERIFY_TOKEN:
                self._send(403, {"error": "bad verification token"})
                return
            self._send(200, {"challenge": body.get("challenge", "")})
            return

        # 2. 令牌校验（事件 v2 的 header.token 或旧版 body.token）
        token = (body.get("header") or {}).get("token") or body.get("token") or ""
        if VERIFY_TOKEN and token != VERIFY_TOKEN:
            self._send(403, {"error": "bad token"})
            return

        task = parse_event(body)
        if not task:
            self._send(200, {"code": 0, "note": "ignored"})
            return

        # 3. 幂等：同 event_id 只处理一次（飞书重试会重发）
        eid = task["event_id"] or hashlib.sha256(raw).hexdigest()[:16]
        if not claim_event(eid, task["open_id"], task["kind"]):
            self._send(200, {"code": 0, "note": "duplicate"})
            return

        # 4. 离职事件同步处理（不碰模型，很快）
        if task["kind"] == "user_deleted":
            if task["open_id"]:
                revoke(task["open_id"])
            self._send(200, {"code": 0, "note": "revoked"})
            return

        # 5. 消息事件：立刻 200，异步处理
        task["event_id"] = eid
        TASKS.put(task)
        self._send(200, {"code": 0})


def serve(port: int) -> None:
    db_init()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"飞书接入层启动: http://127.0.0.1:{port}/feishu/event")
    print(f"模式: {'mock（无需凭据）' if MOCK else 'real（连飞书开放平台）'}")
    print(f"健康检查: http://127.0.0.1:{port}/health")
    print("员工名册:", {k: v['groups'] for k, v in LOCAL_USERS.items()})
    srv.serve_forever()


# ── 自测：把四件必须成立的事真跑一遍 ─────────────────────────────────────────

def selftest() -> int:
    import subprocess
    import tempfile

    global DB, REPLY_LOG, USERS_FILE, REVOKED_FILE, VERIFY_TOKEN
    VERIFY_TOKEN = "selftest-token"      # 自测里必须真的走 token 校验分支
    tmp = Path(tempfile.mkdtemp(prefix="feishu_selftest_"))
    DB, REPLY_LOG = tmp / "bridge.db", tmp / "replies.jsonl"
    USERS_FILE, REVOKED_FILE = tmp / "users.json", tmp / "revoked.json"
    db_init()

    port = 18091
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}/feishu/event"
    passed, failed = [], []

    def post(payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            base, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(name)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

    def msg_event(eid: str, open_id: str, text: str, mid: str = "om_1") -> dict:
        return {"schema": "2.0",
                "header": {"event_id": eid, "event_type": "im.message.receive_v1",
                           "token": VERIFY_TOKEN},
                "event": {"sender": {"sender_id": {"open_id": open_id}},
                          "message": {"message_id": mid, "chat_type": "p2p",
                                      "message_type": "text",
                                      "content": json.dumps({"text": text},
                                                            ensure_ascii=False)}}}

    print("\n== 1. URL 校验握手 ==")
    code, resp = post({"type": "url_verification", "challenge": "abc123", "token": VERIFY_TOKEN})
    check("challenge 原样回显", code == 200 and resp.get("challenge") == "abc123", str(resp))

    print("\n== 2. 坏令牌被拒 ==")
    bad = {"schema": "2.0", "header": {"event_id": "ev_bad", "token": "wrong-token",
                                       "event_type": "im.message.receive_v1"},
           "event": {}}
    code, resp = post(bad)
    check("错误 token 返回 403", code == 403, f"HTTP {code}")

    def read_replies() -> list[dict]:
        if not REPLY_LOG.exists():
            return []
        return [json.loads(l) for l in REPLY_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]

    def wait_replies(n: int, timeout: int = 120) -> bool:
        """等异步 worker 真正落盘 n 条回复。

        不能轮询队列长度：worker 一 get() 队列就变 0，但模型还在跑 12 秒。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(read_replies()) >= n:
                return True
            time.sleep(0.5)
        return False

    print("\n== 3. 回调立即返回（不等模型）==")
    t0 = time.time()
    code, resp = post(msg_event("ev_compliance_1", "ou_mock_zhang",
                                "关联交易审批存在什么问题"))
    ack_ms = (time.time() - t0) * 1000
    check("200 且 1 秒内返回", code == 200 and ack_ms < 1000, f"{ack_ms:.0f} ms")

    print("\n== 4. 异步处理完成并产出回复 ==")
    check("异步写成 1 条回复（≤120s）", wait_replies(1))
    replies = read_replies()
    check("确实只有 1 条", len(replies) == 1, f"{len(replies)} 条")
    if replies:
        txt = replies[0]["text"]
        check("回复里带密级库名", "合规知识库" in txt, txt.replace("\n", " ")[:80])
        check("回复里有实质答案", "14A.35" in txt or "审计" in txt, txt.replace("\n", " ")[:80])

    print("\n== 5. 重复 event_id 被丢弃（飞书重试场景）==")
    before = len(read_replies())
    code, resp = post(msg_event("ev_compliance_1", "ou_mock_zhang",
                               "关联交易审批存在什么问题"))
    time.sleep(2.0)
    after = len(read_replies())
    check("重发返回 duplicate", resp.get("note") == "duplicate", str(resp))
    check("没有产生第二条回复", after == before, f"{before} → {after}")

    print("\n== 6. 无权限用户被挡 ==")
    post(msg_event("ev_guest_1", "ou_mock_guest", "关联交易审批存在什么问题"))
    wait_replies(before + 1, 30)
    replies3 = read_replies()
    last = replies3[-1]["text"] if replies3 else ""
    check("访客收到无权限提示", "没有可访问的知识库" in last, last.replace("\n", " ")[:80])

    print("\n== 7. 离职事件吊销权限 ==")
    post({"schema": "2.0",
          "header": {"event_id": "ev_del_1", "token": VERIFY_TOKEN,
                     "event_type": "contact.user.deleted_v3"},
          "event": {"object": {"open_id": "ou_mock_li"}}})
    post(msg_event("ev_hr_1", "ou_mock_li", "陈大文的月薪是多少"))
    wait_replies(len(replies3) + 1, 30)
    replies4 = read_replies()
    last = replies4[-1]["text"] if replies4 else ""
    check("离职后拒绝回答", "离职" in last, last.replace("\n", " ")[:80])

    srv.shutdown()
    print("\n" + "=" * 60)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项:", failed)
    return 0 if not failed else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--load", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.load:
        print("模式:", "mock" if MOCK else "real")
        print("员工名册:", json.dumps(LOCAL_USERS, ensure_ascii=False, indent=2))
        print("已吊销:", json.dumps(sorted(load_revoked()), ensure_ascii=False))
        print("凭据:", "完整" if not MOCK else "缺失（MOCK 模式）")
        return
    serve(a.port)


if __name__ == "__main__":
    main()
