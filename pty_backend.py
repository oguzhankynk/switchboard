"""Cross-platform PTY sessions: real pty.fork() on POSIX, ConPTY via
pywinpty on Windows. Both expose the same small interface so app.py never
has to branch on platform.
"""
import os

IS_WINDOWS = os.name == "nt"


def default_shell() -> str:
    if IS_WINDOWS:
        return os.environ.get("COMSPEC", "powershell.exe")
    return os.environ.get("SHELL", "/bin/bash")


if not IS_WINDOWS:
    import fcntl
    import pty
    import signal
    import struct
    import termios

    class PtySession:
        def __init__(self, argv: list[str], cwd: str, env: dict[str, str]):
            self.pid, self.fd = pty.fork()
            if self.pid == 0:
                try:
                    os.chdir(cwd)
                    os.environ.update(env)
                    os.execvp(argv[0], argv)
                finally:
                    os._exit(1)

        def read(self, n: int = 65536) -> bytes:
            try:
                return os.read(self.fd, n)
            except OSError:
                return b""

        def write(self, data: bytes) -> None:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

        def resize(self, rows: int, cols: int) -> None:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

        def terminate(self) -> None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass

else:
    import subprocess

    from winpty import PtyProcess  # pywinpty

    class PtySession:
        def __init__(self, argv: list[str], cwd: str, env: dict[str, str]):
            full_env = dict(os.environ)
            full_env.update(env)
            cmdline = subprocess.list2cmdline(argv)
            self.proc = PtyProcess.spawn(cmdline, cwd=cwd, env=full_env)

        def read(self, n: int = 65536) -> bytes:
            try:
                data = self.proc.read(n)
            except EOFError:
                return b""
            if isinstance(data, str):
                data = data.encode("utf-8", errors="replace")
            return data

        def write(self, data: bytes) -> None:
            text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
            try:
                self.proc.write(text)
            except Exception:
                pass

        def resize(self, rows: int, cols: int) -> None:
            try:
                self.proc.setwinsize(rows, cols)
            except Exception:
                pass

        def terminate(self) -> None:
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass
