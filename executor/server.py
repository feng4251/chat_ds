"""Network-isolated Python execution daemon.

The container is launched with ``network_mode: none`` and communicates with
the harness only through a shared Unix-domain socket.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import socketserver
import subprocess
import tempfile
import time
from pathlib import Path


SOCKET_PATH = Path(os.environ.get(
    "EXECUTOR_SOCKET", "/run/chat-ds-executor/executor.sock"
))
MAX_REQUEST_BYTES = 1_000_000
MAX_STDOUT_BYTES = 50_000
MAX_STDERR_BYTES = 10_000
MAX_TIMEOUT = 120
MAX_ADDRESS_SPACE_BYTES = int(os.environ.get(
    "EXECUTOR_MAX_ADDRESS_SPACE_BYTES", str(2 * 1024 * 1024 * 1024)
))

BLAS_THREAD_ENV = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "BLIS_NUM_THREADS": "1",
}


def _child_limits() -> None:
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (125, 125))
    resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE_BYTES,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{label} truncated]"


def _run(payload: dict) -> dict:
    code = str(payload.get("code", ""))
    timeout = max(1, min(int(payload.get("timeout", 30)), MAX_TIMEOUT))
    if not code.strip():
        return {"status": "error", "error": "No code provided."}

    started = time.monotonic()
    temp_dir = tempfile.mkdtemp(prefix="exec_", dir="/tmp")
    os.chmod(temp_dir, 0o777)
    script = Path(temp_dir) / "script.py"
    script.write_text(code, encoding="utf-8")
    os.chmod(script, 0o444)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": temp_dir,
        "TMPDIR": temp_dir,
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        **BLAS_THREAD_ENV,
    }

    try:
        proc = subprocess.Popen(
            ["python", "-I", "-B", str(script)],
            cwd=temp_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_child_limits,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            timed_out = True

        stdout_text = _truncate(
            stdout.decode("utf-8", errors="replace"), MAX_STDOUT_BYTES, "stdout"
        )
        stderr_text = _truncate(
            stderr.decode("utf-8", errors="replace"), MAX_STDERR_BYTES, "stderr"
        )
        result = {
            "status": "success",
            "output": stdout_text,
            "exit_code": proc.returncode,
            "duration_seconds": round(time.monotonic() - started, 2),
            "network": "disabled",
        }
        if timed_out:
            result.update(
                status="timeout",
                error=f"Script timed out after {timeout}s",
            )
        elif proc.returncode != 0:
            result.update(
                status="error",
                error=stderr_text or f"Script exited with code {proc.returncode}",
            )
            if stderr_text:
                result["output"] = stdout_text + "\n--- stderr ---\n" + stderr_text
        return result
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            response = {"status": "error", "error": "Request too large."}
        else:
            try:
                response = _run(json.loads(raw.decode("utf-8")))
            except Exception as exc:
                response = {
                    "status": "error",
                    "error": f"Invalid request: {type(exc).__name__}: {exc}",
                }
        self.wfile.write(
            json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        )


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    with Server(str(SOCKET_PATH), Handler) as server:
        os.chmod(SOCKET_PATH, 0o666)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
