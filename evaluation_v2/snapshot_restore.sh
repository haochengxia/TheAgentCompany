#!/bin/bash
# Restore services from snapshots (fast, 5-30 sec vs 3-10 min container restart)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$SCRIPT_DIR/snapshots}"

restore_rocketchat() {
    if [ ! -f "$SNAPSHOT_DIR/rocketchat/db.dump" ]; then
        echo "[restore] RocketChat: no snapshot found, falling back to container restart"
        curl -s -X POST "http://localhost:2999/api/reset-rocketchat"
        return
    fi
    echo "[restore] RocketChat: restoring from mongodump..."
    docker exec -i rocketchat-mongodb sh -c 'mongorestore --drop --archive' < "$SNAPSHOT_DIR/rocketchat/db.dump"
    echo "[restore] RocketChat: done"
}

restore_gitlab() {
    if [ ! -f "$SNAPSHOT_DIR/gitlab/container_id" ]; then
        echo "[restore] GitLab: no snapshot found, falling back to container restart"
        curl -s -X POST "http://localhost:2999/api/reset-gitlab"
        return
    fi
    echo "[restore] GitLab: resetting via container restart (image has baked-in data)..."
    curl -s -X POST "http://localhost:2999/api/reset-gitlab"
    echo "[restore] GitLab: restart initiated (still slow - GitLab has no DB snapshot support)"
}

restore_owncloud() {
    if [ ! -f "$SNAPSHOT_DIR/owncloud/data.tar" ]; then
        echo "[restore] ownCloud: no snapshot found, falling back to container restart"
        curl -s -X POST "http://localhost:2999/api/reset-owncloud"
        return
    fi
    echo "[restore] ownCloud: restoring data directory..."
    docker exec owncloud sh -c 'rm -rf /var/www/owncloud/data/*'
    docker exec -i owncloud tar xf - -C /var/www/owncloud/data < "$SNAPSHOT_DIR/owncloud/data.tar"
    echo "[restore] ownCloud: done"
}

restore_plane() {
    if [ ! -f "$SNAPSHOT_DIR/plane/db.dump" ]; then
        echo "[restore] Plane: no snapshot found, falling back to container restart"
        curl -s -X POST "http://localhost:2999/api/reset-plane"
        return
    fi
    echo "[restore] Plane: restoring from pg_dump..."
    local pg_container
    pg_container=$(docker ps --format '{{.Names}}' | grep 'plane.*db\|plane.*postgres' | head -1)
    if [ -n "$pg_container" ]; then
        docker exec -i "$pg_container" sh -c 'psql -U plane -d plane' < "$SNAPSHOT_DIR/plane/db.dump" 2>/dev/null || true
        echo "[restore] Plane: done"
    else
        echo "[restore] Plane: postgres container not found, falling back to container restart"
        curl -s -X POST "http://localhost:2999/api/reset-plane"
    fi
}

restore_redis() {
    if [ ! -f "$SNAPSHOT_DIR/redis/dump.rdb" ]; then
        echo "[restore] Redis: no snapshot found, using existing reset"
        curl -s -X POST "http://localhost:2999/api/reset-rocketchat"
        return
    fi
    echo "[restore] Redis: restoring RDB snapshot..."
    docker exec redis-stack redis-cli -a theagentcompany SHUTDOWN NOSAVE 2>/dev/null || true
    docker cp "$SNAPSHOT_DIR/redis/dump.rdb" redis-stack:/data/dump.rdb
    docker restart redis-stack
    echo "[restore] Redis: done"
}

wait_for_service() {
    local service="$1"
    local max_attempts="${2:-60}"
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        http_status=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:2999/api/healthcheck/$service")
        if [ "$http_status" = "200" ]; then
            echo "[restore] $service is ready"
            return 0
        fi
        sleep 5
        attempt=$((attempt + 1))
    done
    echo "[restore] WARNING: $service did not become healthy within $((max_attempts * 5))s"
    return 1
}

if [ "$1" = "--all" ]; then
    echo "=== Restoring all services from snapshots ==="
    restore_redis
    restore_rocketchat
    restore_owncloud
    restore_plane
    restore_gitlab

    echo ""
    echo "=== Waiting for all services to be healthy ==="
    for svc in rocketchat owncloud plane gitlab; do
        wait_for_service "$svc"
    done
    echo "=== All services restored ==="
elif [ $# -gt 0 ]; then
    for service in "$@"; do
        case "$service" in
            rocketchat) restore_rocketchat ;;
            gitlab) restore_gitlab ;;
            owncloud) restore_owncloud ;;
            plane) restore_plane ;;
            redis) restore_redis ;;
            *) echo "Unknown service: $service" ;;
        esac
    done
else
    echo "Usage: $0 [--all | service1 service2 ...]"
    echo "Services: rocketchat, gitlab, owncloud, plane, redis"
    exit 1
fi
