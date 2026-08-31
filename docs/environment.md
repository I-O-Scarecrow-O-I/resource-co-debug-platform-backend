# Local Dependency Environment

## Reusable Layout

- Dependency root: `O:\Code_dependency`
- Python runtime: `O:\Code_dependency\python_runtimes\python-3.11.9`
- Project virtual environment: `O:\Code_dependency\python_envs\resource-co-debug-py311`
- Pip cache: `O:\Code_dependency\pip_cache`

The runtime directory is reusable across projects. Other projects can create their own virtual
environments from:

```powershell
O:\Code_dependency\python_runtimes\python-3.11.9\python.exe -m venv O:\Code_dependency\python_envs\<env-name>
```

## Project Commands

Run all checks:

```powershell
O:\Code\resource-co-debug-platform-backend\scripts\check.ps1
```

Start the development server:

```powershell
O:\Code\resource-co-debug-platform-backend\scripts\run-dev.ps1
```

Direct commands:

```powershell
O:\Code_dependency\python_envs\resource-co-debug-py311\Scripts\python.exe -m pytest -q
O:\Code_dependency\python_envs\resource-co-debug-py311\Scripts\python.exe -m ruff check .
O:\Code_dependency\python_envs\resource-co-debug-py311\Scripts\python.exe -m uvicorn app.main:app --reload
```

