#!/bin/bash
# Multi-instance service manager for TheAgentCompany benchmark.
#
# Manages N independent sets of service containers using docker-compose
# with different project names and port offsets.
#
# Architecture:
#   Instance 0: full stack (gitlab + rocketchat + owncloud + plane) on default ports
#   Instance 1..N: gitlab only on offset ports (base + N*10000)
#
# Usage:
#   ./service_instances.sh start 2            # Start 2 instances (0, 1)
#   ./service_instances.sh stop               # Stop all instances
#   ./service_instances.sh status             # Show running instances
#   ./service_instances.sh reset 1 gitlab     # Reset gitlab on instance 1
#   ./service_instances.sh logs 0             # Show logs for instance 0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$SCRIPT_DIR"
SERVERS_DIR="$(cd "$SCRIPT_DIR/../servers" && pwd)"
PORT_INCREMENT="${TAC_PORT_INCREMENT:-10000}"
HOSTNAME="${TAC_HOSTNAME:-localhost}"

# Base ports (instance 0)
BASE_GITLAB=8929
BASE_API=2999
BASE_ROCKETCHAT=3000
BASE_OWNCLOUD=8092
BASE_PLANE=8091
BASE_COLLABORA=9980
BASE_REDIS=6379
BASE_MONGODB=27017

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

get_port() {
    local instance=$1
    local base=$2
    echo $((base + instance * PORT_INCREMENT))
}

prepare_instance_dir() {
    local instance=$1
    local inst_dir="$EVAL_DIR/.instance_${instance}"
    mkdir -p "$inst_dir"

    # Copy docker-compose.yml
    cp "$SERVERS_DIR/docker-compose.yml" "$inst_dir/docker-compose.yml"

    # Compute ports
    local gitlab_port=$(get_port $instance $BASE_GITLAB)
    local rocketchat_port=$(get_port $instance $BASE_ROCKETCHAT)
    local owncloud_port=$(get_port $instance $BASE_OWNCLOUD)
    local collabora_port=$(get_port $instance $BASE_COLLABORA)
    local redis_port=$(get_port $instance $BASE_REDIS)

    # Write .env
    cat > "$inst_dir/.env" << EOF
GITLAB_PORT=${gitlab_port}
HOSTNAME=${HOSTNAME}
HOST_PORT=${rocketchat_port}
PORT=3000
ROCKETCHAT_PORT=${rocketchat_port}
EOF

    # Patch docker-compose.yml for instance-specific container names
    local suffix=""
    if [ "$instance" -gt 0 ]; then
        suffix="-inst${instance}"
    fi

    local compose="$inst_dir/docker-compose.yml"

    # Replace container names
    sed -i "s/container_name: gitlab$/container_name: gitlab${suffix}/" "$compose"
    sed -i "s/container_name: rocketchat$/container_name: rocketchat${suffix}/" "$compose"
    sed -i "s/container_name: rocketchat-mongodb$/container_name: rocketchat-mongodb${suffix}/" "$compose"
    sed -i "s/container_name: owncloud$/container_name: owncloud${suffix}/" "$compose"
    sed -i "s/container_name: owncloud-collabora$/container_name: owncloud-collabora${suffix}/" "$compose"
    sed -i "s/container_name: redis-stack$/container_name: redis-stack${suffix}/" "$compose"
    sed -i "s/container_name: redis-stack-npc-data-population$/container_name: redis-stack-npc-data-population${suffix}/" "$compose"

    # Patch hardcoded ports for non-zero instances
    if [ "$instance" -gt 0 ]; then
        sed -i "s/\"8092:80\"/\"${owncloud_port}:80\"/" "$compose"
        sed -i "s/\"9980:9980\"/\"${collabora_port}:9980\"/" "$compose"
        sed -i "s/\"6379:6379\"/\"${redis_port}:6379\"/" "$compose"
    fi

    echo "$inst_dir"
}

start_instance() {
    local instance=$1
    local inst_dir="$EVAL_DIR/.instance_${instance}"

    log_info "Preparing instance $instance..."
    local compose_dir=$(prepare_instance_dir "$instance")
    local project="tac-inst-${instance}"
    local api_port=$(get_port $instance $BASE_API)

    # Determine services to start
    local services
    if [ "$instance" -eq 0 ]; then
        services="gitlab rocketchat mongodb redis-stack redis-stack-npc-data-population owncloud owncloud-collabora"
    else
        services="gitlab"
    fi

    log_info "Starting instance $instance (project: $project, services: $services)"
    docker compose \
        -p "$project" \
        --env-file "$compose_dir/.env" \
        -f "$compose_dir/docker-compose.yml" \
        up $services -d

    if [ $? -ne 0 ]; then
        log_error "Failed to start instance $instance"
        return 1
    fi

    # Start api-server container for this instance
    local api_name="api-server-inst${instance}"
    local gitlab_port=$(get_port $instance $BASE_GITLAB)
    local rocketchat_port=$(get_port $instance $BASE_ROCKETCHAT)
    local plane_port=$(get_port $instance $BASE_PLANE)

    # Stop existing api-server if any
    docker stop -t 5 "$api_name" 2>/dev/null || true
    docker rm -f "$api_name" 2>/dev/null || true

    log_info "Starting api-server for instance $instance on port $api_port..."
    docker run -d --rm \
        --name "$api_name" \
        --network host \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -e "SERVER_HOSTNAME=${HOSTNAME}" \
        -e "GITLAB_PORT=${gitlab_port}" \
        -e "ROCKETCHAT_PORT=${rocketchat_port}" \
        -e "PLANE_PORT=${plane_port}" \
        -e "SKIP_SETUP=True" \
        servers-api-server-image:latest

    if [ $? -ne 0 ]; then
        log_error "Failed to start api-server for instance $instance"
        return 1
    fi

    log_info "Instance $instance started successfully"
    log_info "  GitLab:    port $(get_port $instance $BASE_GITLAB)"
    log_info "  API:       port $api_port"
    if [ "$instance" -eq 0 ]; then
        log_info "  RocketChat: port $(get_port $instance $BASE_ROCKETCHAT)"
        log_info "  ownCloud:  port $(get_port $instance $BASE_OWNCLOUD)"
        log_info "  Plane:     port $(get_port $instance $BASE_PLANE)"
    fi
}

start_all() {
    local count=$1
    log_info "Starting $count service instance(s)..."

    for i in $(seq 0 $((count - 1))); do
        start_instance "$i"
    done

    log_info "All $count instances started"
    status_show "$count"
}

stop_instance() {
    local instance=$1
    local project="tac-inst-${instance}"
    local inst_dir="$EVAL_DIR/.instance_${instance}"
    local api_name="api-server-inst${instance}"

    log_info "Stopping instance $instance..."

    # Stop api-server
    docker stop -t 5 "$api_name" 2>/dev/null || true
    docker rm -f "$api_name" 2>/dev/null || true

    # Stop docker-compose services
    if [ -f "$inst_dir/docker-compose.yml" ]; then
        docker compose \
            -p "$project" \
            -f "$inst_dir/docker-compose.yml" \
            down -v --remove-orphans 2>/dev/null || true
    fi

    # For instance 0, also stop plane
    if [ "$instance" -eq 0 ]; then
        docker compose \
            -p "plane-app" \
            -f "$SERVERS_DIR/plane/plane-app/docker-compose.yaml" \
            down -v 2>/dev/null || true
    fi

    log_info "Instance $instance stopped"
}

stop_all() {
    log_info "Stopping all instances..."

    # Find running instance directories
    for inst_dir in "$EVAL_DIR"/.instance_*/; do
        if [ -d "$inst_dir" ]; then
            local instance=$(basename "$inst_dir" | sed 's/\.instance_//')
            stop_instance "$instance"
        fi
    done

    # Also stop any lingering api-server containers
    for name in $(docker ps --format '{{.Names}}' 2>/dev/null | grep 'api-server-inst'); do
        log_info "Stopping $name..."
        docker stop -t 5 "$name" 2>/dev/null || true
        docker rm -f "$name" 2>/dev/null || true
    done

    log_info "All instances stopped"
}

reset_service() {
    local instance=$1
    local service=$2
    local api_port=$(get_port $instance $BASE_API)

    log_info "Resetting $service on instance $instance (api port: $api_port)..."
    curl -s -X POST "http://${HOSTNAME}:${api_port}/api/reset-${service}"
    echo ""
    log_info "Reset initiated for $service on instance $instance"
}

status_show() {
    local count="${1:-auto}"

    if [ "$count" = "auto" ]; then
        # Count from directories
        count=0
        for inst_dir in "$EVAL_DIR"/.instance_*/; do
            if [ -d "$inst_dir" ]; then
                count=$((count + 1))
            fi
        done
        count=${count:-1}
    fi

    echo "=== Service Instances Status ==="
    echo ""

    for i in $(seq 0 $((count - 1))); do
        local project="tac-inst-${i}"
        local api_name="api-server-inst${i}"
        local api_port=$(get_port $i $BASE_API)
        local gitlab_port=$(get_port $i $BASE_GITLAB)

        # Check if docker compose project is running
        local compose_running=false
        if docker compose -p "$project" ps 2>/dev/null | grep -q "gitlab"; then
            compose_running=true
        fi

        # Check if api-server is running
        local api_running=false
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${api_name}$"; then
            api_running=true
        fi

        local status="STOPPED"
        if [ "$compose_running" = true ] && [ "$api_running" = true ]; then
            status="RUNNING"
        elif [ "$compose_running" = true ] || [ "$api_running" = true ]; then
            status="PARTIAL"
        fi

        echo "Instance $i: $status"
        echo "  Project: $project"
        echo "  GitLab:  port $gitlab_port"
        echo "  API:     port $api_port"
        if [ "$i" -eq 0 ]; then
            echo "  Type:    FULL STACK (gitlab+rocketchat+owncloud+plane)"
            echo "  RocketChat: port $(get_port $i $BASE_ROCKETCHAT)"
            echo "  ownCloud:   port $(get_port $i $BASE_OWNCLOUD)"
            echo "  Plane:      port $(get_port $i $BASE_PLANE)"
        else
            echo "  Type:    GITLAB ONLY"
        fi
        echo ""
    done
}

show_logs() {
    local instance=$1
    local project="tac-inst-${instance}"
    shift || true

    docker compose -p "$project" logs "$@"
}

# Main
case "${1:-}" in
    start)
        if [ -z "${2:-}" ]; then
            echo "Usage: $0 start <count> [--hostname HOST]"
            echo "  e.g., $0 start 2    # Start instances 0 and 1"
            exit 1
        fi
        # Parse optional hostname
        shift
        count=$1
        shift 2>/dev/null || true
        while [ $# -gt 0 ]; do
            case "$1" in
                --hostname) HOSTNAME="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        start_all "$count"
        ;;
    stop)
        stop_all
        ;;
    status)
        shift || true
        count="auto"
        while [ $# -gt 0 ]; do
            case "$1" in
                --num-instances) count="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        status_show "$count"
        ;;
    reset)
        if [ -z "${2:-}" ] || [ -z "${3:-}" ]; then
            echo "Usage: $0 reset <instance> <service>"
            echo "  e.g., $0 reset 0 gitlab"
            exit 1
        fi
        reset_service "$2" "$3"
        ;;
    logs)
        if [ -z "${2:-}" ]; then
            echo "Usage: $0 logs <instance> [service]"
            exit 1
        fi
        instance=$2
        shift 2
        show_logs "$instance" "$@"
        ;;
    *)
        echo "TheAgentCompany Multi-Instance Service Manager"
        echo ""
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  start <N>              Start N instances (0 to N-1)"
        echo "  stop                   Stop all instances"
        echo "  status                 Show instance status"
        echo "  reset <N> <service>    Reset service on instance N"
        echo "  logs <N> [service]     Show logs for instance N"
        echo ""
        echo "Environment variables:"
        echo "  TAC_PORT_INCREMENT     Port offset per instance (default: 10000)"
        echo "  TAC_HOSTNAME           Server hostname (default: localhost)"
        exit 0
        ;;
esac
