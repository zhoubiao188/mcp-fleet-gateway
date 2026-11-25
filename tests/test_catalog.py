import pytest

from helpers import make_server, make_tool

from mcp_fleet_gateway import (
    AmbiguousTool,
    ConflictStrategy,
    Risk,
    ToolCatalog,
    ToolNotFound,
    ToolQuarantined,
)


def two_conflicting_servers(**weights):
    a = make_server("alpha", (make_tool("search", "alpha"), make_tool("ping", "alpha")),
                    weight=weights.get("alpha", 100))
    b = make_server("beta", (make_tool("search", "beta"), make_tool("read", "beta")),
                    weight=weights.get("beta", 100))
    return a, b


class TestRegister:
    def test_register_indexes_tools(self):
        cat = ToolCatalog()
        res = cat.register_server(make_server("s1", (make_tool("ping", "s1"),)))
        assert res.added == ("s1__ping",)
        assert cat.get("s1__ping").name == "ping"

    def test_empty_server_id_rejected(self):
        with pytest.raises(ValueError):
            ToolCatalog().register_server(make_server(""))

    def test_separator_in_server_id_rejected(self):
        with pytest.raises(ValueError):
            ToolCatalog().register_server(make_server("a__b"))

    def test_separator_in_tool_name_rejected(self):
        spec = make_server("s1", (make_tool("a__b", "s1"),))
        with pytest.raises(ValueError):
            ToolCatalog().register_server(spec)

    def test_duplicate_tool_name_rejected(self):
        t = make_tool("ping", "s1")
        with pytest.raises(ValueError) as ei:
            ToolCatalog().register_server(make_server("s1", (t, t)))
        assert "重复" in str(ei.value)

    def test_server_id_mismatch_rejected(self):
        spec = make_server("s1", (make_tool("ping", "other"),))
        with pytest.raises(ValueError) as ei:
            ToolCatalog().register_server(spec)
        assert "不一致" in str(ei.value)

    def test_double_register_requires_flag(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1"))
        with pytest.raises(ValueError):
            cat.register_server(make_server("s1"))

    def test_replace_existing_updates_tools(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1", (make_tool("ping", "s1"),)))
        spec = make_server("s1", (make_tool("ping", "s1"), make_tool("pong", "s1")))
        res = cat.register_server(spec, replace_existing=True)
        assert "s1__pong" in res.added
        assert "s1__ping" in res.updated

    def test_revision_increments(self):
        cat = ToolCatalog()
        assert cat.revision == 0
        cat.register_server(make_server("s1"))
        cat.register_server(make_server("s2"))
        assert cat.revision == 2


class TestResolve:
    def test_qualified_name(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1", (make_tool("ping", "s1"),)))
        assert cat.resolve("s1__ping").qualified == "s1__ping"

    def test_unique_bare_name(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1", (make_tool("ping", "s1"),)))
        assert cat.resolve("ping").qualified == "s1__ping"

    def test_bare_name_with_server_hint(self):
        a, b = two_conflicting_servers()
        cat = ToolCatalog()
        cat.register_server(a)
        cat.register_server(b)
        assert cat.resolve("search", server="beta").qualified == "beta__search"

    def test_bare_name_ambiguous_rejected(self):
        a, b = two_conflicting_servers()
        cat = ToolCatalog()
        cat.register_server(a)
        cat.register_server(b)
        with pytest.raises(AmbiguousTool) as ei:
            cat.resolve("search")
        assert len(ei.value.detail["candidates"]) == 2
        assert "server__tool" in ei.value.detail["suggestion"]

    def test_qualified_name_with_wrong_hint(self):
        a, b = two_conflicting_servers()
        cat = ToolCatalog()
        cat.register_server(a)
        cat.register_server(b)
        with pytest.raises(ToolNotFound):
            cat.resolve("alpha__search", server="beta")

    def test_hint_server_without_tool(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1", (make_tool("ping", "s1"),)))
        with pytest.raises(ToolNotFound):
            cat.resolve("nope", server="s1")

    def test_missing_tool_suggests_similar(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1", (make_tool("search_code", "s1"),)))
        with pytest.raises(ToolNotFound) as ei:
            cat.resolve("search_cod")
        assert "search_code" in ei.value.detail.get("hint", "")

    def test_empty_name(self):
        with pytest.raises(ToolNotFound):
            ToolCatalog().resolve("")

    def test_priority_strategy_picks_heaviest(self):
        a, b = two_conflicting_servers(alpha=50, beta=200)
        cat = ToolCatalog(conflict_strategy=ConflictStrategy.PRIORITY)
        cat.register_server(a)
        cat.register_server(b)
        assert cat.resolve("search").qualified == "beta__search"

    def test_alias_strategy(self):
        a, b = two_conflicting_servers()
        cat = ToolCatalog(conflict_strategy=ConflictStrategy.ALIAS)
        cat.register_server(a)
        cat.register_server(b)
        cat.add_alias("search", "beta__search")
        assert cat.resolve("search").qualified == "beta__search"

    def test_alias_strategy_without_alias_raises(self):
        a, b = two_conflicting_servers()
        cat = ToolCatalog(conflict_strategy=ConflictStrategy.ALIAS)
        cat.register_server(a)
        cat.register_server(b)
        with pytest.raises(AmbiguousTool):
            cat.resolve("search")

    def test_alias_target_must_exist(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1"))
        with pytest.raises(ToolNotFound):
            cat.add_alias("search", "s1__ghost")

    def test_alias_cannot_contain_separator(self):
        cat = ToolCatalog()
        cat.register_server(make_server("s1"))
        with pytest.raises(ValueError):
            cat.add_alias("a__b", "s1__ping")


class TestDrift:
    def _register_pair(self, old_tools, new_tools):
        cat = ToolCatalog()
        cat.register_server(make_server("db", old_tools))
        res = cat.register_server(make_server("db", new_tools), replace_existing=True)
        return cat, res

    def test_tool_added_is_not_breaking(self):
        _, res = self._register_pair(
            (make_tool("q", "db"),),
            (make_tool("q", "db"), make_tool("ins", "db")),
        )
        kinds = {(d.kind, d.breaking) for d in res.drift}
        assert ("tool_added", False) in kinds

    def test_tool_removed_is_breaking(self):
        _, res = self._register_pair(
            (make_tool("q", "db"), make_tool("old", "db")),
            (make_tool("q", "db"),),
        )
        assert any(d.kind == "tool_removed" and d.breaking for d in res.drift)

    def test_required_param_added_quarantines(self):
        cat, res = self._register_pair(
            (make_tool("q", "db", props={"q": {"type": "string"}}),),
            (make_tool("q", "db", props={"q": {"type": "string"}, "db": {"type": "string"}},
                       required=("q", "db")),),
        )
        assert any(d.kind == "required_added" and d.breaking for d in res.drift)
        spec = cat.get("db__q")
        assert spec.quarantined
        assert "required_added" in spec.quarantine_reason
        with pytest.raises(ToolQuarantined):
            cat.ensure_usable(spec)

    def test_param_type_changed_quarantines(self):
        cat, res = self._register_pair(
            (make_tool("q", "db", props={"n": {"type": "string"}}),),
            (make_tool("q", "db", props={"n": {"type": "integer"}}),),
        )
        assert any(d.kind == "type_changed" for d in res.drift)
        assert cat.get("db__q").quarantined

    def test_param_removed_quarantines(self):
        cat, res = self._register_pair(
            (make_tool("q", "db", props={"a": {"type": "string"}, "b": {"type": "string"}}),),
            (make_tool("q", "db", props={"a": {"type": "string"}}),),
        )
        assert any(d.kind == "param_removed" and d.breaking for d in res.drift)
        assert cat.get("db__q").quarantined

    def test_enum_narrowed_quarantines(self):
        cat, res = self._register_pair(
            (make_tool("q", "db", props={"s": {"type": "string", "enum": ["a", "b"]}}),),
            (make_tool("q", "db", props={"s": {"type": "string", "enum": ["a"]}}),),
        )
        assert any(d.kind == "enum_narrowed" and d.breaking for d in res.drift)
        assert cat.get("db__q").quarantined

    def test_risk_change_is_not_breaking(self):
        cat, res = self._register_pair(
            (make_tool("q", "db", risk=Risk.READ),),
            (make_tool("q", "db", risk=Risk.WRITE),),
        )
        assert any(d.kind == "risk_changed" and not d.breaking for d in res.drift)
        assert not cat.get("db__q").quarantined

    def test_unaffected_tool_stays_usable(self):
        cat, _ = self._register_pair(
            (make_tool("q", "db"), make_tool("ok", "db", props={"x": {"type": "string"}})),
            (make_tool("q", "db", required=("q", "extra"), props={
                "q": {"type": "string"}, "extra": {"type": "string"}}),
             make_tool("ok", "db", props={"x": {"type": "string"}})),
        )
        assert cat.get("db__q").quarantined
        assert not cat.get("db__ok").quarantined

    def test_acknowledge_clears_quarantine(self):
        cat, _ = self._register_pair(
            (make_tool("q", "db", props={"a": {"type": "string"}}),),
            (make_tool("q", "db", props={"a": {"type": "integer"}}),),
        )
        assert cat.get("db__q").quarantined
        cleared = cat.acknowledge("db__q")
        assert not cleared.quarantined
        assert not cat.get("db__q").quarantined
        assert cat.ensure_usable(cleared).qualified == "db__q"

    def test_drift_history_accumulates(self):
        cat, _ = self._register_pair(
            (make_tool("q", "db", props={"a": {"type": "string"}}),),
            (make_tool("q", "db", props={"a": {"type": "integer"}}),),
        )
        assert len(cat.drift_history) >= 1


class TestQueries:
    def test_all_tools_skips_quarantined_by_default(self):
        cat = ToolCatalog()
        cat.register_server(make_server("db", (make_tool("q", "db", props={"a": {"type": "string"}}),)))
        cat.register_server(
            make_server("db", (make_tool("q", "db", props={"a": {"type": "integer"}}),)),
            replace_existing=True,
        )
        assert cat.all_tools() == []
        assert len(cat.all_tools(include_quarantined=True)) == 1

    def test_server_tools(self):
        a, _ = two_conflicting_servers()
        cat = ToolCatalog()
        cat.register_server(a)
        assert {t.name for t in cat.server_tools("alpha")} == {"search", "ping"}

    def test_unknown_server(self):
        with pytest.raises(ToolNotFound):
            ToolCatalog().server("nope")

    def test_stats(self):
        a, b = two_conflicting_servers()
        cat = ToolCatalog()
        cat.register_server(a)
        cat.register_server(b)
        st = cat.stats()
        assert st["servers"] == 2
        assert st["tools"] == 4
        assert st["duplicate_names"]["search"] == ["alpha__search", "beta__search"]
        assert st["by_risk"]["read"] == 4

    def test_servers_keeps_registration_order(self):
        cat = ToolCatalog()
        cat.register_server(make_server("z"))
        cat.register_server(make_server("a"))
        assert [s.id for s in cat.servers()] == ["z", "a"]
