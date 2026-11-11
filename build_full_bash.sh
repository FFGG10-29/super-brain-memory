#!/usr/bin/env bash
# 全量构建：Flutter Release -> 冻结后端 -> dist\ 组装 -> ISCC 安装器
# 用 bash 跑，绕开 build_full_release.ps1 中 $ErrorActionPreference='Stop' 把 PyInstaller stderr 当致命错误的坑。
set -u
export PATH="/d/flutter/bin:$PATH"
cd "B:/BaiduSyncdisk/desktop/VSProject/super-brain-memory" || exit 1
ROOT="$PWD"
BAD=0

run(){
  echo "=== [$(date +%H:%M:%S)] $1 ==="
  shift
  "$@"
  local rc=$?
  if [ $rc -ne 0 ]; then echo "FAILED($1) rc=$rc"; BAD=1; fi
  return $rc
}

run "Flutter pub get" bash -c 'cd flutter_app && flutter pub get'
[ $BAD -eq 0 ] && run "Flutter analyze" bash -c 'cd flutter_app && flutter analyze'
[ $BAD -eq 0 ] && run "Flutter build windows --release" bash -c 'cd flutter_app && flutter build windows --release'
[ $BAD -eq 0 ] && run "Backend spec+stage" .venv/Scripts/python.exe scripts/build_portable.py --headless
[ $BAD -eq 0 ] && run "PyInstaller" .venv/Scripts/python.exe -m PyInstaller super-brain-backend.spec --noconfirm

if [ $BAD -eq 0 ]; then
  run "trim" .venv/Scripts/python.exe scripts/build_portable.py --trim dist/super-brain-backend
  run "link-models" .venv/Scripts/python.exe scripts/build_portable.py --link-models dist/super-brain-backend
fi

# 组装 dist\：Flutter 在根，后端在 backend\，图标两份
if [ $BAD -eq 0 ]; then
  echo "=== [$(date +%H:%M:%S)] Assemble dist ==="
  mv dist/super-brain-backend build/backend_stage
  rm -rf dist && mkdir -p dist
  robocopy "flutter_app/build/windows/x64/runner/Release" dist /E /NFL /NDL /NJH /NJS /NP >/dev/null 2>&1
  RC=$?; if [ $RC -ge 8 ]; then echo "ROBOCOPY_FLUTTER_FAILED rc=$RC"; BAD=1; fi
  mkdir -p dist/backend
  robocopy build/backend_stage dist/backend /E /NFL /NDL /NJH /NJS /NP >/dev/null 2>&1
  RC=$?; if [ $RC -ge 8 ]; then echo "ROBOCOPY_BACKEND_FAILED rc=$RC"; BAD=1; fi
  rm -rf build/backend_stage
  if [ -f "flutter_app/windows/runner/resources/app_icon.ico" ]; then
    cp "flutter_app/windows/runner/resources/app_icon.ico" dist/app.ico
    cp "flutter_app/windows/runner/resources/app_icon.ico" dist/backend/app.ico
    echo "icon copied"
  else
    echo "WARN icon missing"
  fi
fi

# ISCC 编译安装器
if [ $BAD -eq 0 ]; then
  echo "=== [$(date +%H:%M:%S)] ISCC compile installer ==="
  ISCC="/c/Users/13966/AppData/Local/Programs/Inno Setup 6/ISCC.exe"
  "$ISCC" scripts/installer/setup.iss
  RC=$?
  if [ $RC -ne 0 ]; then echo "ISCC_FAILED rc=$RC"; BAD=1; fi
fi

if [ $BAD -eq 0 ]; then
  echo "BUILD_ALL_OK"
  ls -la --time-style=+%H:%M:%S release/SuperBrainSetup.exe 2>/dev/null
else
  echo "BUILD_ALL_FAILED"
fi
