"""run_all.py — 统一运行 verify / demo / benchmark，产出 .pointwise-runtime.json。

运行：python3 -I run_all.py
所有子任务均以 `python3 -I`（隔离模式）启动，stdout/stderr 落到 logs/。
"""

import json
import os
import platform
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
MANIFEST = os.path.join(HERE, ".pointwise-runtime.json")

TASKS = [
    {
        "name": "verify",
        "script": "verify.py",
        "description": "确定性用例 + 400 轮随机差分 + 不可变性检查",
        "kind": "test",
    },
    {
        "name": "demo",
        "script": "demo.py",
        "description": "流式日志中随时启停规则的真实演示",
        "kind": "demo",
    },
    {
        "name": "benchmark",
        "script": "benchmark.py",
        "description": "构建/启停/扫描/状态稳定性规模实测，输出 JSON",
        "kind": "benchmark",
    },
]


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    py = sys.executable
    manifest = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z") or
                        time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": {
            "executable": py,
            "version": sys.version,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "isolated_mode_flag": "-I",
        "tasks": [],
    }
    all_ok = True
    for task in TASKS:
        script = os.path.join(HERE, task["script"])
        stdout_path = os.path.join(LOG_DIR, task["name"] + ".out.log")
        stderr_path = os.path.join(LOG_DIR, task["name"] + ".err.log")
        t0 = time.perf_counter()
        with open(stdout_path, "w", encoding="utf-8") as out_f, \
             open(stderr_path, "w", encoding="utf-8") as err_f:
            proc = subprocess.run(
                [py, "-I", script],
                stdout=out_f, stderr=err_f, cwd=HERE,
            )
        wall = time.perf_counter() - t0
        entry = {
            "name": task["name"],
            "kind": task["kind"],
            "description": task["description"],
            "command": f"{os.path.basename(py)} -I {task['script']}",
            "returncode": proc.returncode,
            "status": "passed" if proc.returncode == 0 else "failed",
            "wall_clock_sec": round(wall, 4),
            "stdout_log": os.path.relpath(stdout_path, HERE),
            "stderr_log": os.path.relpath(stderr_path, HERE),
        }
        if task["name"] == "benchmark" and proc.returncode == 0:
            with open(stdout_path, encoding="utf-8") as f:
                entry["benchmark_json"] = json.load(f)
        manifest["tasks"].append(entry)
        all_ok = all_ok and proc.returncode == 0
        print(f"[{'PASS' if proc.returncode == 0 else 'FAIL'}] {task['name']} "
              f"({wall:.2f}s) -> {entry['stdout_log']}")

    manifest["overall_status"] = "passed" if all_ok else "failed"
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\nmanifest -> {os.path.relpath(MANIFEST, HERE)} "
          f"[{manifest['overall_status']}]")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
