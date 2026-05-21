#!/bin/bash
# mount_task.sh - Prepares task files from a mounted /task directory
#
# In v1, each task had its own Docker image with ONBUILD directives that:
#   1. Copied evaluator.py, dependencies.yml, task.md into the container
#   2. Encrypted evaluator.py to prevent agent access
#
# In v2, we mount task files at runtime and this script does the same prep.
# The /task directory is bind-mounted from the host at container start.
#
# Expected /task contents:
#   task.md           (required) - Task instructions for the agent
#   evaluator.py      (required) - Grading script
#   dependencies.yml  (required) - Service dependencies
#   scenarios.json    (optional) - NPC scenarios for chat tasks
#   pre_init.sh/py    (optional) - Pre-initialization scripts
#   post_init.sh/py   (optional) - Post-initialization scripts
#   populate_data.py  (optional) - Data population script
#   *.csv, *.xlsx...  (optional) - Data files for workspace or utils
#   eval_data/        (optional) - Directory of evaluator data files
#   Any other files referenced by task Dockerfile COPY commands

set -e

# --- Validate /task exists and has required files ---
if [ ! -d "/task" ]; then
    echo "ERROR: /task directory not mounted"
    exit 1
fi

if [ ! -f "/task/task.md" ]; then
    echo "ERROR: /task/task.md not found"
    exit 1
fi

if [ ! -f "/task/evaluator.py" ]; then
    echo "ERROR: /task/evaluator.py not found"
    exit 1
fi

# --- Copy task instructions ---
cp /task/task.md /instruction/task.md
echo "Copied task.md -> /instruction/task.md"

# --- Copy dependencies ---
if [ -f "/task/dependencies.yml" ]; then
    cp /task/dependencies.yml /utils/dependencies.yml
    echo "Copied dependencies.yml -> /utils/dependencies.yml"
fi

# --- Copy evaluator and encrypt it ---
cp /task/evaluator.py /utils/evaluator.py
python_default /utils/encrypt.py
rm -f /utils/evaluator.py /utils/encrypt.py
echo "Encrypted evaluator.py -> /utils/evaluator.py.enc"

# --- Copy helper Python scripts to /utils ---
for script in pre_init.py post_init.py populate_data.py prompts.py helper.py; do
    if [ -f "/task/$script" ]; then
        cp "/task/$script" "/utils/$script"
        echo "Copied $script -> /utils/$script"
    fi
done

# --- Copy helper shell scripts to /utils ---
for script in pre_init.sh post_init.sh; do
    if [ -f "/task/$script" ]; then
        cp "/task/$script" "/utils/$script"
        chmod +x "/utils/$script"
        echo "Copied $script -> /utils/$script"
    fi
done

# --- Copy scenarios.json for NPC tasks ---
if [ -f "/task/scenarios.json" ]; then
    cp /task/scenarios.json /npc/scenarios.json
    export SCENARIOS_FILE_PATH=/npc/scenarios.json
    echo "Copied scenarios.json -> /npc/scenarios.json"
fi

# --- Copy eval_data directory if present ---
if [ -d "/task/eval_data" ]; then
    cp -r /task/eval_data /utils/eval_data
    echo "Copied eval_data/ -> /utils/eval_data/"
fi

# --- Copy data files ---
# CSV/XLSX/TXT/PDF/etc files go to /workspace (for agent) unless they're
# explicitly for the evaluator (golden answer, reference data)
# Heuristic: files with "golden", "ref_", "expected_", "solution", "average_",
# "short_" go to /utils; everything else to /workspace
for f in /task/*; do
    fname=$(basename "$f")
    # Skip already-handled files and directories
    case "$fname" in
        task.md|evaluator.py|dependencies.yml|scenarios.json|Dockerfile|Makefile|checkpoints.md|README.md)
            continue
            ;;
        pre_init.*|post_init.*|populate_data.*|prompts.py|helper.py)
            continue
            ;;
    esac

    # Skip directories (handled separately)
    [ -d "$f" ] && continue

    # Determine destination
    case "$fname" in
        golden_*|ref_*|expected_*|solution*|average_*|short_*|personell_data_golden*)
            cp "$f" "/utils/$fname"
            echo "Copied $fname -> /utils/$fname (evaluator data)"
            ;;
        init.sql)
            cp "$f" "/data/$fname"
            echo "Copied $fname -> /data/$fname"
            ;;
        *.java|*.cpp|*.go|*.py)
            # Code files (test files, templates) go to /workspace
            cp "$f" "/workspace/$fname"
            echo "Copied $fname -> /workspace/$fname"
            ;;
        *)
            # Default: data files to /workspace for agent access
            cp "$f" "/workspace/$fname"
            echo "Copied $fname -> /workspace/$fname"
            ;;
    esac
done

# --- Handle special directories ---
# app/ directory (sde-debug-crashed-server)
if [ -d "/task/app" ]; then
    cp -r /task/app /workspace/app
    echo "Copied app/ -> /workspace/app/"
fi

# --- Create a marker file indicating mount is complete ---
touch /tmp/task_mounted
echo "=== Task mount complete ==="

# Execute CMD (if any)
exec "$@"
