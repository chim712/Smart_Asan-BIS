#!/usr/bin/env python3
"""도로 그래프 시각화 (matplotlib 필요).

build_road_graph.py 출력 JSON으로 그림 두 장을 만든다.
  1) *_geo.svg       : 실제 위도·경도 위에 도로 구간과 정류소를 그린 지도형 그림
  2) *_schematic.svg : 본선축을 일자로 펴고, 지선을 위도 기준(북쪽이 위)으로 배치한 노선도

프로젝트 루트에서 모듈로 실행한다.
    python -m analysis.draw_road_graph asan_series_4_road_dir1.json --axis 451
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from core.geo import KX, KY

STRAND_COLORS = ["#d6336c", "#1c7ed6", "#2f9e44", "#e8590c", "#7048e8", "#0c8599"]
SHARED_COLOR = "#343a40"
LNG_SCALE = KX / KY  # 경도 1도 길이 / 위도 1도 길이 (core.geo 기준)


def setup_font() -> None:
    for name in ("Noto Sans CJK KR", "NanumGothic", "Malgun Gothic", "AppleGothic"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["svg.fonttype"] = "none"


def strand_colors(graph: dict) -> tuple[dict, list]:
    totals: dict[str, int] = defaultdict(int)
    for route in graph["routes"].values():
        totals[route["strand"]] += route["trips"]
    ranked = sorted(totals, key=lambda s: -totals[s])
    return {s: STRAND_COLORS[i % len(STRAND_COLORS)] for i, s in enumerate(ranked)}, \
           [(s, totals[s]) for s in ranked]


def seg_color(seg: dict, colors: dict) -> str:
    return colors[seg["strands"][0]] if len(seg["strands"]) == 1 else SHARED_COLOR


def seg_width(seg: dict, max_trips: int) -> float:
    return 1.2 + 6.0 * seg["trips"] / max_trips


def legend_text(ranked: list) -> str:
    return " / ".join(f"{name} 가닥 {trips}회" for name, trips in ranked) + "  (검정 = 여러 가닥 공용)"


# ---------------------------------------------------------------- 1) 지도형
def draw_geo(graph: dict, out: str, title: str) -> None:
    colors, ranked = strand_colors(graph)
    segments, stops, nodes = graph["segments"], graph["stops"], graph["nodes"]
    max_trips = max(s["trips"] for s in segments)

    fig, ax = plt.subplots(figsize=(30, 13))
    for seg in sorted(segments, key=lambda s: s["trips"]):
        xs = [p[1] * LNG_SCALE for p in seg["points"]]
        ys = [p[0] for p in seg["points"]]
        ax.plot(xs, ys, color=seg_color(seg, colors), lw=seg_width(seg, max_trips),
                solid_capstyle="round", zorder=2,
                linestyle=(0, (2, 1.5)) if seg["from"] == seg["to"] else "-")
        if seg["length_m"] > 400:
            mid = seg["points"][len(seg["points"]) // 2]
            ax.annotate(f'{",".join(seg["route_names"])}\n{seg["trips"]}회',
                        (mid[1] * LNG_SCALE, mid[0]), fontsize=7, ha="center", va="center",
                        color=seg_color(seg, colors), zorder=6,
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85))

    for stop in stops.values():
        ax.plot(stop["snap"][1] * LNG_SCALE, stop["snap"][0], "o", ms=3.2,
                mfc="white", mec="#495057", mew=0.7, zorder=4)

    # 분기(나가는 구간 2개 이상)는 삼각형, 합류(들어오는 구간 2개 이상)는 사각형
    for node in nodes.values():
        if node["out"] > 1:
            ax.plot(node["lng"] * LNG_SCALE, node["lat"], "^", ms=9, color="#f08c00",
                    mec="black", mew=0.6, zorder=5)
        elif node["in"] > 1:
            ax.plot(node["lng"] * LNG_SCALE, node["lat"], "s", ms=7, color="#74c0fc",
                    mec="black", mew=0.6, zorder=5)

    # 정류소 이름: 구간의 첫·끝 정류소와 일정 간격의 정류소만 표기
    labelled = set()
    for seg in segments:
        ids = seg["stops"]
        for idx, sid in enumerate(ids):
            if idx in (0, len(ids) - 1) or idx % 4 == 0:
                labelled.add(sid)
    for sid in labelled:
        stop = stops[sid]
        ax.annotate(stop["name"], (stop["snap"][1] * LNG_SCALE, stop["snap"][0]),
                    xytext=(3, 4), textcoords="offset points", fontsize=6.5,
                    color="#212529", rotation=25, zorder=7)

    ax.plot([], [], "^", ms=9, color="#f08c00", mec="black", label="분기점")
    ax.plot([], [], "s", ms=7, color="#74c0fc", mec="black", label="합류점")
    ax.plot([], [], "o", ms=4, mfc="white", mec="#495057", label="정류소")
    ax.legend(loc="lower right", fontsize=10)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(f"{title} - 도로 그래프 (실제 좌표)\n{legend_text(ranked)}", fontsize=15)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


# ---------------------------------------------------------------- 2) 노선도형
def seg_span(seg: dict) -> float:
    """노선도에서 구간이 차지하는 가로 길이. 정류소를 같은 간격으로 놓는다."""
    return len(seg["stops"]) + 1 if seg["stops"] else 0.4


def mean_lat(seg: dict) -> float:
    return sum(p[0] for p in seg["points"]) / len(seg["points"])


def layout_schematic(graph: dict, axis_route: str) -> tuple[dict, dict, dict, set]:
    """노드의 x, 노드의 줄(y), 구간의 줄(y)을 정한다.

    x  : 기점에서의 최장 경로 길이. 우회 구간이 본선보다 길면 본선 쪽이 늘어나 맞춰진다.
    y  : 본선축은 0. 본선 밖 구간은 이어진 것끼리 지선 하나로 묶고,
         본선과 만나는 지점보다 북쪽이면 위(+), 남쪽이면 아래(-) 줄에 놓는다.
    """
    segments, nodes = graph["segments"], graph["nodes"]
    axis_rid = next(rid for rid, r in graph["routes"].items() if r["name"] == axis_route)
    axis_set = set(graph["routes"][axis_rid]["segment_path"])
    axis_nodes = {n for sid in axis_set for n in (segments[sid]["from"], segments[sid]["to"])}
    live = [s for s in segments if s["from"] != s["to"]]

    # --- x: 위상 순서대로 최장 경로
    indeg = defaultdict(int)
    for seg in live:
        indeg[seg["to"]] += 1
    all_nodes = {n for s in live for n in (s["from"], s["to"])}
    x = {n: 0.0 for n in all_nodes}
    queue = [n for n in all_nodes if indeg[n] == 0]
    out_of = defaultdict(list)
    for seg in live:
        out_of[seg["from"]].append(seg)
    while queue:
        node = queue.pop()
        for seg in out_of[node]:
            x[seg["to"]] = max(x[seg["to"]], x[node] + seg_span(seg))
            indeg[seg["to"]] -= 1
            if indeg[seg["to"]] == 0:
                queue.append(seg["to"])
    # 다른 곳에서 들어오는 기점은 합류 지점에 바짝 붙인다 (여유 공간을 앞쪽으로 보낸다)
    into = defaultdict(list)
    for seg in live:
        into[seg["to"]].append(seg)
    for _ in range(len(all_nodes)):
        for node in all_nodes:
            if node not in axis_nodes and out_of[node]:
                x[node] = min(x[s["to"]] - seg_span(s) for s in out_of[node])

    # --- y: 본선 밖 구간을 지선 단위로 묶는다
    off = [s for s in live if s["id"] not in axis_set]
    parent = {s["id"]: s["id"] for s in off}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    by_node = defaultdict(list)
    for seg in off:
        for node in (seg["from"], seg["to"]):
            if node not in axis_nodes:
                by_node[node].append(seg["id"])
    for ids in by_node.values():
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
    groups = defaultdict(list)
    for seg in off:
        groups[find(seg["id"])].append(seg)

    node_y = {n: 0.0 for n in all_nodes}
    seg_y = {s["id"]: 0.0 for s in live}
    placed: dict[int, list] = {1: [], -1: []}
    for group in sorted(groups.values(), key=lambda g: -sum(s["length_m"] for s in g)):
        touch = {n for s in group for n in (s["from"], s["to"]) if n in axis_nodes}
        ref_lat = sum(nodes[str(n)]["lat"] for n in touch) / len(touch)
        total = sum(s["length_m"] for s in group)
        lat = sum(mean_lat(s) * s["length_m"] for s in group) / total
        side = 1 if lat >= ref_lat else -1
        xs = [x[n] for s in group for n in (s["from"], s["to"])]
        lo, hi = min(xs), max(xs)
        lane = 1
        while any(l == lane and not (hi < a - 1 or lo > b + 1) for a, b, l in placed[side]):
            lane += 1
        placed[side].append((lo, hi, lane))
        for seg in group:
            seg_y[seg["id"]] = side * lane
            for node in (seg["from"], seg["to"]):
                if node not in axis_nodes:
                    node_y[node] = side * lane
    return x, node_y, seg_y, axis_set


def draw_schematic(graph: dict, out: str, title: str, axis_route: str) -> None:
    colors, ranked = strand_colors(graph)
    segments, stops, routes = graph["segments"], graph["stops"], graph["routes"]
    max_trips = max(s["trips"] for s in segments)
    x, node_y, seg_y, axis_set = layout_schematic(graph, axis_route)
    lane_h, bend = 3.2, 0.8

    width = max(x.values()) - min(x.values())
    fig, ax = plt.subplots(figsize=(max(24, width * 0.42), 13))

    parallel = defaultdict(int)
    for seg in sorted(segments, key=lambda s: -s["trips"]):
        if seg["from"] == seg["to"]:
            continue
        x0, y0 = x[seg["from"]], node_y[seg["from"]] * lane_h
        x1, y1 = x[seg["to"]], node_y[seg["to"]] * lane_h
        key = (seg["from"], seg["to"], seg_y[seg["id"]])
        ys = seg_y[seg["id"]] * lane_h + parallel[key] * 1.0  # 같은 줄에서 같은 두 점을 잇는 구간은 살짝 띄운다
        parallel[key] += 1
        color, lw = seg_color(seg, colors), seg_width(seg, max_trips)

        # 시작 노드 -> (꺾어서) 구간의 줄 -> (꺾어서) 끝 노드
        fx0 = x0 + bend if abs(y0 - ys) > 1e-9 else x0
        fx1 = x1 - bend if abs(y1 - ys) > 1e-9 else x1
        if fx1 < fx0:
            fx0 = fx1 = (x0 + x1) / 2
        path = [(x0, y0), (fx0, ys), (fx1, ys), (x1, y1)]
        ax.plot([p[0] for p in path], [p[1] for p in path], color=color, lw=lw,
                solid_capstyle="round", solid_joinstyle="round",
                zorder=2 + seg["trips"] / (max_trips + 1))

        count = len(seg["stops"])
        on_axis = seg["id"] in axis_set
        above = ys >= 0
        for i, sid in enumerate(seg["stops"], start=1):
            sx = fx0 + (fx1 - fx0) * i / (count + 1)
            ax.plot(sx, ys, "o", ms=5, mfc="white", mec=color, mew=1.2, zorder=4)
            ax.annotate(stops[sid]["name"], (sx, ys),
                        xytext=(0, 7 if above else -7), textcoords="offset points",
                        rotation=55, fontsize=7 if on_axis else 6.5,
                        ha="left" if above else "right", va="bottom" if above else "top",
                        rotation_mode="anchor", color="#212529", zorder=6)
        if count >= 1 or seg["length_m"] > 300:
            ax.annotate(f'{",".join(seg["route_names"])} · {seg["trips"]}회',
                        ((fx0 + fx1) / 2, ys), xytext=(0, -11 if above else 11),
                        textcoords="offset points", fontsize=7, ha="center",
                        va="top" if above else "bottom", color=color, zorder=6,
                        bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.8))

    # 노선 기점·종점 표시
    ends, starts = defaultdict(list), defaultdict(list)
    for route in routes.values():
        ends[segments[route["segment_path"][-1]]["to"]].append(route["name"])
        starts[segments[route["segment_path"][0]]["from"]].append(route["name"])
    for node, names in ends.items():
        ax.plot(x[node], node_y[node] * lane_h, "s", ms=9, color="#212529", zorder=5)
        ax.annotate(f'{",".join(sorted(names))} 종점', (x[node], node_y[node] * lane_h),
                    xytext=(0, -26), textcoords="offset points", fontsize=8, fontweight="bold",
                    ha="center", color="#212529", zorder=7)
    for node, names in starts.items():
        ax.plot(x[node], node_y[node] * lane_h, "o", ms=9, color="#212529", zorder=5)
        ax.annotate(f'{",".join(sorted(names))} 기점', (x[node], node_y[node] * lane_h),
                    xytext=(-8, -4), textcoords="offset points", fontsize=8, fontweight="bold",
                    ha="right", color="#212529", zorder=7)

    ys_all = [v * lane_h for v in node_y.values()] + [v * lane_h for v in seg_y.values()]
    ax.set_ylim(min(ys_all) - 4.5, max(ys_all) + 4.5)
    ax.set_xlim(min(x.values()) - 4, max(x.values()) + 3)
    ax.axis("off")
    ax.set_title(f"{title} - 본선축({axis_route}번 경로) 기준 노선도, 위쪽이 북쪽\n{legend_text(ranked)}",
                 fontsize=15)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="도로 그래프 시각화")
    parser.add_argument("input", help="build_road_graph.py 출력 JSON")
    parser.add_argument("--axis", default=None,
                        help="본선축으로 삼을 노선번호. 생략하면 지도형 그림만 만든다")
    parser.add_argument("--format", default="svg", choices=["svg", "png"])
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as fp:
        graph = json.load(fp)
    setup_font()
    meta = graph["meta"]
    title = f'{meta["series"]}00번대 계열 {"상행" if meta["direction"] == "1" else "하행"}'
    base = args.input.rsplit(".", 1)[0]
    draw_geo(graph, f"{base}_geo.{args.format}", title)
    print(f"저장: {base}_geo.{args.format}")
    if args.axis:
        draw_schematic(graph, f"{base}_schematic.{args.format}", title, args.axis)
        print(f"저장: {base}_schematic.{args.format}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
