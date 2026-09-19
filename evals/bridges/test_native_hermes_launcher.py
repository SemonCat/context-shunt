from __future__ import annotations

import shlex

import pytest

import bridges.native_hermes_launcher as launcher
from bridges.native_hermes_launcher import (
    CONFIRM_TOKEN,
    PINNED_IMAGE_DIGEST,
    ImageDigestMismatch,
    NativeCanaryOrchestrator,
    build_plan,
    execute,
    validate_image_digest,
)


def _plan() -> launcher.LaunchPlan:
    return build_plan(
        ssh_host="hermes-ssh",
        relay_listen_port=18080,
        local_proxy_port=9443,
        host_socket_path="/run/context-shunt/upstream.sock",
        repo_mount_src="/opt/staged/context-shunt-repo",
        synthetic_source_host_path="/opt/staged/artifact.txt",
        question="what does the synthetic artifact say?",
    )


def test_validate_image_digest_accepts_pinned_and_rejects_other() -> None:
    validate_image_digest(PINNED_IMAGE_DIGEST)
    with pytest.raises(ImageDigestMismatch):
        validate_image_digest("sha256:0000000000000000000000000000000000000000000000000000000000000000")


def test_docker_run_argv_uses_plain_digest_entrypoint_override_and_tmpfs() -> None:
    plan = _plan()
    argv = plan.docker_run_argv()
    assert PINNED_IMAGE_DIGEST in argv
    assert "context-shunt/hermes@" not in " ".join(argv)
    assert argv[argv.index("--entrypoint") + 1] == plan.python_bin
    assert f"{plan.tmpfs_path}:rw,size=32m" in argv
    assert "-m" in argv and argv[argv.index("-m") + 1] == "bridges.native_hermes_invoke"
    assert "--live-dispatch" in argv
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv


def test_docker_run_argv_mounts_repo_socket_and_synthetic_source_read_only() -> None:
    plan = _plan()
    argv = plan.docker_run_argv()
    joined = " ".join(argv)
    assert f"{plan.host_socket_path}:{plan.container_socket_path}" in joined
    assert f"{plan.repo_mount_src}:{plan.repo_mount_dst}:ro" in joined
    assert f"{plan.synthetic_source_host_path}:{plan.synthetic_source_container_path}:ro" in joined
    assert plan.synthetic_source_container_path.startswith(plan.workspace_dir)


def test_ssh_docker_run_argv_wraps_with_ssh_and_quotes_every_element() -> None:
    plan = _plan()
    ssh_argv = plan.ssh_docker_run_argv()
    assert ssh_argv[0] == "ssh"
    assert ssh_argv[1] == plan.ssh_host
    for raw, quoted in zip(plan.docker_run_argv(), ssh_argv[2:]):
        assert quoted == shlex.quote(raw)


def test_ssh_docker_run_argv_survives_a_question_containing_spaces_and_quotes() -> None:
    plan = build_plan(
        ssh_host="hermes-ssh", relay_listen_port=18080, local_proxy_port=9443,
        host_socket_path="/run/context-shunt/upstream.sock",
        repo_mount_src="/opt/staged/repo", synthetic_source_host_path="/opt/staged/artifact.txt",
        question="what's the \"synthetic\" answer here?",
    )
    ssh_argv = plan.ssh_docker_run_argv()
    rejoined = shlex.split(" ".join(ssh_argv[2:]))
    assert plan.question in rejoined


def test_ssh_reverse_forward_argv_backgrounds_with_dash_n_and_correct_bind() -> None:
    plan = _plan()
    argv = plan.ssh_reverse_forward_argv()
    assert "-N" in argv
    assert f"{plan.host_socket_path}:127.0.0.1:{plan.local_proxy_port}" in argv


def test_ssh_reverse_forward_argv_disables_control_master() -> None:
    plan = _plan()
    argv = plan.ssh_reverse_forward_argv()
    joined = " ".join(argv)
    assert "-o ControlMaster=no" in joined
    assert "-o ControlPath=none" in joined


def test_ssh_socket_ready_probe_is_a_separate_short_lived_call() -> None:
    plan = _plan()
    probe = plan.ssh_socket_ready_probe_argv()
    assert probe == ["ssh", plan.ssh_host, "test", "-S", plan.host_socket_path]
    assert probe != plan.ssh_reverse_forward_argv()


def test_container_cleanup_argv_is_scoped_to_this_runs_own_container() -> None:
    plan = _plan()
    assert plan.container_cleanup_argv() == ["ssh", plan.ssh_host, "docker", "rm", "-f", plan.container_name]


def test_execute_dry_run_never_touches_a_runner() -> None:
    plan = _plan()
    result = execute(plan, dry_run=True)
    assert result["dry_run"] is True
    assert "ssh_docker_run_argv" in result


def test_execute_live_requires_confirm_token_and_both_runners() -> None:
    plan = _plan()
    with pytest.raises(RuntimeError):
        execute(plan, dry_run=False, confirm_token=CONFIRM_TOKEN)


class _FakePopen:
    def __init__(self) -> None:
        self.terminated = False
        self.waited = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


class _FakeCompleted:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_orchestrator_rejects_wrong_confirm_token_before_any_runner_call() -> None:
    plan = _plan()
    with pytest.raises(PermissionError):
        NativeCanaryOrchestrator(
            plan, confirm_token="wrong",
            popen_runner=lambda argv: _FakePopen(),
            call_runner=lambda argv, **kw: _FakeCompleted(),
        )


def test_orchestrator_waits_for_socket_then_dispatches_then_tears_down_forward() -> None:
    plan = _plan()
    popen_calls: list[list[str]] = []
    call_calls: list[list[str]] = []
    probe_results = iter([_FakeCompleted(1), _FakeCompleted(1), _FakeCompleted(0)])
    fake_popen = _FakePopen()

    def popen_runner(argv: list[str]) -> _FakePopen:
        popen_calls.append(argv)
        return fake_popen

    def call_runner(argv: list[str], *, timeout: float | None = None) -> _FakeCompleted:
        call_calls.append(argv)
        if argv == plan.ssh_socket_ready_probe_argv():
            return next(probe_results)
        return _FakeCompleted(0)

    sleeps: list[float] = []
    with NativeCanaryOrchestrator(
        plan, confirm_token=CONFIRM_TOKEN, popen_runner=popen_runner, call_runner=call_runner,
        poll_interval=0.0, sleep=sleeps.append,
    ) as orchestrator:
        result = orchestrator.run_dispatch()

    assert popen_calls == [plan.ssh_reverse_forward_argv()]
    assert call_calls[-1] == plan.ssh_docker_run_argv()
    assert call_calls.count(plan.ssh_socket_ready_probe_argv()) == 3
    assert result.returncode == 0
    assert fake_popen.terminated is True
    assert fake_popen.waited is True


def test_orchestrator_tears_down_forward_even_if_dispatch_raises() -> None:
    plan = _plan()
    fake_popen = _FakePopen()

    def call_runner(argv: list[str], *, timeout: float | None = None) -> _FakeCompleted:
        if argv == plan.ssh_socket_ready_probe_argv():
            return _FakeCompleted(0)
        raise RuntimeError("dispatch blew up")

    with pytest.raises(RuntimeError):
        with NativeCanaryOrchestrator(
            plan, confirm_token=CONFIRM_TOKEN,
            popen_runner=lambda argv: fake_popen, call_runner=call_runner,
        ) as orchestrator:
            orchestrator.run_dispatch()

    assert fake_popen.terminated is True


def test_orchestrator_raises_timeout_if_socket_never_becomes_ready() -> None:
    plan = _plan()
    fake_popen = _FakePopen()
    clock = iter([0.0, 0.0, 1.0, 100.0])

    with pytest.raises(TimeoutError):
        with NativeCanaryOrchestrator(
            plan, confirm_token=CONFIRM_TOKEN,
            popen_runner=lambda argv: fake_popen,
            call_runner=lambda argv, **kw: _FakeCompleted(1),
            ready_timeout=5.0, poll_interval=0.0, sleep=lambda s: None,
            clock=lambda: next(clock),
        ):
            pass
    assert fake_popen.terminated is True


def test_run_dispatch_passes_dispatch_timeout_through_to_call_runner() -> None:
    plan = _plan()
    seen_timeouts: list[float | None] = []

    def call_runner(argv: list[str], *, timeout: float | None = None) -> _FakeCompleted:
        if argv == plan.ssh_socket_ready_probe_argv():
            return _FakeCompleted(0)
        seen_timeouts.append(timeout)
        return _FakeCompleted(0)

    with NativeCanaryOrchestrator(
        plan, confirm_token=CONFIRM_TOKEN,
        popen_runner=lambda argv: _FakePopen(), call_runner=call_runner,
    ) as orchestrator:
        orchestrator.run_dispatch(dispatch_timeout=42.0)

    assert seen_timeouts == [42.0]


def test_run_dispatch_cleans_up_only_this_runs_container_on_timeout_then_reraises() -> None:
    plan = _plan()
    cleanup_calls: list[list[str]] = []

    def call_runner(argv: list[str], *, timeout: float | None = None) -> _FakeCompleted:
        if argv == plan.ssh_socket_ready_probe_argv():
            return _FakeCompleted(0)
        if argv == plan.container_cleanup_argv():
            cleanup_calls.append(argv)
            return _FakeCompleted(0)
        raise TimeoutError("dispatch exceeded timeout")

    with pytest.raises(TimeoutError):
        with NativeCanaryOrchestrator(
            plan, confirm_token=CONFIRM_TOKEN,
            popen_runner=lambda argv: _FakePopen(), call_runner=call_runner,
        ) as orchestrator:
            orchestrator.run_dispatch(dispatch_timeout=1.0)

    assert cleanup_calls == [plan.container_cleanup_argv()]


def test_run_dispatch_timeout_cleanup_failure_does_not_mask_the_original_timeout() -> None:
    plan = _plan()

    def call_runner(argv: list[str], *, timeout: float | None = None) -> _FakeCompleted:
        if argv == plan.ssh_socket_ready_probe_argv():
            return _FakeCompleted(0)
        if argv == plan.container_cleanup_argv():
            raise RuntimeError("ssh to hermes-ssh also failed")
        raise TimeoutError("dispatch exceeded timeout")

    with pytest.raises(TimeoutError):
        with NativeCanaryOrchestrator(
            plan, confirm_token=CONFIRM_TOKEN,
            popen_runner=lambda argv: _FakePopen(), call_runner=call_runner,
        ) as orchestrator:
            orchestrator.run_dispatch(dispatch_timeout=1.0)


def test_execute_live_propagates_dispatch_timeout_into_run_dispatch() -> None:
    plan = _plan()
    seen_timeouts: list[float | None] = []

    def call_runner(argv: list[str], *, timeout: float | None = None) -> _FakeCompleted:
        if argv == plan.ssh_socket_ready_probe_argv():
            return _FakeCompleted(0)
        seen_timeouts.append(timeout)
        return _FakeCompleted(0)

    execute(
        plan, dry_run=False, confirm_token=CONFIRM_TOKEN,
        popen_runner=lambda argv: _FakePopen(), call_runner=call_runner,
        dispatch_timeout=7.5,
    )
    assert seen_timeouts == [7.5]


def test_main_execute_propagates_the_real_dispatch_returncode(monkeypatch) -> None:
    import subprocess

    class _FakeCompletedProcess:
        returncode = 3

    monkeypatch.setattr(subprocess, "run", lambda cmd, timeout=None: _FakeCompletedProcess())
    monkeypatch.setattr(subprocess, "Popen", lambda cmd: _FakePopen())
    monkeypatch.setattr(
        launcher, "NativeCanaryOrchestrator",
        lambda plan, **kw: _StubOrchestrator(_FakeCompletedProcess()),
    )

    rc = launcher.main([
        "--ssh-host", "hermes-ssh", "--relay-listen-port", "18080", "--local-proxy-port", "9443",
        "--host-socket-path", "/run/context-shunt/upstream.sock",
        "--repo-mount-src", "/opt/staged/repo", "--synthetic-source-host-path", "/opt/staged/artifact.txt",
        "--question", "what does the synthetic artifact say?",
        "--execute", "--confirm-token", CONFIRM_TOKEN,
    ])
    assert rc == 3


class _StubOrchestrator:
    def __init__(self, result: object) -> None:
        self._result = result

    def __enter__(self) -> "_StubOrchestrator":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def run_dispatch(self, *, dispatch_timeout: float | None = None) -> object:
        return self._result


def test_main_execute_returns_124_on_dispatch_timeout(monkeypatch, capsys) -> None:
    import subprocess

    class _RaisingOrchestrator:
        def __enter__(self) -> "_RaisingOrchestrator":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def run_dispatch(self, *, dispatch_timeout: float | None = None) -> object:
            raise TimeoutError("dispatch exceeded 5.0s")

    monkeypatch.setattr(subprocess, "run", lambda cmd, timeout=None: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd: _FakePopen())
    monkeypatch.setattr(launcher, "NativeCanaryOrchestrator", lambda plan, **kw: _RaisingOrchestrator())

    rc = launcher.main([
        "--ssh-host", "hermes-ssh", "--relay-listen-port", "18080", "--local-proxy-port", "9443",
        "--host-socket-path", "/run/context-shunt/upstream.sock",
        "--repo-mount-src", "/opt/staged/repo", "--synthetic-source-host-path", "/opt/staged/artifact.txt",
        "--question", "what does the synthetic artifact say?",
        "--execute", "--confirm-token", CONFIRM_TOKEN, "--dispatch-timeout-seconds", "5.0",
    ])
    assert rc == 124
    err = capsys.readouterr().err
    assert "timeout" in err
