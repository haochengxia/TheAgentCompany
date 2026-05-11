#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT_LLM_CONFIG="agent"
ENV_LLM_CONFIG="env"
OUTPUTS_PATH="outputs"
SERVER_HOSTNAME="localhost"
MAX_GROUPS="${MAX_GROUPS:-4}"
SCHEDULER_ARGS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-llm-config) AGENT_LLM_CONFIG="$2"; shift 2 ;;
        --env-llm-config) ENV_LLM_CONFIG="$2"; shift 2 ;;
        --outputs-path) OUTPUTS_PATH="$2"; shift 2 ;;
        --server-hostname) SERVER_HOSTNAME="$2"; shift 2 ;;
        --max-groups) MAX_GROUPS="$2"; shift 2 ;;
        --tasks) SCHEDULER_ARGS="$SCHEDULER_ARGS --tasks $2"; shift 2 ;;
        --dry-run) SCHEDULER_ARGS="$SCHEDULER_ARGS --dry-run"; shift ;;
        --list-groups) SCHEDULER_ARGS="$SCHEDULER_ARGS --list-groups"; shift ;;
        *)
            echo "Unknown argument: $1"
            echo ""
            echo "Usage: bash run_eval.sh [options]"
            echo "  --agent-llm-config NAME   LLM config for agent (default: agent)"
            echo "  --env-llm-config NAME     LLM config for environment (default: env)"
            echo "  --outputs-path PATH       Output directory (default: outputs)"
            echo "  --server-hostname HOST    Server hostname (default: localhost)"
            echo "  --max-groups N            Max parallel groups (default: 4)"
            echo "  --tasks TASK1,TASK2,...   Run specific tasks only"
            echo "  --dry-run                 Print execution plan without running"
            echo "  --list-groups             Print grouping plan and exit"
            exit 1 ;;
    esac
done

if [[ ! "$OUTPUTS_PATH" = /* ]]; then
    OUTPUTS_PATH="$(cd "$(dirname "$OUTPUTS_PATH")" 2>/dev/null && pwd)/$(basename "$OUTPUTS_PATH")"
fi

exec python3 "$SCRIPT_DIR/scheduler.py" \
    --agent-llm-config "$AGENT_LLM_CONFIG" \
    --env-llm-config "$ENV_LLM_CONFIG" \
    --outputs-path "$OUTPUTS_PATH" \
    --server-hostname "$SERVER_HOSTNAME" \
    --max-groups "$MAX_GROUPS" \
    $SCHEDULER_ARGS
