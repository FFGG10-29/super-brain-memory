"""core.errors —— 全局异常体系。

约定：所有异常继承 BrainError，便于组合根与网关统一捕获并映射为
MCP / HTTP 错误码；存储层异常向上抛出时不得吞掉原始 cause。
"""
from __future__ import annotations


class BrainError(Exception):
    """所有业务异常的根。"""


class ConfigError(BrainError):
    """配置缺失或非法（如 vector 维度与 manifest 不一致）。"""


class StorageError(BrainError):
    """六引擎基座异常（连接、事务、schema 版本校验失败）。"""


class SchemaVersionMismatch(StorageError):
    """库内自建版本号表与 config 声明不一致时的启动校验失败。"""


class EngineUnavailable(BrainError):
    """算力层模型不可用且降级链已耗尽（如 GGUF 缺失）。"""


class AdmissionRejected(BrainError):
    """丘脑门控拒绝：限流、重复、低显著性。"""


class ConflictError(BrainError):
    """冲突消解阶段无法自动裁决，需进入 Life Loop 复盘队列。"""


class SyncError(BrainError):
    """同步平面异常（git 操作、事件重放、快照一致性）。"""


class GatewayAuthError(BrainError):
    """令牌无效、作用域越权或命名空间策略拒绝。"""
