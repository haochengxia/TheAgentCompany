#!/bin/sh
set -e

reset_services=()

check_service_health() {
    local service=$1
    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" -I "http://the-agent-company.com:2999/api/healthcheck/${service}")
    echo "Service $service healthcheck status: $http_status"
    [ "$http_status" = "200" ]
}

wait_for_services() {
    local max_attempts=30
    local attempt=1
    local all_services_ready

    echo "Waiting for services to be ready (max $((max_attempts * 5))s)..."

    while [ $attempt -le $max_attempts ]; do
        all_services_ready=true

        for service in "${reset_services[@]}"; do
            if ! check_service_health "$service"; then
                echo "Service $service not ready yet (attempt $attempt)..."
                all_services_ready=false
                break
            fi
        done

        if [ "$all_services_ready" = true ]; then
            echo "All services are ready!"
            return 0
        fi

        sleep 5
        attempt=$((attempt + 1))
    done

    echo "Error: Timeout waiting for services to be ready"
    return 1
}

# Use snapshot-restore if available, otherwise fall back to API reset
SNAPSHOT_DIR="${TAC_SNAPSHOT_DIR:-/snapshots}"

for service in rocketchat plane gitlab owncloud; do
    if grep -q "$service" /utils/dependencies.yml; then
        echo "Resetting $service..."
        if [ -d "$SNAPSHOT_DIR/$service" ]; then
            # Fast path: snapshot restore (handled by host-side snapshot_restore.sh
            # which is called before this container starts in v2)
            echo "Using snapshot restore for $service"
        else
            # Fallback: API reset (v1 behavior)
            curl -s -X POST "http://the-agent-company.com:2999/api/reset-${service}" > /dev/null
        fi
        reset_services+=("$service")
    fi
done

if [ ${#reset_services[@]} -gt 0 ]; then
    echo "Reset initiated for services: ${reset_services[*]}"
    wait_for_services
else
    echo "No matching services found in dependencies.yml"
fi
