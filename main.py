#!/usr/bin/env python3
"""
Robot Brain - main orchestrator service.

This is the parent service that starts on boot. Future services
(motor controller, sensors, etc.) will depend on this via:
    After=robot-brain.service
"""

import time
import sys

def main():
    print("robot-brain starting", flush=True)
    
    while True:
        print("robot-brain alive", flush=True)
        time.sleep(5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("robot-brain shutting down", flush=True)
        sys.exit(0)
