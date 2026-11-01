"""Run trusted check commands against PR files in disposable Docker containers."""
import logging
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid


def _command(argv, *, timeout, output_limit, input_file=None):
    """Drain output without letting an untrusted test exhaust reviewer memory."""
    started = time.monotonic()
    environment = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                   "HOME": str(Path.home()), "TMPDIR": "/tmp"}
    process = subprocess.Popen(argv, stdin=input_file if input_file is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True,
                               env=environment)
    output = bytearray()
    truncated = False
    timed_out = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > output_limit:
                        del output[:-output_limit]
                        truncated = True
        if timed_out:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
    except (subprocess.TimeoutExpired, ProcessLookupError):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        timed_out = True
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        process.stdout.close()
    return {"exit_code": process.returncode, "timed_out": timed_out,
            "output": output.decode(errors="replace"), "output_truncated": truncated,
            "duration_seconds": round(time.monotonic() - started, 2)}


def _directory(path):
    path = Path(path).resolve(strict=True)
    if not path.is_dir() or any(char in str(path) for char in (",", "\n", "\r")):
        raise ValueError("Invalid validation input directory")
    return path


def _workdir(value):
    if not isinstance(value, str):
        raise ValueError("Invalid validation working directory")
    path = PurePosixPath(value)
    if ".." in path.parts or "\x00" in value:
        raise ValueError("Invalid validation working directory")
    if path.is_absolute():
        if path != PurePosixPath("/workspace") and PurePosixPath("/workspace") not in path.parents:
            raise ValueError("Validation working directory must be in /workspace")
        return str(path)
    return str(PurePosixPath("/workspace") / path)


def _container_flags(name, volume, memory="2g"):
    return ["--rm", "--name", name, "--label", "codex-pr-reviewer.check=true",
            "--pull", "never", "--user", "1000:1000", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--cpus", "2", "--memory", memory,
            "--memory-swap", memory, "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--mount", f"type=volume,source={volume},target=/workspace",
            # Docker otherwise imports proxy credentials from its client config.
            "--env", "HTTP_PROXY=", "--env", "HTTPS_PROXY=",
            "--env", "FTP_PROXY=", "--env", "ALL_PROXY=", "--env", "NO_PROXY=",
            "--env", "http_proxy=", "--env", "https_proxy=",
            "--env", "ftp_proxy=", "--env", "all_proxy=", "--env", "no_proxy="]


def _copy_to_volume(source, docker, keeper, timeout, output_limit):
    # The controller's /data path is not a host-daemon bind path. Stream files over
    # docker exec instead, extracting as the runner UID into its disposable volume.
    with tempfile.TemporaryFile() as stream:
        def ownership(info):
            if not (info.isfile() or info.isdir()):
                raise ValueError("Validation archive contains a non-regular file")
            info.uid = info.gid = 1000
            info.uname = info.gname = ""
            return info
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.add(source, arcname=".", filter=ownership)
        stream.seek(0)
        return _command([*docker, "exec", "-i", keeper, "tar", "-xf", "-", "-C", "/workspace"],
                        timeout=timeout, output_limit=output_limit, input_file=stream)


def run_sandbox(source, dependency_input, stages, image, settings, log=None):
    """Run install stages with manifest-only input, then checks without networking.

    Input copies must allow UID1000 to read files and traverse directories.
    Source snapshots and manifests are read-only host mounts; all writes remain
    in a disposable 3 GB tmpfs-backed volume or bounded /tmp. A network-free
    keeper preserves the tmpfs mount between stages. Only trusted images and
    commands selected by the caller are accepted, never PR-provided commands.
    """
    log = log or logging.getLogger("pr-reviewer")
    if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image):
        raise ValueError("Invalid trusted validation image")
    source = _directory(source)
    dependency_input = _directory(dependency_input)
    if not stages or len(stages) > 12:
        raise ValueError("Validation requires between one and twelve stages")
    code_started = False
    for stage in stages:
        if (not isinstance(stage.get("name"), str) or not stage["name"]
                or not isinstance(stage.get("argv"), list) or not stage["argv"]
                or not all(isinstance(arg, str) and "\x00" not in arg for arg in stage["argv"])):
            raise ValueError("Invalid validation stage")
        if type(stage.get("network", False)) is not bool:
            raise ValueError("Validation stage network must be a boolean")
        _workdir(stage.get("cwd", "/workspace"))
        if stage.get("network", False):
            if code_started:
                raise ValueError("Network-enabled stages must precede source checks")
        else:
            code_started = True

    timeout = min(max(int(settings.get("timeout_seconds", 300)), 1), 900)
    job_timeout = min(max(int(settings.get("job_timeout_seconds", 1800)), 1), 1800)
    deadline = time.monotonic() + job_timeout
    output_limit = min(max(int(settings.get("output_limit_bytes", 1_000_000)), 1), 1_000_000)
    docker = [settings.get("docker", "/usr/local/bin/docker"),
              "--context", settings.get("docker_context", "orbstack")]
    volume = "codex-review-check-" + uuid.uuid4().hex
    report = {"status": "blocked", "stages": [], "reason": "", "cleanup_errors": []}
    containers = []
    copied_source = False
    transport = settings.get("input_transport", "bind")
    if transport not in ("bind", "archive"):
        raise ValueError("Unknown validation input transport")
    try:
        created = _command([*docker, "volume", "create", "--driver", "local",
                            "--opt", "type=tmpfs", "--opt", "device=tmpfs",
                            "--opt", "o=size=3g,uid=1000,gid=1000,mode=0755,nosuid,nodev",
                            "--label", "codex-pr-reviewer.check=true", volume],
                           timeout=30, output_limit=output_limit)
        if created["exit_code"] or created["timed_out"]:
            report["reason"] = "Could not create disposable validation volume: " + created["output"]
            return report
        keeper = volume + "-keeper"
        containers.append(keeper)
        held = _command([*docker, "run", "--detach", *_container_flags(keeper, volume, "64m"),
                         "--network", "none", "--entrypoint", "/bin/sh", image,
                         "-c", 'exec sleep "$1"', "codex-keeper", str(job_timeout + 120)],
                        timeout=30, output_limit=output_limit)
        if held["exit_code"] or held["timed_out"]:
            report["reason"] = "Could not start validation volume keeper: " + held["output"]
            return report
        for index, stage in enumerate(stages):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report["status"] = "timeout"
                report["reason"] = "Validation job exceeded its total time budget"
                return report
            network = bool(stage.get("network", False))
            name = f"{volume}-{index}"
            containers.append(name)
            input_path = dependency_input if network else source
            # A static wrapper copies files once; PR text is passed only as argv.
            copy_input = network or not copied_source
            if copy_input and transport == "archive":
                copied = _copy_to_volume(input_path, docker, keeper, min(60, remaining), output_limit)
                if copied["exit_code"] or copied["timed_out"]:
                    report["reason"] = "Could not stream validation input: " + copied["output"]
                    return report
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    report.update(status="timeout", reason="Validation input exhausted the job time budget")
                    return report
            wrapper = 'workdir=$1; shift; mkdir -p "$HOME" || exit 125; '
            if copy_input and transport == "bind":
                wrapper += 'cp -R /input/. /workspace/ || exit 125; '
            wrapper += 'cd "$workdir" || exit 125; exec "$@"'
            argv = [*docker, "run", *_container_flags(name, volume),
                    "--network", "bridge" if network else "none",
                    *(["--mount", f"type=bind,source={input_path},target=/input,readonly"] if transport == "bind" else []),
                    "--workdir", "/workspace", "--env", "HOME=/tmp/home",
                    "--env", "CI=true", "--env", "GIT_TERMINAL_PROMPT=0",
                    "--env", "UV_CACHE_DIR=/workspace/.validation-cache/uv",
                    "--env", "npm_config_cache=/workspace/.validation-cache/npm",
                    "--env", "PNPM_HOME=/workspace/.validation-pnpm",
                    "--entrypoint", "/bin/sh", image, "-c", wrapper,
                    "codex-check", _workdir(stage.get("cwd", "/workspace")), *stage["argv"]]
            log.info("Validation stage %s: network=%s, source=%s", stage["name"],
                     "dependencies only" if network else "none", input_path)
            result = _command(argv, timeout=min(timeout, remaining), output_limit=output_limit)
            status = ("blocked" if network and (result["timed_out"] or result["exit_code"]) else
                      "timeout" if result["timed_out"] else
                      "blocked" if result["exit_code"] in (125, 126, 127) else
                      "passed" if result["exit_code"] == 0 else "failed")
            report["stages"].append({"name": stage["name"], "argv": stage["argv"],
                                     "network": network, "status": status, **result})
            log.info("Validation stage %s: %s, exit=%s, duration=%ss", stage["name"],
                     status, result["exit_code"], result["duration_seconds"])
            copied_source = copied_source or not network
            if status != "passed":
                report["status"] = status
                report["reason"] = f"{stage['name']} {status}"
                return report
        report["status"] = "passed"
        return report
    except OSError as error:
        report["reason"] = f"Docker validation unavailable: {error}"
        return report
    finally:
        # docker CLI termination alone does not terminate a daemon-owned container.
        for name in reversed(containers):
            try:
                removed = _command([*docker, "rm", "--force", name], timeout=30, output_limit=4000)
                if removed["timed_out"] or (removed["exit_code"] and "No such container" not in removed["output"]):
                    report["cleanup_errors"].append(f"container {name}: {removed['output']}")
            except OSError as error:
                report["cleanup_errors"].append(str(error))
        try:
            removed = _command([*docker, "volume", "rm", "--force", volume], timeout=30, output_limit=4000)
            if removed["exit_code"] or removed["timed_out"]:
                report["cleanup_errors"].append(f"volume {volume}: {removed['output']}")
        except OSError as error:
            report["cleanup_errors"].append(str(error))
        if report["cleanup_errors"]:
            log.warning("Validation cleanup incomplete: %s", "; ".join(report["cleanup_errors"]))
