"""前端 Node 自检脚本的门禁化（P2-A）。

`tests/frontend/*.mjs` 里的断言早就写好了，但**只有人手工敲 `node xxx.mjs` 才会跑**
—— pytest 看不见它们，提交前门禁自然也看不见。于是那几组不变量（市场切换的 epoch
守卫、目标 vs 实际对照条的取数口径）在"全绿"的测试报告里其实一次都没被验过。
本文件把每个 .mjs 收成一个参数化用例：子进程跑 node，非零退出即失败。

无 node 的环境 → skip，不算失败：这里要防的是"改了前端却没人发现"，不是"没装 node
就不许提交"。但**脚本目录为空**不跳过——那是 glob 写错或脚本被误删，属于门禁本身失效。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "tests" / "frontend"

# 每个 mjs 都自跑 stub、自带 assert、以 process.exit(failures===0?0:1) 收尾，天然适合当断言。
SCRIPTS = sorted(p.name for p in FRONTEND_DIR.glob("*.mjs"))

NODE = shutil.which("node")


def test_frontend_selfchecks_exist() -> None:
    """守卫：目录被清空或 glob 写错时，下面的参数化会退化成"零个用例全绿"。"""
    assert SCRIPTS, f"{FRONTEND_DIR} 下没有任何 .mjs 自检脚本"


@pytest.mark.skipif(NODE is None, reason="未找到 node，跳过前端自检")
@pytest.mark.parametrize("script", SCRIPTS)
def test_frontend_selfcheck(script: str) -> None:
    path = FRONTEND_DIR / script
    assert path.is_file(), f"自检脚本不存在: {path}"

    proc = subprocess.run(
        [NODE, str(path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    # mjs 的失败明细打进 stdout（✓/✗ 混排），崩溃栈在 stderr，两边都要带出来才排得动
    assert proc.returncode == 0, (
        f"{script} 自检失败（exit={proc.returncode}）"
        f"\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
