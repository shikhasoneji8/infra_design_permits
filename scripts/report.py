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

COLORS = {"data_hall": "#3b82f6", "generator": "#ef4444", "cooling": "#06b6d4", "substation": "#f59e0b",
          "parking": "#9ca3af", "stormwater_basin": "#22c55e"}


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
                       "knobs": {k: getattr(plan, k) for k in ["it_mw", "cooling_type", "cooling_noise", "generator_tier",
                                                              "generator_enclosure", "bess_mw", "water_source"]},
                       "measured": {k: v for k, v in rv["measured"].items() if k in
                                    ["night_dba_worst_home", "day_dba_worst_home", "nox_pte_tpy", "water_gpd", "new_impervious_acres"]}})
    return {"run_id": run_id, "site_id": r["site_id"], "site_name": site.name, "status": r["status"],
            "layers": site_layers(site), "rounds": rounds}


TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>Permit harness replay</title>
<style>
body{font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;background:#0b1020;color:#e5e7eb;margin:0;padding:18px}
h1{font-size:20px;margin:0 0 6px} .sub{color:#9ca3af;font-size:13px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:1fr 420px;gap:16px}
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:14px}
svg{width:100%;height:auto;background:#0f172a;border-radius:8px}
.parcel{fill:#1e293b;stroke:#94a3b8;stroke-width:2}.home{fill:#7c2d12;stroke:#fb923c;stroke-width:0.8}
.comm{fill:#1f2937;stroke:#6b7280;stroke-width:0.8}.wet{fill:#0e7490;fill-opacity:.55;stroke:#22d3ee;stroke-width:1}
.buf{fill:none;stroke:#22d3ee;stroke-width:1;stroke-dasharray:6 4;opacity:.7}
.obj{stroke:#fff;stroke-width:0.8;transition:all .6s ease}
.pen{font-size:44px;font-weight:700}.pass{color:#22c55e}.fail{color:#ef4444}
.viol{font-size:13px;margin:6px 0;padding:6px 8px;background:#1f2937;border-radius:6px;border-left:3px solid #ef4444}
.lesson{font-size:12px;color:#a7f3d0;margin:4px 0;padding:4px 8px;background:#052e16;border-radius:6px}
.rej{font-size:12px;color:#cbd5e1;white-space:pre-wrap;max-height:150px;overflow:auto;margin-top:8px}
button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:14px;cursor:pointer;margin-right:6px}
input[type=range]{width:100%} .legend span{display:inline-block;margin-right:12px;font-size:12px}
.sw{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin-right:4px}
.knobs{font-size:12px;color:#9ca3af;margin-top:6px}
select{background:#1f2937;color:#fff;border:1px solid #374151;border-radius:6px;padding:6px}
#chart{width:100%;height:220px}
</style></head><body>
<h1>Permit harness replay</h1>
<div class="sub">A designer agent redesigns a 40 MW data center on a real NJ parcel until the code-scored permit reviewer passes it. Memory lives in MongoDB Atlas.</div>
<div class="grid">
 <div class="card">
  <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px">
   <select id="runsel"></select>
   <button onclick="play()">Play</button><button onclick="step(-1)">Prev</button><button onclick="step(1)">Next</button>
   <span id="rlabel" style="font-size:14px;color:#9ca3af"></span>
  </div>
  <input type="range" id="slider" min="1" max="1" value="1" oninput="show(+this.value)">
  <svg id="map"></svg>
  <div class="legend" style="margin-top:8px">
   <span><i class="sw" style="background:#3b82f6"></i>data hall</span><span><i class="sw" style="background:#ef4444"></i>generators</span>
   <span><i class="sw" style="background:#06b6d4"></i>cooling</span><span><i class="sw" style="background:#f59e0b"></i>substation</span>
   <span><i class="sw" style="background:#9ca3af"></i>parking</span><span><i class="sw" style="background:#22c55e"></i>stormwater basin</span>
   <span><i class="sw" style="background:#7c2d12;border:1px solid #fb923c"></i>homes</span><span><i class="sw" style="background:#0e7490"></i>wetland + buffer</span>
  </div>
 </div>
 <div class="card">
  <div id="site" style="font-size:13px;color:#9ca3af"></div>
  <div id="pen" class="pen"></div>
  <div id="knobs" class="knobs"></div>
  <div id="viols"></div>
  <div id="lessons"></div>
  <div id="rej" class="rej"></div>
 </div>
</div>
<div class="card" style="margin-top:16px"><div style="font-size:14px;margin-bottom:6px">Penalty per round (zero = permit passes)</div><svg id="chart" viewBox="0 0 800 220"></svg></div>
<script>
const RUNS = __DATA__;
const COLORS = __COLORS__;
let cur = RUNS[0], idx = 1, timer = null;
const sel = document.getElementById('runsel');
RUNS.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.run_id+' : '+r.site_name;sel.appendChild(o)});
sel.onchange=()=>{cur=RUNS[+sel.value];idx=1;init()};
function init(){document.getElementById('slider').max=cur.rounds.length;show(1);chart()}
function polys(list,cls){return list.map(p=>`<polygon class="${cls}" points="${p}"/>`).join('')}
function show(i){
  idx=i; document.getElementById('slider').value=i;
  const L=cur.layers, R=cur.rounds[i-1];
  const svg=document.getElementById('map');
  svg.setAttribute('viewBox',L.viewbox.join(' '));
  svg.innerHTML = polys(L.parcel,'parcel')+polys(L.commercial,'comm')+polys(L.homes,'home')+polys(L.wetlands,'wet')+polys(L.buffers,'buf')
    + R.objects.map(o=>`<polygon class="obj" points="${o.pts}" fill="${COLORS[o.kind]}"><title>${o.id}</title></polygon>`).join('')
    + `<text x="${L.viewbox[0]+8}" y="${L.viewbox[1]+22}" fill="#e5e7eb" font-size="16">N ↑   round ${R.round}   penalty ${R.penalty}</text>`;
  document.getElementById('rlabel').textContent=`round ${R.round} of ${cur.rounds.length}`;
  document.getElementById('site').textContent=cur.site_name+'  ('+cur.site_id+')';
  const pen=document.getElementById('pen'); pen.textContent=(R.passed?'PERMIT PASSED  ':'penalty ')+R.penalty; pen.className='pen '+(R.passed?'pass':'fail');
  const k=R.knobs; document.getElementById('knobs').textContent=`${k.it_mw} MW IT · cooling ${k.cooling_type}/${k.cooling_noise} · gensets ${k.generator_tier}/${k.generator_enclosure} · BESS ${k.bess_mw} MW · water ${k.water_source}`;
  document.getElementById('viols').innerHTML=R.violations.map(v=>`<div class="viol"><b>${v.rule}</b> (${v.penalty}) ${v.title}<br><span style="color:#9ca3af">${v.measured}</span></div>`).join('') || '<div class="viol" style="border-color:#22c55e">No violations</div>';
  document.getElementById('lessons').innerHTML=(R.lessons_used||[]).slice(0,4).map(t=>`<div class="lesson">memory: ${t}</div>`).join('');
  document.getElementById('rej').textContent=R.rejection||'';
  chart();
}
function step(d){show(Math.min(cur.rounds.length,Math.max(1,idx+d)))}
function play(){if(timer){clearInterval(timer);timer=null;return} idx=0; timer=setInterval(()=>{if(idx>=cur.rounds.length){clearInterval(timer);timer=null;return} show(idx+1)},1400)}
function chart(){
  const svg=document.getElementById('chart'); const W=800,H=220,px=50,py=20;
  const maxR=Math.max(...RUNS.map(r=>r.rounds.length)), maxP=Math.max(10,...RUNS.flatMap(r=>r.rounds.map(x=>x.penalty)));
  const X=n=>px+(n-1)*(W-px-20)/Math.max(1,maxR-1), Y=p=>H-py-(p/maxP)*(H-2*py);
  let s=`<line x1="${px}" y1="${Y(0)}" x2="${W-20}" y2="${Y(0)}" stroke="#374151"/><text x="8" y="${Y(0)+4}" fill="#9ca3af" font-size="11">0</text><text x="8" y="${Y(maxP)+4}" fill="#9ca3af" font-size="11">${maxP}</text>`;
  const cols=['#60a5fa','#f472b6','#a3e635','#fbbf24'];
  RUNS.forEach((r,i)=>{const pts=r.rounds.map(x=>`${X(x.round)},${Y(x.penalty)}`).join(' ');
    s+=`<polyline points="${pts}" fill="none" stroke="${cols[i%4]}" stroke-width="3"/>`+r.rounds.map(x=>`<circle cx="${X(x.round)}" cy="${Y(x.penalty)}" r="4" fill="${cols[i%4]}"/>`).join('')
      +`<text x="${X(r.rounds.length)+6}" y="${Y(r.rounds[r.rounds.length-1].penalty)+4}" fill="${cols[i%4]}" font-size="12">${r.run_id} (${r.rounds.length} rounds)</text>`;
    if(r===cur){const x=r.rounds[idx-1];s+=`<circle cx="${X(x.round)}" cy="${Y(x.penalty)}" r="8" fill="none" stroke="#fff" stroke-width="2"/>`}});
  for(let n=1;n<=maxR;n++) s+=`<text x="${X(n)-3}" y="${H-4}" fill="#9ca3af" font-size="11">${n}</text>`;
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
