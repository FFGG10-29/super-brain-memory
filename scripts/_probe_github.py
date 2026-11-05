"""一次性：探测 GitHub 凭据（env / gh CLI / 项目钥匙串）。打印结论，绝不打印明文。跑完即删。"""
from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

results: list[str] = []

for name in ("GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN"):
    value = os.environ.get(name, "")
    results.append(f"env {name}: {'present(len=%d)' % len(value) if value else 'missing'}")

if shutil.which("gh"):
    results.append("gh CLI: present")
else:
    results.append("gh CLI: missing")

try:
    import keyring  # noqa: PLC0415

    pat = keyring.get_password("super-brain-v21", "github_pat")
    results.append(f"keyring github_pat: {'present(len=%d)' % len(pat) if pat else 'missing'}")
except Exception as exc:  # noqa: BLE001
    results.append(f"keyring: error {exc}")

# 凭据管理器里是否有 github.com 条目（不打印内容）
try:
    import subprocess  # noqa: PLC0415

    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=15,
    )
    has_cred = "password=" in proc.stdout
    results.append(f"git credential manager github.com: {'present' if has_cred else 'missing'}")
except Exception as exc:  # noqa: BLE001
    results.append(f"git credential: error {exc}")

print("\n".join(results))
