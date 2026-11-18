#!/usr/bin/env python3
"""도로 그래프 빌더.

fix_shape 보정을 거친 크롤링 JSON의 노선 선형 좌표(shape)를 도로로 삼아
  1) 도로 층: 좌표가 정확히 일치하는 점을 같은 지점으로 보고 방향 그래프를 만든 뒤,
     분기·합류·기종점 사이를 도로 구간(road segment)으로 압축한다.
  2) 정류소 층: 각 정류소를 자기 노선의 선형 위에 투영해 (구간, 구간 내 거리)로 붙인다.
  3) 노선 층: 노선마다 지나는 도로 구간 목록과 정차 정류소 목록을 가진다.
결과를 JSON으로 저장한다. 표준 라이브러리만 사용한다.

프로젝트 루트에서 모듈로 실행한다(core.geo를 쓴다).
    python -m analysis.build_road_graph data/snapshots/2026-12-01/fixed.json --direction 1 --exclude-types 101

--direction은 원본 route_direction 값으로 거른다(615, 660, 303, 304 오등록 주의).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from core.geo import dedupe, dist_m, project  # noqa: F401  (경도 환산은 core.geo 기준 하나)



def strand_of(route: dict) -> str:
    stops = route["stops"]
    return (stops[0] if route["info"]["route_direction"] == "1" else stops[-1])["name"]


CORE_RATIO = 0.6  # 최대 운행횟수의 이 비율 이상인 구간까지만 공통 본선으로 본다


def trunk_path(segments: list[dict]) -> list[int]:
    """공통 본선: 운행횟수가 가장 많은 구간에서 양쪽으로 가장 굵은 구간을 따라가되,
    운행횟수가 최대치의 CORE_RATIO 아래로 떨어지면 멈춘다."""
    live = [s for s in segments if s["from"] != s["to"]]
    out_of, into = defaultdict(list), defaultdict(list)
    for seg in live:
        out_of[seg["from"]].append(seg)
        into[seg["to"]].append(seg)
    key = lambda s: (s["trips"], s["length_m"])
    best = max(live, key=key)
    floor = best["trips"] * CORE_RATIO
    path, seen = [best["id"]], {best["from"], best["to"]}
    node = best["to"]
    while out_of[node]:
        nxt = max(out_of[node], key=key)
        if nxt["to"] in seen or nxt["trips"] < floor:
            break
        path.append(nxt["id"])
        seen.add(nxt["to"])
        node = nxt["to"]
    node = best["from"]
    while into[node]:
        prev = max(into[node], key=key)
        if prev["from"] in seen or prev["trips"] < floor:
            break
        path.insert(0, prev["id"])
        seen.add(prev["from"])
        node = prev["from"]
    return path


def assign_strands(graph: dict, direction: str) -> None:
    """가닥 = 공통 본선과 도심 쪽에서 만나는 길이 같은 노선들의 묶음.

    상행은 본선에 들어오기 직전 구간, 하행은 본선에서 나간 직후 구간이 같은 노선끼리 묶는다.
    본선 위에서 출발(하행은 도착)하면 '본선' 가닥이다. 기점 정류소 이름이 달라도
    (예: 신정호관광지 / 신정호관광지 건너) 같은 길로 합류하면 한 가닥이 된다.
    """
    segments, routes, stops = graph["segments"], graph["routes"], graph["stops"]
    trunk_set = set(trunk_path(segments))
    for seg in segments:
        seg["trunk"] = seg["id"] in trunk_set

    groups: dict = defaultdict(list)
    for rid, route in routes.items():
        path = route["segment_path"] if direction == "1" else route["segment_path"][::-1]
        first = next((i for i, sid in enumerate(path) if sid in trunk_set), None)
        if first is None:
            key = ("off", path[0])
        elif first == 0:
            key = ("trunk",)
        else:
            key = ("link", path[first - 1])
        groups[key].append(rid)

    for rids in groups.values():
        lead = max(rids, key=lambda r: routes[r]["trips"])
        city_stop = routes[lead]["stops"][0 if direction == "1" else -1]
        for rid in rids:
            routes[rid]["strand"] = stops[city_stop]["name"]
    for seg in segments:
        seg["strands"] = sorted({routes[r]["strand"] for r in seg["route_ids"]})


def build(routes: list[dict], direction: str = "1", merge_below_m: float = 30.0) -> dict:
    """도로 그래프를 만든다.

    교차로에서는 노선마다 선형 좌표가 몇 m씩 어긋나 아주 짧은 구간이 생긴다.
    1차로 그래프를 만들어 merge_below_m 보다 짧은 구간을 찾고, 그 구간의 점들을
    시작점 하나로 합친 선형으로 다시 만들어 같은 교차로가 한 점이 되게 한다.
    """
    shapes = {r["info"]["route_id"]: dedupe(r["shape"]) for r in routes}
    if merge_below_m > 0:
        first = _build(routes, shapes)
        alias: dict[tuple, tuple] = {}

        def find(pt):
            while pt in alias:
                pt = alias[pt]
            return pt

        for seg in first["segments"]:
            if seg["length_m"] < merge_below_m and not seg["stops"]:
                root = find(tuple(seg["points"][0]))
                for pt in seg["points"][1:]:
                    pt = find(tuple(pt))
                    if pt != root:
                        alias[pt] = root
        shapes = {rid: dedupe([find(pt) for pt in pts]) for rid, pts in shapes.items()}
    graph = _build(routes, shapes)
    assign_strands(graph, direction)
    return graph


def _build(routes: list[dict], shapes: dict) -> dict:
    trips = {r["info"]["route_id"]: max(len(r.get("timetable", [])), 1) for r in routes}

    edge_routes: dict[tuple, set] = defaultdict(set)
    succ: dict[tuple, set] = defaultdict(set)
    pred: dict[tuple, set] = defaultdict(set)
    terminals: set[tuple] = set()
    for rid, pts in shapes.items():
        terminals.update((pts[0], pts[-1]))
        for a, b in zip(pts, pts[1:]):
            edge_routes[(a, b)].add(rid)
            succ[a].add(b)
            pred[b].add(a)

    def is_junction(node) -> bool:
        if node in terminals or len(succ[node]) != 1 or len(pred[node]) != 1:
            return True
        (p,), (n,) = tuple(pred[node]), tuple(succ[node])
        return edge_routes[(p, node)] != edge_routes[(node, n)]

    nodes = set(succ) | set(pred)
    junctions = {n for n in nodes if is_junction(n)}
    node_id = {pt: i for i, pt in enumerate(sorted(junctions))}

    segments, seg_by_first_edge = [], {}
    for start in sorted(junctions):
        for nxt in sorted(succ[start]):
            path = [start, nxt]
            while path[-1] not in junctions:
                (following,) = tuple(succ[path[-1]])
                path.append(following)
            rids = sorted(edge_routes[(start, nxt)])
            seg = {
                "id": len(segments),
                "from": node_id[start],
                "to": node_id[path[-1]],
                "points": [list(p) for p in path],
                "length_m": round(sum(dist_m(a, b) for a, b in zip(path, path[1:])), 1),
                "route_ids": rids,
                "trips": sum(trips[r] for r in rids),
                "stops": [],
            }
            segments.append(seg)
            seg_by_first_edge[(start, nxt)] = seg

    route_out, stop_out = {}, {}
    for route in routes:
        rid = route["info"]["route_id"]
        pts = shapes[rid]

        # 노선이 지나는 도로 구간 순서와, 선형의 각 선분이 속한 (구간, 구간 내 선분 번호)
        seg_path, owner = [], []
        i = 0
        while i < len(pts) - 1:
            seg = seg_by_first_edge[(pts[i], pts[i + 1])]
            seg_path.append(seg["id"])
            for k in range(len(seg["points"]) - 1):
                owner.append((seg["id"], k))
            i += len(seg["points"]) - 1

        # 정류소를 선형에 투영한다. 정류소 순서대로 앞에서부터만 찾아 역주행 매칭을 막는다.
        cursor, stop_refs = 0, []
        for stop in route["stops"]:
            p = (stop["lat"], stop["lng"])
            best = None
            for j in range(cursor, len(pts) - 1):
                d, t = project(p, pts[j], pts[j + 1])
                if best is None or d < best[0] - 1e-9:
                    best = (d, j, t)
                if best[0] < 15 and j - best[1] > 25:  # 충분히 가까운 곳을 찾았으면 멀리 가지 않는다
                    break
            d, j, t = best
            cursor = j
            seg_id, k = owner[j]
            seg = segments[seg_id]
            sp = seg["points"]
            offset = sum(dist_m(sp[m], sp[m + 1]) for m in range(k)) + t * dist_m(sp[k], sp[k + 1])
            lat = sp[k][0] + t * (sp[k + 1][0] - sp[k][0])
            lng = sp[k][1] + t * (sp[k + 1][1] - sp[k][1])
            sid = stop["stop_id"]
            if sid not in stop_out:
                stop_out[sid] = {
                    "name": stop["name"], "lat": stop["lat"], "lng": stop["lng"],
                    "segment": seg_id, "offset_m": round(offset, 1),
                    "snap": [round(lat, 7), round(lng, 7)], "snap_dist_m": round(d, 1),
                }
                seg["stops"].append(sid)
            stop_refs.append(sid)

        route_out[rid] = {
            "name": route["info"]["route_name"],
            "trips": trips[rid],
            "strand": strand_of(route),
            "segment_path": seg_path,
            "stops": stop_refs,
        }

    for seg in segments:
        seg["stops"].sort(key=lambda sid: stop_out[sid]["offset_m"])
        seg["route_names"] = sorted({route_out[r]["name"] for r in seg["route_ids"]})
        seg["strands"] = sorted({route_out[r]["strand"] for r in seg["route_ids"]})

    return {
        "nodes": {i: {"lat": pt[0], "lng": pt[1],
                      "in": sum(1 for s in segments if s["to"] == i),
                      "out": sum(1 for s in segments if s["from"] == i)}
                  for pt, i in node_id.items()},
        "segments": segments,
        "stops": stop_out,
        "routes": route_out,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="도로 그래프 빌더")
    parser.add_argument("input", help="crawl_asan.py 출력 JSON (shape 포함)")
    parser.add_argument("--direction", default="1", choices=["1", "2"], help="1 상행, 2 하행")
    parser.add_argument("--exclude-types", default="", help="제외할 route_type (쉼표 구분)")
    parser.add_argument("--exclude-routes", default="", help="제외할 노선번호 (쉼표 구분)")
    parser.add_argument("--merge-below", type=float, default=30.0,
                        help="이 길이(m)보다 짧은 구간은 한 교차로로 합친다 (0이면 끔)")
    parser.add_argument("--out", default=None, help="출력 JSON 경로")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as fp:
        data = json.load(fp)
    ex_types = {t.strip() for t in args.exclude_types.split(",") if t.strip()}
    ex_names = {t.strip() for t in args.exclude_routes.split(",") if t.strip()}
    routes = [
        r for r in data["routes"]
        if r.get("shape") and r.get("stops")
        and r["info"]["route_direction"] == args.direction
        and r["info"].get("route_type") not in ex_types
        and r["info"]["route_name"] not in ex_names
    ]
    if not routes:
        print("조건에 맞는 노선이 없습니다.", file=sys.stderr)
        return 1

    graph = build(routes, args.direction, args.merge_below)
    graph["meta"] = {"series": data["meta"]["series"], "direction": args.direction,
                     "routes": sorted(r["info"]["route_name"] for r in routes)}
    out = args.out or f"asan_series_{data['meta']['series']}_road_dir{args.direction}.json"
    with open(out, "w", encoding="utf-8") as fp:
        json.dump(graph, fp, ensure_ascii=False, separators=(",", ":"))

    nodes = graph["nodes"].values()
    snaps = sorted(s["snap_dist_m"] for s in graph["stops"].values())
    print(f"노선 {len(routes)}개 -> 도로 구간 {len(graph['segments'])}개, "
          f"분기 {sum(n['out'] > 1 for n in nodes)}곳, 합류 {sum(n['in'] > 1 for n in nodes)}곳, "
          f"정류소 {len(snaps)}개 (도로까지 거리 중앙값 {snaps[len(snaps) // 2]}m, 최대 {snaps[-1]}m)")
    print(f"저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
