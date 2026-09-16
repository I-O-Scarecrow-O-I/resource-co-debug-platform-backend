# openEuler 部署基线（第一阶段）

本文提供 `resource-co-debug-platform-backend` 的 systemd 部署基线。它不是 openEuler
实机验收记录；具体发行版版本、软件包名称、仓库可用性、GDB/编译工具可用性和安全策略必须在目标机
确认后才可标记为已验收。

## 服务边界

- 后端使用 Python 3.11，运行在独立虚拟环境：
  `/opt/resource-co-debug-platform-backend/.venv`。
- NaturalCC 使用独立的 Python 3.12 服务和独立虚拟环境；后端仅通过
  `NATURALCC_BASE_URL` 访问它。部署版本必须不早于 `ncc3@c619262`；health 只验证存活，实际
  deferred approval 协议会在任务运行时 fail-closed，不存在 legacy fallback。
- NaturalCC 的模型凭据只能设置在 NaturalCC 服务环境中，不能写入后端
  `backend.env`、任务 metadata、HTTP 请求或日志。
- 任务 API 可执行调用方提供的命令，禁止直接暴露到公网。仅应部署在可信网络内，或置于提供
  认证、授权和限流的反向代理之后；建议使用专用主机或隔离环境。
- 当前 SQLite 与 WebSocket 实时分发基线要求单 worker。systemd unit 未传递
  `--workers`，不要在未引入共享日志/消息层前扩展为多 worker。

## 目标目录、账号和权限

| 用途 | 基线路径 | 建议属主/权限 |
| --- | --- | --- |
| 后端程序和 Python 3.11 venv | `/opt/resource-co-debug-platform-backend` | 由部署账号写入；运行账号仅需读取和执行 |
| 运行时配置 | `/etc/resource-co-debug-platform-backend/backend.env` | `root:resource-co-debug`，`0640` |
| workspace、SQLite 和产物 | `/var/lib/resource-co-debug-platform-backend` | `resource-co-debug:resource-co-debug`，目录 `0750` |

创建专用低权限账号和目录前，应确认 `nologin` 的实际路径与本机账号策略。以下命令是示例：

```bash
sudo groupadd --system resource-co-debug
sudo useradd --system --gid resource-co-debug \
  --home-dir /var/lib/resource-co-debug-platform-backend \
  --shell /sbin/nologin --no-create-home resource-co-debug
sudo install -d -o resource-co-debug -g resource-co-debug -m 0750 \
  /var/lib/resource-co-debug-platform-backend \
  /var/lib/resource-co-debug-platform-backend/workspaces
sudo install -d -o root -g resource-co-debug -m 0750 \
  /etc/resource-co-debug-platform-backend
```

后端会在 `STORAGE_ROOT` 下创建项目、任务 workspace 和产物；运行账号必须可写该目录。不要让
运行账号拥有 `/etc` 配置或发布目录的写权限。

## 目标机依赖检查

先在目标 openEuler 主机确认实际版本和可用包名：

```bash
cat /etc/os-release
systemctl --version
python3.11 --version
python3.11 -m venv --help
command -v make
command -v gcc
command -v gdb
```

Python 3.11、Python 3.11 venv 支持、编译工具、GDB 与 systemd 的确切软件包名称和版本因目标
openEuler 发行版、仓库和镜像配置而异。使用目标机的 `dnf search`、`dnf info` 或受管软件源确认后
安装；不要从本文推断包名或版本。NaturalCC 还需要在其单独服务环境中确认 Python 3.12、Aider 和
libclang 依赖。

## 安装和配置

将已审查的后端发布内容放入 `/opt/resource-co-debug-platform-backend`，并使用目标机确认的
Python 3.11 创建虚拟环境和安装当前项目：

```bash
cd /opt/resource-co-debug-platform-backend
sudo python3.11 -m venv /opt/resource-co-debug-platform-backend/.venv
sudo /opt/resource-co-debug-platform-backend/.venv/bin/python -m pip install .
sudo install -m 0644 deploy/resource-co-debug-platform-backend.service \
  /etc/systemd/system/resource-co-debug-platform-backend.service
sudo install -m 0640 -o root -g resource-co-debug \
  deploy/resource-co-debug-platform-backend.env.example \
  /etc/resource-co-debug-platform-backend/backend.env
sudoedit /etc/resource-co-debug-platform-backend/backend.env
```

编辑 `backend.env` 时至少复核：

- `STORAGE_ROOT` 和 `TASK_DATABASE_PATH` 位于 `/var/lib/resource-co-debug-platform-backend`；
- `APP_HOST`、`APP_PORT` 与反向代理或防火墙拓扑一致；示例默认仅监听 loopback；
- `ALLOWED_CORS_ORIGINS` 仅列出实际前端来源；
- `NATURALCC_BASE_URL` 指向独立 NaturalCC 服务；
- `NATURALCC_APPROVE_EXECUTE` 保持 `false`，除非 NaturalCC 在隔离容器或受限 OS 账号中运行。

unit 在 `EnvironmentFile` 前提供 `APP_HOST=127.0.0.1` 与 `APP_PORT=8000` 默认值，环境文件中的
同名配置会覆盖它们，避免遗漏端口或监听地址时进入重启循环。`KillMode=mixed` 会先向后端主进程
发出停止信号，使 `TaskService` 有机会完成 graceful shutdown；主进程退出或
`TimeoutStopSec=45s` 到期后，systemd 会清理残留子孙进程。uvicorn 的
`--timeout-graceful-shutdown 30` 小于该 systemd 超时。
没有配置 `ProtectSystem`、`PrivateDevices`、`SystemCallFilter` 等可能阻断 GDB、编译器或其子进程的
激进 sandbox 选项。生产加固前应先完成编译、调试、取消和权限实测。

## 启动、健康检查和日志

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now resource-co-debug-platform-backend.service
sudo systemctl status resource-co-debug-platform-backend.service
curl --fail http://127.0.0.1:8000/api/v1/health
curl --fail http://127.0.0.1:8000/api/v1/modules/code-generation/health
journalctl -u resource-co-debug-platform-backend.service -f
```

在目标机可先运行下列命令检查 unit 语法，再启用服务：

```bash
sudo systemd-analyze verify /etc/systemd/system/resource-co-debug-platform-backend.service
```

后端日志写入 journald；任务状态和有限日志历史保存在 `TASK_DATABASE_PATH` 所在 SQLite 文件。将
`/var/lib/resource-co-debug-platform-backend` 作为需要备份的持久化数据，不要将其放入临时目录。

## 升级与回滚

1. 先确认没有运行中的任务，或按业务流程取消并等待其终态；停止 unit 会结束其 cgroup 内进程。
2. 停止服务，并备份 `backend.env` 与 `/var/lib/resource-co-debug-platform-backend`，尤其是 SQLite
   文件及其同目录的 journal/WAL 文件。
3. 替换已审查的发布内容，按目标机依赖策略重建或更新 Python 3.11 venv，再运行部署时允许的测试。
4. 执行 `systemd-analyze verify`、`daemon-reload`、启动服务并完成健康检查。
5. 回滚时恢复已验证兼容的程序版本；只有在确认 SQLite schema 兼容时才恢复数据库备份。不要在未知
   schema 兼容性下直接降级运行时状态。

## openEuler 实机验收清单

- [ ] 目标发行版、仓库、Python 3.11/3.12、GDB、编译器及其包版本已记录。
- [ ] systemd unit 通过 `systemd-analyze verify`，并以 `resource-co-debug` 用户运行。
- [ ] 运行时目录权限允许任务创建、日志写入和 SQLite 持久化，发布目录不可由运行账号修改。
- [ ] 后端健康检查和 NaturalCC 连通检查均通过；后端环境文件不包含模型凭据；使用
  `waiting_approval` 实测 c619262 deferred approval 的 write/execute 策略。
- [ ] 上传、编译、调试、取消和子进程回收在目标机账户与 SELinux/安全策略下实测通过。
- [ ] 真实 GDB/编译工具链和 NaturalCC Python 3.12 环境分别验收，不以 Windows 或开发机结果替代。
- [ ] journald 查询、备份、升级与回滚流程已演练。
