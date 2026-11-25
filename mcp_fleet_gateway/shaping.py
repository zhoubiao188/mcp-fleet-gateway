"""结果瘦身：把 180KB 的检索结果压成 4KB，并给出翻页句柄。

最贵的 token 不是输入，是**工具返回**。
一次 search 返回 30 条、每条 12 个字段，其中模型真正需要的只有 title/url/snippet。
剩下 9 个字段（embedding 分数、内部 id、索引时间戳…）全都在烧钱，而且还稀释注意力。

三种手段，从便宜到贵：
  1. 投影（project）：按 output_fields 白名单砍字段 —— 零风险，先做这个；
  2. 截断（truncate）：超长字符串留头留尾 —— 对付日志、HTML 片段；
  3. 分页（paginate）：只给前 N 条，剩下的换一个 cursor，模型要就再取。
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from .models import ShapedResult
from .validation import narrow

_LIST_KEYS = ("items", "results", "data", "rows", "records", "matches", "hits", "documents")


@dataclass(frozen=True)
class ShapePolicy:
    max_bytes: int = 16_000
    max_items: int = 20
    max_string: int = 500
    project: tuple[str, ...] = ()
    max_depth: int = 8

    def merged(self, *, max_bytes: int | None = None, project: Sequence[str] = ()) -> "ShapePolicy":
        return replace(
            self,
            max_bytes=max_bytes or self.max_bytes,
            project=tuple(project) or self.project,
        )


def _json_bytes(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover
        return len(str(obj).encode("utf-8"))


def _truncate_strings(obj: Any, limit: int, depth: int = 0, max_depth: int = 8) -> Any:
    if depth > max_depth:
        return "…(结构过深已省略)"
    if isinstance(obj, str):
        if len(obj) <= limit:
            return obj
        head = int(limit * 0.7)
        tail = max(0, limit - head - 12)
        return f"{obj[:head]}…({len(obj) - head - tail} 字符省略)…{obj[-tail:] if tail else ''}"
    if isinstance(obj, list):
        return [_truncate_strings(v, limit, depth + 1, max_depth) for v in obj]
    if isinstance(obj, Mapping):
        return {k: _truncate_strings(v, limit, depth + 1, max_depth) for k, v in obj.items()}
    return obj


def _find_list(content: Any) -> tuple[str | None, list]:
    if isinstance(content, list):
        return None, content
    if isinstance(content, Mapping):
        for key in _LIST_KEYS:
            v = content.get(key)
            if isinstance(v, list) and v:
                return key, v
        for key, v in content.items():
            if isinstance(v, list) and v:
                return key, v
    return None, []


def _with_key(content: Any, key: str | None, items: list) -> Any:
    if key is None:
        return items
    return {**content, key: items}


class CursorStore:
    """翻页句柄。句柄不透明、一次性、带 TTL，避免模型自己拼 offset 乱翻。"""

    def __init__(self, *, capacity: int = 128) -> None:
        self.capacity = capacity
        self._pages: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def put(self, *, server_id: str, tool: str, items: list, total: int) -> str:
        cursor = "cur_" + secrets.token_hex(6)
        self._pages[cursor] = {
            "server_id": server_id,
            "tool": tool,
            "items": items,
            "total": total,
        }
        self._order.append(cursor)
        while len(self._order) > self.capacity:
            old = self._order.pop(0)
            self._pages.pop(old, None)
        return cursor

    def take(self, cursor: str) -> dict[str, Any] | None:
        return self._pages.pop(cursor, None)

    def __len__(self) -> int:
        return len(self._pages)


def shape(
    content: Any,
    policy: ShapePolicy,
    *,
    server_id: str = "",
    tool: str = "",
    store: CursorStore | None = None,
) -> ShapedResult:
    before = _json_bytes(content)

    # 1) 投影：最划算的一刀
    if policy.project:
        content = narrow(content, policy.project)
    projected = _json_bytes(content)

    # 2) 投影后已经在预算内、条数也不超标 —— 原样返回，不要"为了瘦身而瘦身"
    key, items = _find_list(content)
    if projected <= policy.max_bytes and len(items) <= policy.max_items:
        return ShapedResult(
            content=content, before=before, after=projected, truncated=projected != before
        )

    # 3) 长字符串截断
    content = _truncate_strings(content, policy.max_string, max_depth=policy.max_depth)
    key, items = _find_list(content)
    total = len(items)

    # 4) 分页
    cursor: str | None = None
    if total > policy.max_items:
        kept, rest = items[: policy.max_items], items[policy.max_items:]
        if store is not None:
            cursor = store.put(server_id=server_id, tool=tool, items=rest, total=total)
        content = _with_key(content, key, kept)
        meta: dict[str, Any] = {"total": total, "returned": len(kept)}
        if cursor:
            meta["next_cursor"] = cursor
            meta["note"] = (
                f"结果共 {total} 条，已返回前 {len(kept)} 条；"
                f"调 gw_page(cursor=...) 取下一页"
            )
        content = {"data": content, "_meta": meta}

    after = _json_bytes(content)

    # 5) 兜底：分页后还是超标，就只留样本 + 统计，别硬塞
    if after > policy.max_bytes:
        inner = content["data"] if isinstance(content, Mapping) and "data" in content else content
        _, inner_items = _find_list(inner)
        if inner_items:
            sample = inner_items[:3]
            meta = {
                "total": total or len(inner_items),
                "returned": len(sample),
                "dropped": "结果过大，已降级为样本；请缩小查询范围（加 filter / 减 limit）",
            }
            content = {"data": {"sample": sample}, "_meta": meta}
        else:
            preview = json.dumps(inner, ensure_ascii=False)[: max(60, policy.max_bytes // 3)]
            content = {
                "data": None,
                "_meta": {
                    "dropped": "结果超出预算且无法按条分页，仅保留预览；建议让工具支持字段过滤",
                    "preview": preview,
                },
            }
        if cursor:
            content["_meta"]["next_cursor"] = cursor
        after = _json_bytes(content)

    return ShapedResult(
        content=content,
        before=before,
        after=after,
        truncated=after != before,
        cursor=cursor,
    )


def page_from_cursor(
    store: CursorStore,
    cursor: str,
    policy: ShapePolicy,
) -> ShapedResult:
    page = store.take(cursor)
    if page is None:
        return ShapedResult(
            content={
                "error": "cursor_invalid",
                "message": "翻页句柄不存在或已过期（每个句柄只能使用一次）",
            },
            before=0,
            after=0,
        )
    return shape(
        page["items"],
        policy,
        server_id=page["server_id"],
        tool=page["tool"],
        store=store,
    )
