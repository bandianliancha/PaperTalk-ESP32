#!/bin/bash
cd "$(dirname "$0")"
export PYTHONPATH="/Users/meng/Desktop/esp32_feishu_voice/server"
/Users/meng/.espressif/python_env/idf5.5_py3.11_env/bin/python3 server.py
