# MCP Fleet Gateway

把 N 个 MCP Server 收敛成**一个可运维的入口**。零第三方依赖，285 个测试，`python3 demo.py` 一条命令跑通全部五类事故处置。

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest          # 285 passed
python3 demo.py            # 12 个 Server / 94 个工具的完整演练
```

## 它解决什么

团队从 3 个 MCP Server 长到 12 个之后，Agent 开始出五种事故：

| 事故 | 现场 | 网关的解法 |
|---|---|---|
| 工具重名 | `search` 在 5 个 Server 里重名，模型随机挑一个 | `server__tool` 命名空间 + 冲突策略（拒绝/别名/优先级） |
| 上下文被吃光 | 94 个工具的清单 1,720 tokens，300 个工具时直接顶破预算 | 预算内裁剪 + Server 索引 + `gw_discover` 两阶段发现 |
| 危险操作裸奔 | 模型"顺手清理"执行 `DROP TABLE`，读 `../../etc/shadow` | 风险分级 + 参数守卫 + 人工审批（绑定实参指纹） |
| 单点拖垮全链路 | k8s Server 卡死，共享线程池被打满，其他 11 个也不可用 | 按 Server 熔断 + 半开探针 + 并发闸 + 超时预算 |
| 返回体过大 | 一次语义检索回 527KB，塞回上下文 | 字段投影 / 长串截断 / 分页句柄 |

## 一次调用的八道关卡

```
resolve → quarantine → validate → policy → approval → idempotency → admit → call → shape → audit
```

顺序是刻意的：

- **validate 在 policy 之前**：参数都不合法，就别去打扰人审批；
- **approval 在 admit 之前**：等待审批的请求不该占着 Server 的并发位；
- **shape 在 audit 之前**：审计要同时记录瘦身前后的字节数。

## 真实输出（`python3 demo.py` 节选）

```
已挂载 Server 12 个，工具 94 个，其中 read=53 write=28 destructive=13
重名工具：{'search': 5, 'get_issue': 2}

直接调 search      -> ok=False code=ambiguous_tool
候选：notion__search, slack__search, jira__search, email__search, vector__search

全量清单：94 个工具，约 1720 tokens
预算 600：12 个工具 + 82 个折叠，实测 561 tokens
压缩比：3.07x，且 12 个高优工具全部在位
模型追问 gw_discover(server='billing') -> 展开 6 个工具，只占 119 tokens

postgres__execute_sql  {'sql': 'DROP TABLE orders'}   -> policy_denied
files__read_file       {'path': '../../etc/shadow'}   -> policy_denied

扩容 10 副本           -> approval_required，工单 tk_25e6fcbc1b6574de
拿这张工单去关核心库    -> approval_invalid：实参与审批单不一致（参数指纹不匹配）
同一张工单再用一次      -> approval_invalid

k8s 第 3 次调用 -> transport_error  熔断器=open
k8s 第 4 次调用 -> circuit_open     熔断器=open
github__search_code      -> ok=True  （k8s 挂了不影响我）
探针结果 -> ok=True，熔断器=closed

原始返回 527,938 字节 → 4,368 字节，节省 99.2%
审计事件 12 条，字节 528,351 -> 4,781（省 99.1%）
```

## 模块地图

| 文件 | 职责 | 关键点 |
|---|---|---|
| `models.py` | 不可变数据模型 | 所有跨模块对象可 digest、可回放 |
| `catalog.py` | 注册与消歧 | 命名空间、冲突策略、**Schema 漂移检测**（Server 升级改参数就立刻隔离工具） |
| `manifest.py` | 工具清单注入 | token 估算、相关度排序、预算硬约束、两阶段发现 |
| `validation.py` | JSON Schema 子集校验 | 报错文案面向模型，带路径 |
| `policy.py` | 策略引擎 | DENY > APPROVAL > ALLOW；参数级守卫；守卫不可被通配放行规则意外绕过 |
| `approval.py` | 审批闸门 | 工单绑定「工具 + 实参指纹 + TTL」，一次性消费 |
| `breaker.py` | 故障隔离 | 滑动窗口、半开探针、指数退避、并发闸 |
| `budget.py` | 超时预算 | Deadline 逐跳传递 + 尝试次数账本 |
| `shaping.py` | 结果瘦身 | 投影 / 截断 / 分页（不透明一次性游标） |
| `audit.py` | 审计与回放 | JSONL sink、按 trace 回放 |
| `gateway.py` | 编排 | 十道关卡、幂等重放、统一错误码 |
| `demo_fleet.py` | 12 个假 Server | 假实现，真 schema、真风险等级、真返回体形状 |

## 几个不显眼但要命的细节

1. **`narrow()` 不能只在最外层过滤**。返回体通常是 `{"total": 40, "items": [{...}]}`，按顶层 key 过滤会把整个结果砍成 `{}`。必须区分「记录」和「容器」。
2. **审批单要绑参数指纹**。只校验 ticket 有效，等于让模型拿"批准了 A 的工单"去执行 B。
3. **守卫不能被通配放行绕过**。一条 `allow: *` 的只读规则会顺手放过 `../../etc/shadow`，只有显式 `override_guards=True` 才能越过。
4. **预算要把 footer 也算进去**。否则会出现"标称 600 token，实际 679"。
5. **`bool` 是 `int` 的子类**。校验 `integer` 时必须单独排除 `True/False`，否则 `limit=True` 会被放行。

## License

MIT
