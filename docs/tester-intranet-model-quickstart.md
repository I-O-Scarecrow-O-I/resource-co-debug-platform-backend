# 内网模型与后端任务快速验收（Windows）

在测试员自己的 Windows 主机上，让平台后端和 NaturalCC Agent 调用既有实验室模型；不部署或下载模型权重。示例目录为 `C:\Code`，可换成自己有写权限的盘符。假设 Git、Python Launcher、Python 3.11/3.12 已安装，已有兼容虚拟环境优先复用。

依赖安装需要包源网络或团队提供的离线制品；任务运行仅需实验室内网。NaturalCC 官方同步会安装较大的研究库依赖，但不下载 70GB 模型权重。没有经验证的最小 Agent 依赖清单，也不能保证新机安装无锁文件或平台依赖冲突。

## 1. 后端（Python 3.11）

在 PowerShell 5.1+ 窗口克隆 `main` 并创建/复用环境。该分支会移动，本指南不代表远端当前状态已验证：

```powershell
$sharedRoot = 'C:\Code'; $backendRoot = Join-Path $sharedRoot 'resource-co-debug-platform-backend'
New-Item -ItemType Directory -Force $sharedRoot | Out-Null
git clone --branch main --single-branch 'https://github.com/I-O-Scarecrow-O-I/resource-co-debug-platform-backend.git' $backendRoot
if ($LASTEXITCODE -ne 0) { throw '后端 clone 失败' }
$backendVenv = Join-Path $sharedRoot 'backend-py311'; $backendPython = Join-Path $backendVenv 'Scripts\python.exe'
if (-not (Test-Path $backendPython)) { py -3.11 -m venv $backendVenv; if ($LASTEXITCODE -ne 0) { throw '创建后端 venv 失败' } }
Set-Location $backendRoot
& $backendPython -c 'import sys; assert sys.version_info[:2] == (3, 11)'
if ($LASTEXITCODE -ne 0) { throw '后端必须使用 Python 3.11' }
& $backendPython -c 'import app.main, uvicorn'
if ($LASTEXITCODE -ne 0) { & $backendPython -m pip install -e .; if ($LASTEXITCODE -ne 0) { throw '安装后端依赖失败' } }
```

在该窗口注入平台配置，创建独立可写 workspace；模型 URL 和密钥不要放进后端：

```powershell
$storageRoot = Join-Path $sharedRoot 'backend-workspaces'; New-Item -ItemType Directory -Force $storageRoot | Out-Null
$taskDatabase = Join-Path $sharedRoot 'backend-tasks.sqlite3'
@{ APP_ENV='local'; APP_HOST='127.0.0.1'; APP_PORT='8000'; STORAGE_ROOT=$storageRoot; TASK_DATABASE_PATH=$taskDatabase; NATURALCC_BASE_URL='http://127.0.0.1:7860'; NATURALCC_APPROVE_EXECUTE='false'; ALLOWED_CORS_ORIGINS='http://127.0.0.1:5173,http://localhost:5173' }.GetEnumerator() | ForEach-Object { Set-Item "Env:$($_.Key)" $_.Value }
& $backendPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

以上启动命令在前台运行，保持窗口打开；另开窗口检查。NaturalCC 和后端须同机，以访问同一 workspace：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/health'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/modules/code-generation/health'
```

`NATURALCC_BASE_URL` 指 NaturalCC 应用 API，不是模型 `/v1`。健康检查只表示 HTTP 可达。

## 2. NaturalCC Agent（Python 3.12）

在独立 PowerShell 窗口获取 `ncc3`。不要激活或复用后端 Python 3.11：

```powershell
$sharedRoot = 'C:\Code'; $deps = Join-Path $sharedRoot '_dependency'; $naturalccRoot = Join-Path $deps 'naturalcc-cs'
New-Item -ItemType Directory -Force $deps | Out-Null
git clone --branch ncc3 --single-branch 'https://github.com/CGCL-codes/naturalcc.git' $naturalccRoot
if ($LASTEXITCODE -ne 0) { throw 'NaturalCC clone 失败' }
Set-Location (Join-Path $naturalccRoot 'code_agent')
```

优先复用已有 Python `>=3.12,<3.13` 环境。没有时按 README.zh.md 用独立 uv 工具环境执行官方 `uv sync --no-dev`；`UV_PROJECT_ENVIRONMENT` 只在这个 NaturalCC 窗口设置：

```powershell
$naturalccEnv = Join-Path $deps 'naturalcc-py312'; $naturalccPython = Join-Path $naturalccEnv 'Scripts\python.exe'
$uvTools = Join-Path $deps 'uv-tools'; $uvPython = Join-Path $uvTools 'Scripts\python.exe'; $uvExe = Join-Path $uvTools 'Scripts\uv.exe'
if (-not (Test-Path $naturalccPython)) {
    if (-not (Test-Path $uvExe)) {
        if (-not (Test-Path $uvPython)) { py -3.12 -m venv $uvTools; if ($LASTEXITCODE -ne 0) { throw '创建 uv 环境失败' } }
        & $uvPython -m pip install uv; if ($LASTEXITCODE -ne 0) { throw '安装 uv 失败' }
    }
    $env:UV_PROJECT_ENVIRONMENT = $naturalccEnv
    & $uvExe sync --project (Join-Path $naturalccRoot 'code_agent') --python 3.12 --no-dev
    if ($LASTEXITCODE -ne 0) { throw 'uv sync 失败；检查包源、锁文件和平台依赖' }
}
& $naturalccPython -c 'import sys; assert (3, 12) <= sys.version_info[:2] < (3, 13)'
if ($LASTEXITCODE -ne 0) { throw 'NaturalCC 必须使用 Python 3.12' }
```

在 NaturalCC 窗口输入团队提供的模型 API base（含 `/v1`）和密钥。README 的 `.env` 不会自动加载；这些变量只注入当前窗口及其 Agent 子进程。`deepseek` 是兼容 provider 名称，和自定义 API base 配合使用，不会因此改指云端 DeepSeek：

```powershell
$modelApiBase = (Read-Host '输入实验室模型 API base URL（以 /v1 结尾）').TrimEnd('/')
if (-not $modelApiBase) { throw '模型 API base URL 不能为空' }
$secureKey = Read-Host '输入模型 API key' -AsSecureString
$env:DEEPSEEK_API_KEY = ([System.Net.NetworkCredential]::new('', $secureKey)).Password; $secureKey.Dispose()
@{ CODE_AGENT_PROVIDER='deepseek'; CODE_AGENT_MODEL='qwen3.6-35b-a3b'; CODE_AGENT_API_BASE=$modelApiBase; CODE_AGENT_CONTEXT_WINDOW_TOKENS='131072'; HF_HUB_OFFLINE='1'; TRANSFORMERS_OFFLINE='1'; HF_HUB_DISABLE_TELEMETRY='1' }.GetEnumerator() | ForEach-Object { Set-Item "Env:$($_.Key)" $_.Value }
```

可选 C/C++ 自检依赖：`libclang==18.1.1` 只提供 DLL，独立安装且不加入 `PYTHONPATH`，以免替换项目的 `clang==18.1.8` Python 绑定。此组合曾通过本机 CParser 样例，新机未验证：

```powershell
$libclangRoot = Join-Path $deps 'libclang-18.1.1'; $libclangDll = Join-Path $libclangRoot 'clang\native\libclang.dll'
if (-not (Test-Path $libclangDll)) { & $naturalccPython -m pip install --no-deps --target $libclangRoot libclang==18.1.1; if ($LASTEXITCODE -ne 0) { throw '安装 libclang DLL 失败' } }
$env:LIBCLANG_PATH = $libclangDll
$cppcheckCmd = Get-Command cppcheck.exe -ErrorAction SilentlyContinue
if ($null -eq $cppcheckCmd) { Write-Warning '无 cppcheck：扫描 coverage 标记未完成；不影响纯 C Agent 补全。' } else { $env:PATH = "$(Split-Path -Parent $cppcheckCmd.Source);$env:PATH" }
```

检查 NaturalCC 健康和模型目录，只显示模型 ID，不输出密钥。`/v1/models` 只验证鉴权目录可达，不证明推理或工具调用：

```powershell
Invoke-RestMethod 'http://127.0.0.1:7860/api/health'
$headers = @{ Authorization = "Bearer $env:DEEPSEEK_API_KEY" }
$modelIds = @((Invoke-RestMethod -Uri "$modelApiBase/models" -Headers $headers).data | ForEach-Object { [string]$_.id }); $modelIds
if ($modelIds -notcontains $env:CODE_AGENT_MODEL) { throw '模型目录未返回配置的模型 ID' }
Set-Location (Join-Path $naturalccRoot 'code_agent'); & $naturalccPython '.\agent_web_api.py'
```

Agent API 前台运行，保持窗口打开；停止按 `Ctrl+C`。HF 标志仅约束相关 Python 库，不是 OS 断网。若缺本地解析资源，记录缺项并准备资源，不要自动联网下载或解除标志。本指南不配置 Pipeline/Aider，也不声称 Agent 环境贯通它们。

## 3. 真实后端任务：补全、产物与自检

在第三个 PowerShell 窗口执行。将只含 `add.c` 的小 ZIP 上传到真实后端任务接口；不要仅用 chat 请求代替：

```powershell
$smokeRoot = Join-Path $env:TEMP ('model-smoke-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory $smokeRoot | Out-Null
$sourcePath = Join-Path $smokeRoot 'add.c'; $zipPath = Join-Path $smokeRoot 'add-project.zip'
$source = @'
int add(int a, int b) { return 0; }
int main(void) {
    if (add(2, 3) != 5) return 1;
    if (add(-2, 1) != -1) return 2;
    return 0;
}
'@
Set-Content -LiteralPath $sourcePath -Value $source -Encoding Ascii
Compress-Archive -LiteralPath $sourcePath -DestinationPath $zipPath
$upload = & curl.exe --fail --silent --show-error --noproxy '*' -F "archive=@$zipPath" -F 'name=local-model-smoke' 'http://127.0.0.1:8000/api/v1/projects' | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $upload.success -or -not $upload.data.id) { throw '上传项目失败' }
$request = @{ project_id=[string]$upload.data.id; operation='completion'; target_files=@('add.c'); instruction='只将 add 函数补全为返回 a+b，保留 main。'; budget=@{ max_llm_calls=8; max_tool_calls=8; max_output_tokens=2048; max_seconds=180 }; timeout_seconds=240 }
$body = [System.Text.Encoding]::UTF8.GetBytes(($request | ConvertTo-Json -Depth 8))
$created = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/modules/code-generation/tasks' -ContentType 'application/json; charset=utf-8' -Body $body
if (-not $created.success -or -not $created.data.id) { throw '创建任务失败' }; $taskId = [string]$created.data.id
$deadline = [DateTime]::UtcNow.AddSeconds(240); $task = $null
do { $task = (Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/$taskId").data; if ($task.status -in @('SUCCEEDED','FAILED','CANCELLED')) { break }; Start-Sleep 2 } while ([DateTime]::UtcNow -lt $deadline)
if ($task.status -notin @('SUCCEEDED','FAILED','CANCELLED')) { Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/v1/tasks/$taskId/cancel" | Out-Null; throw "超时已请求取消；task_id=$taskId" }
if ($task.status -ne 'SUCCEEDED') { throw "任务状态 $($task.status)；task_id=$taskId" }
$task.result | ConvertTo-Json -Depth 12
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/$taskId/logs" | ConvertTo-Json -Depth 8
```

检查 `result.final_answer`、`changed_files`、`self_scan` 和 coverage，查看日志中的模型/工具调用。再列出产物并输入列表中真实的 `path` 下载：

```powershell
$artifacts = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/$taskId/artifacts"; $artifacts.data | Format-Table path,size_bytes
$artifactPath = Read-Host '输入列表中 add.c 的实际 path'
$escapedPath = (($artifactPath -split '/') | ForEach-Object { [uri]::EscapeDataString($_) }) -join '/'
$downloadPath = Join-Path $smokeRoot 'generated-add.c'
Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/v1/tasks/$taskId/artifacts/$escapedPath" -OutFile $downloadPath
$gcc = Get-Command gcc -ErrorAction SilentlyContinue
if ($null -eq $gcc) { Write-Warning '无 GCC：产物已下载，编译运行未验证。' } else { $exePath = Join-Path $smokeRoot 'generated-add.exe'; & $gcc.Source $downloadPath '-o' $exePath; if ($LASTEXITCODE -ne 0) { throw 'GCC 编译失败' }; & $exePath; if ($LASTEXITCODE -ne 0) { throw "断言失败，退出码 $LASTEXITCODE" } }
```

任务 `SUCCEEDED` 只表示任务流程完成，不证明代码质量或扫描 coverage 足够；无 ground truth 时不能声称准确率/召回率。模型目录检查不等于推理或工具链成功；以真实后端任务观察结果。本指南不要求做单独 chat 或额外模型测试。

## 4. 结果记录与范围

记录两个仓库的实际 commit、模型名、脱敏失败日志、task ID、产物和独立测试结果，不记录密钥。若再做效果抽查，可各取一个真实小案例做补全、修复、只读扫描并重复 3 次；用预先答案/单测核对，扫描前后比较源码哈希。此为小规模功能检查，不是性能测试；缺 `cppcheck` 时扫描 coverage 未完成，但不妨碍 Agent 补全判断。

本流程只检查模型 Agent 生成及后端自检链路，不是合同指标 1–5 验收。指标 2 是构建/依赖调试，指标 4 是调度比较，不能由模型调用替代；不声称性能或国产卡本机验证通过。本文未执行模型可用性检查、服务启动、安装或真实联调。
