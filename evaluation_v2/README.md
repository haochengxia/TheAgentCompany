# TheAgentCompany V2 Evaluation

Faster, cheaper benchmark evaluation with single-image architecture and parallel execution.

## What Changed from V1

| | V1 | V2 |
|---|---|---|
| Docker images | 175 per-task images (~600 GB) | 1 base image (~2 GB) |
| Task files | Baked into image at build time | Mounted at runtime |
| Execution | Sequential (1 task at a time) | Parallel (`MAX_PARALLEL` workers) |
| Service reset | Container restart (3-10 min) | Snapshot restore (5-30 sec) |
| Docker cleanup | `docker system prune` after each task | None (reuse cached images) |
| Total runtime | ~4-9 days | ~4-8 hours |

## Prerequisites

Same as V1:
- Python 3.12+
- poetry (`poetry install` in project root)
- Docker + docker buildx
- Root access

Plus (new):
- `xargs` with `-P` support (GNU coreutils, standard on Linux)

## Setup

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

### 3. Create service snapshots (one-time, after servers are healthy)

```bash
cd evaluation_v2
bash snapshot_create.sh
```

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

## Run Evaluation

```bash
cd evaluation_v2

# See the execution plan without running
bash run_eval.sh --agent-llm-config agent --env-llm-config env --dry-run

# Run all 175 tasks with smart parallel scheduling
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
  --tasks "admin-arrange-meeting-rooms,sde-debug-crashed-server,ds-coffee-shop-database-management"

# Run single task directly
poetry run python run_eval.py \
  --task-dir ../workspaces/tasks/admin-arrange-meeting-rooms \
  --agent-llm-config agent \
  --env-llm-config env \
  --outputs-path outputs \
  --server-hostname localhost

# Summarize results after completion
python3 summarize_results.py outputs/
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TAC_BASE_IMAGE` | `tac-base-image:latest` | Base Docker image name |
| `MAX_GROUPS` | `4` | Max parallel dependency groups in scheduler |
| `SNAPSHOT_DIR` | `evaluation_v2/snapshots` | Snapshot storage directory |
| `TMPDIR` | system temp | Temp directory for trajectory exchange |

## Architecture

```
┌─────────────────────────────────────────────────┐
│ HOST                                             │
│                                                  │
│  ┌──────────────────────┐  ┌──────────────────┐ │
│  │ Server Stack (13     │  │ evaluation_v2/   │ │
│  │ containers, always   │  │  run_eval.sh     │ │
│  │ running)             │  │  ├─ worker 1 ──┐ │ │
│  │  GitLab, RocketChat, │  │  ├─ worker 2 ──┤ │ │
│  │  ownCloud, Plane,    │  │  ├─ worker 3 ──┤ │ │
│  │  MongoDB, Redis...   │  │  └─ worker 4 ──┘ │ │
│  └──────────────────────┘  └──────────────────┘ │
│                                     │            │
│                          ┌──────────▼─────────┐ │
│                          │ Single base image   │ │
│                          │ + task dir mounted  │ │
│                          │ at runtime          │ │
│                          └────────────────────┘ │
└─────────────────────────────────────────────────┘
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

## Files

- `run_eval.sh` — Entry point, delegates to scheduler.py
- `scheduler.py` — Smart parallel task scheduler with dependency-aware grouping
- `run_eval.py` — Per-task evaluation script (OpenHands integration)
- `snapshot_create.sh` — Create service DB snapshots (run once)
- `snapshot_restore.sh` — Restore services from snapshots (fast reset)
- `summarize_results.py` — Aggregate results into summary report
- `browsing.py` — Symlink to ../evaluation/browsing.py
