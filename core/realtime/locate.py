"""실시간 차량 위치 계산 (core/realtime/locate.py).

위치 API(searchBusRealLocationDetail) 응답을 노선 선형 위의 위치로 바꾼다.
녹화 재생(analysis/replay_locate.py)과 실시간 폴러(backend)가 같은 코드를 쓴다.
입력이 어디서 왔는지는 모른다: 응답 한 건과 수신 시각만 받는다.

판정 규칙 (docs/PROJECT_CONTEXT.md 5장 "실시간 위치 표시", 3-1-2 근거)
- 기본 위치는 보고된 최근 정류소(stop_id).
- 좌표는 아래를 모두 통과할 때만 쓴다.
  1. 형식: lat != lng, 범위 안
  2. 도로 위: 선형까지 수직 거리 <= snap_tol_m(80m). road_tol_m(30m) 이내는 "ok",
     그 밖은 GPS 오차로 보고 수선의 발에 붙인다("snapped"). 시 선형 자체의 오차도 여기서 흡수한다.
  3. 구간 일치: 선형 위 거리 s가 [정류소 k - back_tol_m, 정류소 k+ahead_stops + ahead_tol_m] 안.
     stop_id는 늦게 바뀌고 정류소를 건너뛰므로 GPS가 stop_id보다 앞서는 것을 허용한다.
  4. 뒤로 가지 않음: 같은 차량의 직전 s보다 backward_tol_m 넘게 뒤면 버린다.
- 2·3에 실패해도 보고된 정류소 좌표에서 park_radius_m 안이면 "정류소 정차".
- 좌표가 없거나 버려지면, 같은 정류소 구간 안에서는 직전 위치를 유지한다(천안 차량 lat=lng 대응).
- 선형에서 snap_tol_m 넘게 떨어진 좌표가 off_route_after회 연달아 오면 "경로 이탈"(GPS 위치 오류)로
  보고 원시 좌표에 표시한다. 그 전까지(한두 번 튀는 좌표)는 직전 위치를 유지한다. 기점·종점 제외.
- 보고된 정류소가 첫/마지막 정류소면 기점 대기/종점 대기.
- 표시 위치는 경로 이탈이 아니면 원시 GPS 점이 아니라 선형 위 s 지점이다.

외부 의존성 없음(표준 라이브러리). 좌표 환산은 기준 위도 36.78도 평면 근사.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

LAT0 = 36.78
KX = 111320.0 * math.cos(math.radians(LAT0))  # 경도 1도 (m)
KY = 110574.0                                  # 위도 1도 (m), build_city_graph의 length_m과 같은 기준


def to_xy(lat: float, lng: float) -> tuple[float, float]:
    return lng * KX, lat * KY


def to_latlng(x: float, y: float) -> tuple[float, float]:
    return y / KY, x / KX


# --------------------------------------------------------------------------- 노선 선형

@dataclass(frozen=True)
class RouteStop:
    idx: int            # 노선 안 순번 (0부터)
    stop_id: str
    name: str
    s: float            # 선형 시작점부터 거리 (m)
    lat: float
    lng: float


class RouteLine:
    """한 방향 노선의 선형과 정류소 위치. 선형 위 거리 s로 모든 것을 다룬다."""

    def __init__(self, route_id: str, name: str, points: list[tuple[float, float]],
                 stops: list[RouteStop], route_type: str = "",
                 segments: list[tuple[float, int]] | None = None) -> None:
        self.route_id = route_id
        self.name = name
        self.route_type = route_type
        self.xy = [to_xy(lat, lng) for lat, lng in points]
        self.cum = [0.0]
        for a, b in zip(self.xy, self.xy[1:]):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.length = self.cum[-1]
        self.stops = stops
        self.stop_index: dict[str, list[int]] = {}
        for st in stops:
            self.stop_index.setdefault(st.stop_id, []).append(st.idx)
        self._stop_s = [st.s for st in stops]
        # (구간 시작 s, segment_id). city_graph에서 만들 때만 있다.
        self.segments = segments
        self._seg_s = [s for s, _ in segments] if segments else None

    # -- 기하
    def _idx(self, s: float) -> int:
        return min(max(0, bisect.bisect_right(self.cum, s) - 1), len(self.xy) - 2)

    def project(self, lat: float, lng: float, s_lo: float, s_hi: float) -> tuple[float, float]:
        """[s_lo, s_hi] 범위의 선형에 투영한다. (수직 거리 m, 선형 위 거리 s)."""
        qx, qy = to_xy(lat, lng)
        best_d, best_s = math.inf, max(0.0, s_lo)
        i0, i1 = self._idx(max(0.0, s_lo)), self._idx(min(self.length, s_hi))
        for i in range(i0, i1 + 1):
            (ax, ay), (bx, by) = self.xy[i], self.xy[i + 1]
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((qx - ax) * dx + (qy - ay) * dy) / l2))
            d = math.hypot(qx - ax - t * dx, qy - ay - t * dy)
            if d < best_d:
                best_d, best_s = d, self.cum[i] + t * math.sqrt(l2)
        return best_d, min(max(best_s, s_lo), s_hi)

    def point_at(self, s: float) -> tuple[float, float]:
        s = min(max(0.0, s), self.length)
        i = self._idx(s)
        seg = self.cum[i + 1] - self.cum[i]
        t = 0.0 if seg == 0 else (s - self.cum[i]) / seg
        (ax, ay), (bx, by) = self.xy[i], self.xy[i + 1]
        return to_latlng(ax + t * (bx - ax), ay + t * (by - ay))

    def segment_at(self, s: float) -> tuple[int | None, float | None]:
        if not self.segments:
            return None, None
        j = max(0, bisect.bisect_right(self._seg_s, s) - 1)
        start, seg_id = self.segments[j]
        return seg_id, round(s - start, 1)

    def stop_at(self, s: float, eps: float = 5.0) -> int:
        """s 지점에서 이미 지난(또는 서 있는) 마지막 정류소 순번."""
        return max(0, bisect.bisect_right(self._stop_s, s + eps) - 1)

    def stop_s(self, idx: int) -> float:
        return self._stop_s[min(max(0, idx), len(self._stop_s) - 1)]

    @property
    def last_idx(self) -> int:
        return len(self.stops) - 1


def _project_stops(points_xy: list[tuple[float, float]], cum: list[float],
                   stops: list[tuple[str, str, float, float]]) -> list[float]:
    """정류소를 순서대로 선형에 투영한다(앞 정류소보다 뒤에서만 찾음)."""
    out, lo = [], 0
    for _, _, lat, lng in stops:
        qx, qy = to_xy(lat, lng)
        best = (math.inf, cum[lo], lo)
        for i in range(lo, len(points_xy) - 1):
            (ax, ay), (bx, by) = points_xy[i], points_xy[i + 1]
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((qx - ax) * dx + (qy - ay) * dy) / l2))
            d = math.hypot(qx - ax - t * dx, qy - ay - t * dy)
            if d < best[0]:
                best = (d, cum[i] + t * math.sqrt(l2), i)
        out.append(best[1])
        lo = best[2]
    return out


def route_from_points(route_id: str, name: str, points: list[tuple[float, float]],
                      stops: list[tuple[str, str, float, float]], route_type: str = "") -> RouteLine:
    """선형 점열 + (stop_id, 이름, lat, lng) 목록으로 노선을 만든다. 정류소 위치는 투영으로 구한다."""
    tmp = RouteLine(route_id, name, points, [], route_type)
    ss = _project_stops(tmp.xy, tmp.cum, stops)
    rs = [RouteStop(i, sid, nm, s, lat, lng) for i, ((sid, nm, lat, lng), s) in enumerate(zip(stops, ss))]
    return RouteLine(route_id, name, points, rs, route_type)


def routes_from_city_graph(graph: dict, route_ids: set[str] | None = None) -> dict[str, RouteLine]:
    """build_city_graph 출력(graph.json)에서 노선 선형을 만든다. 정류소 위치는 route_stop.offset_m을 쓴다."""
    seg = {s["segment_id"]: s for s in graph["segments"]}
    stop_info = {s["stop_id"]: s for s in graph["stops"]}
    info = {r["route_id"]: r for r in graph["routes"]}
    by_route: dict[str, list[dict]] = {}
    for r in graph["route_segment"]:
        by_route.setdefault(r["route_id"], []).append(r)
    stops_by_route: dict[str, list[dict]] = {}
    for r in graph["route_stop"]:
        stops_by_route.setdefault(r["route_id"], []).append(r)

    out = {}
    for rid, rows in by_route.items():
        if route_ids is not None and rid not in route_ids:
            continue
        rows.sort(key=lambda r: r["seq"])
        points: list[tuple[float, float]] = []
        segments: list[tuple[float, int]] = []
        occ: dict[int, list[float]] = {}     # 한 노선이 같은 구간을 두 번 지날 수 있다
        s_acc = 0.0
        for r in rows:
            sg = seg[r["segment_id"]]
            pts = [tuple(p) for p in sg["points"]]
            if points and points[-1] == pts[0]:  # 구간 이음매는 같은 좌표(그래프 검증 완료)
                pts = pts[1:]
            segments.append((s_acc, sg["segment_id"]))
            occ.setdefault(sg["segment_id"], []).append(s_acc)
            points.extend(pts)
            s_acc += sg["length_m"]
        rs, prev_s = [], 0.0
        for i, r in enumerate(sorted(stops_by_route.get(rid, []), key=lambda r: r["ord"])):
            starts = occ.get(r["segment_id"], [0.0])
            cand = [st for st in starts if st + r["offset_m"] >= prev_s - 1.0]
            s = (cand[0] if cand else starts[-1]) + r["offset_m"]
            si = stop_info.get(r["stop_id"], {})
            rs.append(RouteStop(i, r["stop_id"], si.get("name", ""), s, si.get("lat", 0.0), si.get("lng", 0.0)))
            prev_s = s
        meta = info.get(rid, {})
        line = RouteLine(rid, meta.get("route_name", "").strip(), points, rs,
                         meta.get("route_type", "").strip(), segments)
        out[rid] = line
    return out


def routes_from_snapshot(routes_json: dict, fix_fn=None) -> dict[str, RouteLine]:
    """record_realtime.py의 routes.json에서 노선을 만든다. fix_fn은 선형 보정 함수(core.graph.fix_shape.fix)."""
    out = {}
    for rid, r in routes_json["routes"].items():
        if "shape" not in r or not r.get("stops"):
            continue
        shape = r["shape"]
        if fix_fn is not None:
            shape, _ = fix_fn(shape)
        stops = [(s["stop_id"], s["name"], s["lat"], s["lng"]) for s in r["stops"]]
        out[rid] = route_from_points(rid, r.get("route_name", ""), [tuple(p) for p in shape], stops)
    return out


# --------------------------------------------------------------------------- 위치 판정

@dataclass
class Params:
    road_tol_m: float = 30.0       # 검사 2: 이 안이면 정상(ok)
    snap_tol_m: float = 80.0       # 검사 2: 이 안이면 GPS 오차로 보고 선형에 붙임(snapped)
    off_route_after: int = 3       # snap_tol_m 밖 좌표가 이만큼 연달아 오면 경로 이탈(원시 좌표 표시)
    back_tol_m: float = 30.0       # 검사 3: 보고 정류소보다 뒤쪽 여유
    ahead_stops: int = 3           # 검사 3: 앞쪽 허용 정류소 수
    ahead_tol_m: float = 30.0
    backward_tol_m: float = 20.0   # 검사 4
    park_radius_m: float = 80.0    # 정류소 정차
    origin_leave_m: float = 50.0   # 기점 정류소에서 이만큼 나아가면 기점 대기가 아니라 운행
    stale_after_s: float = 60.0    # 새 정보 없이 이만큼 지나면 stale
    forget_after_s: float = 180.0  # 목록에서 빠진 차량 상태 삭제
    lat_range: tuple[float, float] = (36.0, 37.5)
    lng_range: tuple[float, float] = (126.0, 128.0)


# 상태
RUNNING, AT_STOP, ORIGIN_WAIT, TERMINAL_WAIT, UNKNOWN = "running", "at_stop", "origin_wait", "terminal_wait", "unknown"
OFF_ROUTE = "off_route"   # 경로 이탈 = 화면 표기 "GPS 위치 오류". lat/lng는 원시 좌표, s는 마지막 노선 위 위치
# 위치 근거
SRC_GPS, SRC_STOP, SRC_HOLD, SRC_NONE, SRC_RAW = "gps", "stop", "hold", "none", "raw"


@dataclass
class Position:
    veh_id: str
    route_id: str
    ts: float
    state: str
    source: str
    s: float | None = None
    lat: float | None = None
    lng: float | None = None
    segment_id: int | None = None
    offset_m: float | None = None
    stop_idx: int | None = None          # 표시 정류소(지났거나 서 있는 마지막 정류소)
    reported_idx: int | None = None      # API가 보고한 정류소
    info_age_s: float = 0.0              # 마지막으로 새 정보가 들어온 뒤 지난 시간
    stale: bool = False
    check: str = ""                      # 좌표 판정 결과 (ok, lat=lng, off_road, out_of_window, backward, ...)
    raw_lat: float | None = None
    raw_lng: float | None = None
    raw_dist_m: float | None = None      # 원시 좌표 ~ 선형 수직 거리
    raw_s: float | None = None           # 원시 좌표를 선형에 투영한 거리(snap_tol_m 안일 때). 선형 의심 구간 집계용
    raw_segment_id: int | None = None    # raw_s가 속한 구간
    route_changed: bool = False
    extra: dict = field(default_factory=dict)  # 원본 필드 일부(plate_no, low_flag 등)


@dataclass
class _VehState:
    route_id: str
    s: float
    reported_idx: int
    t_info: float                  # 새 정보가 마지막으로 들어온 시각
    last_acc: tuple | None         # 마지막으로 받아들인 원시 좌표
    last_seen: float
    off_streak: int = 0            # snap_tol_m 밖 좌표 연속 횟수 (좌표 없는 관측은 세지도 끊지도 않음)
    last_off: tuple | None = None  # 마지막 경로 밖 원시 좌표


def _parse_coord(v: dict) -> tuple[float | None, float | None]:
    try:
        lat, lng = float(v.get("lat", "")), float(v.get("lng", ""))
    except (TypeError, ValueError):
        return None, None
    return lat, lng


class Tracker:
    """차량별 직전 상태를 기억하며 위치 응답을 위치로 바꾼다. 스레드 안전하지 않다(폴러 한 곳에서만 호출)."""

    KEEP_FIELDS = ("plate_no", "low_flag", "speed", "angle", "stop_name")

    def __init__(self, routes: dict[str, RouteLine], params: Params | None = None) -> None:
        self.routes = routes
        self.p = params or Params()
        self.vehicles: dict[str, _VehState] = {}
        self.positions: dict[str, Position] = {}   # 차량별 최신 위치

    # -- 공개 API
    def update(self, route_id: str, vehicles: list[dict], ts: float) -> list[Position]:
        """노선 하나의 위치 응답(busRealLocList)을 처리한다. ts는 수신 시각(epoch 초)."""
        out = []
        for v in vehicles:
            pos = self._locate(route_id, v, ts)
            self.positions[pos.veh_id] = pos
            out.append(pos)
        return out

    def expire(self, ts: float) -> list[str]:
        """forget_after_s 동안 보이지 않은 차량을 지운다. 지운 veh_id 목록을 돌려준다."""
        gone = [k for k, st in self.vehicles.items() if ts - st.last_seen > self.p.forget_after_s]
        for k in gone:
            self.vehicles.pop(k, None)
            self.positions.pop(k, None)
        return gone

    def current(self) -> list[Position]:
        return list(self.positions.values())

    # -- 내부
    def _coord_ok(self, lat, lng) -> str:
        if lat is None:
            return "no_coord"
        if lat == lng:
            return "lat=lng"
        (a, b), (c, d) = self.p.lat_range, self.p.lng_range
        if not (a < lat < b and c < lng < d):
            return "out_of_range"
        return "ok"

    def _locate(self, route_id: str, v: dict, ts: float) -> Position:
        p = self.p
        veh = str(v.get("veh_id", "")).strip()
        extra = {k: v[k] for k in self.KEEP_FIELDS if k in v}
        lat, lng = _parse_coord(v)
        line = self.routes.get(route_id)
        st = self.vehicles.get(veh)
        route_changed = bool(st and st.route_id != route_id)
        if route_changed:
            st = None

        base = dict(veh_id=veh, route_id=route_id, ts=ts, raw_lat=lat, raw_lng=lng,
                    route_changed=route_changed, extra=extra)
        if line is None:
            return Position(state=UNKNOWN, source=SRC_NONE, check="route_unknown", **base)

        # 보고 정류소 순번. 같은 stop_id가 두 번이면 직전 보고 순번 이후 가장 가까운 것.
        cands = line.stop_index.get(str(v.get("stop_id", "")).strip())
        if not cands:
            if st:  # 노선 데이터와 어긋남(스냅샷 이후 노선 변경 의심): 직전 위치 유지
                st.last_seen = ts
                return self._emit(line, st, ts, SRC_HOLD, "stop_unmapped", None, base, st.reported_idx)
            return Position(state=UNKNOWN, source=SRC_NONE, check="stop_unmapped", **base)
        if st and len(cands) > 1:
            ahead = [c for c in cands if c >= st.reported_idx]
            k = ahead[0] if ahead else cands[-1]
        else:
            k = cands[0]
        s_k = line.stop_s(k)

        # 좌표 판정
        check = self._coord_ok(lat, lng)
        accepted_s = None
        raw_dist = raw_s = None
        parked = False
        if check == "ok":
            s_lo = s_k - p.back_tol_m
            s_hi = (line.stop_s(k + p.ahead_stops) if k + p.ahead_stops <= line.last_idx
                    else line.length) + p.ahead_tol_m
            d, s = line.project(lat, lng, s_lo, s_hi)
            raw_dist = round(d, 1)
            if d <= p.snap_tol_m:
                raw_s = s
                if st and s < st.s - p.backward_tol_m:
                    check = "backward"
                else:
                    accepted_s = max(s, st.s) if st else s   # 작은 흔들림으로 뒤로 가지 않게
                    check = "ok" if d <= p.road_tol_m else "snapped"
            else:
                sx, sy = to_xy(line.stops[k].lat, line.stops[k].lng)
                qx, qy = to_xy(lat, lng)
                if math.hypot(qx - sx, qy - sy) <= p.park_radius_m:
                    parked = True
                    check = "parked"
                else:
                    # 선형 전체에서 가까우면 구간 밖(정류소 보고와 안 맞음), 아니면 도로 밖
                    d_all, _ = line.project(lat, lng, 0.0, line.length)
                    check = "out_of_window" if d_all <= p.snap_tol_m else "off_road"

        # 경로 밖 연속 횟수. 좌표가 없는 관측(lat=lng 등)은 세지도 끊지도 않는다.
        streak = st.off_streak if st else 0
        last_off = st.last_off if st else None
        # 구간 밖(노선의 다른 부분 근처)도 "있어야 할 곳에 없음"이므로 함께 센다(1060번 우회: 옆길이 노선 다른 부분과 가까움).
        if check in ("off_road", "out_of_window"):
            streak, last_off = streak + 1, (lat, lng)
        elif check in ("ok", "snapped", "parked", "backward"):
            streak, last_off = 0, None
        off_route = streak >= p.off_route_after and 0 < k < line.last_idx and last_off is not None

        # 위치 결정
        if accepted_s is not None:
            s_new, src = accepted_s, SRC_GPS
        elif parked:
            s_new, src = max(s_k, st.s) if st else s_k, SRC_STOP
        elif st and st.s >= s_k and line.stop_at(st.s) <= k + p.ahead_stops:
            s_new, src = st.s, SRC_HOLD      # 같은 정류소 구간 안: 직전 위치 유지
        else:
            s_new, src = s_k, SRC_STOP       # 정류소가 바뀌었거나 처음 본 차량
        if st and s_new < st.s:              # 어떤 경우에도 표시 위치는 뒤로 가지 않는다
            s_new = st.s

        # 새 정보: 보고 정류소가 바뀌었거나, 받아들인 좌표가 직전에 받아들인 좌표와 다름
        acc = (lat, lng) if src == SRC_GPS else None
        new_off = off_route and check in ("off_road", "out_of_window")   # 이탈 중 새 원시 좌표도 새 정보
        if st is None:
            st = _VehState(route_id, s_new, k, ts, acc, ts)
            self.vehicles[veh] = st
        else:
            if k != st.reported_idx or (acc is not None and acc != st.last_acc) or \
                    (new_off and (lat, lng) != st.last_off):
                st.t_info = ts
            if acc is not None:
                st.last_acc = acc
            st.s, st.reported_idx = s_new, k
        st.off_streak, st.last_off = streak, last_off
        st.last_seen = ts
        pos = self._emit(line, st, ts, src, check, raw_dist, base, k, parked)
        if raw_s is not None:
            pos.raw_s = round(raw_s, 1)
            pos.raw_segment_id = line.segment_at(raw_s)[0]
        if off_route:
            # 경로 이탈(GPS 위치 오류): 원시 좌표에 표시. 노선 위 진행(s)은 마지막 값을 유지한다.
            pos.state, pos.source = OFF_ROUTE, SRC_RAW
            pos.lat, pos.lng = last_off
            pos.segment_id = pos.offset_m = None
            pos.stale = False
        return pos

    def _emit(self, line: RouteLine, st: _VehState, ts: float, src: str, check: str,
              raw_dist, base: dict, k: int, parked: bool = False) -> Position:
        s = st.s
        lat, lng = line.point_at(s)
        seg_id, off = line.segment_at(s)
        shown = line.stop_at(s)
        if k == 0 and (s <= line.stop_s(0) + self.p.origin_leave_m or src != SRC_GPS):
            # 보고 정류소가 기점인데 GPS로 기점을 벗어난 것이 확인될 때만 운행으로 본다.
            # 대기 공간이 선형 옆에 있으면 GPS가 잠깐 앞으로 잡혔다가 버려지기 때문(991 천안아산역).
            state = ORIGIN_WAIT
        elif k == line.last_idx:
            state = TERMINAL_WAIT
        elif parked:
            state = AT_STOP
        else:
            state = RUNNING
        age = ts - st.t_info
        return Position(state=state, source=src, s=round(s, 1), lat=round(lat, 7), lng=round(lng, 7),
                        segment_id=seg_id, offset_m=off, stop_idx=shown, reported_idx=k,
                        info_age_s=round(age, 1), stale=(state == RUNNING and age >= self.p.stale_after_s),
                        check=check, raw_dist_m=raw_dist, **base)
