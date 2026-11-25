import json

from mcp_fleet_gateway.shaping import CursorStore, ShapePolicy, page_from_cursor, shape


def items(n=30, blob=10):
    return [{"id": i, "title": f"文档 {i}", "blob": "x" * blob} for i in range(n)]


class TestPassthrough:
    def test_small_result_untouched(self):
        payload = {"answer": 42}
        r = shape(payload, ShapePolicy(max_bytes=10_000))
        assert r.content == payload
        assert r.before == r.after
        assert r.truncated is False

    def test_small_list_untouched(self):
        payload = items(5)
        r = shape(payload, ShapePolicy(max_bytes=10_000, max_items=20))
        assert r.content == payload


class TestProjection:
    def test_drops_fields(self):
        payload = {"items": [{"id": 1, "title": "t", "embedding": [0.1] * 100}]}
        r = shape(payload, ShapePolicy(max_bytes=10_000, project=("title",)))
        assert r.content["items"] == [{"title": "t"}]
        assert r.after < r.before

    def test_big_win_on_vectors(self):
        payload = {"matches": [{"title": f"t{i}", "embedding": [0.5] * 512} for i in range(10)]}
        r = shape(payload, ShapePolicy(max_bytes=10_000, project=("title",), max_items=50))
        assert r.after < r.before / 10


class TestTruncation:
    def test_long_string_cut(self):
        payload = {"log": "y" * 5_000}
        r = shape(payload, ShapePolicy(max_bytes=400, max_string=100))
        assert "字符省略" in r.content["log"]
        assert r.truncated is True

    def test_deep_structure_protected(self):
        deep = cur = {}
        for _ in range(20):
            cur["next"] = {}
            cur = cur["next"]
        cur["v"] = 1
        r = shape(deep, ShapePolicy(max_bytes=150, max_string=10, max_depth=3))
        assert "结构过深" in json.dumps(r.content, ensure_ascii=False)


class TestPaging:
    def test_pages_and_returns_cursor(self):
        store = CursorStore()
        r = shape({"items": items(50)}, ShapePolicy(max_bytes=30_000, max_items=20),
                  store=store, server_id="s1", tool="s1__search")
        assert len(r.content["data"]["items"]) == 20
        assert r.content["_meta"]["total"] == 50
        assert r.content["_meta"]["returned"] == 20
        assert r.cursor and r.cursor.startswith("cur_")

    def test_no_cursor_without_store(self):
        r = shape(items(50), ShapePolicy(max_bytes=10, max_items=20))
        assert r.cursor is None
        assert r.content["_meta"]["total"] == 50
        assert "next_cursor" not in r.content["_meta"]

    def test_follow_up_page(self):
        store = CursorStore()
        policy = ShapePolicy(max_bytes=100_000, max_items=20)
        r = shape({"items": items(45)}, policy, store=store, server_id="s1", tool="t")
        page2 = page_from_cursor(store, r.cursor, policy)
        assert len(page2.content["data"]) == 20
        assert page2.cursor is not None
        page3 = page_from_cursor(store, page2.cursor, policy)
        assert len(page3.content) == 5
        assert page3.cursor is None

    def test_invalid_cursor(self):
        r = page_from_cursor(CursorStore(), "cur_nope", ShapePolicy())
        assert r.content["error"] == "cursor_invalid"

    def test_cursor_is_single_use(self):
        store = CursorStore()
        policy = ShapePolicy(max_bytes=100_000, max_items=2)
        r = shape(items(6), policy, store=store)
        page_from_cursor(store, r.cursor, policy)
        again = page_from_cursor(store, r.cursor, policy)
        assert again.content["error"] == "cursor_invalid"

    def test_cursor_capacity_evicts_oldest(self):
        store = CursorStore(capacity=2)
        policy = ShapePolicy(max_bytes=1, max_items=1)
        cursors = [shape(items(3), policy, store=store).cursor for _ in range(3)]
        assert len(store) == 2
        assert page_from_cursor(store, cursors[0], policy).content["error"] == "cursor_invalid"


class TestFallback:
    def test_degrades_to_sample_when_still_too_big(self):
        payload = [{"id": i, "blob": "z" * 900} for i in range(30)]
        r = shape(payload, ShapePolicy(max_bytes=500, max_items=20, max_string=400),
                  store=CursorStore())
        assert "dropped" in r.content["_meta"]
        assert len(r.content["data"]["sample"]) == 3
        assert r.truncated is True

    def test_meta_reports_totals(self):
        payload = [{"id": i, "blob": "z" * 900} for i in range(30)]
        r = shape(payload, ShapePolicy(max_bytes=500, max_items=20, max_string=400))
        assert r.content["_meta"]["total"] == 30


class TestPolicyMerge:
    def test_merged_overrides_bytes(self):
        p = ShapePolicy(max_bytes=1_000, project=("a",))
        m = p.merged(max_bytes=5_000)
        assert m.max_bytes == 5_000
        assert m.project == ("a",)

    def test_merged_overrides_project(self):
        p = ShapePolicy(project=("a",))
        assert p.merged(project=("b",)).project == ("b",)

    def test_merged_keeps_defaults(self):
        m = ShapePolicy(max_items=7).merged()
        assert m.max_items == 7
