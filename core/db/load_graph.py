#!/usr/bin/env python3
"""build_city_graph.py 출력을 MariaDB에 새 버전으로 적재한다.

- 활성 버전과 data_hash가 같으면 아무것도 하지 않는다(변경 없음 → 데이터 수정 없음).
- 다르면 새 version_id로 전 테이블을 넣고(status=loading → ready), 한 트랜잭션으로 current_version을 전환한다.
- 최근 --keep 개 버전만 남기고 오래된 버전은 지운다(FK CASCADE).
- 그래프가 같고 시간표·노선정보만 바뀐 경우도 새 버전으로 넣는다(단순·일관). 변경 종류는 출력으로 알린다.

접속 정보는 core/config.py 가 .env(또는 환경변수)에서 읽는다. 프로젝트 루트에서 모듈로 실행한다.

    python -m core.db.load_graph data/snapshots/2026-12-01/graph.json
    python -m core.db.load_graph graph.json --dry-run      # 변경 여부만 확인
    python -m core.db.load_graph graph.json --no-activate  # 넣기만 하고 전환은 수동
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

import pymysql

from core.config import db_params
from core.graph.build_city_graph import FATAL_PREFIXES

BATCH = 2000      # executemany 묶음 (%s만 있는 INSERT는 pymysql이 여러 행 VALUES로 합친다)
GEO_BATCH = 500   # ST_GeomFromText가 든 INSERT는 pymysql이 합치지 못하므로 직접 여러 행으로 묶는다


def data_hash(g: dict) -> str:
    parts = [g["graph_hash"]] + sorted(f"{r['route_id']}:{r['timetable_hash']}:{r['info_hash']}" for r in g["routes"])
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def wkt_point(lat, lng) -> str:
    return f"POINT({lng} {lat})"


def wkt_line(points) -> str:
    return "LINESTRING(" + ",".join(f"{lng} {lat}" for lat, lng in points) + ")"


def many(cur, sql, rows):
    for i in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[i:i + BATCH])


def many_values(cur, head: str, row_sql: str, rows) -> None:
    """head + row_sql을 여러 행 묶어 한 번에 실행한다. 예: head="INSERT INTO stop VALUES ",
    row_sql="(%s,%s,ST_GeomFromText(%s,4326))"."""
    for i in range(0, len(rows), GEO_BATCH):
        chunk = rows[i:i + GEO_BATCH]
        cur.execute(head + ",".join([row_sql] * len(chunk)), [v for row in chunk for v in row])


def active(cur):
    cur.execute("SELECT v.version_id, v.graph_hash, v.data_hash FROM current_version c "
                "JOIN graph_version v ON v.version_id = c.version_id")
    return cur.fetchone()


def diff_summary(cur, vid, g) -> str:
    cur.execute("SELECT route_id, geom_hash, timetable_hash, info_hash FROM route WHERE version_id=%s", (vid,))
    old = {r[0]: r[1:] for r in cur.fetchall()}
    new = {r["route_id"]: (r["geom_hash"], r["timetable_hash"], r["info_hash"]) for r in g["routes"]}
    added, removed = new.keys() - old.keys(), old.keys() - new.keys()
    both = new.keys() & old.keys()
    geom = [k for k in both if new[k][0] != old[k][0]]
    tt = [k for k in both if new[k][1] != old[k][1]]
    info = [k for k in both if new[k][2] != old[k][2]]
    return (f"추가 {len(added)}, 삭제 {len(removed)}, 선형·정류소 변경 {len(geom)}, "
            f"시간표 변경 {len(tt)}, 기본정보 변경 {len(info)}")


def load(conn, g: dict, dhash: str) -> int:
    cur = conn.cursor()
    meta = json.dumps(g.get("meta", {}), ensure_ascii=False)
    cur.execute("INSERT INTO graph_version (source_crawled_at, graph_hash, data_hash, status, route_count, "
                "segment_count, stop_count, meta) VALUES (%s,%s,%s,'loading',%s,%s,%s,%s)",
                (g.get("meta", {}).get("source_crawled_at"), g["graph_hash"], dhash,
                 len(g["routes"]), len(g["segments"]), len(g["stops"]), meta))
    vid = cur.lastrowid
    conn.commit()
    try:
        many(cur, "INSERT INTO route VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
             [(vid, r["route_id"], r["route_name"], r["route_type"], r["route_direction"], r["company_name"],
               r["st_stop_name"], r["ed_stop_name"], r["trips_weekday"], r["trips_total"], r["group_default"],
               r["geom_hash"], r["timetable_hash"], r["info_hash"]) for r in g["routes"]])
        many(cur, "INSERT INTO route_timetable VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
             [(vid, t["route_id"], t["seq"], t["src_ord"], t["departure"], t["weekday"] == "1",
               t["saturday"] == "1", t["holiday"] == "1", t["vacation"] == "1") for t in g["timetable"]])
        many_values(cur, "INSERT INTO stop VALUES ", "(%s,%s,%s,%s,%s,%s,ST_GeomFromText(%s,4326))",
             [(vid, s["stop_id"], s["service_id"], s["name"], s["lat"], s["lng"], wkt_point(s["lat"], s["lng"]))
              for s in g["stops"]])
        many(cur, "INSERT INTO node VALUES (%s,%s,%s,%s,%s,%s)",
             [(vid, n["node_id"], n["lat"], n["lng"], n["in_deg"], n["out_deg"]) for n in g["nodes"]])
        many_values(cur, "INSERT INTO segment VALUES ", "(%s,%s,%s,%s,%s,%s,ST_GeomFromText(%s,4326))",
             [(vid, s["segment_id"], s["from_node"], s["to_node"], s["length_m"], s["trips_weekday"],
               wkt_line(s["points"])) for s in g["segments"]])
        many(cur, "INSERT INTO route_segment VALUES (%s,%s,%s,%s)",
             [(vid, x["route_id"], x["seq"], x["segment_id"]) for x in g["route_segment"]])
        many(cur, "INSERT INTO route_stop VALUES (%s,%s,%s,%s,%s,%s,%s)",
             [(vid, x["route_id"], x["ord"], x["stop_id"], x["segment_id"], x["offset_m"], x["snap_dist_m"])
              for x in g["route_stop"]])
        cur.execute("UPDATE graph_version SET status='ready' WHERE version_id=%s", (vid,))
        conn.commit()
    except Exception:
        conn.rollback()
        cur.execute("UPDATE graph_version SET status='failed' WHERE version_id=%s", (vid,))
        conn.commit()
        raise
    return vid


def activate(conn, vid: int, keep: int) -> None:
    """vid를 활성 버전으로 전환하고, 활성 버전을 뺀 최근 keep-1개만 남긴다.

    정리 대상은 status가 아니라 current_version 기준으로 고른다. --no-activate 후 SQL로 직접
    전환해 status가 어긋나 있어도 활성 버전은 지워지지 않고, 남은 ready/loading 버전도 정리된다.
    """
    cur = conn.cursor()
    cur.execute("UPDATE graph_version SET status='retired' WHERE version_id<>%s AND (status='active' OR "
                "version_id=(SELECT version_id FROM current_version WHERE id=1))", (vid,))
    cur.execute("INSERT INTO current_version (id, version_id) VALUES (1,%s) "
                "ON DUPLICATE KEY UPDATE version_id=VALUES(version_id)", (vid,))
    cur.execute("UPDATE graph_version SET status='active' WHERE version_id=%s", (vid,))
    conn.commit()
    cur.execute("SELECT version_id FROM graph_version WHERE version_id <> "
                "(SELECT version_id FROM current_version WHERE id=1) "
                "ORDER BY version_id DESC LIMIT 1000 OFFSET %s", (max(keep - 1, 0),))
    old = [r[0] for r in cur.fetchall()]
    for v in old:
        cur.execute("DELETE FROM graph_version WHERE version_id=%s", (v,))
    conn.commit()
    if old:
        print(f"오래된 버전 삭제: {old}")


def main() -> int:
    ap = argparse.ArgumentParser(description="도시 그래프 MariaDB 적재")
    ap.add_argument("graph", help="build_city_graph.py 출력 JSON")
    ap.add_argument("--keep", type=int, default=3, help="보관할 버전 수(활성 포함)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-activate", action="store_true")
    ap.add_argument("--force", action="store_true", help="변경이 없어도 새 버전으로 적재")
    a = ap.parse_args()

    with open(a.graph, encoding="utf-8") as fp:
        g = json.load(fp)
    fatal = [s for s in g.get("meta", {}).get("issues", []) if s.startswith(FATAL_PREFIXES)]
    if fatal:
        print("그래프 검증 실패로 적재하지 않습니다:", *fatal[:10], sep="\n  ")
        return 2
    dhash = data_hash(g)

    conn = pymysql.connect(**db_params())
    cur = conn.cursor()
    cur_ver = active(cur)
    if cur_ver:
        vid, ghash, old_dhash = cur_ver
        if old_dhash == dhash and not a.force:
            print(f"변경 없음 (활성 버전 {vid}). 적재하지 않습니다.")
            return 0
        print(f"활성 버전 {vid} 대비: {diff_summary(cur, vid, g)}"
              + ("" if ghash != g["graph_hash"] else " — 그래프 구조는 동일"))
    else:
        print("활성 버전 없음: 첫 적재")
    if a.dry_run:
        return 0

    new_vid = load(conn, g, dhash)
    print(f"버전 {new_vid} 적재 완료: 노선 {len(g['routes'])}, 구간 {len(g['segments'])}, 정류소 {len(g['stops'])}")
    if a.no_activate:
        print(f"전환하지 않았습니다. 확인 후: UPDATE current_version SET version_id={new_vid};")
    else:
        activate(conn, new_vid, a.keep)
        print(f"활성 버전 → {new_vid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
