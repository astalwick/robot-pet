"""
Logging setup for robot services.

All services log to stdout/stderr, systemd captures to journald.
Use: journalctl -u robot-brain -f
"""

import logging
import sys


def setup_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Configure logging for a service.
    
    Args:
        name: Service name (e.g. "robot-brain")
        level: Log level (default INFO)
    
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Driver modules log under logging.getLogger(__name__) ("drivers.*"), which
    # otherwise has no handler -- their INFO lines would never reach journald.
    for configured in (logger, logging.getLogger("drivers")):
        configured.setLevel(level)
        if not configured.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(level)
            handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            configured.addHandler(handler)

    return logger
