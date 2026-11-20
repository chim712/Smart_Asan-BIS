#!/usr/bin/env python3
"""실시간 위치 녹화본 재생 (analysis/replay_locate.py).

record_realtime.py의 loc.jsonl을 시간 순서대로 core.realtime.locate.Tracker에 흘려
1) 판정 통계 보고서(report.txt), 2) 재생 뷰어용 데이터(replay_data.json),
3) 데이터를 넣은 뷰어(replay.html, analysis/replay_viewer.html 기반)를 만든다.

사용 예 (프로젝트 루트에서):
    python analysis/replay_locate.py data/realtime/2026-11-20_routes/loc.jsonl \\
        --graph data/snapshots/2026-11-17/graph.json --since 15:00
    python analysis/replay_locate.py .../loc.jsonl --routes .../routes.json   # 그래프 없이(선형 보정 적용)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.realtime import locate as L          # noqa: E402
from core.realtime.vehicle import classify     # noqa: E402

STATES = [L.RUNNING, L.AT_STOP, L.ORIGIN_WAIT, L.TERMINAL_WAIT, L.UNKNOWN, L.OFF_ROUTE]
SOURCES = [L.SRC_GPS, L.SRC_STOP, L.SRC_HOLD, L.SRC_NONE, L.SRC_RAW]
CHECKS = ["ok", "lat=lng", "parked", "off_road", "out_of_window", "backward",
          "no_coord", "out_of_range", "stop_unmapped", "route_unknown", "snapped"]
SUSPECT_MIN_OBS = 3        # 선형 의심 구간: 붙임(snapped) 관측 최소 건수
SUSPECT_MIN_VEH = 2        # 서로 다른 차량 최소 대수(한 차량의 GPS 오차는 제외)
COLORS = ["#d6336c", "#1c7ed6", "#2f9e44", "#e8590c", "#7048e8", "#0ca678", "#f59f00", "#495057",
          "#c2255c", "#1971c2", "#5c940d", "#d9480f"]


def _hms(text: str | None) -> str | None:
    """'15:00' → '15:00:00'."""
    if not text:
        return None
    parts = (text.split(":") + ["00", "00"])[:3]
    return ":".join(p.zfill(2) for p in parts)


def load_rows(path: Path, since: str | None, until: str | None) -> list[dict]:
    since, until = _hms(since), _hms(until)
    rows = []
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            hhmmss = r["ts"][11:19]
            if (since and hhmmss < since) or (until and hhmmss >= until):
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


def run(rows: list[dict], routes: dict[str, L.RouteLine], params: L.Params):
    tr = L.Tracker(routes, params)
    out = []          # (t, Position)
    errors = 0
    for r in rows:
        t = datetime.fromisoformat(r["ts"]).timestamp()
        if "vehicles" not in r:
            errors += 1
            continue
        for p in tr.update(r["route_id"], r["vehicles"], t):
            out.append((t, p))
        tr.expire(t)
    return out, errors


def direction_label(line: L.RouteLine) -> str:
    if not line.stops:
        return line.name
    return f"{line.name} {line.stops[0].name}→{line.stops[-1].name}"


def build_report(results, routes, rows, errors, src_desc: str) -> tuple[str, list[dict]]:
    lines: list[str] = []
    w = lines.append
    regions = {}
    for _, p in results:
        if p.veh_id not in regions:
            regions[p.veh_id] = classify(p.veh_id, p.extra.get("plate_no", ""))
    t0 = rows[0]["ts"][11:19] if rows else "-"
    t1 = rows[-1]["ts"][11:19] if rows else "-"
    w("# 실시간 위치 재생 보고서")
    w(f"노선 데이터: {src_desc}")
    w(f"녹화 구간: {t0} ~ {t1}, 호출 {len(rows)}회(오류 {errors}), 관측 {len(results)}건, 차량 {len(regions)}대")
    w("")

    # 1. 지역별 판정
    by_reg = defaultdict(list)
    for _, p in results:
        by_reg[regions[p.veh_id].region or "?"].append(p)
    w("## 1. 지역별 좌표 판정·위치 근거·상태")
    for reg, ps in sorted(by_reg.items()):
        n = len(ps)
        cc = Counter(p.check for p in ps)
        sc = Counter(p.source for p in ps)
        st = Counter(p.state for p in ps)
        coords = n - cc["lat=lng"] - cc["no_coord"] - cc["out_of_range"]
        acc = cc["ok"] + cc["snapped"]
        w(f"- {reg}: {n}건, 형식 정상 좌표 {coords}건 중 GPS 채택 {acc}건({acc / max(1, coords):.1%}) "
          f"= 30m 이내 {cc['ok']} + 30~80m 붙임 {cc['snapped']}")
        w("  - 판정: " + ", ".join(f"{k} {v}({v / n:.1%})" for k, v in cc.most_common()))
        w("  - 근거: " + ", ".join(f"{k} {v}({v / n:.1%})" for k, v in sc.most_common()))
        w("  - 상태: " + ", ".join(f"{k} {v}({v / n:.1%})" for k, v in st.most_common()))
    w("")

    # 2. 위치 정체
    w("## 2. 새 정보 없이 지난 시간(info_age_s, 운행 중만)")
    for reg, ps in sorted(by_reg.items()):
        ages = [p.info_age_s for p in ps if p.state == L.RUNNING]
        stale = sum(p.stale for p in ps)
        w(f"- {reg}: 중앙값 {pct(ages, .5):.0f}초, 90% {pct(ages, .9):.0f}초, 99% {pct(ages, .99):.0f}초, "
          f"최대 {max(ages, default=0):.0f}초, stale 표시 {stale}건")
    w("")

    # 3. 불변식
    last, backward, unmapped = {}, 0, 0
    seen_at = defaultdict(dict)   # veh -> route -> 마지막 시각
    dup = Counter()
    for t, p in results:
        key = (p.veh_id, p.route_id)
        if p.s is not None and key in last and not p.route_changed and p.s < last[key] - 1e-6:
            backward += 1
        if p.s is not None:
            last[key] = p.s
        unmapped += p.check in ("stop_unmapped", "route_unknown")
        for rid, ts in seen_at[p.veh_id].items():
            if rid != p.route_id and t - ts < 30:
                dup[(p.veh_id, rid, p.route_id)] += 1
        seen_at[p.veh_id][p.route_id] = t
    w("## 3. 불변식")
    w(f"- 표시 위치가 뒤로 간 횟수: {backward} (0이어야 함)")
    w(f"- 매핑 실패(정류소·노선 없음): {unmapped}")
    def lab(rid):
        return f"{routes[rid].name}({routes[rid].stops[0].name[:8]}발)" if rid in routes else rid
    w(f"- 같은 차량이 30초 안에 두 노선에 나온 경우(회차 직후 겹침): {sum(dup.values())}건"
      + (" — " + ", ".join(f"{v[-4:]} {lab(a)}→{lab(b)} {n}회" for (v, a, b), n in dup.most_common(8)) if dup else ""))
    w("")

    # 4. 좌표가 버려진 곳
    w("## 4. 좌표가 버려진 곳 (off_road / out_of_window / backward, 기종점 제외)")
    fail = Counter()
    fail_veh = defaultdict(Counter)
    for _, p in results:
        if p.check in ("off_road", "out_of_window", "backward") and p.state not in (L.ORIGIN_WAIT, L.TERMINAL_WAIT):
            line = routes[p.route_id]
            k = (line.name, p.route_id, line.stops[p.reported_idx].name, p.check)
            fail[k] += 1
            fail_veh[k][p.veh_id[-4:]] += 1
    for k, n in fail.most_common(15):
        w(f"- {n:3d}건  {k[0]} ({k[1]}) {k[2]} [{k[3]}]  차량 " +
          ", ".join(f"{v}×{c}" for v, c in fail_veh[k].most_common(3)))
    if not fail:
        w("- 없음")
    w("")

    # 5. 차량별 운행 순서
    w("## 5. 차량별 노선 순서 (첫 관측 시각 노선[보고 정류소 범위])")
    by_veh = defaultdict(list)
    for t, p in results:
        by_veh[p.veh_id].append((t, p))
    for veh, items in sorted(by_veh.items(), key=lambda kv: kv[0][-4:]):
        segs, cur = [], None
        for t, p in items:
            if p.reported_idx is None:
                continue
            if cur is None or cur["rid"] != p.route_id or p.route_changed:
                cur = {"rid": p.route_id, "t": t, "lo": p.reported_idx, "hi": p.reported_idx}
                segs.append(cur)
            cur["hi"] = max(cur["hi"], p.reported_idx)
        vi = regions[veh]
        desc = " → ".join(
            f"{datetime.fromtimestamp(s['t']).strftime('%H:%M')} {routes[s['rid']].name}"
            f"({routes[s['rid']].stops[0].name[:8]}발)"
            f"[{s['lo']}-{s['hi']}/{routes[s['rid']].last_idx}]" for s in segs)
        w(f"- {veh[-4:]} {vi.company or '?'}: {desc}")
    w("")

    # 6. 기종점 대기
    w("## 6. 기점·종점 대기 시간(분, 관측된 범위)")
    dwell = defaultdict(list)
    run_start = {}
    for t, p in results:
        key = (p.veh_id, p.route_id, p.state)
        if p.state in (L.ORIGIN_WAIT, L.TERMINAL_WAIT):
            run_start.setdefault(key, [t, t])[1] = t
        for st_ in (L.ORIGIN_WAIT, L.TERMINAL_WAIT):
            k2 = (p.veh_id, p.route_id, st_)
            if st_ != p.state and k2 in run_start:
                a, b = run_start.pop(k2)
                dwell[(routes[p.route_id].name, p.route_id, st_)].append(round((b - a) / 60, 1))
    for k, (a, b) in run_start.items():   # 녹화가 끝날 때까지 대기 중이던 차량
        dwell[(routes[k[1]].name, k[1], k[2])].append(round((b - a) / 60, 1))
    for k, v in sorted(dwell.items()):
        label = "기점 대기" if k[2] == L.ORIGIN_WAIT else "종점 대기"
        w(f"- {k[0]} ({k[1]}) {label}: {sorted(v)}")

    # 책갈피: 좌표가 3회 이상 연달아 버려진 장면
    marks = []
    streak = {}
    for t, p in results:
        bad = p.check in ("off_road", "out_of_window", "backward") and p.state not in (L.ORIGIN_WAIT, L.TERMINAL_WAIT)
        key = (p.veh_id, p.route_id)
        if bad:
            s = streak.setdefault(key, {"t0": t, "n": 0, "check": p.check, "stop": p.reported_idx})
            s["n"] += 1
            s["t1"] = t
        elif key in streak:
            s = streak.pop(key)
            if s["n"] >= 3:
                marks.append({**s, "veh": p.veh_id, "rid": p.route_id})
    for (veh, rid), s in streak.items():
        if s["n"] >= 3:
            marks.append({**s, "veh": veh, "rid": rid})
    marks.sort(key=lambda m: m["t0"])
    w("")
    w(f"## 7. 책갈피 (좌표가 3회 이상 연달아 버려진 장면) {len(marks)}개")
    for m in marks:
        line = routes[m["rid"]]
        w(f"- {datetime.fromtimestamp(m['t0']).strftime('%H:%M:%S')}~{datetime.fromtimestamp(m['t1']).strftime('%H:%M:%S')} "
          f"{m['veh'][-4:]} {line.name} {line.stops[m['stop']].name} [{m['check']}] {m['n']}회")

    # 8. 경로 이탈(GPS 위치 오류) 구간
    w("")
    off_runs, cur = [], {}
    for t, p in results:
        key = (p.veh_id, p.route_id)
        if p.state == L.OFF_ROUTE:
            r = cur.setdefault(key, {"veh": p.veh_id, "rid": p.route_id, "t0": t, "n": 0, "dmax": 0.0,
                                     "stop": p.reported_idx})
            r["t1"], r["n"] = t, r["n"] + 1
            r["dmax"] = max(r["dmax"], p.raw_dist_m or 0.0)
        elif key in cur:
            off_runs.append(cur.pop(key))
    off_runs += list(cur.values())
    off_runs.sort(key=lambda r: r["t0"])
    w(f"## 8. 경로 이탈(화면 표기 'GPS 위치 오류', 80m 밖 좌표 3회 연속부터) {len(off_runs)}구간")
    for r in off_runs:
        line = routes[r["rid"]]
        w(f"- {datetime.fromtimestamp(r['t0']).strftime('%H:%M:%S')}~{datetime.fromtimestamp(r['t1']).strftime('%H:%M:%S')} "
          f"{r['veh'][-4:]} {line.name} {line.stops[r['stop']].name} 부근, {r['n']}회, 선형에서 최대 {r['dmax']:.0f}m")
    if not off_runs:
        w("- 없음")

    # 9. 선형 의심 구간: 30~80m로 붙인 좌표가 여러 차량에서 같은 구간에 몰린 곳
    w("")
    suspects = shape_suspects(results, routes)
    w(f"## 9. 선형 의심 구간 (붙임 {SUSPECT_MIN_OBS}건 이상, 차량 {SUSPECT_MIN_VEH}대 이상) {len(suspects)}곳")
    w("시 선형이 실제 도로와 어긋났을 가능성이 있는 구간이다. 지금은 보정하지 않고 기록만 한다.")
    for sp in suspects[:20]:
        w(f"- 구간 {sp['segment_id']}: 붙임 {sp['obs']}건, 차량 {sp['vehicles']}대, 선형까지 중앙값 {sp['median_dist_m']}m, "
          f"노선 {', '.join(sp['route_names'])}, 부근 {sp['near_stop']} ({sp['lat']}, {sp['lng']})")
    if not suspects:
        w("- 없음")
    return "\n".join(lines) + "\n", marks


def shape_suspects(results, routes) -> list[dict]:
    """붙임(snapped) 관측을 구간별로 모은다. 여러 차량에서 반복되면 선형 오차일 가능성이 크다."""
    by_seg = defaultdict(list)
    for _, p in results:
        if p.check == "snapped" and p.raw_segment_id is not None:
            by_seg[p.raw_segment_id].append(p)
    out = []
    for seg_id, ps in by_seg.items():
        vehs = {p.veh_id for p in ps}
        if len(ps) < SUSPECT_MIN_OBS or len(vehs) < SUSPECT_MIN_VEH:
            continue
        mid = sorted(ps, key=lambda p: p.raw_s)[len(ps) // 2]
        line = routes[mid.route_id]
        lat, lng = line.point_at(mid.raw_s)
        rnames = sorted({routes[p.route_id].name for p in ps})
        out.append({
            "segment_id": seg_id, "obs": len(ps), "vehicles": len(vehs),
            "median_dist_m": round(sorted(p.raw_dist_m for p in ps)[len(ps) // 2], 1),
            "route_ids": sorted({p.route_id for p in ps}), "route_names": rnames,
            "near_stop": line.stops[line.stop_at(mid.raw_s)].name,
            "lat": round(lat, 6), "lng": round(lng, 6),
        })
    out.sort(key=lambda sp: (-sp["vehicles"], -sp["obs"]))
    return out


def build_viewer_data(results, routes, marks, src_desc: str) -> dict:
    rids = sorted({p.route_id for _, p in results if p.route_id in routes},
                  key=lambda r: (routes[r].name, r))
    r_index = {r: i for i, r in enumerate(rids)}
    vehs = sorted({p.veh_id for _, p in results}, key=lambda v: v[-4:])
    v_index = {v: i for i, v in enumerate(vehs)}
    t0 = min(t for t, _ in results) if results else 0
    plates = {}
    lows = {}
    for _, p in results:
        plates.setdefault(p.veh_id, p.extra.get("plate_no", ""))
        lows.setdefault(p.veh_id, p.extra.get("low_flag") == "1")
    vinfo = []
    for v in vehs:
        c = classify(v, plates[v])
        vinfo.append({"id": v, "no": v[-4:], "plate": plates[v], "region": c.region, "company": c.company,
                      "low": lows[v]})
    rinfo = []
    for i, r in enumerate(rids):
        line = routes[r]
        pts = [line.point_at(0.0)] + [L.to_latlng(x, y) for x, y in line.xy[1:]]
        rinfo.append({"id": r, "name": line.name, "label": direction_label(line), "color": COLORS[i % len(COLORS)],
                      "line": [[round(a, 6), round(b, 6)] for a, b in pts],
                      "stops": [[st.name, round(st.lat, 6), round(st.lng, 6)] for st in line.stops]})
    sc = {s: i for i, s in enumerate(STATES)}
    oc = {s: i for i, s in enumerate(SOURCES)}
    cc = {s: i for i, s in enumerate(CHECKS)}
    frames = []
    for t, p in results:
        if p.route_id not in r_index or p.lat is None:
            continue
        raw_ok = p.raw_lat is not None and p.raw_lat != p.raw_lng
        frames.append([round(t - t0, 1), v_index[p.veh_id], r_index[p.route_id], round(p.lat, 7), round(p.lng, 7),
                       round(p.raw_lat, 6) if raw_ok else None, round(p.raw_lng, 6) if raw_ok else None,
                       sc[p.state], oc[p.source], p.stop_idx, p.reported_idx, p.info_age_s,
                       cc.get(p.check, len(CHECKS) - 1), p.raw_dist_m])
    bm = [{"t": round(m["t0"] - t0, 1), "t1": round(m["t1"] - t0, 1), "veh": v_index[m["veh"]],
           "route": r_index.get(m["rid"]), "check": m["check"], "n": m["n"],
           "stop": routes[m["rid"]].stops[m["stop"]].name} for m in marks if m["veh"] in v_index]
    return {"meta": {"t0": t0, "source": src_desc, "generated": datetime.now().isoformat(timespec="seconds"),
                     "fields": ["t", "veh", "route", "lat", "lng", "raw_lat", "raw_lng", "state", "source",
                                "stop_idx", "reported_idx", "info_age", "check", "raw_dist"],
                     "states": STATES, "sources": SOURCES, "checks": CHECKS},
            "routes": rinfo, "vehicles": vinfo, "frames": frames, "bookmarks": bm}


def main() -> int:
    ap = argparse.ArgumentParser(description="실시간 위치 녹화본 재생")
    ap.add_argument("loc", type=Path, help="record_realtime.py가 만든 loc.jsonl")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--graph", type=Path, help="build_city_graph 출력 graph.json (권장)")
    src.add_argument("--routes", type=Path, help="녹화 폴더의 routes.json (선형 보정 후 사용)")
    ap.add_argument("--since", help="HH:MM[:SS] 이후만")
    ap.add_argument("--until", help="HH:MM[:SS] 이전만")
    ap.add_argument("--out-dir", type=Path, help="출력 폴더 (기본: loc.jsonl 옆 replay/)")
    ap.add_argument("--viewer", type=Path, default=Path(__file__).with_name("replay_viewer.html"),
                    help="뷰어 템플릿")
    ap.add_argument("--no-html", action="store_true")
    args = ap.parse_args()

    rows = load_rows(args.loc, args.since, args.until)
    if not rows:
        print("재생할 기록이 없습니다.", file=sys.stderr)
        return 1
    rids = {r["route_id"] for r in rows}
    if args.graph:
        with open(args.graph, encoding="utf-8") as fp:
            graph = json.load(fp)
        routes = L.routes_from_city_graph(graph, rids)
        src_desc = f"graph.json ({graph.get('graph_hash', '')[:12]}, 수집 {graph.get('meta', {}).get('source_crawled_at', '?')})"
    else:
        from core.graph.fix_shape import fix
        with open(args.routes, encoding="utf-8") as fp:
            routes = L.routes_from_snapshot(json.load(fp), fix_fn=fix)
        src_desc = "routes.json (선형 보정 적용)"
    missing = rids - set(routes)
    if missing:
        print(f"경고: 노선 데이터에 없는 route_id {sorted(missing)}", file=sys.stderr)

    results, errors = run(rows, routes, L.Params())
    report, marks = build_report(results, routes, rows, errors, src_desc)
    out_dir = args.out_dir or args.loc.parent / "replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.txt").write_text(report, encoding="utf-8")
    data = build_viewer_data(results, routes, marks, src_desc)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "replay_data.json").write_text(payload, encoding="utf-8")
    (out_dir / "shape_suspects.json").write_text(
        json.dumps({"source": src_desc, "generated": datetime.now().isoformat(timespec="seconds"),
                    "min_obs": SUSPECT_MIN_OBS, "min_vehicles": SUSPECT_MIN_VEH,
                    "suspects": shape_suspects(results, routes)}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(report)
    if not args.no_html:
        tpl = args.viewer.read_text(encoding="utf-8")
        marker = "/*__REPLAY_DATA__*/null"
        if marker not in tpl:
            print(f"뷰어 템플릿에 자리표시자 {marker}가 없습니다: {args.viewer}", file=sys.stderr)
            return 1
        html = tpl.replace(marker, payload.replace("</", "<\\/"))
        (out_dir / "replay.html").write_text(html, encoding="utf-8")
    print(f"저장: {out_dir} (report.txt, replay_data.json, shape_suspects.json{'' if args.no_html else ', replay.html'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
