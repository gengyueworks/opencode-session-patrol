# opencode-session-patrol

**opencode 会话巡航医生** —— 自动发现卡死的 opencode 会话，诊断死因，发送针对性处方让它恢复。不是喊「继续」，是告诉它病灶在哪、药方是什么。

**English version below ⬇️**

---

## 为什么需要它

用 opencode（尤其免费/共享额度的模型）批量跑任务时，会话经常卡死在 `Service Unavailable`：

- 人只能看到界面上一个报错，看不到后台到底哪一步断了
- 反复喊「继续」经常无效，因为错误在循环：它每次都在同一个地方断
- 几十个并行会话时，人工盯根本盯不过来

本工具相当于一个**能看后台的医生**：直接读 opencode 的本地数据库，识别卡死签名，判断死因，然后给会话发一条**针对它当前病情的处方**。

## 工作原理

```
launchd / 常驻循环（每 5 分钟或每 30 分钟）
   │
   ├─ 1. 只读扫描 ~/.local/share/opencode/opencode.db
   │      找出「近 3 小时活跃 + 最后一步符合卡死签名 + 停留超 5 分钟」的会话
   │
   ├─ 2. 诊断：看它最后一段在干什么（写大文件？等子 agent？）
   │
   └─ 3. 开药方：通过 opencode serve API 给该会话发一条针对性消息
          例：「你反复死在写大文件上，改成每次只写一片、写完落盘再写下一片」
```

### 四种卡死签名（诊断规则）

| 签名 | 数据库特征 | 含义 |
|---|---|---|
| `api-error` | 最后一步 `step-finish reason=unknown` | 请求被上游拒绝（限流/过载），input token 可为 0 或非 0 |
| `empty-stream` | 最后一条是空 text 且带 start 时间 | 输出流中途断掉 |
| `nudged-no-response` | 最后一条是巡航发的处方消息 | 推了但没任何响应——推完后卡住状态会被推消息掩盖，必须专门识别 |
| `no-assistant-start` | 最后一条是用户消息（role=user）且无后续 | 用户发了消息但助手压根没开始跑 |

误判保护：`reason=stop` 是正常收尾，绝不触碰；正在跑的工具调用不碰；同一会话 15 分钟冷却、最多推 8 次、每轮最多推 2 个（控制并发）。

### 处方生成逻辑

不是固定话术，按「签名 × 现场上下文」组合：

- **反复死在写大文件** → 「拆成小片分多次写入，写完落盘再写下一片，最后合并校验」
- **在等一个自己也挂了的子 agent** → 「不要无限等，改用程序化验收（文件大小/尺寸/数量硬指标），继续主线下一步」
- **普通限流抖动** → 「从中断处继续，大段输出拆小」

## 安装

前置：[opencode](https://opencode.ai) CLI 已安装（`~/.opencode/bin/opencode`）。

```bash
# 1. 设置 API 密码（serve 实例的 basic auth）
export OPENCODE_PATROL_PASSWORD="你的密码"

# 2. 手动跑一轮验证
python3 nightpatrol.py --once

# 3. 常驻模式（比如夜间批量任务时）
python3 nightpatrol.py --hours 12

# 4. launchd 全天候模式（每 30 分钟一轮，见 plist.example）
cp com.local.opencode-nightpatrol.plist.example ~/Library/LaunchAgents/com.local.opencode-nightpatrol.plist
# 编辑 plist：把密码写进 EnvironmentVariables，路径改成你的
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.opencode-nightpatrol.plist
```

脚本检测到 serve 实例不在时会自动用同密码在 5599 端口拉起一个（与桌面版共用同一数据目录，互不干扰）。

## 监控

```bash
tail -20 ~/Library/Logs/nightpatrol.log   # 巡航日志：每轮卡住数、推动详情
pkill -f nightpatrol.py                    # 停止巡航
```

## 实战效果

2026-08-22 单日实测：

- 夜间 36 个并行会话挤爆免费模型，一夜自动推动 6 次、救回 4 个窗口
- 白天批量写作/翻译批次反复卡死，逐个诊断出三种不同死因（大文件输出被掐、等死的子 agent、请求没发出），分别开方后全部恢复推进
- 其中一个翻译批次连挂 3 次都是同一个动作（一次性写 65 条翻译的大脚本），处方改为分 4 片写入后一次通过

## License

MIT

---

## English

**opencode Session Patrol Doctor** — automatically detects stuck opencode sessions, diagnoses *why* they are stuck, and sends a targeted prescription so they actually recover. Instead of yelling "continue", it tells the session what is wrong and how to fix it.

### The problem

When running batch workloads on opencode (especially free / shared-quota models), sessions frequently die on `Service Unavailable`. From the outside you only see an error; retrying often loops on the same failure, and nobody can watch dozens of parallel sessions manually.

This tool reads opencode's local SQLite database directly (read-only), detects stall signatures, and injects a context-aware prescription via the opencode serve API.

### Stall signatures

| Signature | DB pattern | Meaning |
|---|---|---|
| `api-error` | last step `step-finish reason=unknown` | request rejected upstream (throttling), input tokens may be zero or non-zero |
| `empty-stream` | empty `text` part with a start timestamp | output stream died mid-generation |
| `nudged-no-response` | last part is a previous patrol message | nudged but no reaction — the stall hides behind the nudge itself |
| `no-assistant-start` | last part is a user message with no follow-up | message never started a run |

Safety rails: never touches sessions that finished normally (`reason=stop`); 15-min cooldown per session; max 8 nudges per session; max 2 nudges per cycle.

### Prescriptions (context-aware, not canned)

- **Died repeatedly writing one huge file** → "split into small chunks, write one chunk per call, merge and verify at the end"
- **Waiting on a subagent that is itself failing** → "don't wait forever; fall back to programmatic verification (file size / dimensions / counts) and move on"
- **Generic throttle blip** → "resume from where you stopped; keep outputs small"

### Install

Requires the [opencode](https://opencode.ai) CLI.

```bash
export OPENCODE_PATROL_PASSWORD="your-secret"
python3 nightpatrol.py --once     # single sweep
python3 nightpatrol.py --hours 12 # resident mode
# see com.local.opencode-nightpatrol.plist.example for the launchd (every-30-min) setup
```

The script auto-starts its own `opencode serve` instance (port 5599) sharing the same data directory as the desktop app.

### Monitoring

```bash
tail -20 ~/Library/Logs/nightpatrol.log
pkill -f nightpatrol.py
```

MIT licensed.
