"""Run the Anamnesis smoke test suite."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SMOKE_TARGETS = [
    "tests/smoke/test_graph_memory_smoke.py",
    "tests/integration/test_full_workflow.py::test_recall_reflection_and_search_workflow",
    "tests/integration/test_real_db_end_to_end.py::test_normal_message_pipeline_with_real_database",
    "tests/integration/test_real_db_end_to_end.py::test_recall_injection_with_real_database",
]


def _runner() -> list[str]:
    """优先用 uv，没装时退回当前解释器，避免脚本在干净环境里直接崩掉。"""
    if shutil.which("uv"):
        return ["uv", "run", "pytest"]
    return [sys.executable, "-m", "pytest"]


def main() -> int:
    plugin_root = Path(__file__).resolve().parents[1]
    # 测试用 astrbot_plugin_anamnesis.* 绝对导入，必须在插件目录的上一层运行。
    workspace_root = plugin_root.parent
    prefix = plugin_root.name
    targets = [f"{prefix}/{target}" for target in SMOKE_TARGETS]
    cmd = [*_runner(), *targets, *sys.argv[1:]]
    print("Running smoke suite:")
    for target in SMOKE_TARGETS:
        print(f"- {target}")
    completed = subprocess.run(cmd, cwd=workspace_root)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
