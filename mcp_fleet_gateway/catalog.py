"""工具目录：命名空间注册、消歧、Schema 漂移检测。

这一层解决的问题是"工具太多了"：
  - 12 个 MCP Server、280 个工具，其中 ``search`` / ``read_file`` / ``query``
    在 3 个 Server 里重名，模型只能靠猜。
  - Server 会在你不知情的时候升级：参数从 ``q`` 改成 ``query``，
    线上所有调用瞬间全部 -32602。
"""
from __future__ import annotations

import time
from dataclasses import replace
from enum import Enum
from typing import Any, Mapping

from .errors import AmbiguousTool, ToolNotFound, ToolQuarantined
from .models import (
    NAMESPACE_SEP,
    RegisterResult,
    SchemaDrift,
    ServerSpec,
    ToolSpec,
)


class ConflictStrategy(str, Enum):
    """裸名冲突时的处理策略。"""

    REJECT = "reject"      # 直接报错，逼调用方写全名（默认，最安全）
    ALIAS = "alias"        # 查别名表，查不到再报错
    PRIORITY = "priority"  # 按 Server 权重降序取第一个（能跑，但危险）


def _param_types(spec: Mapping[str, Any]) -> dict[str, Any]:
    props = (spec or {}).get("properties") or {}
    return {n: (v or {}).get("type") for n, v in props.items()}


def _enums(spec: Mapping[str, Any]) -> dict[str, set]:
    props = (spec or {}).get("properties") or {}
    out = {}
    for n, v in props.items():
        if isinstance(v, Mapping) and v.get("enum"):
            out[n] = set(v["enum"])
    return out


def classify_drift(old: ServerSpec, new: ServerSpec) -> list[SchemaDrift]:
    """对比新旧 Server 定义，找出会打断线上调用的变更。

    判定"破坏性"的标准只有一条：**会不会让昨天还能跑通的调用今天跑不通**。
    """
    drifts: list[SchemaDrift] = []
    old_tools = {t.name: t for t in old.tools}
    new_tools = {t.name: t for t in new.tools}

    for name in sorted(set(old_tools) - set(new_tools)):
        drifts.append(
            SchemaDrift(new.id, name, "tool_removed", "工具在升级后被移除", breaking=True)
        )

    for name in sorted(set(new_tools) - set(old_tools)):
        drifts.append(
            SchemaDrift(new.id, name, "tool_added", "新增工具", breaking=False)
        )

    for name in sorted(set(old_tools) & set(new_tools)):
        o, n = old_tools[name], new_tools[name]
        old_req = set((o.input_schema or {}).get("required", []) or [])
        new_req = set((n.input_schema or {}).get("required", []) or [])
        if new_req - old_req:
            drifts.append(
                SchemaDrift(
                    new.id,
                    name,
                    "required_added",
                    f"新增必填参数 {sorted(new_req - old_req)}",
                    breaking=True,
                )
            )
        old_types, new_types = _param_types(o.input_schema), _param_types(n.input_schema)
        for pname in sorted(set(old_types) & set(new_types)):
            if old_types[pname] != new_types[pname]:
                drifts.append(
                    SchemaDrift(
                        new.id,
                        name,
                        "type_changed",
                        f"参数 {pname} 类型 {old_types[pname]!r} -> {new_types[pname]!r}",
                        breaking=True,
                    )
                )
        for pname in sorted(set(old_types) - set(new_types)):
            drifts.append(
                SchemaDrift(
                    new.id,
                    name,
                    "param_removed",
                    f"参数 {pname} 被移除",
                    breaking=True,
                )
            )
        old_enum, new_enum = _enums(o.input_schema), _enums(n.input_schema)
        for pname in sorted(set(old_enum) & set(new_enum)):
            removed = old_enum[pname] - new_enum[pname]
            if removed:
                drifts.append(
                    SchemaDrift(
                        new.id,
                        name,
                        "enum_narrowed",
                        f"参数 {pname} 枚举收窄，移除了 {sorted(map(str, removed))}",
                        breaking=True,
                    )
                )
        if o.risk != n.risk:
            drifts.append(
                SchemaDrift(
                    new.id,
                    name,
                    "risk_changed",
                    f"风险等级 {o.risk.label} -> {n.risk.label}",
                    breaking=False,
                )
            )
    return drifts


class ToolCatalog:
    """注册中心 + 名称解析器。"""

    def __init__(
        self,
        *,
        separator: str = NAMESPACE_SEP,
        conflict_strategy: ConflictStrategy = ConflictStrategy.REJECT,
    ) -> None:
        self.separator = separator
        self.conflict_strategy = conflict_strategy
        self._servers: dict[str, ServerSpec] = {}
        self._tools: dict[str, ToolSpec] = {}
        self._by_bare: dict[str, list[str]] = {}
        self._aliases: dict[str, str] = {}
        self._order: list[str] = []
        self._drift: list[SchemaDrift] = []
        self._revision = 0

    # ---------------------------------------------------------------- 注册

    def register_server(self, spec: ServerSpec, *, replace_existing: bool = False) -> RegisterResult:
        if not spec.id:
            raise ValueError("server id 不能为空")
        if self.separator in spec.id:
            raise ValueError(f"server id 不能包含命名空间分隔符 {self.separator!r}")
        seen: set[str] = set()
        for t in spec.tools:
            if not t.name:
                raise ValueError(f"{spec.id}: 工具名不能为空")
            if self.separator in t.name:
                raise ValueError(f"{spec.id}/{t.name}: 工具名不能包含 {self.separator!r}")
            if t.name in seen:
                raise ValueError(f"{spec.id}: 工具名重复 {t.name!r}")
            seen.add(t.name)
            if t.server_id != spec.id:
                raise ValueError(f"{spec.id}: 工具 {t.name} 的 server_id 不一致（{t.server_id}）")

        old = self._servers.get(spec.id)
        if old is not None and not replace_existing:
            raise ValueError(f"server {spec.id!r} 已注册；如需更新请使用 replace_existing=True")

        drift: list[SchemaDrift] = []
        if old is not None:
            drift = classify_drift(old, spec)
            self._drift.extend(drift)
            # 破坏性变更 -> 隔离该工具，必须先由人确认再放行
            breaking = {(d.tool): d for d in drift if d.breaking}
            if breaking:
                spec = replace(
                    spec,
                    tools=tuple(
                        t.with_quarantine(f"{breaking[t.name].kind}: {breaking[t.name].detail}")
                        if t.name in breaking and old.tool(t.name) is not None
                        else t
                        for t in spec.tools
                    ),
                )

        # 清掉旧版本的索引
        if old is not None:
            for t in old.tools:
                self._tools.pop(t.qualified, None)
                bare = self._by_bare.get(t.name, [])
                if t.qualified in bare:
                    bare.remove(t.qualified)
                if not bare:
                    self._by_bare.pop(t.name, None)

        conflicts: list[tuple[str, tuple[str, ...]]] = []
        added: list[str] = []
        updated: list[str] = []
        for t in spec.tools:
            others = [q for q in self._by_bare.get(t.name, []) if not q.startswith(spec.id + self.separator)]
            if others:
                conflicts.append((t.name, tuple([t.qualified, *others])))
            self._tools[t.qualified] = t
            bucket = self._by_bare.setdefault(t.name, [])
            if t.qualified not in bucket:
                bucket.append(t.qualified)
            if old is not None and old.tool(t.name) is not None:
                updated.append(t.qualified)
            elif old is not None:
                added.append(t.qualified)

        if old is None:
            added = [t.qualified for t in spec.tools]
            self._order.append(spec.id)
        self._servers[spec.id] = spec
        self._revision += 1

        quarantined = tuple(t.qualified for t in spec.tools if t.quarantined and not (
            old is not None and old.tool(t.name) is not None and old.tool(t.name).quarantined
        ))
        return RegisterResult(
            server_id=spec.id,
            added=tuple(added),
            updated=tuple(updated),
            conflicts=tuple(conflicts),
            drift=tuple(drift),
            quarantine=quarantined,
        )

    def add_alias(self, bare: str, qualified: str) -> None:
        if self.separator in bare:
            raise ValueError(f"别名不能包含分隔符：{bare!r}")
        if qualified not in self._tools:
            raise ToolNotFound(f"别名目标不存在：{qualified}", candidates=list(self._tools)[:10])
        self._aliases[bare] = qualified

    # ---------------------------------------------------------------- 解析

    def resolve(self, name: str, server: str | None = None) -> ToolSpec:
        if not name:
            raise ToolNotFound("工具名为空")

        if self.separator in name:
            if name not in self._tools:
                raise ToolNotFound(f"工具不存在：{name}", hint=self._suggest(name))
            spec = self._tools[name]
            if server and spec.server_id != server:
                raise ToolNotFound(
                    f"工具 {name} 不属于 server {server!r}", actual_server=spec.server_id
                )
            return spec

        if server:
            qualified = f"{server}{self.separator}{name}"
            if qualified not in self._tools:
                raise ToolNotFound(
                    f"server {server!r} 下没有工具 {name!r}", hint=self._suggest(name)
                )
            return self._tools[qualified]

        candidates = self._by_bare.get(name, [])
        if not candidates:
            raise ToolNotFound(f"工具不存在：{name}", hint=self._suggest(name))
        if len(candidates) == 1:
            return self._tools[candidates[0]]

        if self.conflict_strategy is ConflictStrategy.ALIAS:
            target = self._aliases.get(name)
            if target and target in self._tools:
                return self._tools[target]
            raise AmbiguousTool(
                f"工具名 {name!r} 在多个 Server 中重名，且没有配置别名",
                candidates=list(candidates),
                suggestion=f"请改用 {candidates[0]!r} 这类全名，或配置别名",
            )
        if self.conflict_strategy is ConflictStrategy.PRIORITY:
            best = max(
                candidates,
                key=lambda q: (self._servers[self._tools[q].server_id].weight, -self._order.index(self._tools[q].server_id)),
            )
            return self._tools[best]
        raise AmbiguousTool(
            f"工具名 {name!r} 在 {len(candidates)} 个 Server 中重名",
            candidates=list(candidates),
            suggestion="请使用 server__tool 形式的全名",
        )

    def _suggest(self, name: str) -> str:
        from difflib import get_close_matches

        pool = list(self._tools) + list(self._by_bare)
        matches = get_close_matches(name, pool, n=3, cutoff=0.6)
        return ", ".join(matches) if matches else ""

    # ---------------------------------------------------------------- 查询

    def get(self, qualified: str) -> ToolSpec:
        spec = self._tools.get(qualified)
        if spec is None:
            raise ToolNotFound(f"工具不存在：{qualified}")
        return spec

    def ensure_usable(self, spec: ToolSpec) -> ToolSpec:
        if spec.quarantined:
            raise ToolQuarantined(
                f"{spec.qualified} 因 Schema 漂移被隔离：{spec.quarantine_reason}",
                suggestion="确认调用方已适配新签名后，调用 gw.acknowledge 解除隔离",
            )
        return spec

    def acknowledge(self, qualified: str) -> ToolSpec:
        spec = self.get(qualified)
        cleared = spec.cleared()
        self._tools[qualified] = cleared
        server = self._servers[spec.server_id]
        self._servers[spec.server_id] = replace(
            server,
            tools=tuple(cleared if t.name == spec.name else t for t in server.tools),
        )
        return cleared

    def all_tools(self, *, include_quarantined: bool = False) -> list[ToolSpec]:
        items = [self._tools[q] for q in self._tools]
        if not include_quarantined:
            items = [t for t in items if not t.quarantined]
        return items

    def server_tools(self, server_id: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.server_id == server_id and not t.quarantined]

    def servers(self) -> list[ServerSpec]:
        return [self._servers[s] for s in self._order]

    def server(self, server_id: str) -> ServerSpec:
        try:
            return self._servers[server_id]
        except KeyError:
            raise ToolNotFound(f"server 不存在：{server_id}", known=list(self._servers)) from None

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def drift_history(self) -> list[SchemaDrift]:
        return list(self._drift)

    def stats(self) -> dict[str, Any]:
        tools = self.all_tools(include_quarantined=True)
        by_risk: dict[str, int] = {}
        for t in tools:
            by_risk[t.risk.label] = by_risk.get(t.risk.label, 0) + 1
        dup = {k: v for k, v in self._by_bare.items() if len(v) > 1}
        return {
            "servers": len(self._servers),
            "tools": len(tools),
            "quarantined": sum(1 for t in tools if t.quarantined),
            "duplicate_names": dup,
            "by_risk": by_risk,
            "revision": self._revision,
        }
