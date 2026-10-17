"""PROTOTYPE, throwaway: attempt log view (ticket #18, map #15). Do not merge to main.

Question: how should the attempts list, event timeline (filters, paging) and inline
evidence file panel look and behave?

Three variants of the run page, switch with `?variant=A|B|C` or the bottom bar / arrow keys:
  A  one flat table, "open" expands the evidence file inline under the row
  B  master-detail: timeline left, sticky evidence panel right (call and result side by side)
  C  exchanges: call+result folded into one row, noise events collapsed, reply text as blocks

Reads a local logs dir (default: simple-coding-agent-data/logs), read-only, in memory only.
Run:  uv run python prototype/attempt_log/app.py [LOGS_DIR]   ->  http://127.0.0.1:8765
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

LOGS = Path(
    sys.argv[1] if len(sys.argv) > 1 else "/Volumes/Sources/python/ai/simple-coding-agent-data/logs"
)
PAGE_SIZE = 200
PREVIEW_CAP = 256 * 1024
VARIANTS = {"A": "Flat table, inline expand", "B": "Master-detail", "C": "Exchanges"}

ISSUE_RE = re.compile(r"^\d{1,9}$")
RUN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(\.\d+)?Z$")
FILE_RE = re.compile(r"^\d{4,}_(tool_call\.json|tool_result\.json|model_response\.txt)$")
REF_RE = re.compile(
    r"^(?P<tool>.*?)(?: \[(?P<thread>main|subagent:[^\]]+)\])? -> "
    r"(?P<file>\d{4,}_(?:tool_call\.json|tool_result\.json|model_response\.txt))"
)
NOISE = {"limits_checked", "model_usage_shape", "token_budget_reconciled"}

app = FastAPI()


def run_dir(issue: str, run: str) -> Path | None:
    if not (ISSUE_RE.match(issue) and RUN_RE.match(run)):
        return None
    p = LOGS / issue / run
    return p if p.is_dir() else None


def load_attempt(p: Path) -> dict:
    try:
        return json.loads((p / "attempt.json").read_text())
    except (OSError, ValueError):
        return {}


def load_events(p: Path) -> list[dict]:
    out = []
    f = p / "agent_output.json"
    if not f.exists():
        return out
    for i, line in enumerate(f.read_text(errors="replace").splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            ev = {"event": "unparsable_line", "level": "WARNING", "detail": line[:200]}
        ev["i"] = i
        m = REF_RE.match(ev.get("detail") or "")
        if ev.get("event") in ("tool_call", "tool_result", "model_response") and m:
            ev["tool"], ev["thread"], ev["file"] = m["tool"], m["thread"], m["file"]
        out.append(ev)
    # pair call and result FIFO per (tool, thread): results carry no call id
    pending: dict[tuple, deque] = defaultdict(deque)
    by_i = {e["i"]: e for e in out}
    for e in out:
        if "file" not in e:
            continue
        key = (e["tool"], e["thread"])
        if e["event"] == "tool_call":
            pending[key].append(e["i"])
        elif e["event"] == "tool_result" and pending[key]:
            c = by_i[pending[key].popleft()]
            c["result"], e["call"] = e, c
    return out


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def hms(ts: str | None) -> str:
    return (ts or "")[11:19]


# ---------------------------------------------------------------- shared chrome

CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#d7dae0;--dim:#8a91a0;--acc:#6aa9ff;
--warn:#e0b04a;--err:#f06a6a;--ok:#5cc08a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;padding-bottom:70px}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline}
header h1{font-size:14px;margin:0}.dim{color:var(--dim)}
.wrap{padding:12px 16px}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:3px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:normal;position:sticky;top:0;background:var(--bg)}
.ERROR{color:var(--err)}.WARNING{color:var(--warn)}.INFO{color:var(--fg)}
.pill{padding:0 6px;border:1px solid var(--line);border-radius:9px;color:var(--dim)}
.tool{color:var(--acc)}.sub{color:var(--warn)}
.filters{display:flex;gap:10px;margin:0 0 10px;flex-wrap:wrap;align-items:center}
select,input,button{background:var(--panel);color:var(--fg);border:1px solid var(--line);
padding:3px 6px;font:inherit;border-radius:4px}button{cursor:pointer}
pre{margin:0;white-space:pre-wrap;word-break:break-word;background:var(--panel);
padding:8px;border:1px solid var(--line);border-radius:4px;max-height:420px;overflow:auto}
.trunc{color:var(--warn);margin:4px 0}.paging{margin:10px 0;display:flex;gap:12px}
.active{background:#1c2a1f}.row-open{cursor:pointer}
#switcher{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:#fff;color:#111;
border-radius:20px;padding:6px 14px;display:flex;gap:14px;align-items:center;
box-shadow:0 4px 18px #0008;font:600 13px system-ui;z-index:99}
#switcher a{color:#111;font-size:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px}
.grid2{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:12px;align-items:start}
.sticky{position:sticky;top:8px;max-height:calc(100vh - 90px);overflow:auto}
.sel{background:#1b2535}.msg{border-left:3px solid var(--acc);padding:4px 10px;margin:8px 0;
background:var(--panel)}.ex{border:1px solid var(--line);border-radius:6px;margin:6px 0;background:var(--panel)}
.ex>summary{padding:5px 10px;cursor:pointer;list-style:none}.ex .body{padding:0 10px 10px;
display:grid;grid-template-columns:1fr 1fr;gap:8px}.ex h4{margin:6px 0 2px;color:var(--dim);font-weight:normal}
.collapsed{color:var(--dim);padding:2px 10px;font-size:12px}
.tabs a{margin-right:10px}.tabs .on{color:var(--fg);font-weight:bold}
"""

JS = """
const q=(s,r=document)=>r.querySelector(s);
async function loadFile(btn){
  const url=btn.dataset.url, target=document.getElementById(btn.dataset.target);
  if(target.dataset.open==='1'&&btn.dataset.toggle==='1'){target.innerHTML='';target.dataset.open='0';return}
  target.innerHTML='<span class=dim>loading...</span>';
  const r=await fetch(url); const total=r.headers.get('x-total-bytes'); const tr=r.headers.get('x-truncated')==='1';
  let t=await r.text(); const raw=btn.dataset.raw==='1';
  if(!raw && url.includes('.json')){try{t=JSON.stringify(JSON.parse(t),null,2)}catch(e){}}
  const pre=document.createElement('pre'); pre.textContent=t;
  target.innerHTML='';
  const bar=document.createElement('div'); bar.className='dim';
  bar.innerHTML=(tr?'<span class=trunc>truncated at 256 KB, '+total+' B total</span> ':total+' B ')
    +' <a href="'+url+'&download=1">download</a> <a href="#" data-r=1>'+(raw?'formatted':'raw')+'</a>';
  bar.querySelector('[data-r]').onclick=e=>{e.preventDefault();btn.dataset.raw=raw?'0':'1';target.dataset.open='0';loadFile(btn)};
  target.append(bar,pre); target.dataset.open='1';
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-url]');if(b){e.preventDefault();loadFile(b)}});
document.addEventListener('keydown',e=>{
  if(/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName))return;
  const s=document.getElementById('switcher'); if(!s)return;
  if(e.key==='ArrowLeft')location=s.dataset.prev; if(e.key==='ArrowRight')location=s.dataset.next;
});
"""


def page(title: str, body: str, crumbs: str = "", switcher: str = "") -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{esc(title)}</title><style>{CSS}</style>"
        f"<header><h1>PROTOTYPE attempt log</h1><span class=dim>{crumbs}</span></header>"
        f"<div class=wrap>{body}</div>{switcher}<script>{JS}</script>"
    )


def switcher(request_params: dict, current: str) -> str:
    keys = list(VARIANTS)
    i = keys.index(current)

    def url(k):
        return "?" + urlencode({**request_params, "variant": k})

    return (
        f"<div id=switcher data-prev='{url(keys[i - 1])}' data-next='{url(keys[(i + 1) % len(keys)])}'>"
        f"<a href='{url(keys[i - 1])}'>&larr;</a><span>{current} ({VARIANTS[current]})</span>"
        f"<a href='{url(keys[(i + 1) % len(keys)])}'>&rarr;</a></div>"
    )


# ---------------------------------------------------------------- attempts list


@app.get("/", response_class=HTMLResponse)
def attempts():
    rows = []
    for issue_dir in sorted(LOGS.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
        if not issue_dir.is_dir() or not ISSUE_RE.match(issue_dir.name):
            continue
        for rd in sorted(issue_dir.iterdir(), reverse=True):
            if not RUN_RE.match(rd.name):
                continue
            a = load_attempt(rd)
            has_events = (rd / "agent_output.json").exists()
            outcome = a.get("outcome") or ("in progress / crashed" if has_events else "-")
            cls = "active" if not a.get("outcome") and has_events else ""
            dur = a.get("duration_seconds")
            usage = a.get("model_usage") or {}
            cost = sum(v.get("costUSD", 0) for v in usage.values()) if usage else None
            pr = (a.get("pull_request") or {}).get("number")
            rows.append(
                f"<tr class='{cls}'><td><a href='/logs/{issue_dir.name}/{rd.name}'>#{issue_dir.name}</a></td>"
                f"<td>{esc(a.get('issue_title', ''))}</td><td>{esc(a.get('started_at', rd.name))}</td>"
                f"<td>{esc(outcome)}</td><td>{f'{dur:.0f}s' if dur else ''}</td>"
                f"<td>{f'${cost:.2f}' if cost else ''}</td><td>{f'PR {pr}' if pr else ''}</td>"
                f"<td class=dim>{'events' if has_events else 'no events'}</td></tr>"
            )
    body = (
        "<table><tr><th>Issue<th>Title<th>Started<th>Outcome<th>Duration<th>Cost<th>PR<th></tr>"
        + "".join(rows)
        + "</table>"
    )
    return page("Attempts", body, "instances / <b>demo</b> / logs")


# ---------------------------------------------------------------- run page


def filtered(events, level, event, q):
    out = []
    for e in events:
        if level and e.get("level") != level:
            continue
        if event and e.get("event") != event:
            continue
        if q and q.lower() not in (e.get("detail") or "").lower():
            continue
        out.append(e)
    return out


def open_btn(issue, run, e, target, label="open", toggle=True):
    url = f"/file?issue={issue}&run={run}&name={e['file']}"
    return (
        f"<a href='#' data-url='{url}' data-target='{target}' data-toggle='{int(toggle)}'>{label}</a>"
    )


def tool_label(e):
    t = esc(e.get("tool") or "")
    th = e.get("thread") or ""
    sub = f" <span class=sub>[{esc(th)}]</span>" if th.startswith("subagent") else ""
    return f"<span class=tool>{t}</span>{sub}"


def ev_row_A(issue, run, e):
    has_file = "file" in e
    detail = esc(e.get("detail", ""))
    if has_file:
        kind = e["event"]
        action = open_btn(issue, run, e, f"ev{e['i']}")
        pair = ""
        other = e.get("result") or e.get("call")
        if other:
            pair = " " + open_btn(
                issue, run, other, f"ev{e['i']}p", "open " + ("result" if e.get("result") else "call"), True
            )
        detail = (
            f"{tool_label(e) if kind != 'model_response' else '<span class=tool>reply</span>'}"
            f" <span class=dim>{esc(e['file'])}</span> {action}{pair}"
            f"<div id=ev{e['i']}></div><div id=ev{e['i']}p></div>"
        )
    return (
        f"<tr><td class=dim>{e['i']}</td><td>{hms(e.get('timestamp'))}</td>"
        f"<td class={e.get('level')}>{esc(e.get('level'))}</td><td>{esc(e.get('event'))}</td>"
        f"<td>{detail}</td></tr>"
    )


def filters_form(params, events):
    names = sorted({e.get("event", "") for e in events})
    opt = "".join(
        f"<option{' selected' if params.get('event') == n else ''}>{esc(n)}</option>" for n in names
    )
    lv = "".join(
        f"<option{' selected' if params.get('level') == n else ''}>{n}</option>"
        for n in ("INFO", "WARNING", "ERROR")
    )
    return (
        f"<form class=filters method=get><input type=hidden name=variant value='{esc(params['variant'])}'>"
        f"level <select name=level onchange=this.form.submit()><option value=''>all</option>{lv}</select>"
        f"event <select name=event onchange=this.form.submit()><option value=''>all</option>{opt}</select>"
        f"text <input name=q value='{esc(params.get('q', ''))}'><button>filter</button>"
        f"<a href='?variant={params['variant']}'>reset</a></form>"
    )


def paging(params, page_no, total):
    pages = max(1, -(-total // PAGE_SIZE))
    base = {k: v for k, v in params.items() if v}
    nav = []
    if page_no > 1:
        nav.append(f"<a href='?{urlencode({**base, 'page': page_no - 1})}'>&larr; prev</a>")
    nav.append(f"<span class=dim>page {page_no}/{pages}, {total} events</span>")
    if page_no < pages:
        nav.append(f"<a href='?{urlencode({**base, 'page': page_no + 1})}'>next &rarr;</a>")
    return f"<div class=paging>{''.join(nav)}</div>"


def header_box(a, issue, run, n_events):
    if not a:
        return "<p class=dim>no attempt.json: in progress or crashed</p>"
    usage = a.get("model_usage") or {}
    cost = sum(v.get("costUSD", 0) for v in usage.values()) if usage else None
    bits = [
        f"outcome <b>{esc(a.get('outcome', '?'))}</b>",
        f"started {esc(a.get('started_at'))}",
        f"{a.get('duration_seconds', 0):.0f}s" if a.get("duration_seconds") else "",
        f"${cost:.2f}" if cost else "",
        f"v{esc(a.get('agent_version'))}" if a.get("agent_version") else "",
        f"{n_events} events",
    ]
    return f"<p>{esc(a.get('issue_title', ''))} <span class=dim>| {' | '.join(b for b in bits if b)}</span></p>"


def view_A(issue, run, events, params, page_no):
    sel = filtered(events, params["level"], params["event"], params["q"])
    chunk = sel[(page_no - 1) * PAGE_SIZE : page_no * PAGE_SIZE]
    rows = "".join(ev_row_A(issue, run, e) for e in chunk)
    return (
        filters_form(params, events)
        + paging(params, page_no, len(sel))
        + f"<table><tr><th>#<th>time<th>level<th>event<th>detail</tr>{rows}</table>"
        + paging(params, page_no, len(sel))
    )


def view_B(issue, run, events, params, page_no):
    sel = filtered(events, params["level"], params["event"], params["q"])
    chunk = sel[(page_no - 1) * PAGE_SIZE : page_no * PAGE_SIZE]
    rows = []
    for e in chunk:
        extra = ""
        if "file" in e:
            lbl = tool_label(e) if e["event"] != "model_response" else "<span class=tool>reply</span>"
            call = e if e["event"] == "tool_call" else e.get("call")
            res = e.get("result") if e["event"] == "tool_call" else (e if e["event"] == "tool_result" else None)
            urls = {
                "a": f"/file?issue={issue}&run={run}&name={call['file']}" if call else "",
                "b": f"/file?issue={issue}&run={run}&name={res['file']}" if res else "",
            }
            if e["event"] == "model_response":
                urls = {"a": f"/file?issue={issue}&run={run}&name={e['file']}", "b": ""}
            extra = (
                f"{lbl} <span class=dim>{esc(e['file'])}</span> "
                f"<a href='#' onclick=\"selectEv(this,{json.dumps(urls['a'])},{json.dumps(urls['b'])});return false\">view</a>"
            )
        else:
            extra = esc(e.get("detail", ""))[:300]
        rows.append(
            f"<tr><td>{hms(e.get('timestamp'))}</td><td class={e.get('level')}>{esc(e.get('event'))}</td>"
            f"<td>{extra}</td></tr>"
        )
    script = """<script>
async function selectEv(a,u1,u2){
  document.querySelectorAll('tr.sel').forEach(r=>r.classList.remove('sel'));a.closest('tr').classList.add('sel');
  const p=document.getElementById('detail'); p.innerHTML='';
  for(const [label,u] of [['call / reply',u1],['result',u2]]){ if(!u)continue;
    const d=document.createElement('div'); d.innerHTML='<h4 class=dim>'+label+'</h4><div></div>'; p.append(d);
    const t=d.querySelector('div'); t.id='d'+Math.random().toString(36).slice(2);
    const b=document.createElement('a'); b.dataset.url=u; b.dataset.target=t.id; b.dataset.toggle='0';
    await loadFile(b);
  }
}</script>"""
    return (
        filters_form(params, events)
        + paging(params, page_no, len(sel))
        + f"<div class=grid2><div><table><tr><th>time<th>event<th>detail</tr>{''.join(rows)}</table></div>"
        f"<div class='panel sticky' id=detail><span class=dim>Pick an event with a file: call and result "
        f"show together here.</span></div></div>{script}"
    )


def view_C(issue, run, events, params, page_no):
    items = []
    quiet = 0
    for e in events:
        if params["level"] and e.get("level") != params["level"]:
            continue
        ev = e.get("event")
        if ev == "tool_result" and e.get("call"):
            continue  # folded into its call
        if ev in NOISE and not params["event"]:
            quiet += 1
            continue
        if quiet:
            items.append(f"<div class=collapsed>... {quiet} routine events (limits, usage) hidden</div>")
            quiet = 0
        if params["event"] and ev != params["event"]:
            continue
        if params["q"] and params["q"].lower() not in (e.get("detail") or "").lower():
            continue
        if ev == "tool_call" and "file" in e:
            r = e.get("result")
            u1 = f"/file?issue={issue}&run={run}&name={e['file']}"
            u2 = f"/file?issue={issue}&run={run}&name={r['file']}" if r else ""
            secs = ""
            if r and e.get("timestamp") and r.get("timestamp"):
                from datetime import datetime

                f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
                secs = f" <span class=dim>{(f(r['timestamp']) - f(e['timestamp'])).total_seconds():.1f}s</span>"
            ok = "" if r else " <span class=WARNING>no result</span>"
            items.append(
                f"<details class=ex><summary>{hms(e.get('timestamp'))} {tool_label(e)}{secs}{ok}</summary>"
                f"<div class=body><div><h4>call</h4><div id=c{e['i']}>"
                f"<a href='#' data-url='{u1}' data-target=c{e['i']} data-toggle=0>load</a></div></div>"
                f"<div><h4>result</h4><div id=r{e['i']}>"
                + (f"<a href='#' data-url='{u2}' data-target=r{e['i']} data-toggle=0>load</a>" if u2 else "<span class=dim>none</span>")
                + "</div></div></div></details>"
            )
        elif ev == "model_response" and "file" in e:
            u = f"/file?issue={issue}&run={run}&name={e['file']}"
            items.append(
                f"<div class=msg>{hms(e.get('timestamp'))} reply <span class=dim>{esc(e.get('detail'))}</span> "
                f"<a href='#' data-url='{u}' data-target=m{e['i']} data-toggle=1>read</a><div id=m{e['i']}></div></div>"
            )
        elif ev == "tool_result":
            items.append(f"<div class=collapsed>orphan result: {esc(e.get('detail'))}</div>")
        else:
            items.append(
                f"<div class=collapsed><span class={e.get('level')}>{esc(ev)}</span> {esc(e.get('detail', ''))[:200]}</div>"
            )
    if quiet:
        items.append(f"<div class=collapsed>... {quiet} routine events hidden</div>")
    total = len(items)
    chunk = items[(page_no - 1) * PAGE_SIZE : page_no * PAGE_SIZE]
    note = "<p class=dim>Exchanges view: call and result folded together; paging counts rows.</p>"
    return (
        filters_form(params, events)
        + note
        + paging(params, page_no, total)
        + "".join(chunk)
        + paging(params, page_no, total)
    )


@app.get("/logs/{issue}/{run}", response_class=HTMLResponse)
def run_page(
    issue: str,
    run: str,
    variant: str = "A",
    level: str = "",
    event: str = "",
    q: str = "",
    page_no: int = Query(1, alias="page", ge=1),
):
    rd = run_dir(issue, run)
    if rd is None:
        return PlainTextResponse("not found", status_code=404)
    variant = variant if variant in VARIANTS else "A"
    events = load_events(rd)
    a = load_attempt(rd)
    params = {"variant": variant, "level": level, "event": event, "q": q}
    if not events:
        body = header_box(a, issue, run, 0) + "<p>layout not recognised: no agent_output.json</p>"
    else:
        view = {"A": view_A, "B": view_B, "C": view_C}[variant]
        body = header_box(a, issue, run, len(events)) + view(issue, run, events, params, page_no)
    crumbs = f"<a href='/'>instances / demo / logs</a> / #{issue} / {esc(run)}"
    sw = switcher({k: v for k, v in params.items() if v and k != "variant"}, variant)
    return page(f"#{issue} {run}", body, crumbs, sw)


# ---------------------------------------------------------------- evidence file endpoint


@app.get("/file")
def file(issue: str, run: str, name: str, download: int = 0):
    rd = run_dir(issue, run)
    if rd is None or not FILE_RE.match(name):
        return PlainTextResponse("bad request", status_code=400)
    p = rd / name
    if not p.is_file():
        return PlainTextResponse("not found", status_code=404)
    total = p.stat().st_size
    if download:
        return Response(
            p.read_bytes(),
            headers={"content-disposition": f'attachment; filename="{name}"'},
        )
    with p.open("rb") as fh:
        data = fh.read(PREVIEW_CAP)
    return Response(
        data.decode("utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
        headers={"x-total-bytes": str(total), "x-truncated": "1" if total > PREVIEW_CAP else "0"},
    )


if __name__ == "__main__":
    print(f"logs dir: {LOGS}")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
