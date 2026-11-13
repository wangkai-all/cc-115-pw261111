#!/usr/bin/env python3
"""JoinLab 开发启动脚本。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.server import main

if __name__ == "__main__":
    main()
