#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alertmanager 静默中转服务
--------------------------------
飞书告警里的「静默 2 小时 / 1 天 / 2 天」链接指向本服务，
点击后本服务直接调用 Alertmanager API 创建静默，无需在 UI 里手动填 Duration。

用法（飞书链接）：
  http://<本服务地址>:8428/silence?duration=2h&comment=飞书一键静默&filter=<matchers>
  http://<本服务地址>:8428/silence?duration=1d&filter=<matchers>
  http://<本服务地址>:8428/silence?duration=2d&filter=<matchers>

其中 filter 与你现在飞书模板里 {{SplitString $data 0 -3}} 生成的内容完全一致，
形如：alertname="磁盘使用率",alertype="system",device="/dev/vda2",...

依赖：pip install flask requests
启动：python am_silence_proxy.py
"""

import re
import time
import json
import hmac
import base64
import hashlib
import datetime
import urllib.parse

import requests
from flask import Flask, request, Response

# ============ 配置区 ============
ALERTMANAGER_URL = "http://192.168.99.20:9093"   # Alertmanager 地址
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8428
DEFAULT_CREATED_BY = "feishu"

# 飞书群机器人：在群设置 -> 群机器人 -> 添加「自定义机器人」拿到 webhook。
# 若机器人开启了「签名校验」，把密钥填到 FEISHU_SECRET，否则留空 ""。
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/你的机器人token"
FEISHU_SECRET = ""   # 未开启签名校验则留空

# 本服务对外可访问的基础地址（Alertmanager webhook 卡片里的下拉选项会指回这里）。
SELF_BASE_URL = "http://192.168.99.23:8428"

# 卡片里其它跳转按钮（按需修改，留空则不显示）
DASHBOARD_URL = "http://192.168.99.20:3000/d/9CWBz0bi/linux-dashboard?orgId=1"
HISTORY_URL = "http://192.168.99.20:8080/record"

# 静默下拉可选时长：(显示文案, duration 值)
SILENCE_OPTIONS = [
    ("🔕 静默 2 小时", "2h"),
    ("🔕 静默 1 天", "1d"),
    ("🔕 静默 2 天", "2d"),
    ("🔕 静默 1 周", "1w"),
]

# Loki 日志（可选）：填 push 地址即开启，留空 "" 则不推送。
LOKI_URL = "http://192.168.99.23:3100/loki/api/v1/push"
# ===============================

app = Flask(__name__)

# 匹配 key<op>"value"，op 支持 =  !=  =~  !~
MATCHER_RE = re.compile(r'(\w+)\s*(=~|!=|!~|=)\s*"((?:[^"\\]|\\.)*)"')


def parse_duration(text):
    """把 2h / 24h / 1d / 30m / 90s 解析成 timedelta。"""
    text = (text or "").strip().lower()
    m = re.fullmatch(r'(\d+)\s*([smhdw])', text)
    if not m:
        raise ValueError("时长格式不合法，示例：30m 2h 1d 2d 1w")
    num = int(m.group(1))
    unit = m.group(2)
    return {
        "s": datetime.timedelta(seconds=num),
        "m": datetime.timedelta(minutes=num),
        "h": datetime.timedelta(hours=num),
        "d": datetime.timedelta(days=num),
        "w": datetime.timedelta(weeks=num),
    }[unit]


def parse_matchers(filter_str):
    """把 alertname="x",device="/dev/vda2" 解析为 Alertmanager API 的 matchers 列表。"""
    filter_str = (filter_str or "").strip()
    # 去掉可能包裹的花括号
    if filter_str.startswith("{"):
        filter_str = filter_str[1:]
    if filter_str.endswith("}"):
        filter_str = filter_str[:-1]

    matchers = []
    for name, op, value in MATCHER_RE.findall(filter_str):
        is_regex = op in ("=~", "!~")
        is_equal = op in ("=", "=~")
        matchers.append({
            "name": name,
            "value": value,
            "isRegex": is_regex,
            "isEqual": is_equal,
        })
    return matchers


def html_page(title, body_html, status=200):
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 body{{font-family:-apple-system,"PingFang SC",Microsoft YaHei,sans-serif;background:#f5f6fa;margin:0;padding:40px}}
 .card{{max-width:640px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
 h1{{font-size:20px;margin:0 0 16px}}
 .ok{{color:#1a9c3e}} .err{{color:#d93025}}
 .kv{{margin:6px 0;color:#444;font-size:14px}}
 .kv b{{display:inline-block;width:96px;color:#888;font-weight:normal}}
 a.btn{{display:inline-block;margin-top:20px;padding:8px 18px;background:#2f6bff;color:#fff;
        text-decoration:none;border-radius:6px;font-size:14px}}
 code{{background:#f0f1f5;padding:2px 6px;border-radius:4px;font-size:13px}}
</style></head>
<body><div class="card">{body_html}</div></body></html>"""
    return Response(html, status=status, mimetype="text/html; charset=utf-8")


def humanize_delta(delta):
    """把 timedelta 转成「X天X小时X分钟」中文描述。"""
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分钟")
    return "".join(parts) or "0分钟"


def _feishu_sign(secret, timestamp):
    """飞书自定义机器人签名校验。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def notify_feishu(human_dur, end_local, matchers, comment, view_url):
    """静默创建成功后，往飞书群机器人推送一张「已静默」卡片消息。"""
    if not FEISHU_WEBHOOK or "你的机器人token" in FEISHU_WEBHOOK:
        return  # 未配置 webhook，跳过

    labels = {m["name"]: m["value"] for m in matchers}
    alertname = labels.get("alertname", "未知告警")
    instance = labels.get("instance", "")
    service = labels.get("serviceName", "")
    severity = labels.get("severity", "")
    target = " / ".join(x for x in (service, instance) if x)

    # 静默成功卡片：固定橙色
    color = "orange"

    # 卡片正文字段（两列展示）
    fields = [
        {"is_short": True, "text": {"tag": "lark_md",
         "content": f"**🔔 告警名称**\n{alertname}"}},
        {"is_short": True, "text": {"tag": "lark_md",
         "content": f"**⏱ 静默时长**\n{human_dur}"}},
    ]
    if target:
        fields.append({"is_short": True, "text": {"tag": "lark_md",
                       "content": f"**🖥 对象**\n{target}"}})
    if severity:
        fields.append({"is_short": True, "text": {"tag": "lark_md",
                       "content": f"**🚦 告警等级**\n{severity}"}})
    fields.append({"is_short": False, "text": {"tag": "lark_md",
                   "content": f"**⏰ 恢复通知时间**\n{end_local}"}})
    fields.append({"is_short": False, "text": {"tag": "lark_md",
                   "content": f"**📝 备注**\n{comment}"}})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": "🔕 告警已静默"},
        },
        "elements": [
            {"tag": "div", "fields": fields},
            {"tag": "hr"},
            {"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "在 Alertmanager 查看"},
                "url": view_url,
                "type": "primary",
            }]},
        ],
    }
    payload = {"msg_type": "interactive", "card": card}

    if FEISHU_SECRET:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _feishu_sign(FEISHU_SECRET, ts)

    _post_feishu(payload, tag="silence-notify")


def _post_feishu(payload, tag=""):
    """统一往飞书发消息并打印结果日志；返回是否成功（飞书 code==0）。"""
    if not FEISHU_WEBHOOK or "你的机器人token" in FEISHU_WEBHOOK:
        print(f"[feishu][{tag}] 未配置 FEISHU_WEBHOOK，跳过", flush=True)
        return False
    try:
        r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=8)
    except Exception as e:
        print(f"[feishu][{tag}] 请求异常: {e!r}", flush=True)
        return False
    body = (r.text or "")[:300]
    try:
        code = r.json().get("code")
    except Exception:
        code = None
    ok = (r.status_code == 200 and code == 0)
    print(f"[feishu][{tag}] http={r.status_code} code={code} ok={ok} resp={body}",
          flush=True)
    return ok


def send_feishu_card(card):
    """把一张 interactive 卡片发到飞书群机器人（自动处理签名）。"""
    payload = {"msg_type": "interactive", "card": card}
    if FEISHU_SECRET:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _feishu_sign(FEISHU_SECRET, ts)
    return _post_feishu(payload, tag="alert-card")


CST = datetime.timezone(datetime.timedelta(hours=8))


def fmt_cst(ts_str):
    """把 Alertmanager 的 RFC3339 时间转成东八区 'YYYY-MM-DD HH:MM:SS'。"""
    if not ts_str:
        return ""
    s = ts_str.strip().replace("Z", "+00:00")
    # 截断过长的小数秒（Python fromisoformat 只认最多 6 位）
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts_str


def build_filter_from_labels(labels):
    """把标签 dict 拼成 matchers 串: alertname=\"x\",device=\"/dev/vda2\" 。"""
    return ",".join(f'{k}="{v}"' for k, v in labels.items())


def silence_url(labels, duration):
    """生成指回本服务 /silence 的一键静默链接。"""
    matcher_str = build_filter_from_labels(labels)
    q = urllib.parse.urlencode({"duration": duration, "filter": matcher_str})
    return f"{SELF_BASE_URL}/silence?{q}"


def build_alert_element(alert):
    """为单条告警生成卡片元素：详情 + 静默下拉 + 跳转按钮。"""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    status = alert.get("status", "firing")
    resolved = status == "resolved"

    title = annotations.get("title") or labels.get("alertname", "告警")
    tmpl = annotations.get("template") or annotations.get("description", "")
    severity = labels.get("severity", "")
    instance = labels.get("instance", "")
    service = labels.get("serviceName", "")
    target = " / ".join(x for x in (service, instance) if x)

    icon = "🌤" if resolved else "⛈"
    stat_text = "已恢复" if resolved else status

    lines = [f"**{title}** {icon}  {stat_text}"]
    if severity:
        lines.append(f"🔢  **告警等级**：{severity}")
    if target:
        lines.append(f"🖥  **对象**：{target}")
    lines.append(f"🧭  **触发时间**：{fmt_cst(alert.get('startsAt'))}")
    if resolved:
        lines.append(f"🧭  **结束时间**：{fmt_cst(alert.get('endsAt'))}")
    if tmpl:
        lines.append(f"📜  **告警详情**：\n📌  {tmpl}")

    elements = [{"tag": "div", "text": {"tag": "lark_md",
                 "content": "\n".join(lines)}}]

    # 跳转按钮 + 静默下拉
    actions = []
    if not resolved:
        overflow_options = [
            {"text": {"tag": "plain_text", "content": txt},
             "url": silence_url(labels, dur)}
            for txt, dur in SILENCE_OPTIONS
        ]
        actions.append({
            "tag": "overflow",
            "options": overflow_options,
        })
    if DASHBOARD_URL:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📊 图形看板"},
            "url": DASHBOARD_URL, "type": "default",
        })
    if HISTORY_URL:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📚 告警历史"},
            "url": HISTORY_URL, "type": "default",
        })
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return elements, resolved


def _loki_level(status, severity):
    """把告警状态/等级映射成 Grafana 认识的日志级别（用于自动着色）。"""
    if status == "resolved":
        return "info"            # 绿色：已恢复
    s = (severity or "").lower()
    if s in ("critical", "error", "fatal", "emergency", "page"):
        return "critical"        # 红色：严重
    if s in ("warning", "warn"):
        return "warning"         # 黄色：警告
    return "error"               # 其它触发告警统一红色


def push_to_loki(alert):
    """把单条告警写入 Loki（作为一条可搜索的告警日志）。"""
    if not LOKI_URL:
        return
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    status = alert.get("status", "firing")
    # 流标签保持低基数：alertname / severity / status / level
    stream = {
        "job": "alertmanager",
        "alertname": labels.get("alertname", "unknown"),
        "severity": labels.get("severity", "none"),
        "status": status,
        # level 供 Grafana Logs 面板自动着色：
        #   resolved -> info(绿)  critical -> critical(红)  warning -> warning(黄)
        "level": _loki_level(status, labels.get("severity", "")),
    }
    # 日志正文放完整明细，便于全文搜索
    line = json.dumps({
        "status": status,
        "title": annotations.get("title", ""),
        "template": annotations.get("template") or annotations.get("description", ""),
        "instance": labels.get("instance", ""),
        "serviceName": labels.get("serviceName", ""),
        "labels": labels,
        "startsAt": alert.get("startsAt"),
        "endsAt": alert.get("endsAt"),
    }, ensure_ascii=False)
    ts = str(int(time.time() * 1_000_000_000))  # 纳秒时间戳
    payload = {"streams": [{"stream": stream, "values": [[ts, line]]}]}
    try:
        r = requests.post(LOKI_URL, json=payload, timeout=5)
        if r.status_code >= 300:
            print(f"[loki] push failed http={r.status_code} {r.text[:200]}",
                  flush=True)
    except Exception as e:
        print(f"[loki] push exception: {e!r}", flush=True)


@app.route("/alert", methods=["POST"])
def alert():
    """接收 Alertmanager webhook，渲染带静默下拉的飞书卡片并推送。"""
    data = request.get_json(force=True, silent=True) or {}
    alerts = data.get("alerts", [])
    if not alerts:
        return {"status": "no alerts"}, 200

    all_resolved = True
    any_firing = False
    elements = []
    for i, al in enumerate(alerts):
        els, resolved = build_alert_element(al)
        push_to_loki(al)   # 写入 Loki 告警日志
        if resolved:
            pass
        else:
            any_firing = True
            all_resolved = False
        elements.extend(els)
        if i < len(alerts) - 1:
            elements.append({"tag": "hr"})

    # 标题与颜色：触发=红，全部恢复=绿
    if all_resolved:
        color, header = "green", "🌤 告警已恢复"
    elif any_firing:
        color, header = "red", "⛈ 触发告警"
    else:
        color, header = "grey", "告警通知"

    count = len(alerts)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": color,
                   "title": {"tag": "plain_text",
                             "content": f"{header}（{count} 条）"}},
        "elements": elements,
    }
    ok = send_feishu_card(card)
    return {"status": "sent" if ok else "feishu failed", "alerts": count}, 200


@app.route("/silence")
def silence():
    filter_str = request.args.get("filter", "")
    duration_str = request.args.get("duration", "2h")
    comment = request.args.get("comment", "") or f"飞书一键静默 {duration_str}"
    created_by = request.args.get("createdBy", DEFAULT_CREATED_BY)

    try:
        delta = parse_duration(duration_str)
    except ValueError as e:
        return html_page("参数错误",
                         f'<h1 class="err">时长参数错误</h1><div class="kv">{e}</div>', 400)

    matchers = parse_matchers(filter_str)
    if not matchers:
        return html_page("参数错误",
                         '<h1 class="err">未解析到有效 matchers</h1>'
                         '<div class="kv">请检查 filter 参数是否形如 '
                         '<code>alertname="x",device="/dev/vda2"</code></div>', 400)

    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "matchers": matchers,
        "startsAt": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endsAt": (now + delta).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "createdBy": created_by,
        "comment": comment,
    }

    try:
        resp = requests.post(f"{ALERTMANAGER_URL}/api/v2/silences",
                             json=payload, timeout=10)
        resp.raise_for_status()
        silence_id = resp.json().get("silenceID", "")
    except Exception as e:
        return html_page("创建失败",
                         f'<h1 class="err">静默创建失败</h1>'
                         f'<div class="kv">{e}</div>', 502)

    matcher_lines = "".join(
        f'<div class="kv"><b>{m["name"]}</b>{"=~" if m["isRegex"] else "="}"{m["value"]}"</div>'
        for m in matchers
    )
    end_local = (now + delta).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    view_url = f"{ALERTMANAGER_URL}/#/silences/{silence_id}"
    human_dur = humanize_delta(delta)

    # 往飞书群机器人推送「已静默」消息
    notify_feishu(human_dur, end_local, matchers, comment, view_url)

    body = f"""
      <h1 class="ok">✅ 静默创建成功</h1>
      <div class="kv"><b>静默时长</b>{duration_str}（{human_dur}）</div>
      <div class="kv"><b>结束时间</b>{end_local}（本地时区）</div>
      <div class="kv"><b>静默 ID</b><code>{silence_id}</code></div>
      <div class="kv"><b>备注</b>{comment}</div>
      <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
      <div class="kv" style="color:#888">匹配的标签：</div>
      {matcher_lines}
      <a class="btn" href="{view_url}" target="_blank">在 Alertmanager 查看</a>
    """
    return html_page("静默创建成功", body)


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
