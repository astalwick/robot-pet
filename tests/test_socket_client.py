import os
import socket
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from telemetry.socket_client import subscribe


class SubscribeTest(unittest.TestCase):
    def test_subscribe_reconnects_after_malformed_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, "sub.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.listen(2)

            def serve():
                # First connection: a truncated JSON line (hub died mid-write).
                conn, _ = server.accept()
                conn.sendall(b'{"type": "snapsh\n')
                conn.close()
                # Second connection: a good snapshot after reconnect.
                conn, _ = server.accept()
                conn.sendall(b'{"type": "snapshot"}\n')
                conn.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                snapshot = next(subscribe(socket_path, reconnect_interval=0.01))
            finally:
                thread.join(timeout=2.0)
                server.close()

        self.assertEqual(snapshot, {"type": "snapshot"})


if __name__ == "__main__":
    unittest.main()
