# -*- coding: utf-8 -*-
"""test_lite_backend.py — 隔离环境实测精简包 frozen 后端（缺 onnxruntime 等能否启动）"""
import os
import subprocess
import time
import urllib.request

env = dict(os.environ)
env.update({
    "SUPER_BRAIN_DATA_DIR": r"D:\SuperBrain\test-data",
    "GW_HTTP_PORT": "18876",
    "GW_MCP_PORT": "18865",
    "CHAT_PORT": "18869",
})
proc = subprocess.Popen(
    [r"D:\SuperBrain\test-install\backend\headless-server.exe"],
    cwd=r"D:\SuperBrain\test-install\backend", env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW)
ok = False
for _ in range(30):
    time.sleep(1)
    try:
        r = urllib.request.urlopen("http://127.0.0.1:18876/health", timeout=4)
        print("精简包 frozen 后端 18876 health:", r.status, r.read()[:80])
        ok = True
        break
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:100]}"
print("启动验证:", "PASS" if ok else f"FAIL（{err}）")
time.sleep(1)
proc.terminate()
try:
    proc.wait(timeout=8)
except Exception:
    subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
print("测试实例已停")
