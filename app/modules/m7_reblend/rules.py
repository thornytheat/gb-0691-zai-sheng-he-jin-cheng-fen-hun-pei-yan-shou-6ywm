from app.domain import blend
from app.numeric import number
from app.recipe import DEFAULT_RECIPE, definition, targets

def apply(ctx, version=None):
    version=version or DEFAULT_RECIPE
    rows=[]
    for batch in sorted({r['batch'] for r in ctx['eligible']}):
        group=[r for r in ctx['eligible'] if r['batch']==batch]
        lots=sorted({r['lot'] for r in group})
        for lot in lots:
            trial=blend([r for r in group if r['lot']!=lot])
            passing=sum(targets(version,r['元素'])[0]<=number(r['含量下界_pct'])<=number(r['含量上界_pct'])<=targets(version,r['元素'])[1] for r in trial)
            required=len(definition(version)['targets'])
            rows.append({'炉次':batch,'移除批号':lot,'剩余批次':len(lots)-1,'通过元素':passing,'复配结果':'可验收' if passing==required else '需复测'})
    return {'rows':rows,'metrics':[['复配候选',len(rows),'项'],['可验收',sum(r['复配结果']=='可验收' for r in rows),'项'],['配方版本',version,'']]}
