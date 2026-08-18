"""A stand-in for a docker-py Container.

Everything the server touches — exec_run, put_archive, logs, attrs — is backed
by an in-memory filesystem, so the write transaction (validate, back up, stage,
rename, roll back) can be exercised with no Docker daemon anywhere in sight.
"""

import io
import tarfile


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
        self.exec_calls.append(list(cmd))
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

        if program == "test":
            return (0, "") if cmd[-1] in self.files else (1, "")

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
            return 0, ""

        return 127, f"{program}: not found"
