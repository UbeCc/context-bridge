#!/usr/bin/env python3
"""Offline mock of the Context Bridge HTTP API (see ``API.md``).

The real console is served by ``ctxbridge serve`` (``context_bridge/server.py``). This script is a
development tool: it serves API.md-shaped **fictional demo data** plus the static console from
``web/`` on one port, so the UI can be developed, reviewed and screenshotted without a live hub,
without Feishu credentials and without any network access.

Everything here is synthetic and safe to publish: the three bots, their tickets, sessions, board
cards and commands are made up. Timestamps are generated relative to server start so the activity
chart always looks alive.

Usage (from anywhere):

    python3 web/dev/mock-api.py                    # http://localhost:8787/
    python3 web/dev/mock-api.py --port 9000
    python3 web/dev/mock-api.py --no-fake-events   # stable page for screenshots
    python3 web/dev/mock-api.py --token secret     # exercise the 401/token flow

Options: ``--host``, ``--port``, ``--token``, ``--no-fake-events``, ``--verbose``.

This file is stdlib-only and intentionally keeps all demo data in memory.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import queue
import random
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

WEB_ROOT = Path(__file__).resolve().parents[1]  # .../web
VERSION = "0.1.0"
START_TIME = time.time()
NOW = datetime.now(timezone.utc).replace(microsecond=0)

BOARD_COLUMNS = [
    {"id": "inbox", "title": "待处理"},
    {"id": "in_progress", "title": "进行中"},
    {"id": "review", "title": "待验收"},
    {"id": "done", "title": "已完成"},
]
COLUMN_IDS = [c["id"] for c in BOARD_COLUMNS]

BOTS = [
    {
        "id": "opus",
        "label": "Opus",
        "profile": "superagent",
        "color": "#7c5cff",
        "emoji": "🟣",
        "role": "总控与架构",
        "sync_age_s": 40,
        "status": "active",
    },
    {
        "id": "fable",
        "label": "Fable",
        "profile": "superagent-fable",
        "color": "#f0a13b",
        "emoji": "🟠",
        "role": "研究与文档",
        "sync_age_s": 540,
        "status": "idle",
    },
    {
        "id": "sonnet",
        "label": "Sonnet",
        "profile": "superagent-sonnet",
        "color": "#2fc8b5",
        "emoji": "🩵",
        "role": "实现与交付",
        "sync_age_s": 95,
        "status": "active",
    },
]

TICKET_POOL = [
    ("构建跨机器人共享上下文桥", "梳理三个 bot 的状态目录与采集边界，定义记录模型与 git 仓库布局。", ["bridge", "architecture"]),
    ("接入 DSH 会话摘要采集", "解析 v4 jsonl 会话流，抽取标题、首个请求与最终结论，按 4KB 截断并脱敏。", ["collector", "session"]),
    ("实现全文检索索引重建", "用 SQLite FTS5 建立可重建缓存，记录目录 mtime 变化时标记索引过期。", ["index", "fts"]),
    ("看板并发写入互斥", "board.json 读改写加文件锁，避免多写者覆盖，补并发测试。", ["board", "concurrency"]),
    ("飞书指令中继与投递状态", "以桥接身份 @ 目标 bot 发送指令，记录 sent/failed/skipped 与 message_id。", ["console", "relay"]),
    ("脱敏规则覆盖矩阵", "对密钥形状、令牌、JWT、PEM、私网地址逐条构造正反例，确认真实标识不被误伤。", ["redaction", "security"]),
    ("控制台 SSE 实时刷新", "hub 文件变更推送 sync/board/command 事件，15 秒心跳，断线自动重连。", ["web", "sse"]),
    ("演示数据与公开截图", "生成全虚构的 demo hub，覆盖三个 bot 与全部页面，禁止真实数据入仓。", ["demo", "docs"]),
    ("每日摘要确定性生成", "同日输入产生逐字节相同的 Markdown，便于 git diff 审计。", ["digest", "git"]),
    ("部署脚本一键落地", "校验 SQLite 版本、初始化 hub、写配置、拉起 tmux 服务并等待健康检查。", ["deploy", "ops"]),
    ("记忆召回排序策略", "按更新时间与关键词命中率排序，限制单次返回条数，避免上下文爆炸。", ["search", "recall"]),
    ("索引陈旧自动重建", "文件 mtime 与索引 mtime 比较，陈旧时在 sync 末尾重建，失败降级线性扫描。", ["index", "resilience"]),
    ("会话归属判定", "按 cwd slug 前缀把会话文件匹配到 bot，无法归属的会话丢弃并记录计数。", ["collector", "session"]),
    ("公开安全扫描门禁", "tracked files 全量扫描内部路径、凭证形状与飞书 id，命中即失败。", ["ci", "security"]),
]
DECISION_POOL = [
    ("接受：git 仓库作为唯一事实源", "所有记忆、看板与投递记录都落盘 git，快照与回滚用 git 操作，不引入额外数据库服务。", ["architecture"]),
    ("忽略：引入 Redis 作为中转缓存", "本地单机场景下 SQLite FTS 足够，多一个常驻服务不划算。", ["performance"]),
    ("停止：直接向 bot 进程注入指令", "绕过 bot 的授权与审计链路风险不可控，统一走聊天中继。", ["security", "relay"]),
    ("不可用：CDN 加载前端资源", "部署机离线，控制台必须零外链、零构建，全部使用本地静态文件。", ["web", "offline"]),
    ("接受：日报按天确定性生成", "同样的记录集合产出相同 Markdown，方便审查与差分。", ["digest"]),
    ("忽略：自动公开镜像实时数据", "hub 含内部材料，公开仓库只放合成 demo 数据。", ["privacy"]),
    ("接受：采集器单飞模式", "同一时刻只允许一个 sync 在跑，重复触发直接忽略。", ["concurrency"]),
    ("停止：在记录里保存原始会话全文", "只存摘要，全文留在各自 bot 的会话目录，降低泄漏面。", ["privacy", "session"]),
    ("接受：脱敏在写入路径统一执行", "所有写库入口经同一 Redactor，读取端不再二次处理。", ["redaction"]),
]
TRIGGER_POOL = [
    ("请汇总一下最近的桥接进度，重点看跑通没有", "haoran"),
    ("看板上待验收的卡片让 sonnet 逐条过一遍", "haoran"),
    ("把这次脱敏测试的结果贴到共享记忆里", "haoran"),
    ("演示环境部署到哪个端口？更新一下部署说明", "haoran"),
    ("opus 的报告里提到索引慢，谁跟一下？", "haoran"),
    ("今天的日报生成后同步到看板卡片", "haoran"),
    ("检查一下 fable 的会话摘要对 v4 格式的兼容", "haoran"),
    ("桥接服务重启后健康检查通过了吗", "haoran"),
    ("把看板里待处理的卡片按优先级排一下", "haoran"),
]
SESSION_POOL = [
    ("桥接架构评审与模块拆分", "把共享上下文拆成 hub/collector/index/server 四层，先定接口再并行实现。", "结论：模块边界清晰，按 API.md 冻结契约，前端独立开发。", 42),
    ("FTS5 索引重建失败排查", "索引在 hub 内写失败，怀疑锁冲突；逐层验证文件权限与事务边界。", "结论：重建改为临时目录写完后整体替换，避免半成品索引。", 37),
    ("看板拖拽交互实现", "用原生 drag 事件加键盘左右移动按钮，移动后乐观更新并回滚失败请求。", "结论：桌面拖拽、移动端按钮、键盘操作三套入口都可用。", 29),
    ("控制台指令投递链路联调", "dry-run 先跑通命令记录与状态流转，再接真实聊天源做一次人工确认。", "结论：dry-run 记录 status=skipped，真实链路待运营窗口验证。", 55),
    ("公开安全扫描修复", "扫描器命中内部路径与示例 token，逐条替换为占位符并补注释。", "结论：扫描通过，CI 增加 tracked files 门禁。", 18),
    ("演示 hub 数据设计", "设计三个虚构 bot、14 天活动轨迹与看板卡片，覆盖全部页面截图。", "结论：数据全部由脚本生成，可复现、可公开。", 24),
    ("会话摘要截断与脱敏", "长会话只保留标题、首个请求、最终结论与工具计数，写入前统一脱敏。", "结论：单条摘要上限 4KB，脱敏计数随记录落盘。", 33),
    ("SSE 断线重连与心跳", "EventSource 自动重连，服务端 15 秒注释心跳，前端展示连接状态。", "结论：断线 3 秒内恢复，页面无重复注册监听。", 21),
    ("记忆时间线与筛选交互", "按天聚合记录，bot 与 kind 双维筛选，搜索框防抖后走 /api/search。", "结论：搜索命中高亮，空状态给出下一步建议。", 26),
]
NOTE_POOL = [
    ("共享板约定：卡片标题不超过 40 字，描述写验收标准", "写看板卡片时标题短、描述可验证，assignee 只填一个 bot。", ["convention", "board"]),
    ("hub 快照恢复步骤已写入运行手册", "git checkout 到上一个提交即可回滚记录与看板，索引删除后重建。", ["runbook", "git"]),
    ("发现：索引目录可以随手删除重建", "hub/index/search.sqlite 是缓存不是事实源，坏了不影响记忆。", ["index", "ops"]),
    ("脱敏只在写入前做一轮，读取不再二次处理", "因此写入路径必须唯一，任何绕过 Redactor 的写库都是 bug。", ["redaction"]),
    ("演示数据禁止使用真实会话与工单", "公开仓库的示例数据必须全虚构、参数化生成，便于审计。", ["privacy", "demo"]),
    ("看板卡片移动后由 SSE 通知前端重取", "服务端不需要在事件里推详情，前端收到 board 事件刷新即可。", ["web", "sse"]),
    ("控制台不直接注入 bot 进程", "指令经聊天源送达，bot 的正常授权与审计链路仍然生效。", ["console", "security"]),
]

KIND_ORDER = ["ticket", "decision", "trigger", "session", "note"]
KIND_WEIGHTS = {"ticket": 0.32, "session": 0.24, "trigger": 0.15, "decision": 0.15, "note": 0.10, "command": 0.04}
KIND_LABEL = {
    "ticket": "工单",
    "decision": "决策",
    "trigger": "触发",
    "session": "会话",
    "note": "备注",
    "command": "指令",
}
ATTENTION_QUESTIONS = [
    "是否需要先合并 feat/bridge-mvp 再部署演示环境？",
    "索引重建耗时 8 秒，是否可以接受？",
    "脱敏规则里私网地址是否要连同端口一起隐藏？",
    "日报是否要同时推送到飞书群，还是只留在控制台？",
]

UP = {"status": "active", "message": "运行中"}
DOWN = {"status": "idle", "message": "空闲"}


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _hex(seed: str, n: int = 16) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:n]


def _weighted_kind(rng: random.Random) -> str:
    roll = rng.random()
    acc = 0.0
    for kind, weight in KIND_WEIGHTS.items():
        acc += weight
        if roll <= acc:
            return kind
    return "note"


def _record_meta(rng: random.Random, bot: dict, kind: str, title: str, summary: str, tags: list[str], created: datetime) -> dict:
    hexid = _hex(f"{bot['id']}:{kind}:{title}:{iso(created)}")
    updated = created + timedelta(minutes=rng.randint(2, 420))
    if updated > NOW:
        updated = NOW - timedelta(minutes=rng.randint(1, 30))
    if kind == "ticket":
        state = "open" if rng.random() < 0.42 else "ended"
        payload = {
            "state": state,
            "reports": [
                {"state": "progress", "summary": summary},
                {"state": "done", "summary": "已按验收标准自测通过，等待复核。"} if state == "ended" else {"state": "progress", "summary": "正在补齐边界用例。"},
            ],
        }
        refs = {"ticket": _hex(title, 8)}
        source = {"system": "dsh-superagent", "profile": bot["profile"], "source_id": refs["ticket"], "path": f"/srv/context-hub/demo/{bot['id']}/orchestrator.sqlite"}
    elif kind == "decision":
        payload = {
            "decision": rng.choice(["accept", "ignore", "stop", "unavailable"]),
            "reason": summary,
            "strength": rng.choice(["firm", "normal"]),
        }
        refs = {}
        source = {"system": "dsh-superagent", "profile": bot["profile"], "source_id": _hex(title, 8), "path": f"/srv/context-hub/demo/{bot['id']}/orchestrator.sqlite"}
    elif kind == "trigger":
        actor = summary
        payload = {"text": title, "actor": actor, "addressed": True}
        refs = {}
        source = {"system": "dsh-superagent", "profile": bot["profile"], "source_id": _hex(title, 8), "path": f"/srv/context-hub/demo/{bot['id']}/orchestrator.sqlite"}
    elif kind == "session":
        payload = {
            "title": title,
            "first_request": summary,
            "outcome": "结论：任务完成，关键决定与遗留问题已写入共享记忆。",
            "tool_calls": rng.randint(8, 60),
        }
        refs = {"session": _hex(title, 8)}
        source = {"system": "dsh-sessions", "profile": bot["profile"], "source_id": refs["session"], "path": f"/srv/context-hub/demo/{bot['id']}/sessions"}
    else:
        payload = {"text": summary}
        refs = {}
        source = {"system": "ctxbridge note", "profile": bot["profile"], "source_id": _hex(title, 8), "path": f"/srv/context-hub/demo/{bot['id']}/notes"}

    return {
        "v": 1,
        "id": f"{bot['id']}:{kind}:{hexid}",
        "bot": bot["id"],
        "kind": kind,
        "title": title,
        "summary": summary,
        "created_at": iso(created),
        "updated_at": iso(updated),
        "tags": list(tags),
        "refs": refs,
        "source": source,
        "payload": payload,
        "redactions": rng.choices([0, 0, 1, 2, 3], weights=[0.55, 0.2, 0.15, 0.07, 0.03])[0],
    }


def _brief(record: dict) -> dict:
    return {
        "id": record["id"],
        "bot": record["bot"],
        "kind": record["kind"],
        "title": record["title"],
        "summary": record["summary"][:600],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "tags": list(record["tags"]),
    }


def _make_record(rng: random.Random, bot: dict, day: int, slot: int, kind: str) -> dict:
    day_dt = NOW - timedelta(days=day)
    if day == 0:
        hour = rng.randint(0, max(NOW.hour - 1, 0))
        minute = rng.randint(0, 59)
        created = day_dt.replace(hour=hour, minute=minute, second=rng.randint(0, 59))
        recent_minutes = rng.randint(3, 52)
        created = min(created, NOW - timedelta(minutes=recent_minutes))
    else:
        created = day_dt.replace(hour=rng.randint(7, 22), minute=rng.randint(0, 59), second=rng.randint(0, 59))

    if kind == "ticket":
        title, summary, tags = rng.choice(TICKET_POOL)
    elif kind == "decision":
        title, summary, tags = rng.choice(DECISION_POOL)
    elif kind == "trigger":
        title, actor = rng.choice(TRIGGER_POOL)
        summary, tags = actor, ["trigger"]
    elif kind == "session":
        title, summary, _outcome, _tools = rng.choice(SESSION_POOL)
        tags = ["session"]
    elif kind == "note":
        title, summary, tags = rng.choice(NOTE_POOL)
    else:
        title, summary, tags = (f"通过控制台向 {bot['label']} 发送指令", "演示指令记录：dry-run 投递成功。", ["command"])
    return _record_meta(rng, bot, kind, title, summary, tags, created)


def build_demo_data() -> dict:
    rng = random.Random(20261113)
    bots_by_id = {b["id"]: b for b in BOTS}
    records: list[dict] = []
    used_titles: dict[tuple[str, str], set[str]] = {}

    for day in range(13, -1, -1):
        for bot in BOTS:
            # Keep per-bot titles distinct where there is a choice.
            seen = used_titles.setdefault((bot["id"], "all"), set())
            count = 3 if day == 0 else rng.randint(1, 3)
            for slot in range(count):
                kind = _weighted_kind(rng)
                record = _make_record(rng, bot, day, slot, kind)
                if record["title"] in seen and kind in ("ticket", "session", "note", "decision"):
                    # Retry a few times to avoid duplicate titles inside one bot.
                    for _ in range(4):
                        record = _make_record(rng, bot, day, slot, kind)
                        if record["title"] not in seen:
                            break
                seen.add(record["title"])
                records.append(record)

    # A couple of records created "right now" so the feed feels live.
    for i, bot in enumerate(BOTS[:2]):
        kind = "session" if i == 0 else "ticket"
        rec = _make_record(rng, bot, 0, 99, kind)
        rec["created_at"] = iso(NOW - timedelta(minutes=6 + i * 7))
        rec["updated_at"] = iso(NOW - timedelta(minutes=2 + i * 3))
        records.append(rec)

    # Attention items: two recent open tickets.
    attention: list[dict] = []
    attention_candidates = sorted(
        (r for r in records if r["kind"] == "ticket" and r["payload"].get("state") == "open"),
        key=lambda r: r["updated_at"],
        reverse=True,
    )
    chosen: list[dict] = []
    seen_bots: set[str] = set()
    # Prefer spreading attention items across different bots.
    for candidate in attention_candidates:
        if len(chosen) >= len(ATTENTION_QUESTIONS[:2]):
            break
        if candidate["bot"] in seen_bots:
            continue
        chosen.append(candidate)
        seen_bots.add(candidate["bot"])
    for candidate in attention_candidates:
        if len(chosen) >= len(ATTENTION_QUESTIONS[:2]):
            break
        if candidate not in chosen:
            chosen.append(candidate)
    for index, question in enumerate(ATTENTION_QUESTIONS[: len(chosen)]):
        rec = chosen[index]
        rec["payload"]["attention"] = True
        rec["payload"]["question"] = question
        attention.append(
            {
                "bot": rec["bot"],
                "record_id": rec["id"],
                "title": rec["title"],
                "question": question,
                "at": rec["updated_at"],
            }
        )

    records.sort(key=lambda r: r["updated_at"], reverse=True)
    records_by_id = {r["id"]: r for r in records}

    # Board cards.
    board_cards = [
        {"id": "c_demo001", "title": "确认演示环境端口并更新部署说明", "desc": "在 deploy.env 里配置 PORT，健康检查通过后补充到「关于」页面。", "column": "inbox", "assignee": "opus", "tags": ["deploy"], "created_at": iso(NOW - timedelta(days=1, hours=2)), "updated_at": iso(NOW - timedelta(hours=3)), "author": "human"},
        {"id": "c_demo002", "title": "补充记忆搜索的空状态与错误态", "desc": "无结果时给出建议关键词；接口失败时提供重试按钮。", "column": "inbox", "assignee": "sonnet", "tags": ["web", "ux"], "created_at": iso(NOW - timedelta(days=1)), "updated_at": iso(NOW - timedelta(hours=8)), "author": "human"},
        {"id": "c_demo003", "title": "实现 14 天活动柱状图", "desc": "纯 SVG 绘制，支持 hover 数值与 reduced-motion 降级。", "column": "in_progress", "assignee": "sonnet", "tags": ["web", "chart"], "created_at": iso(NOW - timedelta(days=2)), "updated_at": iso(NOW - timedelta(hours=1)), "author": "opus"},
        {"id": "c_demo004", "title": "梳理三 bot 记忆时间线", "desc": "按天聚合工单、会话与决策，标注需要人工关注的事项。", "column": "in_progress", "assignee": "fable", "tags": ["memory"], "created_at": iso(NOW - timedelta(days=3)), "updated_at": iso(NOW - timedelta(hours=5)), "author": "human"},
        {"id": "c_demo005", "title": "控制台指令中继 dry-run 联调", "desc": "验证 POST /api/commands 返回 skipped / sent 时的展示与错误回显。", "column": "review", "assignee": "opus", "tags": ["console"], "created_at": iso(NOW - timedelta(days=4)), "updated_at": iso(NOW - timedelta(hours=2)), "author": "human"},
        {"id": "c_demo006", "title": "脱敏规则正反例语料复核", "desc": "确认 SHA / UUID / 飞书 id 不被误伤，密钥形状全部命中。", "column": "review", "assignee": "fable", "tags": ["security"], "created_at": iso(NOW - timedelta(days=5)), "updated_at": iso(NOW - timedelta(hours=11)), "author": "sonnet"},
        {"id": "c_demo007", "title": "冻结 HTTP API 契约", "desc": "API.md 覆盖全部端点，前端与 server 可以并行开发。", "column": "done", "assignee": None, "tags": ["api"], "created_at": iso(NOW - timedelta(days=9)), "updated_at": iso(NOW - timedelta(days=7)), "author": "human"},
        {"id": "c_demo008", "title": "可重建 FTS 索引落地", "desc": "索引删除后一条命令重建，搜索可降级线性扫描。", "column": "done", "assignee": "sonnet", "tags": ["index"], "created_at": iso(NOW - timedelta(days=8)), "updated_at": iso(NOW - timedelta(days=6)), "author": "opus"},
        {"id": "c_demo009", "title": "公开安全扫描接入 CI", "desc": "tracked files 全量扫描，命中内部信息即失败并给出文件行号。", "column": "done", "assignee": "opus", "tags": ["ci"], "created_at": iso(NOW - timedelta(days=10)), "updated_at": iso(NOW - timedelta(days=8)), "author": "human"},
    ]

    commands = [
        {"id": "cmd_demo0001", "target_bot": "opus", "text": "汇总昨天的桥接进度，并更新共享板上的卡片。", "via": "feishu", "status": "sent", "message_id": "om_demo0001", "error": None, "created_at": iso(NOW - timedelta(hours=2))},
        {"id": "cmd_demo0002", "target_bot": "sonnet", "text": "把「待验收」列的卡片逐条过一遍，失败的写清原因。", "via": "feishu", "status": "sent", "message_id": "om_demo0002", "error": None, "created_at": iso(NOW - timedelta(hours=9))},
        {"id": "cmd_demo0003", "target_bot": "fable", "text": "检查 demo hub 数据是否覆盖所有页面的空状态与错误态。", "via": "feishu", "status": "skipped", "message_id": None, "error": "relay disabled (demo)", "created_at": iso(NOW - timedelta(days=1, hours=3))},
        {"id": "cmd_demo0004", "target_bot": "sonnet", "text": "重启桥接服务并确认 /api/health 正常返回。", "via": "feishu", "status": "failed", "message_id": None, "error": "mock relay: chat CLI unavailable", "created_at": iso(NOW - timedelta(days=2, hours=1))},
    ]

    redaction = {
        "total": 0,
        "by_rule": {
            "api_key": 12,
            "github_token": 4,
            "aws_key": 2,
            "slack_token": 1,
            "jwt": 2,
            "pem_key": 1,
            "bearer_token": 8,
            "password_assign": 6,
            "url_credentials": 2,
            "private_ip": 34,
            "internal_domain": 18,
            "high_entropy": 4,
        },
    }
    redaction["total"] = sum(redaction["by_rule"].values())

    return {
        "bots": bots_by_id,
        "records": records_by_id,
        "records_sorted": records,
        "attention": attention,
        "board": {"columns": copy.deepcopy(BOARD_COLUMNS), "cards": board_cards},
        "commands": commands,
        "redaction": redaction,
    }


DATA = build_demo_data()
DATA_LOCK = threading.Lock()


def bot_summary(bot_id: str) -> dict:
    bot = DATA["bots"][bot_id]
    counts = Counter(r["kind"] for r in DATA["records_sorted"] if r["bot"] == bot_id)
    open_tickets = sum(
        1 for r in DATA["records_sorted"] if r["bot"] == bot_id and r["kind"] == "ticket" and r["payload"].get("state") == "open"
    )
    attention = sum(1 for a in DATA["attention"] if a["bot"] == bot_id)
    last_sync = max((r["updated_at"] for r in DATA["records_sorted"] if r["bot"] == bot_id), default=iso(NOW))
    return {
        "id": bot["id"],
        "label": bot["label"],
        "profile": bot["profile"],
        "color": bot["color"],
        "emoji": bot["emoji"],
        "status": bot["status"],
        "last_sync": last_sync,
        "counts": dict(counts),
        "open_tickets": open_tickets,
        "attention": attention,
    }


def overview() -> dict:
    by_kind = Counter(r["kind"] for r in DATA["records_sorted"])
    days: dict[str, int] = {}
    for offset in range(13, -1, -1):
        day = (NOW - timedelta(days=offset)).date().isoformat()
        days[day] = 0
    for record in DATA["records_sorted"]:
        day = record["created_at"][:10]
        if day in days:
            days[day] += 1
    return {
        "generated_at": iso(NOW),
        "bots": [bot_summary(b["id"]) for b in BOTS],
        "counts": {
            "records": len(DATA["records_sorted"]),
            "by_kind": dict(by_kind),
            "attention": len(DATA["attention"]),
        },
        "activity": [{"date": day, "count": count} for day, count in days.items()],
        "recent": [_brief(r) for r in DATA["records_sorted"][:10]],
        "attention": list(DATA["attention"]),
    }


def digest_dates(bot_id: str) -> list[str]:
    dates = sorted(
        {r["created_at"][:10] for r in DATA["records_sorted"] if r["bot"] == bot_id},
        reverse=True,
    )
    return dates[:7]


def digest_markdown(bot_id: str, date: str) -> str | None:
    if bot_id not in DATA["bots"]:
        return None
    records = [r for r in DATA["records_sorted"] if r["bot"] == bot_id and r["created_at"][:10] == date]
    if not records:
        return None
    bot = DATA["bots"][bot_id]
    kinds = Counter(r["kind"] for r in records)
    lines = [
        f"# {bot['label']} · {date} 日报",
        "",
        "> 演示数据 · 由 web/dev/mock-api.py 生成，可公开。",
        "",
        f"**今日概览**：新增记忆 {len(records)} 条（" + " · ".join(f"{KIND_LABEL.get(k, k)} {v}" for k, v in kinds.items()) + "）。",
    ]
    for kind in ["ticket", "session", "decision", "trigger", "note", "command"]:
        items = [r for r in records if r["kind"] == kind]
        if not items:
            continue
        lines += ["", f"## {KIND_LABEL.get(kind, kind)}"]
        for record in sorted(items, key=lambda r: r["created_at"]):
            lines.append(f"- **{record['title']}** — {record['summary'][:140]}")
    redactions = sum(r["redactions"] for r in records)
    lines += ["", "---", "", f"共 {len(records)} 条记录 · 写入时脱敏 {redactions} 处。"]
    return "\n".join(lines) + "\n"


def search_records(query: str, bot: str, kind: str, limit: int) -> list[dict]:
    needle = query.strip().lower()
    if not needle:
        return []
    items = []
    for record in DATA["records_sorted"]:
        if bot and record["bot"] != bot:
            continue
        if kind and record["kind"] != kind:
            continue
        haystacks = [record["title"], record["summary"], " ".join(record["tags"])]
        joined = "\n".join(haystacks)
        if needle not in joined.lower():
            continue
        if needle in record["title"].lower():
            hay = record["title"]
        elif needle in record["summary"].lower():
            hay = record["summary"]
        else:
            hay = " ".join(record["tags"])
        pos = hay.lower().find(needle)
        start = max(0, pos - 40)
        end = min(len(hay), pos + len(needle) + 90)
        snippet = ("…" if start else "") + hay[start:pos] + "<b>" + hay[pos : pos + len(needle)] + "</b>" + hay[pos + len(needle) : end] + ("…" if end < len(hay) else "")
        items.append({"record": _brief(record), "score": 1.0, "snippet": snippet})
        if len(items) >= limit:
            break
    return items


# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------

SUBSCRIBERS: set[queue.Queue] = set()
SUBSCRIBERS_LOCK = threading.Lock()


def broadcast(event: str, payload: dict) -> None:
    with SUBSCRIBERS_LOCK:
        targets = list(SUBSCRIBERS)
    for target in targets:
        try:
            target.put_nowait((event, payload))
        except queue.Full:
            pass


def fake_event_loop(interval: float = 30.0) -> None:
    while True:
        time.sleep(interval)
        broadcast("sync", {"at": iso(datetime.now(timezone.utc)), "records": len(DATA["records_sorted"])})


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}


class MockHandler(BaseHTTPRequestHandler):
    server_version = "CtxBridgeMock/0.1"
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "bad_json", "request body is not valid JSON")
            return None
        if not isinstance(parsed, dict):
            self._error(400, "bad_json", "request body must be a JSON object")
            return None
        return parsed

    def _authorized(self) -> bool:
        token = getattr(self.server, "token", "")
        if not token:
            return True
        expected = "Bearer " + token
        if self.headers.get("Authorization", "") == expected:
            return True
        self._error(401, "unauthorized", "a bearer token is required; open 关于 / 设置 to set it")
        return False

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    # -- GET ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        query = self._query()
        if path.startswith("/api/"):
            if not self._authorized():
                return
            self._api_get(path, query)
            return
        self._serve_static(path)

    def _api_get(self, path: str, query: dict) -> None:
        if path == "/api/health":
            hub_last_sync = max((r["updated_at"] for r in DATA["records_sorted"]), default=iso(NOW))
            self._send_json(200, {
                "ok": True,
                "version": VERSION,
                "uptime_s": int(time.time() - START_TIME),
                "hub": {"dir": "/srv/context-hub", "records": len(DATA["records_sorted"]), "bots": len(BOTS), "last_sync": hub_last_sync},
                "redaction": copy.deepcopy(DATA["redaction"]),
            })
        elif path == "/api/overview":
            self._send_json(200, overview())
        elif path == "/api/bots":
            self._send_json(200, {"items": [bot_summary(b["id"]) for b in BOTS]})
        elif path.startswith("/api/bots/"):
            bot_id = path[len("/api/bots/"):]
            if bot_id not in DATA["bots"]:
                self._error(404, "not_found", f"unknown bot: {bot_id}")
                return
            recent = [_brief(r) for r in DATA["records_sorted"] if r["bot"] == bot_id][:20]
            digests = [{"date": date, "path": f"digests/{bot_id}/{date}.md"} for date in digest_dates(bot_id)]
            self._send_json(200, {"bot": bot_summary(bot_id), "recent": recent, "digests": digests})
        elif path.startswith("/api/digests/"):
            parts = path[len("/api/digests/"):].split("/")
            if len(parts) != 2:
                self._error(404, "not_found", "digest path must be /api/digests/{bot}/{date}")
                return
            content = digest_markdown(parts[0], parts[1])
            if content is None:
                self._error(404, "not_found", f"no digest for {parts[0]} on {parts[1]}")
                return
            self._send_json(200, {"bot": parts[0], "date": parts[1], "content": content})
        elif path == "/api/records":
            self._api_records(query)
        elif path.startswith("/api/records/"):
            record_id = path[len("/api/records/"):]
            record = DATA["records"].get(record_id)
            if record is None:
                self._error(404, "not_found", f"record not found: {record_id}")
                return
            self._send_json(200, record)
        elif path == "/api/search":
            q = (query.get("q") or [""])[0].strip()
            bot = (query.get("bot") or [""])[0]
            kind = (query.get("kind") or [""])[0]
            limit = _int_param(query, "limit", 30, 1, 200)
            items = search_records(q, bot, kind, limit) if q else []
            self._send_json(200, {"items": items, "query": q})
        elif path == "/api/board":
            with DATA_LOCK:
                self._send_json(200, copy.deepcopy(DATA["board"]))
        elif path == "/api/commands":
            limit = _int_param(query, "limit", 30, 1, 200)
            self._send_json(200, {"items": copy.deepcopy(DATA["commands"][:limit])})
        elif path == "/api/events":
            self._sse()
        else:
            self._error(404, "not_found", f"unknown endpoint: {path}")

    def _api_records(self, query: dict) -> None:
        bot = (query.get("bot") or [""])[0]
        kind = (query.get("kind") or [""])[0]
        q = (query.get("q") or [""])[0].strip().lower()
        limit = _int_param(query, "limit", 50, 1, 200)
        offset = _int_param(query, "offset", 0, 0, 100000)
        records = DATA["records_sorted"]
        if bot:
            records = [r for r in records if r["bot"] == bot]
        if kind:
            records = [r for r in records if r["kind"] == kind]
        if q:
            records = [r for r in records if q in (r["title"] + "\n" + r["summary"] + "\n" + " ".join(r["tags"])).lower()]
        total = len(records)
        page = [_brief(r) for r in records[offset : offset + limit]]
        self._send_json(200, {"items": page, "total": total, "limit": limit, "offset": offset})

    # -- static -------------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            candidate = WEB_ROOT / "index.html"
            self._send_file(candidate)
            return
        rel = path.lstrip("/")
        candidate = (WEB_ROOT / rel).resolve()
        try:
            candidate.relative_to(WEB_ROOT)
        except ValueError:
            self._error(404, "not_found", "path traversal rejected")
            return
        if candidate.is_file():
            self._send_file(candidate)
            return
        if "." not in Path(rel).name:
            # SPA fallback: /memory, /board/... -> index.html
            self._send_file(WEB_ROOT / "index.html")
            return
        self._error(404, "not_found", "static file not found")

    def _send_file(self, candidate: Path) -> None:
        if not candidate.is_file():
            self._error(404, "not_found", "static file not found")
            return
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- SSE ----------------------------------------------------------------

    def _sse(self) -> None:
        channel: queue.Queue = queue.Queue(maxsize=200)
        with SUBSCRIBERS_LOCK:
            SUBSCRIBERS.add(channel)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            self._sse_chunk(b'event: hello\ndata: {"ok": true}\n\n')
            while True:
                try:
                    event, payload = channel.get(timeout=15)
                except queue.Empty:
                    self._sse_chunk(b": heartbeat\n\n")
                    continue
                frame = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                self._sse_chunk(frame.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with SUBSCRIBERS_LOCK:
                SUBSCRIBERS.discard(channel)

    def _sse_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    # -- writes -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/"):
            self._error(404, "not_found", "not found")
            return
        if not self._authorized():
            return
        body = self._read_json()
        if body is None:
            return
        if path == "/api/board/cards":
            self._create_card(body)
        elif path == "/api/commands":
            self._create_command(body)
        elif path == "/api/sync":
            self._send_json(202, {"ok": True})

            def _run() -> None:
                time.sleep(0.8)
                broadcast("sync", {"at": iso(datetime.now(timezone.utc)), "records": len(DATA["records_sorted"])})

            threading.Thread(target=_run, daemon=True).start()
        else:
            self._error(404, "not_found", f"unknown endpoint: {path}")

    def _create_card(self, body: dict) -> None:
        title = str(body.get("title") or "").strip()
        column = str(body.get("column") or "inbox")
        if not title:
            self._error(400, "invalid_title", "title is required")
            return
        if column not in COLUMN_IDS:
            self._error(400, "unknown_column", f"unknown column: {column}")
            return
        now = iso(datetime.now(timezone.utc))
        card = {
            "id": "c_" + _hex(f"card:{now}:{title}", 8),
            "title": title,
            "desc": str(body.get("desc") or ""),
            "column": column,
            "assignee": body.get("assignee") or None,
            "tags": [str(t) for t in (body.get("tags") or [])],
            "created_at": now,
            "updated_at": now,
            "author": str(body.get("author") or "human"),
        }
        with DATA_LOCK:
            DATA["board"]["cards"].append(card)
        broadcast("board", {"at": now})
        self._send_json(201, {"card": card})

    def _create_command(self, body: dict) -> None:
        target = str(body.get("target_bot") or "").strip()
        text = str(body.get("text") or "").strip()
        if target not in DATA["bots"]:
            self._error(400, "unknown_bot", f"unknown bot: {target}")
            return
        if not text:
            self._error(400, "invalid_text", "text is required")
            return
        now = iso(datetime.now(timezone.utc))
        command = {
            "id": "cmd_" + _hex(f"cmd:{now}:{text}", 12),
            "target_bot": target,
            "text": text,
            "via": "feishu",
            "status": "sent",
            "message_id": "om_demo" + _hex(text, 6),
            "error": None,
            "created_at": now,
        }
        with DATA_LOCK:
            DATA["commands"].insert(0, command)
        broadcast("command", {"id": command["id"]})
        self._send_json(200, {"command": command})

    def do_PATCH(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/board/cards/"):
            self._error(404, "not_found", f"unknown endpoint: {path}")
            return
        if not self._authorized():
            return
        body = self._read_json()
        if body is None:
            return
        card_id = path[len("/api/board/cards/"):]
        with DATA_LOCK:
            card = next((c for c in DATA["board"]["cards"] if c["id"] == card_id), None)
            if card is None:
                self._error(404, "not_found", f"card not found: {card_id}")
                return
            if "column" in body and body["column"] not in COLUMN_IDS:
                self._error(400, "unknown_column", f"unknown column: {body['column']}")
                return
            for field in ("title", "desc", "column", "assignee", "tags"):
                if field in body:
                    card[field] = body[field]
            card["updated_at"] = iso(datetime.now(timezone.utc))
            result = copy.deepcopy(card)
        broadcast("board", {"at": result["updated_at"]})
        self._send_json(200, {"card": result})

    def do_DELETE(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/board/cards/"):
            self._error(404, "not_found", f"unknown endpoint: {path}")
            return
        if not self._authorized():
            return
        card_id = path[len("/api/board/cards/"):]
        with DATA_LOCK:
            before = len(DATA["board"]["cards"])
            DATA["board"]["cards"][:] = [c for c in DATA["board"]["cards"] if c["id"] != card_id]
            removed = before != len(DATA["board"]["cards"])
        if not removed:
            self._error(404, "not_found", f"card not found: {card_id}")
            return
        broadcast("board", {"at": iso(datetime.now(timezone.utc))})
        self._send_json(200, {"ok": True})


def _int_param(query: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int((query.get(name) or [str(default)])[0])
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock Context Bridge API + console (fictional data)")
    parser.add_argument("--host", default="localhost", help="bind host (default: localhost)")
    parser.add_argument("--port", type=int, default=8787, help="bind port (default: 8787)")
    parser.add_argument("--token", default="", help="require this bearer token on /api/* (tests 401 flow)")
    parser.add_argument("--no-fake-events", action="store_true", help="do not emit periodic fake sync events")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    server.daemon_threads = True
    server.token = args.token
    server.verbose = args.verbose
    if not args.no_fake_events:
        threading.Thread(target=fake_event_loop, daemon=True).start()
    host = "localhost" if args.host in ("localhost", "0.0.0.0") else args.host
    print(f"Context Bridge mock API on http://{host}:{server.server_address[1]}/")
    print(f"  {len(DATA['records_sorted'])} fictional records · {len(BOTS)} bots · board + commands + SSE")
    print("  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
