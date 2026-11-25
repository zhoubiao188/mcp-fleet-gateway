from mcp_fleet_gateway.validation import narrow, validate

SCHEMA = {
    "type": "object",
    "properties": {
        "q": {"type": "string", "minLength": 2, "maxLength": 40},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "state": {"type": "string", "enum": ["open", "closed"]},
        "pattern": {"type": "string", "pattern": "^[a-z]+$"},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "nested": {
            "type": "object",
            "properties": {"a": {"type": "boolean"}},
            "required": ["a"],
        },
        "score": {"type": "number"},
    },
    "required": ["q"],
    "additionalProperties": False,
}


def ok(value):
    return validate(SCHEMA, value) == []


def errs(value):
    return validate(SCHEMA, value)


class TestBasics:
    def test_valid_payload_passes(self):
        assert ok({"q": "abc", "limit": 5})

    def test_empty_schema_passes_everything(self):
        assert validate(None, {"anything": 1}) == []
        assert validate({}, [1, 2, 3]) == []

    def test_missing_required(self):
        e = errs({"limit": 3})
        assert len(e) == 1
        assert "缺少必填参数" in e[0]
        assert "q" in e[0]

    def test_wrong_type(self):
        e = errs({"q": 123})
        assert "期望类型 string" in e[0]

    def test_integer_rejects_bool(self):
        e = errs({"q": "abc", "limit": True})
        assert "期望类型 integer" in e[0]

    def test_number_accepts_int_and_float(self):
        assert ok({"q": "abc", "score": 1})
        assert ok({"q": "abc", "score": 1.5})

    def test_array_type(self):
        e = errs({"q": "abc", "tags": "not-a-list"})
        assert "期望类型 array" in e[0]

    def test_object_type(self):
        e = errs({"q": "abc", "nested": [1]})
        assert "期望类型 object" in e[0]

    def test_unknown_type_is_lenient(self):
        assert validate({"type": "colour"}, "red") == []


class TestConstraints:
    def test_enum(self):
        e = errs({"q": "abc", "state": "merged"})
        assert "枚举" in e[0]

    def test_pattern(self):
        e = errs({"q": "abc", "pattern": "ABC"})
        assert "不匹配模式" in e[0]

    def test_bad_regex_is_ignored(self):
        assert validate({"type": "string", "pattern": "([a-z"}, "x") == []

    def test_min_max_length(self):
        assert "长度不能小于" in errs({"q": "a"})[0]
        assert "长度不能大于" in errs({"q": "a" * 41})[0]

    def test_min_max_number(self):
        assert "不能小于" in errs({"q": "abc", "limit": 0})[0]
        assert "不能大于" in errs({"q": "abc", "limit": 51})[0]

    def test_max_items(self):
        e = errs({"q": "abc", "tags": ["a", "b", "c"]})
        assert "元素数量不能多于" in e[0]

    def test_items_validated(self):
        e = errs({"q": "abc", "tags": ["a", 2]})
        assert "$.tags[1]" in e[0]

    def test_nested_required(self):
        e = errs({"q": "abc", "nested": {}})
        assert "$.nested" in e[0] and "缺少必填参数" in e[0]

    def test_additional_properties_rejected(self):
        e = errs({"q": "abc", "whoops": 1})
        assert "不允许的参数" in e[0]
        assert "whoops" in e[0]

    def test_multiple_errors_collected(self):
        e = errs({"limit": "x", "state": "nope"})
        assert len(e) >= 3

    def test_union_type(self):
        schema = {"type": ["string", "null"]}
        assert validate(schema, None) == []
        assert validate(schema, "x") == []
        assert validate(schema, 1) != []


class TestNarrow:
    def test_record_projection(self):
        rec = {"id": 1, "title": "t", "embedding": [0.1]}
        assert narrow(rec, ["title"]) == {"title": "t"}

    def test_list_projection(self):
        rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        assert narrow(rows, ["a"]) == [{"a": 1}, {"a": 3}]

    def test_container_keeps_meta_and_projects_items(self):
        payload = {"total": 2, "items": [{"id": 1, "title": "x", "blob": "y" * 100}]}
        out = narrow(payload, ["title"])
        assert out["total"] == 2
        assert out["items"] == [{"title": "x"}]

    def test_container_with_matches(self):
        payload = {"query": "q", "matches": [{"title": "a", "embedding": [1, 2]}]}
        out = narrow(payload, ["title"])
        assert out["matches"] == [{"title": "a"}]

    def test_empty_fields_returns_input(self):
        payload = {"a": 1}
        assert narrow(payload, []) == payload

    def test_scalar_passthrough(self):
        assert narrow("text", ["a"]) == "text"
        assert narrow(42, ["a"]) == 42

    def test_mapping_without_matching_field_is_treated_as_container(self):
        # 无法区分"记录"和"容器"时选择保守策略：保留原有字段，不静默删数据
        assert narrow({"a": 1}, ["z"]) == {"a": 1}
