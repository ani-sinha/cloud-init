"""ansible enables running on first boot either ansible-pull"""

import abc
import logging
import os
import re
import sys
import sysconfig
from copy import deepcopy
from typing import List, Optional

from cloudinit import lifecycle, subp
from cloudinit.cloud import Cloud
from cloudinit.config import Config
from cloudinit.config.schema import MetaSchema
from cloudinit.distros import ALL_DISTROS, Distro
from cloudinit.settings import PER_INSTANCE
from cloudinit.util import get_cfg_by_path

meta: MetaSchema = {
    "id": "cc_ansible",
    "frequency": PER_INSTANCE,
    "distros": [ALL_DISTROS],
    "activate_by_schema_keys": ["ansible"],
}

LOG = logging.getLogger(__name__)
CFG_OVERRIDE = "ansible_config"


class AnsiblePull(abc.ABC):
    def __init__(self, distro: Distro):
        self.cmd_pull = ["ansible-pull"]
        self.cmd_version = ["ansible-pull", "--version"]
        self.distro = distro
        self.env = {}
        self.run_user: Optional[str] = None

        # some ansible modules directly reference os.environ["HOME"]
        # and cloud-init might not have that set, default: /root
        self.env["HOME"] = os.environ.get("HOME", "/root")

    def get_version(self) -> Optional[lifecycle.Version]:
        stdout, _ = self.do_as(self.cmd_version)
        first_line = stdout.splitlines().pop(0)
        matches = re.search(r"([\d\.]+)", first_line)
        if matches:
            version = matches.group(0)
            return lifecycle.Version.from_str(version)
        return None

    def pull(self, *args) -> str:
        stdout, _ = self.do_as([*self.cmd_pull, *args])
        return stdout

    def check_deps(self):
        if not self.is_installed():
            raise ValueError("command: ansible is not installed")

    def do_as(self, command: list, **kwargs):
        if not self.run_user:
            return self.subp(command, **kwargs)
        return self.distro.do_as(command, self.run_user, **kwargs)

    def subp(self, command, **kwargs):
        return subp.subp(command, update_env=self.env, **kwargs)

    @abc.abstractmethod
    def is_installed(self):
        pass

    @abc.abstractmethod
    def install(self, pkg_name: str):
        pass


class AnsiblePullPip(AnsiblePull):
    def __init__(self, distro: Distro, user: Optional[str]):
        super().__init__(distro)
        self.run_user = user
        self.add_pip_install_site_to_path()

    def add_pip_install_site_to_path(self):
        if self.run_user:
            user_base, _ = self.do_as(
                [
                    sys.executable,
                    "-c",
                    "import site; print(site.getuserbase())",
                ]
            )
            ansible_path = f"{user_base}/bin/"

            old_path = self.env.get("PATH")
            if old_path:
                self.env["PATH"] = ":".join([old_path, ansible_path])
            else:
                self.env["PATH"] = ansible_path

    def bootstrap_pip_if_required(self):
        try:
            import pip  # noqa: F401
        except ImportError:
            self.distro.install_packages([self.distro.pip_package_name])

    def install(self, pkg_name: str):
        """should cloud-init grow an interface for non-distro package
        managers? this seems reusable
        """
        self.bootstrap_pip_if_required()

        if not self.is_installed():
            cmd = [
                sys.executable,
                "-m",
                "pip",
                "install",
            ]

            if os.path.exists(
                os.path.join(
                    sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED"
                )
            ):
                cmd.append("--break-system-packages")
            if self.run_user:
                cmd.append("--user")

            self.__upgrade_pip(cmd)
            LOG.info("Installing the %s package", pkg_name)
            self.do_as([*cmd, pkg_name])
            LOG.info("Installed the %s package", pkg_name)

    def __upgrade_pip(self, cmd: list):
        """Try to upgrade pip then not raise an exception
        if the pip upgrade fails"""
        LOG.info("Upgrading pip")
        try:
            self.do_as([*cmd, "--upgrade", "pip"])
        except subp.ProcessExecutionError as e:
            LOG.warning(
                (
                    "Failed at upgrading pip. This is usually not critical"
                    "so the script will skip this step.\n%s"
                ),
                e,
            )
        else:
            LOG.info("Upgraded pip")

    def is_installed(self) -> bool:
        cmd = [sys.executable, "-m", "pip", "list"]

        if self.run_user:
            cmd.append("--user")

        stdout, _ = self.do_as(cmd)
        return "ansible" in stdout


class AnsiblePullDistro(AnsiblePull):
    def __init__(self, distro: Distro, user: Optional[str]):
        super().__init__(distro)
        self.run_user = user

    def install(self, pkg_name: str):
        if not self.is_installed():
            self.distro.install_packages([pkg_name])

    def is_installed(self) -> bool:
        return bool(subp.which("ansible"))


def handle(name: str, cfg: Config, cloud: Cloud, args: list) -> None:

    ansible_cfg: dict = cfg.get("ansible", {})
    ansible_user = ansible_cfg.get("run_user")
    install_method = ansible_cfg.get("install_method")
    setup_controller = ansible_cfg.get("setup_controller")

    galaxy_cfg = ansible_cfg.get("galaxy")
    pull_cfg = ansible_cfg.get("pull")
    package_name = ansible_cfg.get("package_name", "")

    if ansible_cfg:
        ansible: AnsiblePull
        validate_config(ansible_cfg)

        distro: Distro = cloud.distro
        if install_method == "pip":
            ansible = AnsiblePullPip(distro, ansible_user)
        else:
            ansible = AnsiblePullDistro(distro, ansible_user)
        ansible.install(package_name)
        ansible.check_deps()
        ansible_config = ansible_cfg.get("ansible_config", "")

        if ansible_config:
            ansible.env[CFG_OVERRIDE] = ansible_config

        if galaxy_cfg:
            ansible_galaxy(galaxy_cfg, ansible)

        if pull_cfg:
            if isinstance(pull_cfg, dict):
                pull_cfg = [pull_cfg]
            for cfg in pull_cfg:
                run_ansible_pull(ansible, deepcopy(cfg))

        if setup_controller:
            ansible_controller(setup_controller, ansible)


def validate_config(cfg: dict):
    required_keys = (
        "install_method",
        "package_name",
    )
    for key in required_keys:
        if not get_cfg_by_path(cfg, key):
            raise ValueError(f"Missing required key '{key}' from {cfg}")
    pull_cfg = cfg.get("pull")
    if pull_cfg:
        if isinstance(pull_cfg, dict):
            pull_cfg = [pull_cfg]
        elif not isinstance(pull_cfg, list):
            raise ValueError(
                "Invalid value ansible.pull. Expected either dict of list of"
                f" dicts but found {pull_cfg}"
            )
        for p_cfg in pull_cfg:
            if not isinstance(p_cfg, dict):
                raise ValueError(
                    "Invalid value of ansible.pull. Expected dict but found"
                    f" {p_cfg}"
                )
            if not get_cfg_by_path(p_cfg, "url"):
                raise ValueError(f"Missing required key 'url' from {p_cfg}")
            has_playbook = get_cfg_by_path(p_cfg, "playbook_name")
            has_playbooks = get_cfg_by_path(p_cfg, "playbook_names")
            if not any([has_playbook, has_playbooks]):
                raise ValueError(
                    f"Missing required key 'playbook_names' from {p_cfg}"
                )
            elif all([has_playbooks, has_playbook]):
                raise ValueError(
                    "Key 'ansible.pull.playbook_name' and"
                    " 'ansible.pull.playbook_names' are mutually exclusive."
                    f" Please use 'playbook_names' in {p_cfg}"
                )

    controller_cfg = cfg.get("setup_controller")
    if controller_cfg:
        if not any(
            [
                controller_cfg.get("repositories"),
                controller_cfg.get("run_ansible"),
            ]
        ):
            raise ValueError(f"Missing required key from {controller_cfg}")

    install = cfg["install_method"]
    if install not in ("pip", "distro"):
        raise ValueError("Invalid install method {install}")


def filter_args(cfg: dict) -> dict:
    """remove boolean false values"""
    return {
        key.replace("_", "-"): value
        for (key, value) in cfg.items()
        if value is not False
    }


def run_ansible_pull(pull: AnsiblePull, cfg: dict):
    playbook_name: str = cfg.pop("playbook_name", None)
    playbook_names: List[str] = cfg.pop("playbook_names", None)
    if playbook_name:
        playbook_names = [playbook_name]
    v = pull.get_version()
    if not v:
        LOG.warning("Cannot parse ansible version")
    elif v < lifecycle.Version(2, 7, 0):
        # diff was added in commit edaa0b52450ade9b86b5f63097ce18ebb147f46f
        if cfg.get("diff"):
            raise ValueError(
                f"Ansible version {v.major}.{v.minor}.{v.patch}"
                "doesn't support --diff flag, exiting."
            )
    pull_args = [
        f"--{key}={value}" if value is not True else f"--{key}"
        for key, value in filter_args(cfg).items()
    ]
    if v and v >= lifecycle.Version(2, 12, 0):
        # Multiple playbook support was resolved in 2.12.
        # https://github.com/ansible/ansible/pull/73172
        stdout = pull.pull(*pull_args, *playbook_names)
        if stdout:
            sys.stdout.write(f"{stdout}")
    else:
        # Older ansible must pull separate playbooks
        for playbook in playbook_names:
            stdout = pull.pull(*pull_args, playbook)
            if stdout:
                sys.stdout.write(f"{stdout}")


def ansible_galaxy(cfg: dict, ansible: AnsiblePull):
    actions = cfg.get("actions", [])

    if not actions:
        LOG.warning("Invalid config: %s", cfg)
    for command in actions:
        ansible.do_as(command)


def ansible_controller(cfg: dict, ansible: AnsiblePull):
    for repository in cfg.get("repositories", []):
        ansible.do_as(
            ["git", "clone", repository["source"], repository["path"]]
        )
    for args in cfg.get("run_ansible", []):
        playbook_dir = args.pop("playbook_dir")
        playbook_name = args.pop("playbook_name")
        command = [
            "ansible-playbook",
            playbook_name,
            *[f"--{key}={value}" for key, value in filter_args(args).items()],
        ]
        ansible.do_as(command, cwd=playbook_dir)
