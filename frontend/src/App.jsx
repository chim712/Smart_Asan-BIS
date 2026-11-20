import { useEffect, useMemo, useState } from 'react'
import { loadData } from './data.js'
import MapView from './MapView.jsx'
import Sidebar from './Sidebar.jsx'
import { PREVIEW_COLOR, groupLanes, groupRoutes, routeColor, routeLanes, subgraph } from './selection.js'

// URL 해시 ↔ 선택 상태 (#g=4,6&r=450,430&p=1&n=0)
function readHash() {
  const q = new URLSearchParams(location.hash.slice(1))
  const list = (k) => (q.get(k) ? q.get(k).split(',').filter(Boolean) : [])
  return {
    groups: list('g'),
    names: list('r'),
    excludePublic: q.get('p') === '1',
    showNodes: q.get('n') !== '0',
  }
}

function writeHash(s) {
  const q = new URLSearchParams()
  if (s.groups.length) q.set('g', s.groups.join(','))
  if (s.names.length) q.set('r', s.names.join(','))
  if (s.excludePublic) q.set('p', '1')
  if (!s.showNodes) q.set('n', '0')
  const h = q.toString().replace(/%2C/g, ',')
  history.replaceState(null, '', h ? `#${h}` : location.pathname + location.search)
}

export default function App() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [sel, setSel] = useState(readHash)
  const [hidden, setHidden] = useState(() => new Set()) // 숨긴 방향(route 인덱스)
  const [hover, setHover] = useState(null)
  const [preview, setPreview] = useState(null) // 권역 노선 목록에서 마우스를 올린 노선명

  useEffect(() => {
    loadData().then(setData).catch((e) => setError(String(e)))
  }, [])

  useEffect(() => writeHash(sel), [sel])

  const names = useMemo(() => (data ? sel.names.filter((n) => data.byName.has(n)) : []), [data, sel.names])

  const groupLaneMap = useMemo(
    () => (data ? groupLanes(data, sel.groups, sel.excludePublic) : new Map()),
    [data, sel.groups, sel.excludePublic],
  )
  const routeSel = useMemo(
    () => (data ? routeLanes(data, names, hidden) : { lanes: new Map(), visible: [] }),
    [data, names, hidden],
  )
  // 지도에 그리는 노선 = 선택 노선 + 미리보기(선택되지 않은 노선 위에 마우스를 올렸을 때만)
  const drawSel = useMemo(() => {
    if (!data || !preview || names.includes(preview) || !data.byName.has(preview)) return routeSel
    return routeLanes(data, [...names, preview], hidden, (n, k) => (n === preview ? PREVIEW_COLOR : routeColor(k)))
  }, [data, routeSel, names, preview, hidden])
  // 분기·합류 표시와 요약은 노선 선택이 있으면 노선 기준, 없으면 권역 기준 (미리보기 제외)
  const focus = routeSel.lanes.size ? routeSel.lanes : groupLaneMap
  const graph = useMemo(() => (data ? subgraph(data, [...focus.keys()]) : null), [data, focus])
  const groupRouteIdx = useMemo(
    () => (data ? groupRoutes(data, sel.groups, sel.excludePublic) : []),
    [data, sel.groups, sel.excludePublic],
  )

  if (error) {
    return (
      <div className="message">
        <p>데이터를 불러오지 못했습니다: {error}</p>
        <p>프로젝트 루트에서 <code>python -m core.web.export</code>를 먼저 실행하세요.</p>
      </div>
    )
  }
  if (!data) return <div className="message">노선망을 불러오는 중…</div>

  return (
    <div className="app">
      <MapView
        data={data}
        groupLaneMap={groupLaneMap}
        routeSel={drawSel}
        nodeMarks={graph}
        showNodes={sel.showNodes && focus.size > 0}
        onHover={setHover}
      />
      <Sidebar
        data={data}
        sel={sel}
        setSel={setSel}
        names={names}
        hidden={hidden}
        setHidden={setHidden}
        graph={graph}
        groupRouteIdx={groupRouteIdx}
        routeSel={routeSel}
        setPreview={setPreview}
      />
      {hover && <Tooltip data={data} hover={hover} />}
    </div>
  )
}

function Tooltip({ data, hover }) {
  const s = data.segments[hover.i]
  const byName = new Map()
  for (const ri of s.routes) {
    const r = data.routes[ri]
    if (!byName.has(r.name)) byName.set(r.name, r)
  }
  const list = [...byName.values()]
  const shown = list.slice(0, 12)
  const left = Math.min(hover.x + 14, window.innerWidth - 260)
  const top = Math.min(hover.y + 14, window.innerHeight - 40 - shown.length * 18)
  return (
    <div className="tooltip" style={{ left, top }}>
      <div className="tt-head">
        평일 {s.trips}회 · {s.len >= 1000 ? `${(s.len / 1000).toFixed(2)}km` : `${s.len}m`} · 노선 {list.length}개
      </div>
      {shown.map((r) => (
        <div key={r.route_id} className="tt-row">
          <span className="dot" style={{ background: data.groupByCode[r.group]?.color }} />
          <b>{r.name}</b> <span className="muted">{r.st} → {r.ed}</span>
        </div>
      ))}
      {list.length > shown.length && <div className="muted">외 {list.length - shown.length}개</div>}
    </div>
  )
}
