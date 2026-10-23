"""Check that a GitHub macOS runner can serve a Python child over loopback."""

from __future__ import annotations

import http.client
import select
import subprocess
import sys
import time

CHILD = """
import socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
s.listen(1)
print(s.getsockname()[1], flush=True)
c, _ = s.accept()
c.recv(4096)
c.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok')
c.close()
s.close()
"""


def main() -> int:
    for attempt in range(3):
        started = time.monotonic()
        child = subprocess.Popen([sys.executable, "-u", "-c", CHILD],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert child.stdout is not None
            ready, _, _ = select.select([child.stdout], [], [], 10)
            if not ready:
                raise RuntimeError("Python child did not bind loopback within 10 seconds")
            port = int(child.stdout.readline())
            bound = time.monotonic() - started
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/health")
                status = connection.getresponse().status
            finally:
                connection.close()
            elapsed = time.monotonic() - started
            print(f"loopback probe {attempt + 1}: bind={bound:.3f}s total={elapsed:.3f}s HTTP={status}", flush=True)
            if status != 200:
                raise RuntimeError("Python child loopback health failed")
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
