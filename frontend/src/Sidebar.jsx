import { useMemo, useState } from 'react'
import { ROUTE_COLORS, groupRouteNames, routeColor } from './selection.js'

const MAX_RESULTS = 40

function km(m) {
  return `${(m / 1000).toFixed(1)}km`
}

// 목록용 짧은 정류소명: 괄호 설명 제거 (시외버스터미널(환승정류장) → 시외버스터미널)
function shortStop(name) {
  return (name || '').replace(/\s*\(.*?\)\s*/g, '') || name
}

function hhmm(t) {
  return t && t.length === 4 ? `${t.slice(0, 2)}:${t.slice(2)}` : '—'
}

export default function Sidebar({
  data, sel, setSel, names, hidden, setHidden, graph, groupRouteIdx, routeSel, setPreview,
}) {
  const [query, setQuery] = useState('')

  const groupList = useMemo(
    () => groupRouteNames(data, sel.groups, sel.excludePublic),
    [data, sel.groups, sel.excludePublic],
  )
  const toggleName = (n) => {
    setSel((s) => ({ ...s, names: s.names.includes(n) ? s.names.filter((x) => x !== n) : [...s.names, n] }))
  }
  const clearGroupNames = () => {
    const inGroups = new Set(groupList.flatMap((g) => g.names.map((x) => x.name)))
    setSel((s) => ({ ...s, names: s.names.filter((n) => !inGroups.has(n)) }))
  }

  const results = useMemo(() => {
    const q = query.trim()
    if (!q) return []
    return data.names.filter((n) => n.includes(q) && !names.includes(n)).slice(0, MAX_RESULTS)
  }, [data, query, names])

  const toggleGroup = (code) =>
    setSel((s) => ({
      ...s,
      groups: s.groups.includes(code) ? s.groups.filter((g) => g !== code) : [...s.groups, code],
    }))
  const addName = (n) => {
    setSel((s) => ({ ...s, names: [...s.names, n] }))
    setQuery('')
  }
  const removeName = (n) => setSel((s) => ({ ...s, names: s.names.filter((x) => x !== n) }))
  const toggleDir = (ri) =>
    setHidden((h) => {
      const next = new Set(h)
      next.has(ri) ? next.delete(ri) : next.add(ri)
      return next
    })

  const groupNameCount = new Set(groupRouteIdx.map((ri) => data.routes[ri].name)).size
  const m = data.manifest

  return (
    <aside className="sidebar">
      <header>
        <h1>아산 버스 노선망</h1>
        <p className="muted">
          버전 {m.version_id} · 수집 {m.source_crawled_at?.slice(0, 10)} · 구간 {data.segments.length} · 노선 {data.routes.length}
        </p>
      </header>

      <section>
        <h2>권역</h2>
        <div className="chips">
          {data.groups.map((g) => {
            const on = sel.groups.includes(g.code)
            return (
              <button
                key={g.code}
                className={`chip ${on ? 'on' : ''}`}
                style={on ? { background: g.color, borderColor: g.color } : { borderColor: g.color }}
                onClick={() => toggleGroup(g.code)}
                title={g.label}
              >
                {g.code === 'X' ? '기타' : `${g.code}00`}
              </button>
            )
          })}
        </div>
        <label className="check">
          <input type="checkbox" checked={sel.excludePublic}
            onChange={(e) => setSel((s) => ({ ...s, excludePublic: e.target.checked }))} />
          공영버스 제외
        </label>
        <label className="check">
          <input type="checkbox" checked={sel.showNodes}
            onChange={(e) => setSel((s) => ({ ...s, showNodes: e.target.checked }))} />
          분기·합류 점 표시
        </label>
      </section>

      {groupList.length > 0 && (
        <section>
          <div className="list-head">
            <h2>권역 노선 {groupNameCount}개 <span className="muted small">(방향 {groupRouteIdx.length})</span></h2>
            {groupList.some((g) => g.names.some((x) => names.includes(x.name))) && (
              <button className="link" onClick={clearGroupNames}>선택 해제</button>
            )}
          </div>
          <div className="group-list" onMouseLeave={() => setPreview(null)}>
            {groupList.map(({ group, names: rows }) => (
              <div key={group.code}>
                {groupList.length > 1 && (
                  <h3 className="group-title" style={{ color: group.color }}>
                    {group.label} <span className="muted small">{rows.length}개</span>
                  </h3>
                )}
                {rows.map((row) => {
                  const k = names.indexOf(row.name)
                  const on = k >= 0
                  return (
                    <button
                      key={row.name}
                      className={`gcard ${on ? 'on' : ''}`}
                      style={on ? { borderColor: routeColor(k), boxShadow: `inset 4px 0 0 ${routeColor(k)}` } : undefined}
                      onClick={() => toggleName(row.name)}
                      onMouseEnter={() => setPreview(row.name)}
                      onFocus={() => setPreview(row.name)}
                      onBlur={() => setPreview(null)}
                      aria-pressed={on}
                    >
                      <span className="dot" style={{ background: group.color }} />
                      <b className="gname">{row.name}</b>
                      <span className="gstops" title={`${row.st} ↔ ${row.ed}`}>
                        {shortStop(row.st)} ↔ {shortStop(row.ed)}
                      </span>
                      <span className="gmeta">
                        {row.isPublic && <span className="tag">공영</span>}
                        {row.trips}회
                      </span>
                    </button>
                  )
                })}
              </div>
            ))}
          </div>
        </section>
      )}

      <section>
        <h2>노선</h2>
        <input
          className="search"
          type="search"
          placeholder="노선번호 (예: 45, 900)"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && results[0] && addName(results[0])}
        />
        {results.length > 0 && (
          <div className="results">
            {results.map((n) => {
              const r = data.routes[data.byName.get(n)[0]]
              return (
                <button key={n} className="result" onClick={() => addName(n)}>
                  <span className="dot" style={{ background: data.groupByCode[r.group]?.color }} />
                  <b>{n}</b> <span className="muted">{r.st} ↔ {r.ed}</span>
                </button>
              )
            })}
          </div>
        )}

        {names.map((n, k) => (
          <div key={n} className="route-card" style={{ borderLeftColor: ROUTE_COLORS[k % ROUTE_COLORS.length] }}>
            <div className="route-head">
              <b>{n}</b>
              <button className="x" onClick={() => removeName(n)} aria-label={`${n} 선택 해제`}>×</button>
            </div>
            {data.byName.get(n).map((ri) => {
              const r = data.routes[ri]
              return (
                <label key={ri} className="dir">
                  <input type="checkbox" checked={!hidden.has(ri)} onChange={() => toggleDir(ri)} />
                  <span>
                    {r.st} → {r.ed}
                    <span className="muted small">
                      {' '}· 평일 {r.trips}회 · {hhmm(r.first)}~{hhmm(r.last)} · 정류소 {r.stops.length} · {km(r.length)}
                    </span>
                  </span>
                </label>
              )
            })}
          </div>
        ))}
      </section>

      {graph && graph.segments > 0 && (
        <section className="summary">
          <h2>{routeSel.lanes.size ? '선택 노선' : '선택 권역'} 그래프</h2>
          <dl>
            <dt>구간</dt><dd>{graph.segments}</dd>
            <dt>총연장(방향별)</dt><dd>{km(graph.length)}</dd>
            <dt><span className="mark branch" />분기</dt><dd>{graph.branch.length}</dd>
            <dt><span className="mark merge" />합류</dt><dd>{graph.merge.length}</dd>
          </dl>
        </section>
      )}

      <footer className="muted small">
        휠·버튼으로 확대, 끌어서 이동, 더블클릭으로 전체 보기. 상·하행은 몇 m 떨어진 별도 구간이라 겹쳐 보인다.
      </footer>
    </aside>
  )
}
