#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opencode 会话巡航医生（Session Patrol Doctor）

监控本机 opencode 会话，发现卡死的会话后自动诊断死因，
并通过 opencode serve API 发送「针对性处方」帮它恢复——
不是喊"继续"，而是告诉它问题在哪、该怎么改。

卡死签名（诊断规则）：
  A. api-error          最后一步 step-finish reason=unknown（请求被上游拒，input 可为 0 或非 0）
  B. empty-stream       空 text 挂起（流断了）
  C. nudged-no-response 推过消息但没有任何响应（推完后卡住状态会被推消息掩盖）
  D. no-assistant-start 用户发了消息但助手一直没开始跑

处方（按诊断 + 现场上下文生成）：
  - 死在写大文件 → 分片写入策略
  - 等一个自己也挂了的子 agent → 改程序化验收 / 不要无限等
  - 普通限流抖动 → 从中断处继续 + 缩小单步输出

用法：
    python3 nightpatrol.py              # 常驻循环（默认 7 小时）
    python3 nightpatrol.py --hours 12   # 自定义时长
    python3 nightpatrol.py --once       # 只巡一轮（launchd 每 30 分钟模式 / 验证用）

环境变量：
    OPENCODE_PATROL_PASSWORD   serve API 的 basic auth 密码（必须设置）
    OPENCODE_PATROL_PORT       serve API 端口（默认 5599）

日志：~/Library/Logs/nightpatrol.log
"""
import base64
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
LOG = os.path.expanduser("~/Library/Logs/nightpatrol.log")
STATE = "/tmp/nightpatrol-state.json"
PORT = os.environ.get("OPENCODE_PATROL_PORT", "5599")
PASSWORD = os.environ.get("OPENCODE_PATROL_PASSWORD", "")
URL = f"http://127.0.0.1:{PORT}"
AUTH = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode() if PASSWORD else ""

EXCLUDE_IDS = set()
EXCLUDE_TITLE_KEYWORDS = ["夜间巡航", "patrol"]

CYCLE_SEC = 300
STALL_MIN = 5
NUDGE_COOLDOWN = 900
MAX_NUDGES_PER_SESSION = 8
MAX_NUDGES_PER_CYCLE = 2


def log(msg):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"nudges": {}}


def save_state(st):
    json.dump(st, open(STATE, "w"))


def classify_last_part(cur, sid, ts, d):
    """返回卡死签名（字符串）或 None。"""
    typ = d.get("type")
    if typ == "step-finish" and d.get("reason") == "unknown":
        return "api-error"
    if typ == "text" and d.get("text", "") == "":
        return "empty-stream"
    txt = d.get("text") or ""
    if typ == "text" and "巡航" in txt:
        return "nudged-no-response"
    if typ == "text":
        mrow = cur.execute(
            "SELECT data FROM message WHERE id=(SELECT message_id FROM part WHERE session_id=? AND time_created=? LIMIT 1)",
            (sid, ts),
        ).fetchone()
        if mrow:
            try:
                if json.loads(mrow[0]).get("role") == "user":
                    return "no-assistant-start"
            except Exception:
                pass
    return None


def find_stalled():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    cutoff = int((time.time() - 3 * 3600) * 1000)
    rows = cur.execute(
        "SELECT id, title FROM session WHERE time_updated > ? ORDER BY time_updated DESC",
        (cutoff,),
    ).fetchall()
    now_ms = int(time.time() * 1000)
    out = []
    for sid, title in rows:
        if sid in EXCLUDE_IDS or any(k in (title or "") for k in EXCLUDE_TITLE_KEYWORDS):
            continue
        last = cur.execute(
            "SELECT time_created, data FROM part WHERE session_id=? ORDER BY time_created DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if not last:
            continue
        ts, data = last
        try:
            d = json.loads(data)
        except Exception:
            continue
        sig = classify_last_part(cur, sid, ts, d)
        if sig and now_ms - ts > STALL_MIN * 60 * 1000:
            out.append((sid, title or "", sig))
    con.close()
    return out


def gather_context(cur, sid):
    """看会话最后一段在干什么，用于选处方。"""
    rows = cur.execute(
        "SELECT data FROM part WHERE session_id=? ORDER BY time_created DESC LIMIT 15",
        (sid,),
    ).fetchall()
    waiting_on_task = False
    writing_files = False
    for (d,) in rows:
        try:
            j = json.loads(d)
        except Exception:
            continue
        if j.get("type") == "tool":
            tool = j.get("tool")
            st = (j.get("state") or {}).get("status")
            if tool == "task" and st == "running":
                waiting_on_task = True
            if tool in ("write", "edit") and st in ("running", "completed"):
                writing_files = True
    return waiting_on_task, writing_files


def prescription(sig, waiting_on_task, writing_files, nudge_count):
    """按诊断生成针对性处方（而不是喊'继续'）。"""
    head = "[巡航诊断] "
    if sig == "api-error" and writing_files and nudge_count >= 1:
        return (head + "你反复死在同一个动作上：单次输出太长被服务端掐断。"
                "换策略：把要写的内容拆成小片分多次写入（每次只写一个文件或一个分片），"
                "写完一片立刻落盘再写下一片；最后用一个小脚本合并并校验数量。现在从第一片开始重试。")
    if sig == "api-error" and waiting_on_task:
        return (head + "你在等的子任务自己也在报 API 错误，不要无限等。"
                "给它一个检查点：若仍无结果就改用可程序化验证的方式顶替（检查文件大小/尺寸/数量等硬指标），"
                "然后直接继续主任务的下一步，不要阻塞在等待上。")
    if sig == "api-error":
        return (head + "刚才的请求被上游拒绝了（Service Unavailable）。"
                        "从中断处继续；如果上一步是大段输出，把它拆成更小的步骤分次完成。")
    if sig == "no-assistant-start":
        return (head + "你上一条消息发出后一直没有开始执行，应该是请求没发出去。请重新处理上一条消息。")
    if sig == "nudged-no-response":
        return (head + "之前的恢复指令你没有收到响应。请检查当前进度状态，从最后一步未完成的动作继续。")
    if sig == "empty-stream":
        return (head + "你的输出流中途断掉了。请从中断处继续，同样注意把大段输出拆小。")
    return head + "请从最后一步未完成的动作继续。"


def post_message(sid, text):
    body = json.dumps({"parts": [{"type": "text", "text": text}]}).encode()
    req = urllib.request.Request(
        f"{URL}/session/{sid}/message",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {AUTH}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass  # 该接口会挂住等回复，超时正常；以 DB 落库为准
    time.sleep(3)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute(
        "SELECT COUNT(*) FROM part WHERE session_id=? AND data LIKE '%巡航诊断%'",
        (sid,),
    ).fetchone()
    con.close()
    return row[0] > 0


def ensure_server():
    """巡航依赖的 API 实例挂了就拉起来。"""
    try:
        r = urllib.request.Request(f"{URL}/session", headers={"Authorization": f"Basic {AUTH}"})
        urllib.request.urlopen(r, timeout=5)
        return
    except Exception:
        pass
    if not PASSWORD:
        log("错误：未设置 OPENCODE_PATROL_PASSWORD，无法启动/访问 serve 实例")
        sys.exit(2)
    log("API 实例无响应，重启 opencode serve ...")
    subprocess.run(["pkill", "-f", f"opencode serve --port {PORT}"], capture_output=True)
    env = dict(os.environ, OPENCODE_SERVER_PASSWORD=PASSWORD)
    subprocess.Popen(
        [os.path.expanduser("~/.opencode/bin/opencode"), "serve", "--port", PORT, "--log-level", "WARN"],
        env=env,
        stdout=open("/tmp/nightpatrol-server.log", "a"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(8)


def already_running():
    r = subprocess.run(["pgrep", "-f", "nightpatrol.py"], capture_output=True, text=True)
    pids = [int(p) for p in r.stdout.split() if p.strip()]
    me = os.getpid()
    return any(p != me for p in pids)


HEARTBEAT = "/tmp/nightpatrol-heartbeat"


def heartbeat():
    """每轮成功巡检后更新心跳文件，供自检/看门狗判断健康。"""
    json.dump({"ts": int(time.time()), "pid": os.getpid()}, open(HEARTBEAT, "w"))


def self_test():
    """验证核心链路（DB 只读扫描）是否可用。返回 True/False。"""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM session WHERE time_updated > ?", (0,)).fetchone()
        con.close()
        return True
    except Exception:
        return False


def main():
    if not PASSWORD:
        print("错误：请设置 OPENCODE_PATROL_PASSWORD 环境变量", file=sys.stderr)
        sys.exit(2)

    hours, once = 7.0, False
    args = sys.argv[1:]
    if "--once" in args:
        once = True
    if "--hours" in args:
        hours = float(args[args.index("--hours") + 1])

    if already_running():
        # 即使有常驻实例，--once 模式也要做真实自检（否则实例病了检测不到）
        ok = self_test()
        log(f"已有巡航实例在跑；自检{'通过' if ok else '失败！核心链路异常'}")
        if not ok:
            sys.exit(3)
        return

    log(f"=== 会话巡航启动，运行 {hours} 小时，每 {CYCLE_SEC//60} 分钟巡一轮 ===")
    st = load_state()
    start = time.time()
    cycle = 0
    consecutive_errors = 0
    while True:
        cycle += 1
        try:
            ensure_server()
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            cur = con.cursor()
            stalled = find_stalled()
            nudged = 0
            for sid, title, sig in stalled:
                hist = st["nudges"].get(sid, [])
                now = time.time()
                if len(hist) >= MAX_NUDGES_PER_SESSION or (hist and now - hist[-1] < NUDGE_COOLDOWN):
                    continue
                if nudged >= MAX_NUDGES_PER_CYCLE:
                    break
                try:
                    waiting_task, writing = gather_context(cur, sid)
                except Exception as e:
                    log(f"第{cycle}轮 诊断 [{title[:28]}] 出错：{e!r}（跳过）")
                    continue
                msg = prescription(sig, waiting_task, writing, len(hist))
                if post_message(sid, msg):
                    hist.append(now)
                    st["nudges"][sid] = hist
                    save_state(st)
                    nudged += 1
                    log(f"第{cycle}轮 处方→[{title[:28]}] ({sig}) {msg[:60]}…")
            con.close()
            heartbeat()
            consecutive_errors = 0
            log(f"第{cycle}轮 巡检完成：卡住={len(stalled)} 本轮推动={nudged}")
        except Exception as e:
            consecutive_errors += 1
            log(f"第{cycle}轮 异常（连续第{consecutive_errors}次）：{e!r}")
            if consecutive_errors >= 3:
                log("连续 3 轮异常，自我重启……")
                os.execv(sys.executable, [sys.executable] + sys.argv)
        if once or time.time() - start >= hours * 3600:
            break
        time.sleep(CYCLE_SEC)
    log(f"=== 会话巡航结束（共 {cycle} 轮） ===")


if __name__ == "__main__":
    main()
