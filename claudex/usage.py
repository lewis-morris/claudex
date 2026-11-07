"""Usage across both agents: tokens by day, project, model and Claudex role, as terminal charts or a web page.

Sources:
- Claude's own logs (~/.claude/projects/*/*.jsonl): every reply's token counts, model and folder.
- Codex's own logs (~/.codex/sessions/**/rollout-*.jsonl): running token totals per session, folder and model.
- Claudex's ledger (~/.local/share/claudex/usage.jsonl): its one-shot calls (lead, checks, gatekeeper, map),
  which don't appear in those logs, tagged with the role they played.
Neither tool reports how much of your plan's allowance is left, so this shows tokens, not "percent remaining".
"""
import collections
import datetime
import glob
import http.server
import json
import os
import pathlib
import socketserver
import threading
import time

DATA = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local/share")) / "claudex"
LEDGER = DATA / "usage.jsonl"
INDEX = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "claudex/usage-index.json"
CLAUDE_LOGS = pathlib.Path.home() / ".claude/projects"
CODEX_LOGS = pathlib.Path.home() / ".codex/sessions"
COLOURS = {"claude": "#ff8f6b", "codex": "#5fd0e0"}


# ---------- Claudex's own ledger ----------
def record(agent, model, role, cwd, fresh=0, cached=0, output=0, total=None, cost=None, secs=None):
    row = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "agent": agent,
           "model": model, "role": role, "project": str(cwd), "fresh": int(fresh or 0), "cached": int(cached or 0),
           "output": int(output or 0), "total": int(total if total is not None else (fresh or 0) + (output or 0)),
           "cost": cost, "secs": secs}
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def ledger(since):
    out = []
    try:
        for line in LEDGER.read_text().splitlines():
            r = json.loads(line)
            if r["at"] >= since:
                out.append(r)
    except Exception:
        pass
    return out


# ---------- the agents' own logs (cached per file) ----------
def _claude_file(path):
    events, seen = [], set()
    with open(path, errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            m = d.get("message") or {}
            u = m.get("usage")
            if not isinstance(u, dict):
                continue
            key = (m.get("id"), d.get("requestId"))
            if key in seen:      # one reply is logged once per content block
                continue
            seen.add(key)
            fresh = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
            events.append({"at": d.get("timestamp", ""), "agent": "claude", "model": m.get("model") or "?",
                           "project": d.get("cwd") or "?", "fresh": fresh,
                           "cached": int(u.get("cache_read_input_tokens") or 0),
                           "output": int(u.get("output_tokens") or 0)})
    return events


def _codex_file(path):
    events, cwd, model, prev = [], "?", "?", None
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            p = d.get("payload") or {}
            if d.get("type") in ("session_meta", "turn_context"):
                cwd = p.get("cwd") or cwd
                model = p.get("model") or model
                continue
            if p.get("type") != "token_count" or not isinstance(p.get("info"), dict):
                continue
            tot = p["info"].get("total_token_usage") or {}
            cur = (int(tot.get("input_tokens") or 0), int(tot.get("cached_input_tokens") or 0),
                   int(tot.get("output_tokens") or 0))
            base = prev or (0, 0, 0)
            if prev is not None and cur == prev:
                continue
            d_in, d_cached, d_out = (max(0, a - b) for a, b in zip(cur, base))
            prev = cur
            events.append({"at": d.get("timestamp", ""), "agent": "codex", "model": model, "project": cwd,
                           "fresh": max(0, d_in - d_cached), "cached": d_cached, "output": d_out})
    return events


def _files(cutoff):
    files = [(p, _claude_file) for p in glob.glob(str(CLAUDE_LOGS / "**/*.jsonl"), recursive=True)]  # incl. subagents
    files += [(p, _codex_file) for p in glob.glob(str(CODEX_LOGS / "**/rollout-*.jsonl"), recursive=True)]
    for path, parse in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            yield path, parse, st


def native(since_days=30, since_epoch=None):
    """Every token event from both agents' logs (uncached: for a narrow window, like one benchmark run)."""
    cutoff = since_epoch if since_epoch is not None else time.time() - since_days * 86400
    out = []
    for path, parse, _ in _files(cutoff):
        try:
            out.extend(parse(path))
        except Exception:
            pass
    return out


def buckets(since_days=30):
    """Token totals per (day, agent, model, folder) from both agents' logs. Cached per file by size and time,
    as small daily totals, so a big history stays quick."""
    cutoff = time.time() - since_days * 86400
    try:
        cache = json.loads(INDEX.read_text())
    except Exception:
        cache = {}
    merged, fresh_cache = collections.defaultdict(lambda: [0, 0, 0]), {}
    for path, parse, st in _files(cutoff):
        sig = [st.st_mtime, st.st_size]
        hit = cache.get(path)
        if hit and hit.get("sig") == sig and "b" in hit:
            b = hit["b"]
        else:
            b = collections.defaultdict(lambda: [0, 0, 0])
            try:
                for e in parse(path):
                    k = "|".join((str(e["at"])[:10], e["agent"], str(e["model"]), str(e["project"])))
                    b[k][0] += e["fresh"]
                    b[k][1] += e["cached"]
                    b[k][2] += e["output"]
            except Exception:
                pass
            b = dict(b)
        fresh_cache[path] = {"sig": sig, "b": b}
        for k, v in b.items():
            m = merged[k]
            m[0] += v[0]
            m[1] += v[1]
            m[2] += v[2]
    try:
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        INDEX.write_text(json.dumps(fresh_cache))
    except Exception:
        pass
    return [{"at": k.split("|", 3)[0], "agent": k.split("|", 3)[1], "model": k.split("|", 3)[2],
             "project": k.split("|", 3)[3], "fresh": v[0], "cached": v[1], "output": v[2]} for k, v in merged.items()]


# ---------- who did the work, and why (from Claudex's own records in each project) ----------
PROJECTS = DATA / "projects.json"


def remember_project(root):
    try:
        known = set(json.loads(PROJECTS.read_text())) if PROJECTS.exists() else set()
        if str(root) not in known:
            known.add(str(root))
            DATA.mkdir(parents=True, exist_ok=True)
            PROJECTS.write_text(json.dumps(sorted(known)))
    except Exception:
        pass


def _jsonl(path, since):
    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if str(r.get("at", "")) >= since:
                yield r
    except Exception:
        return


def work_report(since, project=None):
    """Steps per agent and model (with the lead's reasons), checks, handovers and permission decisions."""
    try:
        roots = json.loads(PROJECTS.read_text()) if PROJECTS.exists() else []
    except Exception:
        roots = []
    if project:
        want = str(pathlib.Path(project).resolve())
        roots = [r for r in roots if r.startswith(want) or want.startswith(r)]
    since_local = since[:19]
    by = {}
    reasons = collections.defaultdict(collections.Counter)
    checks = collections.Counter()
    perms_by = collections.Counter()
    for root in roots:
        base = pathlib.Path(root) / ".claudex"
        for f in base.glob("sessions/*/steps.jsonl"):
            for r in _jsonl(f, since_local):
                kind = r.get("kind")
                if kind == "step":
                    key = f"{r.get('agent')} · {r.get('model')} · {r.get('effort')}"
                    a = by.setdefault(key, {"who": key, "agent": r.get("agent"), "steps": 0, "tries": 0,
                                            "secs": 0.0, "sent_back": 0, "projects": set()})
                    a["tries"] += 1
                    a["steps"] += 1 if r.get("attempt", 1) == 1 else 0
                    a["secs"] += float(r.get("secs") or 0)
                    a["sent_back"] += 1 if r.get("verdict") == "fix" else 0
                    a["projects"].add(pathlib.Path(root).name)
                    why = (r.get("why") or r.get("title") or "").split(" (handed")[0].strip()
                    if r.get("attempt", 1) == 1 and why:
                        reasons[key][why[:110]] += 1
                elif kind in ("second_opinion", "final_check"):
                    checks[f"{kind.replace('_', ' ')}: {r.get('verdict')}"] += 1
                elif kind == "usage_limit":
                    checks[f"usage limit hit: {r.get('agent')}"] += 1
        for r in _jsonl(base / "permissions.jsonl", since_local):
            perms_by[f"{r.get('by')} → {r.get('decision')}"] += 1
    rows = sorted(by.values(), key=lambda a: -a["tries"])
    for a in rows:
        a["projects"] = sorted(a["projects"])
        a["minutes"] = round(a.pop("secs") / 60, 1)
        a["why"] = [{"reason": k, "times": v} for k, v in reasons[a["who"]].most_common(4)]
    return {"workers": rows, "checks": checks.most_common(), "permissions": perms_by.most_common()}


# ---------- summaries ----------
def summary(days=14, project=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=days - 1)).date()
    since = datetime.datetime.combine(start, datetime.time(), datetime.timezone.utc).isoformat()
    events = [e for e in buckets(days + 1) if e["at"] >= since[:10]]
    events += [dict(r, source="claudex") for r in ledger(since)]
    if project:
        root = str(pathlib.Path(project).resolve())
        events = [e for e in events if str(e.get("project", "")).startswith(root)]
    day_keys = [(start + datetime.timedelta(days=i)).isoformat() for i in range(days)]
    by_day = {d: {"claude": 0, "codex": 0} for d in day_keys}
    by_project = collections.defaultdict(lambda: {"claude": 0, "codex": 0})
    by_model = collections.Counter()
    by_role = collections.defaultdict(lambda: {"claude": 0, "codex": 0})
    totals = {a: {"fresh": 0, "cached": 0, "output": 0, "work": 0} for a in ("claude", "codex")}
    for e in events:
        agent = e.get("agent")
        if agent not in totals:
            continue
        work = int(e.get("fresh") or 0) + int(e.get("output") or 0)
        if "total" in e and not e.get("fresh") and not e.get("output"):
            work = int(e.get("total") or 0)
        day = str(e.get("at", ""))[:10]
        if day in by_day:
            by_day[day][agent] += work
        by_project[pathlib.Path(str(e.get("project", "?"))).name or "?"][agent] += work
        by_model[f"{agent} · {e.get('model', '?')}"] += work
        if e.get("source") == "claudex":
            by_role[e.get("role") or "?"][agent] += work
        for k in ("fresh", "cached", "output"):
            totals[agent][k] += int(e.get(k) or 0)
        totals[agent]["work"] += work          # the headline number: same measure as the chart
    top = sorted(by_project.items(), key=lambda kv: -(kv[1]["claude"] + kv[1]["codex"]))[:10]
    work = work_report(since, project)
    return {"days": days, "generated": now.isoformat(timespec="seconds"), "project": project, "work": work,
            "by_day": [{"day": d, **by_day[d]} for d in day_keys], "by_project": [{"project": k, **v} for k, v in top],
            "by_model": by_model.most_common(10), "by_role": [{"role": k, **v} for k, v in sorted(by_role.items())],
            "totals": totals,
            "note": "Tokens = new input + output (cached reads listed separately). Neither tool reports how much "
                    "of your plan's allowance is left."}


def human(n):
    n = float(n)
    for unit in ("", "k", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.0f}{unit}" if unit == "" else f"{n:.1f}{unit}"
        n /= 1000
    return f"{n:.1f}T"


# ---------- terminal chart ----------
def _rgb(hex_):
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def terminal(s, colour=True, width=50):
    def paint(text, hex_):
        if not colour:
            return text
        r, g, b = _rgb(hex_)
        return f"\033[38;2;{r};{g};{b}m{text}\033[0m"
    dim = (lambda t: f"\033[2m{t}\033[0m") if colour else (lambda t: t)
    lines = [f"Usage, last {s['days']} days" + (f" · {s['project']}" if s.get("project") else "")
             + "   " + paint("█ Claude", COLOURS["claude"]) + "  " + paint("█ Codex", COLOURS["codex"]), ""]
    peak = max([d["claude"] + d["codex"] for d in s["by_day"]] + [1])
    for d in s["by_day"]:
        c = round(d["claude"] / peak * width)
        x = round(d["codex"] / peak * width)
        mark_c = "█" * c if colour else "C" * c
        mark_x = "█" * x if colour else "X" * x
        lines.append(f"{d['day'][5:]} {paint(mark_c, COLOURS['claude'])}{paint(mark_x, COLOURS['codex'])} "
                     + dim(human(d["claude"] + d["codex"])))
    t = s["totals"]
    lines += ["", f"Claude  {human(t['claude']['work'])} tokens "
                  f"({human(t['claude']['output'])} written, {human(t['claude']['cached'])} cached reads)",
              f"Codex   {human(t['codex']['work'])} tokens "
              f"({human(t['codex']['output'])} written, {human(t['codex']['cached'])} cached reads)"]
    if s["by_project"]:
        lines += ["", "Top projects:"]
        for p in s["by_project"][:6]:
            lines.append(f"  {p['project'][:28]:<28} {paint('Claude ' + human(p['claude']), COLOURS['claude'])}  "
                         f"{paint('Codex ' + human(p['codex']), COLOURS['codex'])}")
    if s["by_role"]:
        lines += ["", "Claudex's own calls, by role:"]
        for r in s["by_role"]:
            lines.append(f"  {r['role']:<16} Claude {human(r['claude'])}  Codex {human(r['codex'])}")
    w = s.get("work") or {}
    if w.get("workers"):
        lines += ["", "Who did the work, and why (Claudex jobs):"]
        for a in w["workers"][:8]:
            back = f", sent back {a['sent_back']}×" if a["sent_back"] else ""
            lines.append("  " + paint(f"{a['who']:<34}", COLOURS.get(a["agent"], "#ffffff"))
                         + f" {a['steps']} part(s), {a['minutes']} min{back}")
            for r in a["why"][:2]:
                lines.append(dim(f"      {r['times']}× {r['reason']}"))
    if w.get("checks"):
        lines += ["", "Checks: " + " · ".join(f"{k} ×{v}" for k, v in w["checks"][:6])]
    if w.get("permissions"):
        lines += ["Permission questions: " + " · ".join(f"{k} ×{v}" for k, v in w["permissions"][:6])]
    lines += ["", dim(s["note"])]
    return "\n".join(lines)


# ---------- web page ----------
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Claudex usage</title>
<style>
:root{--bg:#13201a;--panel:#1a2a22;--edge:#2e4a3c;--text:#eef3e6;--dim:#8fa597;--claude:#ff8f6b;--codex:#5fd0e0;--accent:#ffd23f}
@media (prefers-color-scheme: light){:root{--bg:#f7f3e8;--panel:#efe8d6;--edge:#d2c6a9;--text:#2a2a30;--dim:#7c7565;--claude:#e0673f;--codex:#1f8fa3;--accent:#d88a00}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif;padding:16px}
main{max-width:1000px;margin:auto}h1{font-size:22px;margin:4px 0}h1 b{color:var(--accent)}
.card{background:var(--panel);border:1px solid var(--edge);border-radius:12px;padding:14px 16px;margin:14px 0}
.key span{display:inline-block;width:12px;height:12px;border-radius:3px;margin:0 6px 0 14px;vertical-align:-1px}
.dim{color:var(--dim)}table{width:100%;border-collapse:collapse}td,th{padding:5px 6px;text-align:left;border-bottom:1px solid var(--edge)}
td.n{text-align:right;font-variant-numeric:tabular-nums}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.big{font-size:26px;font-weight:600}select{background:var(--panel);color:var(--text);border:1px solid var(--edge);border-radius:6px;padding:3px}
svg text{fill:var(--dim);font-size:11px}
</style></head><body><main>
<h1>🐒 <b>Claudex</b> usage</h1>
<div class="dim">Across Claude and Codex · <select id="days"><option>7</option><option selected>14</option><option>30</option></select> days ·
<span id="when"></span> · refreshes every 30 s</div>
<div class="row" id="tot"></div>
<div class="card"><div class="key"><span style="background:var(--claude)"></span>Claude<span style="background:var(--codex)"></span>Codex</div>
<svg id="chart" width="100%" height="260" role="img" aria-label="Tokens per day"></svg></div>
<div class="row"><div class="card"><h3>Projects</h3><table id="proj"></table></div>
<div class="card"><h3>Models</h3><table id="models"></table></div></div>
<div class="card"><h3>Who did the work, and why</h3><table id="work"></table>
<p class="dim" id="checks"></p></div>
<div class="card"><h3>Claudex's own calls, by role</h3><table id="roles"></table></div>
<p class="dim" id="note"></p></main>
<script>
const fmt=n=>n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);
const esc=x=>String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v);
async function load(){
 const days=document.getElementById('days').value;
 const s=await (await fetch('api/usage?days='+days)).json();
 document.getElementById('when').textContent='updated '+new Date(s.generated).toLocaleTimeString();
 const t=s.totals, sum=a=>t[a].work;
 document.getElementById('tot').innerHTML=['claude','codex'].map(a=>`<div class="card"><div class="dim">${a[0].toUpperCase()+a.slice(1)}</div>
  <div class="big" style="color:var(--${a})">${fmt(sum(a))}</div><div class="dim">${fmt(t[a].output)} written · ${fmt(t[a].cached)} cached reads</div></div>`).join('');
 const svg=document.getElementById('chart'),W=svg.clientWidth||900,H=260,pad=28,n=s.by_day.length;
 const peak=Math.max(1,...s.by_day.map(d=>d.claude+d.codex)),bw=(W-pad*2)/n;
 let h='';s.by_day.forEach((d,i)=>{const x=pad+i*bw+bw*.15,w=bw*.7,hc=(H-50)*d.claude/peak,hx=(H-50)*d.codex/peak,base=H-24;
  h+=`<rect x="${x}" y="${base-hc}" width="${w}" height="${hc}" rx="3" fill="${css('--claude')}"><title>${d.day} Claude ${fmt(d.claude)}</title></rect>`;
  h+=`<rect x="${x}" y="${base-hc-hx}" width="${w}" height="${hx}" rx="3" fill="${css('--codex')}"><title>${d.day} Codex ${fmt(d.codex)}</title></rect>`;
  if(n<=14||i%2==0)h+=`<text x="${x+w/2}" y="${H-8}" text-anchor="middle">${d.day.slice(5)}</text>`;
  if(d.claude+d.codex)h+=`<text x="${x+w/2}" y="${base-hc-hx-4}" text-anchor="middle">${fmt(d.claude+d.codex)}</text>`;});
 svg.innerHTML=h;
 const rows=(id,items,cols)=>document.getElementById(id).innerHTML=items.length?items.map(cols).join(''):'<tr><td class="dim">nothing yet</td></tr>';
 rows('proj',s.by_project,p=>`<tr><td>${esc(p.project)}</td><td class="n" style="color:var(--claude)">${fmt(p.claude)}</td><td class="n" style="color:var(--codex)">${fmt(p.codex)}</td></tr>`);
 rows('models',s.by_model,m=>`<tr><td>${esc(m[0])}</td><td class="n">${fmt(m[1])}</td></tr>`);
 rows('roles',s.by_role,r=>`<tr><td>${esc(r.role)}</td><td class="n" style="color:var(--claude)">${fmt(r.claude)}</td><td class="n" style="color:var(--codex)">${fmt(r.codex)}</td></tr>`);
 const w=s.work||{workers:[],checks:[],permissions:[]};
 rows('work',w.workers,a=>`<tr><td style="color:var(--${a.agent==='codex'?'codex':'claude'})">${esc(a.who)}</td><td class="n">${a.steps} part(s)</td>
  <td class="n">${a.minutes} min</td><td class="n">${a.sent_back?('sent back '+a.sent_back+'×'):''}</td></tr>
  <tr><td colspan="4" class="dim">${a.why.map(r=>esc(r.times+'× '+r.reason)).join('<br>')}</td></tr>`);
 document.getElementById('checks').textContent=[...w.checks.map(c=>c[0]+' ×'+c[1]),...w.permissions.map(c=>'permission '+c[0]+' ×'+c[1])].join(' · ');
 document.getElementById('note').textContent=s.note;}
document.getElementById('days').onchange=load;load();setInterval(load,30000);
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    project = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/usage"):
            q = dict(x.split("=", 1) for x in (self.path.split("?", 1)[1:] or [""])[0].split("&") if "=" in x)
            days = max(1, min(90, int(q.get("days", "14")) if q.get("days", "14").isdigit() else 14))
            body = json.dumps(summary(days, self.project)).encode()
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(host="127.0.0.1", port=8765, project=None, open_browser=True):
    _Handler.project = project
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((host, port), _Handler) as httpd:
        url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
        print(f"Claudex usage page: {url}" + ("  (also on your network)" if host == "0.0.0.0" else ""))
        print("Ctrl-C to stop.")
        if open_browser:
            threading.Timer(0.5, lambda: __import__("webbrowser").open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
