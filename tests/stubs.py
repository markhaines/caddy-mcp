"""A stand-in for a docker-py Container.

Everything the server touches — exec_run, put_archive, logs, attrs — is backed
by an in-memory filesystem, so the write transaction (validate, back up, stage,
rename, roll back) can be exercised with no Docker daemon anywhere in sight.
"""

import io
import re
import shlex
import tarfile
import threading


class StubExecResult:
    def __init__(self, exit_code, output):
        self.exit_code = exit_code
        self.output = output


class StubContainer:
    """Fake Caddy container with a tiny in-memory filesystem.

    files          : {path: bytes} the container starts with
    validate_ok    : whether `caddy validate` succeeds
    validate_output: what `caddy validate` prints
    put_archive_ok : what put_archive returns
    put_archive_fail_paths : destination directories put_archive rejects
    fail           : {command_name: (exit_code, output)} to force a failure,
                     e.g. {"mv": (1, "mv: cross-device link")}
    probe_status   : force the config-type probe's exit status (see PROBE_* in
                     server.py); by default it is derived from `files`
    block_on       : command name whose first call blocks until `release` is
                     set, for exercising concurrency
    """

    name = "caddy"
    status = "running"
    short_id = "deadbeef1234"

    def __init__(
        self,
        files=None,
        validate_ok=True,
        validate_output="",
        put_archive_ok=True,
        put_archive_fail_paths=None,
        fail=None,
        logs_data=b"log line\n",
        attrs=None,
        probe_status=None,
        block_on=None,
    ):
        self.files = dict(files or {})
        self.validate_ok = validate_ok
        self.validate_output = validate_output
        self.put_archive_ok = put_archive_ok
        self.put_archive_fail_paths = set(put_archive_fail_paths or ())
        self.fail = dict(fail or {})
        self.logs_data = logs_data
        self.attrs = attrs if attrs is not None else {
            "State": {"Running": True, "StartedAt": "2026-08-18T10:00:00Z"},
            "Config": {"Image": "caddy:2-alpine"},
            "RestartCount": 0,
        }
        self.probe_status = probe_status
        self.block_on = block_on
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_timeout = 2.0
        # Ordered log of every container interaction, with the calling thread,
        # so tests can prove two transactions did not interleave.
        self.call_log = []
        self.exec_calls = []
        self.put_archive_calls = []
        self.logs_calls = []
        self.reload_calls = 0

    # -- docker-py surface ------------------------------------------------

    @property
    def image(self):
        # docker-py hands back None when the image ID is missing from attrs;
        # anything reaching for `.tags` on this blows up, which is the point.
        return None

    def reload(self):
        self.reload_calls += 1

    def logs(self, tail=None, timestamps=False):
        self.logs_calls.append({"tail": tail, "timestamps": timestamps})
        return self.logs_data

    def put_archive(self, path, data):
        raw = data.read() if hasattr(data, "read") else data
        self.call_log.append((threading.get_ident(), f"put_archive {path}"))
        self.put_archive_calls.append({"path": path, "data": raw})
        if not self.put_archive_ok or path.rstrip("/") in self.put_archive_fail_paths:
            return False
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                content = extracted.read() if extracted else b""
                self.files[f"{path.rstrip('/')}/{member.name}"] = content
        return self.put_archive_ok

    def exec_run(self, cmd, demux=False):
        self.call_log.append((threading.get_ident(), " ".join(cmd)))
        self.exec_calls.append(list(cmd))
        if self.block_on and cmd[0] == self.block_on:
            self.started.set()
            self.release.wait(timeout=self.block_timeout)
        exit_code, output = self._run(list(cmd))
        return StubExecResult(exit_code, output.encode("utf-8") if isinstance(output, str) else output)

    # -- fake shell -------------------------------------------------------

    def _run(self, cmd):
        program = cmd[0]
        if program in self.fail:
            return self.fail[program]

        if program == "cat":
            path = cmd[1]
            if path not in self.files:
                return 1, f"cat: {path}: No such file or directory"
            return 0, self.files[path].decode("utf-8")

        if program in ("cp", "mv"):
            src, dst = [a for a in cmd[1:] if not a.startswith("-")]
            if src not in self.files:
                return 1, f"{program}: can't stat '{src}': No such file or directory"
            self.files[dst] = self.files[src]
            if program == "mv":
                del self.files[src]
            return 0, ""

        if program == "rm":
            for path in [a for a in cmd[1:] if not a.startswith("-")]:
                self.files.pop(path, None)
            return 0, ""

        if program == "caddy":
            return (0 if self.validate_ok else 1), self.validate_output

        if program == "sh":
            script = cmd[-1]
            if "[ -f " in script:              # the config-type probe
                return self._probe(script), ""
            return 0, ""                       # the prune script

        return 127, f"{program}: not found"

    def _probe(self, script):
        """Mimic the server's -f / -L / -e classification of the config path."""
        if self.probe_status is not None:
            return self.probe_status
        path = shlex.split(re.search(r"\[ -f (.+?) \]", script).group(1))[0]
        return 0 if path in self.files else 1


class StubDockerClient:
    """Enough of a docker client for the /health endpoint."""

    def __init__(self, container=None, ping_ok=True):
        self.container = container
        self.ping_ok = ping_ok
        self.pings = 0

    def ping(self):
        self.pings += 1
        if not self.ping_ok:
            raise RuntimeError("docker daemon unreachable")
        return True


class FakeDockerModule:
    """Stands in for the `docker` module so client construction can be observed
    without a daemon. `errors` is the real thing, so exception handling in
    server.py keeps working."""

    def __init__(self, factory):
        self._factory = factory
        import docker as real_docker
        self.errors = real_docker.errors

    def DockerClient(self, **kwargs):
        return self._factory(**kwargs)
