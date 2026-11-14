"""PyInstaller 冻结入口：无参数启动无头后端；带 --self-check / --probe-models 走 CLI。"""
from __future__ import annotations

import sys


def _cli(argv: list[str]) -> int | None:
    as_json = "--json" in argv
    import main as cli

    if "--probe-models" in argv:
        return cli.probe_models(as_json=as_json)
    if "--self-check" in argv:
        return cli.self_check(as_json=as_json)
    if "--mcp-stdio" in argv:
        from gateway.mcp_stdio import main as mcp_main

        return mcp_main()
    return None


def main() -> int:
    code = _cli(sys.argv[1:])
    if code is not None:
        return code
    from headless_server import main as serve_main

    return serve_main(sys.argv[1:])


raise SystemExit(main())
