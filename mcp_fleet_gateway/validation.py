"""一个够用的 JSON Schema 子集校验器。

为什么不用 jsonschema 库：
1. 网关要被嵌进 Agent 进程里，依赖越少越好（本项目零第三方依赖）；
2. 我们只关心"模型会不会把参数传错"这一类问题，不需要支持 $ref / oneOf / 复杂组合；
3. 报错信息要能被模型读懂，所以自己控制文案。

支持的子集：type / required / properties / additionalProperties / enum / pattern /
minimum / maximum / minLength / maxLength / minItems / maxItems / items。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        # bool 是 int 的子类，必须单独排除，否则 True 会被当成合法整数
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    py = _TYPE_MAP.get(expected)
    if py is None:
        return True  # 不认识的类型不做限制，避免误杀
    return isinstance(value, py)


def validate(schema: Mapping[str, Any] | None, value: Any, path: str = "$") -> list[str]:
    """返回错误列表，空列表代表通过。错误文案面向模型，带路径。"""
    if not schema:
        return []
    errors: list[str] = []
    _validate_node(schema, value, path, errors)
    return errors


def _validate_node(schema: Mapping[str, Any], value: Any, path: str, errors: list[str]) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_type_ok(value, t) for t in expected):
            errors.append(f"{path}: 期望类型 {'/'.join(expected)}，实际 {type(value).__name__}")
            return
    elif isinstance(expected, str):
        if not _type_ok(value, expected):
            errors.append(f"{path}: 期望类型 {expected}，实际 {type(value).__name__}")
            return

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"][:8])
        errors.append(f"{path}: 取值不在枚举内（允许：{allowed}）")

    if isinstance(value, str):
        if (ml := schema.get("minLength")) is not None and len(value) < ml:
            errors.append(f"{path}: 长度不能小于 {ml}")
        if (xl := schema.get("maxLength")) is not None and len(value) > xl:
            errors.append(f"{path}: 长度不能大于 {xl}")
        if (pat := schema.get("pattern")) is not None:
            try:
                if re.search(pat, value) is None:
                    errors.append(f"{path}: 不匹配模式 {pat!r}")
            except re.error:  # pragma: no cover - 非法正则视为不限制
                pass

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if (mn := schema.get("minimum")) is not None and value < mn:
            errors.append(f"{path}: 不能小于 {mn}")
        if (mx := schema.get("maximum")) is not None and value > mx:
            errors.append(f"{path}: 不能大于 {mx}")

    if isinstance(value, list):
        if (mn := schema.get("minItems")) is not None and len(value) < mn:
            errors.append(f"{path}: 元素数量不能少于 {mn}")
        if (mx := schema.get("maxItems")) is not None and len(value) > mx:
            errors.append(f"{path}: 元素数量不能多于 {mx}")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for i, item in enumerate(value):
                _validate_node(item_schema, item, f"{path}[{i}]", errors)

    if isinstance(value, Mapping):
        props = schema.get("properties") or {}
        for name in schema.get("required", []) or []:
            if name not in value:
                errors.append(f"{path}: 缺少必填参数 {name!r}")
        for name, sub in props.items():
            if name in value:
                _validate_node(sub, value[name], f"{path}.{name}", errors)
        extra = schema.get("additionalProperties", True)
        if extra is False:
            for name in value:
                if name not in props:
                    errors.append(f"{path}: 不允许的参数 {name!r}（可用：{', '.join(sorted(props)) or '无'}）")


def narrow(value: Any, fields: Sequence[str]) -> Any:
    """按白名单投影。

    难点在于返回体常常是"容器 + 记录列表"：``{"total": 40, "items": [{...}, ...]}``。
    如果只在最外层按 key 过滤，容器会被砍成 ``{}``，等于把结果删了。
    所以这里区分两种 Mapping：
      - 记录（自身含有白名单字段）→ 直接投影；
      - 容器（不含任何白名单字段）→ 保留标量元信息，对内部的记录递归投影。
    """
    keep = set(fields)
    if not keep:
        return value

    def _walk(v: Any) -> Any:
        if isinstance(v, list):
            return [_walk(i) for i in v]
        if isinstance(v, Mapping):
            if any(k in keep for k in v):
                return {k: val for k, val in v.items() if k in keep}
            return {
                k: (_walk(val) if isinstance(val, (list, Mapping)) else val)
                for k, val in v.items()
            }
        return v

    return _walk(value)
