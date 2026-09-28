"""业务规则层。

分层职责（自上而下单向依赖）：

    api/*.py        HTTP 壳：取参数、捕获 ServiceError、拼 JSON
      └─ services/  业务规则：筛选 / 匹配 / 聚合 / 编排，**不认识 Flask**
           └─ dataloaders/  取数与派生
                └─ repository / settings

为什么要有这一层：`api/` 里的函数原先同时做「读 request.form」「跑匹配规则」
「拼 jsonify」，导致同一条规则换个入口（比如定时任务、CLI）就得复制一遍。
下沉之后业务规则不依赖 Flask，可以脱离 Web 单测与复用。

依赖方向约束：services **不得** import `api` 或 `server`，也不得 import `flask`。
`tests/test_layering.py` 会检查这一点。
"""

from __future__ import annotations


class ServiceError(Exception):
    """业务层可预期的失败：参数缺失、无数据、外部服务不可用。

    业务层不知道 HTTP 状态码，路由也不该自己判断业务规则——
    两者靠这一个异常类型解耦。需要非 400 的响应时传 `status`。

    与「未捕获异常」的区别：ServiceError 是**预期内**的错误分支，
    不该进服务日志的 error 通道，也不该带堆栈。
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
