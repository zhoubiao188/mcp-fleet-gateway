"""演示用的 12 个 MCP Server（40 个工具）。

这些 handler 是**假实现**，但 schema、风险等级、返回体形状都按真实场景设计：
  - ``vector__search`` 返回 40 条带 1536 维向量的文档（约 200KB），专门用来打结果瘦身；
  - ``postgres__execute_sql`` 是 DESTRUCTIVE，专门用来打审批闸门；
  - ``search`` 在 5 个 Server 里重名，专门用来打命名空间消歧；
  - ``k8s__scale`` 会随机变慢，专门用来打熔断。
"""
from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

from . import (
    ApprovalQueue,
    AuditLog,
    ConflictStrategy,
    FleetGateway,
    PolicyEngine,
    Risk,
    Rule,
    ServerGuard,  # noqa: F401  (重新导出，方便 demo 直接引用)
    ServerSpec,
    ShapePolicy,
    ToolSpec,
)
from .models import Decision

random.seed(20261125)

EMBED_DIM = 1536


def _schema(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
        "additionalProperties": False,
    }


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}


def tool(
    server: str,
    name: str,
    desc: str,
    props: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    risk: Risk = Risk.READ,
    output_fields: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    idempotent: bool = True,
    max_result_bytes: int = 16_000,
    timeout_ms: int = 5_000,
    pinned: bool = False,
) -> ToolSpec:
    return ToolSpec(
        server_id=server,
        name=name,
        description=desc,
        input_schema=_schema(props, required),
        output_fields=output_fields,
        risk=risk,
        idempotent=idempotent,
        timeout_ms=timeout_ms,
        max_result_bytes=max_result_bytes,
        tags=tags,
        pinned=pinned,
    )


# --------------------------------------------------------------------- 假数据

_CODE_HITS = [
    {
        "path": f"services/pay/src/handler_{i}.go",
        "line": 40 + i * 7,
        "snippet": "if err := charge(ctx, order); err != nil { return err }",
        "repo": "acme/pay",
        "branch": "main",
        "commit": f"{i:040x}"[:40],
        "author": "lin",
        "updated_at": "2026-11-20T08:12:33Z",
        "score": round(0.99 - i * 0.01, 3),
        "index": "code-v7",
        "language": "go",
        "size": 2048 + i,
    }
    for i in range(40)
]

_DOCS = [
    {
        "id": f"doc_{i:03d}",
        "title": f"支付超时排查手册 v{i + 1}",
        "url": f"https://wiki.acme.com/pay/timeout/{i}",
        "score": round(0.95 - i * 0.004, 4),
        "chunk": "第 %d 段：当上游返回 504 时，优先检查网关的 upstream_timeout 配置……" % i,
        "embedding": [round(random.uniform(-1, 1), 4) for _ in range(EMBED_DIM)],
        "index_ts": 1763000000 + i,
        "collection": "kb_v3",
        "rerank_score": round(0.99 - i * 0.002, 4),
    }
    for i in range(40)
]


# --------------------------------------------------------------------- handlers

def _search_code(q: str, repo: str | None = None, limit: int = 40) -> dict[str, Any]:
    items = [h for h in _CODE_HITS if repo is None or h["repo"] == repo][:limit]
    return {"total": len(items), "items": items}


def _vector_search(query: str, k: int = 40) -> dict[str, Any]:
    return {"query": query, "k": k, "matches": _DOCS[:k]}


def _execute_sql(sql: str, database: str = "app") -> dict[str, Any]:
    head = sql.strip().split()[0].lower()
    return {"database": database, "statement": head, "row_count": 1, "ok": True}


def _explain(sql: str, database: str = "app") -> dict[str, Any]:
    return {"database": database, "plan": "Seq Scan on orders (cost=0.00..431.00 rows=1000)"}


def _list_tables(database: str = "app") -> list[str]:
    return ["orders", "order_items", "payments", "users", "refunds"]


def _slow_queries(database: str = "app", min_ms: int = 500) -> dict[str, Any]:
    return {"database": database, "queries": [{"sql": "SELECT ...", "p95_ms": 1840}]}


def _read_file(path: str) -> dict[str, Any]:
    return {"path": path, "content": "package main\n" * 40}


def _write_file(path: str, content: str) -> dict[str, Any]:
    return {"path": path, "bytes": len(content.encode()), "written": True}


def _delete_file(path: str, recursive: bool = False) -> dict[str, Any]:
    return {"path": path, "deleted": True, "recursive": recursive}


def _ok(**kw: Any) -> dict[str, Any]:
    return {"ok": True, **kw}


def _search_generic(q: str, limit: int = 10) -> dict[str, Any]:
    return {
        "total": 3,
        "items": [
            {"id": f"x{i}", "title": f"{q} 相关条目 {i}", "url": f"https://example.com/{i}"}
            for i in range(min(limit, 3))
        ],
    }


def _pods(namespace: str = "prod") -> list[dict[str, Any]]:
    return [
        {"name": f"pay-api-{i}", "ns": namespace, "ready": "1/1", "restarts": i}
        for i in range(6)
    ]


def _logs(pod: str, lines: int = 100) -> dict[str, Any]:
    body = "\n".join(f"2026-11-25T03:{i:02d}:00Z INFO handled request id={i}" for i in range(lines))
    return {"pod": pod, "lines": lines, "log": body}


def _invoices(month: str = "2026-11") -> dict[str, Any]:
    return {"month": month, "amount_cny": 218_400, "invoices": [{"id": "inv_1", "amount": 120_000}]}


def _fetch(url: str) -> dict[str, Any]:
    return {"url": url, "status": 200, "html": "<html><body>" + "x" * 30_000 + "</body></html>"}


def _generic(**kw: Any) -> dict[str, Any]:
    """长尾工具的通用实现：真实项目里每个都有自己的逻辑，这里只为撑起规模。"""
    return {"ok": True, **kw}


# 长尾工具：真实团队里每个 Server 都有一堆"顺手也能用"的工具，
# 这些才是把上下文预算吃干净的主力。
_LONG_TAIL: dict[str, list[tuple[str, str, dict[str, Any], tuple[str, ...], Risk]]] = {
    "github": [
        ("get_file", "读取仓库文件原文", {"repo": S, "path": S}, ("repo", "path"), Risk.READ),
        ("list_branches", "列出分支", {"repo": S}, ("repo",), Risk.READ),
        ("list_commits", "列出提交记录", {"repo": S, "limit": I}, ("repo",), Risk.READ),
        ("create_branch", "创建分支", {"repo": S, "name": S}, ("repo", "name"), Risk.WRITE),
        ("close_issue", "关闭 issue", {"repo": S, "number": I}, ("repo", "number"), Risk.WRITE),
        ("add_label", "给 issue/PR 打标签", {"repo": S, "number": I, "label": S},
         ("repo", "number", "label"), Risk.WRITE),
        ("list_releases", "列出发布版本", {"repo": S}, ("repo",), Risk.READ),
    ],
    "postgres": [
        ("describe_table", "查看表结构", {"table": S}, ("table",), Risk.READ),
        ("table_stats", "表的行数/体积/膨胀率", {"table": S}, ("table",), Risk.READ),
        ("index_usage", "索引使用率", {"table": S}, ("table",), Risk.READ),
        ("kill_query", "杀掉长事务（会回滚）", {"pid": I}, ("pid",), Risk.DESTRUCTIVE),
        ("vacuum", "触发 VACUUM", {"table": S}, ("table",), Risk.WRITE),
    ],
    "files": [
        ("move_file", "移动/重命名文件", {"src": S, "dst": S}, ("src", "dst"), Risk.WRITE),
        ("copy_file", "复制文件", {"src": S, "dst": S}, ("src", "dst"), Risk.WRITE),
        ("stat", "文件元信息", {"path": S}, ("path",), Risk.READ),
        ("grep", "按正则检索文件内容", {"pattern": S, "path": S}, ("pattern",), Risk.READ),
        ("chmod", "修改权限", {"path": S, "mode": S}, ("path", "mode"), Risk.DESTRUCTIVE),
    ],
    "notion": [
        ("list_databases", "列出数据库", {}, (), Risk.READ),
        ("query_database", "按条件查数据库", {"db_id": S, "filter": S}, ("db_id",), Risk.READ),
        ("append_blocks", "追加内容块", {"page_id": S, "blocks": S}, ("page_id", "blocks"), Risk.WRITE),
        ("delete_page", "删除页面", {"page_id": S}, ("page_id",), Risk.DESTRUCTIVE),
    ],
    "slack": [
        ("list_users", "列出成员", {}, (), Risk.READ),
        ("get_thread", "读取消息串", {"channel": S, "ts": S}, ("channel", "ts"), Risk.READ),
        ("upload_file", "上传文件到频道", {"channel": S, "path": S}, ("channel", "path"), Risk.WRITE),
        ("add_reaction", "给消息加表情", {"channel": S, "ts": S, "emoji": S},
         ("channel", "ts", "emoji"), Risk.WRITE),
    ],
    "jira": [
        ("get_issue", "读取工单详情", {"key": S}, ("key",), Risk.READ),
        ("assign", "指派工单", {"key": S, "assignee": S}, ("key", "assignee"), Risk.WRITE),
        ("add_comment", "给工单加评论", {"key": S, "body": S}, ("key", "body"), Risk.WRITE),
        ("list_sprints", "列出迭代", {"board": S}, ("board",), Risk.READ),
    ],
    "sentry": [
        ("list_projects", "列出项目", {}, (), Risk.READ),
        ("get_issue", "读取异常详情", {"issue_id": S}, ("issue_id",), Risk.READ),
        ("ignore_issue", "忽略异常", {"issue_id": S}, ("issue_id",), Risk.WRITE),
        ("release_health", "版本健康度", {"version": S}, ("version",), Risk.READ),
    ],
    "k8s": [
        ("describe", "查看资源详情", {"kind": S, "name": S}, ("kind", "name"), Risk.READ),
        ("get_deployments", "列出 Deployment", {"namespace": S}, (), Risk.READ),
        ("get_events", "查看集群事件", {"namespace": S}, (), Risk.READ),
        ("exec", "在 Pod 内执行命令", {"pod": S, "cmd": S}, ("pod", "cmd"), Risk.DESTRUCTIVE),
        ("port_forward", "端口转发", {"pod": S, "port": I}, ("pod", "port"), Risk.WRITE),
        ("get_nodes", "列出节点与负载", {}, (), Risk.READ),
    ],
    "billing": [
        ("query_orders", "查询订单", {"order_id": S}, ("order_id",), Risk.READ),
        ("create_credit_note", "开红字发票", {"invoice_id": S, "amount": I},
         ("invoice_id", "amount"), Risk.DESTRUCTIVE),
        ("list_plans", "列出套餐", {}, (), Risk.READ),
        ("update_seat", "调整席位（涉钱）", {"account_id": S, "seats": I},
         ("account_id", "seats"), Risk.DESTRUCTIVE),
    ],
    "email": [
        ("get_message", "读取邮件正文", {"message_id": S}, ("message_id",), Risk.READ),
        ("reply", "回复邮件", {"message_id": S, "body": S}, ("message_id", "body"), Risk.WRITE),
        ("list_labels", "列出标签", {}, (), Risk.READ),
        ("mark_read", "标记已读", {"message_id": S}, ("message_id",), Risk.WRITE),
    ],
    "web": [
        ("extract_text", "抽取正文纯文本", {"url": S}, ("url",), Risk.READ),
        ("screenshot_full", "整页截图", {"url": S}, ("url",), Risk.READ),
        ("crawl", "抓取同站若干页", {"url": S, "depth": I}, ("url",), Risk.READ),
    ],
    "vector": [
        ("delete", "删除向量", {"id": S}, ("id",), Risk.DESTRUCTIVE),
        ("list_collections", "列出集合", {}, (), Risk.READ),
        ("get", "按 id 取向量", {"id": S}, ("id",), Risk.READ),
        ("reindex", "重建索引（耗时）", {"collection": S}, ("collection",), Risk.WRITE),
    ],
}


# --------------------------------------------------------------------- Server 定义

def build_fleet(
    *,
    policy: PolicyEngine | None = None,
    budget_tokens: int = 1_200,
    now_fn=None,
    breaker_factory=None,
    max_attempts: int = 2,
    sleep_fn=None,
    conflict_strategy: ConflictStrategy = ConflictStrategy.REJECT,
) -> FleetGateway:
    kwargs: dict[str, Any] = {"conflict_strategy": conflict_strategy}
    if now_fn is not None:
        kwargs["now_fn"] = now_fn
    gw = FleetGateway(
        policy=policy
        or PolicyEngine(
            rules=[
                Rule("allow:read", Decision.ALLOW, max_risk=Risk.READ, reason="只读放行"),
                Rule(
                    "deny:prod-sql-ddl",
                    Decision.DENY,
                    server="postgres",
                    tool="execute_sql",
                    tenants=("prod-agent",),
                    reason="生产库禁止 Agent 直接执行 SQL",
                ),
            ],
            allow_write_default=True,
        ),
        approvals=ApprovalQueue(**(dict(now_fn=now_fn) if now_fn else {})),
        audit=AuditLog(**(dict(now_fn=now_fn) if now_fn else {})),
        shape_policy=ShapePolicy(max_bytes=16_000, max_items=20, max_string=400),
        budget_tokens=budget_tokens,
        max_attempts=max_attempts,
        breaker_factory=breaker_factory,
        **({"sleep_fn": sleep_fn} if sleep_fn else {}),
        **kwargs,
    )

    github = ServerSpec(
        id="github",
        description="代码托管：检索代码、看 PR、提 PR、合并、评论",
        weight=100,
        tools=(
            tool("github", "search_code", "在仓库里检索代码片段", {"q": S, "repo": S, "limit": I},
                 ("q",), tags=("code", "search")),
            tool("github", "list_pull_requests", "列出 PR 及其评审状态",
                 {"repo": S, "state": {"type": "string", "enum": ["open", "closed", "all"]}},
                 ("repo",), tags=("pr",)),
            tool("github", "create_pull_request", "创建 PR（会推送分支）",
                 {"repo": S, "title": S, "head": S, "base": S}, ("repo", "title", "head", "base"),
                 risk=Risk.WRITE, idempotent=False),
            tool("github", "merge_pull_request", "合并 PR 到目标分支（不可逆）",
                 {"repo": S, "number": I}, ("repo", "number"),
                 risk=Risk.DESTRUCTIVE, idempotent=False),
            tool("github", "comment_issue", "在 issue 下发表评论",
                 {"repo": S, "number": I, "body": S}, ("repo", "number", "body"),
                 risk=Risk.WRITE, idempotent=False),
        ),
    )

    postgres = ServerSpec(
        id="postgres",
        description="生产库只读/受控 SQL：执行语句、看执行计划、慢查询",
        weight=110,
        tools=(
            tool("postgres", "execute_sql", "在指定库执行一条 SQL（含写操作）",
                 {"sql": S, "database": S}, ("sql",),
                 risk=Risk.DESTRUCTIVE, idempotent=False, tags=("sql",)),
            tool("postgres", "explain", "查看 SQL 执行计划", {"sql": S, "database": S}, ("sql",),
                 tags=("sql",)),
            tool("postgres", "list_tables", "列出库内的表", {"database": S}),
            tool("postgres", "slow_queries", "查最近的慢查询", {"database": S, "min_ms": I}),
        ),
    )

    files = ServerSpec(
        id="files",
        description="工作区文件：读、写、删、检索",
        weight=90,
        tools=(
            tool("files", "read_file", "读取文件内容", {"path": S}, ("path",), tags=("fs",)),
            tool("files", "write_file", "写入文件（覆盖）", {"path": S, "content": S},
                 ("path", "content"), risk=Risk.WRITE, idempotent=False, tags=("fs",)),
            tool("files", "delete_file", "删除文件或目录", {"path": S, "recursive": B}, ("path",),
                 risk=Risk.DESTRUCTIVE, idempotent=False, tags=("fs",)),
            tool("files", "search_files", "按内容检索文件", {"q": S, "glob": S}, ("q",), tags=("fs",)),
            tool("files", "list_dir", "列出目录", {"path": S}, ("path",)),
        ),
    )

    notion = ServerSpec(
        id="notion",
        description="团队知识库：页面检索与读写",
        weight=80,
        tools=(
            tool("notion", "search", "搜索知识库页面", {"q": S, "limit": I}, ("q",),
                 tags=("search", "kb")),
            tool("notion", "get_page", "读取页面正文", {"page_id": S}, ("page_id",)),
            tool("notion", "create_page", "新建页面", {"title": S, "parent": S, "body": S},
                 ("title", "parent"), risk=Risk.WRITE, idempotent=False),
            tool("notion", "update_page", "更新页面内容", {"page_id": S, "body": S},
                 ("page_id", "body"), risk=Risk.WRITE, idempotent=False),
        ),
    )

    slack = ServerSpec(
        id="slack",
        description="团队消息：检索历史消息、发消息",
        weight=70,
        tools=(
            tool("slack", "search", "检索频道历史消息", {"q": S, "limit": I}, ("q",), tags=("search",)),
            tool("slack", "post_message", "向频道发消息（会通知所有人）",
                 {"channel": S, "text": S}, ("channel", "text"),
                 risk=Risk.WRITE, idempotent=False),
            tool("slack", "list_channels", "列出频道", {}),
        ),
    )

    jira = ServerSpec(
        id="jira",
        description="研发工单：检索、创建、流转",
        weight=60,
        tools=(
            tool("jira", "search", "按 JQL 检索工单", {"q": S, "limit": I}, ("q",), tags=("search",)),
            tool("jira", "create_issue", "创建工单", {"project": S, "summary": S, "type": S},
                 ("project", "summary"), risk=Risk.WRITE, idempotent=False),
            tool("jira", "transition", "流转工单状态", {"key": S, "to": S}, ("key", "to"),
                 risk=Risk.WRITE, idempotent=False),
        ),
    )

    sentry = ServerSpec(
        id="sentry",
        description="错误监控：检索异常、看堆栈、标记已解决",
        weight=65,
        tools=(
            tool("sentry", "search_issues", "检索异常聚合（按频率/影响面）",
                 {"query": S, "limit": I}, ("query",), tags=("search", "error")),
            tool("sentry", "get_event", "取单条异常详情与堆栈", {"event_id": S}, ("event_id",)),
            tool("sentry", "resolve_issue", "标记异常为已解决", {"issue_id": S}, ("issue_id",),
                 risk=Risk.WRITE, idempotent=False),
        ),
    )

    k8s = ServerSpec(
        id="k8s",
        description="集群运维：看 Pod、拉日志、扩缩容、重启",
        weight=120,
        tools=(
            tool("k8s", "get_pods", "列出 Pod 状态", {"namespace": S}, tags=("ops",)),
            tool("k8s", "logs", "拉取 Pod 日志", {"pod": S, "lines": I}, ("pod",), tags=("ops",)),
            tool("k8s", "scale", "调整副本数（影响线上容量）",
                 {"deployment": S, "replicas": I}, ("deployment", "replicas"),
                 risk=Risk.DESTRUCTIVE, idempotent=False, tags=("ops",)),
            tool("k8s", "restart", "滚动重启（会中断连接）", {"deployment": S}, ("deployment",),
                 risk=Risk.DESTRUCTIVE, idempotent=False, tags=("ops",)),
        ),
    )

    billing = ServerSpec(
        id="billing",
        description="账单与退款：查询发票、发起退款",
        weight=50,
        tools=(
            tool("billing", "query_invoices", "查询某月账单", {"month": S}),
            tool("billing", "refund", "对订单发起退款（涉钱）", {"order_id": S, "amount": I},
                 ("order_id", "amount"), risk=Risk.DESTRUCTIVE, idempotent=False),
        ),
    )

    email = ServerSpec(
        id="email",
        description="邮件：检索、发送、归档",
        weight=40,
        tools=(
            tool("email", "search", "检索邮件", {"q": S, "limit": I}, ("q",), tags=("search",)),
            tool("email", "send", "发送邮件（对外）", {"to": S, "subject": S, "body": S},
                 ("to", "subject", "body"), risk=Risk.WRITE, idempotent=False),
            tool("email", "archive", "归档邮件", {"message_id": S}, ("message_id",),
                 risk=Risk.WRITE, idempotent=False),
        ),
    )

    web = ServerSpec(
        id="web",
        description="网页抓取与截图",
        weight=45,
        tools=(
            tool("web", "fetch_url", "抓取网页（返回 HTML）", {"url": S}, ("url",),
                 max_result_bytes=4_000),
            tool("web", "screenshot", "网页截图", {"url": S, "width": I}, ("url",)),
        ),
    )

    vector = ServerSpec(
        id="vector",
        description="向量检索：语义召回知识库片段（返回体很大）",
        weight=85,
        tools=(
            tool("vector", "search", "语义检索最相关的文档片段", {"query": S, "k": I}, ("query",),
                 output_fields=("title", "url", "score", "chunk"),
                 max_result_bytes=6_000, tags=("search", "rag")),
            tool("vector", "upsert", "写入/更新向量", {"id": S, "text": S}, ("id", "text"),
                 risk=Risk.WRITE, idempotent=False),
        ),
    )

    servers = [
        (github, {
            "search_code": _search_code,
            "list_pull_requests": lambda repo, state="open": _ok(
                repo=repo, state=state, items=[{"number": 481, "title": "fix: 超时重试"}]),
            "create_pull_request": lambda repo, title, head, base: _ok(
                repo=repo, number=482, url=f"https://github.com/{repo}/pull/482"),
            "merge_pull_request": lambda repo, number: _ok(repo=repo, number=number, merged=True),
            "comment_issue": lambda repo, number, body: _ok(repo=repo, number=number),
        }),
        (postgres, {
            "execute_sql": _execute_sql,
            "explain": _explain,
            "list_tables": _list_tables,
            "slow_queries": _slow_queries,
        }),
        (files, {
            "read_file": _read_file,
            "write_file": _write_file,
            "delete_file": _delete_file,
            "search_files": lambda q, glob="**/*": _search_generic(f"{q} {glob}"),
            "list_dir": lambda path: {"path": path, "entries": ["src", "tests", "README.md"]},
        }),
        (notion, {
            "search": _search_generic,
            "get_page": lambda page_id: {"page_id": page_id, "title": "支付超时排查手册"},
            "create_page": lambda title, parent, body="": _ok(page_id="pg_new", title=title),
            "update_page": lambda page_id, body: _ok(page_id=page_id, bytes=len(body)),
        }),
        (slack, {
            "search": _search_generic,
            "post_message": lambda channel, text: _ok(channel=channel, ts="1763000000.1"),
            "list_channels": lambda: ["#incident", "#pay", "#random"],
        }),
        (jira, {
            "search": _search_generic,
            "create_issue": lambda project, summary, type="Task": _ok(key=f"{project}-1024"),
            "transition": lambda key, to: _ok(key=key, status=to),
        }),
        (sentry, {
            "search_issues": lambda query, limit=10: {"total": 2, "items": [
                {"id": "E1", "title": "TimeoutError: upstream 504", "events": 1841},
                {"id": "E2", "title": "ConnectionResetError", "events": 233},
            ]},
            "get_event": lambda event_id: {"event_id": event_id, "stack": ["charge()", "retry()"]},
            "resolve_issue": lambda issue_id: _ok(issue_id=issue_id, status="resolved"),
        }),
        (k8s, {
            "get_pods": _pods,
            "logs": _logs,
            "scale": lambda deployment, replicas: _ok(deployment=deployment, replicas=replicas),
            "restart": lambda deployment: _ok(deployment=deployment, restarted=True),
        }),
        (billing, {
            "query_invoices": _invoices,
            "refund": lambda order_id, amount: _ok(order_id=order_id, amount=amount),
        }),
        (email, {
            "search": _search_generic,
            "send": lambda to, subject, body: _ok(to=to, subject=subject),
            "archive": lambda message_id: _ok(message_id=message_id, archived=True),
        }),
        (web, {
            "fetch_url": _fetch,
            "screenshot": lambda url, width=1280: _ok(url=url, width=width, bytes=84_213),
        }),
        (vector, {
            "search": _vector_search,
            "upsert": lambda id, text: _ok(id=id, dims=EMBED_DIM),
        }),
    ]

    for spec, handlers in servers:
        extra = _LONG_TAIL.get(spec.id, [])
        if extra:
            spec = replace(
                spec,
                tools=spec.tools
                + tuple(
                    tool(spec.id, name, desc, props, required, risk=risk)
                    for name, desc, props, required, risk in extra
                ),
            )
            for name, *_rest in extra:
                handlers.setdefault(name, _generic)
        latency = 15.0 if spec.id == "k8s" else 2.0
        gw.add_server(spec, handlers=handlers, latency_ms=latency)

    return gw
