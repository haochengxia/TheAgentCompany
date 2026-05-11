#!/usr/bin/env python3
"""
Smart parallel scheduler for TheAgentCompany benchmark evaluation.

Strategy:
  - Group tasks by their service dependencies (from dependencies.yml)
  - Tasks in the SAME group run sequentially (they share services, must reset between)
  - Tasks in DIFFERENT groups run in parallel (they use different services)
  - Within each group, snapshot-restore happens between tasks (fast)

Example:
  Group A [gitlab]:          47 tasks -> sequential within group
  Group B [owncloud]:        33 tasks -> sequential within group
  Group A and B run in PARALLEL (no shared services)

Usage:
  python scheduler.py --agent-llm-config agent --env-llm-config env
  python scheduler.py --agent-llm-config agent --env-llm-config env --max-groups 3
  python scheduler.py --tasks "admin-arrange-meeting-rooms,sde-debug-crashed-server"
"""

import argparse
import json
import os
import subprocess
import sys
import time
import yaml
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
TASKS_DIR = Path(__file__).parent.parent / "workspaces" / "tasks"


def load_task_deps(task_dir: Path) -> tuple[str, tuple[str, ...]]:
    """Load task name and its service dependencies."""
    name = task_dir.name
    dep_file = task_dir / "dependencies.yml"
    if dep_file.exists():
        with open(dep_file) as f:
            deps = yaml.safe_load(f) or []
    else:
        deps = []
    return name, tuple(sorted(deps))


def group_tasks_by_deps(task_names: list[str] | None = None) -> dict[tuple[str, ...], list[str]]:
    """Group task names by their dependency tuple."""
    groups = defaultdict(list)
    for task_dir in sorted(TASKS_DIR.iterdir()):
        if not task_dir.is_dir():
            continue
        name = task_dir.name
        if task_names and name not in task_names:
            continue
        _, deps = load_task_deps(task_dir)
        groups[deps].append(name)
    return dict(groups)


def find_non_overlapping_groups(
    groups: dict[tuple[str, ...], list[str]],
    max_groups: int = 4
) -> list[list[tuple[str, ...]]]:
    """
    Partition dependency groups into rounds where no two groups in the same round
    share any service. This maximizes parallelism.

    Uses a greedy graph coloring approach: groups are vertices, edges connect
    groups that share services, rounds are color classes.
    """
    dep_sets = {k: set(k) for k in groups}

    rounds: list[list[tuple[str, ...]]] = []
    assigned: dict[tuple[str, ...], int] = {}

    # Sort groups by size descending (larger groups scheduled first)
    sorted_groups = sorted(groups.keys(), key=lambda g: len(groups[g]), reverse=True)

    for group_key in sorted_groups:
        my_deps = dep_sets[group_key]
        # Find the first round where no existing group shares a service
        placed = False
        for round_idx, round_groups in enumerate(rounds):
            conflict = False
            for existing_key in round_groups:
                if my_deps & dep_sets[existing_key]:
                    conflict = True
                    break
            if not conflict:
                round_groups.append(group_key)
                assigned[group_key] = round_idx
                placed = True
                break
        if not placed:
            rounds.append([group_key])
            assigned[group_key] = len(rounds) - 1

    return rounds


def run_task(
    task_name: str,
    agent_llm_config: str,
    env_llm_config: str,
    outputs_path: str,
    server_hostname: str,
    script_dir: str,
) -> dict:
    """Run a single task and return result info."""
    task_dir = str(TASKS_DIR / task_name)
    start = time.time()

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "poetry", "run", "python",
                str(Path(script_dir) / "run_eval.py"),
                "--task-dir", task_dir,
                "--agent-llm-config", agent_llm_config,
                "--env-llm-config", env_llm_config,
                "--outputs-path", outputs_path,
                "--server-hostname", server_hostname,
            ],
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max per task
            env={**os.environ, "TMPDIR": os.path.join(outputs_path, f".tmp_{task_name}")},
        )
        duration = time.time() - start
        success = result.returncode == 0
        if not success:
            # Write failure marker
            err_path = os.path.join(outputs_path, f"eval_{task_name}.json")
            if not os.path.exists(err_path):
                with open(err_path, "w") as f:
                    json.dump({"error": "task_failed", "exit_code": result.returncode}, f)

        return {
            "task": task_name,
            "success": success,
            "duration": round(duration, 1),
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        err_path = os.path.join(outputs_path, f"eval_{task_name}.json")
        if not os.path.exists(err_path):
            with open(err_path, "w") as f:
                json.dump({"error": "timeout"}, f)
        return {"task": task_name, "success": False, "duration": round(duration, 1), "exit_code": -1}
    except Exception as e:
        duration = time.time() - start
        return {"task": task_name, "success": False, "duration": round(duration, 1), "exit_code": -2, "error": str(e)}


def run_group_sequential(
    group_key: tuple[str, ...],
    task_names: list[str],
    agent_llm_config: str,
    env_llm_config: str,
    outputs_path: str,
    server_hostname: str,
    script_dir: str,
) -> list[dict]:
    """Run all tasks in a group sequentially (they share services)."""
    svc = ", ".join(group_key) if group_key else "(no deps)"
    results = []
    for i, task_name in enumerate(task_names, 1):
        # Check if already done
        eval_file = os.path.join(outputs_path, f"eval_{task_name}.json")
        if os.path.exists(eval_file):
            with open(eval_file) as f:
                data = json.load(f)
            if "error" not in data:
                print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: SKIP (already done)")
                results.append({"task": task_name, "success": True, "duration": 0, "exit_code": 0, "skipped": True})
                continue

        print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: RUNNING...")
        result = run_task(task_name, agent_llm_config, env_llm_config, outputs_path, server_hostname, script_dir)
        status = "OK" if result["success"] else "FAIL"
        print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: {status} ({result['duration']}s)")
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description="Smart parallel benchmark scheduler")
    parser.add_argument("--agent-llm-config", required=True)
    parser.add_argument("--env-llm-config", required=True)
    parser.add_argument("--outputs-path", default="outputs")
    parser.add_argument("--server-hostname", default="localhost")
    parser.add_argument("--max-groups", type=int, default=4, help="Max groups to run in parallel")
    parser.add_argument("--tasks", type=str, default=None, help="Comma-separated task names to run")
    parser.add_argument("--list-groups", action="store_true", help="Print grouping plan and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print execution plan without running")
    args = parser.parse_args()

    outputs_path = os.path.abspath(args.outputs_path)
    os.makedirs(outputs_path, exist_ok=True)

    task_names = None
    if args.tasks:
        task_names = [t.strip() for t in args.tasks.split(",")]

    groups = group_tasks_by_deps(task_names)

    print("=" * 60)
    print("TheAgentCompany V2 - Smart Parallel Scheduler")
    print("=" * 60)
    print(f"Total tasks: {sum(len(v) for v in groups.values())}")
    print(f"Dependency groups: {len(groups)}")
    print(f"Max parallel groups: {args.max_groups}")
    print()

    rounds = find_non_overlapping_groups(groups, args.max_groups)

    print("Execution Plan:")
    total_rounds = len(rounds)
    for r_idx, round_groups in enumerate(rounds, 1):
        parallel_desc = []
        for gk in round_groups:
            svc = "+".join(gk) if gk else "(no deps)"
            parallel_desc.append(f"{svc} ({len(groups[gk])} tasks)")
        print(f"  Round {r_idx}/{total_rounds}: " + " || ".join(parallel_desc))
    print()

    if args.list_groups or args.dry_run:
        print("Groups detail:")
        for gk in sorted(groups.keys(), key=lambda x: len(groups[x]), reverse=True):
            svc = ", ".join(gk) if gk else "(no deps)"
            print(f"  [{svc}] ({len(groups[gk])} tasks): {', '.join(groups[gk][:5])}{'...' if len(groups[gk]) > 5 else ''}")
        if args.dry_run:
            print("\nDry run - no tasks executed.")
        return

    all_results = []
    total_start = time.time()
    completed = 0
    total_tasks = sum(len(groups[gk]) for rk in rounds for gk in rk)

    for r_idx, round_groups in enumerate(rounds, 1):
        print(f"\n{'=' * 60}")
        print(f"Round {r_idx}/{total_rounds} ({len(round_groups)} groups in parallel)")
        print(f"{'=' * 60}")

        round_futures = {}
        with ProcessPoolExecutor(max_workers=min(len(round_groups), args.max_groups)) as executor:
            for gk in round_groups:
                svc = ", ".join(gk) if gk else "(no deps)"
                print(f"  Starting group [{svc}] with {len(groups[gk])} tasks")
                future = executor.submit(
                    run_group_sequential,
                    gk, groups[gk],
                    args.agent_llm_config, args.env_llm_config,
                    outputs_path, args.server_hostname,
                    str(SCRIPT_DIR),
                )
                round_futures[future] = gk

            for future in as_completed(round_futures):
                gk = round_futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    completed += len(results)
                    elapsed = time.time() - total_start
                    rate = completed / elapsed * 60 if elapsed > 0 else 0
                    eta = (total_tasks - completed) / rate if rate > 0 else 0
                    print(f"\n  Progress: {completed}/{total_tasks} ({completed*100//total_tasks}%) "
                          f"| {rate:.1f} tasks/min | ETA: {eta:.0f} min")
                except Exception as e:
                    print(f"  Group {'+'.join(gk)} failed: {e}")

    total_duration = time.time() - total_start
    successes = sum(1 for r in all_results if r["success"])
    failures = sum(1 for r in all_results if not r["success"])
    skipped = sum(1 for r in all_results if r.get("skipped"))

    print(f"\n{'=' * 60}")
    print(f"COMPLETE: {successes} passed, {failures} failed, {skipped} skipped")
    print(f"Total time: {total_duration/60:.1f} min ({total_duration:.0f}s)")
    print(f"Results: {outputs_path}")
    print(f"{'=' * 60}")

    summary_path = os.path.join(outputs_path, "summary.json")
    with open(summary_path, "w") as f:
        json.dump({
            "total": len(all_results),
            "passed": successes,
            "failed": failures,
            "skipped": skipped,
            "duration_seconds": round(total_duration, 1),
            "results": all_results,
        }, f, indent=2)
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
