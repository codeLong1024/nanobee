# 多实例部署运维手册

> nanobee Gateway 支持在同一台主机上运行多个独立的 Gateway 实例，
> 每个实例拥有独立的配置文件、数据目录、日志和通道组合。

---

## 目录

1. [单实例 vs 多实例](#单实例-vs-多实例)
2. [目录结构约定](#目录结构约定)
3. [实例配置文件](#实例配置文件)
4. [systemd 托管部署](#systemd-托管部署)
5. [运维命令速查](#运维命令速查)
6. [新增实例](#新增实例)
7. [删除实例](#删除实例)
8. [故障排查](#故障排查)

---

## 单实例 vs 多实例

| 场景 | 命令 | 适用情况 |
|------|------|----------|
| **单实例** | `nanobee gateway -c /path/to/config.yaml` | 开发测试、一台主机只跑一个 Gateway |
| **多实例** | `NANOBEE_DATA_DIR=/nanobee-data nanobee svc start` | 一台主机跑 N 个业务隔离的 Gateway |

**核心原则**：单实例不走 svc 层——直接 `nanobee gateway` 即可。svc 是多实例的批量管理入口，通过 `NANOBEE_DATA_DIR` 环境变量知晓扫描目标。

---

## 目录结构约定

```
/nanobee-data/                     # 数据根目录（NANOBEE_DATA_DIR 指向这里）
├── .pid/                          # PID 文件目录（svc 自动管理）
├── <instance-1>/                  # 实例 1
│   ├── config.yaml                #   实例配置文件
│   ├── core.md                    #   灵魂文件（可选）
│   ├── skills/                    #   实例级技能（管理员配属）
│   │   └── <skill-name>/
│   │       └── SKILL.md
│   ├── logs/                      #   运行时日志（程序自管理 + stderr 重定向）
│   │   ├── gateway-out.log       #     子进程 stdout/stderr 重定向
│   │   └── nanobee.log           #     程序 loguru 文件 sink
│   └── users/                     #   用户数据（上下文、记忆、临时文件）
├── <instance-2>/                  # 实例 2
│   ├── config.yaml
│   ├── core.md
│   ├── skills/
│   ├── logs/
│   └── users/
└── ...
```

**关键约定**：
- **数据根目录**：由 `NANOBEE_DATA_DIR` 环境变量指定，svc 扫描其一级子目录
- **实例目录**：数据根目录下的每个子目录即为一个实例，目录名 = 实例名
- **配置文件**：每个实例目录必须包含 `config.yaml`
- **PID 文件**：统一存放在 `<数据根目录>/.pid/`，命名基于配置路径的 SHA1 哈希

---

## 实例配置文件

每个实例目录下必须有一个 `config.yaml`，参考 `deploy/config.example.yaml` 作为模板。

### 最小配置

```yaml
# 数据目录（必填 —— 指向实例自己的目录）
data_dir: "/nanobee-data/<instance-1>"

# 网关端口（必填 —— 健康检查端口，每个实例必须不同）
gateway:
  port: 8080

# Agent 默认配置
agents:
  defaults:
    provider: custom
    model: deepseek-v4-flash
    max_tokens: 50000
    context_window_tokens: 800000
    temperature: 0.1
    max_iterations: 30

# LLM 提供者
providers:
  custom:
    api_key: "your-api-key"
    api_base: "http://your-endpoint:4000/v1"

# 通道配置（按需启用）
channels:
  channel_dingtalk:
    enabled: true
    client_id: "ding-xxx"
    client_secret: "xxx"
```

### 关键配置项说明

| 配置项 | 必填 | 说明 |
|--------|------|------|
| `data_dir` | 是 | 指向实例目录自身（如 `/nanobee-data/<instance-1>`） |
| `gateway.port` | 是 | 健康检查端口，**多实例不能重复** |
| `core_md_path` | 建议 | 灵魂文件路径，建议放在 `data_dir` 下 |
| `agents.defaults.provider` | 是 | LLM 提供者名称 |
| `agents.defaults.model` | 是 | 模型名称，格式 `provider/model` 或 `model-name` |
| `channels.<name>.enabled` | 可选 | 启用/禁用通道，实现实例级通道隔离 |
| `skills.enabled` | 可选 | 声明要注入的实例级技能名称列表 |
| `plugins` | 可选 | 插件级配置（tool_shell 沙箱、钉钉 MCP 等） |

### 端口规划原则

多实例情况下，每个实例的 `gateway.port` 必须唯一。建议预留连续的端口段：

| 实例名 | 端口 |
|--------|------|
| <instance-1> | 8080 |
| <instance-2> | 8081 |
| <instance-3> | 8082 |

---

## systemd 托管部署

### 第一步：准备数据目录

```bash
# 创建数据根目录
sudo mkdir -p /nanobee-data
sudo chown nanobee:nanobee /nanobee-data

# 创建实例目录并放置配置
sudo -u nanobee mkdir -p /nanobee-data/<instance-1>
sudo -u nanobee cp deploy/config.example.yaml /nanobee-data/<instance-1>/config.yaml
sudo -u nanobee vim /nanobee-data/<instance-1>/config.yaml
```

### 第二步：安装 systemd unit

```bash
# 复制 unit 文件到系统目录
sudo cp deploy/nanobee-gateway.service /etc/systemd/system/

# 根据实际环境修改以下字段：
sudo vim /etc/systemd/system/nanobee-gateway.service
#   User=nanobee                    # 运行用户
#   Group=nanobee                   # 运行用户组
#   Environment="NANOBEE_DATA_DIR=/nanobee-data"   # 数据根目录
#   ExecStart=.../nanobee-gateway.sh start          # 启动脚本路径
#   WorkingDirectory=/home/nanobee/nanobee           # 工作目录
```

### 第三步：启用并启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable nanobee-gateway
sudo systemctl start nanobee-gateway
```

### 第四步：验证

```bash
# 查看服务状态
sudo systemctl status nanobee-gateway

# 查看各实例状态
sudo -u nanobee NANOBEE_DATA_DIR=/nanobee-data nanobee svc status
```

### systemd unit 安全加固说明

`deploy/nanobee-gateway.service` 内建了完整的安全加固配置：

| 配置项 | 作用 |
|--------|------|
| `ProtectSystem=full` | 系统目录只读，防御文件篡改 |
| `ReadWritePaths=/nanobee-data` | 唯一可写路径，最小化攻击面 |
| `PrivateTmp=yes` | 隔离 `/tmp`，防符号链接攻击 |
| `NoNewPrivileges=yes` | 禁止提权（setuid/setgid 等） |
| `ProtectHome=read-only` | 家目录只读（允许读代码，不可写数据） |
| `PrivateDevices=yes` | 隔离设备文件 |
| `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` | 网络协议族白名单 |
| `ProtectKernelTunables=yes` | 禁止修改内核参数 |
| `ProtectControlGroups=yes` | 禁止操作 cgroups |
| `InaccessiblePaths=/root /home/*/.* /etc/shadow /etc/sudoers` | 敏感文件不可访问 |

---

## 运维命令速查

所有 svc 命令需要设置 `NANOBEE_DATA_DIR` 环境变量。如果是 systemd 托管，该变量已在 unit 文件中声明。

### 状态管理

```bash
# 查看所有实例状态（表格格式）
nanobee svc status
# 输出示例:
# NAME                 PORT     PID        STATUS       CONFIG
# --------------------------------------------------------------------------------
# <instance-1>           8080     12345      RUNNING      /nanobee-data/<instance-1>/config.yaml
# <instance-2>           8081     12346      STOPPED      /nanobee-data/<instance-2>/config.yaml

# 启动所有实例
nanobee svc start
# 输出: <instance-1>: OK / <instance-2>: OK

# 启动指定实例
nanobee svc start <instance-1>

# 停止所有实例
nanobee svc stop

# 停止指定实例
nanobee svc stop <instance-1>

# 重启指定实例（先停止，等待 2s，再启动）
nanobee svc restart <instance-1>
```

### 日志管理

```bash
# 查看实例最后 50 行日志
nanobee svc logs <instance-1>

# 查看最后 200 行
nanobee svc logs <instance-1> -n 200

# 跟踪日志（Ctrl+C 退出）
nanobee svc logs <instance-1> -f
```

### 日志文件说明

每个实例有两类日志：

| 日志文件 | 来源 | 内容 |
|----------|------|------|
| `logs/gateway-out.log` | subprocess stdout/stderr 重定向 | Gateway 进程的全部输出（svc 管理） |
| `logs/nanobee.log` | loguru 文件 sink | 程序自管理日志（需在 config.yaml 中配置 `logging.file`） |

---

## 新增实例

在运行中的多实例系统上新增一个实例，**无需重启其他实例**：

```bash
# 1. 创建实例目录和配置
sudo -u nanobee mkdir -p /nanobee-data/<new-instance>/logs
sudo -u nanobee cp deploy/config.example.yaml /nanobee-data/<new-instance>/config.yaml
sudo -u nanobee vim /nanobee-data/<new-instance>/config.yaml
# 修改: data_dir、gateway.port（确保不与其他实例冲突）、通道密钥等

# 2. 创建 core.md（可选）
sudo -u nanobee vim /nanobee-data/<new-instance>/core.md

# 3. 启动新实例
NANOBEE_DATA_DIR=/nanobee-data nanobee svc start <new-instance>

# 4. 验证
NANOBEE_DATA_DIR=/nanobee-data nanobee svc status
```

---

## 删除实例

```bash
# 1. 停止目标实例
NANOBEE_DATA_DIR=/nanobee-data nanobee svc stop <old-instance>

# 2. 删除实例目录
sudo -u nanobee rm -rf /nanobee-data/<old-instance>

# 3. 清理遗弃的 PID 文件（svc status 会自动清理 stale PID，也可手动删除）
# 无需额外操作——下次 status 时会自动处理
```

---

## 故障排查

### 实例无法启动

```bash
# 1. 检查配置文件是否存在
ls -la /nanobee-data/<instance>/config.yaml

# 2. 检查端口是否被占用
ss -tlnp | grep <port>

# 3. 检查日志
nanobee svc logs <instance> -n 100

# 4. 检查虚拟环境
ls -la /path/to/.venv/bin/python
```

### 实例健康检查失败

```bash
# 手动测试健康端点
curl http://127.0.0.1:<port>/health

# 检查 Gateway 进程是否在运行
nanobee svc status
```

### PID 文件残留

svc 的 `status` 命令会自动检测并清理 stale PID（文件存在但进程已死）。如果仍有残留：

```bash
# 查看 .pid 目录内容
ls /nanobee-data/.pid/

# 手动删除可疑 PID
rm /nanobee-data/.pid/<sha1-hash>.pid

# 重新运行 status 确认
NANOBEE_DATA_DIR=/nanobee-data nanobee svc status
```

### systemd 相关

```bash
# 查看 systemd 日志
sudo journalctl -u nanobee-gateway -f

# 查看最近 50 行
sudo journalctl -u nanobee-gateway -n 50

# 检查环境变量是否生效
sudo systemctl show nanobee-gateway | grep Environment
```

### 常用检查清单

- [ ] `NANOBEE_DATA_DIR` 环境变量指向正确的数据根目录
- [ ] 每个实例的 `config.yaml` 中 `gateway.port` 不重复
- [ ] `config.yaml` 中 `data_dir` 指向实例自身目录
- [ ] 虚拟环境存在且 Python 可执行
- [ ] systemd unit 中 `User`/`Group` 有数据目录的读写权限
- [ ] 防火墙/安全组放行了各实例的 `gateway.port`
