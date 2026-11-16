import {api} from '../api/client.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {chart} from '../components/common/chart.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

export const backtestApi = {
  batches: () => api('/backtest/batches'),
  batch: id => api('/backtest/batches/' + id),
  receive: data => api('/backtest/batches', {method: 'POST', body: JSON.stringify(data)}),
  revisions: reader => api('/backtest/revisions/' + encodeURIComponent(reader)),
  cases: () => api('/backtest/cases'),
  case: id => api('/backtest/cases/' + id),
  point: (id, pointId) => api('/backtest/cases/' + id + '/points/' + pointId),
  create: data => api('/backtest/cases', {method: 'POST', body: JSON.stringify(data)}),
  run: id => api('/backtest/cases/' + id + '/run', {method: 'POST'}),
  resume: id => api('/backtest/cases/' + id + '/resume', {method: 'POST'}),
  approve: id => api('/backtest/cases/' + id + '/approve', {method: 'POST'}),
  briefings: () => api('/backtest/briefings'),
  briefing: data => api('/backtest/briefings', {method: 'POST', body: JSON.stringify(data)})
};

const STATUS={queued:'待执行',running:'计算中',partial:'部分完成，可恢复',completed:'全部完成',failed:'失败'};
const POINT={queued:'待执行',running:'计算中',completed:'已完成',failed:'失败可恢复'};
const editable=()=>session.user.role!=='viewer';

function downloadBlob(path,filename){
  return fetch('/api'+path,{headers:{Authorization:'Bearer '+session.token}}).then(async r=>{
    if(!r.ok){const d=await r.json().catch(()=>({detail:'下载失败'}));throw new Error(typeof d.detail==='string'?d.detail:'下载失败');}
    const u=URL.createObjectURL(await r.blob()),a=document.createElement('a');a.href=u;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
  });
}

export async function backtestPage(id){
  const [batches,cases,briefings]=await Promise.all([backtestApi.batches(),backtestApi.cases(),backtestApi.briefings()]);
  workspace.backtestBatches=batches;workspace.backtestCases=cases;
  const detail=id?await backtestApi.case(id):null;
  return heading('06 / HISTORICAL BACKTEST','历史回测案','按读者保存行为与标签的历次修订，区分业务发生时间与实际接收时间。接收时间由系统记录、不可回填；每个时点只取此前已接收且已到期的最新样本。')
    +`<div class="backtest-grid">
      <section class="panel"><div class="section-title"><h2>历史资料接收</h2><span>${batches.length} 批 · 接收时间不可回填</span></div>
        <div class="batch-list">${batches.map(b=>`<button class="batch-item" data-batch="${b.id}"><strong>${esc(b.name)}</strong><span>${b.record_count} 条 · ${esc(b.source)}</span><small>接收 ${esc(date(b.received_at))} · SHA256 ${esc(b.checksum.slice(0,18))}…</small></button>`).join('')||'<p class="empty">尚无历史资料批次。</p>'}</div>
        <form id="history-form"><label>批次名称<input name="name" required minlength="2" placeholder="例如：2025-09 续读回执批 · 10月接收"></label><label>来源文件<input name="source" value="reader_renewal.csv" maxlength="200"></label><label>接收说明<input name="note" maxlength="500" placeholder="这批资料为何迟到或为何更正"></label><label>记录 JSON<textarea name="records" rows="7" required placeholder='[{"reader_id":"HR-0001","revision":1,"sessions_14d":6,"completion_pct":62.5,"days_inactive":3,"issues_read":8,"renewed":0,"occurred_at":"2025-08-30","matured_at":"2025-09-13"}]'></textarea></label>
        <p class="hint">字段：reader_id、revision、四项行为、renewed、occurred_at（业务发生）、matured_at（标签到期）。不得填写 received_at：接收时间由系统在整批校验通过后写入、不可回填。迟到记录与更正请作为新批次接收，旧修订不被覆盖；整批校验失败时一条也不落库。</p>
        <button type="submit" class="primary" ${editable()?'':'disabled'}>校验并整批接收</button></form>
        <div id="history-preview"></div></section>
      <section class="panel"><div class="section-title"><h2>建立回测案</h2><span>冻结接收范围与多个时点</span></div>
        <form id="case-form"><label>回测案名称<input name="name" required minlength="2" value="2025 续读结果回测"></label>
        <div class="parameter-grid"><label>接收截止时间<input name="receive_cutoff" required value="${new Date().toISOString().slice(0,10)}T23:59:59Z" placeholder="2025-12-31T23:59:59Z"></label><label>高风险阈值<input name="threshold" type="number" step="0.01" min="0.1" max="0.9" value="0.5"></label></div>
        <label>回测时点（每行一个 ISO 日期，逗号或换行分隔）<textarea name="points" rows="4" required>2025-03-31
2025-06-30
2025-09-30
2025-12-31</textarea></label>
        <label>限定资料批次<select name="batch_ids" multiple size="5">${batches.map(b=>`<option value="${b.id}">${esc(b.name)} · 接收 ${esc(date(b.received_at))}</option>`).join('')}</select></label>
        <p class="hint">不选择批次时，冻结“接收截止前已完成接收”的全部批次。建案时即固化每点纳入/排除决定；之后到达的迟到记录、更正或新批次不会进入已建案件。时点不得晚于接收截止。</p>
        <button type="submit" class="primary" ${editable()?'':'disabled'}>冻结范围并建案</button></form></section></div>
      <section class="panel"><div class="section-title"><h2>回测案进度</h2><span>${cases.length} 案 · 全部时点成功才开放下载、批准与简报</span></div>
        <div class="schedule-list">${cases.map(c=>`<article><div><strong>${esc(c.name)}</strong><p>接收截止 ${esc(date(c.receive_cutoff))} · ${c.completeness.points_completed}/${c.completeness.points_total} 时点完成${c.completeness.points_failed?' · '+c.completeness.points_failed+' 个失败':''}${c.approved?' · 已批准':''}</p><small>范围 SHA256 ${esc(c.scope_checksum.slice(0,18))}… · 冻结 ${c.scope_batch_ids.length} 批</small></div><div><span class="status ${esc(c.status)}">${esc(STATUS[c.status]||c.status)}</span><a class="button small" href="#/backtest/${c.id}">核对时点 ↗</a>${c.status!=='completed'&&!c.approved&&editable()?`<button class="button small" data-resume="${c.id}">恢复进度</button>`:''}</div></article>`).join('')||'<p class="empty">尚无回测案。</p>'}</div></section>
      ${detail?caseDetail(detail,briefings):''}
      <section class="panel"><div class="section-title"><h2>逐点核对与简报归档</h2><span>仅由齐全且获批的回测案生成简报</span></div>${briefings.map(b=>`<div class="delivery"><div><strong>${esc(b.name)}</strong><p>案 #${b.case_id} · ${esc(b.destination)} · ${esc(date(b.created_at))}</p><small>SHA256 ${esc(b.checksum.slice(0,24))}…</small></div><button class="text-button" data-briefing="${b.id}">JSON ↓</button></div>`).join('')||'<p class="empty">全部时点成功并经负责人批准后，才能生成内部简报归档。</p>'}</section>`;
}

function caseDetail(c,briefings){
  const ready=c.completeness.all_completed;
  return `<section class="panel" id="case-detail"><div class="section-title"><h2>${esc(c.name)}</h2><span>${esc(STATUS[c.status]||c.status)} · 接收截止 ${esc(date(c.receive_cutoff))}</span></div>
    <div class="metrics">${[['时点完成',c.completeness.points_completed+'/'+c.completeness.points_total],['失败',c.completeness.points_failed],['未完成',c.completeness.points_pending],['冻结批次',c.scope_batch_ids.length]].map(([k,v])=>`<div><span>${k}</span><strong>${v}</strong></div>`).join('')}</div>
    <div class="result-selector"><span>冻结范围 SHA256 ${esc(c.scope_checksum)}</span><span>${c.approved?'已批准 '+esc(c.approved_by):'待齐全后批准'}</span>${ready&&editable()?`<button class="button small" id="run-case">重新执行未完成时点</button>`:''}${!ready&&editable()?`<button class="button small" id="resume-case">从原进度恢复</button>`:''}${ready?`<button class="text-button" id="download-case">整案 JSON ↓</button><button class="text-button" id="download-derivation">纳入清单 CSV ↓</button>`:''}</div>
    ${!ready?'<p class="warning">时点未齐全：不开放整案下载、批准与简报使用。单点失败可恢复，重试复用同一试验，不重复建试验。</p>':''}
    ${c.points.some(p=>p.status==='completed')?`<section class="panel"><div class="section-title"><h2>跨时点趋势</h2><span>各时点独立重算，非时间外验证</span></div>${chart({type:'bar',labels:c.points.map(p=>date(p.cutoff_at).slice(0,10)),series:[{name:'纳入读者（人）',values:c.points.map(p=>p.included_count)},{name:'样本内AUC×100',values:c.points.map(p=>p.trial?.result?.point?.auc!=null?Math.round(p.trial.result.point.auc*100):0)}]})}<p class="hint">柱 1 为各时点纳入人数；柱 2 为该时点样本内 AUC×100，仅供口径核对，不是时间外验证。</p></section>`:''}
    <div class="table-wrap"><table><thead><tr><th>时点</th><th>状态</th><th>纳入</th><th>排除</th><th>排除原因</th><th>尝试</th><th>试验</th><th>操作</th></tr></thead><tbody>${c.points.map(p=>`<tr><td>${esc(date(p.cutoff_at))}</td><td><span class="status ${esc(p.status)}">${esc(POINT[p.status]||p.status)}</span></td><td>${p.included_count}</td><td>${p.excluded_count}</td><td>${Object.entries(p.reason_counts||{}).map(([k,v])=>esc((p.reason_labels||{})[k]||k)+' '+v).join('，')||'—'}</td><td>${p.attempts}</td><td>${p.trial?esc(p.trial.status)+' · 第'+p.trial.attempts+'次':'未创建'}${p.error?'<br><small>'+esc(p.error)+'</small>':''}</td><td><button class="text-button" data-point="${p.id}">核对明细</button></td></tr>`).join('')}</tbody></table></div>
    <div id="point-panel"></div>
    <div class="two-columns"><section class="panel"><div class="section-title"><h2>冻结接收范围</h2><span>建案后不再变化</span></div><ul class="timeline">${c.scope_snapshot.map(b=>`<li><strong>${esc(b.name)}</strong><span>${b.record_count} 条 · 接收 ${esc(b.received_at_iso)}</span></li>`).join('')}</ul></section>
      <section class="method-sheet compact"><p class="eyebrow">RECEIPT POLICY</p><h3>时点还原口径</h3><p>每个时点只取接收时间与标签到期时间均不晚于该时点、且为该读者最新修订的样本。时点之后到达的迟到记录与更正不回溯进入该时点；后续时点自动采用新修订。标签到期时间由业务发生时间决定，接收时间由系统记录、不可回填。</p>
      ${ready?`<form id="briefing-form"><label>简报名称<input name="name" value="历史回测内部复核简报" required minlength="2"></label><label>接收组<input name="destination" value="业务复核组 · 本地收件箱" required></label><button type="submit" class="primary" ${c.approved&&session.user.role==='admin'?'':'disabled'}>${c.approved?'生成内部简报归档':'须先由负责人批准'}</button></form>`:'<p class="hint">时点齐全后开放批准与简报。</p>'}
      ${ready&&!c.approved&&session.user.role==='admin'?'<button class="primary" id="approve-case">批准此回测结论</button>':''}
      </section></div></section>`;
}

async function pointPanel(caseId,pointId){
  const p=await backtestApi.point(caseId,pointId);
  const r=p.trial?.result;
  return `<div class="section-title"><h2>时点 ${esc(date(p.cutoff_at))} · 纳入与排除核对</h2><span>快照 SHA256 ${esc(p.snapshot_checksum.slice(0,20))}…</span></div>
    ${r?`<p class="eyebrow">COMPUTED FINDING</p><h3>${esc(r.summary)}</h3><div class="metrics">${(r.metrics||[]).map(m=>`<div><span>${esc(m.label)}</span><strong>${esc(m.value)}${esc(m.unit||'')}</strong></div>`).join('')}${r.point?`<div><span>样本内 AUC</span><strong>${esc(r.point.auc)}</strong></div><div><span>覆盖（阈值决策）</span><strong>${esc(r.point.coverage_pct)}<small>%</small></strong></div>`:''}</div>${r.point?`<p class="hint">${esc(r.point.basis)}；纳入 ${r.point.included}、未续读 ${r.point.positives}、续读 ${r.point.negatives}；Brier ${esc(r.point.brier)}。</p>`:''}`:''}
    <div class="two-columns"><div><h3>采用的历史修订（${p.included_count}）</h3>${table([{key:'reader_id',label:'读者'},{key:'revision',label:'采用修订'},{key:'received_at_iso',label:'接收时间'},{key:'matured_at_iso',label:'标签到期'},{key:'renewed',label:'续读'}],(p.decisions||[]).map(d=>({...d,received_at_iso:date(d.received_at),matured_at_iso:date(d.matured_at)})),'此时点没有纳入的读者。')}</div>
    <div><h3>排除原因（${p.excluded_count}）</h3>${table([{key:'reader_id',label:'读者'},{key:'reason_label',label:'原因'},{key:'latest_received_at',label:'接收时间'},{key:'earliest_matured_at',label:'到期时间'}],p.exclusions||[],'此时点没有排除的读者。')}</div></div>
    ${p.decisions&&p.decisions.length?`<p class="hint">核对提示：核对读者在该时点采用了第几次修订；点击左侧批次中的读者行或调用修订接口可查看历次修订的接收时间。当前时点前六位：${p.decisions.slice(0,6).map(d=>esc(d.reader_id)+'→v'+d.revision).join('，')}…</p>`:''}`;
}

export async function bindBacktest(refresh,id){
  const form=document.querySelector('#history-form');
  if(form)submit(form,async f=>{
    const records=JSON.parse(f.get('records'));
    const b=await backtestApi.receive({name:f.get('name'),source:f.get('source')||'history.json',note:f.get('note')||'',records});
    notify('资料已整批接收，接收时间 '+date(b.received_at));await refresh();
  });
  document.querySelectorAll('[data-batch]').forEach(button=>button.onclick=async()=>{
    try{
      const b=await backtestApi.batch(button.dataset.batch),el=document.querySelector('#history-preview'),rows=b.samples.slice(0,30);
      el.innerHTML=`<div class="section-title"><h2>${esc(b.name)}</h2><span>接收 ${esc(date(b.received_at))} · 不可回填</span></div>`+table([{key:'reader_id',label:'读者'},{key:'revision',label:'修订'},{key:'sessions_14d',label:'14日访问'},{key:'completion_pct',label:'完成率'},{key:'days_inactive',label:'未读天数'},{key:'issues_read',label:'已读期数'},{key:'renewed',label:'续读'}],rows)+'<p class="hint">点击读者行查看历次修订的接收时间与标签到期时间。</p>';
      el.querySelectorAll('tbody tr').forEach((tr,i)=>{const row=rows[i];if(!row)return;tr.style.cursor='pointer';tr.onclick=async()=>{
        try{const h=await backtestApi.revisions(row.reader_id);notify(h.reader_id+'：'+h.revisions.map(x=>'v'+x.revision+' 接收 '+date(x.received_at)+'，到期 '+date(x.matured_at)).join('；'));}catch(e){notify(e.message,true);}
      };});
    }catch(e){notify(e.message,true);}
  });
  const caseForm=document.querySelector('#case-form');
  if(caseForm)submit(caseForm,async f=>{
    const points=(f.get('points')||'').split(/[\n,，]+/).map(v=>v.trim()).filter(Boolean);
    const batchIds=Array.from(new FormData(caseForm).getAll('batch_ids')).map(Number);
    const c=await backtestApi.create({name:f.get('name'),receive_cutoff:f.get('receive_cutoff'),points,params:{threshold:Number(f.get('threshold'))},...(batchIds.length?{batch_ids:batchIds}:{})});
    notify('回测案已建立，范围与 '+c.points_count+' 个时点已冻结');location.hash='#/backtest/'+c.id;await refresh();
  });
  document.querySelectorAll('[data-resume]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await backtestApi.resume(b.dataset.resume);notify('已按原进度恢复，后台继续未完成时点');await refresh();}catch(e){notify(e.message,true);b.disabled=false;}});
  document.querySelectorAll('[data-briefing]').forEach(b=>b.onclick=()=>downloadBlob('/backtest/briefings/'+b.dataset.briefing+'/download','backtest-briefing-'+b.dataset.briefing+'.json').catch(e=>notify(e.message,true)));
  if(!id)return;
  document.querySelector('#resume-case')?.addEventListener('click',async e=>{e.target.disabled=true;try{await backtestApi.resume(id);notify('已从原进度恢复，后台继续未完成时点');await refresh();}catch(err){notify(err.message,true);e.target.disabled=false;}});
  document.querySelector('#run-case')?.addEventListener('click',async e=>{e.target.disabled=true;try{await backtestApi.run(id);notify('已提交执行，后台逐点生成试验');await refresh();}catch(err){notify(err.message,true);e.target.disabled=false;}});
  document.querySelector('#approve-case')?.addEventListener('click',async()=>{try{await backtestApi.approve(id);notify('回测结论已批准');await refresh();}catch(e){notify(e.message,true);}});
  document.querySelector('#download-case')?.addEventListener('click',()=>downloadBlob('/backtest/cases/'+id+'/download','backtest-case-'+id+'.json').catch(e=>notify(e.message,true)));
  document.querySelector('#download-derivation')?.addEventListener('click',()=>downloadBlob('/backtest/cases/'+id+'/derivation.csv','backtest-case-'+id+'-derivation.csv').catch(e=>notify(e.message,true)));
  document.querySelectorAll('[data-point]').forEach(b=>b.onclick=async()=>{try{document.querySelector('#point-panel').innerHTML=await pointPanel(id,b.dataset.point);}catch(e){notify(e.message,true);}});
  const briefingForm=document.querySelector('#briefing-form');
  if(briefingForm)submit(briefingForm,async f=>{const b=await backtestApi.briefing({case_id:id,name:f.get('name'),destination:f.get('destination')});notify('简报已归档 '+b.filename);await refresh();});
}
