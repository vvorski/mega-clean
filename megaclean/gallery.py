"""A browsable gallery of duplicate groups.

The page carries its data inline and reads thumbnails from a sibling folder, so
it opens straight off the filesystem with no server. Rendering is paginated in
the browser because an account this size produces thousands of groups and tens
of thousands of images.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from .dupes import Cluster
from .thumbs import thumb_path

log = logging.getLogger(__name__)


def _wasted(cluster: Cluster) -> int:
    return sum(m.size for m in cluster.members if m.key not in cluster.keeper_keys)


def build_payload(clusters: Sequence[Cluster], *,
                  thumbs_dir: Path | None = None,
                  thumb_rel: str = "thumbs") -> dict:
    def rel_thumb(key):
        if thumbs_dir is None:
            return None
        candidate = thumb_path(thumbs_dir, key)
        if not candidate.is_file():
            return None
        return f"{thumb_rel}/{candidate.parent.name}/{candidate.name}"

    groups = []
    for cluster in sorted(clusters, key=lambda c: -_wasted(c)):
        # Members of a non-variant group are the same picture, so one preview
        # stands in for all of them. Variants genuinely differ and never share.
        shared = (rel_thumb(cluster.keeper.key)
                  if cluster.kind != "variant" else None)
        exact_labels = {}
        for i, group in enumerate(cluster.exact_groups, start=1):
            for member in group:
                exact_labels[member.key] = i
        members = []
        for m in cluster.members:
            entry = {
                "id": m.key,
                "path": m.path,
                "name": m.path.rsplit("/", 1)[-1],
                "dir": m.path.rsplit("/", 1)[0] if "/" in m.path else "",
                "size": m.size,
                "keep": m.key in cluster.keeper_keys,
                "w": m.width,
                "h": m.height,
            }
            if m.key in exact_labels:
                entry["identical"] = exact_labels[m.key]
            own = rel_thumb(m.key)
            if own:
                entry["thumb"] = own
            elif shared:
                entry["thumb"] = shared
                entry["shared_thumb"] = True
            members.append(entry)
        groups.append({
            "kind": cluster.kind,
            "wasted": _wasted(cluster),
            "count": len(cluster.members),
            "members": members,
        })

    redundant = [m for c in clusters for m in c.members
                 if m.key not in c.keeper_keys]
    totals = {
        "groups": len(clusters),
        "redundant_files": len(redundant),
        "reclaimable": sum(m.size for m in redundant),
        "verified_groups": sum(1 for c in clusters if c.kind == "exact"),
        "unverified_groups": sum(1 for c in clusters if c.kind == "identical"),
        "variant_groups": sum(1 for c in clusters if c.kind == "variant"),
        "thumbs": sum(1 for g in groups for m in g["members"] if m.get("thumb")),
    }
    return {"totals": totals, "groups": groups}


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MEGA duplicates</title>
<style>
:root{--bg:#f6f6f4;--fg:#1b1b1a;--mut:#6b6b66;--card:#fff;--line:#e2e2dd;
--keep:#1a7f4b;--dup:#b23a2f;--warn:#8a6d1f;--accent:#2f6fb2}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececea;--mut:#9a9a94;
--card:#1e1e1e;--line:#2f2f2d;--keep:#4cc98a;--dup:#ef7c6e;--warn:#d9b34b;--accent:#79b0e8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);
border-bottom:1px solid var(--line);padding:14px 20px}
h1{margin:0 0 6px;font-size:17px;letter-spacing:-.01em}
.stats{color:var(--mut);font-size:13px}
.stats b{color:var(--fg);font-variant-numeric:tabular-nums}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}
input,select,button{font:inherit;padding:6px 9px;border:1px solid var(--line);
border-radius:6px;background:var(--card);color:var(--fg)}
input[type=search]{min-width:230px}
button{cursor:pointer}
button:hover{border-color:var(--accent)}
main{padding:16px 20px 60px;max-width:1500px;margin:0 auto}
.group{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 14px;margin-bottom:14px}
.ghead{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:10px}
.waste{font-weight:600;font-variant-numeric:tabular-nums}
.tag{font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);
color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.tag.exact{color:var(--keep);border-color:currentColor}
.tag.identical{color:var(--warn);border-color:currentColor}
.tag.variant{color:var(--accent);border-color:currentColor}
.files{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
figure{margin:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;
background:var(--bg);display:flex;flex-direction:column}
.shot{aspect-ratio:4/3;background:var(--line);display:flex;align-items:center;
justify-content:center;overflow:hidden}
.shot img{width:100%;height:100%;object-fit:cover;display:block}
.shot .none{color:var(--mut);font-size:11px}
figcaption{padding:7px 8px;font-size:12px;min-width:0}
.nm{font-weight:600;word-break:break-all}
.dir{color:var(--mut);font-size:11px;word-break:break-all;margin-top:2px}
.meta{margin-top:4px;color:var(--mut);font-variant-numeric:tabular-nums}
.badge{display:inline-block;font-size:10px;padding:0 5px;border-radius:3px;
margin-right:4px;text-transform:uppercase;letter-spacing:.04em}
.badge.keep{background:var(--keep);color:#fff}
.badge.dupe{background:var(--dup);color:#fff}
.badge.same{border:1px solid var(--mut);color:var(--mut)}
.pager{display:flex;gap:8px;align-items:center;justify-content:center;margin:22px 0}
.note{color:var(--mut);font-size:12px;margin:10px 0 16px;line-height:1.5}
.empty{color:var(--mut);padding:40px;text-align:center}
</style></head><body>
<header>
  <h1>MEGA duplicate groups</h1>
  <div class="stats" id="stats"></div>
  <div class="controls">
    <input type="search" id="q" placeholder="filter by path or filename">
    <select id="kind">
      <option value="">all kinds</option>
      <option value="exact">byte-verified</option>
      <option value="identical">fingerprint only</option>
      <option value="variant">variant</option>
    </select>
    <select id="sort">
      <option value="wasted">most reclaimable first</option>
      <option value="count">most copies first</option>
      <option value="path">by path</option>
    </select>
    <button id="prev">&larr; prev</button>
    <span id="pos" class="stats"></span>
    <button id="next">next &rarr;</button>
  </div>
</header>
<main>
  <p class="note" id="legend"></p>
  <div id="list"></div>
  <div class="pager">
    <button id="prev2">&larr; prev</button><span id="pos2" class="stats"></span>
    <button id="next2">next &rarr;</button>
  </div>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const PER = 60;
let page = 0, view = DATA.groups;
const el = id => document.getElementById(id);
const gb = n => n >= 1e9 ? (n/1e9).toFixed(2)+' GB'
              : n >= 1e6 ? (n/1e6).toFixed(1)+' MB'
              : n >= 1e3 ? (n/1e3).toFixed(0)+' KB' : n+' B';
const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const t = DATA.totals;
el('stats').innerHTML = `<b>${t.groups.toLocaleString()}</b> groups &middot;
  <b>${t.redundant_files.toLocaleString()}</b> redundant files &middot;
  <b>${gb(t.reclaimable)}</b> reclaimable &middot;
  <b>${t.verified_groups.toLocaleString()}</b> byte-verified,
  <b>${t.unverified_groups.toLocaleString()}</b> fingerprint-only`;
el('legend').textContent =
  'Byte-verified groups had their contents compared. Fingerprint-only groups '
  + 'match on MEGA\\'s stored checksum, which was wrong for 1 group in 80 when '
  + 'measured against this account \\u2014 treat them as candidates. The keeper '
  + 'is a suggestion (largest pixels, then largest file, then oldest). Nothing '
  + 'here has been deleted; this tool cannot delete.';

function apply(){
  const q = el('q').value.trim().toLowerCase(), k = el('kind').value, s = el('sort').value;
  view = DATA.groups.filter(g =>
    (!k || g.kind === k) &&
    (!q || g.members.some(m => m.path.toLowerCase().includes(q))));
  if (s === 'count') view = [...view].sort((a,b) => b.count - a.count || b.wasted - a.wasted);
  else if (s === 'path') view = [...view].sort((a,b) =>
    a.members[0].path.localeCompare(b.members[0].path));
  else view = [...view].sort((a,b) => b.wasted - a.wasted);
  page = 0; render();
}
function render(){
  const pages = Math.max(1, Math.ceil(view.length / PER));
  page = Math.min(page, pages - 1);
  const slice = view.slice(page*PER, page*PER + PER);
  el('list').innerHTML = slice.length ? slice.map(g => `
    <section class="group">
      <div class="ghead">
        <span class="waste">${gb(g.wasted)} reclaimable</span>
        <span class="tag ${g.kind}">${g.kind === 'exact' ? 'byte-verified'
          : g.kind === 'identical' ? 'fingerprint only' : 'variant'}</span>
        <span class="stats">${g.count} copies</span>
      </div>
      <div class="files">${g.members.map(m => `
        <figure>
          <div class="shot">${m.thumb
            ? `<img loading="lazy" src="${esc(m.thumb)}" alt="">`
            : `<span class="none">no preview</span>`}</div>
          <figcaption>
            <div>${m.keep ? '<span class="badge keep">keep</span>'
                          : '<span class="badge dupe">duplicate</span>'}
              ${m.identical ? `<span class="badge same">identical #${m.identical}</span>` : ''}
            </div>
            <div class="nm">${esc(m.name)}</div>
            <div class="dir">${esc(m.dir)}</div>
            <div class="meta">${gb(m.size)}${m.w ? ` &middot; ${m.w}\\u00d7${m.h}` : ''}</div>
          </figcaption>
        </figure>`).join('')}</div>
    </section>`).join('') : '<p class="empty">No groups match that filter.</p>';
  const label = `page ${page+1} of ${pages} \\u2014 ${view.length.toLocaleString()} groups`;
  el('pos').textContent = label; el('pos2').textContent = label;
  window.scrollTo({top:0});
}
for (const id of ['prev','prev2']) el(id).onclick = () => { page--; render(); };
for (const id of ['next','next2']) el(id).onclick = () => { page++; render(); };
el('q').oninput = apply; el('kind').onchange = apply; el('sort').onchange = apply;
apply();
</script></body></html>
"""


def _embed(payload: dict) -> str:
    """Serialise for embedding inside a <script> block.

    A filename containing "</script>" would otherwise close the block early,
    breaking the page and injecting whatever followed. Escaping the three
    characters that can start markup keeps the JSON valid and inert.
    """
    text = json.dumps(payload, separators=(",", ":"))
    return (text.replace("<", "\\u003c").replace(">", "\\u003e")
                .replace("&", "\\u0026"))


def write_gallery(clusters: Sequence[Cluster], out_dir: Path, *,
                  thumbs_dir: Path | None) -> Path:
    """Write the gallery and, if thumbnails exist, link them alongside it."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = build_payload(clusters, thumbs_dir=thumbs_dir)

    if thumbs_dir is not None:
        dest = out_dir / "thumbs"
        linked = 0
        for member in (m for g in payload["groups"] for m in g["members"]):
            rel = member.get("thumb")
            if not rel:
                continue
            source = thumb_path(thumbs_dir, member["id"])
            target = out_dir / rel
            if target.exists():
                continue
            if not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, target)      # instant, and costs no extra disk
            except OSError:
                shutil.copyfile(source, target)
            linked += 1
        log.info("linked %d thumbnails into %s", linked, dest)

    page = _PAGE.replace("__DATA__", _embed(payload))
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    return index
