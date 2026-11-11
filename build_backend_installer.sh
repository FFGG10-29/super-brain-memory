#!/usr/bin/env bash
# 后端冻结 + 组装 dist\ + ISCC 安装器（不含 flutter，前端已单独打好）
set -u
ROOT="B:/BaiduSyncdisk/desktop/VSProject/super-brain-memory"
VENV="$ROOT/.venv/Scripts/python.exe"
SPEC="$ROOT/super-brain-backend.spec"
DIST="$ROOT/dist"
BACKEND_BUNDLE="$DIST/super-brain-backend"
FLUTTER_REL="$ROOT/flutter_app/build/windows/x64/runner/Release"
ISS="$ROOT/scripts/installer/setup.iss"
ISCC="C:/Users/13966/AppData/Local/Programs/Inno Setup 6/ISCC.exe"

step() { echo "===== STEP $1 : $2 @ $(date +%H:%M:%S) ====="; }

# 1) 重生成 spec
step 1 "regenerate spec"
"$VENV" "$ROOT/scripts/build_portable.py" --headless
grep -n "INCLUDE_MODELS" "$SPEC" | head -1

# 2) PyInstaller 冻结后端（独享内存）
step 2 "PyInstaller"
"$VENV" -m PyInstaller "$SPEC" --noconfirm
RC=$?
echo "PYINSTALLER_RC=$RC"
[ "$RC" -ne 0 ] && { echo "PYINSTALLER FAILED"; exit 1; }

# 3) trim
step 3 "trim"
"$VENV" "$ROOT/scripts/build_portable.py" --trim "$BACKEND_BUNDLE"
# 3.5) 合并覆盖到安装目录（保留安装目录既有 _internal/models，避免重拷 3.3GB）
step 3.5 "merge-copy to install (keep models)"
INSTALL_BACKEND="D:/SuperBrain/backend"
mkdir -p "$INSTALL_BACKEND"
cp -f "$BACKEND_BUNDLE/headless-server.exe" "$BACKEND_BUNDLE/headless-server-cli.exe" "$BACKEND_BUNDLE/app.ico" "$INSTALL_BACKEND/" 2>/dev/null
cp -r "$BACKEND_BUNDLE/_internal/." "$INSTALL_BACKEND/_internal/"
if [ -d "$INSTALL_BACKEND/_internal/models" ]; then echo "install models kept: $(du -sh "$INSTALL_BACKEND/_internal/models" | cut -f1)"; else echo "WARN install models missing -> 需 link-models 进安装目录"; fi
echo "--- 安装后自检（确认不再 ImportError，用控制台版 cli）---"
SC_OUT=$(timeout 180 "$INSTALL_BACKEND/headless-server-cli.exe" --self-check 2>&1)
echo "$SC_OUT" | tail -25
if echo "$SC_OUT" | grep -qi "ImportError\|set_distill_trigger"; then echo "SELF-CHECK STILL FAILS"; else echo "self-check passed (no import error)"; fi
# 4) link-models（仅给安装器产物用，不影响已安装目录）
step 4 "link-models"
"$VENV" "$ROOT/scripts/build_portable.py" --link-models "$BACKEND_BUNDLE"

# 5) 组装 dist\（先拷前端根，再把后端包改名成 backend\，绝不先删 dist）
step 5 "assemble dist"
mkdir -p "$DIST"
echo "--- copy flutter release -> dist root ---"
cp -r "$FLUTTER_REL/." "$DIST/"
echo "--- rename backend bundle -> dist/backend ---"
rm -rf "$DIST/backend"
mv "$BACKEND_BUNDLE" "$DIST/backend"
ICON="$ROOT/flutter_app/windows/runner/resources/app_icon.ico"
if [ -f "$ICON" ]; then cp -f "$ICON" "$DIST/app.ico"; cp -f "$ICON" "$DIST/backend/app.ico"; echo "icon copied"; else echo "WARN icon missing"; fi
echo "--- dist tree ---"
ls -la "$DIST" | head
ls -la "$DIST/backend/headless-server.exe" 2>&1

# 6) ISCC 安装器（Git Bash 直接执行 exe，路径用 cygpath 转 Windows 格式）
step 6 "ISCC installer"
ISS_WIN=$(cygpath -w "$ISS")
"$ISCC" "$ISS_WIN"
RC2=$?
echo "ISCC_RC=$RC2"

# 7) 核验
step 7 "verify"
echo "--- release ---"
ls -la --time-style=+%H:%M:%S "$ROOT/release" 2>&1 | head
echo "BUILD_BACKEND_INSTALLER_DONE rc=$RC2"
