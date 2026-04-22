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
    logger.setLevel(level)
    
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    
    return logger
