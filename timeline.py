"""一条 trace 走完十道关卡：真实的排障任务时间线。

场景：告警说"支付接口 P99 从 300ms 涨到 2s"，Agent 要在一次任务里
定位原因、并尝试恢复。10 次工具调用里会撞上：清单预算、结果截断、
分页、审批、策略拒绝——全都在同一条 trace 里。

运行：
    python3 timeline.py
"""
from __future__ import annotations

import json
import time

from mcp_fleet_gateway import CallRequest
from mcp_fleet_gateway.demo_fleet import build_fleet
from mcp_fleet_gateway.manifest import estimate_tokens

TRACE = "tr_pay_p99_20261125"


def _tokens(payload) -> int:
    """粗略估算这段返回体注入上下文要多少 token。"""
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    return estimate_tokens(text)


def main() -> None:
    gw = build_fleet(budget_tokens=600)

    # 第一步不是调工具，是决定"给模型看哪些工具"
    manifest = gw.manifest(query="支付 超时 延迟 数据库 发布")
    print(f"本轮工具清单：{len(manifest.tools)} 个工具 / {manifest.est_tokens} tokens"
          f"（全量清单需 {gw.manifest(budget_tokens=100_000).est_tokens} tokens）")

    steps: list[tuple[str, dict, str]] = [
        ("sentry__search_issues", {"query": "payment timeout", "limit": 5}, ""),
        ("k8s__get_deployments", {"namespace": "prod"}, ""),
        ("k8s__logs", {"pod": "pay-api-7d9f2-xk4", "lines": 2000}, ""),
        ("postgres__slow_queries", {"database": "orders", "min_ms": 500}, ""),
        ("vector__search", {"query": "支付接口 P99 升高 历史复盘", "k": 40}, ""),
        ("k8s__restart", {"deployment": "pay-api"}, ""),
        ("postgres__execute_sql", {"sql": "DROP TABLE tmp_orders_0921"}, ""),
    ]

    rows: list[dict] = []
    step_no = 0
    cursor = None

    for tool, args, _ in steps:
        step_no += 1
        res = gw.invoke(CallRequest(tool=tool, arguments=args, trace_id=TRACE,
                                    deadline_ms=8_000))
        rows.append({
            "step": step_no,
            "tool": res.tool,
            "outcome": "ok" if res.ok else res.error_code,
            "ms": res.elapsed_ms,
            "bytes": f"{res.bytes_before}->{res.bytes_after}",
            "tokens": _tokens(res.content) if res.ok else 0,
        })
        if res.cursor:
            cursor = res.cursor
        # 审批：拿到工单 -> 人批 -> 带 ticket 重试（同一步的第二次尝试）
        if res.error_code == "approval_required" and res.approval_ticket:
            gw.approvals.approve(res.approval_ticket, "sre-oncall")
            retry = gw.invoke(CallRequest(tool=tool, arguments=args, trace_id=TRACE,
                                          approval_ticket=res.approval_ticket))
            rows.append({
                "step": "6b",
                "tool": retry.tool,
                "outcome": "ok(approved)" if retry.ok else retry.error_code,
                "ms": retry.elapsed_ms,
                "bytes": f"{retry.bytes_before}->{retry.bytes_after}",
                "tokens": _tokens(retry.content) if retry.ok else 0,
            })

    # 分页：527KB 变成"前 20 条 + 一个句柄"，模型按需再取
    if cursor:
        step_no += 1
        t0 = time.perf_counter()
        page = gw.fetch_page(cursor)
        page_ms = (time.perf_counter() - t0) * 1000
        rows.append({
            "step": step_no,
            "tool": "gw_page",
            "outcome": "ok" if page.ok else page.error_code,
            "ms": page_ms,
            "bytes": f"{page.bytes_before}->{page.bytes_after}",
            "tokens": _tokens(page.content),
        })

    def w(text: str) -> int:
        """终端显示宽度：CJK 占两列。"""
        return sum(2 if ord(c) > 0x2E80 else 1 for c in text)

    def pad(text: str, width: int, right: bool = False) -> str:
        text = str(text)
        space = " " * max(0, width - w(text))
        return space + text if right else text + space

    line = "─" * 84
    print("\n" + line)
    print(pad("步", 5) + pad("工具", 26) + pad("结果", 16)
          + pad("耗时ms", 9, True) + pad("返回字节", 18, True) + pad("注入tok", 9, True))
    print(line)
    for r in rows:
        print(pad(r["step"], 5) + pad(r["tool"], 26) + pad(r["outcome"], 16)
              + pad(f"{r['ms']:.1f}", 9, True) + pad(r["bytes"], 18, True)
              + pad(f"{r['tokens']:,}", 9, True))
    print(line)

    # 账目：这一条 trace 到底花了多少
    events = gw.audit.replay(TRACE)
    raw_bytes = sum(item for r in rows for item in [int(r["bytes"].split("->")[0])])
    kept_bytes = sum(int(r["bytes"].split("->")[1]) for r in rows)
    injected = manifest.est_tokens * len(rows) + sum(r["tokens"] for r in rows)
    full_manifest = gw.manifest(budget_tokens=100_000).est_tokens
    print(f"\n审计事件 {len(events)} 条，全部挂在 {TRACE} 下")
    print(f"工具清单：全量注入 {full_manifest} × {len(rows)} 轮 = {full_manifest * len(rows)} tokens"
          f"  ->  预算裁剪后 {manifest.est_tokens} × {len(rows)} 轮 = "
          f"{manifest.est_tokens * len(rows)} tokens")
    print(f"返回体：原始合计 {raw_bytes:,} 字节（原样注入约 {raw_bytes / 3.6:,.0f} tokens）"
          f"  ->  实际保留 {kept_bytes:,} 字节 / {sum(r['tokens'] for r in rows):,} tokens")
    print(f"本次任务上下文总计约 {injected:,} tokens")

    print("\n按 trace 回放（只取关键字段）：")
    for e in events:
        print(f"  {e['tool']:<26} {e['decision']:<9} {e['rule']:<20} "
              f"{e['error_code'] or 'ok':<17} {e['args']}")

    print("\n各 Server 健康度：")
    for sid, h in gw.health().items():
        if h["calls"]:
            print(f"  {sid:<9} state={h['state']:<9} calls={h['calls']} "
                  f"fail={h['failures']} failure_rate={h['failure_rate']}")


if __name__ == "__main__":
    main()
