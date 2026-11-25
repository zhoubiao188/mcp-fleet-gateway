"""策略引擎：决定"这个调用到底要不要放行"。

三条铁律：
1. **默认最小权限**：没声明风险等级的 Server 一律按 WRITE 处理；
2. **DENY 优先于 APPROVAL 优先于 ALLOW**：任何一条 deny 规则命中就否决，不给"另一条规则允许"翻盘的机会；
3. **参数级守卫**：只判断工具名没用。``db__execute_sql`` 本身很正当，
   危险的是它的参数里出现 ``DROP TABLE``。所以策略要能看到序列化后的实参。
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .models import CallRequest, Decision, PolicyDecision, Risk, ToolSpec

# 常见"模型自作主张"的危险参数形态。不是完备的 WAF，只是最后一根稻草。
DANGEROUS_ARG_PATTERNS: dict[str, str] = {
    "sql_drop": r"(?is)\b(drop|truncate)\s+(table|database|schema)\b",
    "sql_delete_all": r"(?is)\bdelete\s+from\s+[\w.\"`]+\s*(?:;|\"|'|$)",
    "sql_update_all": r"(?is)\bupdate\s+[\w.\"`]+\s+set\b(?![\s\S]*\bwhere\b)",
    "shell_rm_rf": r"(?s)\brm\s+-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)\b",
    "shell_fork_bomb": r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;\s*:",
    "path_traversal": r"(?:^|[/\\])\.\.(?:[/\\]|$)",
    "sensitive_path": r"(?i)(?:/etc/(?:shadow|passwd)|\.ssh/id_(?:rsa|ed25519)|\.aws/credentials)",
    "force_push": r"\bpush\s+(?:-f|--force)\b",
    "git_reset_hard": r"\breset\s+--hard\b",
}

_COMPILED = {k: re.compile(v) for k, v in DANGEROUS_ARG_PATTERNS.items()}


def args_text(arguments: Mapping[str, Any]) -> str:
    try:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 极端情况
        return str(arguments)


# 命中后直接拒绝的模式。其余（如 sql_update_all）只作为提示，不硬拦。
_HARD_GUARDS = frozenset(
    {
        "sql_drop",
        "sql_delete_all",
        "shell_rm_rf",
        "shell_fork_bomb",
        "path_traversal",
        "sensitive_path",
        "force_push",
        "git_reset_hard",
    }
)


def matched_guards(arguments: Mapping[str, Any]) -> list[str]:
    text = args_text(arguments)
    return [name for name, pat in _COMPILED.items() if pat.search(text)]


@dataclass(frozen=True)
class Rule:
    """一条策略。字符串字段支持 glob，便于 "给 staging 全放开"。"""

    name: str
    effect: Decision
    server: str | None = None
    tool: str | None = None
    min_risk: Risk | None = None
    max_risk: Risk | None = None
    tenants: tuple[str, ...] = ()
    arg_guards: tuple[str, ...] = ()
    override_guards: bool = False
    reason: str = ""

    def matches(self, req: CallRequest, spec: ToolSpec, guards: Sequence[str]) -> bool:
        if self.tenants and req.tenant not in self.tenants and "*" not in self.tenants:
            return False
        if self.server and not fnmatch.fnmatch(spec.server_id, self.server):
            return False
        if self.tool:
            if not (
                fnmatch.fnmatch(spec.name, self.tool)
                or fnmatch.fnmatch(spec.qualified, self.tool)
            ):
                return False
        if self.min_risk is not None and spec.risk < self.min_risk:
            return False
        if self.max_risk is not None and spec.risk > self.max_risk:
            return False
        if self.arg_guards and not any(g in guards for g in self.arg_guards):
            return False
        return True


@dataclass
class PolicyEngine:
    rules: list[Rule] = field(default_factory=list)
    allow_write_default: bool = True
    destructive_requires_approval: bool = True
    # 危险参数命中时是否直接拒绝（False 则降级为"需要审批"）
    hard_block_guards: bool = True

    def __post_init__(self) -> None:
        self.rules = list(self.rules)

    def add(self, rule: Rule) -> "PolicyEngine":
        self.rules.append(rule)
        return self

    def evaluate(self, req: CallRequest, spec: ToolSpec) -> PolicyDecision:
        guards = matched_guards(req.arguments)
        hits: list[Rule] = [r for r in self.rules if r.matches(req, spec, guards)]

        deny = [r for r in hits if r.effect is Decision.DENY]
        if deny:
            r = deny[0]
            return PolicyDecision(Decision.DENY, r.name, r.reason or "命中拒绝规则", spec.risk)

        # 参数守卫兜底：没有显式规则覆盖时，危险参数形态也拦
        guarded = [g for g in guards if g in _HARD_GUARDS]
        # 只有显式声明 override_guards 的规则才能越过安全守卫。
        # 否则一条"只读全放行"的通配规则会顺手把 rm -rf 也放过去。
        explicit_allow = any(r.effect is Decision.ALLOW for r in hits)
        guard_override = any(
            r.effect is Decision.ALLOW and r.override_guards for r in hits
        )
        if guarded and not guard_override:
            if self.hard_block_guards:
                return PolicyDecision(
                    Decision.DENY,
                    "arg_guard",
                    f"参数命中危险模式：{', '.join(guarded)}",
                    spec.risk,
                )
            return PolicyDecision(
                Decision.APPROVAL,
                "arg_guard",
                f"参数命中危险模式，需人工确认：{', '.join(guarded)}",
                spec.risk,
            )

        approval = [r for r in hits if r.effect is Decision.APPROVAL]
        if approval:
            r = approval[0]
            return PolicyDecision(Decision.APPROVAL, r.name, r.reason or "需要人工审批", spec.risk)

        if explicit_allow:
            r = [x for x in hits if x.effect is Decision.ALLOW][0]
            return PolicyDecision(Decision.ALLOW, r.name, r.reason or "显式放行", spec.risk)

        # 没有任何规则命中 -> 按风险等级走默认策略
        if spec.risk is Risk.READ:
            return PolicyDecision(Decision.ALLOW, "default:read", "只读工具默认放行", spec.risk)
        if spec.risk is Risk.WRITE:
            if self.allow_write_default:
                return PolicyDecision(Decision.ALLOW, "default:write", "写操作按默认策略放行", spec.risk)
            return PolicyDecision(Decision.DENY, "default:write", "未授权的写操作", spec.risk)
        if self.destructive_requires_approval:
            return PolicyDecision(
                Decision.APPROVAL, "default:destructive", "不可逆操作必须人工审批", spec.risk
            )
        return PolicyDecision(Decision.ALLOW, "default:destructive", "破坏性操作默认放行（不推荐）", spec.risk)

    # 常用预设 ------------------------------------------------------------

    @classmethod
    def readonly_tenant(cls, tenant: str) -> "PolicyEngine":
        """只读租户：把该租户所有写操作拒掉。"""
        return cls(
            rules=[
                Rule("readonly:block-write", Decision.DENY, tenants=(tenant,), min_risk=Risk.WRITE,
                     reason="只读租户不允许任何写操作"),
            ]
        )

    @classmethod
    def for_production(cls) -> "PolicyEngine":
        return cls(
            rules=[
                Rule("allow:read", Decision.ALLOW, max_risk=Risk.READ, reason="只读放行"),
                Rule("allow:write-non-prod", Decision.ALLOW, tenants=("staging", "*dev*"),
                     max_risk=Risk.WRITE, reason="非生产环境写操作放行"),
                Rule("approve:destructive", Decision.APPROVAL, min_risk=Risk.DESTRUCTIVE,
                     reason="不可逆操作需审批"),
            ],
            allow_write_default=False,
        )
