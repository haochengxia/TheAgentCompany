import os
import shutil
import subprocess
import sys
import json
import yaml
import tempfile
import base64
import logging

from harness import BaseHarness, OpenHandsHarness, DockerHarness

logger = logging.getLogger(__name__)

BASE_IMAGE = os.environ.get("TAC_BASE_IMAGE", "tac-base-image:latest")


def load_dependencies(harness: BaseHarness, task_dir: str | None = None) -> list[str]:
    if task_dir:
        dep_path = os.path.join(task_dir, "dependencies.yml")
        if os.path.exists(dep_path):
            with open(dep_path) as f:
                dependencies = yaml.safe_load(f)
            if dependencies is None:
                dependencies = []
            return dependencies

    result = harness.run_command("cat /utils/dependencies.yml")
    assert result.exit_code == 0, f"Failed to load dependencies: {result.content}"
    raw = result.content
    lines = [l for l in raw.splitlines() if not l.lstrip().startswith("#")]
    dependencies = yaml.safe_load("\n".join(lines)) or []
    return dependencies


def init_task_env(harness: BaseHarness, hostname: str, llm_api_key: str | None,
                  llm_base_url: str | None, llm_model: str | None,
                  port_overrides: dict | None = None):
    """Initialize task environment inside the container.

    Args:
        harness: The harness to run commands through.
        hostname: Server hostname for SERVICE_HOSTNAME.
        llm_api_key: LITELLM API key.
        llm_base_url: LITELLM base URL.
        llm_model: LITELLM model name.
        port_overrides: Optional dict of service -> port mappings for multi-instance.
    """
    env_vars = (
        f"SERVER_HOSTNAME={hostname} "
        f"LITELLM_API_KEY={llm_api_key} "
        f"LITELLM_BASE_URL={llm_base_url} "
        f"LITELLM_MODEL={llm_model} "
    )

    if port_overrides:
        env_vars += f"GITLAB_PORT={port_overrides.get('gitlab', 8929)} "
        env_vars += f"API_PORT={port_overrides.get('api_server', 2999)} "
        env_vars += f"ROCKETCHAT_PORT={port_overrides.get('rocketchat', 3000)} "
        env_vars += f"OWNCLOUD_PORT={port_overrides.get('owncloud', 8092)} "
        env_vars += f"PLANE_PORT={port_overrides.get('plane', 8091)} "

    command = (
        env_vars +
        "echo '' | sudo tee -a /etc/hosts && "
        "bash /utils/init.sh"
    )
    result = harness.run_command(command, timeout=900)
    assert result.exit_code == 0, f"init_task_env failed: {result.content}"


def run_solver(harness: BaseHarness, task_name: str, dependencies: list[str],
               save_final_state: bool, state_dir: str,
               save_screenshots: bool, screenshots_dir: str):
    instruction = "Complete the task in /instruction/task.md"
    if "gitlab" in dependencies:
        instruction += "\n\nGitlab username is 'root' and password is 'theagentcompany'"

    state = harness.run_agent(instruction=instruction, max_iterations=100,
                              dependencies=dependencies)

    if save_screenshots and state.screenshots:
        task_screenshots_dir = os.path.join(screenshots_dir, task_name)
        os.makedirs(task_screenshots_dir, exist_ok=True)
        for image_id, screenshot_data in enumerate(state.screenshots):
            image_data = base64.b64decode(screenshot_data.replace("data:image/png;base64,", ""))
            with open(os.path.join(task_screenshots_dir, f"{image_id}.png"), "wb") as f:
                f.write(image_data)

    if save_final_state:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, f"state_{task_name}.json"), "w") as f:
            json.dump(str(state), f, indent=4)

    return state


def run_evaluator(harness: BaseHarness, llm_api_key: str | None,
                  llm_base_url: str | None, llm_model: str | None,
                  trajectory_path: str, result_path: str):
    command = (
        f"LITELLM_API_KEY={llm_api_key} "
        f"LITELLM_BASE_URL={llm_base_url} "
        f"LITELLM_MODEL={llm_model} "
        f"DECRYPTION_KEY='theagentcompany is all you need' "
        f"python_default /utils/eval.py --trajectory_path {trajectory_path} --result_path {result_path}"
    )
    result = harness.run_command(command, timeout=600)
    assert result.exit_code == 0, f"Evaluator failed: {result.content}"


def _build_port_overrides(service_instance: dict | None) -> dict | None:
    if service_instance is None:
        return None
    return {
        "gitlab": service_instance.get("gitlab_port", 8929),
        "api_server": service_instance.get("api_port", 2999),
        "rocketchat": service_instance.get("rocketchat_port", 3000),
        "owncloud": service_instance.get("owncloud_port", 8092),
        "plane": service_instance.get("plane_port", 8091),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TheAgentCompany V2 - Single task evaluation")
    parser.add_argument("--task-dir", type=str, default=None,
                        help="Path to task directory (v2 mode)")
    parser.add_argument("--task-image-name", type=str, default=None,
                        help="(Legacy v1) Task image name. Ignored if --task-dir is provided.")
    parser.add_argument("--outputs-path", type=str, default="./outputs",
                        help="Folder path to save trajectories and evaluation results")
    parser.add_argument("--server-hostname", type=str, default="localhost",
                        help="Server hostname")
    parser.add_argument("--harness", type=str, default="openhands",
                        choices=["openhands", "docker"],
                        help="Harness to use (default: openhands)")
    parser.add_argument("--base-image", type=str, default=None,
                        help="Base Docker image (default: TAC_BASE_IMAGE or tac-base-image:latest)")
    parser.add_argument("--agent-llm-config", type=str, default=None,
                        help="LLM config for agent (openhands harness only)")
    parser.add_argument("--env-llm-config", type=str, default=None,
                        help="LLM config for evaluation environment (NPC & evaluator)")
    parser.add_argument("--build-image-only", action="store_true",
                        help="Build runtime image and exit (openhands harness only)")
    parser.add_argument("--service-instance", type=str, default=None,
                        help="JSON string with service instance info (multi-instance mode)")
    args = parser.parse_args()

    service_instance = None
    if args.service_instance:
        service_instance = json.loads(args.service_instance)
        logger.info(f"Multi-instance mode: instance {service_instance.get('instance_id', 0)}")

    port_overrides = _build_port_overrides(service_instance)

    if args.task_dir:
        task_dir = os.path.abspath(args.task_dir)
        task_short_name = os.path.basename(task_dir).replace("-image", "")
        base_image = args.base_image or BASE_IMAGE
        logger.info(f"V2 mode: task_dir={task_dir}, short_name={task_short_name}")
    elif args.task_image_name:
        base_image = args.task_image_name
        task_short_name = args.task_image_name.split("/")[-1].split(":")[0]
        task_dir = None
        logger.info(f"V1 compat mode: image={base_image}, short_name={task_short_name}")
    else:
        raise ValueError("Must provide either --task-dir (v2) or --task-image-name (v1)")

    env_api_key = None
    env_base_url = None
    env_model = None
    temp_dir = os.path.abspath(os.getenv("TMPDIR") or tempfile.mkdtemp())
    mount_path = os.path.join(temp_dir, f"mount_{task_short_name}")
    os.makedirs(mount_path, exist_ok=True)

    if args.harness == "openhands":
        from openhands.core.config import LLMConfig, get_llm_config_arg

        agent_llm_config = None
        if args.agent_llm_config:
            agent_llm_config = get_llm_config_arg(args.agent_llm_config)
        if agent_llm_config is None:
            raise ValueError(f"Could not find LLM config for agent: --agent-llm-config {args.agent_llm_config}")
        if agent_llm_config.api_key is None:
            raise ValueError("LLM API key is not set for agent")

        env_llm_config = None
        if args.env_llm_config:
            env_llm_config = get_llm_config_arg(args.env_llm_config)
        if env_llm_config is None:
            raise ValueError(f"Could not find LLM config for env: --env-llm-config {args.env_llm_config}")
        if env_llm_config.api_key is None:
            raise ValueError("LLM API key is not set for environment")

        env_api_key = env_llm_config.api_key.get_secret_value() if env_llm_config.api_key else None
        env_base_url = env_llm_config.base_url
        env_model = env_llm_config.model

        if args.build_image_only:
            logger.info("build-image-only mode, creating runtime and exiting")
            harness = OpenHandsHarness(base_image=base_image, llm_config=LLMConfig(),
                                       task_short_name=task_short_name)
            harness.start(mount_path=mount_path)
            logger.info("Runtime image built successfully")
            sys.exit()

        harness = OpenHandsHarness(base_image=base_image, llm_config=agent_llm_config,
                                   task_short_name=task_short_name)
        harness.start(mount_path=mount_path)

    elif args.harness == "docker":
        if args.env_llm_config:
            env_api_key = os.environ.get("LITELLM_API_KEY")
            env_base_url = os.environ.get("LITELLM_BASE_URL")
            env_model = os.environ.get("LITELLM_MODEL")

        harness = DockerHarness(base_image=base_image or BASE_IMAGE)
        harness.start(mount_path=mount_path)

    if task_dir:
        staging_dir = os.path.join(mount_path, "task_staging")
        if os.path.exists(staging_dir):
            try:
                shutil.rmtree(staging_dir)
            except PermissionError:
                subprocess.run(["sudo", "rm", "-rf", staging_dir], check=True)
        shutil.copytree(task_dir, staging_dir)

        harness.setup_task_files(task_dir)

    init_task_env(harness, args.server_hostname, env_api_key, env_base_url, env_model,
                  port_overrides=port_overrides)
    dependencies = load_dependencies(harness, task_dir=task_dir)
    logger.info(f"Service dependencies: {dependencies}")

    outputs_path = os.path.abspath(args.outputs_path)
    os.makedirs(outputs_path, exist_ok=True)

    if isinstance(harness, OpenHandsHarness):
        from browsing import pre_login
        try:
            pre_login(harness._runtime, dependencies, save_screenshots=True,
                      screenshots_dir=os.path.join(outputs_path, "screenshots"))
        except Exception as e:
            logger.error(f"Failed to pre-login: {e}")
            init_task_env(harness, args.server_hostname, env_api_key, env_base_url, env_model,
                          port_overrides=port_overrides)
            pre_login(harness._runtime, dependencies, save_screenshots=True,
                      screenshots_dir=os.path.join(outputs_path, "screenshots"))

    state = run_solver(harness, task_short_name, dependencies,
                       save_final_state=True, state_dir=outputs_path,
                       save_screenshots=True,
                       screenshots_dir=os.path.join(outputs_path, "screenshots"))

    trajectory_path = f"/outputs/traj_{task_short_name}.json"
    result_path = f"/outputs/eval_{task_short_name}.json"

    run_evaluator(harness, env_api_key, env_base_url, env_model,
                  trajectory_path, result_path)

    shutil.move(
        os.path.join(mount_path, f"traj_{task_short_name}.json"),
        os.path.join(outputs_path, f"traj_{task_short_name}.json"),
    )
    shutil.move(
        os.path.join(mount_path, f"eval_{task_short_name}.json"),
        os.path.join(outputs_path, f"eval_{task_short_name}.json"),
    )

    harness.stop()
    logger.info(f"Task {task_short_name} completed successfully")
