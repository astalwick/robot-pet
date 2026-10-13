"""Run a node's service loop in a thread, and take the node down with it."""

import threading
from typing import Any, Callable

import rclpy


def start_service_thread(target: Callable[..., Any], *args: Any) -> threading.Thread:
    """Start the service loop in a daemon thread.

    When the loop returns or crashes, shut rclpy down so spin() in the main
    thread exits and systemd restarts the unit. Otherwise a dead loop leaves
    a live process publishing frozen topics that nothing ever restarts. The
    crash itself still prints through the thread excepthook, so it lands in
    the journal.
    """

    def run() -> None:
        try:
            target(*args)
        finally:
            rclpy.try_shutdown()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
