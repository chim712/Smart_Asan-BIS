#!/usr/bin/env python3
"""시 전체 유향 도로 그래프 빌더 (DB 적재용).

fix_shape.py로 보정한 크롤링 JSON 전체(상·하행, 시내·공영 모두)를 방향 있는 그래프 하나로 만든다.
상·하행은 좌표가 겹치지 않으므로 방향별로 나눌 필요가 없다.

  도로 층  : 좌표가 같은 점 = 같은 지점. 분기·합류·기종점·노선집합 변화점 사이를 구간(segment)으로 압축.
             교차로의 몇 m 어긋남은 merge_below_m 보다 짧은 무정차 구간을 한 점으로 합쳐 없앤다.
  정류소 층: 노선마다 정류소를 자기 선형에 순서대로 투영 → (segment_id, 구간 내 거리).
  노선 층  : route_segment(노선이 지나는 구간 순서), route_stop(정차 정류소 순서).

ID는 좌표에서 만든 해시(63bit 정수)라 재빌드해도 안 바뀐 점·구간은 같은 ID를 유지한다.
출력은 DB 테이블과 1:1로 대응하는 JSON이다. 표준 라이브러리만 사용한다.

    python3 build_city_graph.py asan_series_all_fixed.json --out city_graph.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict

LAT0 = 36.78
KX = 111_320 * math.cos(math.radians(LAT0))  # 경도 1도(m)
KY = 110_574                                  # 위도 1도(m)


# ---------- 기하 ----------
def dist_m(a, b) -> float:
    return math.hypot((a[0] - b[0]) * KY, (a[1] - b[1]) * KX)


def project(p, a, b):
    """점 p를 선분 a-b에 투영 → (거리 m, 비율 t)."""
    ax, ay, bx, by = a[1] * KX, a[0] * KY, b[1] * KX, b[0] * KY
    px, py = p[1] * KX, p[0] * KY
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / n))
    return math.hypot(px - ax - t * dx, py - ay - t * dy), t


def dedupe(points) -> list[tuple]:
    out: list[tuple] = []
    for lat, lng in points:
        p = (lat, lng)
        if not out or out[-1] != p:
            out.append(p)
    return out


# ---------- ID / 해시 ----------
def h63(*parts) -> int:
    """안정 ID: 내용 해시의 앞 8바이트를 63bit 양수로."""
    s = "|".join(str(x) for x in parts).encode()
    return int.from_bytes(hashlib.sha1(s).digest()[:8], "big") >> 1


def hex_hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def pt_key(p) -> str:
    return f"{p[0]:.7f},{p[1]:.7f}"


# ---------- 계열 기본값 ----------
def default_group(name: str) -> str:
    """번호 규칙 기본값. 관리자 테이블(route_group_override)이 우선한다."""
    m = re.fullmatch(r"(\d+)(?:-\d+)?", name.strip())
    if not m:
        return "X"
    n = int(m.group(1))
    if 100 <= n <= 999:
        return str(n // 100)
    if 10 <= n <= 99:
        return str(n // 10)
    return "X"


# ---------- 빌드 ----------
def build(routes: list[dict], merge_below_m: float = 30.0) -> dict:
    shapes = {r["info"]["route_id"]: dedupe(r["shape"]) for r in routes}
    if merge_below_m > 0:
        first = _graph(shapes, _stop_points(routes, shapes))
        alias: dict[tuple, tuple] = {}

        def find(p):
            while p in alias:
                p = alias[p]
            return p

        for seg in first:
            if seg["length_m"] < merge_below_m and not seg["has_stop"]:
                root = find(seg["points"][0])
                for p in seg["points"][1:]:
                    p = find(p)
                    if p != root:
                        alias[p] = root
        shapes = {rid: dedupe([find(p) for p in pts]) for rid, pts in shapes.items()}
    return _assemble(routes, shapes)


def _stop_points(routes, shapes) -> set:
    """정류소가 얹히는 선분의 시작점 집합(합치기에서 정류소 있는 구간을 보호)."""
    out = set()
    for r in routes:
        pts = shapes[r["info"]["route_id"]]
        cur = 0
        for s in r["stops"]:
            j, _, _ = _locate((s["lat"], s["lng"]), pts, cur)
            cur = j
            out.add(pts[j])
            out.add(pts[j + 1])
    return out


def _locate(p, pts, cursor):
    """정류소 순서를 지키며 cursor 이후에서 가장 가까운 선분을 찾는다."""
    best = None
    for j in range(cursor, len(pts) - 1):
        d, t = project(p, pts[j], pts[j + 1])
        if best is None or d < best[1] - 1e-9:
            best = (j, d, t)
        if best[1] < 15 and j - best[0] > 25:
            break
    return best


def _topology(shapes):
    edge_routes: dict[tuple, set] = defaultdict(set)
    succ, pred = defaultdict(set), defaultdict(set)
    terminals = set()
    for rid, pts in shapes.items():
        terminals.update((pts[0], pts[-1]))
        for a, b in zip(pts, pts[1:]):
            edge_routes[(a, b)].add(rid)
            succ[a].add(b)
            pred[b].add(a)

    def is_junction(v) -> bool:
        if v in terminals or len(succ[v]) != 1 or len(pred[v]) != 1:
            return True
        (p,), (n,) = tuple(pred[v]), tuple(succ[v])
        return edge_routes[(p, v)] != edge_routes[(v, n)]

    nodes = set(succ) | set(pred)
    junctions = {v for v in nodes if is_junction(v)}
    segs = []
    for start in sorted(junctions):
        for nxt in sorted(succ[start]):
            path = [start, nxt]
            while path[-1] not in junctions:
                (f,) = tuple(succ[path[-1]])
                path.append(f)
            segs.append({"points": path, "route_ids": edge_routes[(start, nxt)],
                         "length_m": sum(dist_m(a, b) for a, b in zip(path, path[1:]))})
    return segs, junctions


def _graph(shapes, stop_pts):
    segs, _ = _topology(shapes)
    for s in segs:
        s["has_stop"] = any(p in stop_pts for p in s["points"])
    return segs


def _assemble(routes, shapes) -> dict:
    segs, junctions = _topology(shapes)
    node_id = {p: h63("N", pt_key(p)) for p in junctions}
    seg_by_first = {}
    segments = {}
    for s in segs:
        pts = s["points"]
        sid = h63("S", *(pt_key(p) for p in pts))
        seg = {"segment_id": sid, "from_node": node_id[pts[0]], "to_node": node_id[pts[-1]],
               "length_m": round(s["length_m"], 1), "points": [[p[0], p[1]] for p in pts],
               "route_ids": sorted(s["route_ids"])}
        segments[sid] = seg
        seg_by_first[(pts[0], pts[1])] = seg

    out_routes, route_segment, route_stop, timetable = [], [], [], []
    stops: dict[str, dict] = {}
    for r in routes:
        info = {k: (v.strip() if isinstance(v, str) else v) for k, v in r["info"].items()}
        rid, pts = info["route_id"], shapes[info["route_id"]]

        # 구간 경로 + 선형 선분 → (구간, 구간 내 선분 번호, 구간 시작까지 누적 m)
        path, owner = [], []
        i = 0
        while i < len(pts) - 1:
            seg = seg_by_first[(pts[i], pts[i + 1])]
            path.append(seg["segment_id"])
            sp, acc = seg["points"], 0.0
            for k in range(len(sp) - 1):
                owner.append((seg, k, acc))
                acc += dist_m(sp[k], sp[k + 1])
            i += len(sp) - 1
        for seq, sid in enumerate(path, 1):
            route_segment.append({"route_id": rid, "seq": seq, "segment_id": sid})

        cur = 0
        for s in r["stops"]:
            p = (s["lat"], s["lng"])
            j, d, t = _locate(p, pts, cur)
            cur = j
            seg, k, acc = owner[j]
            sp = seg["points"]
            offset = acc + t * dist_m(sp[k], sp[k + 1])
            route_stop.append({"route_id": rid, "ord": s["ord"], "stop_id": s["stop_id"],
                               "segment_id": seg["segment_id"], "offset_m": round(offset, 1),
                               "snap_dist_m": round(d, 1)})
            if s["stop_id"] not in stops:
                stops[s["stop_id"]] = {"stop_id": s["stop_id"], "service_id": s.get("service_id", ""),
                                       "name": s["name"], "lat": s["lat"], "lng": s["lng"]}

        # 시간표: 완전히 같은 행은 한 번만(82-1처럼 중복 행 존재). API의 ord는 유일하지 않으므로
        # (88-1은 ord 1~5와 1~7 두 묶음이 섞여 옴) 원본 ord는 src_ord로 두고 seq를 새로 매긴다.
        tt, seen_rows = [], set()
        for row in r.get("timetable") or []:
            k = tuple(sorted(row.items()))
            if k not in seen_rows:
                seen_rows.add(k)
                tt.append(row)
        for seq, row in enumerate(tt, 1):
            timetable.append({"route_id": rid, "seq": seq, "src_ord": row.get("ord"),
                              **{k: v for k, v in row.items() if k != "ord"}})
        out_routes.append({
            "route_id": rid, "route_name": info["route_name"], "route_type": info.get("route_type", ""),
            "route_direction": info.get("route_direction", ""), "company_name": info.get("company_name", ""),
            "st_stop_name": info.get("st_stop_name", ""), "ed_stop_name": info.get("ed_stop_name", ""),
            "trips_weekday": sum(1 for x in tt if x.get("weekday") == "1"),
            "trips_total": len(tt), "group_default": default_group(info["route_name"]),
            # 변경 감지용 해시: 선형·정류소(그래프에 영향) / 시간표(그래프와 무관) / 기본정보
            "geom_hash": hex_hash({"shape": r["shape"], "stops": [(s["stop_id"], s["ord"]) for s in r["stops"]]}),
            "timetable_hash": hex_hash(tt),
            "info_hash": hex_hash(info),
        })

    # 노드 진입·진출 수
    indeg, outdeg = Counter(), Counter()
    for s in segments.values():
        outdeg[s["from_node"]] += 1
        indeg[s["to_node"]] += 1
    nodes = [{"node_id": nid, "lat": p[0], "lng": p[1], "in_deg": indeg[nid], "out_deg": outdeg[nid]}
             for p, nid in node_id.items()]
    trips = {x["route_id"]: x["trips_weekday"] for x in out_routes}
    for s in segments.values():
        s["trips_weekday"] = sum(trips[r] for r in s["route_ids"])

    graph_hash = hex_hash(sorted((x["route_id"], x["geom_hash"]) for x in out_routes))
    return {"nodes": nodes, "segments": list(segments.values()), "stops": list(stops.values()),
            "routes": out_routes, "route_segment": route_segment, "route_stop": route_stop,
            "timetable": timetable, "graph_hash": graph_hash}


# ---------- 검증 ----------
def validate(g: dict) -> list[str]:
    issues = []
    seg = {s["segment_id"]: s for s in g["segments"]}
    by_route = defaultdict(list)
    for x in g["route_segment"]:
        by_route[x["route_id"]].append(x)
    for rid, rows in by_route.items():
        rows.sort(key=lambda x: x["seq"])
        for a, b in zip(rows, rows[1:]):
            if seg[a["segment_id"]]["to_node"] != seg[b["segment_id"]]["from_node"]:
                issues.append(f"경로 끊김: {rid} seq {a['seq']}→{b['seq']}")
    for x in g["route_stop"]:
        if x["snap_dist_m"] > 100:
            issues.append(f"선형 누락 의심(정류소가 선형 밖): {x['route_id']} ord {x['ord']} {x['stop_id']} {x['snap_dist_m']}m")
        elif x["snap_dist_m"] > 30:
            issues.append(f"정류소가 선형에서 멂: {x['route_id']} ord {x['ord']} {x['stop_id']} {x['snap_dist_m']}m")
    ords = defaultdict(list)
    for t in g["timetable"]:
        ords[t["route_id"]].append(t["src_ord"])
    for rid, xs in ords.items():
        if len(xs) != len(set(xs)):
            issues.append(f"시간표 원본 ord 중복(묶음이 섞였을 수 있음): {rid} {len(xs)}건")
    if len({s["segment_id"] for s in g["segments"]}) != len(g["segments"]):
        issues.append("segment_id 해시 충돌")
    if len({n["node_id"] for n in g["nodes"]}) != len(g["nodes"]):
        issues.append("node_id 해시 충돌")
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description="시 전체 유향 도로 그래프 빌더")
    ap.add_argument("input", help="fix_shape.py 출력 JSON")
    ap.add_argument("--merge-below", type=float, default=30.0, help="이 길이(m) 미만 무정차 구간을 한 점으로 합침")
    ap.add_argument("--out", default="city_graph.json")
    a = ap.parse_args()

    data = json.load(open(a.input, encoding="utf-8"))
    routes = [r for r in data["routes"] if r.get("shape") and r.get("stops")
              and r["info"].get("route_type", "").strip() != "13"]
    g = build(routes, a.merge_below)
    issues = validate(g)
    g["meta"] = {"source_crawled_at": data["meta"].get("crawled_at"),
                 "shape_fixes": data["meta"].get("shape_fixes", []),
                 "merge_below_m": a.merge_below, "issues": issues}
    json.dump(g, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    n = g["nodes"]
    snaps = sorted(x["snap_dist_m"] for x in g["route_stop"])
    print(f"노선 {len(g['routes'])}개 → 구간 {len(g['segments'])}개, 노드 {len(n)}개 "
          f"(분기 {sum(x['out_deg'] > 1 for x in n)}, 합류 {sum(x['in_deg'] > 1 for x in n)}), "
          f"정류소 {len(g['stops'])}개, 노선-정류소 {len(snaps)}건 "
          f"(투영거리 중앙값 {snaps[len(snaps)//2]}m, 최대 {snaps[-1]}m)")
    print(f"graph_hash {g['graph_hash'][:12]}  검증 문제 {len(issues)}건")
    for s in issues[:20]:
        print("  -", s)
    print(f"저장: {a.out}")
    return 0 if not any(s.startswith(("경로 끊김", "segment_id", "node_id")) for s in issues) else 2


if __name__ == "__main__":
    sys.exit(main())
