# Hello Web

A tiny CPU-only Python FastAPI demo. No API key or external database required.

Python 3.11. Install dependencies with pip install -r requirements.txt.
Start with python -m uvicorn app:app --host 0.0.0.0 --port 8000.
Health endpoint: /health returns {"status":"ok"}.

RepoLaunch built-in demo reads these real fixture files but simulates Docker execution.
