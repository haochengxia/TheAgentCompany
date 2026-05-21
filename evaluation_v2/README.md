# TheAgentCompany V2 Evaluation

Faster, cheaper benchmark evaluation with single-image architecture, parallel execution, and pluggable harness system.

## What Changed from V1

| | V1 | V2 |
|---|---|---|
| Docker images | 175 per-task images (~600 GB) | 1 base image (~2 GB) |
| Task files | Baked into image at build time | Mounted at runtime |
| Execution | Sequential (1 task at a time) | Parallel (up to 4 groups) |
| Service reset | Container restart (3-10 min) | Snapshot restore (5-30 sec) |
| Agent framework | OpenHands only | Pluggable harness (OpenHands, custom) |
| Docker cleanup | `docker system prune` after each task | None (reuse cached images) |
| Total runtime | ~4-9 days | ~4-8 hours |

## Prerequisites

- Python 3.12+
- poetry (`poetry install` in project root)
- Docker + docker buildx
- Root access
- `xargs` with `-P` support (GNU coreutils, standard on Linux)
- **For OpenHands harness**: `openhands-ai` package installed
- **For custom harness**: No additional dependencies

## Quick Start (OpenHands)

### 1. Build the base image

```bash
cd workspaces/base_image
make -f Makefile.v2 build
```

### 2. Start server infrastructure

```bash
cd servers
bash setup.sh
```

Wait until all services are healthy (typically 3-5 minutes for first start).

### 3. Create service snapshots (one-time)

```bash
cd evaluation_v2
bash snapshot_create.sh
```

This dumps each service's database to `snapshots/`. Snapshots are used for fast reset between tasks within the same dependency group.

### 4. Create config.toml

```toml
[llm.agent]
model = "<model_name>"
base_url = "<base_url>"
api_key = "<api_key>"

[llm.env]
model = "<model_name>"
base_url = "<base_url>"
api_key = "<api_key>"
```

- `agent`: LLM used by the agent to solve tasks
- `env`: LLM used by the evaluator and NPC simulations

### 5. Run

```bash
cd evaluation_v2

# See the execution plan without running anything
bash run_eval.sh --agent-llm-config agent --env-llm-config env --dry-run

# Run all 175 tasks with parallel scheduling
bash run_eval.sh \
  --agent-llm-config agent \
  --env-llm-config env \
  --outputs-path outputs \
  --server-hostname localhost \
  --max-groups 4

# Run specific tasks only
bash run_eval.sh \
  --agent-llm-config agent \
  --env-llm-config env \
  --tasks "admin-arrange-meeting-rooms,sde-debug-crashed-server"

# Run a single task directly (useful for debugging)
poetry run python run_eval.py \
  --task-dir ../workspaces/tasks/admin-arrange-meeting-rooms \
  --agent-llm-config agent \
  --env-llm-config env \
  --outputs-path outputs \
  --server-hostname localhost

# Summarize results after completion
python3 summarize_results.py outputs/
```

---

## Custom Harness

The V2 evaluation pipeline uses a **harness abstraction** that decouples the agent framework from the evaluation logic. You can run your own agent framework (Claude Code, Aider, SWE-Agent, custom) without any dependency on OpenHands.

### How It Works

The evaluation pipeline needs exactly 4 things from a harness:

| Operation | Method | Description |
|---|---|---|
| Start container | `start(mount_path)` | Create and connect to a Docker container |
| Run command | `run_command(cmd, timeout)` | Execute a shell command inside the container |
| Run agent | `run_agent(instruction, max_iterations)` | Your agent solves the task |
| Stop container | `stop()` | Cleanup |

Everything else — file setup, evaluator encryption, dependency loading, result collection — is handled by the shared `BaseHarness` and `run_eval.py`.

### Option A: Subclass `DockerHarness` (Recommended)

This is the simplest path. Create a single file with your agent logic:

```python
# my_harness.py
import os
from harness import DockerHarness, AgentState


class MyAgentHarness(DockerHarness):
    """Example: an agent that uses a local LLM API."""

    def run_agent(self, instruction, max_iterations=100, fake_user_response=None):
        # Step 1: Read the task instruction
        result = self.run_command("cat /instruction/task.md")
        task_description = result.content

        # Step 2: Run your agent loop
        trajectory = []
        for i in range(max_iterations):
            # Call your LLM / agent framework
            action = call_my_agent(task_description, trajectory)

            if action.type == "command":
                # Execute the agent's command in the container
                cmd_result = self.run_command(action.command, timeout=300)
                trajectory.append({
                    "action": action.command,
                    "observation": cmd_result.content,
                    "exit_code": cmd_result.exit_code,
                })
            elif action.type == "done":
                break

        # Step 3: Save trajectory (required for evaluator)
        traj_path = "/outputs/traj.json"
        import json
        self.run_command(
            f"echo '{json.dumps(trajectory)}' > {traj_path}"
        )

        return AgentState(
            success=True,
            trajectory_path=traj_path,
            history=trajectory,
        )
```

Then run:

```bash
cd evaluation_v2

# Single task
poetry run python -c "
import sys
sys.path.insert(0, '.')
from run_eval import *
from my_harness import MyAgentHarness
import argparse, os, shutil, tempfile

task_dir = '../workspaces/tasks/admin-arrange-meeting-rooms'
task_short_name = os.path.basename(task_dir)
base_image = os.environ.get('TAC_BASE_IMAGE', 'tac-base-image:latest')

temp_dir = tempfile.mkdtemp()
mount_path = os.path.join(temp_dir, f'mount_{task_short_name}')
os.makedirs(mount_path, exist_ok=True)

harness = MyAgentHarness(base_image=base_image)
harness.start(mount_path=mount_path)

staging_dir = os.path.join(mount_path, 'task_staging')
if os.path.exists(staging_dir):
    shutil.rmtree(staging_dir)
shutil.copytree(task_dir, staging_dir)
harness.setup_task_files(task_dir)

init_task_env(harness, 'localhost', None, None, None)
dependencies = load_dependencies(harness)

state = run_solver(harness, task_short_name, dependencies,
                   save_final_state=True, state_dir='outputs',
                   save_screenshots=False, screenshots_dir='outputs/screenshots')

run_evaluator(harness, None, None, None,
              f'/outputs/traj_{task_short_name}.json',
              f'/outputs/eval_{task_short_name}.json')
harness.stop()
"
```

### Option B: Use `--harness docker` via CLI

For a simpler integration where you just want a container with task files set up, and you handle the agent externally:

```bash
# 1. Start the evaluation with docker harness (skips agent, just sets up container)
poetry run python run_eval.py \
  --task-dir ../workspaces/tasks/admin-arrange-meeting-rooms \
  --harness docker \
  --outputs-path outputs \
  --server-hostname localhost
```

Note: `--harness docker` with `run_eval.py` will fail at `run_agent()` unless you subclass `DockerHarness` and override it. For full automation, use Option A or C.

### Option C: Standalone Script (Full Control)

If you want complete control over the evaluation loop, write a standalone script that uses `BaseHarness` primitives directly:

```python
#!/usr/bin/env python3
"""
Standalone evaluation script for a custom agent.
No OpenHands dependency required.
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from harness import DockerHarness, AgentState


def evaluate_task(task_dir: str, base_image: str, server_hostname: str):
    task_short_name = os.path.basename(task_dir)
    mount_path = tempfile.mkdtemp()
    os.makedirs(mount_path, exist_ok=True)

    # 1. Start container
    harness = MyAgentHarness(base_image=base_image)
    harness.start(mount_path=mount_path)

    try:
        # 2. Stage task files into the container
        staging_dir = os.path.join(mount_path, "task_staging")
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir)
        shutil.copytree(task_dir, staging_dir)
        harness.setup_task_files(task_dir)

        # 3. Initialize task environment (runs init.sh, sets up hosts)
        harness.run_command(
            f"SERVER_HOSTNAME={server_hostname} "
            "bash /utils/init.sh",
            timeout=900,
        )

        # 4. Run your agent
        state = harness.run_agent(
            instruction="Complete the task in /instruction/task.md",
            max_iterations=100,
        )

        # 5. Run evaluator (inside container, evaluates agent's trajectory)
        harness.run_command(
            "DECRYPTION_KEY='theagentcompany is all you need' "
            f"python_default /utils/eval.py "
            f"--trajectory_path /outputs/traj_{task_short_name}.json "
            f"--result_path /outputs/eval_{task_short_name}.json",
            timeout=600,
        )

        # 6. Collect results
        shutil.copy(
            os.path.join(mount_path, f"eval_{task_short_name}.json"),
            f"outputs/eval_{task_short_name}.json",
        )

    finally:
        harness.stop()


# Run multiple tasks in parallel
if __name__ == "__main__":
    from concurrent.futures import ProcessPoolExecutor

    tasks_dir = "../workspaces/tasks"
    base_image = os.environ.get("TAC_BASE_IMAGE", "tac-base-image:latest")

    tasks = sorted(os.listdir(tasks_dir))[:10]  # first 10 tasks

    with ProcessPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                evaluate_task,
                os.path.join(tasks_dir, t),
                base_image,
                "localhost",
            ): t
            for t in tasks
        }
        for future in futures:
            task = futures[future]
            try:
                future.result()
                print(f"  {task}: DONE")
            except Exception as e:
                print(f"  {task}: FAILED ({e})")
```

### API Reference

#### `BaseHarness` (Abstract Base Class)

All harnesses inherit from `BaseHarness`. You must implement these methods:

```python
class BaseHarness(ABC):
    def start(self, mount_path: str | None = None):
        """Create and start the container. mount_path is bind-mounted as /outputs."""

    def stop(self):
        """Stop and remove the container."""

    def run_command(self, command: str, timeout: int = 300) -> CommandResult:
        """Execute a shell command. Returns CommandResult(exit_code, content)."""

    def run_agent(self, instruction: str, max_iterations: int = 100,
                  fake_user_response: Callable | None = None) -> AgentState:
        """Run the agent. Returns AgentState(success, trajectory_path, history, screenshots)."""

    def copy_to_container(self, src: str, dst: str):
        """Copy host file/directory into the container."""
```

These methods are **provided** by `BaseHarness` (no need to override):

```python
def setup_task_files(self, task_dir: str):
    """Parse the task's Dockerfile, copy files to correct locations, encrypt evaluator."""

@staticmethod
def parse_dockerfile_copies(task_dir: str) -> dict[str, str]:
    """Parse COPY commands from the task Dockerfile. Returns {src_name: dst_path}."""
```

#### `DockerHarness`

Plain Docker container management. No OpenHands dependency.

```python
harness = DockerHarness(
    base_image="tac-base-image:latest",
    container_name="my-eval",     # optional, auto-generated if omitted
    network="host",               # Docker network
)
```

`run_agent()` raises `NotImplementedError` by default — you **must** subclass and override it.

#### `OpenHandsHarness`

Wraps the OpenHands runtime. Drop-in replacement for the original V1 evaluation.

```python
harness = OpenHandsHarness(
    base_image="tac-base-image:latest",
    llm_config=agent_llm_config,       # OpenHands LLMConfig object
    task_short_name="admin-arrange-meeting-rooms",
)
harness.start(mount_path="/tmp/outputs")
```

#### Data Classes

```python
@dataclass
class CommandResult:
    exit_code: int
    content: str

@dataclass
class AgentState:
    success: bool
    trajectory_path: str
    history: list        # agent trajectory events
    screenshots: list    # base64-encoded screenshots (optional)
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TAC_BASE_IMAGE` | `tac-base-image:latest` | Base Docker image name |
| `MAX_GROUPS` | `4` | Max parallel dependency groups in scheduler |
| `SNAPSHOT_DIR` | `evaluation_v2/snapshots` | Snapshot storage directory |
| `TMPDIR` | system temp | Temp directory for trajectory exchange |

## CLI Flags (run_eval.py)

| Flag | Default | Description |
|------|---------|-------------|
| `--task-dir` | — | Path to task directory (V2 mode) |
| `--task-image-name` | — | Task image name (V1 compat mode) |
| `--harness` | `openhands` | Harness: `openhands` or `docker` |
| `--base-image` | `$TAC_BASE_IMAGE` | Override the base Docker image |
| `--agent-llm-config` | — | LLM config for agent (OpenHands only) |
| `--env-llm-config` | — | LLM config for evaluator/NPC |
| `--outputs-path` | `./outputs` | Where to save trajectories and results |
| `--server-hostname` | `localhost` | Server hostname |
| `--build-image-only` | `false` | Build runtime image and exit (OpenHands only) |

## CLI Flags (scheduler.py)

| Flag | Default | Description |
|------|---------|-------------|
| `--agent-llm-config` | required | LLM config for agent |
| `--env-llm-config` | required | LLM config for evaluator/NPC |
| `--outputs-path` | `outputs` | Output directory |
| `--server-hostname` | `localhost` | Server hostname |
| `--harness` | `openhands` | Harness: `openhands` or `docker` |
| `--base-image` | `$TAC_BASE_IMAGE` | Override base Docker image |
| `--max-groups` | `4` | Max parallel groups |
| `--tasks` | — | Comma-separated task names |
| `--dry-run` | `false` | Print plan and exit |
| `--list-groups` | `false` | Print dependency groups and exit |

## Architecture

```
┌──────────────────────────────────────────────────────┐
│ HOST                                                  │
│                                                       │
│  ┌──────────────────────┐  ┌───────────────────────┐ │
│  │ Server Stack (13     │  │ evaluation_v2/        │ │
│  │ containers, always   │  │  scheduler.py         │ │
│  │ running)             │  │  ├─ worker 1 ──────┐  │ │
│  │  GitLab, RocketChat, │  │  ├─ worker 2 ──┐   │  │ │
│  │  ownCloud, Plane,    │  │  ├─ worker 3 ─┐ │   │  │ │
│  │  MongoDB, Redis...   │  │  └─ worker 4  │ │   │  │ │
│  └──────────────────────┘  └───────────────┼─┼───┼──┘ │
│                                     │     │ │   │    │
│              ┌──────────────────────┼─────┘ │   │    │
│              │  Harness Layer       │       │   │    │
│              │  ┌───────────────────▼───────▼───▼──┐ │
│              │  │ DockerHarness / OpenHandsHarness  │ │
│              │  │   start() / run_command() /       │ │
│              │  │   run_agent() / stop()            │ │
│              │  └──────────────────────────────────┘ │
│              │         │                              │
│              │  ┌──────▼──────────────────────────┐  │
│              │  │ Single base image                │  │
│              │  │ + task files mounted at runtime   │  │
│              │  └──────────────────────────────────┘  │
│              └────────────────────────────────────────┘
└──────────────────────────────────────────────────────┘
```

## Smart Parallel Scheduling

Tasks are grouped by service dependency and scheduled to maximize parallelism:
- Tasks in the **same** dependency group run **sequentially** (they share services)
- Tasks in **different** groups run **in parallel** (no service conflict)
- Uses greedy graph-coloring to partition groups into rounds

```
Round 1: [gitlab] 47 tasks || [owncloud+rocketchat] 33 tasks || [plane] 6 tasks || [no deps] 3 tasks
Round 2: [owncloud] 33 tasks || [rocketchat] 24 tasks || [gitlab+plane] 5 tasks
Round 3: [gitlab+rocketchat] 15 tasks
Round 4: [plane+rocketchat] 5 tasks || [gitlab+owncloud] 2 tasks
Round 5: [gitlab+owncloud+plane+rocketchat] 1 task
Round 6: [gitlab+owncloud+rocketchat] 1 task
```

Round 1 processes 89 tasks with 4 groups running simultaneously.

## V1 Compatibility

V2 `run_eval.py` accepts both `--task-dir` (v2) and `--task-image-name` (v1).
When `--task-image-name` is used, it behaves like the original v1 script.

Default `--harness openhands` preserves identical behavior to V1.

## Files

- `harness.py` — Harness abstraction (BaseHarness, DockerHarness, OpenHandsHarness)
- `run_eval.py` — Per-task evaluation script (harness-agnostic)
- `run_eval.sh` — Entry point, delegates to scheduler.py
- `scheduler.py` — Smart parallel task scheduler with dependency-aware grouping
- `snapshot_create.sh` — Create service DB snapshots (run once)
- `snapshot_restore.sh` — Restore services from snapshots (fast reset)
- `summarize_results.py` — Aggregate results into summary report
- `browsing.py` — Symlink to ../evaluation/browsing.py (OpenHands browser pre-login)
- `DESIGN_DOC.md` — Full architecture and design rationale
