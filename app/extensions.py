"""Flask 之外的运行时共享对象（目前主要是后台任务管理器）。

单独抽一层是为了打断循环导入：
蓝图（app/api/*）需要 `tasks` 实例，而 `tasks` 又必须在应用组装前就存在。
若把它挂在 server 模块上，`api → server → api` 就成环了。

依赖方向：  settings  ←  extensions  ←  api  ←  server
"""

from __future__ import annotations

import taskcenter
from settings import (
    TASK_HISTORY_MAX,
    TASK_HISTORY_PATH,
    TASK_LOG_DIR,
    TASK_LOG_MAX,
    WORKDIR,
)

_parse_progress = taskcenter.parse_progress   # 兼容旧引用（测试可调 extensions._parse_progress）
_error_summary = taskcenter.error_summary
KIND_SLUGS = taskcenter.KIND_SLUGS           # 任务 ID 前缀表（ASCII，避免中文进 URL / 文件名）


class TaskManager(taskcenter.TaskManager):
    """绑定本项目数据目录的任务管理器。

    路径走**显式参数注入**（缺省才回落到 settings 的常量），
    这样测试可以指向临时目录而不必 monkeypatch 某个模块的全局变量——
    依赖模块全局会让「到底改的是哪个命名空间」变成隐式契约。
    """

    def __init__(self, history_path=None, log_dir=None, workdir=None,
                 history_max: int | None = None, log_max: int | None = None) -> None:
        super().__init__(
            history_path=history_path if history_path is not None else TASK_HISTORY_PATH,
            log_dir=log_dir if log_dir is not None else TASK_LOG_DIR,
            workdir=workdir if workdir is not None else WORKDIR,
            history_max=history_max if history_max is not None else TASK_HISTORY_MAX,
            log_max=log_max if log_max is not None else TASK_LOG_MAX,
        )


tasks = TaskManager()
