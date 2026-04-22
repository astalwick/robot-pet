#!/usr/bin/env python3
"""
Robot Brain - main orchestrator service.

This is the parent service that starts on boot. Future services
(motor controller, sensors, etc.) will depend on this via:
    After=robot-brain.service
"""

import sys
import time

from lib.log import setup_logging

log = setup_logging("robot-brain")


def main():
    log.info("starting")
    
    while True:
        log.info("alive")
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)
    except Exception:
        log.exception("crashed")
        sys.exit(1)
