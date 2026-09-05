# 🐦 PteroPulse

**PteroPulse** —— Pterodactyl 面板多服务器状态监控 · 单文件 Python,零第三方依赖。
支持 Render(Docker / Blueprint)一键部署,也支持本地运行或任意 Docker 环境。

![runtime](https://img.shields.io/badge/runtime-Python%203.13%20slim-blue) ![deps](https://img.shields.io/badge/dependencies-none-success)

## 功能

- **自动发现服务器**:只需配置 Pterodactyl Client API Key,自动列出该 Key 能访问的所有服务器
- **后台轮询**:每 30 秒(可配)拉取一次所有服务器的 CPU / 内存 / 磁盘 / 网络状态并打印日志,掉线自动 ALERT
- **网页仪表盘**(`/`):每个服务器一张卡片,展示 **服务器类型**(从 docker 镜像解析,如 `java_21`)、**内存**、**磁盘**、**网络流量**,10 秒自动刷新
- **状态 API**(`/api/status`):JSON 快照,含缓存回退(面板偶发失败时展示最近一次成功数据)
- **管理 API**:凭 `ADMIN_TOKEN` 在线增删 Client API Key,自动回写环境变量(Render 上重启不丢)
- **单文件**:核心逻辑全部在 `monitor.py`,标准库实现,无任何 pip 依赖

> v2.0 起不再包含免费实例自 ping 保活逻辑。如需保活,可参考 README 末尾「保活方案」。

## 目录结构

```
pteropulse/
├── monitor.py        # 全部核心逻辑(监控循环 + Web 服务 + 仪表盘)
├── Dockerfile        # Docker 部署(python:3.13-slim)
├── render.yaml       # Render Blueprint 一键部署配置
├── requirements.txt  # 空(标准库实现,无依赖)
├── .gitignore
└── README.md
```

## 快速开始

### 方式一:Render Blueprint(推荐)

1. 把本仓库 Fork / 推送到你的 GitHub
2. Render Dashboard → **New** → **Blueprint**,选择该仓库
3. 在提示时填入 `PTERO_API_KEYS`,每个 key 后面用 `@` 标明所属面板,如 `ptlc_xxx@https://panel.example.com`,多个用逗号分隔
4. 部署完成后打开服务 URL 即可看到仪表盘

> 面板地址跟随 key 走:每个 key 用 `key@https://面板地址` 格式指定所属面板(支持多个不同面板混管);也可以统一设置环境变量 `PTERO_PANEL` 作为不带 `@` 的 key 的默认面板。两者都没有时启动会直接报错退出。

### 方式二:Docker

```bash
docker build -t pteropulse .
docker run -d -p 10000:10000 \
  -e PTERO_API_KEYS=ptlc_xxx@https://panel.example.com,ptlc_yyy@https://other-panel.com \
  pteropulse
```

### 方式三:本地运行

```bash
# 常驻模式(起 Web 仪表盘)
PTERO_API_KEYS=ptlc_xxx@https://panel.example.com python monitor.py

# 单次检查所有服务器状态并退出(适合脚本 / cron)
PTERO_API_KEYS=ptlc_xxx@https://panel.example.com python monitor.py --once
```

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `PTERO_API_KEYS` | ✅ | — | Client API Key,格式 `key@https://面板地址`,多个用逗号分隔 |
| `PANEL` / `PTERO_PANEL` | — | — | 默认面板地址(可选);不带 `@` 的 key 会使用它 |
| `PORT` | — | `10000` | HTTP 监听端口 |
| `MONITOR_INTERVAL` | — | `30` | 后台轮询间隔(秒) |
| `ADMIN_TOKEN` | — | — | 管理 API 鉴权 token;不设则管理接口禁用 |
| `RENDER_API_KEY` | — | — | 配合 `SERVICE_ID`,Key 增删后回写环境变量 |
| `SERVICE_ID` | — | — | Render 服务 ID(`srv-` 开头) |

## API

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|--- |
| GET | `/` `/page` | 无 | 网页仪表盘 |
| GET | `/api/status` | 无 | 所有服务器状态 JSON(类型/内存/磁盘/网络) |
| GET | `/health` | 无 | 健康检查 |
| GET | `/api/keys` | ADMIN_TOKEN | 查看已配置的 API Key 列表 |
| POST | `/api/keys` | ADMIN_TOKEN | 添加 Key(自动校验、发现服务器、持久化) |
| DELETE | `/api/keys/<key>` | ADMIN_TOKEN | 删除 Key(自动持久化) |

## 工作原理

```
启动
 ├─ discover_servers()  对每个 Client Key 调 /api/client,自动发现服务器
 ├─ 后台线程 monitor_loop   每 30s 拉取 /servers/{id}/resources,打印日志并缓存
 └─ HTTP 服务 (ThreadingHTTPServer)
     ├─ GET /           返回内置仪表盘(10s 自动刷新,fetch /api/status)
     ├─ GET /api/status 实时拉取 + 失败回退缓存,返回类型/内存/磁盘/网络
     ├─ GET/POST/DELETE /api/keys  管理接口(ADMIN_TOKEN 鉴权)
     └─ GET /health     健康检查
```

## 保活方案(可选)

v2.0 已移除自 ping 保活。如果部署在 Render 免费实例且需要常驻:

1. **外部拨测**(推荐):用 [UptimeRobot](https://uptimerobot.com) / [cron-job.org](https://cron-job.org) 每 5 分钟 GET 一次服务 URL,效果等同于旧版自 ping
2. **Render 付费实例**:Starter 起不休眠,无需保活
3. **旧版自 ping**:若需要,可在环境变量 `SELF_URL` 设置自身地址后,自行加回 `keepalive_loop` 相关代码(git 历史中保留)

## License

MIT
