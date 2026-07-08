# Alertmanager 飞书告警静默中转服务

一个轻量的 Python Flask 中转服务，为 **Alertmanager + 飞书群机器人** 的告警链路补齐两项原生做不到的能力：

1. **一键静默任意时长**——飞书卡片里直接点「静默 2 小时 / 1 天 / 2 天 / 1 周」，服务后台调用 Alertmanager API 创建静默，无需进 Alertmanager UI 手动填 Duration。
2. **告警日志归档与着色**——把每条告警写入 Loki，并按级别打 `level` 标签，Grafana Logs 面板自动按红/黄/绿着色。

> 为什么需要中转服务？Alertmanager 自带的静默页面（`Parsing.elm`）只接受 `filter` 和 `comment` 两个 URL 参数，**无法通过链接预填 Duration**。PrometheusAlert 的飞书通道卡片骨架又是硬编码的，无法插入下拉/按钮组件。因此静默下拉方案必须绕开二者，由本服务用 Alertmanager 直连 webhook 实现。

## 功能特性

- 🔕 **一键静默**：支持 `s / m / h / d / w` 任意时长，点击即建静默。
- 📇 **飞书交互卡片**：Alertmanager webhook 触发后推送卡片，内置 `overflow` 折叠菜单实现「下拉选时长 + URL 跳转」（飞书自定义机器人唯一可行的下拉方案）。
- 🎨 **颜色区分**：触发=红、恢复=绿；静默回执=橙。
- 🗂 **多告警合并**：一次 webhook 内的多条告警合并进同一张卡片。
- 🕗 **东八区时间**：所有时间戳转为 CST 显示。
- 📊 **Loki 日志**：低基数标签（`job/alertname/severity/status/level`）+ 完整 JSON 明细正文，配合 Grafana Logs 面板按级别着色。

## 架构

```
                     (1) 告警                (2) webhook POST /alert
Prometheus ─────► Alertmanager ─────────────────────────────► am_silence_proxy (:8428)
                       ▲                                          │  │
                       │ (4) POST /api/v2/silences                │  │ (3) 推送交互卡片
                       │     创建静默                              │  ▼
                       └──────────────────────────────────────┐  │ 飞书群机器人
                     (5) 用户点卡片下拉 GET /silence?duration= ─┘  │
                                                                  ▼ (6) push
                                                             Loki (:3100) ──► Grafana Logs 面板
```

## 目录结构

```
alertmanager-feishu-silence/
├── am_silence_proxy.py          # 主服务（Flask）
├── requirements.txt             # Python 依赖
├── README.md
├── LICENSE
├── .gitignore
└── deploy/
    ├── am-silence-proxy.service # systemd 单元文件
    ├── loki-docker-compose.yml  # Loki 部署（可选）
    └── loki-config.yml          # Loki 配置（可选）
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 修改配置

编辑 `am_silence_proxy.py` 顶部「配置区」，至少改这些：

| 变量 | 说明 |
| --- | --- |
| `ALERTMANAGER_URL` | Alertmanager 地址，如 `http://192.168.99.20:9093` |
| `FEISHU_WEBHOOK` | 飞书群机器人 webhook（**必填真实值**） |
| `FEISHU_SECRET` | 机器人签名密钥，未开启签名校验则留空 `""` |
| `SELF_BASE_URL` | 本服务对外可访问地址，卡片下拉链接会指回这里 |
| `DASHBOARD_URL` / `HISTORY_URL` | 卡片上的跳转按钮，按需修改 |
| `SILENCE_OPTIONS` | 静默下拉可选时长列表 |
| `LOKI_URL` | Loki push 地址；留空 `""` 则不推送日志 |

> ⚠️ 飞书自定义机器人若开启「自定义关键词」校验，请确保关键词包含卡片标题里的字样（本项目卡片标题均含「告警」二字）。

### 3. 启动

```bash
python am_silence_proxy.py
# 或用 systemd（见下）
```

### 4. 配置 Alertmanager webhook

在 `alertmanager.yml` 里把接收器指向本服务：

```yaml
receivers:
  - name: feishu-proxy
    webhook_configs:
      - url: http://192.168.99.23:8428/alert
        send_resolved: true

route:
  receiver: 'feishu-card'        # 默认走这个
  group_by: ['alertname', 'instance']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  # 如需部分告警仍走 PrometheusAlert，可加 routes 子路由分流
```

## systemd 部署

```bash
cp am_silence_proxy.py /root/am_silence_proxy.py
cp deploy/am-silence-proxy.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now am-silence-proxy
systemctl status am-silence-proxy
```

## 测试告警
1. 触发告警卡片 → 应为红色
```bash
curl -X POST http://192.168.99.23:8428/alert \
  -H 'Content-Type: application/json' \
  -d '{
    "alerts": [{
      "status": "firing",
      "labels": {"alertname":"颜色测试-触发","severity":"warning","instance":"1.2.3.4:9100","serviceName":"测试主机"},
      "annotations": {"title":"红色卡片测试","template":"这是触发告警，应为红色"},
      "startsAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"
    }]
  }'
```
<img width="609" height="235" alt="image" src="https://github.com/user-attachments/assets/3158b079-9dff-4b35-8fad-88966aa654c9" />

2. 恢复告警卡片 → 应为绿色
```bash
curl -X POST http://192.168.99.23:8428/alert \
  -H 'Content-Type: application/json' \
  -d '{
    "alerts": [{
      "status": "resolved",
      "labels": {"alertname":"颜色测试-恢复","severity":"warning","instance":"1.2.3.4:9100","serviceName":"测试主机"},
      "annotations": {"title":"绿色卡片测试","template":"这是恢复告警，应为绿色"},
      "startsAt": "'$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)'",
      "endsAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"
    }]
  }'
```
<img width="607" height="279" alt="image" src="https://github.com/user-attachments/assets/88e7a348-d3bb-46b9-9def-ea22b7901881" />

3. 静默成功回执卡片 → 应为橙色
```bash
curl -X POST http://192.168.99.23:8428/alert \
  -H 'Content-Type: application/json' \
  -d '{
    "alerts": [{
      "status": "resolved",
      "labels": {"alertname":"颜色测试-恢复","severity":"warning","instance":"1.2.3.4:9100","serviceName":"测试主机"},
      "annotations": {"title":"绿色卡片测试","template":"这是恢复告警，应为绿色"},
      "startsAt": "'$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)'",
      "endsAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"
    }]
  }'
```
<img width="606" height="319" alt="image" src="https://github.com/user-attachments/assets/c5c62163-534f-472a-911a-b6e71090b62d" />


> **代理环境提示**：若手动 `python` 启动正常、`systemctl` 启动却连不上飞书，通常是 systemd 不继承 shell 的代理变量。在 service 文件中补充：
> ```ini
> Environment=HTTP_PROXY=http://<proxy>:<port>
> Environment=HTTPS_PROXY=http://<proxy>:<port>
> Environment=NO_PROXY=localhost,127.0.0.1,192.168.0.0/16
> ```
> `NO_PROXY` 必须包含内网网段（如 `192.168.0.0/16`），否则访问 Alertmanager/Loki 会走代理失败。

## Loki + Grafana（可选）

部署 Loki：

```bash
cd deploy
docker compose -f loki-docker-compose.yml up -d
```

在 Grafana 添加 Loki 数据源（`http://<host>:3100`），新建 **Logs** 面板，查询示例：

```logql
{job="alertmanager"}
```

日志左侧竖条会按 `level` 标签自动着色：

| 告警状态 / 级别 | `level` 值 | 颜色 |
| --- | --- | --- |
| resolved（恢复） | `info` | 绿 |
| critical / error / fatal | `critical` | 红 |
| warning | `warning` | 黄 |
| 其它 | `error` | 红 |

> 改动前写入的旧日志没有 `level` 标签，只有新告警才带颜色。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/silence?duration=2h&filter=<matchers>&comment=<备注>` | 创建静默，返回 HTML 成功页并推送橙色回执卡片 |
| POST | `/alert` | 接收 Alertmanager webhook，渲染并推送飞书交互卡片 |
| GET | `/healthz` | 健康检查，返回 `ok` |

`filter` 为 Alertmanager matchers 串，形如：
```
alertname="磁盘使用率",device="/dev/vda2"
```
<img width="1560" height="747" alt="image" src="https://github.com/user-attachments/assets/4f254bbb-d206-458e-8ff6-844cc199ecd1" />


## 已知设计约束

- 飞书自定义机器人只能用 `overflow` 折叠菜单实现「下拉 + URL 跳转」；`select_static` 需要回调服务器，机器人场景不可用。
- 静默期间告警恢复**不会**发恢复通知（Alertmanager 层面已抑制该静默下的所有通知）。

## License

[MIT](LICENSE)
