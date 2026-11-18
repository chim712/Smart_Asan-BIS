"""기하 공통 함수. 좌표는 (lat, lng) 순서다.

아산(위도 36.78°) 부근 평면 근사: 경도 1도 = 111,320·cos(36.78°) m, 위도 1도 = 110,574 m.
파이프라인과 analysis 모두 이 기준 하나만 쓴다. 표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import math

LAT0 = 36.78                                  # 기준 위도
LNG0 = 127.0                                  # 평면 좌표 원점 경도(시각화용)
KX = 111_320 * math.cos(math.radians(LAT0))  # 경도 1도(m)
KY = 110_574                                  # 위도 1도(m)


def xy(p, origin=None) -> tuple[float, float]:
    """(lat, lng) → 평면 (x, y) m. origin=(lat, lng)을 주면 그 점이 (0, 0)이다."""
    if origin is None:
        return p[1] * KX, p[0] * KY
    return (p[1] - origin[1]) * KX, (p[0] - origin[0]) * KY


def ll(x, y, origin) -> tuple[float, float]:
    """xy(p, origin)의 역변환 → (lat, lng)."""
    return y / KY + origin[0], x / KX + origin[1]


def dist_m(a, b) -> float:
    """두 (lat, lng) 사이 거리(m)."""
    return math.hypot((a[0] - b[0]) * KY, (a[1] - b[1]) * KX)


def project(p, a, b) -> tuple[float, float]:
    """점 p를 선분 a-b에 투영 → (거리 m, 비율 t ∈ [0, 1])."""
    ax, ay, bx, by = a[1] * KX, a[0] * KY, b[1] * KX, b[0] * KY
    px, py = p[1] * KX, p[0] * KY
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / n))
    return math.hypot(px - ax - t * dx, py - ay - t * dy), t


def dedupe(points) -> list[tuple]:
    """연속 중복 좌표 제거. 결과는 (lat, lng) tuple 목록."""
    out: list[tuple] = []
    for lat, lng in points:
        p = (lat, lng)
        if not out or out[-1] != p:
            out.append(p)
    return out


def length_m(points) -> float:
    """점열 길이(m)."""
    return sum(dist_m(a, b) for a, b in zip(points, points[1:]))
