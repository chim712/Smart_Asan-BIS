#!/usr/bin/env python3
"""⑤ 웹용 정적 데이터 생성.

DB의 활성 버전(v_* 뷰)을 읽어 브라우저가 한 번에 받아 쓰는 JSON을 만든다.
  network.<hash>.json : 구간(평면 좌표 점열, 3m 단순화)과 분기점
  routes.<hash>.json  : 노선 → 구간 순서, 정차 정류소, 기본정보·첫차·막차
  stops.<hash>.json   : 정류소
  manifest.json       : 버전, 계열 이름·색, 위 파일 이름

좌표는 core.geo.xy(origin=(LAT0, LNG0)) 평면 m 정수다(x=동쪽, y=북쪽). 63bit ID는 JS 숫자 범위를
넘으므로 파일 안의 상호 참조는 배열 인덱스로 하고 원래 ID는 문자열로 둔다.
필터(권역·노선 선택)는 브라우저가 이 색인으로 처리한다.

    python -m core.web.export                         # → frontend/public/data
    python -m core.web.export --out deploy/www/data --tol 3
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pymysql

from core.config import ROOT, db_params
from core.geo import LAT0, LNG0, simplify, xy

ORIGIN = (LAT0, LNG0)
DEFAULT_OUT = ROOT / "frontend" / "public" / "data"


def parse_linestring(wkt: str) -> list[tuple[float, float]]:
    """'LINESTRING(lng lat,...)' → [(lat, lng), ...]"""
    body = wkt[wkt.index("(") + 1:wkt.rindex(")")]
    out = []
    for pair in body.split(","):
        lng, lat = pair.split()
        out.append((float(lat), float(lng)))
    return out


def to_xy_int(p) -> list[int]:
    x, y = xy(p, ORIGIN)
    return [round(x), round(y)]


def fetch(conn) -> dict:
    cur = conn.cursor()

    def q(sql):
        cur.execute(sql)
        return cur.fetchall()

    ver = q("SELECT v.version_id, v.data_hash, v.source_crawled_at FROM current_version c "
            "JOIN graph_version v ON v.version_id = c.version_id")
    if not ver:
        raise SystemExit("활성 버전이 없습니다. 먼저 python -m core.db.load_graph 로 적재하세요.")
    return {
        "version": ver[0],
        "groups": q("SELECT group_code, label, color FROM route_group_def ORDER BY sort_order"),
        "routes": q("SELECT route_id, route_name, route_type, group_code, st_stop_name, ed_stop_name, "
                    "trips_weekday, trips_total FROM v_route ORDER BY route_name, route_id"),
        "nodes": q("SELECT node_id, lat, lng, in_deg, out_deg FROM v_node ORDER BY node_id"),
        "segments": q("SELECT segment_id, from_node, to_node, length_m, trips_weekday, ST_AsText(geom) "
                      "FROM v_segment ORDER BY segment_id"),
        "stops": q("SELECT stop_id, name, lat, lng FROM v_stop ORDER BY stop_id"),
        "route_segment": q("SELECT route_id, seq, segment_id FROM v_route_segment ORDER BY route_id, seq"),
        "route_stop": q("SELECT route_id, ord, stop_id, segment_id, offset_m FROM v_route_stop "
                        "ORDER BY route_id, ord"),
        "timetable": q("SELECT route_id, departure, weekday FROM v_route_timetable ORDER BY route_id, seq"),
    }


def build(d: dict, tol: float) -> dict:
    node_ix = {r[0]: i for i, r in enumerate(d["nodes"])}
    seg_ix = {r[0]: i for i, r in enumerate(d["segments"])}
    stop_ix = {r[0]: i for i, r in enumerate(d["stops"])}
    route_ix = {r[0]: i for i, r in enumerate(d["routes"])}

    nodes = [{"id": str(nid), "x": to_xy_int((lat, lng))[0], "y": to_xy_int((lat, lng))[1],
              "in": i_d, "out": o_d} for nid, lat, lng, i_d, o_d in d["nodes"]]

    seg_routes = defaultdict(set)
    paths = defaultdict(list)
    for rid, _seq, sid in d["route_segment"]:
        paths[rid].append(seg_ix[sid])
        seg_routes[seg_ix[sid]].add(route_ix[rid])

    segments = []
    for i, (sid, fn, tn, length, trips, wkt) in enumerate(d["segments"]):
        pts = [to_xy_int(p) for p in parse_linestring(wkt)]
        pts = [p for k, p in enumerate(pts) if k == 0 or p != pts[k - 1]]   # 정수화로 생긴 중복 제거
        if len(pts) == 1:
            pts = pts * 2
        segments.append({"id": str(sid), "from": node_ix[fn], "to": node_ix[tn], "len": round(length),
                         "trips": trips, "pts": simplify(pts, tol), "routes": sorted(seg_routes[i])})

    stops_by_route = defaultdict(list)
    for rid, _ord, stid, sid, off in d["route_stop"]:
        stops_by_route[rid].append([stop_ix[stid], seg_ix[sid], round(off)])
    deps = defaultdict(lambda: ([], []))
    for rid, dep, wk in d["timetable"]:
        deps[rid][0 if wk else 1].append(dep)

    routes = []
    for rid, name, rtype, group, st, ed, tw, tt in d["routes"]:
        wk, other = deps[rid]
        times = sorted(wk or other)
        routes.append({"route_id": rid, "name": name, "type": rtype, "group": group, "st": st or "",
                       "ed": ed or "", "trips": tw, "trips_total": tt,
                       "first": times[0] if times else None, "last": times[-1] if times else None,
                       "segs": paths[rid], "stops": stops_by_route[rid]})

    stops = [{"id": stid, "name": name, "x": to_xy_int((lat, lng))[0], "y": to_xy_int((lat, lng))[1]}
             for stid, name, lat, lng in d["stops"]]
    return {"network": {"segments": segments, "nodes": nodes}, "routes": routes, "stops": stops}


def check(out: dict) -> list[str]:
    """노선 경로가 이어지는지 확인한다(앞 구간의 to = 다음 구간의 from)."""
    segs = out["network"]["segments"]
    bad = []
    for r in out["routes"]:
        for a, b in zip(r["segs"], r["segs"][1:]):
            if segs[a]["to"] != segs[b]["from"]:
                bad.append(f"경로 끊김: {r['route_id']}")
                break
    return bad


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="웹용 정적 데이터 생성 (DB 활성 버전 → JSON)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="출력 폴더")
    ap.add_argument("--tol", type=float, default=3.0, help="구간 좌표 단순화 허용 오차(m)")
    a = ap.parse_args()
    out_dir = Path(a.out)

    conn = pymysql.connect(**db_params())
    try:
        d = fetch(conn)
    finally:
        conn.close()
    data = build(d, a.tol)
    bad = check(data)
    if bad:
        print("검증 실패로 쓰지 않습니다:", *bad[:10], sep="\n  ", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    files, sizes = {}, []
    for key in ("network", "routes", "stops"):
        body = dumps(data[key])
        name = f"{key}.{hashlib.sha1(body).hexdigest()[:10]}.json"
        files[key] = name
        (out_dir / name).write_bytes(body)
        sizes.append(f"{name} {len(body) / 1024:.0f}KB (gzip {len(gzip.compress(body, 9)) / 1024:.0f}KB)")
    for old in out_dir.glob("*.json"):              # 이전 해시 파일 정리
        if old.name != "manifest.json" and old.name not in files.values():
            old.unlink()

    vid, dhash, crawled = d["version"]
    manifest = {"version_id": vid, "data_hash": dhash, "source_crawled_at": crawled,
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "origin": {"lat": LAT0, "lng": LNG0}, "tol_m": a.tol,
                "groups": [{"code": c, "label": l, "color": col} for c, l, col in d["groups"]],
                "files": files}
    (out_dir / "manifest.json").write_bytes(dumps(manifest))

    net = data["network"]
    npts = sum(len(s["pts"]) for s in net["segments"])
    print(f"버전 {vid}: 구간 {len(net['segments'])}, 노드 {len(net['nodes'])}, 정류소 {len(data['stops'])}, "
          f"노선 {len(data['routes'])}, 구간 좌표 {npts}점 (허용 오차 {a.tol}m)")
    for s in sizes:
        print("  ", s)
    print(f"저장: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
