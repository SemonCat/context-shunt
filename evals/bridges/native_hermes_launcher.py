"""Local, reviewable plan-builder and lifecycle orchestrator for the isolated native
Hermes canary.

Every function that only *builds* argv/config is pure: it never touches a socket, a
subprocess, a credential, or the real USD ledger. The only code paths that can shell out
are `NativeCanaryOrchestrator` and `execute()`, and both require the caller to supply
their own `popen_runner`/`subprocess_runner` plus the exact `CONFIRM_TOKEN` -- this module
has no default runner, so nothing in this repo can accidentally run `ssh`/`docker` against
`hermes-ssh` just by importing or calling it. Every real-execution code path here has been
exercised only against fakes in `test_native_hermes_launcher.py`; it has never been run by
this session against the live host.

Design notes (fixed after review):

- The container's real work (relay + registration + one genuine `context_shunt_read`
  dispatch) all happens in a single `docker run` of
  `bridges.native_hermes_invoke --live-dispatch`, inside one process, one network
  namespace -- `native_hermes_invoke.live_run()` already starts the relay and performs
  the dispatch itself, so the launcher does not need (and must not run) a second,
  separate `docker run` for the relay alone.
- `docker run` targets the plain pinned image ID (`sha256:...`); there is no
  `context-shunt/hermes` repository tag for this digest, and `docker run <id>` does not
  need one.
- Every `docker run` (and the readiness probe) is wrapped through `ssh <host> ...` --
  the docker daemon lives on `hermes-ssh`, not on the operator's machine -- with each
  argv element `shlex.quote`d before being joined into the remote command line, since
  `ssh` does not quote its trailing arguments for you.
- The default image entrypoint boots an s6 supervisor; `--entrypoint` must be set
  explicitly to the image's own interpreter so `docker run <image> -m
  bridges.native_hermes_invoke ...` runs exactly that module and nothing else.
- `--read-only` requires an explicit writable `--tmpfs` for `HERMES_HOME`, the
  workspace, and the cache dir; harness/adapter/core code and the one synthetic source
  artifact are separate read-only bind mounts layered over that tmpfs root.
- The `ssh -R ... -N` reverse-forward process blocks forever by design (that's what
  keeps the forward open) so it must be started as a background `Popen`, then the
  launcher polls (over a *separate* short-lived `ssh ... test -S <path>` call) until the
  forwarded socket exists on the remote host, only then runs the docker command, and
  always tears the forward down afterwards -- this sequencing/cleanup lives in
  `NativeCanaryOrchestrator`, a real context manager, not a plan/argv printout.
- The reverse-forward argv sets `ControlMaster=no`/`ControlPath=none`: without them,
  terminating the forward's `Popen` can leave it alive on a shared multiplexed ssh
  connection instead of actually closing it.
- `run_dispatch(dispatch_timeout=...)` threads a timeout into `call_runner`; a real
  wrapper (see `main()`) converts `subprocess.TimeoutExpired` to `TimeoutError`, and on
  that, `run_dispatch` best-effort tears down only this run's own disposable container
  (`container_cleanup_argv()`, scoped by `container_name`) before re-raising.
- `main()` propagates the real dispatch's `.returncode` as its own exit code (not an
  unconditional 0), and returns 124 if the dispatch timed out.
"""
from __future__ import annotations

import dataclasses
import shlex
import sys
import time
from typing import Any, Callable, Optional

PINNED_IMAGE_DIGEST = "sha256:7ae35667fc2bd17cd8f6da6d117ca3f0a5754d8ebeb4b76bcff46c0768b9fb4b"
CONFIRM_TOKEN = "I_UNDERSTAND_THIS_RUNS_AGAINST_LIVE_HERMES"


class ImageDigestMismatch(RuntimeError):
    """The live host's image does not match PINNED_IMAGE_DIGEST."""


def validate_image_digest(reported_digest: str) -> None:
    """Fail closed unless the live image is exactly the reviewed, pinned digest.

    Callers must obtain ``reported_digest`` themselves via a read-only inspect (this
    module never runs that inspect) -- e.g. ``docker inspect <container> --format
    '{{.Image}}'`` resolved to its ``docker image inspect --format '{{.Id}}'``.
    """
    if reported_digest != PINNED_IMAGE_DIGEST:
        raise ImageDigestMismatch(
            f"live image {reported_digest!r} does not match pinned {PINNED_IMAGE_DIGEST!r}; "
            "refusing to build a plan against an unreviewed image"
        )


@dataclasses.dataclass(frozen=True)
class LaunchPlan:
    """Pure description of what an operator would run. Never executed by constructing it."""

    container_name: str
    ssh_host: str
    relay_listen_port: int
    container_socket_path: str  # path *inside* the container; bind-mounted from host_socket_path
    host_socket_path: str  # path on hermes-ssh where the ssh -R forward binds
    local_proxy_port: int  # TCP port of luna_budget_proxy.py on the operator's machine
    repo_mount_src: str  # read-only staged repo checkout, already present on hermes-ssh
    synthetic_source_host_path: str  # read-only staged synthetic artifact, already on hermes-ssh
    question: str
    repo_mount_dst: str = "/opt/context-shunt"
    tmpfs_path: str = "/run/context-shunt"
    tmpfs_size_mb: int = 32
    python_bin: str = "/opt/hermes/.venv/bin/python"
    model: str = "gpt-5.6-luna"

    @property
    def hermes_home(self) -> str:
        return f"{self.tmpfs_path}/hermes-home"

    @property
    def workspace_dir(self) -> str:
        return f"{self.tmpfs_path}/workspace"

    @property
    def cache_dir(self) -> str:
        return f"{self.tmpfs_path}/cache"

    @property
    def synthetic_source_container_path(self) -> str:
        return f"{self.workspace_dir}/artifact.txt"

    @property
    def adapter_init_path(self) -> str:
        return f"{self.repo_mount_dst}/adapters/hermes/context-shunt/__init__.py"

    def relay_base_url(self) -> str:
        """The `custom` provider base_url Hermes (inside the same container) would dial."""
        return f"http://127.0.0.1:{self.relay_listen_port}/v1"

    def docker_run_argv(self) -> list[str]:
        """Local (pre-ssh) docker argv. Runs the combined relay+invoke entrypoint --
        never a separate relay-only container -- inside one `--network none` namespace."""
        pythonpath = f"{self.repo_mount_dst}/evals:{self.repo_mount_dst}/packages/core-py/src"
        return [
            "sudo", "docker", "run", "--rm",
            "--name", self.container_name,
            "--network", "none",
            "--read-only",
            "--entrypoint", self.python_bin,
            "--tmpfs", f"{self.tmpfs_path}:rw,size={self.tmpfs_size_mb}m",
            "-v", f"{self.host_socket_path}:{self.container_socket_path}",
            "-v", f"{self.repo_mount_src}:{self.repo_mount_dst}:ro",
            "-v", f"{self.synthetic_source_host_path}:{self.synthetic_source_container_path}:ro",
            "-e", f"PYTHONPATH={pythonpath}",
            "-e", "NATIVE_HERMES_I_UNDERSTAND=1",
            PINNED_IMAGE_DIGEST,
            "-m", "bridges.native_hermes_invoke",
            "--live-dispatch",
            "--hermes-home", self.hermes_home,
            "--workspace-dir", self.workspace_dir,
            "--cache-dir", self.cache_dir,
            "--adapter-init-path", self.adapter_init_path,
            "--relay-listen-port", str(self.relay_listen_port),
            "--upstream-socket", self.container_socket_path,
            "--model", self.model,
            "--question", self.question,
            "--source-file", self.synthetic_source_container_path,
        ]

    def ssh_docker_run_argv(self) -> list[str]:
        """`docker_run_argv()` wrapped over ssh -- the docker daemon lives on
        `hermes-ssh`, not the operator's machine. Each element is `shlex.quote`d
        because ssh joins trailing argv with plain spaces and does not quote for you,
        which would otherwise split `self.question` (or any mount path with spaces)
        into multiple remote words."""
        return ["ssh", self.ssh_host] + [shlex.quote(part) for part in self.docker_run_argv()]

    def ssh_reverse_forward_argv(self) -> list[str]:
        """`ControlMaster=no`/`ControlPath=none` disable ssh's connection-sharing --
        without them, terminating this `Popen` can leave the forward alive on a shared
        multiplexed master connection instead of actually tearing it down."""
        return [
            "ssh",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StreamLocalBindUnlink=yes",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-R", f"{self.host_socket_path}:127.0.0.1:{self.local_proxy_port}",
            "-N", self.ssh_host,
        ]

    def ssh_socket_ready_probe_argv(self) -> list[str]:
        """Short-lived (non-blocking) check for the reverse-forward socket's existence
        on the remote host -- separate from the long-lived `-N` forward process."""
        return ["ssh", self.ssh_host, "test", "-S", self.host_socket_path]

    def container_cleanup_argv(self) -> list[str]:
        """Best-effort teardown of only *this run's own* disposable container, scoped
        by its exact `container_name` -- never a fleet-wide cleanup. Used after a
        dispatch timeout: `--rm` alone does not remove a container that ssh/docker
        still considers running until it is stopped."""
        return ["ssh", self.ssh_host, "docker", "rm", "-f", self.container_name]


def build_plan(
    *,
    ssh_host: str,
    relay_listen_port: int,
    local_proxy_port: int,
    repo_mount_src: str,
    synthetic_source_host_path: str,
    question: str,
    container_name: str = "context-shunt-native-canary",
    container_socket_path: str = "/run/context-shunt/upstream.sock",
    host_socket_path: str,
    model: str = "gpt-5.6-luna",
) -> LaunchPlan:
    return LaunchPlan(
        container_name=container_name,
        ssh_host=ssh_host,
        relay_listen_port=relay_listen_port,
        container_socket_path=container_socket_path,
        host_socket_path=host_socket_path,
        local_proxy_port=local_proxy_port,
        repo_mount_src=repo_mount_src,
        synthetic_source_host_path=synthetic_source_host_path,
        question=question,
        model=model,
    )


PopenRunner = Callable[[list[str]], Any]  # returns an object with .poll()/.terminate()/.wait()
# returns an object with .returncode (and usually output); accepts an optional
# `timeout` kwarg (seconds) -- a real wrapper must convert `subprocess.TimeoutExpired`
# to `TimeoutError` (see main()'s call_runner) so callers can catch one exception type.
CallRunner = Callable[..., Any]


class NativeCanaryOrchestrator:
    """Real lifecycle context manager: start the ssh reverse forward in the background,
    wait for its socket to exist remotely, then let the caller run the combined
    relay+invoke container over ssh, and always tear the forward down afterwards.

    Every side effect goes through the injected `popen_runner`/`call_runner`; this class
    has no default for either, so constructing or using it cannot shell out on its own.
    Tests exercise this exclusively with fakes -- it must never be pointed at a real
    `popen_runner`/`call_runner` by anything this session runs.
    """

    def __init__(
        self,
        plan: LaunchPlan,
        *,
        confirm_token: str,
        popen_runner: PopenRunner,
        call_runner: CallRunner,
        ready_timeout: float = 30.0,
        poll_interval: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if confirm_token != CONFIRM_TOKEN:
            raise PermissionError("NativeCanaryOrchestrator requires the exact CONFIRM_TOKEN")
        self._plan = plan
        self._popen_runner = popen_runner
        self._call_runner = call_runner
        self._ready_timeout = ready_timeout
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock
        self._forward_process: Optional[Any] = None

    def __enter__(self) -> "NativeCanaryOrchestrator":
        self._forward_process = self._popen_runner(self._plan.ssh_reverse_forward_argv())
        try:
            self._wait_for_forward_socket()
        except Exception:
            # __exit__ is never called if __enter__ raises, so this is the only chance
            # to avoid leaking the background ssh -N forward process.
            self._terminate_forward()
            raise
        return self

    def _wait_for_forward_socket(self) -> None:
        deadline = self._clock() + self._ready_timeout
        probe_argv = self._plan.ssh_socket_ready_probe_argv()
        while True:
            result = self._call_runner(probe_argv)
            if getattr(result, "returncode", 1) == 0:
                return
            if self._clock() >= deadline:
                raise TimeoutError(
                    f"reverse-forward socket {self._plan.host_socket_path!r} on "
                    f"{self._plan.ssh_host!r} never became ready within {self._ready_timeout}s"
                )
            self._sleep(self._poll_interval)

    def run_dispatch(self, *, dispatch_timeout: Optional[float] = None) -> Any:
        """Run the combined relay+invoke container over ssh and return the call result.
        Must only be called after `__enter__` has confirmed the forward socket exists.

        On a `TimeoutError` from `call_runner` (the real wrapper converts
        `subprocess.TimeoutExpired` to this), best-effort tears down only this run's own
        disposable container (`container_cleanup_argv()`, scoped by `container_name`)
        before re-raising -- `docker run --rm` does not clean up a container that is
        still considered running because its process was never signaled to stop."""
        try:
            return self._call_runner(self._plan.ssh_docker_run_argv(), timeout=dispatch_timeout)
        except TimeoutError:
            try:
                self._call_runner(self._plan.container_cleanup_argv())
            except Exception:
                pass
            raise

    def _terminate_forward(self) -> None:
        if self._forward_process is not None:
            self._forward_process.terminate()
            try:
                self._forward_process.wait(timeout=5)
            except Exception:
                self._forward_process.kill()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._terminate_forward()
        return False


def execute(
    plan: LaunchPlan,
    *,
    dry_run: bool = True,
    confirm_token: Optional[str] = None,
    popen_runner: Optional[PopenRunner] = None,
    call_runner: Optional[CallRunner] = None,
    dispatch_timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Dry-run by default: returns the plan's argv/config as plain data, runs nothing.

    Only shells out when `dry_run=False` *and* `confirm_token == CONFIRM_TOKEN` *and* the
    caller supplies both `popen_runner` and `call_runner` (e.g. real
    `subprocess.Popen`/`subprocess.run` wrappers). There is no default for either, so no
    code path here can execute anything without an operator explicitly wiring them in.
    """
    rendered = {
        "ssh_reverse_forward_argv": plan.ssh_reverse_forward_argv(),
        "ssh_socket_ready_probe_argv": plan.ssh_socket_ready_probe_argv(),
        "ssh_docker_run_argv": plan.ssh_docker_run_argv(),
        "relay_base_url": plan.relay_base_url(),
        "hermes_home": plan.hermes_home,
        "workspace_dir": plan.workspace_dir,
        "cache_dir": plan.cache_dir,
    }
    if dry_run:
        return {"dry_run": True, **rendered}
    if popen_runner is None or call_runner is None:
        raise RuntimeError(
            "execute(dry_run=False) requires explicit popen_runner and call_runner; "
            "this module supplies no default so nothing runs by accident"
        )
    with NativeCanaryOrchestrator(
        plan, confirm_token=confirm_token or "", popen_runner=popen_runner, call_runner=call_runner,
    ) as orchestrator:
        result = orchestrator.run_dispatch(dispatch_timeout=dispatch_timeout)
    return {"dry_run": False, **rendered, "result": result}


def main(argv: Optional[list[str]] = None) -> int:
    """Executable CLI: prints the rendered plan by default (no ssh/docker/socket
    touched). Real execution requires `--execute`, `--confirm-token`, and wiring real
    `subprocess.Popen`/`subprocess.run` at this call site -- an operator's job, not
    something this module or this session performs on its own."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--relay-listen-port", type=int, required=True)
    parser.add_argument("--local-proxy-port", type=int, required=True)
    parser.add_argument("--host-socket-path", required=True)
    parser.add_argument("--repo-mount-src", required=True)
    parser.add_argument("--synthetic-source-host-path", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-token", default=None)
    parser.add_argument(
        "--dispatch-timeout-seconds", type=float, default=None,
        help="abort the docker-run dispatch and clean up its container after this many seconds",
    )
    args = parser.parse_args(argv)

    plan = build_plan(
        ssh_host=args.ssh_host, relay_listen_port=args.relay_listen_port,
        local_proxy_port=args.local_proxy_port, host_socket_path=args.host_socket_path,
        repo_mount_src=args.repo_mount_src, synthetic_source_host_path=args.synthetic_source_host_path,
        question=args.question, model=args.model,
    )

    if not args.execute:
        print(json.dumps(execute(plan, dry_run=True), indent=2))
        return 0

    import subprocess

    def popen_runner(cmd: list[str]) -> "subprocess.Popen[bytes]":
        return subprocess.Popen(cmd)

    def call_runner(cmd: list[str], *, timeout: Optional[float] = None) -> "subprocess.CompletedProcess[bytes]":
        try:
            return subprocess.run(cmd, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(str(exc)) from exc

    try:
        result = execute(
            plan, dry_run=False, confirm_token=args.confirm_token,
            popen_runner=popen_runner, call_runner=call_runner,
            dispatch_timeout=args.dispatch_timeout_seconds,
        )
    except TimeoutError as exc:
        print(json.dumps({"dry_run": False, "timeout": str(exc)}, indent=2), file=sys.stderr)
        return 124

    print(json.dumps({k: v for k, v in result.items() if k != "result"}, indent=2))
    return getattr(result.get("result"), "returncode", 0)


if __name__ == "__main__":
    raise SystemExit(main())
