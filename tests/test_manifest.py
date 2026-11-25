import pytest

from helpers import make_server, make_tool

from mcp_fleet_gateway import ToolCatalog
from mcp_fleet_gateway.manifest import (
    build_manifest,
    estimate_tokens,
    render_tool_line,
    required_budget,
    score_tool,
)


def big_catalog(servers: int = 6, per: int = 5) -> ToolCatalog:
    cat = ToolCatalog()
    for i in range(servers):
        sid = f"s{i}"
        tools = tuple(
            make_tool(
                f"tool_{j}",
                sid,
                description="用于检索和处理业务数据的工具，支持过滤、排序与分页" * 2,
                tags=("search",) if j == 0 else (),
            )
            for j in range(per)
        )
        cat.register_server(make_server(sid, tools, description="某业务域服务", weight=100 - i * 5))
    return cat


class TestEstimate:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_ascii_is_cheaper_than_cjk(self):
        assert estimate_tokens("abcdefgh") < estimate_tokens("中文字符八个")

    def test_cjk_one_token_per_char(self):
        assert estimate_tokens("中文") >= 2

    def test_nonzero_for_tiny_input(self):
        assert estimate_tokens("a") >= 1

    def test_longer_text_costs_more(self):
        assert estimate_tokens("x" * 100) > estimate_tokens("x" * 10)


class TestRender:
    def test_optional_params_marked(self):
        t = make_tool("q", props={"a": {"type": "string"}, "b": {"type": "string"}},
                      required=("a",))
        line = render_tool_line(t)
        assert "a," in line and "b?" in line

    def test_risk_shown_only_when_not_read(self):
        assert "<write>" in render_tool_line(make_tool("w", risk=__import__(
            "mcp_fleet_gateway").Risk.WRITE))
        assert "<" not in render_tool_line(make_tool("r"))

    def test_long_description_truncated(self):
        t = make_tool("x", description="描" * 200)
        assert "…" in render_tool_line(t)

    def test_param_list_capped(self):
        props = {f"p{i}": {"type": "string"} for i in range(10)}
        line = render_tool_line(make_tool("many", props=props))
        assert "…" in line

    def test_qualified_name_present(self):
        assert "s9__ping" in render_tool_line(make_tool("ping", "s9"))


class TestScoring:
    def test_name_match_scores_higher(self):
        t1 = make_tool("refund", description="退款")
        t2 = make_tool("query", description="查询")
        assert score_tool(t1, "refund") > score_tool(t2, "refund")

    def test_tag_match(self):
        t = make_tool("x", description="无关", tags=("billing",))
        assert score_tool(t, "billing") > 0

    def test_no_query_no_score(self):
        assert score_tool(make_tool("x"), "") == 0.0

    def test_chinese_substring_match(self):
        t = make_tool("x", description="语义检索最相关的文档片段")
        assert score_tool(t, "语义检索") > 0


class TestBuildManifest:
    def test_empty_catalog(self):
        m = build_manifest(ToolCatalog(), budget_tokens=500)
        assert "尚未挂载" in m.text
        assert m.tools == ()

    def test_fits_budget_no_staging(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1", (make_tool("ping", "s1"),)))
        m = build_manifest(cat, budget_tokens=5_000)
        assert m.staged is False
        assert m.omitted == ()
        assert "s1__ping" in m.text

    def test_over_budget_stages(self):
        cat = big_catalog()
        m = build_manifest(cat, budget_tokens=500)
        assert m.staged is True
        assert 0 < len(m.tools) < 30
        assert len(m.omitted) > 0
        assert m.est_tokens <= 500

    def test_index_mentions_omitted_servers(self):
        cat = big_catalog()
        m = build_manifest(cat, budget_tokens=400)
        assert "gw_discover" in m.text
        assert "个）" in m.text

    def test_pinned_always_included(self):
        cat = ToolCatalog()
        tools = tuple(
            make_tool(f"t{i}", "s1", description="很长的工具描述" * 10, pinned=(i == 7))
            for i in range(12)
        )
        cat.register_server(make_server("s1", tools))
        m = build_manifest(cat, budget_tokens=80)
        assert "s1__t7" in m.tools

    def test_expanded_server_fully_included(self):
        cat = big_catalog()
        m = build_manifest(cat, budget_tokens=400, expand=("s5",))
        assert all(f"s5__tool_{j}" in m.tools for j in range(5))

    def test_query_promotes_relevant_tool(self):
        cat = ToolCatalog()
        tools = tuple(
            make_tool(f"t{i}", "s1", description="通用工具" * 12) for i in range(20)
        ) + (make_tool("refund_order", "s1", description="发起退款，涉及资金"),)
        cat.register_server(make_server("s1", tools))
        m = build_manifest(cat, budget_tokens=260, query="退款 refund")
        assert "s1__refund_order" in m.tools

    def test_servers_field_lists_included_servers(self):
        cat = big_catalog(servers=3, per=2)
        m = build_manifest(cat, budget_tokens=5_000)
        assert set(m.servers) == {"s0", "s1", "s2"}

    def test_required_budget_helper(self):
        t = make_tool("x", description="短")
        assert required_budget([t]) > 0

    def test_budget_zero_still_returns_something(self):
        cat = big_catalog(servers=1, per=3)
        m = build_manifest(cat, budget_tokens=0)
        assert m.text
        assert len(m.tools) >= 1


class TestGatewayManifest:
    def test_footer_lists_gateway_tools(self):
        from helpers import make_gateway

        gw = make_gateway()
        gw.add_server(make_server("s1", (make_tool("ping", "s1"),)))
        m = gw.manifest()
        assert "gw_page" in m.text and "gw_discover" in m.text

    def test_discover_returns_all_server_tools(self):
        from helpers import make_gateway

        gw = make_gateway()
        gw.add_server(make_server("s1", tuple(
            make_tool(f"t{i}", "s1", description="很长" * 30) for i in range(6)
        )))
        out = gw.discover("s1")
        assert out["count"] == 6
        assert len(out["tools"]) == 6
        assert out["server"] == "s1"

    def test_discover_unknown_server(self):
        from helpers import make_gateway
        from mcp_fleet_gateway import ToolNotFound

        gw = make_gateway()
        with pytest.raises(ToolNotFound):
            gw.discover("ghost")

    def test_custom_budget(self):
        from helpers import make_gateway

        gw = make_gateway(budget_tokens=5_000)
        gw.add_server(make_server("s1", (make_tool("ping", "s1"),)))
        assert gw.manifest(budget_tokens=100).est_tokens <= gw.manifest(budget_tokens=5000).est_tokens
