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
- 未配置模型 API key 时，只能验收 NaturalCC 进程、CParser 与不调用模型的插件；不得将
  code completion 或 Agent 模型功能宣称为已上线。
- `c619262` 的 NaturalCC Agent 管理 API 没有认证；当前部署只适用于可信内网，且仅监听
  loopback。不可信调用方必须先由上游服务认证，或在平台任务沙箱完成身份拆分后，才能开放访问。
- 任务 API 可执行调用方提供的命令，禁止直接暴露到公网。仅应部署在可信网络内，或置于提供
  认证、授权和限流的反向代理之后；建议使用专用主机或隔离环境。
- 当前 SQLite 与 WebSocket 实时分发基线要求单 worker。systemd unit 未传递
  `--workers`，不要在未引入共享日志/消息层前扩展为多 worker。

## 目标目录、账号和权限

| 用途 | 基线路径 | 建议属主/权限 |
| --- | --- | --- |
| 后端程序和 Python 3.11 venv | `/opt/resource-co-debug-platform-backend` | 由部署账号写入；运行账号仅需读取和执行 |
| 运行时配置 | `/etc/resource-co-debug-platform-backend/backend.env` | `root:resource-co-debug`，`0640` |
| NaturalCC 源码 | `/opt/naturalcc` | `root:root`，运行账号只读 |
| NaturalCC Python 3.12 运行环境和 libclang | `/opt/naturalcc-runtime` | `root:root`，运行账号只读和执行 |
| NaturalCC 运行时配置 | `/etc/naturalcc-agent/naturalcc.env` | `root:root`，`0600` |
| NaturalCC 状态 | `/var/lib/naturalcc-agent` | 由 `StateDirectory=naturalcc-agent` 创建，`naturalcc-agent:naturalcc-agent`，`0750` |
| workspace、SQLite 和产物 | `/var/lib/resource-co-debug-platform-backend` | workspace 根目录为 `resource-co-debug:naturalcc-workspace`，`2770` 加默认 ACL |

创建专用低权限账号和目录前，应确认 `nologin` 的实际路径与本机账号策略。以下命令是示例：
新装环境可直接执行；已运行环境必须先按“安装和配置”节的升级前置顺序停机和备份，再执行 ACL、venv 和
unit 变更。

```bash
for group in resource-co-debug naturalcc-agent naturalcc-workspace; do
  if getent group "$group" >/dev/null; then getent group "$group"; else sudo groupadd --system "$group"; fi
done
if getent passwd resource-co-debug >/dev/null; then
  id resource-co-debug
else
  sudo useradd --system --gid resource-co-debug --home-dir /var/lib/resource-co-debug-platform-backend \
    --shell /sbin/nologin --no-create-home resource-co-debug
fi
if getent passwd naturalcc-agent >/dev/null; then
  id naturalcc-agent
else
  sudo useradd --system --gid naturalcc-agent --home-dir /var/lib/naturalcc-agent \
    --shell /sbin/nologin --no-create-home naturalcc-agent
fi
for user in resource-co-debug naturalcc-agent; do
  if id -nG "$user" | tr ' ' '\n' | grep -Fx naturalcc-workspace >/dev/null; then
    id "$user"
  else
    sudo usermod -a -G naturalcc-workspace "$user"
  fi
done
sudo install -d -o resource-co-debug -g resource-co-debug -m 0750 \
  /var/lib/resource-co-debug-platform-backend \
  /var/lib/resource-co-debug-platform-backend/workspaces
sudo setfacl -m u:naturalcc-agent:--x /var/lib/resource-co-debug-platform-backend
sudo chgrp naturalcc-workspace /var/lib/resource-co-debug-platform-backend/workspaces
sudo chmod 2770 /var/lib/resource-co-debug-platform-backend/workspaces
sudo setfacl -R -m g:naturalcc-workspace:rwX,m::rwX \
  /var/lib/resource-co-debug-platform-backend/workspaces
sudo find /var/lib/resource-co-debug-platform-backend/workspaces -type d -exec \
  setfacl -m d:g:naturalcc-workspace:rwx,d:m::rwx {} +
sudo install -d -o root -g resource-co-debug -m 0750 \
  /etc/resource-co-debug-platform-backend
```

后端会在 `STORAGE_ROOT` 下创建项目、任务 workspace 和产物；运行账号必须可写该目录。不要让
运行账号拥有 `/etc` 配置或发布目录的写权限。两个服务账号只通过 `naturalcc-workspace` 组和上述
setgid/default ACL 共享 workspace；`naturalcc-agent` 在后端状态根只有 `--x` traverse ACL，不能读取或
写入 `tasks.sqlite3`。执行 ACL 迁移后，以真实深层路径验证其读写权限：

```bash
sudo -u naturalcc-agent sh -ceu '
probe="$(mktemp -d /var/lib/resource-co-debug-platform-backend/workspaces/.naturalcc-acl.XXXXXX)"
trap "rm -rf \"$probe\"" EXIT
mkdir -p "$probe/deep/level"
printf "naturalcc-agent\n" > "$probe/deep/level/roundtrip.txt"
test -r "$probe/deep/level/roundtrip.txt"
test "$(cat "$probe/deep/level/roundtrip.txt")" = naturalcc-agent
'
```

## 目标机依赖检查

先在目标 openEuler 主机确认实际版本和可用包名：

```bash
cat /etc/os-release
systemctl --version
python3.11 --version
python3.11 -m venv --help
command -v curl
command -v setfacl
sudo dnf install -y jq
command -v jq
command -v make
command -v gcc
command -v gdb
```

Python 3.11、Python 3.11 venv 支持、uv 管理的 Python 3.12、编译工具、GDB 与 systemd 的确切软件包名称和版本因目标
openEuler 发行版、仓库和镜像配置而异。使用目标机的 `dnf search`、`dnf info` 或受管软件源确认后
安装；不要从本文推断包名或版本。NaturalCC 还需要在其单独服务环境中确认 Python 3.12、Aider 和
libclang 依赖。`jq` 是本部署健康 JSON 断言的明确依赖；openEuler 24.03 安装前应执行上述
`sudo dnf install -y jq`，而不是仅以 HTTP 200 作为健康结论。

## 安装和配置

### 新装与升级顺序

新装环境按本文顺序执行账号/ACL、后端 venv、NaturalCC 运行环境、unit/env，最后启动健康检查。已运行环境在
修改任何 ACL、venv 或 unit 前，必须先确认无 `RUNNING` 任务，停止后端再停止 NaturalCC，并完成备份：

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/tasks | \
  jq -e '[.data[] | select(.status == "RUNNING")] | length == 0'
sudo systemctl stop resource-co-debug-platform-backend.service
sudo systemctl stop naturalcc.service
backup=/var/backups/resource-co-debug-platform-backend/pre-naturalcc-$(date -u +%Y%m%dT%H%M%SZ).tar.gz
sudo install -d -o root -g root -m 0700 "$(dirname "$backup")"
sudo tar -C / -czf "$backup" \
  etc/resource-co-debug-platform-backend/backend.env \
  etc/naturalcc-agent/naturalcc.env \
  var/lib/resource-co-debug-platform-backend \
  var/lib/naturalcc-agent
```

该归档包含两套 env、两套 SQLite 状态及存在的 `-wal`/`-shm` 文件。升级时保留现有 env；以下示例文件仅在
目标文件不存在时安装。

将已审查的后端发布内容放入 `/opt/resource-co-debug-platform-backend`，并使用目标机确认的
Python 3.11 创建虚拟环境和安装当前项目：

```bash
cd /opt/resource-co-debug-platform-backend
sudo python3.11 -m venv /opt/resource-co-debug-platform-backend/.venv
sudo /opt/resource-co-debug-platform-backend/.venv/bin/python -m pip install .
sudo install -m 0644 deploy/resource-co-debug-platform-backend.service \
  /etc/systemd/system/resource-co-debug-platform-backend.service
if ! sudo test -e /etc/resource-co-debug-platform-backend/backend.env; then
  sudo install -m 0640 -o root -g resource-co-debug \
    deploy/resource-co-debug-platform-backend.env.example \
    /etc/resource-co-debug-platform-backend/backend.env
fi
sudoedit /etc/resource-co-debug-platform-backend/backend.env
```

对于已验收的 NaturalCC `ncc3@c619262`，将源码部署在 `/opt/naturalcc`。使用固定的 uv `0.12.15` 和
CPython `3.12.14`，
将锁定依赖同步到独立运行环境，并安装固定版本 libclang；完成 tokenizer 安装后再将源码保持为 root 只读：

```bash
sudo install -d -o root -g root -m 0755 /opt/naturalcc-runtime/uv-bootstrap/bin \
  /opt/naturalcc-runtime/python
# 使用 uv 官方 standalone installer；UV_UNMANAGED_INSTALL 不修改 shell 配置或 PATH。
curl -LsSf https://astral.sh/uv/0.12.15/install.sh | \
  sudo env UV_UNMANAGED_INSTALL=/opt/naturalcc-runtime/uv-bootstrap/bin sh
UV_BIN=/opt/naturalcc-runtime/uv-bootstrap/bin/uv
UV_PYTHON_INSTALL_DIR=/opt/naturalcc-runtime/python
test -x "$UV_BIN"
"$UV_BIN" --version | grep -Fx "uv 0.12.15"
sudo env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
  "$UV_BIN" python install 3.12.14
MANAGED_PYTHON="$(sudo env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
  "$UV_BIN" python find --managed-python 3.12.14)"
"$MANAGED_PYTHON" --version | grep -Fx "Python 3.12.14"
sudo env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
  UV_PROJECT_ENVIRONMENT=/opt/naturalcc-runtime/venv \
  "$UV_BIN" sync --project /opt/naturalcc/code_agent --python "$MANAGED_PYTHON" --frozen --no-python-downloads
sudo env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
  "$UV_BIN" pip install --python "$MANAGED_PYTHON" \
  --target /opt/naturalcc-runtime/libclang-18.1.1 libclang==18.1.1 --no-python-downloads
sudo /opt/naturalcc-runtime/venv/bin/python \
  /opt/naturalcc/code_agent/scripts/install_deepseek_tokenizer.py
sudo chown -R root:root /opt/naturalcc
sudo chmod -R go-w /opt/naturalcc
```

本文不虚构 installer 或 Python 分发包的 checksum；生产部署必须按组织制品校验策略使用受控镜像或已验证
的 hash/attestation 后再下载上述版本化 URL。

然后安装仓库中的配套 unit、环境文件示例和后端依赖 drop-in：

```bash
sudo install -d -o root -g root -m 0755 /etc/naturalcc-agent
sudo install -m 0644 deploy/openeuler/naturalcc.service \
  /etc/systemd/system/naturalcc.service
if ! sudo test -e /etc/naturalcc-agent/naturalcc.env; then
  sudo install -m 0600 -o root -g root \
    deploy/openeuler/naturalcc.env.example /etc/naturalcc-agent/naturalcc.env
fi
sudo install -d -o root -g root -m 0755 \
  /etc/systemd/system/resource-co-debug-platform-backend.service.d
sudo install -m 0644 deploy/openeuler/resource-co-debug-platform-backend-naturalcc.conf \
  /etc/systemd/system/resource-co-debug-platform-backend.service.d/naturalcc.conf
sudoedit /etc/naturalcc-agent/naturalcc.env
```

模型密钥与 `LIBCLANG_PATH` 只在 `naturalcc.env` 中设置；示例仅保留注释密钥占位。该文件由 PID 1
读取后传给服务，因此必须保持 `root:root`、`0600`。NaturalCC unit 由 `StateDirectory=naturalcc-agent`
创建状态目录，并仅额外允许写入后端 workspace 目录。启用服务前验证运行账号可读依赖，并执行真实 CParser
解析冒烟：

```bash
sudo -u naturalcc-agent /usr/bin/test -r \
  /opt/naturalcc-runtime/libclang-18.1.1/clang/native/libclang.so
sudo -u naturalcc-agent /usr/bin/test -r \
  /opt/naturalcc/code_agent/resources/deepseek_v3_tokenizer/tokenizer.json
sudo -u naturalcc-agent env PYTHONPATH=/opt/naturalcc \
  LIBCLANG_PATH=/opt/naturalcc-runtime/libclang-18.1.1/clang/native/libclang.so \
  /opt/naturalcc-runtime/venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from code_agent.rag.c.cfile_parse import CParser

with TemporaryDirectory() as directory:
    source = Path(directory) / "smoke.c"
    source.write_text("int main(void) { return 0; }\\n", encoding="utf-8")
    assert CParser().parse(str(source)), "CParser returned no parse result"
print("CParser smoke passed")
PY
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
sudo systemd-analyze verify /etc/systemd/system/naturalcc.service \
  /etc/systemd/system/resource-co-debug-platform-backend.service
sudo systemctl enable --now naturalcc.service
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:7860/api/health | jq -e '.status == "ok"'
sudo systemctl enable --now resource-co-debug-platform-backend.service
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/health | jq -e '.data.status == "UP"'
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/modules/code-generation/health | \
  jq -e '.data.status == "available"'
sudo systemctl status naturalcc.service resource-co-debug-platform-backend.service
journalctl -u resource-co-debug-platform-backend.service -f
```

后端 `/api/v1/modules/code-generation/health` 返回 HTTP 200 不足以说明 NaturalCC 可用；必须由
`jq -e '.data.status == "available"'` 成功退出。NaturalCC 的独立健康端点是 `7860/api/health`。
后端 drop-in 会在单次启动内对 NaturalCC readiness 最多探测 5 次：每次 `curl` 最长 5 秒，失败后等待
5 秒，因此 `systemctl start resource-co-debug-platform-backend.service` 最多等待约 50 秒。NaturalCC 冷启动
约 8 秒时，后端会在该窗口内继续等待而不是首次探测失败即返回；若窗口耗尽，继承的
`Restart=on-failure` 会在 `RestartSec=5s` 后开始新的有界窗口。该 `/bin/sh -c` 命令用单引号将循环作为
一个参数传给 shell，且只遍历常量列表，不含 `$` 变量展开。

后端日志写入 journald；任务状态和有限日志历史保存在 `TASK_DATABASE_PATH` 所在 SQLite 文件。将
`/var/lib/resource-co-debug-platform-backend` 作为需要备份的持久化数据，不要将其放入临时目录。

## 升级与回滚

已运行环境必须先完成“新装与升级顺序”中的无 `RUNNING` 检查、停机和备份。完成备份后：

1. 替换已审查的发布内容，按目标机依赖策略更新后端 venv，并重跑上述由 `UV_BIN` 和
   `UV_PYTHON_INSTALL_DIR` 固定的 `sync --frozen` 与 libclang 安装步骤。
2. 重新执行账号/组验证、父目录 traverse ACL、workspace ACL 和 unit 安装；两套现有 env 均不得覆盖。
3. 执行 `systemd-analyze verify`、`daemon-reload`，先启动 NaturalCC 并通过 `7860/api/health`，再启动后端并
   通过 `jq -e '.data.status == "available"'` 验收连通性。
4. 回滚时恢复已验证兼容的程序版本；只有在确认 SQLite schema 兼容时才恢复数据库备份。不要在未知
   schema 兼容性下直接降级运行时状态。

## openEuler 实机验收清单

- [ ] 目标发行版、仓库、Python 3.11/3.12、GDB、编译器及其包版本已记录。
- [ ] systemd unit 通过 `systemd-analyze verify`；后端以 `resource-co-debug`、NaturalCC 以
  `naturalcc-agent` 运行。
- [ ] workspace 的 setgid/default ACL 允许两个服务账号协作，发布目录和两套环境文件不可由运行账号修改。
- [ ] NaturalCC 的 libclang/tokenizer 可读检查和真实 CParser 冒烟通过。
- [ ] 后端健康检查和 NaturalCC 连通检查均通过，且连通接口的 `.data.status` 为 `available`；后端环境文件不包含模型凭据；使用
  `waiting_approval` 实测 c619262 deferred approval 的 write/execute 策略。
- [ ] 上传、编译、调试、取消和子进程回收在目标机账户与 SELinux/安全策略下实测通过。
- [ ] 真实 GDB/编译工具链和 NaturalCC Python 3.12 环境分别验收，不以 Windows 或开发机结果替代。
- [ ] journald 查询、备份、升级与回滚流程已演练。
