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

> ⚠️ `Detail` 不要用 `{EVENT.RECOVERY.MESSAGE}`——该宏**只在恢复操作的消息正文里解析**，放进媒介参数永远是字面量。

### 1.2 消息模板（Message templates）

**必须添加**，否则媒介不执行（动作日志报 `No message defined for media type.`，手动测试弹窗报 `foreach() argument must be of type array|object, null given`）：

- **问题（Problem）**：内容随意但**不能为空**，如 `Zabbix problem alert`
- **问题恢复（Problem recovery）**：同上，如 `Zabbix recovery alert`

### 1.3 脚本（Script）

粘贴 `examples/zabbix/zabbix_alertmanager.js` 全文：

```javascript
var params = JSON.parse(value),
    req = new HttpRequest(),
    severity_map = {
        disaster: 'critical',
        high: 'critical',
        average: 'warning',
        warning: 'warning',
        information: 'info',
        'not classified': 'info'
    },
    severity = severity_map[params.Level.toLowerCase()] || 'info',
    site_rules = [
        { match: '南阳4F-haier', site: '南阳4F-haier专线' }
    ],
    site = params.Host || params.Target,
    i,
    alert;

// Site label: used by Alertmanager inhibit_rules (equal: ['site']).
// Add one rule per site/link; fallback is host or IP so the label is never empty.
for (i = 0; i < site_rules.length; i++) {
    if ((params.Name || '').indexOf(site_rules[i].match) !== -1 ||
        (params.Host || '').indexOf(site_rules[i].match) !== -1) {
        site = site_rules[i].site;
        break;
    }
}

var now = new Date();
// Problem: keep the alert alive for 4min (< resolve_timeout 5m) so Alertmanager
// auto-resolves it if the Zabbix recovery notification is ever lost.
// Recovery: endsAt=now marks the matching alert resolved immediately.
var startsAt = params.Status === 'RESOLVED' ? new Date(now.getTime() - 1000).toISOString() : now.toISOString();
var endsAt = params.Status === 'RESOLVED' ? now.toISOString() : new Date(now.getTime() + 4 * 60 * 1000).toISOString();

// Label/annotation keys aligned with am_silence_proxy.py:
//   labels:      alertname / severity / instance / serviceName (object line)
//   annotations: title (card line title, fallback alertname) / template|description (detail)
alert = {
    labels: {
        alertname: params.Name,
        severity: severity,
        instance: params.Target,
        serviceName: params.Host,
        site: site,
        source: 'zabbix'
    },
    annotations: {
        title: params.Name,
        description: params.Detail || params.Subject,
        zabbix_status: params.Status
    },
    startsAt: startsAt,
    endsAt: endsAt
};

req.addHeader('Content-Type: application/json');
var resp = req.post(params.URL, JSON.stringify([alert]));

if (req.getStatus() !== 200) {
    throw 'Alertmanager failed: HTTP ' + req.getStatus() + ' ' + resp;
}
return resp;
```

**脚本要点：**

| 设计 | 说明 |
| --- | --- |
| `severity_map` | Zabbix 级别映射为 AM 级别：disaster/high → `critical`，average/warning → `warning`，其余 → `info` |
| `site_rules` | 按触发器名称/主机名关键词提取「站点」标签，供 `inhibit_rules` 的 `equal: ['site']` 匹配。**每条专线/站点加一行规则**；兜底为主机名或 IP，标签永不为空 |
| `endsAt` 问题 = now+4min | 小于 AM 的 `resolve_timeout`（默认 5m）：即使 Zabbix 恢复通知丢失，AM 也会在 4 分钟后自动 resolve，告警不会常驻 |
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
  - receiver: 'feishu-zabbix'
    match:
      source: zabbix
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
    "endsAt": "'$(date -u -d '+4 minute' +%Y-%m-%dT%H:%M:%SZ)'"
  }]'
```

预期：Alertmanager UI Alerts 列表出现该告警（带 `source=zabbix`）→ 约 `group_wait` 后飞书群B 收到红色卡片 → 不手动 resolve 的话约 4~5 分钟后自动收到绿色恢复卡片。

**② Zabbix 侧测试**：媒介类型页「测试」按字面传参（宏不展开），Status 手填 `PROBLEM`/`RESOLVED` 各测一次；最后用真实触发器验证端到端。

**③ 静默联动**：在群B 的告警卡片下拉选「静默 2 小时」，回执为橙色，且 Alertmanager UI Silences 中出现对应静默——证明第二实例的静默回链配置正确。

**④ 抑制验证**：同 `site` 下先注入一条 `critical`，再注入 `warning`；后者在 Alertmanager UI 中显示 Suppressed，飞书不推。

---

## 实战坑位清单

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 动作不执行：`No message defined for media type.` | Webhook 媒介没配消息模板 | 添加「问题」+「问题恢复」两条模板，内容非空 |
| 测试媒介弹窗 `foreach() argument must be of type array\|object, null given` | 同上（测试流程也读取模板） | 同上 |
| 动作日志 `No media defined for user.` | 动作引用的用户未挂载该媒介 | 用户 → 报警媒介中添加 |
| 保存/执行脚本 `SyntaxError: invalid token` | 复制粘贴引入全角字符/不间断空格 | 使用 `examples/zabbix/zabbix_alertmanager.js` 原文件；中文仅允许出现在规则表字符串值中 |
| 恢复消息里的宏传到脚本为字面量 | `{EVENT.RECOVERY.MESSAGE}` 只在恢复操作消息正文解析 | 媒介参数 `Detail` 用 `{EVENT.OPDATA}` |
| 告警已恢复但 AM 里常驻不消失 | Zabbix 恢复通知丢失，AM 一直等 resolve | JS 已内置 `endsAt=now+4min < resolve_timeout 5m`，超时自动 resolve |
| P0 触发后同站点 P1 仍推送 | `site` 标签未命中（检查 JS `site_rules` 是否覆盖该主机名/触发器名关键词） | 补规则行，站点关键词取触发器名称或主机名的稳定片段 |

---

## Grafana 告警看板（可选）

导入 `examples/grafana-alert-center.json`（Dashboards → Import），需要：Loki 已部署且两实例的 `LOKI_URL` 已配置、Grafana 已添加 Loki 数据源。包含 24h 触发/恢复统计、级别分布、趋势、TOP5 告警、TOP10 对象、实时日志流 8 个面板，Zabbix 与 Prometheus 告警统一展示。
