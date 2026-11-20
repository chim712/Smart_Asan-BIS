import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { laneOffsets, offsetPolyline, pathD, pointAt } from './geom.js'

// 저작권 표기 (좌측 상단). 표기 주체는 필요하면 여기서 바꾼다.
const COPYRIGHT_HOLDER = 'Smart Asan BIS by ch.im'
const COPYRIGHT_YEAR = 2026

const MIN_K = 0.005 // px per m
const MAX_K = 4

function fitView(bbox, w, h) {
  const pad = 24
  const k = Math.min((w - pad * 2) / (bbox.maxX - bbox.minX), (h - pad * 2) / (bbox.maxY - bbox.minY))
  return { cx: (bbox.minX + bbox.maxX) / 2, cy: (bbox.minY + bbox.maxY) / 2, k }
}

// 전체 망 (선택과 무관하게 그대로라 확대·이동 때 다시 그리지 않는다)
const BaseLayer = memo(function BaseLayer({ segments, maxTrips, dim }) {
  return (
    <g className="base" fill="none" strokeLinecap="round" strokeLinejoin="round">
      {segments.map((s) => (
        <path
          key={s.i}
          d={s.d}
          stroke={dim ? 'var(--base-dim)' : 'var(--base)'}
          strokeWidth={dim ? 0.8 : 0.56 + Math.sqrt(s.trips / maxTrips) * 3.2}
          vectorEffect="non-scaling-stroke"
        />
      ))}
    </g>
  )
})

// 마우스 판정용 넓은 투명 선
const HitLayer = memo(function HitLayer({ segments }) {
  return (
    <g className="hit" fill="none" stroke="transparent">
      {segments.map((s) => (
        <path key={s.i} data-i={s.i} d={s.d} strokeWidth={12} vectorEffect="non-scaling-stroke" />
      ))}
    </g>
  )
})

// lanes: Map<segIdx, [{key,color,width}]> → 평행선 path 목록
function buildLanePaths(segments, lanes, k) {
  const out = []
  for (const [si, list] of lanes) {
    const s = segments[si]
    const offs = laneOffsets(list.map((l) => l.width))
    list.forEach((l, j) => {
      out.push({ id: `${si}:${l.key}`, d: pathD(offsetPolyline(s.pts, offs[j] / k)), color: l.color, width: l.width })
    })
  }
  return out
}

function laneOffsetWorld(segments, lanes, si, key, k) {
  const list = lanes.get(si)
  if (!list) return 0
  const offs = laneOffsets(list.map((l) => l.width))
  const j = list.findIndex((l) => l.key === key)
  return j < 0 ? 0 : offs[j] / k
}

export default function MapView({ data, groupLaneMap, routeSel, nodeMarks, showNodes, onHover }) {
  const wrapRef = useRef(null)
  const svgRef = useRef(null)
  const [size, setSize] = useState({ w: 800, h: 600 })
  const [view, setView] = useState(null)
  const drag = useRef(null)

  useEffect(() => {
    const el = wrapRef.current
    const ro = new ResizeObserver(([e]) => {
      const { width, height } = e.contentRect
      setSize({ w: Math.max(100, width), h: Math.max(100, height) })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    setView((v) => v || fitView(data.bbox, size.w, size.h))
  }, [data, size])

  const zoomAt = useCallback((factor, sx, sy) => {
    setView((v) => {
      if (!v) return v
      const k = Math.min(MAX_K, Math.max(MIN_K, v.k * factor))
      const wx = v.cx + (sx - size.w / 2) / v.k
      const wy = v.cy + (sy - size.h / 2) / v.k
      return { k, cx: wx - (sx - size.w / 2) / k, cy: wy - (sy - size.h / 2) / k }
    })
  }, [size])

  useEffect(() => {
    const el = svgRef.current
    if (!el) return undefined // 첫 렌더(view 계산 전)에는 svg가 없다
    const onWheel = (e) => {
      e.preventDefault()
      const r = el.getBoundingClientRect()
      zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomAt, view != null])

  const onPointerDown = (e) => {
    drag.current = { x: e.clientX, y: e.clientY, moved: false }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e) => {
    const dg = drag.current
    if (dg) {
      const dx = e.clientX - dg.x
      const dy = e.clientY - dg.y
      if (Math.abs(dx) + Math.abs(dy) > 2) dg.moved = true
      dg.x = e.clientX
      dg.y = e.clientY
      setView((v) => ({ ...v, cx: v.cx - dx / v.k, cy: v.cy - dy / v.k }))
      onHover(null)
      return
    }
    const i = e.target.getAttribute?.('data-i')
    onHover(i == null ? null : { i: +i, x: e.clientX, y: e.clientY })
  }
  const onPointerUp = () => {
    drag.current = null
  }

  // 확대 단계를 양자화해 평행선·화살표 재계산 횟수를 줄인다
  const kq = view ? Math.pow(2, Math.round(Math.log2(view.k) * 4) / 4) : 1

  const groupPaths = useMemo(() => buildLanePaths(data.segments, groupLaneMap, kq), [data, groupLaneMap, kq])
  const routePaths = useMemo(() => buildLanePaths(data.segments, routeSel.lanes, kq), [data, routeSel, kq])

  const arrows = useMemo(() => {
    const out = []
    const size = 9 / kq
    for (const { ri, color, name } of routeSel.visible) {
      for (const si of data.routes[ri].segs) {
        const s = data.segments[si]
        if (s.len * kq < 70) continue
        const off = laneOffsetWorld(data.segments, routeSel.lanes, si, name, kq)
        const p = pointAt(off ? offsetPolyline(s.pts, off) : s.pts, 0.5)
        const { x, y, ux, uy } = p
        const pts = [
          [x + ux * size, y + uy * size],
          [x - ux * size * 0.6 - uy * size * 0.6, y - uy * size * 0.6 + ux * size * 0.6],
          [x - ux * size * 0.6 + uy * size * 0.6, y - uy * size * 0.6 - ux * size * 0.6],
        ]
        out.push({ id: `${ri}:${si}`, d: pathD(pts) + 'Z', color })
      }
    }
    return out
  }, [data, routeSel, kq])

  const stopMarks = useMemo(() => {
    const seen = new Map()
    for (const { ri, color } of routeSel.visible) {
      const st = data.routes[ri].stops
      st.forEach(([stopIdx], k) => {
        const end = k === 0 || k === st.length - 1
        const prev = seen.get(stopIdx)
        if (!prev || (end && !prev.end)) seen.set(stopIdx, { stopIdx, color, end })
      })
    }
    return [...seen.values()]
  }, [data, routeSel])

  if (!view) return <div className="map" ref={wrapRef} />

  const vb = [view.cx - size.w / 2 / view.k, view.cy - size.h / 2 / view.k, size.w / view.k, size.h / view.k]
  const anySel = groupLaneMap.size > 0 || routeSel.lanes.size > 0
  const showStops = view.k > 0.05 // 100px ≈ 2km보다 확대하면 중간 정류소까지
  const showLabels = view.k > 0.35
  const px = 1 / view.k

  return (
    <div className="map" ref={wrapRef}>
      <svg
        ref={svgRef}
        width={size.w}
        height={size.h}
        viewBox={vb.join(' ')}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => onHover(null)}
        onDoubleClick={() => setView(fitView(data.bbox, size.w, size.h))}
      >
        <BaseLayer segments={data.segments} maxTrips={data.maxTrips} dim={anySel} />
        <g fill="none" strokeLinecap="round" strokeLinejoin="round" opacity={routeSel.lanes.size ? 0.35 : 1}>
          {groupPaths.map((p) => (
            <path key={p.id} d={p.d} stroke={p.color} strokeWidth={p.width} vectorEffect="non-scaling-stroke" />
          ))}
        </g>
        <g fill="none" strokeLinecap="round" strokeLinejoin="round">
          {routePaths.map((p) => (
            <path key={p.id} d={p.d} stroke={p.color} strokeWidth={p.width} vectorEffect="non-scaling-stroke" />
          ))}
        </g>
        <g>
          {arrows.map((a) => (
            <path key={a.id} d={a.d} fill="#fff" stroke={a.color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
          ))}
        </g>
        {showNodes && (
          <g>
            {nodeMarks.merge.map((ni) => {
              const n = data.nodes[ni]
              return <rect key={`m${ni}`} x={n.x - 3 * px} y={n.y - 3 * px} width={6 * px} height={6 * px}
                fill="var(--merge)" stroke="#fff" strokeWidth={1} vectorEffect="non-scaling-stroke" />
            })}
            {nodeMarks.branch.map((ni) => {
              const n = data.nodes[ni]
              return <circle key={`b${ni}`} cx={n.x} cy={n.y} r={3.5 * px}
                fill="var(--branch)" stroke="#fff" strokeWidth={1} vectorEffect="non-scaling-stroke" />
            })}
          </g>
        )}
        <g>
          {stopMarks.filter((m) => m.end || showStops).map(({ stopIdx, color, end }) => {
            const s = data.stops[stopIdx]
            return <circle key={stopIdx} cx={s.x} cy={s.y} r={(end ? 5.5 : 3) * px}
              fill={end ? color : '#fff'} stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
          })}
        </g>
        {showLabels && (
          <g className="labels" fontSize={11 * px}>
            {stopMarks.map(({ stopIdx }) => {
              const s = data.stops[stopIdx]
              return <text key={stopIdx} x={s.x + 7 * px} y={s.y + 4 * px}>{s.name}</text>
            })}
          </g>
        )}
        <HitLayer segments={data.segments} />
      </svg>
      <div className="zoom">
        <button onClick={() => zoomAt(1.6, size.w / 2, size.h / 2)} aria-label="확대">+</button>
        <button onClick={() => zoomAt(1 / 1.6, size.w / 2, size.h / 2)} aria-label="축소">−</button>
        <button onClick={() => setView(fitView(data.bbox, size.w, size.h))} aria-label="전체 보기">⤢</button>
      </div>
      <div className="scale">{scaleLabel(view.k)}</div>
      <div className="notice" role="note">
        <div className="notice-copy">© {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER}. All rights reserved.</div>
        <div>개발 중인 버전입니다. 무단 복제·재배포를 금합니다.</div>
        <div className="notice-src">버스 정보 출처: 아산시 버스정보시스템 · 데이터 {data.manifest.source_crawled_at?.slice(0, 10)}</div>
      </div>
    </div>
  )
}

function scaleLabel(k) {
  const m = 100 / k
  return m >= 1000 ? `100px ≈ ${(m / 1000).toFixed(1)}km` : `100px ≈ ${Math.round(m)}m`
}
