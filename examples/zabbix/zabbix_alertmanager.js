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
    station = '',
    i,
    alert;

// Site label: used by Alertmanager inhibit_rules (equal: ['site']).
// Priority: 1) Zabbix host group via API (params.APIURL + APITOKEN + HostID)
//             -> picks the longest group name, e.g. "南阳万达/3楼";
//          2) site_rules table below (only if site is still just host/IP);
//          3) fallback = host or IP so the label is never empty.
if (params.APIURL && params.APITOKEN && params.HostID) {
    try {
        var api = new HttpRequest(),
            api_body = JSON.stringify({
                jsonrpc: '2.0',
                method: 'host.get',
                id: 1,
                auth: params.APITOKEN,
                params: { hostids: [params.HostID], selectGroups: ['name'] }
            });
        api.addHeader('Content-Type: application/json');
        var hosts = JSON.parse(api.post(params.APIURL, api_body)).result || [],
            best = '';
        // host.get returns host objects; groups live in hosts[0].groups
        if (hosts.length && hosts[0].groups) {
            var gs = hosts[0].groups;
            for (var g = 0; g < gs.length; g++) {
                if (gs[g].name.length > best.length) { best = gs[g].name; }
            }
        }
        if (best) {
            site = best;                       // 楼层，如 "南阳/万达/3楼"
            var idx = best.lastIndexOf('/');   // 站点 = 去掉最后一级
            if (idx > 0) { station = best.slice(0, idx); }  // "南阳/万达"
        }
    } catch (e) {
        // API unavailable -> site falls back to site_rules / host name
    }
}
// site_rules is only a fallback: skip it when the site already came from a
// host group or an explicit rule (i.e. site != plain host/target name).
if (site === (params.Host || params.Target)) {
    for (i = 0; i < site_rules.length; i++) {
        if ((params.Name || '').indexOf(site_rules[i].match) !== -1 ||
            (params.Host || '').indexOf(site_rules[i].match) !== -1) {
            site = site_rules[i].site;
            break;
        }
    }
}

var now = new Date();
// Problem: keep the alert alive for 2h10m after the notification. The Zabbix
// action re-sends unresolved problems every 2h (escalation step), so endsAt
// must outlive that interval for a seamless window in AM/Karma — with 30min
// the entry would vanish between re-sends. Recovery: endsAt=now marks the
// matching alert resolved immediately; a lost recovery notification
// auto-resolves after 2h10m.
var startsAt = params.Status === 'RESOLVED' ? new Date(now.getTime() - 1000).toISOString() : now.toISOString();
var endsAt = params.Status === 'RESOLVED' ? now.toISOString() : new Date(now.getTime() + 130 * 60 * 1000).toISOString();

var detail = params.Detail || params.Subject || '';
// Auto-label bandwidth operational data: "96.66 Mbps, 55.19 Mbps" (from
// {ITEM.LASTVALUE1}, {ITEM.LASTVALUE2}) -> "IN: 96.66 Mbps, OUT: 55.19 Mbps".
// Only the two-value bps/Kbps/Mbps pattern is rewritten; ICMP/CPU/etc. pass
// through. Assumes the trigger expression checks IN first, OUT second
// (Zabbix default).
var bw = /^\s*([\d.]+)\s*(bps|Kbps|Mbps)\s*,\s*([\d.]+)\s*(bps|Kbps|Mbps)\s*$/i.exec(detail);
if (bw) {
    detail = 'IN: ' + bw[1] + ' ' + bw[2] + ', OUT: ' + bw[3] + ' ' + bw[4];
}

// Label/annotation keys aligned with am_silence_proxy.py:
//   labels:      alertname / severity / instance / serviceName (object line)
//   annotations: title (card line title, fallback alertname) / template|description (detail)
alert = {
    labels: {
        alertname: params.Name,
        severity: severity,
        instance: params.Target,
        serviceName: params.Host,
        ip: params.Target || '',
        site: site,
        station: station,
        source: 'zabbix'
    },
    annotations: {
        title: params.Name,
        description: detail,
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
