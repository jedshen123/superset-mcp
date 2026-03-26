# 服务器部署与调试指南

在服务器上拉取最新分支、安装依赖并运行 Superset MCP 服务。

---

## 一、环境要求

- Linux 服务器（已 SSH 可登录）
- Python 3.10 或以上
- Git

---

## 二、拉取代码并安装

### 1. 克隆仓库（首次部署）

```bash
# 进入你打算放代码的目录，例如
cd /opt
sudo mkdir -p apps && sudo chown $(whoami) apps
cd apps

# 克隆仓库并进入项目
git clone https://github.com/jedshen123/superset-mcp.git
cd superset-mcp
```

### 2. 拉取指定分支（若已克隆过，只更新代码）

```bash
cd /path/to/superset-mcp   # 改成你的项目路径

git fetch origin
git checkout momcozy       # 或你的目标分支名
git pull origin momcozy
```

### 3. 安装 Python 依赖

**方式 A：用 pip（推荐，通用）**

```bash
cd /path/to/superset-mcp
python3 -m venv .venv
source .venv/bin/activate   # Linux/macOS
# Windows: .venv\Scripts\activate

pip install -e .
```

**方式 B：用 uv（若已安装 uv）**

```bash
cd /path/to/superset-mcp
uv venv && source .venv/bin/activate
uv pip install -e .
```

### 4. 配置环境变量

```bash
cp .env.example .env
nano .env   # 或 vim / vi
```

在 `.env` 中填写你**服务器能访问到的** Superset 地址和账号（可与本地不同）。

**若要从本机 Kimi 等客户端连到这台服务器的 MCP**，需让 MCP 监听所有网卡并开放端口：

- 在 `.env` 中增加（或启动前 export）：
  ```env
  FASTMCP_HOST=0.0.0.0
  FASTMCP_PORT=8000
  ```
- 服务器防火墙/安全组放行 **8000** 端口（入站）。

```env
SUPERSET_BASE_URL=http://你的Superset地址:端口
SUPERSET_USERNAME=你的用户名
SUPERSET_PASSWORD=你的密码
```

保存后确认文件存在且未提交到 Git：

```bash
cat .env
# 确认 .gitignore 包含 .env（本项目已包含）
```

---

## 三、运行与调试

### 前台运行（调试用，看日志）

**STDIO 模式（供本地 MCP 客户端拉起进程用）：**

```bash
cd /path/to/superset-mcp
source .venv/bin/activate
python main.py
```

**HTTP 模式（对外提供 MCP 接口，方便 Kimi/其他客户端通过 URL 连接）：**

```bash
cd /path/to/superset-mcp
source .venv/bin/activate
python main.py --transport http --port 8000
```

- 默认监听 `127.0.0.1:8000`，仅本机可访问。
- 若需外网访问，需在 Superset MCP 所在服务器开放端口，或使用 Nginx 反向代理；同时注意防火墙与安全（建议加认证或内网访问）。

调试时直接看终端输出即可；按 `Ctrl+C` 停止。

### 后台运行（长期运行）

用 `nohup` 或 `screen`/`tmux` 即可，例如：

```bash
cd /path/to/superset-mcp
source .venv/bin/activate
nohup python main.py --transport http --port 8000 > mcp.log 2>&1 &
echo $!   # 记下 PID，便于后续 kill
```

查看日志：

```bash
tail -f /path/to/superset-mcp/mcp.log
```

---

## 四、可选：systemd 服务（开机自启、重启管理）

创建服务文件：

```bash
sudo nano /etc/systemd/system/superset-mcp.service
```

写入（**注意把 `/path/to/superset-mcp` 和 `你的用户名` 改成实际值**）：

```ini
[Unit]
Description=Superset MCP Server (HTTP)
After=network.target

[Service]
Type=simple
User=你的用户名
WorkingDirectory=/path/to/superset-mcp
Environment="PATH=/path/to/superset-mcp/.venv/bin:/usr/local/bin:/usr/bin"
ExecStart=/path/to/superset-mcp/.venv/bin/python main.py --transport http --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable superset-mcp
sudo systemctl start superset-mcp
sudo systemctl status superset-mcp
```

常用命令：

```bash
sudo systemctl stop superset-mcp    # 停止
sudo systemctl restart superset-mcp # 重启
journalctl -u superset-mcp -f       # 看日志
```

---

## 五、更新代码（后续拉取最新分支）

```bash
cd /path/to/superset-mcp
git fetch origin
git checkout momcozy
git pull origin momcozy
source .venv/bin/activate
pip install -e .   # 依赖有变更时执行
# 若用 systemd：
sudo systemctl restart superset-mcp
```

---

## 六、验证

- **HTTP 模式**：在服务器本机或能访问该端口的机器上执行：
  ```bash
  curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/mcp
  ```
  返回 `405` 为正常（GET 不允许，需 POST/MCP 协议）。
- 用 Kimi CLI 等配置 MCP 服务器地址为：`http://服务器IP:8000/mcp`（需网络可达并做好安全策略）。

---

## 七、常见问题

| 现象 | 处理 |
|------|------|
| `python: command not found` | 使用 `python3`，或安装 Python 3.10+ |
| 依赖安装失败 | 确认已激活 venv，或使用 `pip install -e .` 前先升级 pip |
| 连接 Superset 超时 | 检查 `.env` 中 `SUPERSET_BASE_URL` 在服务器上能否访问（curl 或浏览器） |
| 看板列表为空 | 见 README「Troubleshooting」中 Public 角色权限说明 |
| **Kimi 报错「Failed to connect / All connection attempts failed」** | 见下方「八、远程连接（Kimi 连服务器）」 |

---

## 八、远程连接（Kimi 连服务器 MCP）

要让本机 Kimi 通过 `http://54.226.190.74:8000/mcp` 连到服务器上的 MCP，需满足：

1. **MCP 监听 0.0.0.0**（否则只接受本机 127.0.0.1）  
   在**服务器**上启动前设置并启动：
   ```bash
   export FASTMCP_HOST=0.0.0.0
   export FASTMCP_PORT=8000
   python main.py --transport http
   ```
   或在 `.env` 里写 `FASTMCP_HOST=0.0.0.0` 和 `FASTMCP_PORT=8000` 再启动。

2. **防火墙 / 安全组放行 8000**  
   - 云服务器：在控制台为该实例的**安全组**添加入站规则，放行 TCP 8000（来源 0.0.0.0/0 或你的本机 IP）。  
   - 本机防火墙：`sudo ufw allow 8000` 等（视系统而定）。

3. **在本机验证**  
   在你电脑上执行：
   ```bash
   curl -v http://54.226.190.74:8000/mcp
   ```
   - 能连上：通常返回 405（Method Not Allowed）为正常。  
   - 超时或 connection refused：检查上面 1、2 步和服务器是否在跑、端口是否一致。

4. **按用户鉴权（HTTP Basic Auth，可选）**  
   - **不传 Basic Auth**：使用服务器 `.env` 里的默认 Superset 账号，Kimi 只需 `kimi mcp add --transport http superset http://服务器:8000/mcp`。  
   - **传 Basic Auth**：按用户切换 Superset 账号（将 `user` / `pass` 换成实际账号）：  
     `kimi mcp add --transport http superset http://服务器:8000/mcp --header "Authorization: Basic $(echo -n 'user:pass' | base64)"`  
   - 若请求里带了 Basic 但 Superset 登录失败，返回 401。
