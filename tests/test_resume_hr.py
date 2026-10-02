"""两份 Word 简历导出（HR 版 / 完整版）+ 个人照片。

守住四件事：

1. **产物必须真的能打开且内容完整**：它是给真人 HR 看的最终交付物，
   生成一个打不开的文件比排版难看严重得多；
2. **HR 版不该出现的内容不能出现**：高中经历、家庭信息、带 ⚠️ / ⛔ 标记的
   「待核实」值——这些进了给 HR 的文档就是事故，不是瑕疵；
3. **完整版必须一条不落**：它存在的意义就是「HR 同款排版 + 档案全量」，
   高中、绩点、学号、家庭成员都要写出来。两份的差别只有内容取舍，
   版式共用同一套（谁偷偷分叉了，这里就红）；
4. **照片只在两份 .docx 里**：纯文本导出（.md / .txt）不含图，
   照片本身的存储还要防越界（格式 / 大小）。

写文件的用例一律走 tmp_path，不碰真实的 data/ 目录。
"""

from __future__ import annotations

import io
import zipfile
from urllib.parse import unquote

import pytest
import server
from docx import Document
from services import ServiceError
from services import profile as svc
from services import resume_hr as hr

# 1×1 与 3×4 的合法 PNG（自带像素数据，不依赖 Pillow）
PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """档案与照片全部落到 tmp_path，别碰用户真实数据。"""
    monkeypatch.setattr(svc, "PROFILE_PATH", tmp_path / "投递档案.json")
    monkeypatch.setattr(svc, "DATA", tmp_path)


@pytest.fixture()
def client():
    return server.app.test_client()


def _profile(**kwargs):
    return svc.normalize(kwargs)


def _texts(raw: bytes) -> list[str]:
    """把生成的 docx 解成段落文本列表（含头部表格里的）。"""
    doc = Document(io.BytesIO(raw))
    out = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                out.extend(p.text for p in cell.paragraphs)
    return [t for t in out if t.strip()]


def _media(raw: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return [n for n in z.namelist() if n.startswith("word/media/")]


def _xml(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return z.read("word/document.xml").decode("utf-8")


def _download_name(resp) -> str:
    """从 Content-Disposition 里取出文件名（中文走 RFC 5987 的 filename*，要解码）。"""
    disp = resp.headers["Content-Disposition"]
    return unquote(disp.split("filename*=UTF-8''")[-1]) if "filename*=" in disp else disp


SAMPLE = dict(
    basic=[{"k": "姓名", "v": "张三"}, {"k": "性别", "v": "男"},
           {"k": "出生日期", "v": "2002-08-02"}, {"k": "年龄", "v": "24"},
           {"k": "移动电话", "v": "13800000000"}, {"k": "电子邮箱", "v": "a@b.com"}],
    education=[{"学校名称": "养正中学", "学历": "普通高中",
                "入学日期": "2017-09-01", "毕业/预计毕业日期": "2020-06-30"},
               {"学校名称": "武汉理工大学", "学历": "硕士研究生", "专业": "机械工程",
                "入学日期": "2024-09-01", "毕业/预计毕业日期": "预计 2027-06"}],
    projects=[{"项目名称": "BMS 系统研发", "时间": "2025.03 -- 2026.01",
               "项目简介": "嵌入式软件开发。",
               "负责工作": "- 实时软件开发：基于 FreeRTOS 完成任务开发。\n- 故障诊断：参与底层诊断开发。"}],
    skills=[{"k": "开发语言", "v": "C"}, {"k": "计算机水平", "v": "其他"}],
    languages=[{"外语语种": "英语", "外语水平": "CET6级", "成绩": "430（听力 168）· 2023 年"}],
    awards=[{"获奖时间": "2024-10", "其他获奖名称": "学业二等奖学金", "奖励批准单位": "武汉理工大学"},
            {"获奖时间": "2025-10", "其他获奖名称": "学业二等奖学金", "奖励批准单位": "武汉理工大学"}],
    summary=["控制开发与仿真测试：熟悉 MATLAB/Simulink。"],
    hobbies=["足球、篮球"],
    family=[{"亲属姓名": "张父", "亲属关系": "父亲"}],
)


# ---------------------------------------------------------------- 基本产物


def test_hr_is_valid_docx():
    raw = hr.render_hr_docx(_profile(**SAMPLE))
    assert raw[:2] == b"PK"
    doc = Document(io.BytesIO(raw))
    assert len(doc.paragraphs) > 10


def test_hr_contains_every_section():
    """有内容的模块都必须出现在文档里，不能悄悄少一块。"""
    text = "\n".join(_texts(hr.render_hr_docx(_profile(**SAMPLE))))
    for title in ("教育经历", "项目经验", "专业技能", "荣誉奖项", "自我评价", "兴趣爱好"):
        assert title in text, f"缺少分区：{title}"
    assert "张三" in text
    assert "13800000000" in text


def test_hr_keeps_project_bullets():
    """「负责工作」的多行要点要拆成独立行，且去掉行首的 `- `。"""
    text = "\n".join(_texts(hr.render_hr_docx(_profile(**SAMPLE))))
    assert "实时软件开发" in text and "故障诊断" in text
    assert "- 实时软件开发" not in text


def test_hr_merges_same_award():
    """同名同单位的奖项合并成一行，年度并排；重复两条会显得像复制粘贴。"""
    rows = [t for t in _texts(hr.render_hr_docx(_profile(**SAMPLE))) if "奖学金" in t]
    assert len(rows) == 1
    assert "2024.10" in rows[0] and "2025.10" in rows[0]


# ---------------------------------------------------------------- 不该出现的内容


def test_hr_skips_high_school():
    """有硕士学历时高中不该上简历——占地方且无信息量。"""
    text = "\n".join(_texts(hr.render_hr_docx(_profile(**SAMPLE))))
    assert "养正中学" not in text
    assert "武汉理工大学" in text


def test_hr_drops_flagged_values():
    """带 ⚠️ / ⛔ / 「见 §」的待核实值绝不能进给 HR 的文档。"""
    profile = _profile(
        basic=[{"k": "姓名", "v": "张三"}, {"k": "移动电话", "v": "13900000000"}],
        skills=[{"k": "其他技能", "v": "⚠️ 编造：精通 COMSOL"},
                {"k": "特长", "v": "⛔ 证书扫描件缺失"},
                {"k": "开发语言", "v": "C"}],
        summary=["见 §18.8 补齐", "真实的自我评价。"],
    )
    text = "\n".join(_texts(hr.render_hr_docx(profile)))
    for bad in ("⚠️", "⛔", "见 §", "编造", "COMSOL", "扫描件缺失"):
        assert bad not in text, f"待核实内容泄漏进 HR 版：{bad}"
    # 对照组：同一批字段里没有标记的要照常输出，不能为了过滤把整块都丢了
    assert "真实的自我评价。" in text
    assert "13900000000" in text
    assert "开发语言" in text


def test_hr_excludes_family():
    text = "\n".join(_texts(hr.render_hr_docx(_profile(**SAMPLE))))
    assert "张父" not in text
    assert "家庭成员" not in text


# ---------------------------------------------------------------- 照片


def test_hr_without_photo_has_no_media():
    raw = hr.render_hr_docx(_profile(**SAMPLE), None)
    assert _media(raw) == []


def test_hr_embeds_photo(tmp_path):
    photo = tmp_path / "简历照片.png"
    photo.write_bytes(PNG_1PX)
    raw = hr.render_hr_docx(_profile(**SAMPLE), photo)
    assert len(_media(raw)) == 1


def test_hr_survives_broken_photo(tmp_path):
    """图坏了也不能让整份简历导不出来——那是从「少张图」升级成「交不了差」。"""
    bad = tmp_path / "坏图.png"
    bad.write_bytes(b"this is not an image")
    raw = hr.render_hr_docx(_profile(**SAMPLE), bad)
    assert raw[:2] == b"PK"
    assert "武汉理工大学" in "\n".join(_texts(raw))


def test_full_embeds_photo(tmp_path):
    """完整版和 HR 版一样带照片——它俩共用同一套版式。"""
    photo = tmp_path / "简历照片.png"
    photo.write_bytes(PNG_1PX)
    assert len(_media(hr.render_full_docx(_profile(**SAMPLE), photo))) == 1


# ---------------------------------------------------------------- 完整版
#
# 「HR 同款排版 + 档案全量」：版式与 HR 版共用，内容一条不落。


def test_full_is_valid_docx():
    raw = hr.render_full_docx(_profile(**SAMPLE))
    assert raw[:2] == b"PK"
    assert Document(io.BytesIO(raw)).paragraphs


def test_full_contains_every_module():
    """按 MODULES 遍历，模块一个都不能少——包括界面标「· 填表」的家庭成员。

    每个模块都塞一条占位内容：空模块不出分区是正常行为，不塞就分不清
    「模块被漏了」和「模块本来就没内容」。
    """
    sample = {"fields": [{"k": "占位字段", "v": "占位内容"}],
              "items": [{"占位字段": "占位内容"}],
              "lines": ["占位内容"]}
    profile = _profile(**{m["key"]: sample[m["kind"]] for m in svc.MODULES})
    text = "\n".join(_texts(hr.render_full_docx(profile)))
    for mod in svc.MODULES:
        assert mod["name"] in text, f"模块 {mod['key']} 没进完整版"


def test_full_includes_form_only_content():
    """学号 / 紧急联系人 / 家庭成员在完整版里必须写出来——这就是它存在的理由。"""
    profile = _profile(
        basic=[{"k": "姓名", "v": "张三"}, {"k": "学号", "v": "0123456789"},
               {"k": "紧急联系人电话", "v": "13900000000"}],
        family=[{"亲属姓名": "张父", "亲属关系": "父亲", "工作单位": "某某厂"}],
    )
    text = "\n".join(_texts(hr.render_full_docx(profile)))
    for want in ("家庭成员", "0123456789", "13900000000", "张父", "某某厂"):
        assert want in text, f"完整版漏了 {want}"


def test_full_keeps_what_hr_version_drops():
    """高中、绩点这些 HR 版会挑掉的内容，完整版照写——两份的差别就在这一条。"""
    profile = _profile(
        basic=[{"k": "姓名", "v": "张三"}],
        education=[{"学校名称": "养正中学", "学历": "普通高中"},
                   {"学校名称": "武汉理工大学", "学历": "硕士研究生", "学分绩点": "3.76"}],
    )
    full = "\n".join(_texts(hr.render_full_docx(profile)))
    curated = "\n".join(_texts(hr.render_hr_docx(profile)))
    assert "养正中学" in full and "3.76" in full
    assert "养正中学" not in curated


def test_full_drops_flagged_values():
    """⚠️ / ⛔ / 见§ 的待核实值两份都不进——排版可以不同，这条底线一样。"""
    profile = _profile(
        basic=[{"k": "姓名", "v": "张三"}],
        skills=[{"k": "其他技能", "v": "⚠️ 编造：精通 COMSOL"}, {"k": "开发语言", "v": "C"}],
        summary=["见 §18.8 补齐", "真实的自我评价。"],
    )
    text = "\n".join(_texts(hr.render_full_docx(profile)))
    for bad in ("⚠", "⛔", "见 §", "COMSOL"):
        assert bad not in text, f"待核实内容泄漏进完整版：{bad}"
    assert "真实的自我评价。" in text
    assert "开发语言" in text


def test_full_drops_local_file_pointer():
    """`照片` 字段的值是本机文件名，写进文档只是噪音。"""
    profile = _profile(basic=[{"k": "姓名", "v": "张三"},
                              {"k": "照片", "v": "简历照片.png"}])
    assert "简历照片.png" not in "\n".join(_texts(hr.render_full_docx(profile)))


def test_full_shares_layout_with_hr():
    """两份共用同一套版式：A4 + 头部无边框表格（右边那格放照片）。"""
    raw = hr.render_full_docx(_profile(**SAMPLE), None)
    assert "<w:tbl>" in _xml(raw), "完整版应与 HR 版一样用表格做头部布局"
    assert "<w:drawing>" not in _xml(raw), "没传照片时不该凭空插图"
    section = Document(io.BytesIO(raw)).sections[0]
    assert section.page_width.cm == pytest.approx(hr.PAGE_W_CM, abs=0.05)
    assert section.page_height.cm == pytest.approx(hr.PAGE_H_CM, abs=0.05)


def test_full_survives_broken_photo(tmp_path):
    bad = tmp_path / "坏图.png"
    bad.write_bytes(b"this is not an image")
    raw = hr.render_full_docx(_profile(**SAMPLE), bad)
    assert raw[:2] == b"PK"
    assert "武汉理工大学" in "\n".join(_texts(raw))


# ---------------------------------------------------------------- 日期归一化


@pytest.mark.parametrize("raw,expect", [
    ("2024-09-01", "2024.09"),
    ("2024.07 -- 2025.03", "2024.07 – 2025.03"),
    ("2024.07至2025.03", "2024.07 – 2025.03"),
    ("2026.01 -- 至今", "2026.01 – 至今"),      # 「至今」是内容，不是分隔符
    ("预计 2027-06", "预计 2027.06"),           # 前缀不能叠加成「预计 预计」
    ("2024-10", "2024.10"),
    ("至今", "至今"),
    ("", ""),
])
def test_ym_normalisation(raw, expect):
    assert hr._ym(raw) == expect


# ---------------------------------------------------------------- 照片存储


def test_photo_save_and_delete():
    assert svc.photo_meta()["exists"] is False
    meta = svc.save_photo("photo.png", PNG_1PX)
    assert meta["exists"] and meta["filename"] == "简历照片.png"
    assert svc.find_photo().is_file()
    assert svc.delete_photo() is True
    assert svc.find_photo() is None
    assert svc.delete_photo() is False


def test_photo_replaces_previous_extension():
    """换格式后 data/ 下只能留一张，否则 find_photo 会取到旧图。"""
    svc.save_photo("a.png", PNG_1PX)
    svc.save_photo("b.jpg", b"\xff\xd8\xff\xe0stub")
    assert svc.find_photo().name == "简历照片.jpg"
    assert not (svc.DATA / "简历照片.png").exists()


def test_photo_rejects_bad_input():
    """断言到具体异常类型：用裸 Exception 会把「忘了写校验」也算成通过。"""
    with pytest.raises(ServiceError):
        svc.save_photo("a.txt", PNG_1PX)
    with pytest.raises(ServiceError):
        svc.save_photo("a.png", b"")
    with pytest.raises(ServiceError):
        svc.save_photo("a.png", b"x" * (svc.MAX_PHOTO_BYTES + 1))


def test_photo_filename_without_extension():
    with pytest.raises(ServiceError):
        svc.save_photo("noext", PNG_1PX)


# ---------------------------------------------------------------- 接口


def test_api_photo_roundtrip(client):
    assert client.get("/api/profile/photo").status_code == 404
    assert client.get("/api/profile").get_json()["photo"]["exists"] is False

    resp = client.post("/api/profile/photo",
                       data={"file": (io.BytesIO(PNG_1PX), "我.png")},
                       content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["photo"]["exists"] is True

    assert client.get("/api/profile/photo").status_code == 200
    assert client.get("/api/profile").get_json()["photo"]["exists"] is True

    assert client.delete("/api/profile/photo").get_json()["deleted"] is True
    assert client.get("/api/profile/photo").status_code == 404


def test_api_photo_requires_file(client):
    assert client.post("/api/profile/photo", data={}).status_code == 400


def test_api_photo_rejects_wrong_format(client):
    resp = client.post("/api/profile/photo",
                       data={"file": (io.BytesIO(b"hello"), "notes.txt")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "格式" in resp.get_json()["error"]


def test_api_hr_docx_download(client):
    resp = client.post("/api/profile/render.docx?style=hr", json=SAMPLE)
    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["Content-Type"]
    assert resp.data[:2] == b"PK"
    # 文件名要能区分出这是 HR 版，否则和完整版下载下来同名
    assert "HR" in _download_name(resp)


def test_api_full_docx_is_the_default(client):
    """不传 style 时出完整版——它现在是最常用的那一份。"""
    resp = client.post("/api/profile/render.docx", json=SAMPLE)
    assert resp.status_code == 200
    assert "完整版" in _download_name(resp)


def test_api_full_docx_carries_archive_content(client):
    """完整版经接口出来也要带家庭成员——排版换了，内容口径不能跟着变。"""
    resp = client.post("/api/profile/render.docx?style=full", json=SAMPLE)
    assert "张父" in "\n".join(_texts(resp.data))


def test_api_plain_style_is_alias_of_full(client):
    """`plain` 是历史别名（早年那版禁表格的「可解析版」），现等价于完整版。"""
    plain = client.post("/api/profile/render.docx?style=plain", json=SAMPLE)
    full = client.post("/api/profile/render.docx?style=full", json=SAMPLE)
    assert plain.status_code == 200
    assert _download_name(plain) == _download_name(full)


def test_api_unknown_style_falls_back_to_full(client):
    """style 参数写错不该 500，退回完整版即可。"""
    resp = client.post("/api/profile/render.docx?style=nonsense", json=SAMPLE)
    assert resp.status_code == 200
    assert "完整版" in _download_name(resp)
