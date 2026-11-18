"""선형(shape) 꼭짓점 순서 꼬임 보정.
링크 일부가 역순으로 저장되어 같은 직선 위에서 앞뒤로 왔다 갔다 하는 구간(지그재그)을 찾아
양 끝 기준점 사이 직선에 투영한 순서로 재정렬한다. 직선에서 벗어난 점이 있으면 손대지 않는다(실제 회차로 간주)."""
import json,math,sys
LAT0=36.78
def xy(p): return (p[1]*111320*math.cos(math.radians(LAT0)), p[0]*110574)
def dedup(S):
    out=[S[0]]
    for p in S[1:]:
        if p!=out[-1]: out.append(p)
    return out
def uturns(S,minlen=15,cos_th=-0.97):
    P=[xy(p) for p in S]; h=[]
    for k in range(1,len(P)-1):
        ax,ay=P[k][0]-P[k-1][0],P[k][1]-P[k-1][1]
        bx,by=P[k+1][0]-P[k][0],P[k+1][1]-P[k][1]
        la,lb=math.hypot(ax,ay),math.hypot(bx,by)
        if la<minlen or lb<minlen: continue
        if (ax*bx+ay*by)/la/lb<cos_th: h.append(k)
    return h
def length(S):
    P=[xy(p) for p in S]; return sum(math.dist(P[i],P[i+1]) for i in range(len(P)-1))
def fix(S,tol=30,gap=6):
    S=dedup(S); log=[]
    while True:
        h=uturns(S)
        # U턴이 gap 이내로 2개 이상 몰린 묶음
        groups=[];cur=[]
        for k in h:
            if cur and k-cur[-1]>gap: groups.append(cur);cur=[]
            cur.append(k)
        if cur: groups.append(cur)
        groups=[g for g in groups if len(g)>=2]
        done=False
        for g in groups:
            # 기준점: 묶음 앞뒤로 직선 위에 있는 점까지 확장
            a,b=g[0]-1,g[-1]+1
            A,B=xy(S[a]),xy(S[b])
            ux,uy=B[0]-A[0],B[1]-A[1]; L=math.hypot(ux,uy)
            if L<1: continue
            ux,uy=ux/L,uy/L
            seg=S[a:b+1]
            ok=all(abs((xy(p)[0]-A[0])*uy-(xy(p)[1]-A[1])*ux)<tol for p in seg)
            if not ok: continue
            proj=lambda p:(xy(p)[0]-A[0])*ux+(xy(p)[1]-A[1])*uy
            mid=sorted(S[a+1:b],key=proj)
            mid=[p for p in mid if 0<proj(p)<L]
            new=S[:a+1]+mid+S[b:]
            if length(new)<length(S)-1:
                log.append({'from_idx':a,'to_idx':b,'removed_m':round(length(S)-length(new)),'at':S[a]})
                S=dedup(new); done=True; break
        if not done: break
    return S,log
if __name__=='__main__':
    src,dst=sys.argv[1],sys.argv[2]
    d=json.load(open(src)); fixes=[]
    for r in d['routes']:
        new,log=fix(r['shape'])
        if log:
            r['shape']=new
            fixes.append({'route_id':r['info']['route_id'],'route_name':r['info']['route_name'].strip(),'fixes':log})
    d['meta']['shape_fixes']=fixes
    json.dump(d,open(dst,'w'),ensure_ascii=False)
    for f in fixes: print(f['route_name'],f['route_id'],[(x['from_idx'],x['removed_m'],x['at']) for x in f['fixes']])
