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
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import dify_ingest as ingest          # 写入/撤销 Dify 知识库
except ImportError:                       # 单独跑时允许缺失，图片入库会报错
    ingest = None

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
UPLOADS = OUT / "uploads"
UPLOADS.mkdir(exist_ok=True)
DB = OUT / "feishu_bridge.db"
USERS_FILE = OUT / "feishu_users.json"
REPLY_LOG = OUT / "feishu_replies.jsonl"

GATEWAY = "http://127.0.0.1:8090/ask"
FEISHU_BASE = "https://open.feishu.cn"


def _load_dotenv() -> None:
    """读项目根目录 .env（已在 .gitignore 里）。

    凭据只放这里或系统环境变量，不要写进代码或提交进仓库。
    """
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

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
    """读 open_id 名册。以 `_` 开头的键当注释忽略（防止有人写 "_说明" 进来）。"""
    if USERS_FILE.exists():
        try:
            raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            return {k: v for k, v in raw.items()
                    if isinstance(v, dict) and not k.startswith("_")}
        except (json.JSONDecodeError, AttributeError):
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
        # 待确认的图片上传：token 让「确认/取消/撤销」能对上具体内容
        c.execute("""CREATE TABLE IF NOT EXISTS pending (
            token TEXT PRIMARY KEY, ts REAL, open_id TEXT, user_name TEXT,
            tier TEXT, doc_name TEXT, text TEXT, confidence REAL,
            doc_id TEXT, status TEXT)""")


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

    def download_resource(self, message_id: str, file_key: str,
                          rtype: str = "image") -> bytes:
        """下载消息里的图片/文件（需要 im:resource 权限）。

        rtype: image | file
        """
        req = urllib.request.Request(
            f"{FEISHU_BASE}/open-apis/im/v1/messages/{message_id}/resources/"
            f"{file_key}?type={rtype}",
            headers={"Authorization": f"Bearer {self.tenant_token()}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()


# ── 核心处理 ────────────────────────────────────────────────────────────────

CLIENT = FeishuClient() if not MOCK else None
LOCAL_USERS = load_local_users()
_USERS_CACHE: dict = {"mtime": 0.0, "data": LOCAL_USERS}


def local_users() -> dict:
    """open_id → {name, groups} 的本地映射，改文件即时生效（按 mtime 判断）。

    小公司常见情况：所有人挂在根部门 '0'，部门映射永远查不到东西。
    这时按 open_id 精确指定密级是唯一可行的办法。
    """
    try:
        m = USERS_FILE.stat().st_mtime
    except OSError:
        return LOCAL_USERS
    if m != _USERS_CACHE["mtime"]:
        _USERS_CACHE["data"] = load_local_users()
        _USERS_CACHE["mtime"] = m
    return _USERS_CACHE["data"]


TASKS: "queue.Queue[dict]" = queue.Queue()


def resolve_identity(open_id: str) -> tuple[str, list[str], str]:
    """open_id → (显示名, 密级组列表, 错误说明)。

    绝不从消息体里读部门：员工能改的字段一律不参与权限判断。

    三段优先级：
      1. open_id 精确映射（out/feishu_users.json）—— 覆盖一切，也用于扁平组织
      2. 部门映射（out/feishu_depmap.json）—— 组织架构规范时的正路
      3. 都没有 → 拒绝并说清原因（不猜、不给默认权限）
    """
    if open_id in load_revoked():
        return "", [], "账号已离职，权限已吊销"

    # 1. open_id 精确映射
    u = local_users().get(open_id)
    if u:
        return u.get("name", open_id[:10]), list(u.get("groups", [])), ""

    if MOCK:
        return "", [], "不在员工名册（mock）"

    # 2. 部门映射
    try:
        deps = CLIENT.user_departments(open_id)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001
        return "", [], f"查通讯录失败: {e}"
    if not deps:
        return "", [], "该用户没有部门归属"
    dep_map = _load_dep_map()
    groups: list[str] = []
    for dep in deps:
        groups.extend(dep_map.get(dep, []))
    if not groups:
        # 根部门 '0' 是典型情况：全员挂在根上，按部门无法区分密级
        hint = "（全员都在根部门，请改用 open_id 映射）" if deps == ["0"] else ""
        return open_id[:10], [], f"部门 {deps} 未映射到任何密级{hint}"
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


CONFIRM_WORDS = {"确认", "确定", "是", "入库", "ok", "OK", "好"}
CANCEL_WORDS = {"取消", "不要", "放弃", "算了"}
REVOKE_WORDS = {"撤销", "撤回", "删除"}
CONF_AUTO = 0.95      # 高于这个且校验通过 → 直接入库（可撤销）
CONF_MIN = 0.70       # 低于这个 → 让人重拍
UNDO_WINDOW = 300     # 秒：确认/撤销的有效期

WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
CHECK = "10X98765432"
ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_OCR_ENGINE = None


def ocr_engine():
    """RapidOCR 引擎单例（每次 new 要 1 秒，问一次图就 new 一次太浪费）。

    注意：必须用项目 .venv 的 python 跑，系统 python 没装 rapidocr
    （实测踩过：用错解释器时这里才报错，前面所有文字问答都是好的，很难发现）。
    """
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise RuntimeError(
                "OCR 引擎不可用：缺 rapidocr_onnxruntime。"
                "请用 .venv/Scripts/python.exe 运行本脚本，"
                "或先 uv pip install -r requirements.txt") from e
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def ocr_image(path: Path) -> tuple[str, float, int]:
    """本地 OCR。返回 (全文, 平均置信度, 行数)。全程不出网。"""
    result, _ = ocr_engine()(str(path))
    if not result:
        return "", 0.0, 0
    lines = [t for _, t, _ in result]
    confs = [float(s) for _, _, s in result]
    return "\n".join(lines), sum(confs) / len(confs), len(result)


def id_check(num: str) -> bool:
    """GB 11643 校验位。号码长得像不等于号码对。"""
    num = num.upper()
    if len(num) != 18 or not num[:17].isdigit():
        return False
    return CHECK[sum(int(c) * w for c, w in zip(num[:17], WEIGHTS)) % 11] == num[17]


def extract_fields(text: str) -> dict:
    """从 OCR 文本里抽结构化字段。

    有身份证号时按号码反推出生日期/性别（号码是权威源，图上印的字反而容易被 OCR 认错），
    再用校验位判断这一整条记录可不可信。
    """
    flat = re.sub(r"\s", "", text)
    info: dict = {"kind": "通用文本", "valid": True, "note": ""}
    m = ID_RE.search(flat)
    if not m:
        return info
    num = m.group(1).upper()
    ok = id_check(num)
    info.update({
        "kind": "身份证",
        "id_number": num,
        "valid": ok,
        "note": "校验位通过" if ok else "校验位不通过（号码可能被识别错，或本身是假号）",
        "birth": f"{num[6:10]}-{num[10:12]}-{num[12:14]}",
        "gender": "男" if int(num[16]) % 2 else "女",
    })
    return info


def tiers_for_groups(groups: list[str]) -> list[str]:
    """组 → 可写的密级库（按 .dify-tiered.json 的 groups 反向查）。"""
    cfg = json.loads((ROOT / ".dify-tiered.json").read_text(encoding="utf-8"))
    gs = set(groups)
    return [t for t, v in cfg.items() if gs & set(v.get("groups", []))]


def pending_save(token: str, open_id: str, user_name: str, tier: str,
                 doc_name: str, text: str, confidence: float, status: str) -> None:
    with sqlite3.connect(DB, timeout=10) as c:
        c.execute("""INSERT OR REPLACE INTO pending
                     (token,ts,open_id,user_name,tier,doc_name,text,confidence,doc_id,status)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (token, time.time(), open_id, user_name, tier, doc_name,
                   text, confidence, "", status))


def pending_latest(open_id: str, statuses: tuple[str, ...]) -> dict | None:
    q = ",".join("?" * len(statuses))
    with sqlite3.connect(DB, timeout=10) as c:
        c.row_factory = sqlite3.Row
        r = c.execute(f"SELECT * FROM pending WHERE open_id=? AND status IN ({q}) "
                      f"ORDER BY ts DESC LIMIT 1", (open_id, *statuses)).fetchone()
    return dict(r) if r else None


def pending_update(token: str, status: str, doc_id: str = "") -> None:
    with sqlite3.connect(DB, timeout=10) as c:
        c.execute("UPDATE pending SET status=?, doc_id=? WHERE token=?", (status, doc_id, token))


def mock_image(message_id: str) -> Path:
    """mock 模式下没有真图可下载，用一张程序画的身份证顶替，好把链路跑通。"""
    sample = OUT / "id_card_sample.png"
    if sample.exists():
        return sample
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1000, 631
    img = Image.new("RGB", (W, H), (238, 240, 245))
    d = ImageDraw.Draw(img)
    fp = next((f for f in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]
               if Path(f).exists()), None)
    f1 = ImageFont.truetype(fp, 42) if fp else ImageFont.load_default()
    f2 = ImageFont.truetype(fp, 32) if fp else ImageFont.load_default()
    d.rectangle([20, 20, W - 20, H - 20], outline=(180, 185, 200), width=3)
    d.text((60, 50), "中华人民共和国", font=f1, fill=(30, 30, 40))
    d.text((60, 100), "居民身份证", font=f2, fill=(30, 30, 40))
    y = 200
    for label, val in [("姓    名", "林嘉欣"), ("性    别", "男        民 族  汉"),
                       ("出    生", "1990 年 3 月 7 日"),
                       ("住    址", "广东省深圳市南山区科技园路 88 号 3 栋 1201"),
                       ("公民身份号码", "44030419900307123X")]:
        d.text((60, y), label, font=f2, fill=(60, 60, 70))
        d.text((260, y), val, font=f2, fill=(20, 20, 25))
        y += 70
    img.save(sample, quality=95)
    return sample


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


def reply_and_log(open_id: str, message_id: str, name: str, event_id: str,
                  t0: float, text: str, tier: str = "", blocked: bool = False) -> None:
    deliver(open_id, message_id, text)
    log_reply(event_id, open_id, name, tier, blocked,
              int((time.time() - t0) * 1000), len(text), text[:60] if blocked else "")


def call_llm(question: str, context: str = "") -> tuple[bool, str]:
    """直接调本地模型（不走知识库）。

    用途：员工发了一张图，接着问「这上面金额多少」——
    这类问题要答的是**那张图里的字**，不是知识库，所以不能走 RAG。
    """
    prompt = (f"下面是一张图片的文字识别结果（可能有识别错误）。请只依据这些文字回答；"
              f"文字里没有的信息就说「图片里没看到」，不要猜、不要补充常识。"
              f"如果对方是在问图片里有什么、让复述内容，就把这些文字整理成简洁条目给他。\n\n"
              f"【图片文字】\n{context}\n\n【问题】{question}") if context else question
    body = json.dumps({"model": "qwen2.5:7b", "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.1, "num_predict": 400}}).encode()
    req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return True, json.loads(r.read().decode()).get("response", "").strip()
    except Exception as e:  # noqa: BLE001
        return False, f"本地模型不可用: {e}"


def handle_image(task: dict, name: str, groups: list[str], t0: float) -> None:
    """图片 → 本地 OCR → 回识别结果，**默认不写库**。

    发照片不等于要入库：可能是想「这合同有什么问题」，也可能是私人证件不该进公司库。
    所以这里只做识别并把选择权交回人手上：回「入库」才写，直接问问题就按图回答。
    """
    event_id, open_id, message_id = task["event_id"], task["open_id"], task["message_id"]
    image_key = (task.get("content") or {}).get("image_key", "")
    if not image_key:
        reply_and_log(open_id, message_id, name, event_id, t0,
                      "这张图没取到内容（缺 image_key），重发一次试试。", blocked=True)
        return

    tiers = tiers_for_groups(groups)
    if not tiers:
        reply_and_log(open_id, message_id, name, event_id, t0,
                      f"{name}，你没有可写入的知识库，只能识别不能入库。", blocked=True)
        return
    tier = tiers[0]

    # 1. 下载
    try:
        if MOCK:
            path = mock_image(message_id)
        else:
            path = UPLOADS / f"{message_id}.png"
            path.write_bytes(CLIENT.download_resource(message_id, image_key, "image"))
    except Exception as e:  # noqa: BLE001
        reply_and_log(open_id, message_id, name, event_id, t0,
                      f"下载图片失败：{e}", blocked=True)
        return

    # 2. 本地 OCR（不出网）
    try:
        text, conf, n = ocr_image(path)
    except Exception as e:  # noqa: BLE001
        reply_and_log(open_id, message_id, name, event_id, t0,
                      f"OCR 没跑起来：{e}", blocked=True)
        return
    if not text.strip() or conf < CONF_MIN:
        reply_and_log(open_id, message_id, name, event_id, t0,
                      f"这张没认清（置信度 {conf:.2f}，识别到 {n} 段文字）。\n"
                      f"请靠近一点、对准、避开反光和手指遮挡，重拍一张。", blocked=True)
        return

    fields = extract_fields(text)
    doc_name = f"飞书图片-{name.replace('（', '-').replace('）', '')}-" \
               f"{time.strftime('%Y%m%d-%H%M%S')}.txt"
    token = hashlib.sha256(f"{message_id}{time.time()}".encode()).hexdigest()[:16]

    head = [f"识别结果（{fields['kind']}，置信度 {conf:.2f}，{n} 段文字）"]
    if fields["kind"] == "身份证":
        head += [f"号码：{fields['id_number']}",
                 f"校验：{fields['note']}",
                 f"出生：{fields['birth']}　性别：{fields['gender']}"]
    head += ["", "原文：", text[:280] + ("…" if len(text) > 280 else "")]

    # 3. 存为「仅识别」状态：等用户决定是入库还是提问
    pending_save(token, open_id, name, tier, doc_name, text, conf, "ocr_only")

    body = ("\n".join(head) +
            f"\n\n要怎么处理？\n"
            f"· 回「入库」→ 写进「{tier}」\n"
            f"· 直接问 → 我按这张图的内容回答（例如「上面的金额是多少」）\n"
            f"（5 分钟内有效）")
    reply_and_log(open_id, message_id, name, event_id, t0, body, tier)


def handle_confirm(open_id: str, message_id: str, name: str, event_id: str, t0: float) -> None:
    p = pending_latest(open_id, ("ocr_only", "awaiting", "ingested", "revoked"))
    if not p or time.time() - p["ts"] > UNDO_WINDOW:
        reply_and_log(open_id, message_id, name, event_id, t0,
                      "没有待入库的内容（5 分钟内有效）。请先发一张图片。", blocked=True)
        return
    if p["status"] == "ingested":
        reply_and_log(open_id, message_id, name, event_id, t0,
                      f"这张图已经在「{p['tier']}」里了（{p['doc_name']}）。\n"
                      f"重复写入只会让库里多一条重复内容，所以没有再加。\n"
                      f"要存新的一张请重新发图。", blocked=True)
        return
    if p["status"] == "revoked":
        reply_and_log(open_id, message_id, name, event_id, t0,
                      "这张图刚才被撤销了。要重新入库请再发一次图片。", blocked=True)
        return
    if ingest is None:
        reply_and_log(open_id, message_id, name, event_id, t0,
                      "写入模块没加载（dify_ingest.py），暂时不能入库。", blocked=True)
        return
    payload = f"[来源：飞书图片上传 by {name}]\n{p['text']}"
    ok, doc_id, msg = ingest.add_text(p["tier"], p["doc_name"], payload)
    if ok:
        pending_update(p["token"], "ingested", doc_id)
        reply_and_log(open_id, message_id, name, event_id, t0,
                      f"✅ 已写入「{p['tier']}」。5 分钟内回「撤销」可删除。", p["tier"])
    else:
        reply_and_log(open_id, message_id, name, event_id, t0, f"⚠ 写入失败：{msg}", blocked=True)


def handle_cancel(open_id: str, message_id: str, name: str, event_id: str, t0: float) -> None:
    p = pending_latest(open_id, ("awaiting",))
    if not p:
        reply_and_log(open_id, message_id, name, event_id, t0,
                      "没有待确认的内容。", blocked=True)
        return
    pending_update(p["token"], "cancelled")
    reply_and_log(open_id, message_id, name, event_id, t0, "已放弃，没有写入任何内容。")


def handle_revoke(open_id: str, message_id: str, name: str, event_id: str, t0: float) -> None:
    p = pending_latest(open_id, ("ingested",))
    if not p or time.time() - p["ts"] > UNDO_WINDOW:
        reply_and_log(open_id, message_id, name, event_id, t0,
                      "没有可撤销的记录（撤销窗口 5 分钟）。", blocked=True)
        return
    ok, msg = ingest.delete_document(p["tier"], p["doc_id"])
    if ok:
        pending_update(p["token"], "revoked")
        reply_and_log(open_id, message_id, name, event_id, t0,
                      f"已从「{p['tier']}」删除该文档。", p["tier"])
    else:
        reply_and_log(open_id, message_id, name, event_id, t0, f"⚠ 撤销失败：{msg}", blocked=True)


def handle_task(task: dict) -> None:
    event_id = task["event_id"]
    open_id = task["open_id"]
    message_id = task["message_id"]
    text = task.get("text", "").strip()
    msg_type = task.get("msg_type", "text")
    t0 = time.time()

    name, groups, why = resolve_identity(open_id)
    if why:
        deliver(open_id, message_id, f"无法回答：{why}。请联系管理员开通权限。")
        log_reply(event_id, open_id, name or open_id[:10], "", True,
                  int((time.time() - t0) * 1000), 0, why)
        return

    if msg_type == "image":
        handle_image(task, name, groups, t0)
        return

    if text in CONFIRM_WORDS:
        handle_confirm(open_id, message_id, name, event_id, t0)
        return
    if text in CANCEL_WORDS:
        handle_cancel(open_id, message_id, name, event_id, t0)
        return
    if text in REVOKE_WORDS:
        handle_revoke(open_id, message_id, name, event_id, t0)
        return

    if not text:
        reply_and_log(open_id, message_id, name, event_id, t0,
                      "直接问问题，或者发一张照片让我识别入库。", blocked=True)
        return

    # 刚发过图、接着问了句话 → 按那张图回答（不走知识库）
    # 注意：这里的图片上下文与「是否入库」无关 —— 入库、撤销之后那张图仍然可问，
    # 所以不能只查 ocr_only（实测踩过：撤销后问「刚刚识别的图片」变成去查知识库）
    img = pending_latest(open_id, ("ocr_only", "awaiting", "ingested", "revoked"))
    if img and time.time() - img["ts"] <= UNDO_WINDOW:
        ok, ans = call_llm(text, img["text"])
        if ok:
            reply_and_log(open_id, message_id, name, event_id, t0,
                          f"{name}，按你刚发的那张图：\n\n{ans}\n\n"
                          f"（要把它存进「{img['tier']}」就回「入库」）", img["tier"])
        else:
            reply_and_log(open_id, message_id, name, event_id, t0, ans, blocked=True)
        return

    status, data = call_gateway(groups, text)
    latency = int((time.time() - t0) * 1000)

    if status == 401:
        reply = "身份校验失败（令牌无效）。"
    elif status == 403:
        reply = f"{name}，你没有可访问的知识库。{data.get('error', '')}"
    elif status != 200:
        reply = f"查询失败（{status}）：{data.get('error', '未知错误')[:120]}"
    else:
        reply = format_answer(name, data)

    deliver(open_id, message_id, reply)
    log_reply(event_id, open_id, name, data.get("routed_tier", ""),
              status != 200, latency, len(reply), "" if status == 200 else str(status))


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
                "text": text.strip(), "chat_type": msg.get("chat_type", ""),
                "msg_type": msg.get("message_type", "text"), "content": content}

    if etype == "contact.user.deleted_v3":
        u = event.get("object") or event.get("user") or {}
        return {"kind": "user_deleted", "event_id": header.get("event_id", ""),
                "open_id": u.get("open_id", ""), "message_id": "", "text": ""}

    return None


def enqueue(task: dict) -> tuple[bool, str]:
    """HTTP 回调和长连接两个入口共用：幂等抢占 → 入队。返回 (是否新事件, 说明)。"""
    eid = task.get("event_id") or hashlib.sha256(
        (task.get("open_id", "") + task.get("text", "") + str(time.time_ns())).encode()
    ).hexdigest()[:16]
    if not claim_event(eid, task.get("open_id", ""), task.get("kind", "message")):
        return False, "duplicate"
    task["event_id"] = eid
    if task.get("kind") == "user_deleted":
        if task.get("open_id"):
            revoke(task["open_id"])
        return True, "revoked"
    TASKS.put(task)
    return True, "queued"


# ── 长连接模式（真环境推荐：无需公网 IP / 内网穿透）─────────────────────────

def run_ws() -> int:
    """飞书 WebSocket 长连接：本地主动连飞书，事件从这条通道推过来。

    比 HTTP 回调更适合内网部署：
      · 无需公网 IP、无需域名、无需内网穿透（只要本机能出公网）
      · SDK 建连时鉴权，后续事件已是明文，不用自己验签解密
      · 限制：仅企业自建应用；仍需 3 秒内返回（超时会重推）；多实例只有一个收到
    """
    if MOCK:
        print("长连接模式需要真实凭据：请设置 FEISHU_APP_ID / FEISHU_APP_SECRET")
        return 1
    try:
        import lark_oapi as lark
    except ImportError:
        print("缺依赖：uv pip install --python .venv/Scripts/python.exe lark-oapi")
        return 1

    db_init()
    threading.Thread(target=worker, daemon=True).start()

    def on_msg(data) -> None:
        try:
            e, h = data.event, data.header
            msg = e.message
            sid = getattr(e.sender, "sender_id", None)
            open_id = (getattr(sid, "open_id", "") or "") if sid else ""
            try:
                content = json.loads(msg.content or "{}")
            except json.JSONDecodeError:
                content = {}
            text = content.get("text", "")
            for m in (msg.mentions or []):
                key = getattr(m, "key", "") or ""
                if key:
                    text = text.replace(key, "")
            ok, why = enqueue({"kind": "message", "event_id": h.event_id or "",
                               "open_id": open_id, "message_id": msg.message_id or "",
                               "text": text.strip(),
                               "msg_type": getattr(msg, "message_type", "text") or "text",
                               "content": content})
            print(f"[ws] 消息 open_id={open_id[:12]}… type={getattr(msg, 'message_type', '?')} "
                  f"{len(text)} 字 → {why}")
        except Exception as ex:  # noqa: BLE001
            print(f"[warn] 长连接消息解析失败: {ex}")

    def on_user_deleted(data) -> None:
        try:
            oid = getattr(data.event.object, "open_id", "") or ""
            ok, why = enqueue({"kind": "user_deleted", "event_id": data.header.event_id or "",
                               "open_id": oid, "message_id": "", "text": ""})
            print(f"[ws] 离职事件 open_id={oid[:12]}… → {why}")
        except Exception as ex:  # noqa: BLE001
            print(f"[warn] 离职事件解析失败: {ex}")

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_msg)
               .register_p2_contact_user_deleted_v3(on_user_deleted)
               .build())

    print("飞书长连接启动中…（Ctrl+C 退出）")
    print("提示：飞书后台「事件与回调 → 订阅方式」此时才能选『使用长连接接收事件』并保存成功")
    try:
        lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler,
                       log_level=lark.LogLevel.INFO).start()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


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

        # 3. 幂等 + 入队（与长连接模式共用同一条路径）
        _, why = enqueue(task)
        self._send(200, {"code": 0, "note": why})


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

    global DB, REPLY_LOG, USERS_FILE, REVOKED_FILE, VERIFY_TOKEN, MOCK, CLIENT, ingest, call_llm
    # 自测一律走 mock：.env 里一旦填了真实凭据，不强制就会去真调飞书 API（实测踩过）
    MOCK = True
    CLIENT = None
    # 自测不真调模型：7B 单卡一次 5-15 秒，两项就是半分钟起，会把自测拖到超时（实测 420s 被打断）
    call_llm = lambda q, ctx="": (True, f"[假模型] 依据图片文字回答：{q}")
    VERIFY_TOKEN = "selftest-token"      # 自测里必须真的走 token 校验分支

    class _FakeIngest:
        """假的知识库写入：自测绝不能真写 Dify，否则污染真实库。"""

        def __init__(self) -> None:
            self.docs: dict[str, dict] = {}

        def add_text(self, tier: str, name: str, text: str):
            doc_id = f"fake-{len(self.docs) + 1:03d}"
            self.docs[doc_id] = {"tier": tier, "name": name, "text": text}
            return True, doc_id, f"已写入「{tier}」文档 {name}"

        def delete_document(self, tier: str, doc_id: str):
            if doc_id in self.docs:
                del self.docs[doc_id]
                return True, "已删除"
            return False, "文档不存在"

    FAKE = _FakeIngest()
    ingest = FAKE
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

    def msg_event(eid: str, open_id: str, text: str, mid: str = "om_1",
                  msg_type: str = "text", content: dict | None = None) -> dict:
        body = json.dumps({"text": text}, ensure_ascii=False) if content is None \
            else json.dumps(content, ensure_ascii=False)
        return {"schema": "2.0",
                "header": {"event_id": eid, "event_type": "im.message.receive_v1",
                           "token": VERIFY_TOKEN},
                "event": {"sender": {"sender_id": {"open_id": open_id}},
                          "message": {"message_id": mid, "chat_type": "p2p",
                                      "message_type": msg_type,
                                      "content": body}}}

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

    print("\n== 8. 发图 → 识别结果，但**不自动入库** ==")
    base_n = len(read_replies())
    post(msg_event("ev_img_1", "ou_mock_zhang", "", mid="om_img_1",
                   msg_type="image", content={"image_key": "img_fake_1"}))
    wait_replies(base_n + 1, 200)
    r8 = read_replies()[-1]["text"] if len(read_replies()) > base_n else ""
    check("识别出身份证号", "44030419900307123X" in r8, r8.replace("\n", " ")[:110])
    check("校验位通过", "校验位通过" in r8, r8.replace("\n", " ")[:110])
    check("问了要怎么处理", "要怎么处理" in r8, r8.replace("\n", " ")[-90:])
    check("没有自动写库", len(FAKE.docs) == 0, f"{len(FAKE.docs)} 条")

    print("\n== 9. 回「入库」才真的写进去 ==")
    base_n = len(read_replies())
    post(msg_event("ev_ingest_1", "ou_mock_zhang", "入库", mid="om_ingest_1"))
    wait_replies(base_n + 1, 60)
    r9 = read_replies()[-1]["text"] if len(read_replies()) > base_n else ""
    check("写入成功", "已写入" in r9, r9.replace("\n", " ")[:110])
    check("假库里有 1 条", len(FAKE.docs) == 1, f"{len(FAKE.docs)} 条")

    print("\n== 10. 回「撤销」删掉 → 库空 ==")
    base_n = len(read_replies())
    post(msg_event("ev_revoke_1", "ou_mock_zhang", "撤销", mid="om_revoke_1"))
    wait_replies(base_n + 1, 60)
    r10 = read_replies()[-1]["text"] if len(read_replies()) > base_n else ""
    check("撤销成功", "已从" in r10 and "删除" in r10, r10.replace("\n", " ")[:110])
    check("假库空了", len(FAKE.docs) == 0, f"{len(FAKE.docs)} 条")

    print("\n== 11. 发图后直接提问 → 按图回答，不走知识库 ==")
    base_n = len(read_replies())
    post(msg_event("ev_img_3", "ou_mock_zhang", "", mid="om_img_3",
                   msg_type="image", content={"image_key": "img_fake_3"}))
    wait_replies(base_n + 1, 200)
    base_n = len(read_replies())
    post(msg_event("ev_ask_1", "ou_mock_zhang", "上面的出生日期是多少", mid="om_ask_1"))
    wait_replies(base_n + 1, 120)
    r11 = read_replies()[-1]["text"] if len(read_replies()) > base_n else ""
    check("走的是按图回答", "按你刚发的那张图" in r11, r11.replace("\n", " ")[:110])
    check("没有把图写进库", len(FAKE.docs) == 0, f"{len(FAKE.docs)} 条")

    print("\n== 12. 离职用户发图被拒（不留后门）==")
    base_n = len(read_replies())
    post(msg_event("ev_img_2", "ou_mock_li", "", mid="om_img_2",
                   msg_type="image", content={"image_key": "img_fake_2"}))
    wait_replies(base_n + 1, 60)
    r12 = read_replies()[-1]["text"] if len(read_replies()) > base_n else ""
    check("离职用户不能上传", "离职" in r12, r12.replace("\n", " ")[:110])
    check("没有写进任何库", len(FAKE.docs) == 0, f"{len(FAKE.docs)} 条")

    print("\n== 13. 重复回「入库」被挡，不产生重复文档 ==")
    base_n = len(read_replies())
    post(msg_event("ev_ingest_2", "ou_mock_zhang", "入库", mid="om_ingest_2"))
    wait_replies(base_n + 1, 60)
    base_n = len(read_replies())
    post(msg_event("ev_ingest_3", "ou_mock_zhang", "入库", mid="om_ingest_3"))
    wait_replies(base_n + 1, 60)
    r13 = read_replies()[-1]["text"] if len(read_replies()) > base_n else ""
    check("第二次被挡", "已经在" in r13, r13.replace("\n", " ")[:110])
    check("库里还是 1 条", len(FAKE.docs) == 1, f"{len(FAKE.docs)} 条")

    print("\n== 14. 撤销之后仍能对那张图提问 ==")
    base_n = len(read_replies())
    post(msg_event("ev_revoke_2", "ou_mock_zhang", "撤销", mid="om_revoke_2"))
    wait_replies(base_n + 1, 60)
    base_n = len(read_replies())
    post(msg_event("ev_ask_2", "ou_mock_zhang", "刚刚识别的图片", mid="om_ask_2"))
    wait_replies(base_n + 1, 120)
    r14 = read_replies()[-1]["text"] if len(read_replies()) > base_n else ""
    check("撤销后仍按图回答", "按你刚发的那张图" in r14, r14.replace("\n", " ")[:120])
    check("撤销后没偷偷写回库", len(FAKE.docs) == 0, f"{len(FAKE.docs)} 条")

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
    ap.add_argument("--ws", action="store_true",
                    help="用飞书长连接收事件（真环境推荐，无需公网 IP）")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.ws:
        sys.exit(run_ws())
    if a.load:
        print("模式:", "mock" if MOCK else "real")
        print("员工名册:", json.dumps(LOCAL_USERS, ensure_ascii=False, indent=2))
        print("已吊销:", json.dumps(sorted(load_revoked()), ensure_ascii=False))
        print("凭据:", "完整" if not MOCK else "缺失（MOCK 模式）")
        return
    serve(a.port)


if __name__ == "__main__":
    main()
