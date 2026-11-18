"""여러 크롤링 JSON에서 지정한 노선만 골라 하나로 합친다.
사용: merge.py out.json series_label file1:names|all[:dirs] file2:...   names는 쉼표 구분"""
import json,sys
out,label=sys.argv[1],sys.argv[2]
routes=[]
for spec in sys.argv[3:]:
    parts=spec.split(':');path=parts[0];names=parts[1] if len(parts)>1 else 'all';dirs=parts[2] if len(parts)>2 else '12'
    d=json.load(open(path,encoding='utf-8'))
    want=None if names=='all' else set(names.split(','))
    for r in d['routes']:
        i=r['info']
        if (want is None or i['route_name'] in want) and i['route_direction'] in dirs and r.get('shape'):
            routes.append(r)
json.dump({'meta':{'series':label,'weekcd':'1'},'routes':routes},open(out,'w',encoding='utf-8'),ensure_ascii=False)
print(out,len(routes),'routes')
