"""通用工具包。

约束：本包内的模块**不得** import app 下的业务模块（crawler / repository / analyze …），
只依赖标准库，避免出现循环导入。业务模块可以依赖 utils，反之不行。
"""

from __future__ import annotations
