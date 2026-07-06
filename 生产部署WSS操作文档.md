# MT5 多节点跟单系统 · 生产服务器部署 WSS 操作文档

> 本文专讲「如何在生产服务器上把系统从 HTTP/WS 升级为 HTTPS/WSS」。
> 通用部署流程见 [`使用与部署文档.md`](./使用与部署文档.md)，架构细节见 [`技术实现方案.md`](./技术实现方案.md)。
> 按本文从上到下执行，即可让「浏览器后台、MT5 节点端、TradingView Webhook」全部走加密通道。

---

## 0. 原理与现状（先理解，再动手）

### 0.1 加密在哪里终止

TLS（证书）只在 **Nginx** 上终止，后端 FastAPI 仍跑明文，**后端代码无需任何改动**：

```
浏览器 / MT5 节点端 / TradingView
        │  wss:// , https://         （公网加密）
        ▼
   ┌─────────────┐
   │   Nginx     │  443 监听 + 证书，处理 WebSocket Upgrade
   └─────────────┘
        │  http://backend:8000        （容器内网，明文）
        ▼
   ┌─────────────┐
   │  backend    │  FastAPI（/ws/node、/ws/admin、/api、/webhook）
   └─────────────┘
```

因此「部署 WSS」本质上就是三件事：**给 Nginx 装证书 → 打开 443 配置 → 把客户端地址改成 `wss://`/`https://`**。

### 0.2 本项目当前状态（基于现有代码）

| 项 | 现状 | 本文要做的事 |
| --- | --- | --- |
| Nginx 80 端口 | `deploy/nginx/default.conf` 已配好 `/ws/` 的 Upgrade 转发 | 改为「跳转到 443」 |
| Nginx 443 端口 | 文件第 43–66 行是**被注释的模板**，未启用 | 取消注释并补全为生产配置 |
| 证书目录 | `deploy/nginx/certs/` 为空（仅 `.gitkeep`），compose 已挂载到容器 `/etc/nginx/certs` | 放入 `fullchain.pem` / `privkey.pem` |
| 前端 WS | `frontend/src/services/ws.ts` 会按页面协议自动选择 `wss`/`ws` | **无需改前端**，用 `https://` 打开后台即自动走 `wss` |
| 后端节点网关 | `/ws/node` 首包鉴权（token 在消息体，不在 URL） | 无需改 |
| 后台实时推送 | `/ws/admin?token=<JWT>` | 无需改 |
| 节点端 | `node_client/.env` 现为 `MANAGER_WS_URL=ws://127.0.0.1:8080/ws/node` | 改为 `wss://<域名>/ws/node` |

---

## 1. 前置条件

1. 已能用 HTTP 正常启动整套服务（见《使用与部署文档》§5），即 `mysql + redis + backend + nginx` 四个容器健康。
2. 一个**域名**（如 `hub.example.com`），并已把 A 记录解析到服务器公网 IP。
   - WSS 证书强依赖域名；如果只有公网 IP 没有域名，只能用自签证书（见 §2.3，浏览器/节点端默认不信任）。
3. 服务器安全组 / 防火墙放行入站 **443**（用 Let's Encrypt 首次签发还需放行 **80**）。
4. 已安装 Docker 与 Docker Compose。

> Windows 服务器有额外注意点（端口、证书签发工具），见 §10。

---

## 2. 准备 TLS 证书（三选一）

目标产物固定为两份文件，最终放到 `deploy/nginx/certs/`：

```
deploy/nginx/certs/fullchain.pem   # 证书链（服务器证书 + 中间证书）
deploy/nginx/certs/privkey.pem     # 私钥
```

### 2.1 方案 A：Let's Encrypt 免费证书（推荐，Linux）

公共 CA 签发，**浏览器与节点端默认信任，节点端零额外配置**。

首次签发用 `standalone` 模式（让 certbot 临时占用 80 端口，因此先停掉 nginx 容器）：

```bash
# 1) 安装 certbot（Ubuntu/Debian 示例）
sudo apt update && sudo apt install -y certbot

# 2) 临时停掉 nginx，腾出 80 端口
docker compose stop nginx

# 3) 签发证书（把域名和邮箱换成你的）
sudo certbot certonly --standalone \
  -d hub.example.com \
  --non-interactive --agree-tos -m admin@example.com

# 4) 拷贝到项目证书目录（certbot 默认存放在 /etc/letsencrypt/live/<域名>/）
sudo cp /etc/letsencrypt/live/hub.example.com/fullchain.pem deploy/nginx/certs/fullchain.pem
sudo cp /etc/letsencrypt/live/hub.example.com/privkey.pem   deploy/nginx/certs/privkey.pem
```

> 续期见 §9。若希望续期时不停服，请改用 webroot 模式（§9.2）。

### 2.2 方案 B：已有商业证书 / 云厂商证书

把厂商给的文件整理成如下两份，放进 `deploy/nginx/certs/`：

- `fullchain.pem`：**服务器证书 + 中间证书**按顺序拼接（很多厂商会单独给 `*.crt` 和 `*_chain/bundle.crt`，需合并；顺序：服务器证书在前，中间证书在后）。
- `privkey.pem`：私钥（PEM 格式，`-----BEGIN PRIVATE KEY-----` 开头）。

合并示例：

```bash
cat your_domain.crt intermediate.crt > deploy/nginx/certs/fullchain.pem
cp  your_domain.key                    deploy/nginx/certs/privkey.pem
```

### 2.3 方案 C：自签证书（仅内网 / 临时测试）

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout deploy/nginx/certs/privkey.pem \
  -out    deploy/nginx/certs/fullchain.pem \
  -subj "/CN=hub.example.com"
```

> 自签证书**浏览器会报不安全**、**节点端 `wss://` 默认握手失败**。处理办法见 §8.3。生产环境强烈建议用方案 A 或 B。

### 证书文件权限（重要）

```bash
chmod 600 deploy/nginx/certs/privkey.pem
```

---

## 3. 启用 Nginx 的 443 / WSS 配置

编辑 `deploy/nginx/default.conf`，用下面这份**生产就绪配置整体替换全文**（最省心，避免 `upstream`/`map` 重复定义的坑）。记得把两处 `server_name` 改成你的真实域名。

```nginx
upstream mt5_backend {
    server backend:8000;
}

# WebSocket 升级所需：根据 Upgrade 头决定 Connection 头
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

# ---- HTTP 80：放行 ACME 验证 + 其余 301 跳转到 HTTPS ----
server {
    listen 80;
    server_name hub.example.com;            # ← 改成你的域名

    # Let's Encrypt webroot 续期用（standalone 续期可忽略此段）
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# ---- HTTPS 443：TLS 终止 + WSS ----
server {
    listen 443 ssl;
    http2 on;                                # 老版本 nginx(<1.25.1) 改回: listen 443 ssl http2;
    server_name hub.example.com;             # ← 必须与证书域名完全一致

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1h;
    add_header Strict-Transport-Security "max-age=31536000" always;

    client_max_body_size 1m;

    # WebSocket：节点 /ws/node 与后台 /ws/admin —— 必须处理 Upgrade
    location /ws/ {
        proxy_pass http://mt5_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;            # 长连接：避免空闲被断开
        proxy_send_timeout 3600s;
    }

    # HTTP API / webhook / 健康检查 / 文档
    location ~ ^/(api|webhook|health|docs|redoc|openapi.json) {
        proxy_pass http://mt5_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;   # webhook IP 白名单依赖此头
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 前端 SPA
    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }
}
```

> 说明：
> - `/ws/` 段必须保留 `proxy_http_version 1.1` + `Upgrade`/`Connection` 头，否则 WebSocket 握手失败（返回 400/426）。
> - `proxy_read_timeout 3600s` 防止节点长连接因空闲被 Nginx 断开（节点心跳间隔 15s，远小于此值）。
> - `X-Forwarded-For` 必须透传，否则 Webhook 的 IP 白名单（`WHITELISTED_IPS`）会误判来源为 Nginx。

---

## 4. 调整环境变量与端口

### 4.1 Linux（标准 80/443）

`docker-compose.yml`（Linux 版）已固定发布 `80:80` 与 `443:443`，**无需改端口**。确认 `.env` 中以下生产项已设置（参考《使用与部署文档》§7）：

```bash
JWT_SECRET=<足够长的随机串>
ADMIN_PASSWORD=<强密码>
MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD=<强密码>
ENABLE_AUTH=true
AUTH_TOKEN=<webhook 共享 token>
```

### 4.2 Windows（`docker-compose.windows.yml`）

该版本端口可参数化，`.env` 默认 `HTTP_PORT=8080 / HTTPS_PORT=8443`。详见 §10。

---

## 5. 构建前端并启动

```bash
# 1) 构建前端静态资源（Nginx 挂载 frontend/dist 托管）
cd frontend && npm install && npm run build && cd ..

# 2) 启动 / 重建（Linux）
docker compose up -d --build

# 3) 让新的 Nginx 配置与证书生效
docker compose restart nginx

# 4) 确认 nginx 配置语法正确、容器正常
docker compose exec nginx nginx -t
docker compose ps
docker compose logs --tail=50 nginx
```

> 前端无需任何改动：`frontend/src/services/ws.ts` 会根据页面协议自动选择 —— 用 `https://` 打开后台时，后台实时连接自动变成 `wss://<域名>/ws/admin`。

---

## 6. 配置 MT5 节点端使用 WSS

在**每台**运行节点的 Windows 主机上，编辑 `node_client/.env`：

```ini
# 由本地的 ws:// 改为生产的 wss://（域名需与证书一致）
MANAGER_WS_URL=wss://hub.example.com/ws/node
```

然后重启节点（双击 `run_node.bat` 或 `python node_client.py`）。

- 使用 **方案 A/B（受信任 CA）证书** 时：节点端 `websockets` 默认校验即可通过，**无需改任何代码**。
- 使用 **方案 C（自签）证书** 时：节点端握手会因证书不被信任而失败，处理见 §8.3。

> 安全提示：节点 token 不在 URL 上传输（首包 `auth` 走消息体），但仍务必使用 `wss://` 以加密整条信道。

---

## 7. 配置 TradingView Webhook

把 TradingView 警报的 Webhook URL 改为 HTTPS：

```
https://hub.example.com/webhook
```

开启了 `ENABLE_AUTH=true` 时，消息体仍需带 `token` 字段（或 `X-Auth-Token` 头 / `?token=`），与 HTTP 时一致。

---

## 8. 验证 WSS 是否真正生效

### 8.1 验证证书与 HTTPS

```bash
# 证书链与有效期（应看到你的域名、颁发者、起止时间）
echo | openssl s_client -connect hub.example.com:443 -servername hub.example.com 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates

# 健康检查（应返回 {"status":"ok",...}）
curl -sS https://hub.example.com/health
```

### 8.2 验证后台 WSS（浏览器）

1. 用 `https://hub.example.com/` 打开后台并登录。
2. 打开浏览器开发者工具 → Network → 过滤 **WS**。
3. 应看到对 `wss://hub.example.com/ws/admin?token=...` 的请求，状态 **101 Switching Protocols**，且持续收发 `ping/pong`、`snapshot` 等帧。
4. 地址栏为锁形图标、无「不安全 / Mixed Content」警告。

### 8.3 验证节点 WSS

- 最直接：节点端启动后，控制台出现 `authenticated as node ...`，后台「总览」该节点变为**在线**并周期刷新账户。
- 命令行探活（可选，需 `npm i -g wscat`）：

```bash
# 不发首包鉴权，仅验证 wss 握手能建立（随后会被服务端按超时关闭，属正常）
wscat --connect "wss://hub.example.com/ws/node"
```

**自签证书（方案 C）下节点端连不上的处理**（二选一）：

- 推荐：把自签 CA 证书导入运行节点的 Windows 主机「受信任的根证书颁发机构」，Python 即可信任（无需改代码）。
- 临时（**不安全，仅内网测试**）：修改 `node_client/node_client.py` 中 `websockets.connect(...)`，传入关闭校验的 SSL 上下文：

```python
import ssl
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE
# 仅当 URL 以 wss:// 开头时传入 ssl=ssl_ctx
async with websockets.connect(settings.manager_ws_url, ssl=ssl_ctx, ping_interval=20, ping_timeout=20, max_queue=128) as ws:
    ...
```

---

## 9. 证书续期（Let's Encrypt）

Let's Encrypt 证书有效期 90 天，必须自动续期。

### 9.1 简单方式：standalone 续期（会短暂停 Nginx）

新建脚本 `deploy/renew-cert.sh`：

```bash
#!/usr/bin/env bash
set -e
cd /path/to/robin-project-2026

docker compose stop nginx
certbot renew --standalone
cp /etc/letsencrypt/live/hub.example.com/fullchain.pem deploy/nginx/certs/fullchain.pem
cp /etc/letsencrypt/live/hub.example.com/privkey.pem   deploy/nginx/certs/privkey.pem
docker compose start nginx
```

加到 crontab（每天凌晨 3:30 尝试，未到期会自动跳过）：

```bash
30 3 * * * /bin/bash /path/to/robin-project-2026/deploy/renew-cert.sh >> /var/log/mt5-cert-renew.log 2>&1
```

### 9.2 进阶方式：webroot 续期（不停服）

1. 在 `docker-compose.yml` 的 `nginx.volumes` 增加一行挂载（与 §3 配置里的 `/var/www/certbot` 对应）：

```yaml
    volumes:
      - ./deploy/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
      - ./deploy/nginx/certs:/etc/nginx/certs:ro
      - ./frontend/dist:/usr/share/nginx/html:ro
      - ./deploy/nginx/certbot-webroot:/var/www/certbot:ro   # 新增
```

2. 首次签发与续期都用 webroot（无需停 nginx）：

```bash
mkdir -p deploy/nginx/certbot-webroot
certbot certonly --webroot -w $(pwd)/deploy/nginx/certbot-webroot -d hub.example.com \
  --non-interactive --agree-tos -m admin@example.com
# 续期：certbot renew，然后拷贝证书并热加载
docker compose exec nginx nginx -s reload
```

---

## 10. Windows 服务器特别说明（`docker-compose.windows.yml`）

容器内仍是 Linux Nginx，配置（§3）通用。Windows 的差异只在**端口**与**证书签发工具**：

| 事项 | 说明 |
| --- | --- |
| 端口 | 80/443 常被 IIS/HTTP.sys 占用。`.env` 默认 `HTTP_PORT=8080 / HTTPS_PORT=8443`。若想用标准 443，先确保端口空闲（`netstat -ano \| findstr :443`），再设 `HTTP_PORT=80 / HTTPS_PORT=443`。 |
| 非标端口的访问地址 | 若保持 8443，则所有地址都要带端口：后台 `https://hub.example.com:8443/`、节点 `MANAGER_WS_URL=wss://hub.example.com:8443/ws/node`、Webhook `https://hub.example.com:8443/webhook`。同时 §3 的 80→443 跳转在非标端口下不准确，建议直接用带端口的 https 地址访问、或把端口设为标准 443。 |
| 证书签发 | 服务器上 certbot 偏 Linux。Windows 建议用**商业证书**（方案 B），或 `win-acme`(wacs.exe) 签发 Let's Encrypt，产物同样整理成 `fullchain.pem`/`privkey.pem` 放进 `deploy/nginx/certs/`。 |
| 启动命令 | `docker compose -f docker-compose.windows.yml up -d --build`，重载证书 `docker compose -f docker-compose.windows.yml restart nginx`。 |
| 换行符 | `deploy/nginx/default.conf` 建议保存为 **LF**；若 nginx 报配置解析错，转 LF 后重启。 |

---

## 11. 故障排查（FAQ）

| 现象 | 排查方向 |
| --- | --- |
| `nginx -t` 报错 / 容器起不来 | 多半是 `upstream`/`map` 重复定义或证书路径写错；§3 用整体替换可避免；确认 `deploy/nginx/certs/` 下确有两份 pem |
| 浏览器：连接不安全 / 证书无效 | 证书 `server_name` 与访问域名不一致；或用了自签（§2.3）；或只放了服务器证书没拼中间证书（方案 B 需 fullchain） |
| 浏览器：Mixed Content（混合内容） | 仍用 `http://` 打开了后台。务必用 `https://`，前端才会自动走 `wss`；§3 已配 80→443 跳转 |
| WS 握手 400 / 426 | `/ws/` 段缺少 `proxy_http_version 1.1` 或 `Upgrade`/`Connection` 头（§3 已含） |
| 节点端 `wss` 连不上、报 SSL 错误 | 多为自签证书不被信任，见 §8.3；或域名解析/防火墙 443 未放行 |
| 节点频繁断线重连 | Nginx `proxy_read_timeout` 太短；§3 设为 3600s |
| Webhook 返回 403 | IP 白名单开启但未透传 `X-Forwarded-For`（§3 已透传）；确认来源 IP 在 `WHITELISTED_IPS` |
| 证书到期后全站红锁 | 续期未生效；检查 §9 的 cron 日志，手动跑一次续期脚本并 `restart nginx` |

查看日志：`docker compose logs -f nginx`、`docker compose logs -f backend`、节点端控制台。

---

## 12. 上线安全清单

- [ ] 使用受信任 CA 证书（方案 A/B），而非自签。
- [ ] `JWT_SECRET`、`ADMIN_PASSWORD`、数据库密码均为强随机值。
- [ ] `ENABLE_AUTH=true` 且设置了 `AUTH_TOKEN`；按需开启 IP 白名单。
- [ ] 全站强制 HTTPS（80→443 跳转）、已加 `Strict-Transport-Security`。
- [ ] 所有节点端 `MANAGER_WS_URL` 已改为 `wss://`，TradingView Webhook 已改为 `https://`。
- [ ] `privkey.pem` 权限 600；证书私钥不入库（`.gitignore` 已忽略 `deploy/nginx/certs/*.pem|*.key|*.crt`）。
- [ ] 已配置证书自动续期，并验证过续期脚本可正常执行。
- [ ] 浏览器 Network 中确认 `/ws/admin` 为 `wss` + 101；节点在后台显示在线。
