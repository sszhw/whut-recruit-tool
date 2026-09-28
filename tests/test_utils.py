"""utils 层单元测试。

这批用例的意义在于**锁定行为契约**：`strip_html` 原先在 crawler / resume 里
各有一份且实现不一致，合并后必须保证结果是取「较完整的那一版」，而不是凭运气。
后续若有人改动 utils.text，这里的用例会立刻告诉他改坏了什么。
"""

from __future__ import annotations

import json

from utils import io as io_utils
from utils import text as text_utils


class TestStripHtml:
    def test_none_and_empty(self):
        assert text_utils.strip_html(None) == ""
        assert text_utils.strip_html("") == ""
        assert text_utils.strip_html(0) == ""

    def test_removes_script_and_style(self):
        html = "<script>var a=1;</script><style>.x{color:red}</style><p>正文</p>"
        assert "script" not in text_utils.strip_html(html)
        assert text_utils.strip_html(html) == "正文"

    def test_br_and_block_tags_become_newlines(self):
        assert text_utils.strip_html("第一行<br>第二行") == "第一行\n第二行"
        assert text_utils.strip_html("<p>甲</p><p>乙</p>") == "甲\n乙"

    def test_unescapes_entities(self):
        # 这是原 resume 版做不到的：它只手工替换四个实体
        assert text_utils.strip_html("A&amp;B") == "A&B"
        assert text_utils.strip_html("&quot;引用&quot;") == '"引用"'

    def test_nbsp_becomes_space(self):
        assert text_utils.strip_html("武汉\u00a0理工") == "武汉 理工"

    def test_collapses_inline_spaces_and_drops_blank_lines(self):
        assert text_utils.strip_html("甲\t\t乙") == "甲 乙"
        assert text_utils.strip_html("<p>甲</p>\n\n\n<p>乙</p>") == "甲\n乙"

    def test_accepts_non_str_input(self):
        assert text_utils.strip_html(123) == "123"


class TestTruncate:
    def test_is_none_safe(self):
        assert text_utils.truncate(None) == ""
        assert text_utils.truncate("") == ""

    def test_strips_and_limits(self):
        assert text_utils.truncate("  hello  ", 5) == "hello"
        assert text_utils.truncate("abcdefgh", 3) == "abc"


class TestExtractJsonObject:
    def test_plain_json(self):
        assert text_utils.extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_code_block(self):
        content = '好的，结果如下：\n```json\n{"company_type": "央企"}\n```\n请查收'
        assert text_utils.extract_json_object(content) == {"company_type": "央企"}

    def test_surrounding_prose(self):
        assert text_utils.extract_json_object('我觉得应该是 {"x": 2} 这样') == {"x": 2}

    def test_invalid_returns_none(self):
        assert text_utils.extract_json_object("完全不是 JSON") is None
        assert text_utils.extract_json_object("") is None
        assert text_utils.extract_json_object(None) is None

    def test_non_dict_returns_none(self):
        # JSON 数组是合法的 JSON，但不是本项目要的对象结构
        assert text_utils.extract_json_object("[1, 2, 3]") is None


class TestJsonDictIO:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert io_utils.load_json_dict(tmp_path / "nope.json") == {}

    def test_corrupted_file_returns_empty_dict(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{半个 json", encoding="utf-8")
        assert io_utils.load_json_dict(path) == {}

    def test_non_dict_top_level_returns_empty_dict(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[1, 2]", encoding="utf-8")
        assert io_utils.load_json_dict(path) == {}

    def test_reads_valid_file(self, tmp_path):
        path = tmp_path / "ok.json"
        path.write_text(json.dumps({"a": 1}, ensure_ascii=False), encoding="utf-8")
        assert io_utils.load_json_dict(path) == {"a": 1}


class TestAtomicWrite:
    def test_roundtrip_and_no_temp_leftover(self, tmp_path):
        path = tmp_path / "out.json"
        io_utils.write_json_atomic(path, {"企业": "测试"}, indent=1)
        assert json.loads(path.read_text(encoding="utf-8")) == {"企业": "测试"}
        # 原子写若成功，临时文件不应残留
        assert not list(tmp_path.glob("*.tmp"))

    def test_overwrite_keeps_valid_json(self, tmp_path):
        path = tmp_path / "out.json"
        io_utils.write_json_atomic(path, {"v": 1})
        io_utils.write_json_atomic(path, {"v": 2})
        assert io_utils.load_json_dict(path) == {"v": 2}
