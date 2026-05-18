"""
Harness interface for TheAgentCompany benchmark evaluation.

Defines the abstract interface that any agent harness must implement,
plus two concrete implementations:

- DockerHarness: Plain Docker containers, no OpenHands dependency.
  Subclass and override run_agent() with your own agent logic.
- OpenHandsHarness: Wraps OpenHands runtime (original behavior).

Usage (custom agent):
    from harness import DockerHarness

    class MyAgentHarness(DockerHarness):
        def run_agent(self, instruction, max_iterations=100, **kwargs):
            self.run_command(f"echo '{instruction}' > /tmp/task.txt")
            return AgentState(success=True, trajectory_path="/tmp/traj.json")

    harness = MyAgentHarness(base_image="tac-base-image:latest")
    harness.start()
    harness.setup_task_files(task_dir)
    state = harness.run_agent(instruction="Complete the task in /instruction/task.md")
    harness.stop()

Usage (OpenHands, backward compatible):
    from harness import OpenHandsHarness
    harness = OpenHandsHarness(base_image="...", llm_config=agent_llm_config)
    harness.start(mount_path="/tmp/outputs")
    harness.setup_task_files(task_dir)
    state = harness.run_agent(instruction="Complete the task in /instruction/task.md")
"""

import asyncio
import json
import os
import subprocess
import tempfile
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def build_anti_drift_message(
    initial_instruction: str,
    user_msg_count: int,
    re_anchor_every: int = 5,
) -> str | None:
    if user_msg_count % re_anchor_every != 0:
        return None
    return (
        f"ANCHOR CHECK (prompt #{user_msg_count}): "
        f"Restate the final deliverable from the original task:\n---\n{initial_instruction}\n"
        f"---\n"
        "Do you have explicit evidence that the deliverable is complete? "
        "Check the world-state (file system, remote UI) before saying done. "
        "If not complete, continue working."
    )


def build_enhanced_user_response(
    initial_instruction: str,
    default_fn: Optional[Callable] = None,
    re_anchor_every: int = 5,
) -> Callable:
    def enhanced_response(state) -> str:
        from openhands.events.action import MessageAction

        user_msgs = [
            e for e in state.history
            if isinstance(e, MessageAction) and e.source == "user"
        ]
        count = len(user_msgs)

        if default_fn is not None:
            base_msg = default_fn(state)
        else:
            base_msg = (
                "Please continue working on the task. "
                "If you have finished, the task is done — no need to ask for help."
            )

        anchor = build_anti_drift_message(initial_instruction, count, re_anchor_every)
        if anchor is not None:
            return anchor

        return base_msg

    return enhanced_response


@dataclass
class CommandResult:
    exit_code: int
    content: str


@dataclass
class AgentState:
    success: bool
    trajectory_path: str
    history: list = field(default_factory=list)
    screenshots: list = field(default_factory=list)


def parse_dockerfile_copies(task_dir: str) -> dict[str, str]:
    dockerfile = os.path.join(task_dir, "Dockerfile")
    copies: dict[str, str] = {}
    if not os.path.exists(dockerfile):
        return copies
    with open(dockerfile) as f:
        for line in f:
            line = line.strip()
            if line.startswith("COPY ") and "ONBUILD" not in line:
                parts = line.split()
                if len(parts) >= 3:
                    src = parts[-2]
                    dst = parts[-1].rstrip("/")
                    src_name = src.rstrip("/").split("/")[-1]
                    if src.endswith("/") and "/" in src[:-1]:
                        src_name = src.rstrip("/").split("/")[-1] + "/"
                    copies[src_name] = dst
    return copies


class BaseHarness(ABC):

    @abstractmethod
    def start(self, mount_path: str | None = None):
        ...

    @abstractmethod
    def stop(self):
        ...

    @abstractmethod
    def run_command(self, command: str, timeout: int = 300) -> CommandResult:
        ...

    @abstractmethod
    def run_agent(self, instruction: str, max_iterations: int = 100,
                  fake_user_response: Optional[Callable] = None,
                  dependencies: Optional[list[str]] = None) -> AgentState:
        ...

    @abstractmethod
    def copy_to_container(self, src: str, dst: str):
        ...

    def setup_task_files(self, task_dir: str):
        abs_task_dir = os.path.abspath(task_dir)
        routes = parse_dockerfile_copies(abs_task_dir)

        commands: list[str] = []
        commands.append("mkdir -p /instruction /data")
        commands.append("cp /outputs/task_staging/task.md /instruction/task.md")
        commands.append("cp /outputs/task_staging/dependencies.yml /utils/dependencies.yml")
        commands.append("cp /outputs/task_staging/evaluator.py /utils/evaluator.py")
        commands.append("python_default /utils/encrypt.py")
        commands.append("rm -f /utils/evaluator.py /utils/encrypt.py")

        for script in ["pre_init.py", "post_init.py", "populate_data.py",
                        "populate_db.py", "prompts.py", "helper.py"]:
            commands.append(
                f"if [ -f /outputs/task_staging/{script} ]; then "
                f"cp /outputs/task_staging/{script} /utils/{script}; fi"
            )
        for script in ["pre_init.sh", "post_init.sh"]:
            commands.append(
                f"if [ -f /outputs/task_staging/{script} ]; then "
                f"cp /outputs/task_staging/{script} /utils/{script} && "
                f"chmod +x /utils/{script}; fi"
            )

        handled = {
            "task.md", "evaluator.py", "dependencies.yml", "scenarios.json",
            "Dockerfile", "Makefile", "checkpoints.md", "README.md",
            "pre_init.py", "post_init.py", "post_init.sh", "pre_init.sh",
            "populate_data.py", "populate_db.py", "prompts.py", "helper.py",
        }

        for src_name, dst in routes.items():
            if src_name in handled:
                continue
            commands.append(f"cp -r /outputs/task_staging/{src_name} {dst}/{src_name}")

        if "scenarios.json" not in routes and os.path.exists(os.path.join(abs_task_dir, "scenarios.json")):
            commands.append("cp /outputs/task_staging/scenarios.json /npc/scenarios.json")

        full_command = " && ".join(commands)
        result = self.run_command(full_command, timeout=120)
        if result.exit_code != 0:
            raise RuntimeError(f"Task setup failed: {result.content}")
        logger.info("Task files mounted and evaluator encrypted successfully")


class DockerHarness(BaseHarness):

    def __init__(self, base_image: str = "tac-base-image:latest",
                 container_name: str | None = None,
                 network: str = "host"):
        self.base_image = base_image
        self.container_name = container_name or f"tac-eval-{int(time.time())}"
        self.network = network
        self._mount_path: str | None = None

    def start(self, mount_path: str | None = None):
        self._mount_path = mount_path or tempfile.mkdtemp()
        os.makedirs(self._mount_path, exist_ok=True)
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            f"--network={self.network}",
            "-v", f"{self._mount_path}:/outputs",
            self.base_image,
            "tail", "-f", "/dev/null",
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        logger.info(f"Container {self.container_name} started (mount: {self._mount_path})")

    def stop(self):
        subprocess.run(["docker", "stop", self.container_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
        logger.info(f"Container {self.container_name} stopped")

    def run_command(self, command: str, timeout: int = 300) -> CommandResult:
        result = subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-c", command],
            capture_output=True, text=True, timeout=timeout,
        )
        return CommandResult(exit_code=result.returncode, content=result.stdout + result.stderr)

    def copy_to_container(self, src: str, dst: str):
        subprocess.run(
            ["docker", "cp", src, f"{self.container_name}:{dst}"],
            check=True, capture_output=True,
        )

    def run_agent(self, instruction: str, max_iterations: int = 100,
                  fake_user_response: Optional[Callable] = None,
                  dependencies: Optional[list[str]] = None) -> AgentState:
        raise NotImplementedError(
            "DockerHarness.run_agent() is a no-op by default. "
            "Subclass DockerHarness and override run_agent() with your agent logic."
        )


class OpenHandsHarness(BaseHarness):

    def __init__(self, base_image: str, llm_config=None, task_short_name: str = "task"):
        self.base_image = base_image
        self.llm_config = llm_config
        self.task_short_name = task_short_name
        self._runtime = None
        self._config = None
        self._mount_path: str | None = None

    def start(self, mount_path: str | None = None):
        from openhands.core.config import (
            OpenHandsConfig, SandboxConfig, LLMConfig, get_llm_config_arg, get_parser,
        )
        from openhands.core.config.agent_config import AgentConfig
        from openhands.core.config.condenser_config import NoOpCondenserConfig
        from openhands.core.main import create_runtime
        from openhands.utils.async_utils import call_async_from_sync

        self._mount_path = mount_path or tempfile.mkdtemp()
        llm = self.llm_config or LLMConfig()

        self._config = OpenHandsConfig(
            run_as_openhands=False,
            max_budget_per_task=4,
            max_iterations=100,
            save_trajectory_path=os.path.join(self._mount_path, f"traj_{self.task_short_name}.json"),
            sandbox=SandboxConfig(
                base_container_image=self.base_image,
                enable_auto_lint=True,
                use_host_network=True,
                timeout=300,
                api_key=os.environ.get("ALLHANDS_API_KEY"),
            ),
            workspace_mount_path=self._mount_path,
            workspace_mount_path_in_sandbox="/outputs",
        )
        self._config.set_llm_config(llm)
        agent_config = AgentConfig(
            enable_prompt_extensions=False,
            enable_history_truncation=False,
            enable_som_visual_browsing=False,
            condenser=NoOpCondenserConfig(),
        )
        self._config.set_agent_config(agent_config)

        self._runtime = create_runtime(self._config)
        call_async_from_sync(self._runtime.connect)

    def stop(self):
        if self._runtime:
            del self._runtime
            self._runtime = None

    def run_command(self, command: str, timeout: int = 300) -> CommandResult:
        from openhands.events.action import CmdRunAction

        action = CmdRunAction(command=command)
        action.set_hard_timeout(timeout)
        obs = self._runtime.run_action(action)
        logger.info(action, extra={"msg_type": "ACTION"})
        logger.info(obs, extra={"msg_type": "OBSERVATION"})
        return CommandResult(exit_code=obs.exit_code, content=obs.content)

    def run_agent(self, instruction: str, max_iterations: int = 100,
                  fake_user_response: Optional[Callable] = None,
                  dependencies: Optional[list[str]] = None) -> AgentState:
        from openhands.controller.state.state import State
        from openhands.core.main import run_controller
        from openhands.events.action import MessageAction

        def default_user_response(state):
            msg = (
                "Please continue working on the task on whatever approach you think is suitable.\n"
                "If you think you have solved the task, please finish the interaction.\n"
                "IMPORTANT: YOU SHOULD NEVER ASK FOR HUMAN HELP.\n"
            )
            if state.history:
                user_msgs = [
                    e for e in state.history
                    if isinstance(e, MessageAction) and e.source == "user"
                ]
                if len(user_msgs) >= 2:
                    return msg + "If you want to give up, run: <execute_bash> exit </execute_bash>.\n"
            return msg

        if fake_user_response is not None:
            enhanced_fn = fake_user_response
        else:
            enhanced_fn = build_enhanced_user_response(
                instruction,
                default_fn=default_user_response,
            )

        state: State | None = asyncio.run(
            run_controller(
                config=self._config,
                sid="eval",
                initial_user_action=MessageAction(content=instruction),
                runtime=self._runtime,
                fake_user_response_fn=enhanced_fn,
            )
        )

        return AgentState(
            success=state is not None,
            trajectory_path=self._config.save_trajectory_path,
            history=list(state.history) if state else [],
        )

    def copy_to_container(self, src: str, dst: str):
        raise NotImplementedError("OpenHandsHarness does not support copy_to_container")
