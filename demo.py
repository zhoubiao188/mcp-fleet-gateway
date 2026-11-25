"""端到端演示：12 个 MCP Server、94 个工具，跑通 5 类真实事故的处置。

运行：
    python3 demo.py
"""
from __future__ import annotations

import json

from mcp_fleet_gateway import CallRequest, CircuitBreaker, ConflictStrategy
from mcp_fleet_gateway.demo_fleet import build_fleet
from mcp_fleet_gateway.manifest import estimate_tokens


class Clock:
    """演示用可控时钟，让熔断的冷却过程不用真的等 30 秒。"""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def hr(title: str) -> None:
    print("\n" + "─" * 78)
    print(f"▌{title}")
    print("─" * 78)


def main() -> None:
    TOOL_BUDGET = 600
    gw = build_fleet(budget_tokens=TOOL_BUDGET)

    # ---------------------------------------------------------------- 规模
    hr("0. 现状：一个中型团队的工具面")
    st = gw.stats()["catalog"]
    print(f"已挂载 Server {st['servers']} 个，工具 {st['tools']} 个，"
          f"其中 read={st['by_risk']['read']} write={st['by_risk']['write']} "
          f"destructive={st['by_risk']['destructive']}")
    print(f"重名工具：{ {k: len(v) for k, v in st['duplicate_names'].items()} }")
    for s in gw.catalog.servers()[:4]:
        print(f"  - {s.id:<9} {len(s.tools):>2} 个工具   {s.description}")
    print("  - ...")

    # ---------------------------------------------------------------- 事故 1
    hr("1. 工具重名：模型调 search，到底搜哪里？")
    r = gw.invoke(CallRequest(tool="search", arguments={"q": "支付超时"}))
    print(f"直接调 search      -> ok={r.ok} code={r.error_code}")
    print("候选：" + ", ".join(r.error_detail["candidates"]))
    r2 = gw.invoke(CallRequest(tool="notion__search", arguments={"q": "支付超时"}))
    print(f"调 notion__search  -> ok={r2.ok} 命中 {r2.content['total']} 条")

    # ---------------------------------------------------------------- 事故 2
    hr("2. 上下文预算：94 个工具一次性塞给模型要多少钱")
    full = gw.manifest(budget_tokens=100_000)
    print(f"全量清单：{len(full.tools)} 个工具，约 {full.est_tokens} tokens")
    staged = gw.manifest()
    print(f"预算 {TOOL_BUDGET}：{len(staged.tools)} 个工具 + {len(staged.omitted)} 个折叠，"
          f"实测 {staged.est_tokens} tokens")
    print(f"压缩比：{full.est_tokens / staged.est_tokens:.2f}x，"
          f"且 {len(staged.tools)} 个高优工具全部在位")
    print("\n模型实际看到的前 8 行：")
    for line in staged.text.splitlines()[:8]:
        print("   " + line)
    print("   ...")
    print(f"\n折叠索引示例：")
    for line in [l for l in staged.text.splitlines() if l.startswith("- ") and "个）" in l][:3]:
        print("   " + line)
    found = gw.discover("billing")
    print(f"\n模型追问 gw_discover(server='billing') -> 展开 {found['count']} 个工具，"
          f"只占 {found['tokens']} tokens")

    # 关键词检索也能把对的工具顶上来
    q = gw.manifest(query="退款 资金 refund")
    hit = [t for t in q.tools if "refund" in t]
    print(f"带 query 的清单：refund 相关工具被提升到前 {q.tools.index(hit[0]) + 1} 位"
          if hit else "带 query 的清单：未命中 refund")

    # ---------------------------------------------------------------- 事故 3
    hr("3. 危险操作：DROP TABLE 和越界路径")
    for tool, args in [
        ("postgres__execute_sql", {"sql": "DROP TABLE orders"}),
        ("postgres__execute_sql", {"sql": "DELETE FROM orders;"}),
        ("files__read_file", {"path": "../../config/prod.env"}),
    ]:
        res = gw.invoke(CallRequest(tool=tool, arguments=args))
        print(f"{tool:<22} {str(args):<42} -> {res.error_code}")

    print("\n同一个工具，换成一个正常 SQL 呢？")
    ok = gw.invoke(CallRequest(tool="postgres__explain",
                               arguments={"sql": "SELECT * FROM orders WHERE id=1"}))
    print(f"postgres__explain -> ok={ok.ok}")

    # ---------------------------------------------------------------- 事故 4
    hr("4. 审批闸门：批的是 A，执行的是 B")
    first = gw.invoke(CallRequest(tool="k8s__scale",
                                  arguments={"deployment": "pay-api", "replicas": 10}))
    print(f"扩容 10 副本           -> {first.error_code}，工单 {first.approval_ticket}")
    ticket = first.approval_ticket
    pending = gw.approvals.get(ticket)
    print(f"审批面板上的内容       -> {pending.tool}  {pending.summary}  "
          f"风险={pending.risk.label}  TTL={pending.ttl_ms // 1000}s")

    gw.approvals.approve(ticket, "sre-oncall")
    hijack = gw.invoke(CallRequest(tool="k8s__scale",
                                   arguments={"deployment": "core-db", "replicas": 0},
                                   approval_ticket=ticket))
    print(f"拿这张工单去关核心库    -> {hijack.error_code}：{hijack.error[:46]}…")

    done = gw.invoke(CallRequest(tool="k8s__scale",
                                 arguments={"deployment": "pay-api", "replicas": 10},
                                 approval_ticket=ticket))
    print(f"按审批内容执行          -> ok={done.ok} replicas={done.content['replicas']}")
    again = gw.invoke(CallRequest(tool="k8s__scale",
                                  arguments={"deployment": "pay-api", "replicas": 10},
                                  approval_ticket=ticket))
    print(f"同一张工单再用一次      -> {again.error_code}")

    # ---------------------------------------------------------------- 事故 5
    hr("5. 故障隔离：k8s 的进程卡死，别的 Server 还活着吗")
    clk = Clock()
    gwb = build_fleet(
        sleep_fn=lambda _s: None,
        max_attempts=1,
        now_fn=clk,
        breaker_factory=lambda sid: CircuitBreaker(
            sid, failure_threshold=3, min_samples=3, open_s=30, now_fn=clk
        ),
    )
    gwb.transports["k8s"].fail_next(20)
    for i in range(1, 5):
        res = gwb.invoke(CallRequest(tool="k8s__get_pods", arguments={"namespace": "prod"}))
        state = gwb.health()["k8s"]["state"]
        print(f"k8s 第 {i} 次调用 -> {res.error_code or 'ok':<16} 熔断器={state}")
    print()
    for tool, args in [
        ("github__search_code", {"q": "charge", "limit": 3}),
        ("notion__get_page", {"page_id": "pg_17"}),
        ("billing__query_invoices", {"month": "2026-11"}),
    ]:
        res = gwb.invoke(CallRequest(tool=tool, arguments=args))
        print(f"{tool:<24} -> ok={res.ok}  （k8s 挂了不影响我）")

    print("\n冷却 30s 后放一个探针：")
    clk.advance(31)
    gwb.transports["k8s"].heal()
    probe = gwb.invoke(CallRequest(tool="k8s__get_pods", arguments={"namespace": "prod"}))
    print(f"探针结果 -> ok={probe.ok}，熔断器={gwb.health()['k8s']['state']}")

    # ---------------------------------------------------------------- 事故 6
    hr("6. 返回体瘦身：一次语义检索烧掉半本书")
    big = gw.invoke(CallRequest(tool="vector__search", arguments={"query": "支付超时", "k": 40}))
    print(f"原始返回 {big.bytes_before:,} 字节（40 条 × 1536 维向量 + 无关字段）")
    print(f"网关处理后 {big.bytes_after:,} 字节，节省 {big.saved_ratio:.1%}，"
          f"截断={big.truncated} 句柄={big.cursor}")
    print(f"元信息：{json.dumps(big.content['_meta'], ensure_ascii=False)}")
    print(f"第一条记录只剩：{list(big.content['data']['matches'][0].keys())}")

    page = gw.fetch_page(big.cursor)
    payload = page.content["data"] if isinstance(page.content, dict) and "data" in page.content else page.content
    print(f"\n模型调 gw_page -> 取回 {len(payload)} 条，下一句柄={page.cursor}")
    expired = gw.fetch_page(big.cursor)
    print(f"同一个句柄再用一次 -> ok={expired.ok} code={expired.error_code}")

    # ---------------------------------------------------------------- 审计
    hr("7. 审计回放：这一次调用到底发生了什么")
    for row in gw.audit.replay(big.trace_id):
        print(json.dumps(row, ensure_ascii=False))

    denied = gw.invoke(CallRequest(tool="postgres__execute_sql",
                                   arguments={"sql": "TRUNCATE TABLE orders"}))
    print("\n拒绝事件同样留痕：")
    print(json.dumps(gw.audit.replay(denied.trace_id)[0], ensure_ascii=False))

    # ---------------------------------------------------------------- 汇总
    hr("8. 汇总")
    stats = gw.stats()
    c = stats["counters"]
    a = stats["audit"]
    print(f"调用 {c['calls']} 次：成功 {c['ok']}，拒绝 {c['denied']}，"
          f"待审批 {c['approval_required']}，审批后放行 {c['approval_used']}")
    print(f"幂等重放 {c['replays']} 次，触发重试 {c['retries']} 次，"
          f"结果被瘦身 {c['shaped']} 次")
    print(f"审计事件 {a['events']} 条，字节 {a['bytes_before']:,} -> {a['bytes_after']:,}"
          f"（省 {a['bytes_saved_ratio']:.1%}）")
    print("\n各 Server 健康度：")
    for sid, h in stats["health"].items():
        print(f"  {sid:<9} state={h['state']:<9} calls={h['calls']:<3} "
              f"fail={h['failures']:<3} inflight={h['inflight']}")


if __name__ == "__main__":
    main()
