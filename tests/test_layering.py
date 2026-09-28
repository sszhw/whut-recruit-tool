"""分层与兼容层回归测试 —— 阶段 2 拆分的安全网。

拆分 server.py 的风险有两类，都不是靠"跑一遍首页"能发现的：

1. **依赖方向被破坏**：settings 是全项目依赖终点，一旦它 import 了 analyze /
   crawler / repository，就会把 requests 拖进配置层，并且等 repository 反过来
   import settings（它迟早要拿 DATA 路径）时直接成环。
2. **兼容层悄悄失效**：`server.__all__` 里挂着一批旧入口，测试与脚本仍在用。
   拆分后实现搬走了，只要漏重导出一个，导入时才会炸——那时已经是运行时。

这里把两件事都固化成断言。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import server

APP_DIR = Path(server.__file__).resolve().parent

# settings 允许牵连的模块：只允许标准库 + providers + utils
_FORBIDDEN = ("requests", "flask", "bs4", "crawler", "analyze", "repository", "resume", "taskcenter")


def test_settings_has_no_business_dependency():
    """在干净子进程里 import settings，确认没有业务模块/网络库被牵连进来。

    必须开子进程：本测试进程里 pytest 早已把 server、requests 等全部导入，
    在进程内看 sys.modules 永远都是"已被污染"的状态，检查不出问题。
    """
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(APP_DIR)!r});"
        "import settings;"
        f"bad=[m for m in {list(_FORBIDDEN)!r} if m in sys.modules];"
        "print('POLLUTED:' + ','.join(bad) if bad else 'CLEAN')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"import settings 失败：{out.stderr}"
    assert "CLEAN" in out.stdout, f"settings 牵连了业务模块：{out.stdout.strip()}"


def test_default_model_single_source():
    """默认模型名只能有一处定义，否则同一份配置在分析与推荐里会用到不同模型。"""
    import settings
    from analyze import DEFAULT_MODEL as analyze_model
    from resume import DEFAULT_MODEL as resume_model

    assert analyze_model == settings.DEFAULT_MODEL == resume_model


def test_server_compat_exports_resolve():
    """server.__all__ 里声明的每个旧入口都必须真的取得到。"""
    missing = [name for name in server.__all__ if not hasattr(server, name)]
    assert not missing, f"server 声明了但已失效的兼容导出：{missing}"


def test_no_api_module_imports_server():
    """蓝图不得反向 import server——那会让 api → server → api 成环。"""
    for path in sorted((APP_DIR / "api").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            line = line.strip()
            if line.startswith(("import server", "from server")):
                raise AssertionError(f"{path.name} 反向依赖 server：{line}")
