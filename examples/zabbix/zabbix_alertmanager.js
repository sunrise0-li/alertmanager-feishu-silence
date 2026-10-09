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
