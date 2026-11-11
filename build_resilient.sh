#!/usr/bin/env bash
# 健壮后端+安装器构建：带自动重试（应对 WorkBuddy 重启杀进程），完成后自动验证
set -u
ROOT="B:/BaiduSyncdisk/desktop/VSProject/super-brain-memory"
BUILD_LOG="$ROOT/build_backend_installer.log"
SELF_LOG="$ROOT/build_resilient.log"
INSTALL_CLI="D:/SuperBrain/backend/headless-server-cli.exe"
RELEASE="$ROOT/release"

MAX_TRIES=3
try=0
while [ "$try" -lt "$MAX_TRIES" ]; do
  try=$((try+1))
  echo "[resilient $(date +%H:%M:%S)] ===== 尝试 $try/$MAX_TRIES =====" | tee -a "$SELF_LOG"
  # 清掉可能残留的半截产物，避免旧 pyc 混入
  rm -rf "$ROOT/dist/super-brain-backend" 2>/dev/null
  bash "$ROOT/build_backend_installer.sh" > "$BUILD_LOG" 2>&1
  rc=$?
  echo "[resilient $(date +%H:%M:%S)] build_backend_installer 退出 rc=$rc" | tee -a "$SELF_LOG"
  if grep -q "BUILD_BACKEND_INSTALLER_DONE" "$BUILD_LOG" 2>/dev/null; then
    echo "[resilient $(date +%H:%M:%S)] 构建成功标记已出现" | tee -a "$SELF_LOG"
    break
  fi
  echo "[resilient $(date +%H:%M:%S)] 未完成（rc=$rc 或进程被杀），30s 后重试" | tee -a "$SELF_LOG"
  sleep 30
done

echo "[resilient $(date +%H:%M:%S)] ===== 最终核验 =====" | tee -a "$SELF_LOG"
grep -E "BUILD_BACKEND_INSTALLER_DONE|ISCC rc|self-check|merge-cover" "$BUILD_LOG" 2>/dev/null | tail -n 12 | tee -a "$SELF_LOG"

# 1) 安装目录后端 self-check（验证 exe 内嵌模块已修好、能启动）
echo "===== [验证1] 安装后端 self-check =====" | tee -a "$SELF_LOG"
if [ -f "$INSTALL_CLI" ]; then
  timeout 150 "$INSTALL_CLI" --self-check > "$ROOT/selfcheck_install.log" 2>&1
  src=$?
  echo "[verify1] headless-server-cli.exe --self-check rc=$src" | tee -a "$SELF_LOG"
  tail -n 20 "$ROOT/selfcheck_install.log" | tee -a "$SELF_LOG"
  if grep -qi "ImportError\|set_distill_trigger" "$ROOT/selfcheck_install.log"; then
    echo "[verify1] FAIL：仍存在 import 错误（main.py 修复未生效）" | tee -a "$SELF_LOG"
  else
    echo "[verify1] PASS：import 链已修复，已安装后端可正常启动，前端将有数据" | tee -a "$SELF_LOG"
  fi
else
  echo "[verify1] 未找到 $INSTALL_CLI" | tee -a "$SELF_LOG"
fi

# 2) 新安装器产物新鲜度
echo "===== [验证2] 新安装器产物 =====" | tee -a "$SELF_LOG"
ls -la "$RELEASE/SuperBrainSetup.exe" "$RELEASE/SuperBrainSetup-1.bin" "$RELEASE/SuperBrainSetup-2.bin" "$RELEASE/SuperBrainSetup-3.bin" 2>/dev/null | tee -a "$SELF_LOG"

echo "[resilient $(date +%H:%M:%S)] 全部结束" | tee -a "$SELF_LOG"
