"""Build a self-contained HTML replay of one or more runs: top-down site plan per round
(parcel, homes, wetlands + buffers, every placed object) with the reviewer's rejection,
plus the penalty curve for all runs on one chart.

  uv run python scripts/report.py --run demo1 --run demo2            # from Atlas
  uv run python scripts/report.py --offline                          # synthetic demo, no network
Writes reports/replay.html
"""
from __future__ import annotations

import argparse
import html
import json
import pathlib

from permit_harness.plan import Plan
from permit_harness.site import Site, site_from_doc
from permit_harness.store import MongoStore, MemoryStore

OUT = pathlib.Path(__file__).resolve().parents[1] / "reports"

# USWDS-derived palette (the EPA / NJDEP look), validated for colorblind separation; parking is hatched.
COLORS = {"data_hall": "#005ea2", "generator": "#b50909", "cooling": "#0081a1", "substation": "#c2850c",
          "parking": "#8d9297", "stormwater_basin": "#2e8540"}


def _poly_points(geom) -> list[str]:
    polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    return [" ".join(f"{x:.1f},{-y:.1f}" for x, y in p.exterior.coords) for p in polys]


def site_layers(site: Site) -> dict:
    layers = {"parcel": _poly_points(site.parcel), "homes": [], "commercial": [], "wetlands": [], "buffers": []}
    for n in site.neighbors:
        (layers["homes"] if n.is_residential else layers["commercial"]).extend(_poly_points(n.geom))
    for w in site.wetlands:
        layers["wetlands"].extend(_poly_points(w.geom))
        if w.buffer_m > 0:
            layers["buffers"].extend(_poly_points(w.geom.buffer(w.buffer_m)))
    minx, miny, maxx, maxy = site.parcel.buffer(260).bounds
    layers["viewbox"] = [minx, -maxy, maxx - minx, maxy - miny]
    return layers


def collect(store, run_id: str) -> dict:
    r = store.get_run(run_id)
    if not r:
        raise SystemExit(f"run {run_id} not found")
    site = site_from_doc(store.get_site(r["site_id"]))
    rounds = []
    for h in r["history"]:
        n = h["round"]
        d = store.get_design(f"{run_id}:r{n}")
        rv = store.get_review(run_id, n)
        plan = Plan.model_validate(d["plan"])
        objs = []
        for o in plan.objects:
            objs.append({"id": o.id, "kind": o.kind, "pts": _poly_points(o.footprint())[0]})
        rounds.append({"round": n, "penalty": rv["penalty"], "passed": rv["passed"], "objects": objs,
                       "violations": [{"rule": v["rule"], "title": v["title"], "measured": v["measured"], "penalty": v["penalty"]}
                                      for v in rv["violations"]],
                       "rejection": rv.get("rejection_text", ""), "lessons_used": d.get("lessons_used", []),
                       "knobs": {k: getattr(plan, k) for k in ["it_mw", "cooling_type", "cooling_noise", "cooling_barrier", "generator_tier",
                                                              "generator_enclosure", "bess_mw", "water_source"]},
                       "measured": {k: v for k, v in rv["measured"].items() if k in
                                    ["night_dba_worst_home", "day_dba_worst_home", "nox_pte_tpy", "water_gpd", "new_impervious_acres"]}})
    return {"run_id": run_id, "site_id": r["site_id"], "site_name": site.name, "status": r["status"],
            "layers": site_layers(site), "rounds": rounds}


TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Permit Harness Replay</title>
<style>
:root{--blue:#005ea2;--blue-dark:#1a4480;--ink:#1b1b1b;--ink2:#565c65;--line:#dfe1e2;--bg:#f0f0f0;--panel:#ffffff;--green:#2e8540;--red:#b50909;--gold:#c2850c;--surface:#fcfcfb}
*{box-sizing:border-box}
body{font-family:"Public Sans","Source Sans Pro",-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);margin:0;font-size:15px;line-height:1.45}
.gov{background:var(--blue-dark);color:#fff;padding:6px 24px;font-size:12px;letter-spacing:.02em}
header{background:var(--blue);color:#fff;padding:16px 24px}
header h1{font-size:22px;margin:0;font-weight:700}
header .sub{opacity:.9;font-size:14px;margin-top:4px}
main{padding:18px 24px;max-width:1360px;margin:0 auto}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 440px;gap:16px}
@media(max-width:980px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-top:4px solid var(--blue);padding:14px 16px}
.howto{background:#e7f6f8;border:1px solid #99deea;border-left:6px solid #00bde3;padding:12px 16px;margin-bottom:16px;position:relative}
.howto h2{font-size:16px;margin:0 0 6px}.howto ol{margin:6px 0 0 18px;padding:0}.howto li{margin:3px 0}
.howto .close{position:absolute;right:10px;top:8px;background:none;border:0;color:var(--ink2);font-size:18px;cursor:pointer;padding:2px 6px;margin:0}
svg.map{width:100%;height:auto;background:var(--surface);border:1px solid var(--line)}
.parcel{fill:#f5f1e6;stroke:#565c65;stroke-width:2}.home{fill:#f3e1c9;stroke:#a86437;stroke-width:0.8}
.comm{fill:#e6e6e6;stroke:#8d9297;stroke-width:0.8}.wet{fill:#9bd4c9;fill-opacity:.7;stroke:#168a7a;stroke-width:1}
.buf{fill:none;stroke:#168a7a;stroke-width:1;stroke-dasharray:6 4;opacity:.8}
.obj{stroke:#1b1b1b;stroke-width:0.8;transition:all .6s ease}
.maplabel{font-size:15px;fill:var(--ink)}
.pen{font-size:40px;font-weight:700;line-height:1.1;margin:4px 0}.pass{color:var(--green)}.fail{color:var(--red)}
.viol{font-size:13px;margin:6px 0;padding:6px 10px;background:#fff;border:1px solid var(--line);border-left:4px solid var(--red)}
.viol b{color:var(--red)}.viol span{color:var(--ink2)}
.viol.ok{border-left-color:var(--green)}
.lesson{font-size:12.5px;color:#1b1b1b;margin:4px 0;padding:5px 10px;background:#eaf5ea;border-left:4px solid var(--green)}
.rej{font-size:12.5px;color:var(--ink);white-space:pre-wrap;max-height:170px;overflow:auto;margin-top:10px;padding:8px 10px;background:#f9f9f9;border:1px solid var(--line);font-family:Georgia,"Times New Roman",serif}
button{background:var(--blue);color:#fff;border:0;border-radius:4px;padding:8px 14px;font-size:14px;font-weight:600;cursor:pointer;margin-right:6px}
button:hover{background:var(--blue-dark)}
input[type=range]{width:100%;accent-color:var(--blue)}
.legend span{display:inline-block;margin-right:12px;font-size:12.5px;color:var(--ink2)}
.sw{display:inline-block;width:12px;height:12px;vertical-align:middle;margin-right:4px;border:1px solid #1b1b1b}
.knobs{font-size:12.5px;color:var(--ink2);margin:4px 0 8px}
.sitename{font-size:13px;color:var(--ink2)}
select{background:#fff;color:var(--ink);border:1px solid #565c65;border-radius:4px;padding:6px;font-size:14px;max-width:420px}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:8px;flex-wrap:wrap}
#chart{width:100%;height:230px}
.charttitle{font-size:15px;font-weight:600;margin-bottom:4px}
footer{padding:14px 24px;color:var(--ink2);font-size:12px}
</style></head><body>
<div class="gov">Data center permit harness · rules cite N.J.A.C. 7:29 (noise), 7:7A (wetlands), 7:8 (stormwater), NJDEP air and water programs · demo values are marked</div>
<header><h1>Permit Harness Replay</h1><div class="sub">A designer agent redesigns a 40 MW data center on a real New Jersey parcel until a code-scored permit review passes. Every rejection is remembered in MongoDB Atlas.</div></header>
<main>
<div class="howto" id="howto">
 <button class="close" onclick="document.getElementById('howto').style.display='none'" title="hide">×</button>
 <h2>How to read this page</h2>
 <ol>
  <li><b>Pick a run</b> in the dropdown (one run = one parcel). Press <b>Play</b> to watch the rounds, or drag the slider.</li>
  <li><b>The map</b> is the parcel (cream) with neighboring homes (tan), wetlands (teal) and their legal buffers (dashed). Colored boxes are what the agent placed this round.</li>
  <li><b>The right panel</b> is the permit review for that round: the penalty score (0 = approved), each violation with the measured value, the lessons pulled from memory before designing, and the reviewer's letter.</li>
  <li><b>The chart</b> at the bottom is the penalty per round for every run. The story is the line reaching zero, and the second site getting there in fewer rounds.</li>
 </ol>
</div>
<div class="grid">
 <div class="card">
  <div class="toolbar">
   <select id="runsel"></select>
   <button onclick="play()">Play</button><button onclick="step(-1)">Prev</button><button onclick="step(1)">Next</button>
   <span id="rlabel" style="font-size:14px;color:var(--ink2)"></span>
  </div>
  <input type="range" id="slider" min="1" max="1" value="1" oninput="show(+this.value)">
  <svg id="map" class="map"></svg>
  <div class="legend" style="margin-top:8px">
   <span><i class="sw" style="background:#005ea2"></i>data hall</span><span><i class="sw" style="background:#b50909"></i>generators</span>
   <span><i class="sw" style="background:#0081a1"></i>cooling</span><span><i class="sw" style="background:#c2850c"></i>substation</span>
   <span><i class="sw" style="background:repeating-linear-gradient(45deg,#8d9297 0 2px,#fff 2px 4px)"></i>parking</span><span><i class="sw" style="background:#2e8540"></i>stormwater basin</span>
   <span><i class="sw" style="background:#f3e1c9;border-color:#a86437"></i>homes</span><span><i class="sw" style="background:#9bd4c9;border-color:#168a7a"></i>wetland + buffer</span>
  </div>
 </div>
 <div class="card">
  <div id="site" class="sitename"></div>
  <div id="pen" class="pen"></div>
  <div id="knobs" class="knobs"></div>
  <div id="viols"></div>
  <div id="lessons"></div>
  <div id="rej" class="rej"></div>
 </div>
</div>
<div class="card" style="margin-top:16px"><div class="charttitle">Penalty per round (0 = permit approved)</div><svg id="chart" viewBox="0 0 800 230"></svg></div>
</main>
<footer>Sources: NJOGIS Parcels and MOD-IV Composite; NJDEP Land Use/Land Cover 2020 wetlands. Noise propagation, buffers, setbacks, air and water thresholds are computed in code; the agents decide what to change.</footer>
<script>
const RUNS = __DATA__;
const COLORS = __COLORS__;
let cur = RUNS[0], idx = 1, timer = null;
const sel = document.getElementById('runsel');
RUNS.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.run_id+' : '+r.site_name;sel.appendChild(o)});
sel.onchange=()=>{cur=RUNS[+sel.value];idx=1;init()};
function init(){document.getElementById('slider').max=cur.rounds.length;show(1);chart()}
function polys(list,cls){return list.map(p=>`<polygon class="${cls}" points="${p}"/>`).join('')}
function objFill(o){return o.kind==='parking' ? 'url(#hatch)' : COLORS[o.kind]}
function show(i){
  idx=i; document.getElementById('slider').value=i;
  const L=cur.layers, R=cur.rounds[i-1];
  const svg=document.getElementById('map');
  svg.setAttribute('viewBox',L.viewbox.join(' '));
  svg.innerHTML = `<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#fff"/><rect width="3" height="6" fill="#8d9297"/></pattern></defs>`
    + polys(L.parcel,'parcel')+polys(L.commercial,'comm')+polys(L.homes,'home')+polys(L.wetlands,'wet')+polys(L.buffers,'buf')
    + R.objects.map(o=>`<polygon class="obj" points="${o.pts}" fill="${objFill(o)}"><title>${o.id}</title></polygon>`).join('')
    + `<text class="maplabel" x="${L.viewbox[0]+8}" y="${L.viewbox[1]+22}">N ↑   round ${R.round}   penalty ${R.penalty}</text>`;
  document.getElementById('rlabel').textContent=`round ${R.round} of ${cur.rounds.length}`;
  document.getElementById('site').textContent=cur.site_name+'  ('+cur.site_id+')';
  const pen=document.getElementById('pen'); pen.textContent=(R.passed?'PERMIT APPROVED · penalty ':'PERMIT DENIED · penalty ')+R.penalty; pen.className='pen '+(R.passed?'pass':'fail');
  const k=R.knobs; document.getElementById('knobs').textContent=`${k.it_mw} MW IT · cooling ${k.cooling_type}/${k.cooling_noise}${k.cooling_barrier?'+barrier':''} · gensets ${k.generator_tier}/${k.generator_enclosure} · BESS ${k.bess_mw} MW · water ${k.water_source}`;
  document.getElementById('viols').innerHTML=R.violations.map(v=>`<div class="viol"><b>${v.rule}</b> (${v.penalty}) ${v.title}<br><span>${v.measured}</span></div>`).join('') || '<div class="viol ok">No violations. All measured values are within limits.</div>';
  document.getElementById('lessons').innerHTML=(R.lessons_used||[]).slice(0,4).map(t=>`<div class="lesson">from memory: ${t}</div>`).join('');
  document.getElementById('rej').textContent=R.rejection||'';
  chart();
}
function step(d){show(Math.min(cur.rounds.length,Math.max(1,idx+d)))}
function play(){if(timer){clearInterval(timer);timer=null;return} idx=0; timer=setInterval(()=>{if(idx>=cur.rounds.length){clearInterval(timer);timer=null;return} show(idx+1)},1400)}
function chart(){
  const svg=document.getElementById('chart'); const W=800,H=230,px=50,py=22;
  const maxR=Math.max(...RUNS.map(r=>r.rounds.length)), maxP=Math.max(10,...RUNS.flatMap(r=>r.rounds.map(x=>x.penalty)));
  const X=n=>px+(n-1)*(W-px-150)/Math.max(1,maxR-1), Y=p=>H-py-(p/maxP)*(H-2*py);
  let s=`<line x1="${px}" y1="${Y(0)}" x2="${W-150}" y2="${Y(0)}" stroke="#a9aeb1"/><text x="10" y="${Y(0)+4}" fill="#565c65" font-size="11">0</text><text x="10" y="${Y(maxP)+4}" fill="#565c65" font-size="11">${maxP}</text>`;
  const cols=['#005ea2','#b50909','#2e8540','#c2850c'];
  RUNS.forEach((r,i)=>{const pts=r.rounds.map(x=>`${X(x.round)},${Y(x.penalty)}`).join(' ');
    s+=`<polyline points="${pts}" fill="none" stroke="${cols[i%4]}" stroke-width="2"/>`+r.rounds.map(x=>`<circle cx="${X(x.round)}" cy="${Y(x.penalty)}" r="4" fill="${cols[i%4]}" stroke="#fcfcfb" stroke-width="2"><title>${r.run_id} round ${x.round}: penalty ${x.penalty}</title></circle>`).join('')
      +`<text x="${X(r.rounds.length)+8}" y="${Y(r.rounds[r.rounds.length-1].penalty)+4}" fill="#1b1b1b" font-size="12">${r.run_id} · ${r.rounds.length} rounds</text>`;
    if(r===cur){const x=r.rounds[idx-1];s+=`<circle cx="${X(x.round)}" cy="${Y(x.penalty)}" r="8" fill="none" stroke="#1b1b1b" stroke-width="2"/>`}});
  for(let n=1;n<=maxR;n++) s+=`<text x="${X(n)-3}" y="${H-4}" fill="#565c65" font-size="11">${n}</text>`;
  svg.innerHTML=s;
}
init();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", default=[])
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default=str(OUT / "replay.html"))
    a = ap.parse_args()
    if a.offline:
        from permit_harness.loop import run
        from permit_harness.site import site_doc_from_geojson_bundle
        from permit_harness.synthetic import bundle
        store = MemoryStore()
        store.upsert_site(site_doc_from_geojson_bundle(bundle()))
        store.upsert_site(site_doc_from_geojson_bundle(bundle(site_id="syn2", homes_side="W", wetland_side="S")))
        run("synthetic_wayne", "site1", store=store, use_llm=False)
        run("syn2", "site2", store=store, use_llm=False)
        runs = ["site1", "site2"]
    else:
        store = MongoStore()
        runs = a.run or [r["_id"] for r in __import__("permit_harness.db", fromlist=["db"]).db().runs.find({}, {"_id": 1})]
    data = [collect(store, r) for r in runs]
    page = TEMPLATE.replace("__DATA__", json.dumps(data)).replace("__COLORS__", json.dumps(COLORS))
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB) for runs {runs}")


if __name__ == "__main__":
    main()
