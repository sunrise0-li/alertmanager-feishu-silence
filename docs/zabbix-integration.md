# Zabbix 告警接入指南（Zabbix → Alertmanager → 飞书）

把 Zabbix 告警汇入 Alertmanager，与 Prometheus 告警走同一套飞书卡片 / 一键静默 / Loki 归档链路，并且支持按 `source` 标签分群推送、按 `site` 标签做跨主机告警抑制。

## 方案定位

Zabbix 原生媒介只能直推一种 IM，且没有分组抑制、静默管理能力。经 Alertmanager 中转后：

- 复用本项目的交互卡片与「一键静默」（静默直接创建到 Alertmanager）；
- 复用 Loki 告警归档（Grafana 看板统一展示 Zabbix + Prometheus 告警）；
- 获得 `inhibit_rules` 抑制能力：如同一条专线两台主机，专线流量 P0 触发时，ICMP 延迟 P1 不再推送；
- 按 `source: zabbix` 标签路由到独立的中转实例，推送到不同的飞书群，与 Prometheus 告警分群。

## 架构

```
                (1) Webhook 媒介 JS
Zabbix 动作 ──────────────────────► Alertmanager /api/v2/alerts
                                        │
                                        │ route: source=zabbix（子路由首条）
                    ┌───────────────────┴───────────────────┐
                    ▼                                       ▼
          am_silence_proxy (:8428)             am_silence_proxy_zabbix (:8429)
          Prometheus 告警 → 飞书群A             Zabbix 告警 → 飞书群B
                    │                                       │
                    └──────────────┬────────────────────────┘
                                   ▼ (每条告警同步写 Loki)
                          Loki (:3100) → Grafana 告警看板

  抑制：inhibit_rules
    同 instance 内 critical 压 warning          （Prometheus 原行为）
    同 site 内 critical 压 warning|info         （跨主机专线场景，site 由 JS 规则表写入）
```

## 目录内容

```
examples/zabbix/
├── zabbix_alertmanager.js           # Zabbix Webhook 媒介类型的 JavaScript 脚本
├── alertmanager.yml                 # 融合版 Alertmanager 配置（Zabbix 路由 + site 抑制）
├── am_silence_proxy_zabbix.py       # 第二实例（:8429，推独立飞书群）
└── am-silence-proxy-zabbix.service  # 第二实例 systemd 单元文件
examples/grafana-alert-center.json   # Grafana 告警中心看板（可选）
```

---

## 步骤 1：Zabbix Webhook 媒介类型

**告警媒介类型**（Alerts → Media types → Create media type），Type 选 **Webhook**：

### 1.1 参数（Parameters）

| 参数名 | 值 | 说明 |
| --- | --- | --- |
| `URL` | `http://<alertmanager>:9093/api/v2/alerts` | Alertmanager API v2 地址 |
| `Level` | `{EVENT.SEVERITY}` | Zabbix 事件级别（disaster/high/average/warning/information/not classified） |
| `Name` | `{EVENT.NAME}` | 触发器名称 → 卡片标题 / `alertname` |
| `Host` | `{HOST.NAME}` | 主机名称 → 卡片对象行 / `serviceName` |
| `Target` | `{IPADDRESS}` | 主机 IP → `instance` 标签 |
| `Status` | `{EVENT.STATUS}` | `PROBLEM` / `RESOLVED` |
| `Detail` | `{EVENT.OPDATA}` | 当前运维数据 → 卡片详情 |
| `APIURL` | `http://<zabbix>/api_jsonrpc.php` | （可选）Zabbix API 地址，用于反查主机群组作为 `site` |
| `APITOKEN` | Zabbix API Token | （可选）用户设置 → API 令牌 中创建。**不要提交到仓库** |
| `HostID` | `{HOST.ID}` | （可选）主机 ID，配合上面两项查询该主机所属群组 |

> ⚠️ `Detail` 不要用 `{EVENT.RECOVERY.MESSAGE}`——该宏**只在恢复操作的消息正文里解析**，放进媒介参数永远是字面量。
>
> 💡 `APIURL / APITOKEN / HostID` 三项都填了，脚本会自动调 Zabbix API 取主机所属群组（取最长的组名，如 `南阳万达/3楼`）作为 `site` 标签——按楼层分组的主机无需再维护 site_rules；三项缺任一或 API 失败则回退 site_rules 规则表，再兜底主机名/IP。

### 1.2 消息模板（Message templates）

**必须添加**，否则媒介不执行（动作日志报 `No message defined for media type.`，手动测试弹窗报 `foreach() argument must be of type array|object, null given`）：

- **问题（Problem）**：内容随意但**不能为空**，如 `Zabbix problem alert`
- **问题恢复（Problem recovery）**：同上，如 `Zabbix recovery alert`

### 1.3 脚本（Script）

粘贴 `examples/zabbix/zabbix_alertmanager.js` 全文：

```javascript
// 完整脚本见仓库文件 examples/zabbix/zabbix_alertmanager.js（与此处保持同步，以文件为准）。
// 粘贴时使用该文件原文，避免从聊天窗口复制带入全角字符。
```

**脚本要点：**

| 设计 | 说明 |
| --- | --- |
| `severity_map` | Zabbix 级别映射为 AM 级别：disaster/high → `critical`，average/warning → `warning`，其余 → `info` |
| `site_rules` | **站点标签三级取值**：① 配了 `APIURL/APITOKEN/HostID` 时调 Zabbix API 反查主机群组（取最长组名，如 `南阳万达/3楼`），按楼层分组的主机零维护；② API 未配或失败时用 site_rules 规则表按关键词提取（仅在 site 仍是裸主机名/IP 时生效）；③ 兜底主机名或 IP，标签永不为空。标签供 `inhibit_rules` 的 `equal: ['site']` 及站点级静默使用 |
| 自动标注带宽方向 | 详情形如「X Mbps, Y Mbps」两个值时，脚本自动改写为「IN: X Mbps, OUT: Y Mbps」——无需逐个修改触发器的操作数据字段；ICMP/CPU 等其它格式原样透传。前提：触发器表达式按 先 IN 后 OUT 的顺序写（Zabbix 惯例） |
| `endsAt` 问题 = now+2h10m | Zabbix 动作配置了每 2h 重发未恢复问题（升级步骤），`endsAt` 取 2h10m（略长于重发间隔）保证重发之间**无缝续期**——AM/Karma 里故障期间持续可见，`inhibit_rules` 的 `equal: ['site']` 也全程生效。恢复通知到达时 `endsAt=now` 立即 resolve，不受影响；恢复通知丢失时最长残留 2h10m 后自动清理 |
| `endsAt` 恢复 = now | 与问题告警按标签匹配，立即 resolve |
| 标签对齐 | `alertname/severity/instance/serviceName` 与本服务消费的标签完全对齐；`annotations.title` 作为卡片行标题 |

> ⚠️ **粘贴脚本务必使用本文件原文**。从聊天窗口复制容易带入全角引号、不间断空格等不可见字符，Zabbix（Duktape ES5.1）会报 `SyntaxError: invalid token`。中文只允许出现在 `site_rules` 的字符串值中。

### 1.4 挂载媒介

- 用户（Administration → Users → 报警媒介）添加该媒介类型，**动作里「发送给用户」引用的用户必须已挂媒介**，否则动作日志报 `No media defined for user.`；
- 告警动作（Actions）里配置触发条件与操作步骤，「发送到媒介类型」选择该 Webhook。

---

## 步骤 2：Alertmanager 配置

在现有 `alertmanager.yml` 基础上融合（完整参考 `examples/zabbix/alertmanager.yml`）：

```yaml
route:
  receiver: 'feishu-card'
  group_wait: 20s
  group_interval: 2m
  repeat_interval: 2h
  group_by: ['alertname','app','instance','site']
  routes:
  # Zabbix 告警 → 群B 中转实例（:8429，推到新群机器人）
  # ⚠️ 必须放在其他子路由之前，先匹配先生效
  # group_wait 单独调小：Zabbix 告警几乎各自独立成组，合并价值低，快推优先
  - receiver: 'feishu-zabbix'
    match:
      source: zabbix
    group_wait: 5s
  # 其余告警（Prometheus 等）→ 群A 原有链路
  - receiver: 'feishu-card'
    group_wait: 20s
    repeat_interval: 1h
    match_re:
      severity: critical|warning|High|fatal|low

receivers:
- name: 'feishu-card'
  webhook_configs:
  - url: 'http://192.168.99.23:8428/alert'
    send_resolved: true
- name: 'feishu-zabbix'
  webhook_configs:
  - url: 'http://192.168.99.23:8429/alert'
    send_resolved: true

inhibit_rules:
# 原有规则：同 instance（主机维度）内 critical 压 warning，Prometheus 告警沿用
- source_match:
    severity: 'critical'
  target_match:
    severity: 'warning'
  equal: ['instance']
# 新增规则：同 site（站点/专线维度，跨主机）内 critical 压 warning 和 info
# site 标签由 Zabbix webhook JS 按站点规则表写入，如 site="南阳4F-haier专线"
- source_match:
    severity: 'critical'
  target_match_re:
    severity: 'warning|info'
  equal: ['site']
```

**要点：**

| 设计 | 说明 |
| --- | --- |
| 子路由顺序 | `match: {source: zabbix}` 放在 `match_re` 子路由**之前**，Zabbix 告警优先命中分群路由 |
| 首次通知延迟 ≈ `group_wait` | AM 收到告警后会等 `group_wait` 再发第一次通知（合并同组告警的设计）。全局 20s 对 Zabbix 场景太长，子路由单独设 `group_wait: 5s`；追求极致可设 `0s`，代价是同时到达的多条告警不再合并成一卡 |
| `equal: ['instance']` 与 `equal: ['site']` 并存 | 两条规则是 OR 关系：主机维度抑制沿用原行为，站点维度覆盖跨主机场景（同一专线两台主机 `instance` 不同但 `site` 相同） |
| 旧语法 `match`/`source_match` | Alertmanager 0.22+ 标记 deprecated 但完全兼容；如需新语法可改写为 `matchers: ['source="zabbix"']` |
| `send_resolved: true` | 恢复通知统一由 Alertmanager 下发，走本服务渲染绿色恢复卡片 |

改完执行 `amtool check-config alertmanager.yml` 校验，然后 `systemctl restart alertmanager`（或 `curl -X POST http://<am>:9093/-/reload`）。

---

## 步骤 3：第二中转实例（独立飞书群）

Zabbix 告警要推到独立飞书群时，复制主服务为第二实例（同一份代码，改配置区 4 行）：

```bash
cp examples/zabbix/am_silence_proxy_zabbix.py /root/am_silence_proxy_zabbix.py
```

编辑 `/root/am_silence_proxy_zabbix.py` 配置区：

| 变量 | 值 |
| --- | --- |
| `LISTEN_PORT` | `8429`（与主实例 8428 区分） |
| `FEISHU_WEBHOOK` | 新群的机器人 webhook |
| `FEISHU_SECRET` | 新群机器人开启签名校验才填 |
| `SELF_BASE_URL` | `http://<本机>:8429`（卡片静默下拉指回本实例） |

部署 systemd：

```bash
cp examples/zabbix/am-silence-proxy-zabbix.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now am-silence-proxy-zabbix
systemctl status am-silence-proxy-zabbix
```

> 两个实例的 `LOKI_URL` 都指向同一 Loki 时，Zabbix 与 Prometheus 告警在 Grafana 看板中统一归档。

---

## 步骤 4：验证

**① curl 直测 Alertmanager**（模拟一条 Zabbix 告警，走完整路由）：

```bash
curl -X POST http://<alertmanager>:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{
    "labels": {
      "alertname": "接入测试-触发",
      "severity": "critical",
      "instance": "10.0.0.1",
      "serviceName": "测试主机",
      "site": "接入测试站点",
      "source": "zabbix"
    },
    "annotations": {"title": "接入测试-触发", "description": "curl 注入测试"},
    "startsAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'",
    "endsAt": "'$(date -u -d '+30 minute' +%Y-%m-%dT%H:%M:%SZ)'"
  }]'
```

预期：Alertmanager UI Alerts 列表出现该告警（带 `source=zabbix`）→ 约 `group_wait` 后飞书群B 收到红色卡片 → 正常情况下 Zabbix 恢复通知到达后立即收到绿色恢复卡片；若恢复通知丢失，最长 2h10m 后告警自动 resolve。

**② Zabbix 侧测试**：媒介类型页「测试」按字面传参（宏不展开），Status 手填 `PROBLEM`/`RESOLVED` 各测一次；最后用真实触发器验证端到端。

**③ 静默联动**：在群B 的告警卡片下拉选「静默 2 小时」，回执为橙色，且 Alertmanager UI Silences 中出现对应静默——证明第二实例的静默回链配置正确。

**④ 抑制验证**：同 `site` 下先注入一条 `critical`，再注入 `warning`；后者在 Alertmanager UI 中显示 Suppressed，飞书不推。

---

## 楼层/站点级静默（静默整个楼层或站点）

告警同时携带两个标签（主机群组产生时）：

- `site` = **楼层**：完整群组名，如 `成都/中汇/11楼`（同时用于 `inhibit_rules` 的 `equal: ['site']`）
- `station` = **站点**：群组去掉最后一级，如 `成都/中汇`；群组名不含 `/` 时为空

飞书卡片底部有**两个 ⋮ 按钮**：第一个是普通单条静默（2 小时/1 天/2 天/1 周）；第二个是楼层/站点静默，共 4 个选项：

- 🔕 静默本楼层 2 小时 / 1 天（filter 只带 `site="成都/中汇/11楼"`，拦该楼层全部告警）
- 🔕 静默本站点 2 小时 / 1 天（filter 只带 `station="成都/中汇"`，拦该站点全部告警）

适合整层楼断电（用楼层）或整站点专线检修（用站点）。两个选项组在 `am_silence_proxy*.py` 的 `FLOOR_SILENCE_OPTIONS` / `STATION_SILENCE_OPTIONS`，按需增删时长。

> 注意：站点级静默与 `inhibit_rules` 相互独立——静默是「时间段内不通知」，抑制是「critical 活跃时压低级别」；静默期间告警恢复不发恢复通知、重新触发同样被拦，到期时仍活跃的告警按 `repeat_interval` 补推。

## 实战坑位清单

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 动作不执行：`No message defined for media type.` | Webhook 媒介没配消息模板 | 添加「问题」+「问题恢复」两条模板，内容非空 |
| 测试媒介弹窗 `foreach() argument must be of type array\|object, null given` | 同上（测试流程也读取模板） | 同上 |
| 动作日志 `No media defined for user.` | 动作引用的用户未挂载该媒介 | 用户 → 报警媒介中添加 |
| 保存/执行脚本 `SyntaxError: invalid token` | 复制粘贴引入全角字符/不间断空格 | 使用 `examples/zabbix/zabbix_alertmanager.js` 原文件；中文仅允许出现在规则表字符串值中 |
| 恢复消息里的宏传到脚本为字面量 | `{EVENT.RECOVERY.MESSAGE}` 只在恢复操作消息正文解析 | 媒介参数 `Detail` 用 `{EVENT.OPDATA}` |
| 告警已恢复但 AM 里常驻不消失 | Zabbix 恢复通知丢失，AM 一直等 resolve | JS 已内置 2h10m 兜底自动 resolve；正常恢复仍实时清理 |
| 静默了 P0，同站点的 P1 却还在推 | 一键静默是精确匹配，只静默那一条告警本身；联动压制靠 `inhibit_rules`，而它要求 P0 在 AM 里处于活跃状态——若 `endsAt` 窗口太短，P0 早已过期消失，抑制无从谈起 | JS 已把问题告警 `endsAt` 拉长到 2h10m（配合动作每 2h 重发），故障期间 P0 一直活跃，同站点 warning/info 自动 Suppressed；另注意静默操作与抑制机制相互独立，静默不会让告警在 AM 里"复活" |
| P0 触发超过 30 分钟后同站点 P1 恢复推送 | Zabbix 无重复通知机制时，P0 的活跃窗口就是那条 `endsAt` | 已通过 Zabbix 动作升级步骤（每 2h 重发）+ JS `endsAt=130*60*1000` 解决；调整重发间隔时需同步按比例调大该值 |
| P0 触发后同站点 P1 仍推送 | `site` 标签未命中（检查 JS `site_rules` 是否覆盖该主机名/触发器名关键词） | 补规则行，站点关键词取触发器名称或主机名的稳定片段 |

---

## Grafana 告警看板（可选）

两个版本任选：

- `examples/grafana-alert-center.json`（基础版，8 面板）：24h 触发/恢复统计、级别分布、趋势、TOP 告警、实时日志流，纯 Loki 数据源。
- `examples/grafana-alert-center-v3.json`（推荐，13 面板）：在基础版之上增加 **平均恢复时长 MTTR / 最长恢复耗时 / 期末未恢复条数 / 最长未恢复已持续** 四个指标卡（dthms 时长单位）、TOP 表格化展示、**表格化实时日志**。v3 依赖 proxy 的 `/report` 与 `/logs` 接口：

| 接口 | 说明 |
| --- | --- |
| `GET /report?from=&to=` | 统计汇总（epoch 毫秒参数）；`?list=top_names\|top_hosts\|top_sites` 返回扁平 TOP 数组 |
| `GET /logs?from=&to=&limit=300` | 结构化告警日志（解析 Loki JSON 行），按时间倒序，时间带年份 |

导入后将面板中 proxy 地址（默认 `http://192.168.99.23:8428`）替换为实际地址即可。

---

## 告警周报 / 月报（alert_report.py）

定时把上一周期的告警汇总推送到飞书群：触发/恢复/critical 次数、平均恢复时长（MTTR）、最长恢复耗时、期末未恢复（含最长持续）、按告警名称/楼层/站点/主机 TOP5。

### 部署步骤

1. 脚本在 `examples/alert_report.py`，放到任意有 Python 3.8+ 的机器（与 proxy 同机即可），仅依赖标准库（urllib/json），无需 pip 安装；
2. 修改脚本头部两个配置：

```python
LOKI_URL = "http://192.168.99.23:3100"          # Loki 地址（查询端点，不是 push 端点）
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"  # 接收报表的飞书群机器人
```

3. 手动先跑一次验证：

```bash
python3 alert_report.py weekly    # 上一自然周（周一 00:00 ~ 周日 24:00）
python3 alert_report.py monthly   # 上一自然月
```

飞书群收到蓝色卡片即成功。

4. 配置 crontab 定时推送（周一早上发周报，每月 1 号发月报）：

```cron
0 9 * * 1  cd /opt/alert-report && /usr/bin/python3 alert_report.py weekly  >> /var/log/alert_report.log 2>&1
0 9 1 * *  cd /opt/alert-report && /usr/bin/python3 alert_report.py monthly >> /var/log/alert_report.log 2>&1
```

### 说明

- 统计数据来源与看板 v3 相同（Loki 中 `job="alertmanager"` 的 JSON 日志行），触发/恢复按 (告警名, 对象, 主机) 配对估算恢复时长，2h 升级重发只计首次触发；
- 周报/月报统计的是**上一完整自然周期**，与看板的时间范围选择互不影响；
- 想改推送时间或推送目标群，改 cron 时间和 `FEISHU_WEBHOOK` 即可。
