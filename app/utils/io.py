"""JSON 文件的容错读与原子写。

为什么必须原子写：Windows 下「任务中心 → 停止」是硬杀进程。若用
`write_text` 原地覆盖，139MB 主库会被截成半个 JSON；而读取侧对损坏文件
是容错返回空 dict，最终表现为「数据全丢了」——实际上只是最后一批没写完。

原子写保证任一时刻磁盘上的文件都是某个完整版本。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_json_dict(path: Path) -> dict:
    """读 JSON 字典文件；缺失、损坏或顶层不是 dict 时返回 {}。

    本项目所有「读缓存 / 读配置」都必须是容错的：宁可退化为空，
    也不能因为一个坏文件让整个服务起不来。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json_atomic(path: Path, data: Any, indent: int = 2) -> Path:
    """原子写 JSON：先写同目录临时文件，再 `os.replace` 整体替换。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, path)
    return path
