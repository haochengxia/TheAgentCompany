#!/usr/bin/env python3
"""
Smart parallel scheduler for TheAgentCompany benchmark evaluation.

Strategy:
  - Group tasks by their service dependencies (from dependencies.yml)
  - Tasks in the SAME group run sequentially (they share services, must reset between)
  - Tasks in DIFFERENT groups run in parallel (they use different services)
  - Within each group, snapshot-restore happens between tasks (fast)
  - With --num-instances N > 1, gitlab tasks are distributed across N instances

Multi-instance support:
  - Instance 0: full stack (gitlab + rocketchat + owncloud + plane)
  - Instance 1..N: gitlab only
  - Gitlab-only tasks can use any instance; tasks needing other services use instance 0
  - The scheduler acquires/releases instances to avoid conflicts

Reset strategy:
  - Instance 0: uses api-server HTTP API (fast, in-process reset)
  - Instance 1+: uses `docker restart` (faster than stop/rm/create since
    gitlab data is baked into the image; a restart = fresh state)

Example:
  python scheduler.py --agent-llm-config agent --env-llm-config env
  python scheduler.py --agent-llm-config agent --env-llm-config env --max-groups 3
  python scheduler.py --agent-llm-config agent --env-llm-config env --num-instances 2
  python scheduler.py --tasks "admin-arrange-meeting-rooms,sde-debug-crashed-server"
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
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
        placed = False
        for r_idx, rnd in enumerate(rounds):
            # Check if this group overlaps with any group already in this round
            if all(not (dep_sets[group_key] & dep_sets[existing]) for existing in rnd):
                rnd.append(group_key)
                assigned[group_key] = r_idx
                placed = True
                break
        if not placed:
            rounds.append([group_key])
            assigned[group_key] = len(rounds) - 1

    return rounds


class MockConfig:
    """Encapsulates mock mode configuration. Immutable once created."""

    def __init__(self, enabled: bool = False, duration_range: tuple[float, float] = (10, 30)):
        self.enabled = enabled
        self.duration_range = duration_range

    def __bool__(self) -> bool:
        return self.enabled


# Module-level singleton — set once in main() when --mock is parsed.
# Functions read from this via _get_mock_config() so the rest of the module
# doesn't need to thread the config through every call.
_MOCK_CONFIG = MockConfig()


def _get_mock_config() -> MockConfig:
    """Return the current mock config (module-level singleton)."""
    return _MOCK_CONFIG


def run_task(
    task_name: str,
    agent_llm_config: str,
    env_llm_config: str,
    outputs_path: str,
    server_hostname: str,
    script_dir: str,
    harness: str = "openhands",
    base_image: str | None = None,
    service_instance: dict | None = None,
) -> dict:
    """Run a single task and return result info."""
    mock = _get_mock_config()
    task_dir = str(TASKS_DIR / task_name)
    start = time.time()

    tmpdir = os.path.join(outputs_path, f".tmp_{task_name}")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        if mock.enabled:
            # Mock mode: use run_eval_mock.py with fast durations
            lo, hi = mock.duration_range
            cmd = [
                sys.executable,
                str(Path(script_dir) / "run_eval_mock.py"),
                "--task-dir", task_dir,
                "--outputs-path", outputs_path,
                "--min-duration", str(lo),
                "--max-duration", str(hi),
            ]
            if service_instance:
                cmd += ["--service-instance", json.dumps(service_instance)]
        else:
            cmd = [
                sys.executable, "-m", "poetry", "run", "python",
                str(Path(script_dir) / "run_eval.py"),
                "--task-dir", task_dir,
                "--agent-llm-config", agent_llm_config,
                "--env-llm-config", env_llm_config,
                "--outputs-path", outputs_path,
                "--server-hostname", server_hostname,
                "--harness", harness,
            ]
            if base_image:
                cmd += ["--base-image", base_image]
            if service_instance:
                cmd += ["--service-instance", json.dumps(service_instance)]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5400,  # 90 min max per task
            env={**os.environ, "TMPDIR": tmpdir},
        )
        duration = time.time() - start
        success = result.returncode == 0
        if not success:
            _write_error_marker(outputs_path, task_name, result)

        return {
            "task": task_name,
            "success": success,
            "duration": round(duration, 1),
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        _write_error_marker(outputs_path, task_name, error="timeout")
        return {"task": task_name, "success": False, "duration": round(duration, 1), "exit_code": -1}
    except Exception as e:
        duration = time.time() - start
        return {"task": task_name, "success": False, "duration": round(duration, 1), "exit_code": -2, "error": str(e)}


def _write_error_marker(outputs_path: str, task_name: str,
                        result: subprocess.CompletedProcess | None = None,
                        error: str = "task_failed"):
    """Write an error JSON file so the scheduler can detect failures on re-run."""
    err_path = os.path.join(outputs_path, f"eval_{task_name}.json")
    if os.path.exists(err_path):
        return
    data: dict = {"error": error}
    if result is not None:
        data["exit_code"] = result.returncode
        data["stderr_tail"] = result.stderr[-500:] if result.stderr else ""
        data["stdout_tail"] = result.stdout[-500:] if result.stdout else ""
    with open(err_path, "w") as f:
        json.dump(data, f)


def run_group_sequential(
    group_key: tuple[str, ...],
    task_names: list[str],
    agent_llm_config: str,
    env_llm_config: str,
    outputs_path: str,
    server_hostname: str,
    script_dir: str,
    harness: str = "openhands",
    base_image: str | None = None,
    service_instance: dict | None = None,
) -> list[dict]:
    """Run all tasks in a group sequentially (they share services)."""
    mock = _get_mock_config()
    svc = ", ".join(group_key) if group_key else "(no deps)"
    results = []
    for i, task_name in enumerate(task_names, 1):
        # Check if already done (skip tasks with valid results, re-run errors)
        eval_file = os.path.join(outputs_path, f"eval_{task_name}.json")
        if os.path.exists(eval_file):
            with open(eval_file) as f:
                data = json.load(f)
            if "error" not in data:
                score = data.get("final_score", {})
                s = f"{score.get('result', '?')}/{score.get('total', '?')}"
                print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: SKIP (score={s})")
                results.append({"task": task_name, "success": True, "duration": 0, "exit_code": 0, "skipped": True})
                continue
            else:
                err = data.get("error", "unknown")[:60]
                print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: RE-RUN (prev error: {err})")

        # Reset services before each task (skip first task and mock mode)
        if service_instance and i > 1 and not mock.enabled:
            _reset_services_for_group(service_instance, group_key)

        print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: RUNNING...")
        result = run_task(task_name, agent_llm_config, env_llm_config, outputs_path,
                          server_hostname, script_dir, harness, base_image, service_instance)
        status = "OK" if result["success"] else "FAIL"
        print(f"  [{svc}] [{i}/{len(task_names)}] {task_name}: {status} ({result['duration']}s)")
        results.append(result)
    return results


def _reset_services_for_group(instance_info: dict, group_key: tuple[str, ...]):
    """Reset services needed by a group.

    Instance 0 (has api-server): HTTP POST to /api/reset-{service}.
    Instance 1+ (gitlab-only): `docker restart` — fast because gitlab data is
    baked into the image, so restarting the container restores fresh state
    without the overhead of stop/rm/create.
    """
    inst_id = instance_info.get("instance_id", 0) if instance_info else 0
    hostname = instance_info.get("hostname", "localhost") if instance_info else "localhost"

    if inst_id == 0:
        _reset_via_api_server(instance_info, hostname, group_key)
    else:
        _reset_via_docker_restart(inst_id, group_key)

    # Wait for gitlab to become healthy after reset
    if "gitlab" in group_key:
        gitlab_port = instance_info.get("gitlab_port", 8929) if instance_info else 8929
        _wait_for_gitlab_healthy(hostname, gitlab_port)


def _reset_via_api_server(instance_info: dict, hostname: str, group_key: tuple[str, ...]):
    """Reset services using the api-server HTTP endpoint (instance 0 only)."""
    api_port = instance_info.get("api_port", 2999) if instance_info else 2999
    for service in group_key:
        url = f"http://{hostname}:{api_port}/api/reset-{service}"
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=120) as resp:
                print(f"    Reset {service} (api-server): {resp.read().decode()[:100]}")
        except Exception as e:
            print(f"    Reset {service} via api-server failed: {e}")


def _reset_via_docker_restart(inst_id: int, group_key: tuple[str, ...]):
    """Reset services using `docker restart` (instances 1+).

    Since gitlab's data is baked into the Docker image, restarting the
    container effectively restores a clean state. This is much faster than
    stop + rm + compose up because:
      - No image pull check (compose up checks even with pull_policy: missing)
      - No container creation overhead
      - Container config is preserved
    """
    for service in group_key:
        container_name = f"{service}-{inst_id}"
        try:
            print(f"    Reset {service} (docker restart): {container_name}...")
            subprocess.run(
                ["docker", "restart", container_name],
                capture_output=True, timeout=60,
            )
            print(f"    Reset {service} (docker restart): done")
        except Exception as e:
            print(f"    Reset {service} via docker restart failed: {e}")


def _wait_for_gitlab_healthy(hostname: str, port: int, timeout: int = 120):
    """Poll gitlab until it responds to HTTP requests."""
    url = f"http://{hostname}:{port}/"
    deadline = time.time() + timeout
    print(f"    Waiting for gitlab:{port} to become healthy...", end="", flush=True)
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    elapsed = time.time() - (deadline - timeout)
                    print(f" OK ({elapsed:.0f}s)")
                    return
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(5)
    print(f" TIMEOUT after {timeout}s")


def _split_groups_across_instances(
    round_groups: list[tuple[str, ...]],
    groups: dict[tuple[str, ...], list[str]],
    instance_manager_ref: dict,
) -> dict[tuple[tuple[str, ...], int | None], tuple[list[str], dict | None]]:
    """Assign groups to instances with load balancing.

    Tracks total task count per instance per round and assigns
    each group to the lightest-loaded matching instance.
    Gitlab-only groups prefer non-zero instances.
    """
    result = {}
    mgr = instance_manager_ref.get("manager")
    load: dict[int, int] = {}

    for gk in round_groups:
        task_names = groups[gk]
        svc_label = "+".join(gk) if gk else "no-deps"
        services_needed = list(gk) if gk else []

        if mgr is None:
            result[(gk, None)] = (task_names, None)
            continue

        is_gitlab_only = services_needed == ["gitlab"]

        matching = [
            iid for iid in mgr.instances
            if all(svc in mgr.instances[iid].services for svc in services_needed)
        ]

        if is_gitlab_only:
            non_zero = sorted([i for i in matching if i != 0], key=lambda i: load.get(i, 0))
            targets = non_zero if non_zero else sorted(matching, key=lambda i: load.get(i, 0))
        else:
            targets = sorted(matching, key=lambda i: load.get(i, 0))

        if not targets:
            targets = [0]

        if len(targets) <= 1 or len(task_names) <= 1:
            tid = targets[0]
            load[tid] = load.get(tid, 0) + len(task_names)
            inst_info = mgr.get_connection_info(tid)
            result[(gk, tid)] = (task_names, inst_info)
            print(f"  [{svc_label}] {len(task_names)} tasks -> instance {tid}")
        else:
            targets.sort(key=lambda i: load.get(i, 0))
            for idx, tid in enumerate(targets):
                chunk = task_names[idx::len(targets)]
                if not chunk:
                    continue
                load[tid] = load.get(tid, 0) + len(chunk)
                inst_info = mgr.get_connection_info(tid)
                result[(gk, tid)] = (chunk, inst_info)
                print(f"  [{svc_label}] chunk {idx}: {len(chunk)} tasks -> instance {tid}")

    return result
def _pick_instance_for_group(
    group_key: tuple[str, ...],
    instance_manager_ref: dict,
) -> dict | None:
    """
    Pick the best instance for a group based on service requirements.
    Returns instance connection info dict, or None for single-instance mode.
    """
    mgr = instance_manager_ref.get("manager")
    if mgr is None:
        return None

    services_needed = list(group_key) if group_key else []
    inst_id = mgr.acquire_instance(services_needed, locked_by="+".join(group_key) if group_key else "no-deps")
    return mgr.get_connection_info(inst_id)


def _release_instance(instance_manager_ref: dict, instance_info: dict | None):
    """Release an acquired instance back to the pool."""
    if instance_info is None:
        return
    mgr = instance_manager_ref.get("manager")
    if mgr is None:
        return
    mgr.release_instance(instance_info["instance_id"])


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
    parser.add_argument("--harness", type=str, default="openhands",
                        choices=["openhands", "docker"],
                        help="Harness to use (default: openhands)")
    parser.add_argument("--base-image", type=str, default=None,
                        help="Base Docker image (overrides TAC_BASE_IMAGE)")
    parser.add_argument("--num-instances", type=int, default=1,
                        help="Number of service instances (default: 1, backward compatible)")
    parser.add_argument("--full-stack-ids", type=str, default="0",
                        help="Comma-separated instance IDs with all services (default: 0)")
    parser.add_argument("--mock", action="store_true",
                        help="Use mock harness (no LLM API, simulate task execution)")
    parser.add_argument("--mock-duration", type=str, default="10,30",
                        help="Mock duration range in seconds: min,max (default: 10,30)")
    args = parser.parse_args()

    # Configure mock mode
    global _MOCK_CONFIG
    if args.mock:
        parts = args.mock_duration.split(",")
        lo = float(parts[0])
        hi = float(parts[1]) if len(parts) > 1 else lo + 20
        _MOCK_CONFIG = MockConfig(enabled=True, duration_range=(lo, hi))
        print(f"MOCK MODE: simulating tasks with duration {lo}-{hi}s")

    outputs_path = os.path.abspath(args.outputs_path)
    os.makedirs(outputs_path, exist_ok=True)

    task_names = None
    if args.tasks:
        task_names = [t.strip() for t in args.tasks.split(",")]

    groups = group_tasks_by_deps(task_names)

    # Initialize multi-instance manager if requested
    instance_manager_ref: dict = {"manager": None}
    if args.num_instances > 1:
        from service_manager import ServiceManager
        full_stack_ids = [int(x) for x in args.full_stack_ids.split(",") if x]
        mgr = ServiceManager(num_instances=args.num_instances, hostname=args.server_hostname, full_stack_ids=full_stack_ids)
        instance_manager_ref["manager"] = mgr

    print("=" * 60)
    print("TheAgentCompany V2 - Smart Parallel Scheduler")
    print("=" * 60)
    print(f"Total tasks: {sum(len(v) for v in groups.values())}")
    print(f"Dependency groups: {len(groups)}")
    print(f"Max parallel groups: {args.max_groups}")
    if args.num_instances > 1:
        print(f"Service instances: {args.num_instances} (full-stack={full_stack_ids}, rest gitlab-only)")
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
        sub_groups = _split_groups_across_instances(
            round_groups, groups, instance_manager_ref,
        )
        unique_insts = len({inst_id for _, inst_id in sub_groups.keys()})
        max_workers = min(unique_insts, len(sub_groups), 16)
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            for (gk, inst_id), (task_batch, inst_info) in sub_groups.items():
                svc = ", ".join(gk) if gk else "(no deps)"
                print(f"  [{svc}] {len(task_batch)} tasks -> instance {inst_id}")
                future = executor.submit(
                    run_group_sequential,
                    gk, task_batch,
                    args.agent_llm_config, args.env_llm_config,
                    outputs_path, args.server_hostname,
                    str(SCRIPT_DIR),
                    args.harness, args.base_image,
                    inst_info,
                )
                round_futures[future] = (gk, inst_info)

            for future in as_completed(round_futures):
                gk, instance_info = round_futures[future]
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
                finally:
                    _release_instance(instance_manager_ref, instance_info)

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
            "num_instances": args.num_instances,
            "results": all_results,
        }, f, indent=2)
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
