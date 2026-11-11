"""gateway.account_pool —— 号池网关 Python 重写（v4.1 / §16.14 裁决）。

聚合本机 WorkBuddy 登录账号（可扩 xdpool 快照）为多账号额度池，对外暴露与
`gateway.model_router.ModelClient` 兼容的 `PoolClient`。端点、头与指纹常量照抄
dsh-xdpool v2.6.4 / dsh-connect-workbuddy v5.21.3（均 MIT）。
"""

__all__ = ["AccountPool", "AccountPoolError", "PoolClient", "WorkBuddyCredential"]

from gateway.account_pool.pool import AccountPool, AccountPoolError, PoolClient
from gateway.account_pool.store import WorkBuddyCredential
