const alloyState={busy:false};const meta=JSON.parse(document.querySelector('#run-meta').textContent);const feedback=document.querySelector('#feedback');
async function submitAlloy(url,payload){if(alloyState.busy)return;alloyState.busy=true;feedback.textContent='正在复算…';document.querySelectorAll('button').forEach(b=>b.disabled=true);try{const options={method:'POST'};if(payload instanceof FormData){options.body=payload}else{options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(payload)}const response=await fetch(url,options);const data=await response.json();if(!response.ok)throw Error(data.message||'请求失败，请重试');if(data.manifest){location.href=location.pathname+'?run_id='+encodeURIComponent(data.manifest.run_id)}else{location.reload()}}catch(error){feedback.textContent=error.message}finally{alloyState.busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}}
document.querySelector('#sample').addEventListener('click',()=>submitAlloy('/api/run',{}));document.querySelector('#submit').addEventListener('click',()=>submitAlloy('/api/run',{csv:document.querySelector('#csv').value}));document.querySelector('#upload').addEventListener('click',()=>{const file=document.querySelector('#file').files[0];if(!file){feedback.textContent='先选择CSV文件。';return}const form=new FormData();form.append('file',file);submitAlloy('/api/run',form)});document.querySelectorAll('[data-decision]').forEach(button=>button.addEventListener('click',()=>submitAlloy('/api/review',{...meta,decision:button.dataset.decision})));document.querySelector('#print').addEventListener('click',()=>window.print());

// 配方版本切换（仅验收对照页存在相关控件）
const recipeApply=document.querySelector('#recipe-apply');
if(recipeApply){
  const recipeRun=document.querySelector('#recipe-run');
  const policy=document.querySelector('#recipe-policy');
  const batchInput=document.querySelector('#recipe-batches');
  const expected=document.querySelector('#recipe-expected');
  const recipeFeedback=document.querySelector('#recipe-feedback');
  const runId=recipeRun?recipeRun.value:(meta.id||'');
  const batches=()=>batchInput.value.split(/[,，\s]+/).filter(Boolean);
  async function recipeRequest(url,body){
    recipeFeedback.textContent='正在保存…';
    const response=await fetch(url,{method:'POST',headers={'Content-Type':'application/json'},body:JSON.stringify({...body,id:runId})});
    const data=await response.json();
    if(!response.ok)throw Error(data.message||'切换失败');
    const note=data.idempotent?'（重复提交，未重复改判）':'';
    recipeFeedback.className='alert';
    recipeFeedback.textContent=(data.changed&&data.changed.length?('改判炉次：'+data.changed.join('、')):'结论未变')+note;
    setTimeout(()=>location.reload(),600);
  }
  const guard=fn=>async()=>{try{recipeApply.disabled=true;await fn()}catch(error){recipeFeedback.textContent=error.message}finally{recipeApply.disabled=false}};
  recipeApply.addEventListener('click',guard(()=>recipeRequest('/api/recipe/switch',{policy:policy.value,batches:batches(),expected_revision:Number(expected.value||0)})));
  document.querySelector('#recipe-restore').addEventListener('click',guard(()=>recipeRequest('/api/recipe/restore',{target_revision:0,expected_revision:Number(expected.value||0)})));
}
