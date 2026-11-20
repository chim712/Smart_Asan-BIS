"""core/realtime/locate.py 시험.

    python -m unittest tests.test_locate -v          # 프로젝트 루트에서

합성 노선(동쪽으로 곧은 3km, 정류소 500m 간격 7개)으로 판정 규칙을 하나씩 확인한다.
녹화본 통합 시험은 환경변수가 있을 때만 돈다:
    ASANBIS_REPLAY_LOC=data/realtime/.../loc.jsonl  ASANBIS_GRAPH=data/snapshots/.../graph.json
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime

from core.realtime import locate as L

LAT, LNG = 36.78, 127.0


def ll(s: float, north: float = 0.0) -> tuple[float, float]:
    """기준점에서 동쪽 s m, 북쪽 north m 지점."""
    return LAT + north / L.KY, LNG + s / L.KX


def make_line(route_id="R1", length=3000, gap=500, stop_ids=None) -> L.RouteLine:
    pts = [ll(x) for x in range(0, length + 1, 100)]
    n = length // gap + 1
    stop_ids = stop_ids or [f"S{i}" for i in range(n)]
    stops = [(sid, f"정류소{i}", *ll(i * gap)) for i, sid in enumerate(stop_ids)]
    return L.route_from_points(route_id, "999", pts, stops)


def veh(stop_id, lat=None, lng=None, vid="288041001"):
    v = {"veh_id": vid, "stop_id": stop_id, "plate_no": "충남72자1001"}
    if lat is not None:
        v["lat"], v["lng"] = f"{lat:.7f}", f"{lng:.7f}"
    return v


def bad(lat):  # lng 자리에 lat이 복사된 좌표
    return {"lat": f"{lat:.6f}", "lng": f"{lat:.6f}"}


class RouteLineTest(unittest.TestCase):
    def test_stop_positions_and_geometry(self):
        line = make_line()
        self.assertEqual(line.last_idx, 6)
        for i, st in enumerate(line.stops):
            self.assertAlmostEqual(st.s, i * 500, delta=0.5)
        lat, lng = line.point_at(1234)
        d, s = line.project(lat, lng, 0, line.length)
        self.assertLess(d, 0.1)
        self.assertAlmostEqual(s, 1234, delta=0.5)
        self.assertEqual(line.stop_at(990), 1)
        self.assertEqual(line.stop_at(996), 2)       # 정류소 5m 앞부터 도착으로 본다

    def test_city_graph_builder_with_revisited_segment(self):
        # A→B, B→C, C→B(되돌아옴) 처럼 같은 정류소 구간을 두 번 지나는 노선
        a, b, c = ll(0), ll(500), ll(1000)
        graph = {
            "segments": [
                {"segment_id": 1, "length_m": 500.0, "points": [list(a), list(b)]},
                {"segment_id": 2, "length_m": 500.0, "points": [list(b), list(c)]},
                {"segment_id": 3, "length_m": 500.0, "points": [list(c), list(ll(1000, 0.0001)), list(b)]},
            ],
            "stops": [{"stop_id": "X", "name": "가", "lat": a[0], "lng": a[1]},
                      {"stop_id": "Y", "name": "나", "lat": ll(750)[0], "lng": ll(750)[1]}],
            "routes": [{"route_id": "G", "route_name": "1 ", "route_type": "102"}],
            "route_segment": [{"route_id": "G", "seq": i + 1, "segment_id": sid} for i, sid in enumerate([1, 2, 3])],
            "route_stop": [{"route_id": "G", "ord": 1, "stop_id": "X", "segment_id": 1, "offset_m": 0.0},
                           {"route_id": "G", "ord": 2, "stop_id": "Y", "segment_id": 2, "offset_m": 250.0},
                           {"route_id": "G", "ord": 3, "stop_id": "Y", "segment_id": 3, "offset_m": 250.0}],
        }
        line = L.routes_from_city_graph(graph)["G"]
        self.assertEqual(line.name, "1")
        self.assertEqual([round(st.s) for st in line.stops], [0, 750, 1250])
        self.assertEqual(line.stop_index["Y"], [1, 2])
        self.assertEqual(line.segment_at(1100), (3, 100.0))


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.line = make_line()
        self.tr = L.Tracker({"R1": self.line})

    def up(self, v, t, rid="R1"):
        return self.tr.update(rid, [v], t)[0]

    def test_gps_accepted_between_stops(self):
        p = self.up(veh("S1", *ll(700, 5)), 0)
        self.assertEqual((p.source, p.check, p.state), (L.SRC_GPS, "ok", L.RUNNING))
        self.assertAlmostEqual(p.s, 700, delta=1)
        self.assertEqual((p.stop_idx, p.reported_idx), (1, 1))

    def test_latlng_defect_falls_back_to_stop_then_holds(self):
        p = self.up({**veh("S1"), **bad(36.78)}, 0)
        self.assertEqual((p.source, p.check), (L.SRC_STOP, "lat=lng"))
        self.assertAlmostEqual(p.s, 500, delta=1)
        p = self.up(veh("S1", *ll(800)), 15)
        self.assertEqual(p.source, L.SRC_GPS)
        p = self.up({**veh("S1"), **bad(36.78)}, 30)      # 같은 정류소 구간: 800 유지
        self.assertEqual(p.source, L.SRC_HOLD)
        self.assertAlmostEqual(p.s, 800, delta=1)
        p = self.up({**veh("S2"), **bad(36.78)}, 45)      # 정류소가 바뀌면 그 정류소로
        self.assertEqual(p.source, L.SRC_STOP)
        self.assertAlmostEqual(p.s, 1000, delta=1)

    def test_gps_may_lead_stop_id_up_to_three_stops(self):
        p = self.up(veh("S1", *ll(1900)), 0)               # 보고는 S1, 실제는 S3을 지남
        self.assertEqual(p.source, L.SRC_GPS)
        self.assertEqual((p.stop_idx, p.reported_idx), (3, 1))
        tr = L.Tracker({"R1": self.line})
        p = tr.update("R1", [veh("S1", *ll(2600))], 0)[0]  # S4+30m 넘어섬
        self.assertEqual((p.source, p.check), (L.SRC_STOP, "out_of_window"))

    def test_snapped_within_80m(self):
        p = self.up(veh("S2", *ll(1100, 60)), 0)           # 선형에서 60m: GPS 오차로 보고 붙임
        self.assertEqual((p.state, p.source, p.check), (L.RUNNING, L.SRC_GPS, "snapped"))
        self.assertAlmostEqual(p.s, 1100, delta=1)
        self.assertAlmostEqual(p.raw_s, 1100, delta=1)
        lat, lng = ll(1100)
        self.assertAlmostEqual(p.lat, lat, places=6)       # 표시는 수선의 발
        self.assertAlmostEqual(p.lng, lng, places=6)

    def test_parked_when_stop_itself_is_off_line(self):
        # 정류소 좌표가 선형에서 50m 떨어진 노선(승강장·대기 공간이 선형 밖)
        pts = [ll(x) for x in range(0, 3001, 100)]
        stops = [(f"S{i}", f"정류소{i}", *ll(i * 500, 50 if i == 2 else 0)) for i in range(7)]
        line = L.route_from_points("R1", "999", pts, stops)
        p = L.Tracker({"R1": line}).update("R1", [veh("S2", *ll(1000, 100))], 0)[0]
        self.assertEqual((p.state, p.source, p.check), (L.AT_STOP, L.SRC_STOP, "parked"))

    def test_off_route_after_three_and_shown_at_raw(self):
        self.up(veh("S2", *ll(1100)), 0)
        far = ll(1300, 200)
        for t in (15, 30):                                 # 한두 번은 튄 좌표로 보고 직전 위치 유지
            p = self.up(veh("S2", *far), t)
            self.assertEqual((p.check, p.source, p.state), ("off_road", L.SRC_HOLD, L.RUNNING))
            self.assertAlmostEqual(p.s, 1100, delta=1)
        p = self.up(veh("S2", *ll(1400, 220)), 45)         # 세 번째: 경로 이탈, 원시 좌표에 표시
        self.assertEqual((p.state, p.source), (L.OFF_ROUTE, L.SRC_RAW))
        self.assertEqual((round(p.lat, 6), round(p.lng, 6)), tuple(round(x, 6) for x in ll(1400, 220)))
        self.assertAlmostEqual(p.s, 1100, delta=1)         # 노선 위 진행은 유지
        self.assertIsNone(p.segment_id)
        p = self.up({**veh("S2"), **bad(36.78)}, 60)       # 좌표 없음: 이탈 상태와 마지막 원시 좌표 유지
        self.assertEqual(p.state, L.OFF_ROUTE)
        self.assertEqual((round(p.lat, 6), round(p.lng, 6)), tuple(round(x, 6) for x in ll(1400, 220)))
        p = self.up(veh("S3", *ll(1600, 10)), 75)          # 노선으로 돌아옴
        self.assertEqual((p.state, p.source, p.check), (L.RUNNING, L.SRC_GPS, "ok"))
        self.assertAlmostEqual(p.s, 1600, delta=1)

    def test_no_off_route_at_origin_or_terminal(self):
        for t in (0, 15, 30, 45):
            p = self.up(veh("S0", *ll(0, 300)), t)
        self.assertEqual(p.state, L.ORIGIN_WAIT)
        tr = L.Tracker({"R1": self.line})
        for t in (0, 15, 30, 45):
            p = tr.update("R1", [veh("S6", *ll(3000, 300))], t)[0]
        self.assertEqual(p.state, L.TERMINAL_WAIT)

    def test_never_moves_backward(self):
        self.up(veh("S1", *ll(900)), 0)
        p = self.up(veh("S1", *ll(890)), 15)               # 10m 흔들림: 받아들이되 위치는 900
        self.assertEqual(p.source, L.SRC_GPS)
        self.assertAlmostEqual(p.s, 900, delta=1)
        p = self.up(veh("S1", *ll(700)), 30)               # 200m 뒤: 버림
        self.assertEqual((p.check, p.source), ("backward", L.SRC_HOLD))
        self.assertAlmostEqual(p.s, 900, delta=1)

    def test_origin_and_terminal(self):
        p = self.up(veh("S0", *ll(10, 40)), 0)             # 기점 뒤 대기 공간
        self.assertEqual(p.state, L.ORIGIN_WAIT)
        p = self.up(veh("S0", *ll(200)), 15)               # 출발: stop_id는 아직 S0
        self.assertEqual(p.state, L.RUNNING)
        p = self.up({**veh("S6"), **bad(36.78)}, 30)
        self.assertEqual(p.state, L.TERMINAL_WAIT)
        self.assertAlmostEqual(p.s, 3000, delta=1)

    def test_route_change_resets_progress(self):
        other = make_line("R2", stop_ids=[f"T{i}" for i in range(7)])
        tr = L.Tracker({"R1": self.line, "R2": other})
        tr.update("R1", [veh("S6", *ll(3000))], 0)
        p = tr.update("R2", [veh("T0", *ll(0))], 60)[0]
        self.assertTrue(p.route_changed)
        self.assertEqual((p.state, round(p.s)), (L.ORIGIN_WAIT, 0))

    def test_duplicate_stop_id_picks_occurrence_after_previous(self):
        ids = ["S0", "S1", "LOOP", "S3", "LOOP", "S5", "S6"]
        line = make_line(stop_ids=ids)
        tr = L.Tracker({"R1": line})
        tr.update("R1", [veh("S3", *ll(1600))], 0)
        p = tr.update("R1", [{**veh("LOOP"), **bad(36.78)}], 15)[0]
        self.assertEqual(p.reported_idx, 4)
        self.assertAlmostEqual(p.s, 2000, delta=1)
        p = L.Tracker({"R1": line}).update("R1", [{**veh("LOOP"), **bad(36.78)}], 0)[0]
        self.assertEqual(p.reported_idx, 2)                # 처음 본 차량은 첫 번째

    def test_unmapped_stop_and_unknown_route(self):
        p = self.up(veh("NOPE", *ll(100)), 0)
        self.assertEqual((p.state, p.check), (L.UNKNOWN, "stop_unmapped"))
        self.up(veh("S1", *ll(600)), 10)
        p = self.up(veh("NOPE", *ll(700)), 20)
        self.assertEqual((p.source, p.check), (L.SRC_HOLD, "stop_unmapped"))
        p = self.tr.update("ZZ", [veh("S1")], 30)[0]
        self.assertEqual(p.check, "route_unknown")

    def test_info_age_and_stale(self):
        self.up(veh("S1", *ll(700)), 0)
        for t in (15, 30, 45):
            p = self.up(veh("S1", *ll(700)), t)            # 같은 좌표 반복
        self.assertEqual(p.info_age_s, 45)
        self.assertFalse(p.stale)
        p = self.up({**veh("S1"), **bad(36.78)}, 75)
        self.assertTrue(p.stale)
        p = self.up(veh("S1", *ll(750)), 90)               # 새 좌표
        self.assertEqual((p.info_age_s, p.stale), (0, False))

    def test_expire(self):
        self.up(veh("S1", *ll(700)), 0)
        self.assertEqual(self.tr.expire(100), [])
        self.assertEqual(self.tr.expire(181), ["288041001"])
        self.assertEqual(self.tr.current(), [])


@unittest.skipUnless(os.environ.get("ASANBIS_REPLAY_LOC") and os.environ.get("ASANBIS_GRAPH"),
                     "녹화본 경로 환경변수가 없음")
class RecordingTest(unittest.TestCase):
    def test_replay_invariants(self):
        with open(os.environ["ASANBIS_GRAPH"], encoding="utf-8") as fp:
            graph = json.load(fp)
        rows = []
        with open(os.environ["ASANBIS_REPLAY_LOC"], encoding="utf-8") as fp:
            for line in fp:
                rows.append(json.loads(line))
        rids = {r["route_id"] for r in rows}
        tr = L.Tracker(L.routes_from_city_graph(graph, rids))
        last, n, unmapped = {}, 0, 0
        for r in sorted(rows, key=lambda r: r["ts"]):
            if "vehicles" not in r:
                continue
            t = datetime.fromisoformat(r["ts"]).timestamp()
            for p in tr.update(r["route_id"], r["vehicles"], t):
                n += 1
                unmapped += p.check == "stop_unmapped"
                key = (p.veh_id, p.route_id)
                if p.s is not None and key in last and not p.route_changed:
                    self.assertGreaterEqual(p.s, last[key] - 1e-6, f"{key} 뒤로 감 at {r['ts']}")
                if p.s is not None:
                    last[key] = p.s
            tr.expire(t)
        self.assertGreater(n, 0)
        self.assertEqual(unmapped, 0)


if __name__ == "__main__":
    unittest.main()
