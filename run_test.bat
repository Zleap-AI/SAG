@echo off
REM 使用虚拟环境运行测试脚本
cd /d D:\zleap-platform\data_flow
.venv\Scripts\python.exe tests\ai\test_sumy_summary.py %*
