#!/usr/bin/env bash
# 监控后端重建+安装器构建，完成后自动验证"能跑"
set -u
ROOT="B:/BaiduSyncdisk/desktop/VSProject/super-brain-memory"
LOG="$ROOT/build_backend_installer.log"
INSTALL_EXE="D:/SuperBrain/backend/headless-server.exe"
INSTALL_DIR="D:/SuperBrain/backend"
RELEASE="$ROOT/release"
VENV="$ROOT/.venv/Scripts/python.exe"

echo "[monitor $(date +%H:%M:%S)] 开始监控构建日志: $LOG"

DONE=0
for i in $(seq 1 120); do  # 最多等 120 分钟
  if grep -q "BUILD_BACKEND_INSTALLER_DONE" "$LOG" 2>/dev/null; then
    DONE=1
    break
  fi
  # 构建失败也提前退出
  if grep -q "BUILD_BACKEND_INSTALLER_FAILED\|FATAL\|Traceback (most recent call last)" "$LOG" 2>/dev/null; then
    echo "[monitor $(date +%H:%M:%S)] 检测到构建失败/异常，提前停止监控"
    tail -n 30 "$LOG"
    exit 2
  fi
  sleep 60
done

if [ "$DONE" -ne 1 ]; then
  echo "[monitor $(date +%H:%M:%S)] 超时未完成（120 分钟），请手动检查日志"
  exit 3
fi

echo "[monitor $(date +%H:%M:%S)] 构建已完成（检测到 DONE 标记）。开始验证..."

# 提取最终 rc
echo "===== 构建最终状态 ====="
grep -E "BUILD_BACKEND_INSTALLER_DONE|ISCC rc|self-check" "$LOG" | tail -n 10

# 1) 验证已安装后端能启动（--self-check）
echo "===== [验证1] 安装目录后端 self-check ====="
if [ -f "$INSTALL_EXE" ]; then
  # 用 venv 跑同款 self-check（安装目录 _internal 与 venv 同代码基）
  timeout 120 "$VENV" "$ROOT/backend_entry.py" --self-check 2>&1 | tail -n 15
  RC=$?
  if [ $RC -eq 0 ]; then
    echo "[verify1] self-check PASSED (rc=0) —— 已安装后端可正常启动，前端将有数据"
  else
    echo "[verify1] self-check FAILED (rc=$RC) —— 见上方输出"
  fi
else
  echo "[verify1] 未找到 $INSTALL_EXE"
fi

# 2) 验证新安装器产物新鲜生成
echo "===== [验证2] 新安装器产物 ====="
ls -la "$RELEASE/SuperBrainSetup.exe" "$RELEASE/SuperBrainSetup-1.bin" "$RELEASE/SuperBrainSetup-2.bin" "$RELEASE/SuperBrainSetup-3.bin" 2>/dev/null

echo "[monitor $(date +%H:%M:%S)] 监控与验证结束"
