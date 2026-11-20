"""② 선형(shape) 꼭짓점 순서 꼬임 보정.

링크 일부가 역순으로 저장되어 같은 직선 위에서 앞뒤로 왔다 갔다 하는 구간(지그재그)을 찾아
양 끝 기준점 사이 직선에 투영한 순서로 재정렬한다. 직선에서 벗어난 점이 있으면 손대지 않는다(실제 회차로 간주).
보정 내역은 출력 JSON meta.shape_fixes에 남는다. 입력 파일(raw.json)은 수정하지 않는다.

    python -m core.graph.fix_shape data/snapshots/2026-12-01/raw.json data/snapshots/2026-12-01/fixed.json
"""
from __future__ import annotations

import json
import math
import sys

from core.geo import dedupe, length_m, xy


def uturns(S, minlen=15, cos_th=-0.97) -> list[int]:
    P = [xy(p) for p in S]
    h = []
    for k in range(1, len(P) - 1):
        ax, ay = P[k][0] - P[k - 1][0], P[k][1] - P[k - 1][1]
        bx, by = P[k + 1][0] - P[k][0], P[k + 1][1] - P[k][1]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la < minlen or lb < minlen:
            continue
        if (ax * bx + ay * by) / la / lb < cos_th:
            h.append(k)
    return h


def fix(S, tol=30, gap=6) -> tuple[list[tuple], list[dict]]:
    """선형 S를 보정 → (보정 선형, 보정 내역). 점이 2개 미만이면 그대로 돌려준다."""
    S = dedupe(S)
    log = []
    if len(S) < 3:
        return S, log
    while True:
        h = uturns(S)
        # U턴이 gap 이내로 2개 이상 몰린 묶음
        groups, cur = [], []
        for k in h:
            if cur and k - cur[-1] > gap:
                groups.append(cur)
                cur = []
            cur.append(k)
        if cur:
            groups.append(cur)
        groups = [g for g in groups if len(g) >= 2]
        done = False
        for g in groups:
            # 기준점: 묶음 바로 앞뒤 점
            a, b = g[0] - 1, g[-1] + 1
            A, B = xy(S[a]), xy(S[b])
            ux, uy = B[0] - A[0], B[1] - A[1]
            L = math.hypot(ux, uy)
            if L < 1:
                continue
            ux, uy = ux / L, uy / L
            if not all(abs((xy(p)[0] - A[0]) * uy - (xy(p)[1] - A[1]) * ux) < tol for p in S[a:b + 1]):
                continue

            def proj(p):
                q = xy(p)
                return (q[0] - A[0]) * ux + (q[1] - A[1]) * uy

            mid = [p for p in sorted(S[a + 1:b], key=proj) if 0 < proj(p) < L]
            new = S[:a + 1] + mid + S[b:]
            old_len, new_len = length_m(S), length_m(new)
            if new_len < old_len - 1:
                log.append({"from_idx": a, "to_idx": b, "removed_m": round(old_len - new_len), "at": S[a]})
                S = dedupe(new)
                done = True
                break
        if not done:
            break
    return S, log


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 1
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as fp:
        d = json.load(fp)
    fixes, skipped = [], []
    for r in d["routes"]:
        shape = r.get("shape")
        if not shape or len(shape) < 2:      # 수집 실패 또는 --no-shape: ③에서 걸러진다
            skipped.append(r["info"]["route_id"])
            continue
        new, log = fix(shape)
        if log:
            r["shape"] = [list(p) for p in new]
            fixes.append({"route_id": r["info"]["route_id"], "route_name": r["info"]["route_name"].strip(),
                          "fixes": log})
    d["meta"]["shape_fixes"] = fixes
    with open(dst, "w", encoding="utf-8") as fp:
        json.dump(d, fp, ensure_ascii=False, separators=(",", ":"))
    for f in fixes:
        print(f["route_name"], f["route_id"], [(x["from_idx"], x["removed_m"], x["at"]) for x in f["fixes"]])
    if skipped:
        print(f"선형 없음(건너뜀) {len(skipped)}건: {', '.join(skipped[:10])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
