import asyncio
import os
import shutil
import sys
from typing import List
import json
import yaml
import tempfile
import base64

from openhands.controller.state.state import State
from openhands.core.config import (
    OpenHandsConfig,
    SandboxConfig,
    LLMConfig,
    get_llm_config_arg,
    get_parser,
)
from openhands.core.config.agent_config import AgentConfig
from openhands.core.config.condenser_config import NoOpCondenserConfig
from openhands.core.logger import openhands_logger as logger
from openhands.core.main import create_runtime, run_controller
from openhands.events.action import CmdRunAction, MessageAction
from openhands.events.observation import CmdOutputObservation, BrowserOutputObservation
from openhands.runtime.base import Runtime
from openhands.utils.async_utils import call_async_from_sync

from browsing import pre_login

BASE_IMAGE = os.environ.get(
    'TAC_BASE_IMAGE',
    'tac-base-image:latest'
)


def get_config(
    base_container_image: str,
    task_short_name: str,
    mount_path_on_host: str,
    task_dir_on_host: str,
    llm_config: LLMConfig
) -> OpenHandsConfig:
    config = OpenHandsConfig(
        run_as_openhands=False,
        max_budget_per_task=4,
        max_iterations=100,
        save_trajectory_path=os.path.join(mount_path_on_host, f'traj_{task_short_name}.json'),
        sandbox=SandboxConfig(
            base_container_image=base_container_image,
            enable_auto_lint=True,
            use_host_network=True,
            timeout=300,
            api_key=os.environ.get('ALLHANDS_API_KEY', None),
        ),
        workspace_mount_path=mount_path_on_host,
        workspace_mount_path_in_sandbox='/outputs',
    )
    config.set_llm_config(llm_config)
    agent_config = AgentConfig(
        enable_prompt_extensions=False,
        enable_history_truncation=False,
        enable_som_visual_browsing=False,
        condenser=NoOpCondenserConfig(),
    )
    config.set_agent_config(agent_config)
    return config


def mount_task_files(runtime: Runtime, task_dir: str):
    """
    V2: Copy task files into the running container via /task mount point.
    The container's ENTRYPOINT (mount_task.sh) handles routing files to
    /instruction, /utils, /workspace, /npc etc.
    """
    abs_task_dir = os.path.abspath(task_dir)
    if not os.path.isdir(abs_task_dir):
        raise ValueError(f"Task directory does not exist: {abs_task_dir}")

    required_files = ['task.md', 'evaluator.py']
    for f in required_files:
        if not os.path.exists(os.path.join(abs_task_dir, f)):
            raise ValueError(f"Required file '{f}' not found in {abs_task_dir}")

    # The mount_task.sh ENTRYPOINT runs when the container starts.
    # We need to bind-mount the task directory as /task before the container starts.
    # OpenHands creates the container via SandboxConfig, so we use a different approach:
    # Copy files into the running container using runtime.run_action (shell commands).
    #
    # However, since OpenHands manages container lifecycle, we instead:
    # 1. Set base_container_image to our single base image
    # 2. Use workspace_mount_path to pass files through the mounted /outputs directory
    # 3. Run a setup command to copy task files into place

    # Copy all task files to the container via /outputs staging area
    # (which is already bind-mounted from host)
    for item in os.listdir(abs_task_dir):
        src = os.path.join(abs_task_dir, item)
        if os.path.isfile(src):
            with open(src, 'rb') as f:
                content = f.read()
            # We'll stage files and then move them in the container
            dst = os.path.join(os.environ.get('TMPDIR', tempfile.gettempdir()), item)
            shutil.copy2(src, dst)

    # Also copy any subdirectories (eval_data, app, etc.)
    for item in os.listdir(abs_task_dir):
        src = os.path.join(abs_task_dir, item)
        if os.path.isdir(src):
            dst = os.path.join(os.environ.get('TMPDIR', tempfile.gettempdir()), item)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)


def parse_dockerfile_copies(task_dir: str) -> dict[str, str]:
    """Parse the task's Dockerfile to extract exact COPY source->destination mappings.
    Returns {source_name: container_destination}
    """
    dockerfile = os.path.join(task_dir, 'Dockerfile')
    copies = {}
    if not os.path.exists(dockerfile):
        return copies
    with open(dockerfile) as f:
        for line in f:
            line = line.strip()
            if line.startswith('COPY ') and 'ONBUILD' not in line:
                parts = line.split()
                if len(parts) >= 3:
                    src = parts[-2]
                    dst = parts[-1].rstrip('/')
                    src_name = src.rstrip('/').split('/')[-1]
                    if src.endswith('/') and '/' in src[:-1]:
                        src_name = src.rstrip('/').split('/')[-1] + '/'
                    copies[src_name] = dst
    return copies


def setup_task_in_container(runtime: Runtime, task_dir: str):
    abs_task_dir = os.path.abspath(task_dir)
    tmp_dir = os.environ.get('TMPDIR', tempfile.gettempdir())

    staging_dir = os.path.join(tmp_dir, 'task_staging')
    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir)
    shutil.copytree(abs_task_dir, staging_dir)

    # Parse the task's Dockerfile to get EXACT file routing (no heuristics)
    dockerfile_routes = parse_dockerfile_copies(abs_task_dir)

    commands = []
    commands.append("mkdir -p /instruction /data")

    # task.md and dependencies.yml always go to the same place
    commands.append("cp /outputs/task_staging/task.md /instruction/task.md")
    commands.append("cp /outputs/task_staging/dependencies.yml /utils/dependencies.yml")

    # Encrypt evaluator
    commands.append("cp /outputs/task_staging/evaluator.py /utils/evaluator.py")
    commands.append("python_default /utils/encrypt.py")
    commands.append("rm -f /utils/evaluator.py /utils/encrypt.py")

    # Helper scripts always go to /utils
    for script in ['pre_init.py', 'post_init.py', 'populate_data.py', 'populate_db.py', 'prompts.py', 'helper.py']:
        commands.append(
            f"if [ -f /outputs/task_staging/{script} ]; then "
            f"cp /outputs/task_staging/{script} /utils/{script}; fi"
        )
    for script in ['pre_init.sh', 'post_init.sh']:
        commands.append(
            f"if [ -f /outputs/task_staging/{script} ]; then "
            f"cp /outputs/task_staging/{script} /utils/{script} && chmod +x /utils/{script}; fi"
        )

    # Route files using the Dockerfile's COPY mappings (ground truth, no guessing)
    handled_files = {
        'task.md', 'evaluator.py', 'dependencies.yml', 'scenarios.json',
        'Dockerfile', 'Makefile', 'checkpoints.md', 'README.md',
        'pre_init.py', 'post_init.py', 'post_init.sh', 'pre_init.sh',
        'populate_data.py', 'prompts.py', 'helper.py',
    }

    for src_name, dst in dockerfile_routes.items():
        src_name_clean = src_name.rstrip('/')
        if src_name_clean in handled_files:
            continue

        if dst == '/npc':
            commands.append(
                f"cp /outputs/task_staging/'{src_name_clean}' /npc/'{src_name_clean}'"
            )
            handled_files.add(src_name_clean)
        elif dst == '/data' or dst == '/data/':
            commands.append(
                f"cp /outputs/task_staging/'{src_name_clean}' /data/'{src_name_clean}'"
            )
            handled_files.add(src_name_clean)
        elif dst in ('/utils', '/utils/'):
            commands.append(
                f"cp /outputs/task_staging/'{src_name_clean}' /utils/'{src_name_clean}'"
            )
            handled_files.add(src_name_clean)
        elif dst.startswith('/workspace'):
            if src_name.endswith('/'):
                commands.append(
                    f"if [ -d /outputs/task_staging/'{src_name_clean}' ]; then "
                    f"cp -r /outputs/task_staging/'{src_name_clean}' {dst}; fi"
                )
            else:
                commands.append(
                    f"cp /outputs/task_staging/'{src_name_clean}' {dst}"
                )
            handled_files.add(src_name_clean)

    # Handle eval_data directory: V1 copies individual files from eval_data/ to /utils
    if os.path.isdir(os.path.join(abs_task_dir, 'eval_data')):
        commands.append(
            "if [ -d /outputs/task_staging/eval_data ]; then "
            "cp -r /outputs/task_staging/eval_data/* /utils/ 2>/dev/null || true; fi"
        )

    # Handle app directory if not already routed by Dockerfile
    if os.path.isdir(os.path.join(abs_task_dir, 'app')) and 'app/' not in dockerfile_routes:
        commands.append(
            "if [ -d /outputs/task_staging/app ]; then "
            "cp -r /outputs/task_staging/app /workspace/app; fi"
        )

    # Handle scenarios.json for NPC tasks if not in Dockerfile routes
    if 'scenarios.json' not in dockerfile_routes and os.path.exists(os.path.join(abs_task_dir, 'scenarios.json')):
        commands.append("cp /outputs/task_staging/scenarios.json /npc/scenarios.json")

    full_command = ' && '.join(commands)
    action = CmdRunAction(command=full_command)
    action.set_hard_timeout(120)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    if obs.exit_code != 0:
        raise RuntimeError(f"Task setup failed: {obs.content}")
    logger.info("Task files mounted and evaluator encrypted successfully")


def load_dependencies(runtime: Runtime) -> List[str]:
    command = "cat /utils/dependencies.yml"
    action = CmdRunAction(command=command)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs: CmdOutputObservation = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0
    dependencies = yaml.safe_load(obs.content)
    if dependencies is None:
        dependencies = []
    return dependencies


def init_task_env(runtime: Runtime, hostname: str, env_llm_config: LLMConfig):
    command = (
        f"SERVER_HOSTNAME={hostname} "
        f"LITELLM_API_KEY={env_llm_config.api_key.get_secret_value() if env_llm_config.api_key else None} "
        f"LITELLM_BASE_URL={env_llm_config.base_url} "
        f"LITELLM_MODEL={env_llm_config.model} "
        "echo '' | sudo tee -a /etc/hosts && "
        "bash /utils/init.sh"
    )
    action = CmdRunAction(command=command)
    action.set_hard_timeout(900)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0


def codeact_user_response(state: State) -> str:
    msg = (
        'Please continue working on the task on whatever approach you think is suitable.\n'
        'If you think you have solved the task, please finish the interaction.\n'
        'IMPORTANT: YOU SHOULD NEVER ASK FOR HUMAN HELP.\n'
    )
    if state.history:
        user_msgs = [
            event
            for event in state.history
            if isinstance(event, MessageAction) and event.source == 'user'
        ]
        if len(user_msgs) >= 2:
            return (
                msg
                + 'If you want to give up, run: <execute_bash> exit </execute_bash>.\n'
            )
    return msg


def run_solver(runtime: Runtime, task_name: str, config: OpenHandsConfig, dependencies: List[str],
               save_final_state: bool, state_dir: str,
               save_screenshots: bool, screenshots_dir: str) -> State:
    instruction = "Complete the task in /instruction/task.md"
    if 'gitlab' in dependencies:
        instruction += "\n\nGitlab username is 'root' and password is 'theagentcompany'"

    state: State | None = asyncio.run(
        run_controller(
            config=config,
            sid=task_name,
            initial_user_action=MessageAction(content=instruction),
            runtime=runtime,
            fake_user_response_fn=codeact_user_response,
        )
    )
    logger.info(state)

    if save_screenshots:
        screenshots_dir = os.path.join(screenshots_dir, task_name)
        os.makedirs(screenshots_dir, exist_ok=True)
        for image_id, obs in enumerate(state.history):
            if isinstance(obs, BrowserOutputObservation):
                image_data = base64.b64decode(
                    obs.screenshot.replace('data:image/png;base64,', '')
                )
                with open(os.path.join(screenshots_dir, f'{image_id}.png'), 'wb') as file:
                    file.write(image_data)

    if save_final_state:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, f'state_{task_name}.json'), 'w') as file:
            json.dump(str(state), file, indent=4)

    return state


def run_evaluator(runtime: Runtime, env_llm_config: LLMConfig, trajectory_path: str, result_path: str):
    command = (
        f"LITELLM_API_KEY={env_llm_config.api_key.get_secret_value() if env_llm_config.api_key else None} "
        f"LITELLM_BASE_URL={env_llm_config.base_url} "
        f"LITELLM_MODEL={env_llm_config.model} "
        f"DECRYPTION_KEY='theagentcompany is all you need' "
        f"python_default /utils/eval.py --trajectory_path {trajectory_path} --result_path {result_path}"
    )
    action = CmdRunAction(command=command)
    action.set_hard_timeout(600)
    logger.info(action, extra={'msg_type': 'ACTION'})
    obs = runtime.run_action(action)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.exit_code == 0


if __name__ == '__main__':
    parser = get_parser()
    parser.add_argument(
        '--task-dir',
        type=str,
        default=None,
        help='Path to task directory (v2: replaces --task-image-name)',
    )
    parser.add_argument(
        '--task-image-name',
        type=str,
        default=None,
        help='(Legacy v1) Task image name. Ignored if --task-dir is provided.',
    )
    parser.add_argument(
        '--outputs-path',
        type=str,
        default='./outputs',
        help='Folder path to save trajectories and evaluation results'
    )
    parser.add_argument(
        '--server-hostname',
        type=str,
        default='localhost',
        help='Server hostname',
    )
    parser.add_argument(
        '--agent-llm-config',
        type=str,
        default=None,
        help='LLM config for agent',
    )
    parser.add_argument(
        '--env-llm-config',
        type=str,
        default=None,
        help='LLM config for evaluation environment (NPC & llm-based evaluator)',
    )
    parser.add_argument(
        '--build-image-only',
        type=bool,
        default=False,
        help='Just build an OpenHands runtime image and then exit',
    )
    args, _ = parser.parse_known_args()

    # Resolve task identity
    if args.task_dir:
        task_dir = os.path.abspath(args.task_dir)
        task_short_name = os.path.basename(task_dir).replace('-image', '')
        base_image = BASE_IMAGE
        logger.info(f"V2 mode: task_dir={task_dir}, short_name={task_short_name}")
    elif args.task_image_name:
        base_image = args.task_image_name
        task_short_name = args.task_image_name.split('/')[-1].split(':')[0]
        task_dir = None
        logger.info(f"V1 compat mode: image={base_image}, short_name={task_short_name}")
    else:
        raise ValueError("Must provide either --task-dir (v2) or --task-image-name (v1)")

    # Setup temp directory for trajectory/result exchange
    if os.getenv('TMPDIR') and os.path.exists(os.getenv('TMPDIR')):
        temp_dir = os.path.abspath(os.getenv('TMPDIR'))
    else:
        temp_dir = tempfile.mkdtemp()

    # Build-only mode (pre-cache the OpenHands runtime image)
    if args.build_image_only:
        logger.info("build-image-only mode, will build runtime image and exit")
        config = get_config(base_image, task_short_name, temp_dir, task_dir, LLMConfig())
        runtime = create_runtime(config)
        call_async_from_sync(runtime.connect)
        logger.info(f"Built runtime image {runtime.runtime_container_image}")
        sys.exit()

    # Load LLM configs
    agent_llm_config = None
    if args.agent_llm_config:
        agent_llm_config = get_llm_config_arg(args.agent_llm_config)
    if agent_llm_config is None:
        raise ValueError(f'Could not find LLM config for agent: --agent-llm-config {args.agent_llm_config}')
    if agent_llm_config.api_key is None:
        raise ValueError('LLM API key is not set for agent')

    env_llm_config = None
    if args.env_llm_config:
        env_llm_config = get_llm_config_arg(args.env_llm_config)
    if env_llm_config is None:
        raise ValueError(f'Could not find LLM config for env: --env-llm-config {args.env_llm_config}')
    if env_llm_config.api_key is None:
        raise ValueError('LLM API key is not set for environment')

    # Create runtime with single base image
    config = get_config(base_image, task_short_name, temp_dir, task_dir, agent_llm_config)
    runtime = create_runtime(config)
    call_async_from_sync(runtime.connect)

    # V2: Mount task files into the running container
    if task_dir:
        setup_task_in_container(runtime, task_dir)

    # Standard evaluation pipeline (unchanged from v1)
    init_task_env(runtime, args.server_hostname, env_llm_config)
    dependencies = load_dependencies(runtime)
    logger.info(f"Service dependencies: {dependencies}")

    try:
        pre_login(runtime, dependencies, save_screenshots=True,
                  screenshots_dir=os.path.join(os.path.abspath(args.outputs_path), "screenshots"))
    except Exception as e:
        logger.error(f"Failed to pre-login: {e}")
        init_task_env(runtime, args.server_hostname, env_llm_config)
        pre_login(runtime, dependencies, save_screenshots=True,
                  screenshots_dir=os.path.join(os.path.abspath(args.outputs_path), "screenshots"))

    state = run_solver(runtime, task_short_name, config, dependencies,
                       save_final_state=True, state_dir=os.path.abspath(args.outputs_path),
                       save_screenshots=True,
                       screenshots_dir=os.path.join(os.path.abspath(args.outputs_path), "screenshots"))

    trajectory_path = f'/outputs/traj_{task_short_name}.json'
    result_path = f'/outputs/eval_{task_short_name}.json'

    run_evaluator(runtime, env_llm_config, trajectory_path, result_path)

    shutil.move(
        os.path.join(temp_dir, f'traj_{task_short_name}.json'),
        os.path.join(os.path.abspath(args.outputs_path), f'traj_{task_short_name}.json')
    )
    shutil.move(
        os.path.join(temp_dir, f'eval_{task_short_name}.json'),
        os.path.join(os.path.abspath(args.outputs_path), f'eval_{task_short_name}.json')
    )
