"""Sensor message transports.

All sources push ``(raw_message, receive_epoch)`` tuples into one queue, so
the controller loop does not care where readings come from:

  * UdpSource          - Python simulator (JSON datagrams, default port 5005)
  * SerialSocketSource - Cooja "Serial Socket (SERVER)" plugin: TCP stream of
                         the mote's printf() lines (default port 60001)
  * StdinSource        - lines piped in, e.g. a Contiki-NG `native` build:
                         ./sensor_node.native | python -m controller.main --source stdin
"""

from __future__ import annotations

import queue
import socket
import sys
import threading
import time

from .logging_setup import log_event

Message = tuple[str, float]


class _Source(threading.Thread):
    def __init__(self, out: "queue.Queue[Message]", name: str):
        super().__init__(name=name, daemon=True)
        self.out = out
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()


class UdpSource(_Source):
    def __init__(self, out, host: str = "127.0.0.1", port: int = 5005):
        super().__init__(out, f"udp:{host}:{port}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.5)
        self.address = self.sock.getsockname()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, _addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self.out.put((data.decode("utf-8", errors="replace"), time.time()))
        self.sock.close()


class SerialSocketSource(_Source):
    """TCP client for Cooja's Serial Socket (SERVER) plugin. Reconnects.

    Two-way: :meth:`send` writes a line to the mote's serial input (used to push
    the configuration to the controller mote), ``greeting()`` is sent on every
    (re)connect, and every received line is mirrored to ``console_log`` so the
    dashboard can show the mote's serial console.
    """

    def __init__(self, out, host: str = "127.0.0.1", port: int = 60001,
                 console_log=None, greeting=None):
        super().__init__(out, f"serial-socket:{host}:{port}")
        self.host, self.port = host, port
        self.console_log = console_log
        self.greeting = greeting
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()

    def send(self, line: str) -> bool:
        with self._send_lock:
            if self._sock is None:
                return False
            try:
                self._sock.sendall((line.rstrip("\n") + "\n").encode())
                return True
            except OSError:
                return False

    def mirror(self, text: str) -> None:
        if self.console_log is None:
            return
        try:
            with open(self.console_log, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')}  {text}\n")
        except OSError:
            pass

    def run(self) -> None:
        connected_logged = False
        while not self._stop_event.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=2) as sock:
                    sock.settimeout(0.5)
                    log_event("SYSTEM", f"connected to Cooja serial socket {self.host}:{self.port}")
                    connected_logged = True
                    self._sock = sock
                    self.mirror(f"# connected to Cooja serial socket {self.host}:{self.port}")
                    if self.greeting is not None:
                        line = self.greeting()
                        if line and self.send(line):
                            self.mirror(f"<< {line}")
                    buf = b""
                    while not self._stop_event.is_set():
                        try:
                            chunk = sock.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            raise ConnectionError("serial socket closed")
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = line.decode("utf-8", errors="replace").strip()
                            if text:
                                self.mirror(f">> {text}")
                                self.out.put((text, time.time()))
            except OSError as exc:
                self._sock = None
                if connected_logged:
                    log_event("SYSTEM", f"serial socket disconnected ({exc}); retrying")
                    connected_logged = False
                self._stop_event.wait(2.0)


class StdinSource(_Source):
    def __init__(self, out):
        super().__init__(out, "stdin")

    def run(self) -> None:
        for line in sys.stdin:
            if self._stop_event.is_set():
                break
            line = line.strip()
            if line:
                self.out.put((line, time.time()))


def is_sensor_line(text: str) -> bool:
    """Contiki prints boot/log lines too; only forward sensor messages.

    Anything that *looks* like a sensor report (JSON or starts with MOISTURE)
    is forwarded, including corrupted ones, so they are counted as malformed.
    """
    t = text.lstrip()
    return t.startswith("{") or t.upper().startswith("MOISTURE")


class SensorManager:
    def __init__(self) -> None:
        self.queue: "queue.Queue[Message]" = queue.Queue()
        self.sources: list[_Source] = []

    def add_udp(self, host: str = "127.0.0.1", port: int = 5005) -> UdpSource:
        src = UdpSource(self.queue, host, port)
        self.sources.append(src)
        return src

    def add_serial_socket(self, host: str = "127.0.0.1", port: int = 60001,
                          console_log=None, greeting=None) -> SerialSocketSource:
        src = SerialSocketSource(self.queue, host, port, console_log=console_log, greeting=greeting)
        self.sources.append(src)
        return src

    def add_stdin(self) -> StdinSource:
        src = StdinSource(self.queue)
        self.sources.append(src)
        return src

    def start(self) -> None:
        for src in self.sources:
            src.start()

    def stop(self) -> None:
        for src in self.sources:
            src.stop()

    def poll(self, timeout: float = 0.5) -> list[Message]:
        """Block up to ``timeout`` for the first message, then drain."""
        out: list[Message] = []
        try:
            out.append(self.queue.get(timeout=timeout))
        except queue.Empty:
            return out
        while True:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                return out
