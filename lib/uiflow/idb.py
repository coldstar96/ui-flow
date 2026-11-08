import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from uiflow import paths

SIDECAR_READY_TIMEOUT = 90.0
DEFAULT_TIMEOUT = 120.0
CONNECT_POLL_SECONDS = 0.05


class IdbError(Exception):
    pass


class IdbUnavailable(IdbError):
    pass


def companion_binary():
    found = os.environ.get("UIFLOW_IDB_COMPANION") or shutil.which("idb_companion")
    if not found:
        raise IdbUnavailable(
            "idb_companion not found — install with "
            "`brew trust facebook/fb && brew install facebook/fb/idb-companion`"
        )
    return found


def venv_dir():
    return os.environ.get("UIFLOW_IDB_VENV") or os.path.join(paths.state_dir(), "idb-venv")


def venv_python():
    return os.path.join(venv_dir(), "bin", "python3")


def venv_ready():
    python = venv_python()
    if not os.path.exists(python):
        return False
    probe = subprocess.run([python, "-c", "import idb"], capture_output=True, check=False)
    return probe.returncode == 0


def ensure_venv(stream=None):
    """fb-idb is a gRPC client that cannot live in the skill's own interpreter: Homebrew python is
    externally managed, and importing it per call would cost ~2s. It gets its own venv, and only the
    sidecar ever imports it."""
    if venv_ready():
        return venv_python()
    with _lock("venv"):
        if venv_ready():
            return venv_python()
        stream = stream or sys.stderr
        directory = venv_dir()
        stream.write("installing fb-idb into %s (first run only)\n" % directory)
        staging = directory + ".building"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(directory, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "venv", staging], check=True, capture_output=True)
        install = subprocess.run(
            [os.path.join(staging, "bin", "pip"), "install", "--quiet", "fb-idb"],
            capture_output=True,
            text=True,
            check=False,
        )
        probe = subprocess.run(
            [os.path.join(staging, "bin", "python3"), "-c", "import idb"], capture_output=True, check=False
        )
        if install.returncode != 0 or probe.returncode != 0:
            shutil.rmtree(staging, ignore_errors=True)
            raise IdbUnavailable("could not install fb-idb: %s" % (install.stderr or "").strip()[-400:])
        # A venv only becomes visible once it is complete, so a crashed install cannot be reused.
        os.rename(staging, directory)
    return venv_python()


SOCKET_PATH_LIMIT = 100


@contextlib.contextmanager
def _lock(name):
    handle = os.open(os.path.join(paths.state_dir(), "idb-%s.lock" % name), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def socket_path(udid, state=None):
    """AF_UNIX paths are capped near 104 bytes, and a worktree checkout already spends most of that,
    so the name carries a udid prefix rather than the whole udid and falls back out of the tree
    entirely when even that will not fit."""
    directory = state or paths.state_dir()
    short = os.path.join(directory, "idb-%s.sock" % udid[:8])
    if len(short) <= SOCKET_PATH_LIMIT:
        return short
    digest = hashlib.sha1(("%s|%s" % (directory, udid)).encode()).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), "uiflow-idb-%s.sock" % digest)


def state_path(udid):
    return os.path.join(paths.state_dir(), "idb-%s.json" % udid)


def read_state(udid):
    try:
        with open(state_path(udid)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_state(udid, payload):
    tmp = "%s.%s.tmp" % (state_path(udid), os.getpid())
    with open(tmp, "w") as handle:
        json.dump(payload, handle)
    os.replace(tmp, state_path(udid))


def clear_state(udid):
    for path in (state_path(udid), socket_path(udid)):
        try:
            os.remove(path)
        except OSError:
            pass


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def _connect(udid, timeout=1.0):
    path = socket_path(udid)
    if not os.path.exists(path):
        return None
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
    except OSError:
        client.close()
        return None
    return client


def send(ops, udid, timeout=DEFAULT_TIMEOUT):
    """One request carries the whole batch, exactly like the driver channel: the sidecar's value is
    that a burst of taps costs one round trip and one live gRPC connection."""
    client = _connect(udid, timeout=timeout)
    if client is None:
        raise IdbError("no idb sidecar for %s — run `uiflow idb up`" % udid)
    try:
        client.settimeout(timeout)
        client.sendall((json.dumps({"ops": list(ops)}, ensure_ascii=False) + "\n").encode())
        chunks = []
        while True:
            chunk = client.recv(1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
            if chunks[-1].endswith(b"\n"):
                break
        raw = b"".join(chunks).decode("utf-8", "replace").strip()
    finally:
        client.close()
    if not raw:
        raise IdbError("idb sidecar closed the connection without replying")
    try:
        return json.loads(raw)
    except ValueError:
        raise IdbError("idb sidecar sent malformed JSON: %s" % raw[:200])


def alive(udid):
    client = _connect(udid, timeout=0.5)
    if client is None:
        return False
    client.close()
    return True


def _start_companion(udid, port, log_path):
    with open(log_path, "a") as log:
        process = subprocess.Popen(
            [companion_binary(), "--udid", udid, "--grpc-port", str(port), "--log-level", "info"],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return process.pid


def _companion_listening(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(CONNECT_POLL_SECONDS)
    return False


def _start_sidecar(udid, port, log_path):
    # Deliberately OUTSIDE the uiflow package: a script's own directory leads sys.path, so a
    # sidecar living next to this file would import THIS module as `idb` instead of fb-idb.
    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uiflow_idb_sidecar.py"
    )
    with open(log_path, "a") as log:
        process = subprocess.Popen(
            [venv_python(), script, "--udid", udid, "--port", str(port), "--socket", socket_path(udid)],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return process.pid


def up(udid, timeout=SIDECAR_READY_TIMEOUT):
    """The lock makes two sessions in one checkout queue instead of racing: the loser would
    otherwise unlink the winner's socket and orphan a sidecar nothing can reach or kill."""
    with _lock(udid):
        if alive(udid):
            return read_state(udid)
        ensure_venv()
        state = read_state(udid)
        log_path = os.path.join(paths.state_dir(), "idb-%s.log" % udid)
        port = state.get("port")
        companion_pid = state.get("companionPid")
        if not port or not _pid_alive(companion_pid) or not _companion_listening(port, timeout=0.5):
            port = free_port()
            companion_pid = _start_companion(udid, port, log_path)
            # Recorded before it is known to work: an unrecorded pid is an orphan `down` cannot reap.
            write_state(
                udid,
                {
                    "udid": udid,
                    "port": port,
                    "companionPid": companion_pid,
                    "socket": socket_path(udid),
                },
            )
            if not _companion_listening(port, timeout=timeout):
                raise IdbUnavailable("idb_companion did not start listening on port %s" % port)
        sidecar_pid = _start_sidecar(udid, port, log_path)
        payload = {
            "udid": udid,
            "port": port,
            "companionPid": companion_pid,
            "sidecarPid": sidecar_pid,
            "socket": socket_path(udid),
        }
        write_state(udid, payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if alive(udid):
                return payload
            if not _pid_alive(sidecar_pid):
                raise IdbUnavailable("idb sidecar exited; see %s" % log_path)
            time.sleep(CONNECT_POLL_SECONDS)
        raise IdbUnavailable("idb sidecar did not come up within %ss; see %s" % (timeout, log_path))


def down(udid):
    if alive(udid):
        try:
            send(["quit"], udid, timeout=5.0)
        except IdbError:
            pass
    state = read_state(udid)
    for key in ("sidecarPid", "companionPid"):
        pid = state.get(key)
        if _pid_alive(pid):
            try:
                os.kill(int(pid), 15)
            except OSError:
                pass
    clear_state(udid)
    return state


def status(udid):
    state = dict(read_state(udid))
    state["udid"] = udid
    state["live"] = alive(udid)
    return state
