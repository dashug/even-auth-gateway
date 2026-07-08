"""治理服务日志接线。

问题(设计评审·ops):uvicorn 只配了自己的 logger,`even_auth_gov.*` 的 INFO/WARNING
默认没 handler → 审批/离职/禁用失败**全静默**,一个安全关键服务的审计轨迹丢失。
这里给 even_auth_gov 包 logger 挂一个 stdout handler(容器日志可采集),幂等。
级别由 SSO_LOG_LEVEL 控(默认 INFO)。
"""
from __future__ import annotations
import logging
import os
import sys

_CONFIGURED = False


def setup() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logger = logging.getLogger("even_auth_gov")
    logger.setLevel(os.getenv("SSO_LOG_LEVEL", "INFO").upper())
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(h)
        logger.propagate = False  # 不重复冒泡到 root(避免与 uvicorn root handler 双打)
    _CONFIGURED = True
