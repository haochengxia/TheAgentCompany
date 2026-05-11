#!/usr/bin/env python3
"""Aggregate evaluation results into a summary report."""
import json
import os
import sys
import glob
import yaml
from collections import defaultdict
from pathlib import Path

TASKS_DIR = Path(__file__).parent.parent / "workspaces" / "tasks"


def get_task_category(task_name: str) -> str:
    return task_name.split("-")[0]


def get_task_deps(task_name: str) -> list[str]:
    dep_file = TASKS_DIR / task_name / "dependencies.yml"
    if dep_file.exists():
        with open(dep_file) as f:
            return yaml.safe_load(f) or []
    return []


def main():
    if len(sys.argv) < 2:
        print("Usage: python summarize_results.py <outputs_dir>")
        sys.exit(1)

    outputs_dir = sys.argv[1]
    if not os.path.isdir(outputs_dir):
        print(f"Error: {outputs_dir} is not a directory")
        sys.exit(1)

    eval_files = sorted(glob.glob(os.path.join(outputs_dir, "eval_*.json")))
    if not eval_files:
        print("No eval_*.json files found")
        sys.exit(1)

    results = []
    for f in eval_files:
        task_name = os.path.basename(f).replace("eval_", "").replace(".json", "")
        with open(f) as fh:
            data = json.load(fh)
        results.append({"task": task_name, **data})

    total = len(results)
    errored = sum(1 for r in results if "error" in r)
    scored = [r for r in results if "error" not in r]

    total_score = 0
    total_possible = 0
    passed_tasks = 0
    category_stats = defaultdict(lambda: {"score": 0, "possible": 0, "tasks": 0, "passed": 0})
    dep_stats = defaultdict(lambda: {"score": 0, "possible": 0, "tasks": 0, "passed": 0})

    for r in scored:
        checkpoints = r.get("checkpoints", [])
        task_score = sum(cp.get("result", 0) for cp in checkpoints)
        task_possible = sum(cp.get("total", 0) for cp in checkpoints)
        task_passed = task_score == task_possible and task_possible > 0

        total_score += task_score
        total_possible += task_possible
        if task_passed:
            passed_tasks += 1

        cat = get_task_category(r["task"])
        category_stats[cat]["score"] += task_score
        category_stats[cat]["possible"] += task_possible
        category_stats[cat]["tasks"] += 1
        if task_passed:
            category_stats[cat]["passed"] += 1

        for dep in get_task_deps(r["task"]):
            dep_stats[dep]["score"] += task_score
            dep_stats[dep]["possible"] += task_possible
            dep_stats[dep]["tasks"] += 1
            if task_passed:
                dep_stats[dep]["passed"] += 1

    print("=" * 60)
    print(f"RESULTS SUMMARY: {outputs_dir}")
    print("=" * 60)
    print(f"Total tasks: {total}")
    print(f"Scored: {len(scored)} | Errors: {errored}")
    print(f"Overall score: {total_score}/{total_possible} ({total_score*100/max(total_possible,1):.1f}%)")
    print(f"Fully solved: {passed_tasks}/{len(scored)} ({passed_tasks*100/max(len(scored),1):.1f}%)")
    print()

    print("--- By Category ---")
    for cat in sorted(category_stats.keys()):
        s = category_stats[cat]
        pct = s["score"] * 100 / max(s["possible"], 1)
        print(f"  {cat:12s}: {s['score']:3d}/{s['possible']:3d} ({pct:5.1f}%) | {s['passed']}/{s['tasks']} solved")
    print()

    print("--- By Service Dependency ---")
    for dep in sorted(dep_stats.keys()):
        s = dep_stats[dep]
        pct = s["score"] * 100 / max(s["possible"], 1)
        print(f"  {dep:12s}: {s['score']:3d}/{s['possible']:3d} ({pct:5.1f}%) | {s['passed']}/{s['tasks']} tasks")
    print()

    if errored > 0:
        print("--- Failed Tasks ---")
        for r in results:
            if "error" in r:
                print(f"  {r['task']}: {r.get('error', 'unknown')} (exit={r.get('exit_code', '?')})")
        print()

    report_path = os.path.join(outputs_dir, "report.json")
    report = {
        "total_tasks": total,
        "scored_tasks": len(scored),
        "error_tasks": errored,
        "total_score": total_score,
        "total_possible": total_possible,
        "score_pct": round(total_score * 100 / max(total_possible, 1), 1),
        "fully_solved": passed_tasks,
        "by_category": dict(category_stats),
        "by_dependency": dict(dep_stats),
        "failed_tasks": [r["task"] for r in results if "error" in r],
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved to: {report_path}")


if __name__ == "__main__":
    main()
