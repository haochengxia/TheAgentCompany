#!/bin/bash
# Fast service snapshot-restore system
# Replaces container restart resets (3-10 min) with DB/data restore (5-30 sec)
#
# Usage:
#   ./snapshot_create.sh        # Take snapshots of all services (run once after setup)
#   ./snapshot_restore.sh [service...]  # Restore specific services from snapshots
#   ./snapshot_restore.sh --all         # Restore all services

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$SCRIPT_DIR/snapshots}"
SERVERS_DIR="$(cd "$SCRIPT_DIR/../../servers" && pwd)"

mkdir -p "$SNAPSHOT_DIR"

create_snapshot() {
    local service="$1"
    echo "[snapshot] Creating snapshot for $service..."

    case "$service" in
        rocketchat)
            mkdir -p "$SNAPSHOT_DIR/rocketchat"
            docker exec rocketchat-mongodb sh -c 'mongodump --archive' > "$SNAPSHOT_DIR/rocketchat/db.dump"
            echo "[snapshot] RocketChat: mongodump saved"
            ;;
        gitlab)
            mkdir -p "$SNAPSHOT_DIR/gitlab"
            # GitLab data lives in its container's persisted volumes
            # The pre-baked image already contains initial data, so snapshot = record container ID
            docker inspect gitlab --format='{{.Id}}' > "$SNAPSHOT_DIR/gitlab/container_id" 2>/dev/null || true
            echo "[snapshot] GitLab: container ID recorded (uses image-baked data)"
            ;;
        owncloud)
            mkdir -p "$SNAPSHOT_DIR/owncloud"
            # ownCloud data is in the container - snapshot the data directory
            docker exec owncloud tar cf - -C /var/www/owncloud/data . > "$SNAPSHOT_DIR/owncloud/data.tar" 2>/dev/null || true
            echo "[snapshot] ownCloud: data directory saved"
            ;;
        plane)
            mkdir -p "$SNAPSHOT_DIR/plane"
            # Plane uses postgres - dump the database
            if docker ps --format '{{.Names}}' | grep -q 'plane-app'; then
                # Find the plane postgres container
                local pg_container
                pg_container=$(docker ps --format '{{.Names}}' | grep 'plane.*db\|plane.*postgres' | head -1)
                if [ -n "$pg_container" ]; then
                    docker exec "$pg_container" sh -c 'pg_dumpall -U plane' > "$SNAPSHOT_DIR/plane/db.dump" 2>/dev/null || true
                    echo "[snapshot] Plane: pg_dump saved"
                else
                    echo "[snapshot] Plane: no postgres container found, skipping"
                fi
            else
                echo "[snapshot] Plane: not running, skipping"
            fi
            ;;
        redis)
            mkdir -p "$SNAPSHOT_DIR/redis"
            # Redis BGSAVE creates an RDB snapshot
            docker exec redis-stack redis-cli -a theagentcompany BGSAVE 2>/dev/null || true
            docker cp redis-stack:/data/dump.rdb "$SNAPSHOT_DIR/redis/dump.rdb" 2>/dev/null || true
            echo "[snapshot] Redis: RDB snapshot saved"
            ;;
        *)
            echo "[snapshot] Unknown service: $service"
            return 1
            ;;
    esac
}

# Create snapshots for all services
echo "=== Creating service snapshots ==="
for service in rocketchat gitlab owncloud plane redis; do
    create_snapshot "$service" || true
done
echo "=== Snapshots saved to $SNAPSHOT_DIR ==="
echo ""
echo "To restore a service, run: ./snapshot_restore.sh <service>"
