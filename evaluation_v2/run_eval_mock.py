#!/usr/bin/env python3
"""
Mock run_eval for infrastructure testing.

Simulates task execution without calling any LLM API.
Uses realistic timing based on historical benchmark data.

Usage:
  python run_eval_mock.py --task-dir /path/to/task --outputs-path /mydata/test ...
  
  (Same interface as run_eval.py)
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path


# Historical average task durations from real benchmark runs (seconds)
# Grouped by service dependency for realism
TYPICAL_DURATIONS = {
    "gitlab": (120, 300),       # gitlab tasks: 2-5 min
    "rocketchat": (100, 250),   # rocketchat tasks: 1.5-4 min  
    "owncloud": (90, 220),      # owncloud tasks: 1.5-3.5 min
    "plane": (80, 200),         # plane tasks: 1.3-3.3 min
    "default": (100, 250),      # fallback
}

# Success rates by service (from real benchmark)
SUCCESS_RATES = {
    "gitlab": 0.40,
    "rocketchat": 0.35,
    "owncloud": 0.38,
    "plane": 0.42,
    "default": 0.37,
}


def detect_service(task_dir: str) -> str:
    """Detect which service a task depends on from dependencies.yml."""
    dep_file = os.path.join(task_dir, "dependencies.yml")
    if os.path.exists(dep_file):
        try:
            import yaml
            with open(dep_file) as f:
                deps = yaml.safe_load(f) or []
            if deps:
                return deps[0]
        except Exception:
            pass
    return "default"


def main():
    parser = argparse.ArgumentParser(description="Mock run_eval for infra testing")
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--agent-llm-config", default="agent")
    parser.add_argument("--env-llm-config", default="env")
    parser.add_argument("--outputs-path", required=True)
    parser.add_argument("--server-hostname", default="localhost")
    parser.add_argument("--harness", default="openhands")
    parser.add_argument("--base-image", default=None)
    parser.add_argument("--service-instance", default=None)
    parser.add_argument("--min-duration", type=float, default=0,
                        help="Override minimum duration (for fast testing)")
    parser.add_argument("--max-duration", type=float, default=0,
                        help="Override maximum duration (for fast testing)")
    args = parser.parse_args()

    task_name = Path(args.task_dir).name
    service = detect_service(args.task_dir)

    # Determine duration
    if args.min_duration > 0 or args.max_duration > 0:
        lo = args.min_duration or 5
        hi = args.max_duration or (lo + 30)
    else:
        lo, hi = TYPICAL_DURATIONS.get(service, TYPICAL_DURATIONS["default"])

    duration = random.uniform(lo, hi)

    # Determine success
    success_rate = SUCCESS_RATES.get(service, SUCCESS_RATES["default"])
    success = random.random() < success_rate

    # Simulate execution
    print(f"[MOCK] {task_name} ({service}): simulating {duration:.1f}s ...")
    time.sleep(duration)

    # Write outputs (mimic real run_eval.py output format)
    eval_path = os.path.join(args.outputs_path, f"eval_{task_name}.json")

    if success:
        total_subtasks = random.randint(3, 6)
        passed_subtasks = random.randint(2, total_subtasks)
        eval_data = {
            "final_score": {
                "result": passed_subtasks,
                "total": total_subtasks,
            },
            "mock": True,
            "simulated_duration": round(duration, 1),
        }
    else:
        eval_data = {
            "error": "task_failed",
            "exit_code": 1,
            "mock": True,
            "simulated_duration": round(duration, 1),
        }

    with open(eval_path, "w") as f:
        json.dump(eval_data, f, indent=2)

    print(f"[MOCK] {task_name}: {'OK' if success else 'FAIL'} (simulated {duration:.1f}s)")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
