// 선택(권역·노선) → 화면에 그릴 강조 줄(lane)과 부분 그래프 요약을 계산한다.

export const ROUTE_COLORS = ['#d6336c', '#1c7ed6', '#2f9e44', '#e8590c', '#7048e8', '#0c8599', '#f59f00', '#5c5f66']

export const PUBLIC_TYPE = '101' // 공영버스

function laneWidth(trips) {
  // 운행횟수 √ 비례 (px)
  return Math.max(1.2, Math.min(5.6, 0.8 + Math.sqrt(trips) * 0.44))
}

// 권역 선택: 구간마다 [{key: 계열, color, trips, width}] (계열 순서대로)
export function groupLanes(data, groups, excludePublic) {
  const lanes = new Map()
  if (!groups.length) return lanes
  const on = new Set(groups)
  for (const s of data.segments) {
    const acc = new Map()
    for (const ri of s.routes) {
      const r = data.routes[ri]
      if (!on.has(r.group) || (excludePublic && r.type === PUBLIC_TYPE)) continue
      acc.set(r.group, (acc.get(r.group) || 0) + r.trips)
    }
    if (!acc.size) continue
    const list = [...acc.entries()]
      .sort((a, b) => data.groupByCode[a[0]].order - data.groupByCode[b[0]].order)
      .map(([g, trips]) => ({ key: g, color: data.groupByCode[g].color, trips, width: laneWidth(trips) }))
    lanes.set(s.i, list)
  }
  return lanes
}

// 권역 선택에 들어가는 노선 인덱스
export function groupRoutes(data, groups, excludePublic) {
  const on = new Set(groups)
  return data.routes
    .filter((r) => on.has(r.group) && !(excludePublic && r.type === PUBLIC_TYPE))
    .map((r) => r.i)
}

export const PREVIEW_COLOR = '#111111'

export function routeColor(k) {
  return ROUTE_COLORS[k % ROUTE_COLORS.length]
}

// 권역 노선 목록: [{group, names: [{name, routes:[route 인덱스], trips, st, ed, isPublic}]}] (계열 순서, 노선명 자연 정렬)
export function groupRouteNames(data, groups, excludePublic) {
  const on = new Set(groups)
  const out = []
  for (const g of data.groups) {
    if (!on.has(g.code)) continue
    const names = []
    for (const name of data.names) {
      const rs = data.byName.get(name).filter((ri) => {
        const r = data.routes[ri]
        return r.group === g.code && !(excludePublic && r.type === PUBLIC_TYPE)
      })
      if (!rs.length) continue
      const lead = data.routes[rs[0]]
      names.push({
        name,
        routes: rs,
        trips: rs.reduce((a, ri) => a + data.routes[ri].trips, 0),
        st: lead.st,
        ed: lead.ed,
        isPublic: rs.every((ri) => data.routes[ri].type === PUBLIC_TYPE),
      })
    }
    // 시내·순환 노선(400, 402 …)을 먼저, 공영(40, 41 …)을 뒤에. 각각 노선명 자연 정렬은 유지한다.
    names.sort((a, b) => a.isPublic - b.isPublic)
    out.push({ group: g, names })
  }
  return out
}

// 노선 선택: 노선명마다 색, 숨긴 방향(route 인덱스) 제외. 구간마다 [{key: 노선명, color, width}]
// colorOf(name, k)로 색을 바꿀 수 있다(미리보기 등).
export function routeLanes(data, names, hidden, colorOf = (_name, k) => routeColor(k)) {
  const lanes = new Map()
  const visible = []
  names.forEach((name, k) => {
    const color = colorOf(name, k)
    for (const ri of data.byName.get(name) || []) {
      if (hidden.has(ri)) continue
      visible.push({ ri, color, name })
      for (const si of data.routes[ri].segs) {
        if (!lanes.has(si)) lanes.set(si, [])
        const list = lanes.get(si)
        if (!list.some((l) => l.key === name)) list.push({ key: name, color, width: 3.2, order: k })
      }
    }
  })
  for (const list of lanes.values()) list.sort((a, b) => a.order - b.order)
  return { lanes, visible }
}

// 강조된 구간 집합의 부분 그래프: 분기(out>1)·합류(in>1) 노드와 요약
export function subgraph(data, segIdx) {
  const outDeg = new Map()
  const inDeg = new Map()
  let length = 0
  for (const si of segIdx) {
    const s = data.segments[si]
    outDeg.set(s.from, (outDeg.get(s.from) || 0) + 1)
    inDeg.set(s.to, (inDeg.get(s.to) || 0) + 1)
    length += s.len
  }
  const branch = [...outDeg].filter(([, v]) => v > 1).map(([n]) => n)
  const merge = [...inDeg].filter(([, v]) => v > 1).map(([n]) => n)
  return { branch, merge, length, segments: segIdx.length }
}
