@echo off
chcp 65001 >nul
REM =====================================================================
REM  超级大脑 dev 态启动（启用 cognition 认知契约）
REM
REM  用途：B 盘源码改动无需 PyInstaller 重新打包即可生效。
REM  本次改动：brain/router_extractor.py、brain/hippocampus.py、
REM            gateway/mcp_server.py、gateway/tool_catalog.py、
REM            gateway/http_adapter.py、gateway/mcp_stdio.py
REM
REM  前置：先停掉 D:\SuperBrain\backend\headless-server.exe（frozen 旧版），
REM        否则 8766 端口冲突，且 Kuzu 单写者会抢锁。
REM  数据：沿用 D:\Super-Brain\Data，与 frozen 版同一份，无需迁移。
REM  模型：复用安装包内权重，不重复占磁盘。
REM  令牌：仍读 %APPDATA%\super-brain\mcp_token.txt，mcp.json / 插件无需改。
REM
REM  --no-circadian：.venv 未装 apscheduler，关闭昼夜调度（不影响认知链路）。
REM =====================================================================
setlocal
set "ROOT=B:\BaiduSyncdisk\desktop\VSProject\super-brain-memory"
set "SUPER_BRAIN_DATA_DIR=D:\Super-Brain\Data"
set "SUPER_BRAIN_MODELS_DIR=D:\SuperBrain\backend\_internal\models"
set "SUPER_BRAIN_RESOURCE_DIR=D:\SuperBrain\backend\_internal"
set "PYTHONPATH=%ROOT%"
cd /d "%ROOT%"
"%ROOT%\.venv\Scripts\python.exe" -m headless_server --no-circadian
endlocal
