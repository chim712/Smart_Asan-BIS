"""시 전체 노선망(무방향) 빌더 — 시각화용.
사용: python -m analysis.build_city_net <fixed.json> <city_net.json>
상·하행 선형을 반경 R 안의 기존 꼭짓점에 붙여(snap) 한 줄의 도로로 합친다.
출력: nodes, edges(무방향, 지나는 계열 집합), segments(분기점 사이 체인)"""
import json,math,re,sys,heapq
from collections import defaultdict
from core.geo import LAT0, LNG0, KX, KY, xy, ll
from core.groups import default_group as group_of
ORIGIN=(LAT0,LNG0)
def to_xy(p): return xy(p,ORIGIN)
def to_ll(x,y): return ll(x,y,ORIGIN)
STEP=10; R=25; CELL=25
def densify(S):
    P=[to_xy(p) for p in S]; out=[P[0]]
    for a,b in zip(P,P[1:]):
        L=math.dist(a,b)
        if L<0.5: continue
        k=max(1,int(L//STEP))
        for i in range(1,k+1): out.append((a[0]+(b[0]-a[0])*i/k,a[1]+(b[1]-a[1])*i/k))
    return out
class Net:
    def __init__(s):
        s.V=[];s.owner=[];s.grid=defaultdict(list);s.adj=defaultdict(set);s.E=defaultdict(lambda:{'groups':set(),'routes':set(),'trips':0})
    def add(s,p,rid):
        i=len(s.V);s.V.append(p);s.owner.append(rid)
        s.grid[(int(p[0]//CELL),int(p[1]//CELL))].append(i);return i
    def near(s,p,rid,frozen):
        cx,cy=int(p[0]//CELL),int(p[1]//CELL);best=None
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                for i in s.grid.get((cx+dx,cy+dy),()):
                    if i>=frozen: continue  # 현재 노선이 방금 만든 점에는 붙지 않음
                    d=math.dist(p,s.V[i])
                    if d<R and (best is None or d<best[0]): best=(d,i)
        return best[1] if best else None
    def path(s,u,w,limit):
        # 기존 그래프에서 u→w 최단경로(짧은 우회만 허용)
        dist={u:0};pq=[(0,u)];prev={}
        while pq:
            d,x=heapq.heappop(pq)
            if x==w: break
            if d>dist.get(x,1e18) or d>limit: continue
            for y in s.adj[x]:
                nd=d+math.dist(s.V[x],s.V[y])
                if nd<dist.get(y,1e18): dist[y]=nd;prev[y]=x;heapq.heappush(pq,(nd,y))
        if w not in dist or dist[w]>limit: return None
        p=[w]
        while p[-1]!=u: p.append(prev[p[-1]])
        return p[::-1]
    def link(s,u,w,meta):
        if u==w: return
        k=(min(u,w),max(u,w)); s.adj[u].add(w);s.adj[w].add(u)
        e=s.E[k];e['groups'].add(meta[0]);e['routes'].add(meta[1]);e['trips']+=meta[2]
def build(src):
    with open(src, encoding='utf-8') as fp:
        d=json.load(fp)
    net=Net()
    routes=sorted(d['routes'],key=lambda r:-len(r.get('timetable') or []))
    for r in routes:
        if not r.get('shape'): continue
        rid=r['info']['route_id'];g=group_of(r['info']['route_name'])
        meta=(g,r['info']['route_name'].strip(),len(r.get('timetable') or []))
        frozen=len(net.V); seq=[]
        for p in densify(r['shape']):
            v=net.near(p,rid,frozen)
            if v is None: v=net.add(p,rid)
            if not seq or seq[-1]!=v: seq.append(v)
        for u,w in zip(seq,seq[1:]):
            if w in net.adj[u]: net.link(u,w,meta);continue
            if u<frozen and w<frozen:
                straight=math.dist(net.V[u],net.V[w])
                p=net.path(u,w,straight*1.6+30)
                if p:
                    for a,b in zip(p,p[1:]): net.link(a,b,meta)
                    continue
            net.link(u,w,meta)
    return net,d
def chains(net):
    """차수≠2 또는 계열집합이 바뀌는 점을 끊어 세그먼트로 압축"""
    key=lambda u,w:(min(u,w),max(u,w))
    gs=lambda u,w:frozenset(net.E[key(u,w)]['groups'])
    def brk(v):
        nb=list(net.adj[v])
        return len(nb)!=2 or gs(v,nb[0])!=gs(v,nb[1])
    seen=set();segs=[]
    starts=[v for v in net.adj if brk(v)]
    def walk(a,b):
        line=[a,b]
        while not brk(line[-1]):
            nxt=[y for y in net.adj[line[-1]] if y!=line[-2]][0]
            if key(line[-1],nxt) in seen: break
            seen.add(key(line[-1],nxt)); line.append(nxt)
        return line
    for a in starts:
        for b in net.adj[a]:
            if key(a,b) in seen: continue
            seen.add(key(a,b)); segs.append(walk(a,b))
    for (u,w) in list(net.E):  # 분기점 없는 고리
        if (u,w) not in seen: seen.add((u,w)); segs.append(walk(u,w))
    out=[]
    for line in segs:
        e=net.E[key(line[0],line[1])]
        routes=set();trips=0
        for a,b in zip(line,line[1:]):
            routes|=net.E[key(a,b)]['routes']
        out.append({'nodes':line,'groups':sorted(e['groups']),'routes':sorted(routes),
                    'length_m':round(sum(math.dist(net.V[a],net.V[b]) for a,b in zip(line,line[1:])))})
    return out
if __name__=='__main__':
    net,d=build(sys.argv[1]); segs=chains(net)
    print('vertices',len(net.V),'edges',len(net.E),'segments',len(segs))
    with open(sys.argv[2],'w',encoding='utf-8') as fp:
        json.dump({'V':net.V,'segments':segs},fp,ensure_ascii=False)
