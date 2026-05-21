#!/usr/bin/env python3
"""
Multi-instance service manager for TheAgentCompany benchmark.

Manages N independent sets of service containers. Each instance runs its own
docker-compose project with port offsets so they don't collide.

Architecture:
  - Instance 0: full stack (gitlab + rocketchat + owncloud + plane)
  - Instance 1..N: gitlab only (biggest bottleneck, 47 tasks)
  - Port offset formula: base_port + n * PORT_INCREMENT (default 10000)
  - Each instance has its own api-server container

Port mapping:
  Instance 0: gitlab=8929, api-server=2999, rocketchat=3000, owncloud=8092, plane=8091
  Instance 1: gitlab=18929, api-server=12999
  Instance 2: gitlab=28929, api-server=22999
  ...

Usage:
  from service_manager import ServiceManager
  mgr = ServiceManager(num_instances=2)
  mgr.start_all()
  mgr.reset_service(0, "gitlab")
  inst = mgr.acquire_instance(["gitlab"])  # returns 0 or 1
  mgr.release_instance(inst)
  mgr.stop_all()
"""

import json
import re
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Base ports for instance 0 (matches existing setup)
BASE_PORTS = {
    "gitlab": 8929,
    "api-server": 2999,
    "rocketchat": 3000,
    "owncloud": 8092,
    "owncloud-collabora": 9980,
    "plane": 8091,
    "mongodb": 27017,
    "redis": 6379,
}

# How much to offset per instance
PORT_INCREMENT = int(os.environ.get("TAC_PORT_INCREMENT", "10000"))

# Paths on the host
SERVERS_DIR = Path(os.environ.get("TAC_SERVERS_DIR", "/users/Haocheng/TheAgentCompany/servers"))
EVAL_DIR = Path(os.environ.get("TAC_EVAL_DIR", "/users/Haocheng/TheAgentCompany/evaluation_v2"))


class InstanceStatus(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class ServiceInstance:
    """Represents one set of service containers."""
    instance_id: int
    project_name: str
    services: list[str] = field(default_factory=lambda: ["gitlab"])
    status: InstanceStatus = InstanceStatus.STOPPED
    ports: dict[str, int] = field(default_factory=dict)
    locked_by: Optional[str] = None  # task name or group key
    compose_dir: Optional[Path] = None
    api_server_container: Optional[str] = None

    def __post_init__(self):
        if not self.ports:
            self.ports = self._compute_ports()
        if not self.compose_dir:
            self.compose_dir = EVAL_DIR / f".instance_{self.instance_id}"
        if not self.project_name:
            self.project_name = f"tac-inst-{self.instance_id}"

    def _compute_ports(self) -> dict[str, int]:
        """Compute port mapping for this instance."""
        offset = self.instance_id * PORT_INCREMENT
        return {svc: base + offset for svc, base in BASE_PORTS.items()}

    def get_port(self, service: str) -> int:
        return self.ports.get(service, BASE_PORTS.get(service, 0) + self.instance_id * PORT_INCREMENT)

    def info(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "project_name": self.project_name,
            "services": self.services,
            "status": self.status.value,
            "ports": dict(self.ports),
            "locked_by": self.locked_by,
        }


class ServiceManager:
    """
    Manages N independent service instances.

    Instance 0 always runs the full stack (gitlab + rocketchat + owncloud + plane).
    Instances 1..N run gitlab only (to parallelize the 47-task gitlab bottleneck).
    """

    def __init__(self, num_instances: int = 1, hostname: str = "localhost",
                 full_stack_ids: list[int] | None = None):
        """Create service manager.

        Args:
            num_instances: Total number of instances.
            hostname: Hostname for connection info.
            full_stack_ids: List of instance IDs that have full services.
                Default: [0] only (instance 0 has full stack, rest gitlab-only).
        """
        if full_stack_ids is None:
            full_stack_ids = [0]
        self.num_instances = num_instances
        self.hostname = hostname
        self.instances: dict[int, ServiceInstance] = {}
        self._lock = threading.Lock()

        for i in range(num_instances):
            if i in full_stack_ids:
                services = ["gitlab", "rocketchat", "owncloud", "plane"]
            else:
                services = ["gitlab"]
            inst = ServiceInstance(
                instance_id=i,
                project_name=f"tac-inst-{i}",
                services=services,
            )
            inst.status = InstanceStatus.RUNNING
            self.instances[i] = inst

    def _prepare_compose_dir(self, inst: ServiceInstance) -> Path:
        """Create a per-instance docker-compose directory with env file."""
        compose_dir = inst.compose_dir
        compose_dir.mkdir(parents=True, exist_ok=True)

        # Copy the original docker-compose.yml
        src = SERVERS_DIR / "docker-compose.yml"
        dst = compose_dir / "docker-compose.yml"
        shutil.copy2(src, dst)

        # Write .env with port overrides
        env_lines = [
            f"GITLAB_PORT={inst.get_port('gitlab')}",
            f"HOSTNAME={self.hostname}",
            f"HOST_PORT={inst.get_port('rocketchat')}",
            f"PORT=3000",
            f"ROCKETCHAT_PORT={inst.get_port('rocketchat')}",
        ]
        env_path = compose_dir / ".env"
        with open(env_path, "w") as f:
            f.write("\n".join(env_lines) + "\n")

        # Modify docker-compose.yml to use instance-specific container names
        # and port bindings
        self._patch_compose_file(dst, inst)

        return compose_dir

    def _patch_compose_file(self, compose_path: Path, inst: ServiceInstance):
        """Patch docker-compose.yml for instance-specific ports and container names."""

        with open(compose_path) as f:
            content = f.read()

        n = inst.instance_id
        suffix = f"-inst{n}" if n > 0 else ""


        # Replace container_name directives with instance-specific names
        content = re.sub(
            r"container_name:\s+gitlab\b",
            f"container_name: gitlab{suffix}",
            content,
        )
        content = re.sub(
            r"container_name:\s+rocketchat\b",
            f"container_name: rocketchat{suffix}",
            content,
        )
        content = re.sub(
            r"container_name:\s+rocketchat-mongodb\b",
            f"container_name: rocketchat-mongodb{suffix}",
            content,
        )
        content = re.sub(
            r"container_name:\s+owncloud\b",
            f"container_name: owncloud{suffix}",
            content,
        )
        content = re.sub(
            r"container_name:\s+owncloud-collabora\b",
            f"container_name: owncloud-collabora{suffix}",
            content,
        )
        content = re.sub(
            r"container_name:\s+redis-stack\b",
            f"container_name: redis-stack{suffix}",
            content,
        )
        content = re.sub(
            r"container_name:\s+redis-stack-npc-data-population\b",
            f"container_name: redis-stack-npc-data-population{suffix}",
            content,
        )

        # Fix hardcoded port bindings for owncloud and collabora (for instance 0 they stay the same)
        # Only patch for non-zero instances where these services shouldn't run
        if n > 0:
            # Patch owncloud port: "8092:80" -> "<offset_port>:80"
            oc_port = inst.get_port("owncloud")
            content = re.sub(r'"8092:80"', f'"{oc_port}:80"', content)
            # Patch collabora port
            collab_port = inst.get_port("owncloud-collabora")
            content = re.sub(r'"9980:9980"', f'"{collab_port}:9980"', content)
            # Patch redis port
            redis_port = inst.get_port("redis")
            content = re.sub(r'"6379:6379"', f'"{redis_port}:6379"', content)

        with open(compose_path, "w") as f:
            f.write(content)

    def _start_api_server(self, inst: ServiceInstance) -> str:
        """Start an api-server container for the instance."""
        n = inst.instance_id
        container_name = f"api-server-inst{n}"
        api_port = inst.get_port("api-server")

        # Build environment for the api-server
        env_flags = [
            f"-e SERVER_HOSTNAME={self.hostname}",
            f"-e GITLAB_PORT={inst.get_port('gitlab')}",
            f"-e ROCKETCHAT_PORT={inst.get_port('rocketchat')}",
            f"-e PLANE_PORT={inst.get_port('plane')}",
            "-e SKIP_SETUP=True",  # Don't let api-server start services
        ]

        cmd = (
            f"docker run -d --rm "
            f"--name {container_name} "
            f"--network host "
            f"-v /var/run/docker.sock:/var/run/docker.sock "
            f"{' '.join(env_flags)} "
            f"servers-api-server-image:latest"
        )

        logger.info(f"Starting api-server for instance {n}: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to start api-server for instance {n}: {result.stderr}")

        inst.api_server_container = container_name
        return container_name

    def _stop_api_server(self, inst: ServiceInstance):
        """Stop the api-server container for an instance."""
        if inst.api_server_container:
            subprocess.run(
                ["docker", "stop", "-t", "5", inst.api_server_container],
                capture_output=True, timeout=30,
            )
            subprocess.run(
                ["docker", "rm", "-f", inst.api_server_container],
                capture_output=True, timeout=15,
            )
            inst.api_server_container = None

    def start_instance(self, n: int, services: list[str] | None = None):
        """Start all services for instance n."""
        inst = self.instances[n]
        inst.status = InstanceStatus.STARTING

        try:
            compose_dir = self._prepare_compose_dir(inst)

            # Determine which services to start
            if services is None:
                if n == 0:
                    services_to_start = [
                        "gitlab", "rocketchat", "mongodb", "redis-stack",
                        "redis-stack-npc-data-population", "owncloud",
                        "owncloud-collabora",
                    ]
                else:
                    services_to_start = ["gitlab"]
            else:
                services_to_start = services

            services_arg = " ".join(services_to_start)
            env_file = compose_dir / ".env"

            cmd = (
                f"docker compose "
                f"-p {inst.project_name} "
                f"--env-file {env_file} "
                f"-f {compose_dir / 'docker-compose.yml'} "
                f"up {services_arg} -d"
            )

            logger.info(f"Starting instance {n}: {cmd}")
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=600,
            )

            if result.returncode != 0:
                logger.error(f"Failed to start instance {n}: {result.stderr}")
                inst.status = InstanceStatus.ERROR
                raise RuntimeError(f"docker compose up failed: {result.stderr}")

            # Start api-server for this instance
            self._start_api_server(inst)

            inst.status = InstanceStatus.RUNNING
            logger.info(f"Instance {n} started successfully")

        except Exception as e:
            inst.status = InstanceStatus.ERROR
            logger.error(f"Error starting instance {n}: {e}")
            raise

    def stop_instance(self, n: int):
        """Stop and remove all containers for instance n."""
        inst = self.instances[n]
        inst.status = InstanceStatus.STOPPING

        try:
            # Stop api-server first
            self._stop_api_server(inst)

            if inst.compose_dir and (inst.compose_dir / "docker-compose.yml").exists():
                cmd = (
                    f"docker compose "
                    f"-p {inst.project_name} "
                    f"-f {inst.compose_dir / 'docker-compose.yml'} "
                    f"down -v --remove-orphans"
                )
                logger.info(f"Stopping instance {n}: {cmd}")
                subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)

            # Also stop Plane for instance 0 (it uses a separate compose)
            if n == 0:
                subprocess.run(
                    ["docker", "compose", "-p", "plane-app", "-f",
                     "/users/Haocheng/TheAgentCompany/servers/plane/plane-app/docker-compose.yaml",
                     "down", "-v"],
                    capture_output=True, text=True, timeout=60,
                )

            inst.status = InstanceStatus.STOPPED
            logger.info(f"Instance {n} stopped")

        except Exception as e:
            inst.status = InstanceStatus.ERROR
            logger.error(f"Error stopping instance {n}: {e}")
            raise

    def reset_service(self, instance_n: int, service_name: str):
        """Reset a specific service in instance n via its api-server."""
        inst = self.instances[instance_n]
        api_port = inst.get_port("api-server")

        url = f"http://{self.hostname}:{api_port}/api/reset-{service_name}"
        logger.info(f"Resetting {service_name} on instance {instance_n}: {url}")

        try:
            import urllib.request
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode()
                logger.info(f"Reset response: {body}")
        except Exception as e:
            logger.warning(f"Reset failed for {service_name} on instance {instance_n}: {e}")
            # Fallback: try docker restart
            self._reset_via_docker_restart(inst, service_name)

    def _reset_via_docker_restart(self, inst: ServiceInstance, service_name: str):
        """Fallback: reset service by stopping/removing/recreating container."""
        n = inst.instance_id
        suffix = f"-inst{n}" if n > 0 else ""
        container_map = {
            "gitlab": f"gitlab{suffix}",
            "rocketchat": f"rocketchat{suffix}",
            "owncloud": f"owncloud{suffix}",
        }
        container_name = container_map.get(service_name)
        if not container_name:
            return

        compose_dir = inst.compose_dir
        if not compose_dir or not (compose_dir / "docker-compose.yml").exists():
            return

        cmd = (
            f"docker compose "
            f"-p {inst.project_name} "
            f"--env-file {compose_dir / '.env'} "
            f"-f {compose_dir / 'docker-compose.yml'} "
            f"stop {service_name} && "
            f"docker compose "
            f"-p {inst.project_name} "
            f"--env-file {compose_dir / '.env'} "
            f"-f {compose_dir / 'docker-compose.yml'} "
            f"rm -f {service_name} && "
            f"docker compose "
            f"-p {inst.project_name} "
            f"--env-file {compose_dir / '.env'} "
            f"-f {compose_dir / 'docker-compose.yml'} "
            f"up {service_name} -d"
        )

        logger.info(f"Fallback reset via docker: {cmd}")
        subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)

    def get_instance_info(self, n: int) -> dict:
        """Get info about instance n."""
        return self.instances[n].info()

    def acquire_instance(self, required_services: list[str], locked_by: str = "") -> int:
        """
        Find an idle instance that has all required_services.
        Prefers lighter instances (fewer services) so gitlab-only instances
        are preferred for gitlab-only tasks, leaving instance 0 for full-stack tasks.
        Blocks until one is available.
        """
        while True:
            with self._lock:
                # Collect eligible instances, sorted by service count (prefer lighter)
                candidates = []
                for inst_id, inst in self.instances.items():
                    if inst.status != InstanceStatus.RUNNING:
                        continue
                    if inst.locked_by is not None:
                        continue
                    if all(svc in inst.services for svc in required_services):
                        candidates.append((inst_id, len(inst.services)))
                
                candidates.sort(key=lambda x: x[1])
                
                if candidates:
                    inst_id = candidates[0][0]
                    inst = self.instances[inst_id]
                    inst.locked_by = locked_by or f"task-{time.time()}"
                    logger.info(f"Acquired instance {inst_id} for {required_services} (locked by {inst.locked_by})")
                    return inst_id

            # No instance available, wait
            logger.debug("No instance available, waiting...")
            time.sleep(5)

    def release_instance(self, instance_id: int):
        """Mark instance as available."""
        with self._lock:
            inst = self.instances[instance_id]
            logger.info(f"Releasing instance {instance_id} (was locked by {inst.locked_by})")
            inst.locked_by = None

    def start_all(self):
        """Start all instances."""
        for i in range(self.num_instances):
            logger.info(f"Starting instance {i}/{self.num_instances-1}...")
            self.start_instance(i)

    def stop_all(self):
        """Stop all instances."""
        for i in range(self.num_instances):
            try:
                self.stop_instance(i)
            except Exception as e:
                logger.warning(f"Error stopping instance {i}: {e}")

    def healthcheck(self, instance_n: int, service: str, timeout: int = 120) -> bool:
        """Wait for a service to become healthy."""
        inst = self.instances[instance_n]
        api_port = inst.get_port("api-server")
        url = f"http://{self.hostname}:{api_port}/api/healthcheck/{service}"

        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(5)
        return False

    def get_connection_info(self, instance_n: int) -> dict:
        """Get connection info for task containers to use with this instance."""
        inst = self.instances[instance_n]
        return {
            "instance_id": instance_n,
            "hostname": self.hostname,
            "api_port": inst.get_port("api-server"),
            "gitlab_port": inst.get_port("gitlab"),
            "rocketchat_port": inst.get_port("rocketchat"),
            "owncloud_port": inst.get_port("owncloud"),
            "plane_port": inst.get_port("plane"),
            "services": inst.services,
        }


# CLI interface for service_instances.sh
if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Service instance manager CLI")
    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="Start instances")
    start_p.add_argument("count", type=int, help="Number of instances")
    start_p.add_argument("--hostname", default="localhost")

    stop_p = sub.add_parser("stop", help="Stop all instances")
    stop_p.add_argument("--hostname", default="localhost")

    status_p = sub.add_parser("status", help="Show instance status")
    status_p.add_argument("--hostname", default="localhost")
    status_p.add_argument("--num-instances", type=int, default=1)

    reset_p = sub.add_parser("reset", help="Reset a service")
    reset_p.add_argument("--instance", type=int, required=True)
    reset_p.add_argument("--service", required=True)
    reset_p.add_argument("--hostname", default="localhost")

    args = parser.parse_args()

    if args.command == "start":
        mgr = ServiceManager(num_instances=args.count, hostname=args.hostname)
        mgr.start_all()
        print(json.dumps({i: mgr.get_instance_info(i) for i in range(args.count)}, indent=2))

    elif args.command == "stop":
        mgr = ServiceManager(num_instances=1, hostname=args.hostname)
        # Discover running instances
        for i in range(10):
            inst = ServiceInstance(instance_id=i, project_name=f"tac-inst-{i}", services=[])
            if inst.compose_dir and (inst.compose_dir / "docker-compose.yml").exists():
                try:
                    mgr.stop_instance(i)
                except Exception:
                    pass
        # Also stop any api-server containers
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        )
        for name in result.stdout.strip().split("\n"):
            if name.startswith("api-server-inst"):
                subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True)
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        print("All instances stopped")

    elif args.command == "status":
        for i in range(args.num_instances):
            inst = ServiceInstance(instance_id=i, project_name=f"tac-inst-{i}", services=[])
            # Check if containers are running
            result = subprocess.run(
                ["docker", "compose", "-p", f"tac-inst-{i}", "ps"],
                capture_output=True, text=True,
            )
            running = result.returncode == 0 and "gitlab" in result.stdout
            print(f"Instance {i}: {'RUNNING' if running else 'STOPPED'}")
            if running:
                print(f"  Ports: {inst._compute_ports()}")

    elif args.command == "reset":
        mgr = ServiceManager(num_instances=args.instance + 1, hostname=args.hostname)
        mgr.reset_service(args.instance, args.service)
        print(f"Reset {args.service} on instance {args.instance}")
