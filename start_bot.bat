@echo off
setlocal
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe run.py
) else (
  python run.py
)
endlocal
