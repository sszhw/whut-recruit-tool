"""投递档案：结构化简历数据的持久化与档案全文纯文本导出。

这组测试守住三件容易悄悄坏掉的事：

1. **读取零抛出**：档案坏了 / 缺字段 / 顶层不是 dict，都不能让「我的」整页打不开；
2. **归一化兜底**：模块缺了要补齐成空数组，字段类型错了要纠正，
   否则前端 `profArr()` 拿到 undefined 会整块空白；
3. **导出即全量**：档案是「网申要填的全部字段」的存档，导出时少一块就得回头翻原始
   材料。所以学号、紧急联系人、证书编号、家庭成员都要写进文档；只丢两类东西——
   本地文件指针（照片字段的值是个文件名）与带 ⚠️ / ⛔ 的内部批注。

写文件的用例一律走 tmp_path，不碰用户真实的 data/投递档案.json。
"""

from __future__ import annotations

import pytest
import server
from services import profile as svc

# 公开仓库里的测试夹具不许出现真实证件号：这是示例值（北京东城 + 2000-01-01）。
FAKE_ID_NO = "110101200001011234"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "PROFILE_PATH", tmp_path / "投递档案.json")


# ---------------------------------------------------------------- 归一化


def test_empty_profile_has_every_module():
    """空档案必须含全部模块键，前端才能无脑 profArr(key)。"""
    profile = svc.empty_profile()
    for key in svc.MODULE_KEYS:
        assert key in profile, f"模块 {key} 缺失"
        assert profile[key] == []


def test_load_tolerates_missing_and_broken_file():
    """文件缺失 / 不是 JSON / 顶层不是 dict，都要退化成空档案而不是抛异常。"""
    assert svc.load_profile()["version"] == svc._SCHEMA_VERSION

    svc.PROFILE_PATH.write_text("{ 这不是 JSON", encoding="utf-8")
    assert svc.load_profile()["basic"] == []

    svc.PROFILE_PATH.write_text("[1, 2, 3]", encoding="utf-8")
    assert svc.load_profile()["basic"] == []


def test_normalize_drops_malformed_entries():
    """没有键名的字段行、非 dict 的卡片都要丢掉，不能让脏数据渲染出空行。"""
    raw = {
        "basic": [{"k": "姓名", "v": "张三"}, {"k": "  ", "v": "x"}, "不是对象", {"v": "没有键"}],
        "education": ["不是对象", {"学校名称": "某大学"}],
        "summary": "一行文字",
    }
    out = svc.normalize(raw)
    assert out["basic"] == [{"k": "姓名", "v": "张三"}]
    assert out["education"] == [{"学校名称": "某大学"}]
    # 字符串要按行拆开，否则整段会被当成一条
    assert out["summary"] == ["一行文字"]


def test_save_is_partial_by_module():
    """只提交某个模块时，其余模块必须原样保留。"""
    svc.save_profile({"basic": [{"k": "姓名", "v": "张三"}]})
    svc.save_profile({"intent": [{"k": "到岗时间", "v": "2027-08-01"}]})
    loaded = svc.load_profile()
    assert loaded["basic"][0]["v"] == "张三"
    assert loaded["intent"][0]["v"] == "2027-08-01"
    assert loaded["updated_at"]


# ---------------------------------------------------------------- 简历导出


def _profile_with(**kwargs):
    profile = svc.empty_profile()
    profile.update(kwargs)
    return svc.normalize(profile)


def test_resume_includes_form_only_fields():
    """学号 / 紧急联系人 / 证书编号 都是网申要填的信息，必须进导出。

    这里的证件号码是**示例值**（110101200001011234，北京东城 + 2000-01-01），
    不是任何人的真实身份证号 —— 仓库是公开的，测试夹具里不许出现真实证件号。
    """
    profile = _profile_with(basic=[
        {"k": "姓名", "v": "张三"},
        {"k": "本科学号", "v": "012345"},
        {"k": "紧急联系人电话", "v": "13900000000"},
        {"k": "证件号码", "v": FAKE_ID_NO},
    ])
    text = svc.render_resume(profile, "md")
    assert "姓名：张三" in text
    assert "012345" in text
    assert "13900000000" in text
    assert FAKE_ID_NO in text


def test_resume_includes_family():
    """家庭成员是网申字段，但也是档案的一部分——导出时不能整块丢掉。"""
    profile = _profile_with(
        basic=[{"k": "姓名", "v": "张三"}],
        family=[{"亲属姓名": "张父", "亲属关系": "父亲", "亲属联系电话": "13200000000"}],
    )
    text = svc.render_resume(profile, "md")
    assert "家庭成员" in text
    assert "张父" in text
    assert "13200000000" in text


def test_resume_covers_every_module():
    """模块表里的每个模块都要在导出里出现——漏一个模块是最容易悄悄发生的回归。"""
    sample = {"fields": [{"k": "占位字段", "v": "占位内容"}],
              "items": [{"占位字段": "占位内容"}],
              "lines": ["占位内容"]}
    profile = _profile_with(**{m["key"]: sample[m["kind"]] for m in svc.MODULES})
    text = svc.render_resume(profile, "md")
    for mod in svc.MODULES:
        assert f"## {mod['name']}" in text, f"模块 {mod['key']} 没进导出"


def test_resume_drops_local_file_pointer_and_internal_notes():
    """只丢两类：本地文件指针（照片字段）与带 ⚠️ / ⛔ / 见§ 的内部批注。"""
    profile = _profile_with(
        basic=[{"k": "姓名", "v": "张三"}, {"k": "照片", "v": "简历照片.png"},
               {"k": "硕士指导教师", "v": "陈智君"}],
        education=[{"学校名称": "武汉理工大学",
                    "备注": "⚠️ 学分绩点 3.76 是折算值，非成绩单原值",
                    "学分绩点": "3.76"}],
        awards=[{"其他获奖名称": "学业奖学金", "备注": "⛔ 证书扫描件缺失"}],
        summary=["一段正常评价。", "⚠️ 这条是编造的"],
    )
    text = svc.render_resume(profile, "md")
    assert "简历照片.png" not in text, "照片字段只是本地文件名，不该出现在文档里"
    assert "⚠" not in text and "⛔" not in text, "内部批注标记不该出现在文档里"
    assert "折算值" not in text and "扫描件缺失" not in text, "批注内容要整条丢掉"
    # 丢掉批注不等于丢掉字段：陈智君、绩点、奖项名照常导出
    assert "陈智君" in text
    assert "3.76" in text
    assert "学业奖学金" in text
    assert "一段正常评价。" in text


def test_include_field_is_the_single_cut():
    """纯文本导出与两份 Word 导出共用这一处取舍，语义单独钉死。

    它是「导出即全量」与「不泄漏内部批注」两条规则的唯一交点：
    改坏了它，三条导出链路会同时坏，所以值得单独测。
    """
    assert svc.include_field("姓名", "张三") is True
    assert svc.include_field("学号", "012345") is True          # 网申字段照写
    assert svc.include_field("家庭成员", "张父") is True
    assert svc.include_field("照片", "简历照片.png") is False    # 本地文件指针
    assert svc.include_field("备注", "⚠️ 折算值") is False       # 内部批注
    assert svc.include_field("姓名", "") is False                # 空值
    assert svc.include_field("姓名", None) is False


def test_attachments_module_is_gone():
    """附件清单已按用户要求移除：模块表里不该再有它，旧数据也不该被读回来。"""
    assert "attachments" not in svc.MODULE_KEYS
    profile = svc.normalize({"attachments": [{"材料": "成绩单", "文件名": "成绩单.pdf"}]})
    assert "attachments" not in profile


def test_resume_keeps_real_content():
    """教育 / 项目 / 外语 / 自我评价这些真简历内容必须出现。"""
    profile = _profile_with(
        basic=[{"k": "姓名", "v": "张三"}],
        education=[{"学校名称": "武汉理工大学", "学历": "本科（学士）", "专业": "轮机工程"}],
        projects=[{"项目名称": "BMS 研发", "时间": "2025.03 -- 2026.01",
                   "负责工作": "- 采集开发\n- 故障诊断"}],
        languages=[{"外语语种": "英语", "外语水平": "CET6级"}],
        summary=["熟悉嵌入式开发。"],
    )
    text = svc.render_resume(profile, "md")
    assert "轮机工程" in text
    assert "BMS 研发" in text
    assert "- 故障诊断" in text          # 多行值要原样保留要点
    assert "CET6级" in text
    assert "熟悉嵌入式开发。" in text


def test_item_title_distinguishes_same_school():
    """同一学校的本科与硕士不能都叫「武汉理工大学」——列表里会分不清。"""
    mod = next(m for m in svc.MODULES if m["key"] == "education")
    assert svc.item_title({"学校名称": "武汉理工大学", "学历": "本科（学士）"}, mod) == "武汉理工大学 · 本科（学士）"
    assert svc.item_title({"学校名称": "武汉理工大学", "学历": "硕士研究生"}, mod) == "武汉理工大学 · 硕士研究生"


def test_headings_are_preceded_by_blank_line():
    """标题紧贴正文会被解析器当成普通文本，必须留空行。"""
    profile = _profile_with(
        basic=[{"k": "姓名", "v": "张三"}],
        education=[{"学校名称": "某大学"}],
    )
    text = svc.render_resume(profile, "md")
    assert "\n\n## 教育经历" in text


def test_md_and_txt_share_the_same_content():
    """两种格式共用一份块序列，内容不该各自漂移。"""
    profile = _profile_with(
        basic=[{"k": "姓名", "v": "张三"}, {"k": "专业", "v": "机械"}],
        summary=["一段自我评价。"],
    )
    md, txt = svc.render_resume(profile, "md"), svc.render_resume(profile, "txt")
    assert "张三" in md and "张三" in txt
    assert "机械" in md and "机械" in txt
    assert "一段自我评价。" in md and "一段自我评价。" in txt


def test_filename_carries_name():
    profile = _profile_with(basic=[{"k": "姓名", "v": "张三"}])
    assert svc.resume_filename(profile, "md").startswith("张三-")
    assert svc.resume_filename(profile, "txt").endswith(".txt")
    assert svc.resume_filename(profile, "docx").endswith(".docx")


# ---------------------------------------------------------------- Word 导出
#
# 两份 `.docx`（HR 版 / 完整版）都在 tests/test_resume_hr.py 里测——它们同属一套
# 排版，放在一起才看得出「同一份内容来源、两种取舍」的对照。这里只留接口层约定。


def test_api_docx_download(client):
    """不传 style 时出完整版：二进制流 + 正确的 MIME 与附件头。"""
    resp = client.post("/api/profile/render.docx", json={"basic": [{"k": "姓名", "v": "张三"}]})
    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["Content-Type"]
    assert "attachment" in resp.headers["Content-Disposition"]
    assert resp.data[:2] == b"PK"


def test_api_docx_rejects_bad_body(client):
    assert client.post("/api/profile/render.docx", json="不是对象").status_code == 400


# ---------------------------------------------------------------- 接口


@pytest.fixture()
def client():
    return server.app.test_client()


def test_api_roundtrip(client):
    """GET 拿到模块元信息；POST 按模块保存后能读回。"""
    resp = client.get("/api/profile")
    assert resp.status_code == 200
    assert resp.get_json()["modules"]

    resp = client.post("/api/profile", json={"basic": [{"k": "姓名", "v": "张三"}]})
    assert resp.status_code == 200
    assert resp.get_json()["profile"]["version"] == svc._SCHEMA_VERSION

    assert client.get("/api/profile").get_json()["profile"]["basic"][0]["v"] == "张三"


def test_api_render_rejects_bad_body(client):
    resp = client.post("/api/profile/render?format=md", json="不是对象")
    assert resp.status_code == 400


def test_api_fill_requires_text(client):
    resp = client.post("/api/profile/fill", json={})
    assert resp.status_code == 400
