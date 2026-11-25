"""工具清单注入：把 280 个工具塞进 1200 token 的预算里。

问题：MCP 的惯例是"把所有工具定义一次性喂给模型"。40 个 Server、280 个工具，
光工具描述就要 2.4 万 token —— 还没开始干活，上下文先烧掉三成，
而且工具越多模型选错的概率越高（选择困难是真实的工程问题，不是玄学）。

解法：**两阶段发现**。
  阶段一：只给"按相关度排序的前 N 个工具" + 一句 Server 索引；
  阶段二：模型对某个 Server 感兴趣时，调 gw_discover 展开它。
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Sequence

from .catalog import ToolCatalog
from .models import Manifest, ToolSpec

_CJK = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")
_WORD = re.compile(r"[a-zA-Z0-9_]+")


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    中文按 1 字 ≈ 1 token，英文/符号按 3.6 字符 ≈ 1 token。
    这个系数比真实分词器保守（宁可少塞），因为超预算的代价比少一个工具大。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk + other / 3.6))


def _param_summary(spec: ToolSpec, limit: int = 4) -> str:
    props = (spec.input_schema or {}).get("properties") or {}
    required = set((spec.input_schema or {}).get("required", []) or [])
    names = []
    for name in props:
        names.append(name if name in required else f"{name}?")
    if len(names) > limit:
        names = names[:limit] + ["…"]
    return ", ".join(names)


def render_tool_line(spec: ToolSpec, *, with_risk: bool = True) -> str:
    desc = spec.description.strip().replace("\n", " ")
    if len(desc) > 60:
        desc = desc[:57] + "…"
    risk = f" <{spec.risk.label}>" if with_risk and spec.risk.value > 0 else ""
    params = _param_summary(spec)
    return f"- {spec.qualified}({params})：{desc}{risk}"


def _terms(query: str) -> list[str]:
    q = query.lower().strip()
    return [t for t in _WORD.findall(q) if len(t) > 1]


def score_tool(spec: ToolSpec, query: str) -> float:
    if not query:
        return 0.0
    terms = _terms(query)
    chunks = re.findall(r"[\u3400-\u9fff]{2,}", query)
    if not terms and not chunks:
        return 0.0
    hay_name = spec.qualified.lower()
    hay_desc = f"{spec.description} {' '.join(spec.tags)}".lower()
    score = 0.0
    for t in terms:
        if t in hay_name:
            score += 3.0
        if t in hay_desc:
            score += 1.5
        for tag in spec.tags:
            if t == tag.lower():
                score += 2.0
    # 中文没有空格，做一次子串命中
    for chunk in chunks:
        if chunk in spec.description:
            score += 3.0
        elif chunk[:2] in spec.description:
            score += 0.5
    return score


def build_manifest(
    catalog: ToolCatalog,
    *,
    budget_tokens: int = 1_200,
    query: str | None = None,
    expand: Sequence[str] = (),
    header: str = "## 可用工具（经 MCP Fleet Gateway 收敛）",
) -> Manifest:
    """在预算内生成工具清单，必要时降级为两阶段发现。"""
    tools = catalog.all_tools()
    if not tools:
        return Manifest(text=f"{header}\n（尚未挂载任何工具）", est_tokens=8)

    expanded = set(expand)
    pinned = [t for t in tools if t.pinned]
    pinned_q = {t.qualified for t in pinned}
    # "必须出现"的工具：显式置顶的 + 被 gw_discover 展开的 Server 的全部工具。
    # 它们不受预算裁剪，因为调用方已经明确要求看到它们。
    must_include = pinned_q | {t.qualified for t in tools if t.server_id in expanded}

    def rank(t: ToolSpec) -> tuple:
        in_expand = t.server_id in expanded
        return (
            0 if t.qualified in pinned_q else 1,
            0 if in_expand else 1,
            -score_tool(t, query or ""),
            -catalog.server(t.server_id).weight,
            t.qualified,
        )
    ordered = sorted(tools, key=rank)

    # 先算"全量"要多少 token，不超预算就别折腾
    all_text = "\n".join([header, *[render_tool_line(t) for t in ordered]])
    full_tokens = estimate_tokens(all_text)
    if full_tokens <= budget_tokens:
        return Manifest(
            text=all_text,
            tools=tuple(t.qualified for t in ordered),
            est_tokens=full_tokens,
            servers=tuple(dict.fromkeys(t.server_id for t in ordered)),
        )

    lines = [header]
    used = estimate_tokens(header)
    included: list[str] = []

    def index_line(sid: str, count: int) -> str:
        desc = catalog.server(sid).description.strip().replace("\n", " ")
        if len(desc) > 40:
            desc = desc[:37] + "…"
        return f"- {sid}（{count} 个）：{desc}"

    # 按"所有 Server 都要出现在索引里"的悲观情况预留，保证最终一定不超预算。
    # 预留多了会在后面被真实索引"退"回来，宁可保守。
    all_sids = list(dict.fromkeys(t.server_id for t in tools))
    reserve = sum(
        estimate_tokens(index_line(sid, sum(1 for x in tools if x.server_id == sid))) + 1
        for sid in all_sids
    )
    hard_cap = max(0, budget_tokens - estimate_tokens(header) - reserve)

    for t in ordered:
        line = render_tool_line(t)
        cost = estimate_tokens(line) + 1
        if used + cost > hard_cap and included and t.qualified not in must_include:
            break
        lines.append(line)
        included.append(t.qualified)
        used += cost

    included_set = set(included)
    omitted = [t for t in ordered if t.qualified not in included_set]

    # 未展开的 Server 用一行索引代替，模型可以按需 gw_discover
    by_server: dict[str, list[ToolSpec]] = {}
    for t in omitted:
        by_server.setdefault(t.server_id, []).append(t)

    if by_server:
        lines.append("")
        lines.append("## 未展开的 Server（调用 gw_discover(server=...) 查看其工具）")
        for sid, items in sorted(by_server.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(index_line(sid, len(items)))

    text = "\n".join(lines)
    server_order = [t.server_id for t in ordered if t.qualified in included_set]
    return Manifest(
        text=text,
        tools=tuple(included),
        est_tokens=estimate_tokens(text),
        omitted=tuple(t.qualified for t in omitted),
        staged=True,
        servers=tuple(dict.fromkeys(server_order)),
    )


def budget_headroom(manifest: Manifest, budget_tokens: int) -> int:
    return max(0, budget_tokens - manifest.est_tokens)


def coverage(manifest: Manifest, total: int) -> float:
    if total <= 0:
        return 1.0
    return len(manifest.tools) / total


def required_budget(tools: Iterable[ToolSpec], header_tokens: int = 10) -> int:
    return sum(estimate_tokens(render_tool_line(t)) + 1 for t in tools) + header_tokens


def ceil_div(a: int, b: int) -> int:  # pragma: no cover - 工具函数
    return math.ceil(a / b)
