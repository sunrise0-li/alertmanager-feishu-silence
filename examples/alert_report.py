#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
告警周报/月报：从 Loki 汇总上一周期的告警记录，推送飞书卡片。

用法（cron 或手动）:
  python3 alert_report.py weekly    # 上周一 00:00 ~ 上周日 24:00（周一早上跑）
  python3 alert_report.py monthly   # 上个自然月（每月 1 号跑）

建议 crontab（服务器时区为 CST）:
  0 9 * * 1  python3 /opt/alert_report.py weekly
  0 9 1 * *  python3 /opt/alert_report.py monthly
"""
import json
import re
import sys
import datetime
import urllib.request
import urllib.parse

# ===============================
# 配置区
# ===============================
LOKI_URL = "http://192.168.99.23:3100"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/你的token"
CST = datetime.timezone(datetime.timedelta(hours=8))
TOP_N = 5          # 各榜单条数
LOKI_LIMIT = 5000  # 最多拉取的日志条数
# ===============================


def period_range(kind):
    """返回 (start, end) CST datetime：上一自然周 / 上一自然月。"""
    now = datetime.datetime.now(CST)
    today = now.date()
    if kind == "weekly":
        last_monday = today - datetime.timedelta(days=today.weekday() + 7)
        start = datetime.datetime.combine(last_monday, datetime.time.min, CST)
        end = start + datetime.timedelta(days=7)
    elif kind == "monthly":
        first_of_this = today.replace(day=1)
        last_month_end = first_of_this - datetime.timedelta(days=1)
        start = datetime.datetime.combine(
            last_month_end.replace(day=1), datetime.time.min, CST)
        end = datetime.datetime.combine(
            first_of_this, datetime.time.min, CST)
    else:
        raise ValueError("kind must be weekly or monthly")
    return start, end


def to_ns(dt):
    return str(int(dt.timestamp() * 1_000_000_000))


def query_loki(start, end):
    """拉取时间范围内的全部告警记录（job="alertmanager"）。"""
    query = '{job="alertmanager"}'
    params = urllib.parse.urlencode({
        "query": query,
        "start": to_ns(start),
        "end": to_ns(end),
        "limit": LOKI_LIMIT,
        "direction": "forward",
    })
    url = f"{LOKI_URL}/loki/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    lines = []
    for stream in data.get("data", {}).get("result", []):
        for ts, line in stream.get("values", []):
            try:
                rec = json.loads(line)
                rec["_ts"] = int(ts) / 1e9  # 记录推送时刻（秒），用于配对计时
                lines.append(rec)
            except json.JSONDecodeError:
                continue
    return lines


def pair_durations(records, end):
    """
    按 (title, instance, serviceName) 配对触发/恢复记录，估算恢复时长。
    返回 (durations[], unresolved[])：
      durations  = [(秒, title, host)] 已恢复问题（首个 firing 到 resolved）
      unresolved = [(秒, title, host)] 周期末仍未恢复（firing 到 end）
    说明：同一问题期间的 2h 升级重发视为同一问题，只计首次触发时间。
    """
    events = []
    for r in records:
        labels = r.get("labels", {})
        key = (r.get("title") or labels.get("alertname", "unknown"),
               labels.get("instance", ""),
               labels.get("serviceName", ""))
        events.append((r.get("_ts", 0), r.get("status"), key,
                       r.get("title") or labels.get("alertname", "unknown"),
                       labels.get("serviceName") or labels.get("instance", "")))
    events.sort(key=lambda e: e[0])

    durations, unresolved = [], []
    open_problems = {}   # key -> (since_ts, title, host)，支持多个问题并行
    for ts, status, key, title, host in events:
        if status == "firing":
            if key not in open_problems:   # 2h 升级重发不会重复开问题
                open_problems[key] = (ts, title, host)
        elif status == "resolved":
            if key in open_problems:
                since, o_title, o_host = open_problems.pop(key)
                durations.append((max(ts - since, 0), o_title, o_host))
    for since, title, host in open_problems.values():
        unresolved.append((max(end.timestamp() - since, 0), title, host))
    return durations, unresolved


def human_dur(sec):
    sec = int(sec)
    if sec >= 3600:
        h, m = sec // 3600, (sec % 3600) // 60
        return f"{h}小时{m:02d}分" if m else f"{h}小时"
    return f"{sec // 60}分{sec % 60:02d}秒"


def top_n(counter, n):
    return sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:n]


def build_report(kind, start, end, records):
    """聚合统计，返回飞书 lark_md 报告正文。"""
    firing = [r for r in records if r.get("status") == "firing"]
    resolved = [r for r in records if r.get("status") == "resolved"]
    critical = [r for r in firing
                if r.get("labels", {}).get("severity") == "critical"]

    by_name, by_site, by_station, by_host = {}, {}, {}, {}
    for r in firing:
        labels = r.get("labels", {})
        by_name[r.get("title") or labels.get("alertname", "unknown")] = \
            by_name.get(r.get("title") or labels.get("alertname", "unknown"), 0) + 1
        if labels.get("site"):
            by_site[labels["site"]] = by_site.get(labels["site"], 0) + 1
        if labels.get("station"):
            by_station[labels["station"]] = by_station.get(labels["station"], 0) + 1
        host = labels.get("serviceName") or labels.get("instance", "unknown")
        by_host[host] = by_host.get(host, 0) + 1

    title = "📊 告警周报" if kind == "weekly" else "📊 告警月报"
    span = f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M}"
    durations, unresolved = pair_durations(records, end)
    lines = [
        f"**统计周期**：{span}",
        f"**触发告警**：{len(firing)} 次 ｜ **恢复**：{len(resolved)} 次 ｜ "
        f"**严重(critical)**：{len(critical)} 次",
        "",
        "**⏱ 恢复时长**",
    ]
    if durations:
        avg = sum(d for d, _, _ in durations) / len(durations)
        worst = max(durations, key=lambda x: x[0])
        lines.append(
            f"- 平均恢复时长：{human_dur(avg)}（{len(durations)} 例）")
        lines.append(
            f"- 最长恢复耗时：{human_dur(worst[0])}（{worst[1]} @ {worst[2]}）")
    else:
        lines.append("- 本周期无完整「触发→恢复」配对")
    if unresolved:
        u = max(unresolved, key=lambda x: x[0])
        lines.append(
            f"- ⚠️ 期末未恢复：{len(unresolved)} 条，最长已持续 "
            f"{human_dur(u[0])}（{u[1]} @ {u[2]}）")
    lines.append("")
    lines.append("**🔥 告警次数 TOP%d（按告警名称）**" % TOP_N)
    for name, cnt in top_n(by_name, TOP_N):
        lines.append(f"- {name}：{cnt} 次")
    if not by_name:
        lines.append("- （无）")

    lines.append("")
    lines.append("**🏢 楼层分布 TOP%d**" % TOP_N)
    for site, cnt in top_n(by_site, TOP_N):
        lines.append(f"- {site}：{cnt} 次")
    if not by_site:
        lines.append("- （无 site 标签的告警）")

    lines.append("")
    lines.append("**📍 站点分布 TOP%d**" % TOP_N)
    for station, cnt in top_n(by_station, TOP_N):
        lines.append(f"- {station}：{cnt} 次")
    if not by_station:
        lines.append("- （无 station 标签的告警）")

    lines.append("")
    lines.append("**🖥 主机分布 TOP%d**" % TOP_N)
    for host, cnt in top_n(by_host, TOP_N):
        lines.append(f"- {host}：{cnt} 次")
    if not by_host:
        lines.append("- （无）")

    return title, "\n".join(lines)


def send_feishu(title, body):
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": body},
            }],
        },
    }
    req = urllib.request.Request(
        FEISHU_WEBHOOK,
        data=json.dumps(card).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("code") not in (0, None):
        print(f"[feishu] push failed: {resp}", flush=True)


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    start, end = period_range(kind)
    records = query_loki(start, end)
    title, body = build_report(kind, start, end, records)
    send_feishu(title, body)
    print(f"[{kind}] {start} ~ {end}: {len(records)} records, report sent.",
          flush=True)


if __name__ == "__main__":
    main()
