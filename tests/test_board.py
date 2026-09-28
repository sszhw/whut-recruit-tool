"""投递看板业务层测试。"""

from __future__ import annotations

import json

import board_store
import pytest
from services import ServiceError, board


@pytest.fixture(autouse=True)
def isolated_board(tmp_path, monkeypatch):
    """看板测试只写临时文件，不触碰用户真实进度。"""
    monkeypatch.setattr(board_store, "BOARD_PATH", tmp_path / "投递看板.json")


def _payload(source_id="r-1", unit="甲公司", title="校园招聘"):
    return {
        "source_type": "招聘",
        "source_id": source_id,
        "snapshot": {"单位": unit, "标题": title, "原网页": f"https://example.com/{source_id}"},
    }


def test_store_missing_and_corrupt_file_fall_back_to_empty():
    assert board_store.load_board() == {"version": 1, "items": []}
    board_store.BOARD_PATH.write_text("{bad json", encoding="utf-8")
    assert board_store.load_board() == {"version": 1, "items": []}


def test_crud_keeps_snapshot_after_source_is_gone():
    item = board.create_item(_payload())
    assert item["stage"] == "关注"
    assert item["snapshot"] == {
        "unit": "甲公司",
        "title": "校园招聘",
        "link": "https://example.com/r-1",
        "date": "",
        "location": "",
    }

    updated = board.update_note(item["id"], "已完成网申")
    assert updated["note"] == "已完成网申"
    assert board.list_items()[0]["snapshot"]["unit"] == "甲公司"

    deleted = board.delete_item(item["id"])
    assert deleted["id"] == item["id"]
    assert board.list_items() == []
    assert json.loads(board_store.BOARD_PATH.read_text(encoding="utf-8"))["items"] == []


def test_duplicate_source_is_rejected():
    board.create_item(_payload())
    with pytest.raises(ServiceError) as caught:
        board.create_item(_payload())
    assert caught.value.status == 409


def test_legal_status_transitions_and_filtering():
    item = board.create_item(_payload())
    for stage in ("投递", "笔试", "面试", "Offer", "放弃", "关注"):
        item = board.update_status(item["id"], stage)
        assert item["stage"] == stage
    assert [event["stage"] for event in item["history"]] == [
        "关注", "投递", "笔试", "面试", "Offer", "放弃", "关注",
    ]
    assert board.list_items("关注")[0]["id"] == item["id"]
    assert board.list_items("面试") == []


def test_illegal_status_transition_and_not_found():
    item = board.create_item(_payload())
    with pytest.raises(ServiceError, match="不能从"):
        board.update_status(item["id"], "面试")
    with pytest.raises(ServiceError) as caught:
        board.update_note("missing", "备注")
    assert caught.value.status == 404


def test_statistics_counts_and_historical_conversion_rates():
    first = board.create_item(_payload("r-1"))
    board.update_status(first["id"], "投递")
    board.update_status(first["id"], "面试")
    board.update_status(first["id"], "Offer")

    second = board.create_item(_payload("r-2", unit="乙公司"))
    board.update_status(second["id"], "投递")
    board.create_item(_payload("r-3", unit="丙公司"))

    stats = board.statistics()
    assert stats["total"] == 3
    assert stats["counts"]["关注"] == 1
    assert stats["counts"]["投递"] == 1
    assert stats["counts"]["Offer"] == 1
    assert stats["conversion_rates"]["关注→投递"] == pytest.approx(66.7)
    assert stats["conversion_rates"]["投递→面试"] == 50.0
    assert stats["conversion_rates"]["面试→Offer"] == 100.0
