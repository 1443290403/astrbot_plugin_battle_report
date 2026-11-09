const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;
let jobId = '', category = 'matched', offset = 0, timer, sequence = 0, dbReady = false, running = false;
let lastResult, comparisonLoading = false;
let pageRole='',credentialTransport=false,managementSequence=0,managementEpoch=0,managementOffset=0,managementTotal=0;
let adminTarget=null,memberTarget=null,playerTarget=null,playerOffset=0,playerSequence=0,confirmPlayer=null;
const adminSelected=new Map();
let adminMemberSequence=0,adminMemberOffset=0,adminMemberTotal=0,adminMemberTimer,adminExpectedKnown=false,adminTeam='',adminReady=false,adminSaving=false;
let view = 'local', libraryOffset = 0, libraryData, librarySequence = 0;
let auditOffset=0,auditData,auditSequence=0,auditLoading=false;
let auditDetailTarget=null,auditDetailOffset=0,auditDetailSequence=0,auditDetailLoading=false;
const selection = new Map();
let dispatchId = '', dispatchTimer, dispatchSequence = 0, dispatchRunning = false;
const dispatchLabels={prepared:'待确认',running:'发送中',completed:'全部发送成功',partial:'部分未完成',interrupted:'已中断'};
const labels = {pending:'准备中',running:'核对中',completed:'已完成',failed:'未完成',cancelled:'已取消',interrupted:'已中断'};
const rowNotes = {matched:'规范内容相同',missing_in_db:'本地未匹配',only_in_db:'本次网站范围未检出',
  db_duplicates:'本地多条相同内容',site_duplicates:'网站多条相同内容',parse_failed:'原文无法解析',
  out_of_range:'正文日期不在所选月份',other_team:'正文战队与搜索队名不符'};
const formatTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value)) : '';
const node = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = String(text); if (cls) e.className = cls; return e; };
function legacyCopy(text) {
  const active = document.activeElement, selection = window.getSelection();
  const ranges = selection ? Array.from({length:selection.rangeCount},(_,i)=>selection.getRangeAt(i).cloneRange()) : [];
  const buffer = node('textarea'); buffer.value = text; buffer.readOnly = true; buffer.tabIndex = -1;
  buffer.className = 'copy-buffer'; buffer.setAttribute('aria-hidden','true');
  // A modal dialog traps focus; put the copy field inside the current top layer.
  (document.querySelector('dialog[open]') || document.body).append(buffer);
  try {
    buffer.focus({preventScroll:true}); buffer.select(); buffer.setSelectionRange(0,text.length);
    return typeof document.execCommand === 'function' && document.execCommand('copy');
  } catch { return false; }
  finally {
    buffer.remove();
    if(active instanceof HTMLElement && active.isConnected)active.focus({preventScroll:true});
    if(selection){selection.removeAllRanges();for(const range of ranges)selection.addRange(range);}
  }
}
function copyButton(text, subject='战报全文') {
  const button = node('button','复制全文','secondary copy-report'); button.type='button';
  button.addEventListener('click',async()=>{
    try {
      // Keep the synchronous path in the original user click, including HTTP/sandbox.
      if(!legacyCopy(text)) {
        if(!navigator.clipboard?.writeText)throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(text);
      }
      button.textContent='已复制'; button.setAttribute('aria-label','已复制'+subject);
    } catch {
      button.textContent='复制失败'; button.setAttribute('aria-label','复制'+subject+'失败');
      notice('浏览器阻止了复制，请允许剪贴板访问后重试。');
    }
  });
  return button;
}
function notice(text = '') { $('notice').textContent = text; $('notice').hidden = !text; }
function setRunning(value) { running = value; $('start').disabled = !dbReady || (value && view === 'comparison'); $('cancel').hidden = !value; $('history').disabled = value; }
function monthDates() {
  const [y,m] = $('month').value.split('-').map(Number);
  if (!y || !m) return;
  $('publish-start').value = `${y}-${String(m).padStart(2,'0')}-01`;
  $('publish-end').value = `${y}-${String(m).padStart(2,'0')}-${new Date(y,m,0).getDate()}`;
}
$('month').addEventListener('change', monthDates);
function readUrl() {
  const source = new URL($('source-url').value.trim());
  if (source.protocol !== 'https:' || source.hostname !== 'rep.ygobbs2.com' || !['','/','/index.php'].includes(source.pathname) || source.username || source.password || (source.port && source.port !== '443')) throw new Error('请粘贴 rep.ygobbs2.com 的 HTTPS 查询地址。');
  if (source.searchParams.get('c') !== 'search' || source.searchParams.get('a') !== 'sub') throw new Error('这不是战报搜索结果地址。');
  if (source.searchParams.get('username') || source.searchParams.get('playername')) throw new Error('整队核对请清空发布者和选手筛选。');
  const team = source.searchParams.get('groupname') || '';
  $('site-team').value = team;
  if (!$('team').value) $('team').value = team;
  const start = source.searchParams.get('starttime'), end = source.searchParams.get('endtime');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start || '') || !/^\d{4}-\d{2}-\d{2}$/.test(end || '')) throw new Error('地址中的日期格式不正确。');
  $('month').value = start.slice(0,7); $('publish-start').value = start; $('publish-end').value = end;
  notice('已读取地址。请确认本地战队、比赛月份与网站发布范围。');
}
$('apply-url').addEventListener('click', () => { try { readUrl(); } catch(e) { notice(e.message); } });
async function options(initial = false) {
  const data = await bridge.apiGet('comparison/options');
  dbReady = data.ready;
  pageRole=data.role;
  credentialTransport=data.secure_passwords===true;
  $('accounts-open').hidden=pageRole!=='master' || !credentialTransport;
  document.querySelectorAll('[data-master-only]').forEach(el=>{el.hidden=pageRole!=='master';});
  if(initial)$('management-teams').replaceChildren(...data.teams.map(t=>{const e=node('option');e.value=t;return e;}));
  if (initial) {
    $('month').value = data.month; monthDates();
    $('audit-month').value=data.current_month || new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit'}).format(new Date());
    $('teams').replaceChildren(...data.teams.map(t => {const e = node('option'); e.value = t; return e;}));
    if (data.teams.length === 1) $('team').value = data.teams[0];
    $('audit-team').value=$('team').value;
  }
  $('audit-start').disabled=!dbReady;
  const recent = node('option','最近的核对'); recent.value = '';
  $('history').replaceChildren(recent, ...data.history.map(j => {
    const e = node('option', `${j.query.team} · ${j.query.start.slice(0,7)} · ${formatTime(j.created_at)} · ${labels[j.status] || j.status}`);
    e.value = j.id; return e;
  }));
  $('history').value = jobId;
  setRunning(running);
  return data;
}
$('query-form').addEventListener('submit', async e => {
  e.preventDefault();
  if (view !== 'comparison') { libraryOffset = 0; await loadLibrary(); return; }
  if (running) return;
  notice(); $('start').disabled = true;
  try {
    const job = await bridge.apiPost('comparison/jobs', {team:$('team').value.trim(), site_team:$('site-team').value.trim(), month:$('month').value,
      source_url:$('source-url').value.trim(), publish_start:$('publish-start').value, publish_end:$('publish-end').value});
    category = 'matched'; offset = 0; jobId = job.id; lastResult=null; setRunning(true); $('download').disabled = true;
    $('records').replaceChildren(); $('categories').replaceChildren();
    for (const id of ['site-total','db-total','matched','review-total']) $(id).textContent = '—';
    await refresh();
  } catch(e) { notice(e.message); setRunning(false); }
});
function categories(job) {
  $('categories').replaceChildren(...(job.categories || []).map(c => {
    const e = node('button', c.label, c.key === category ? 'active' : '');
    e.type = 'button'; e.setAttribute('aria-pressed', String(c.key === category));
    e.append(node('span', c.count, 'count'));
    e.addEventListener('click', () => { category = c.key; offset = 0; refresh(); });
    return e;
  }));
  $('category-note').textContent = ((job.categories || []).find(c => c.key === category)?.note || '')
    + ($('report-outcome').value==='all'?'':` 当前仅显示${$('report-outcome').value==='win'?'胜场':'负场'}；上方为完整核对统计。`);
}
function drawRows(data) {
  $('records').replaceChildren(...data.records.map(row => {
    const r = row.site[0] || row.local[0] || {};
    const tr = node('tr'), identity = node('td');
    identity.append(node('strong', `${r.team_a || '待解析'} VS ${r.team_b || '待解析'}`),node('small', `${r.match_time || '日期未知'} · ${r.location || '地点未知'}`));
    tr.append(identity);
    for (const side of ['site','local']) {
      const td = node('td');
      if (row[side].length) td.append(...row[side].map(r => {
        const block = node('article',null,'inline-report'), top = node('div',null,'report-meta');
        top.append(selectLabel({...r,source:side === 'site' ? 'cloud' : 'local',home_team:side === 'site' ? '' : data.job.query.team}),copyButton(r.body));
        block.append(top,node('pre',r.body)); return block;
      }));
      else td.append(node('span','—','id-list'));
      if (row[side].length) td.append(node('small', [...new Set(row[side].map(r => r.winner).filter(Boolean))].join(' / ') || '暂无胜方'));
      tr.append(td);
    }
    const note = node('td', row.note || rowNotes[category] || '请查看原文核对');
    tr.append(note);
    const action = node('td'), button = node('button','查看原文','secondary');
    button.addEventListener('click', () => detail(row)); action.append(button); tr.append(action);
    return tr;
  }));
  $('empty').hidden = data.records.length > 0;
  if (!data.records.length) {
    $('empty').querySelector('h3').textContent = data.total ? '此页没有记录' : '本分类没有记录';
    $('empty').querySelector('p').textContent = '可以切换上方分类，查看其他核对结果。';
  }
  $('pagination').hidden = !data.total;
  $('page-label').textContent = `第 ${offset + 1}–${Math.min(offset + data.limit,data.total)} 项，共 ${data.total} 项`;
  $('prev').disabled = !offset; $('next').disabled = offset + data.limit >= data.total;
}
function pagingState() {
  const comparison = view === 'comparison', data = comparison ? lastResult : view==='audit'?auditData:libraryData;
  const ready = comparison ? !comparisonLoading && data?.job.status === 'completed' : view==='audit'?!!data && !auditLoading:!!data;
  const limit = data?.limit || 30, pages = Math.ceil((data?.total || 0) / limit);
  return {comparison, data, ready, limit, pages, current:Math.floor((data?.offset || 0) / limit) + 1};
}
function jumpToPage(page) {
  const state = pagingState();
  if (!state.ready || page < 1 || page > state.pages || page === state.current) return;
  if (state.comparison) {offset = (page-1)*state.limit; refresh();}
  else if(view==='audit'){auditOffset=(page-1)*state.limit;loadAudit();}
  else {libraryOffset = (page-1)*state.limit; loadLibrary();}
}
function syncPageNumbers(state) {
  const panel = $('page-numbers');
  panel.setAttribute('aria-busy', String(!state.ready));
  if (!state.ready) {for (const button of panel.children) button.disabled=true; return;}
  panel.hidden = !state.pages;
  const scope = state.comparison ? `${view}:${jobId}:${category}:${$('report-outcome').value}` : view==='audit'?`${view}:${JSON.stringify(state.data.query)}:${state.data.only_exceeded}`:`${view}:${JSON.stringify(state.data.query)}:${$('local-kind').value}:${$('report-outcome').value}`;
  if (panel.dataset.scope !== scope || panel.children.length !== state.pages) {
    panel.dataset.scope = scope; panel.dataset.current = ''; panel.scrollTop=0;
    const buttons=[];
    for (let page=1;page<=state.pages;page++) {
      const button=node('button',page,'page-number');button.type='button';
      button.setAttribute('aria-label',`第 ${page} 页`);button.title=`第 ${page} 页`;
      button.addEventListener('click',()=>jumpToPage(page));buttons.push(button);
    }
    panel.replaceChildren(...buttons);
  }
  for (const button of panel.children) {
    button.disabled=false;
    button.setAttribute('aria-controls',state.comparison?'records':view==='audit'?'audit-records':'library-records');
    if(Number(button.textContent)===state.current)button.setAttribute('aria-current','page');
    else button.removeAttribute('aria-current');
  }
  if(panel.dataset.current!==String(state.current)) {
    panel.dataset.current=state.current;
    const active=panel.children[state.current-1];
    if(active) {
      // Scroll only the page-number column; keep the report's reading position.
      const top=active.offsetTop, bottom=top+active.offsetHeight;
      if(top<panel.scrollTop)panel.scrollTop=top;
      else if(bottom>panel.scrollTop+panel.clientHeight)panel.scrollTop=bottom-panel.clientHeight;
    }
  }
}
function syncSidePaging() {
  if(['teams','members'].includes(view)){$('side-prev').disabled=true;$('side-next').disabled=true;$('page-numbers').hidden=true;return;}
  const state=pagingState(), {comparison,ready}=state, prefix = comparison ? '' : view==='audit'?'audit-':'library-';
  for (const direction of ['prev','next']) {
    const button = $(`side-${direction}`);
    button.disabled = !ready || $(`${prefix}pagination`).hidden || $(`${prefix}${direction}`).disabled;
    button.setAttribute('aria-controls', comparison ? 'records' : view==='audit'?'audit-records':'library-records');
  }
  syncPageNumbers(state);
}
for (const direction of ['prev','next']) $('side-'+direction).addEventListener('click',()=>{
  // Reuse the current tab's existing pagination action and boundaries.
  $(`${view === 'comparison' ? '' : view==='audit'?'audit-':'library-'}${direction}`).click();
});
async function refresh() {
  clearTimeout(timer); if (!jobId) return;
  const selected = jobId, version = ++sequence;
  comparisonLoading = true; syncSidePaging();
  try {
    const data = await bridge.apiGet(`comparison/jobs/${selected}`,{category,offset,limit:30,outcome:$('report-outcome').value});
    if (version !== sequence || selected !== jobId) return;
    lastResult = data;
    const job = data.job, active = ['pending','running'].includes(job.status);
    $('run-status').textContent = labels[job.status] || job.status; $('run-message').textContent = job.message + (job.archive ? ` 云端归档：新增 ${job.archive.inserted}，刷新 ${job.archive.refreshed}。` : '');
    $('scope').textContent = `${job.query.team} · 比赛 ${job.query.start} 至 ${job.query.end} · 网站发布 ${job.query.publish_start} 至 ${job.query.publish_end}${job.finished_at ? ' · 核对于 '+formatTime(job.finished_at) : ''}`;
    $('progress').hidden = !active; $('progress').value = job.pages ? job.page / job.pages * 100 : 0;
    setRunning(active); $('download').disabled = job.status !== 'completed';
    if (job.status !== 'completed') {
      for (const id of ['site-total','db-total','matched','review-total']) $(id).textContent = '—';
    }
    if (active) {
      $('empty').hidden = false; $('empty').querySelector('h3').textContent = '正在读取网站战报';
      $('empty').querySelector('p').textContent = '抓取完成后会读取本地数据并展示核对结果。';
      timer = setTimeout(refresh,1200); return;
    }
    await options();
    if (version !== sequence || selected !== jobId) return;
    if (job.status !== 'completed') {
      notice(job.message); $('records').replaceChildren(); $('categories').replaceChildren(); $('pagination').hidden = true;
      $('category-note').textContent = ''; $('empty').hidden = false; $('empty').querySelector('h3').textContent = '本次未生成核对结果';
      $('empty').querySelector('p').textContent = '完整抓取成功后才会进行对比，可以调整范围后重试。'; return;
    }
    const summary = job.summary;
    $('site-total').textContent = summary.site_total; $('db-total').textContent = summary.db_total;
    $('matched').textContent = summary.matched_groups; $('review-total').textContent = summary.review_count;
    categories(job); drawRows(data);
    if (view !== 'comparison') await loadLibrary();
    const excluded = Object.values(summary.excluded || {}).reduce((a,b) => a + b, 0);
    if (excluded) notice(`已排除 ${excluded} 份踢馆等非友谊战报。`);
  } catch(e) {
    if (selected !== jobId || version !== sequence) return;
    notice(e.message); if (running) timer = setTimeout(refresh,3000);
  } finally {if (version === sequence) {comparisonLoading = false; syncSidePaging();}}
}
$('history').addEventListener('change', () => { if (!$('history').value) return; jobId = $('history').value; category = 'matched'; offset = 0; notice(); refresh(); });
$('prev').addEventListener('click', () => {offset = Math.max(0,offset - 30); refresh();});
$('next').addEventListener('click', () => {if (lastResult && offset + 30 < lastResult.total) {offset += 30; refresh();}});
$('cancel').addEventListener('click', async () => {try {await bridge.apiPost(`comparison/jobs/${jobId}/cancel`,{}); await refresh();} catch(e) {notice(e.message);}});
$('download').addEventListener('click', async () => {try {await bridge.download(`comparison/jobs/${jobId}/download`,{},`战报核对-${lastResult.job.query.team}-${lastResult.job.query.start.slice(0,7)}.json`);} catch(e) {notice(e.message);}});
function detail(row) {
  $('detail-note').textContent = row.note || $('category-note').textContent;
  for (const side of ['site','local']) {
    const target = $(side === 'site' ? 'site-detail' : 'local-detail');
    target.replaceChildren(...row[side].map(r => {
      const block = node('article',null,'report-block'), top = node('div',null,'report-meta');
      top.append(node('strong',`#${r.id} · 胜方 ${r.winner || '未知'}`));
      const copy = copyButton(r.body);
      top.append(copy); block.append(top);
      if (r.publish_date || r.publisher) block.append(node('p', `${r.publish_date ? `发布于 ${r.publish_date} · ` : ''}${r.publisher || ''}`, 'hint'));
      if (r.errors?.length || r.warnings?.length) block.append(node('p',[...(r.errors || []),...(r.warnings || [])].join('；'),'hint warnings'));
      block.append(node('pre',r.body)); return block;
    }));
    if (!row[side].length) target.append(node('p','此侧没有相同内容的记录。','hint'));
  }
  $('detail').showModal();
}
$('close-detail').addEventListener('click', () => $('detail').close());
$('detail').addEventListener('click', e => {if (e.target === $('detail')) {const r = $('detail').getBoundingClientRect(); if(e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) $('detail').close();}});
window.addEventListener('pagehide', () => { clearTimeout(timer); clearTimeout(dispatchTimer); });
// 查询页与跨页选择篮：发送内容始终由后台重新读取，前端只提交来源和编号。
function selectKey(record) { return `${record.source}:${record.id}`; }
function selectionChanged() {
  const records = [...selection.values()], local = records.filter(r => r.source === 'local').length;
  $('selection-count').textContent = `已选 ${records.length} 份战报${records.length ? ` · 本地 ${local} / 云端 ${records.length-local}` : ''}`;
  $('send-qq').disabled = !records.length; $('clear-selection').disabled = !records.length;
  document.querySelectorAll('[data-report-key]').forEach(input => { input.checked = selection.has(input.dataset.reportKey); });
  const current = libraryData?.records || [];
  $('library-select-all').checked = current.length > 0 && current.every(r => selection.has(selectKey(r)));
  $('library-select-all').indeterminate = current.some(r => selection.has(selectKey(r))) && !$('library-select-all').checked;
}
function selectLabel(record, showId = true) {
  const label = node('label',null,'report-select'), input = node('input');
  input.type = 'checkbox'; input.dataset.reportKey = selectKey(record); input.checked = selection.has(selectKey(record));
  input.setAttribute('aria-label',`勾选${record.source === 'cloud' ? '云端' : '本地'}战报 ${record.id}`);
  input.addEventListener('change', () => {if (input.checked) selection.set(selectKey(record),record); else selection.delete(selectKey(record)); selectionChanged();});
  label.append(input); if (showId) label.append(node('span',`#${record.id}`,'id-list')); return label;
}
function selectComparisonPage(source) {
  for (const row of lastResult?.records || []) for (const record of row[source === 'cloud' ? 'site' : 'local']) {
    const r = {...record,source,home_team:source === 'local' ? lastResult.job.query.team : ''}; selection.set(selectKey(r),r);
  }
  selectionChanged();
}
$('select-site-page').addEventListener('click',()=>selectComparisonPage('cloud'));
$('select-local-page').addEventListener('click',()=>selectComparisonPage('local'));
$('clear-selection').addEventListener('click',()=>{selection.clear();selectionChanged();});
function clearAuditSummary(){
  for(const id of ['audit-report-count','audit-day-count','audit-pair-count','audit-excess-count'])$(id).textContent='—';
  $('audit-scope').textContent='';$('audit-skipped').hidden=true;$('audit-skipped').textContent='';
}
async function loadAudit(){
  if(view!=='audit' || !dbReady)return;
  const version=++auditSequence;
  const params={team:$('audit-team').value.trim(),month:$('audit-month').value,
    only_exceeded:$('audit-only').checked?'1':'0',offset:auditOffset,limit:30};
  auditLoading=true;auditData=null;clearAuditSummary();notice();
  $('audit-start').disabled=true;$('audit-records').inert=true;$('audit-records').setAttribute('aria-busy','true');
  $('audit-prev').disabled=true;$('audit-next').disabled=true;syncSidePaging();
  $('audit-empty').hidden=$('audit-records').children.length>0;
  $('audit-empty').querySelector('h3').textContent='正在检查';$('audit-empty').querySelector('p').textContent='';
  try{
    const data=await bridge.apiGet('comparison/friendly-limits',params);
    if(version!==auditSequence || view!=='audit')return;
    auditData=data;
    for(const [id,key] of [['audit-report-count','reports'],['audit-day-count','days'],['audit-pair-count','exceeded_pairs'],['audit-excess-count','excess_wins']])$(id).textContent=data.summary[key];
    $('audit-scope').textContent=`${data.query.team} · ${data.query.start} 至 ${data.query.end}`;
    $('audit-skipped').hidden=!data.summary.skipped_reports;
    $('audit-skipped').textContent=data.summary.skipped_reports?`另有 ${data.summary.skipped_reports} 份胜负不明或战队不符的记录未计入，请核对原文。`:'';
    $('audit-records').replaceChildren(...data.records.map(row=>{
      const card=node('article',null,`audit-card${row.exceeded?' exceeded':''}`);
      card.append(node('span',row.date,'hint'),node('h3',`${row.team} VS ${row.opponent}`));
      const scores=node('div',null,'audit-scores');
      for(const [team,wins,excess] of [[row.team,row.wins,row.excess_wins],[row.opponent,row.opponent_wins,row.opponent_excess_wins]]){
        const score=node('div',null,`audit-score${excess?' over':''}`);
        score.append(node('span',team),node('strong',`${wins} 胜`),node('small',excess?`超出 ${excess} 胜`:`上限 ${data.daily_limit} 胜`));scores.append(score);
      }
      const footer=node('div',null,'audit-card-footer'),button=node('button','查看战报','secondary');button.type='button';
      button.addEventListener('click',()=>showAuditReports(row,data.query));
      footer.append(node('span',`${row.total} 场友谊 · ${row.exceeded?'存在超限':'未超限'}`,'hint'),button);card.append(scores,footer);return card;
    }));
    $('audit-empty').hidden=data.records.length>0;
    $('audit-empty').querySelector('h3').textContent=data.total?'此页没有记录':data.summary.reports?(data.only_exceeded?'未发现超限友谊':'暂无记录'):'没有可判定胜负的友谊记录';
    $('audit-empty').querySelector('p').textContent=data.summary.reports?`已检查 ${data.summary.pair_days} 个日期与对手组合。`:'可调整战队或比赛月份后重新检查。';
    $('audit-pagination').hidden=!data.total;
    $('audit-page-label').textContent=`共 ${data.total} 组 · 第 ${Math.floor(data.offset/data.limit)+1} / ${Math.max(1,Math.ceil(data.total/data.limit))} 页`;
    $('audit-prev').disabled=!data.offset;$('audit-next').disabled=data.offset+data.limit>=data.total;
  }catch(error){
    if(version!==auditSequence || view!=='audit')return;
    $('audit-records').replaceChildren();$('audit-empty').hidden=false;$('audit-pagination').hidden=true;
    $('audit-empty').querySelector('h3').textContent='检查未完成';$('audit-empty').querySelector('p').textContent=error.message;
  }finally{
    if(version===auditSequence){auditLoading=false;$('audit-start').disabled=!dbReady;$('audit-records').inert=false;$('audit-records').removeAttribute('aria-busy');syncSidePaging();}
  }
}
$('audit-form').addEventListener('submit',event=>{event.preventDefault();auditOffset=0;loadAudit();});
$('audit-only').addEventListener('change',()=>{auditOffset=0;loadAudit();});
$('audit-prev').addEventListener('click',()=>{if(!auditLoading && auditOffset){auditOffset=Math.max(0,auditOffset-30);loadAudit();}});
$('audit-next').addEventListener('click',()=>{if(!auditLoading && auditData && auditOffset+30<auditData.total){auditOffset+=30;loadAudit();}});
function showAuditReports(row,query){
  auditDetailTarget={row,query,total:0};auditDetailOffset=0;
  $('audit-detail-title').textContent=`${row.date} · ${row.team} VS ${row.opponent}`;
  $('audit-detail-records').replaceChildren();$('audit-detail').showModal();loadAuditReports();
}
async function loadAuditReports(){
  const target=auditDetailTarget,version=++auditDetailSequence;
  if(!target || !$('audit-detail').open)return;
  auditDetailLoading=true;$('audit-detail-records').inert=true;
  $('audit-detail-notice').hidden=false;$('audit-detail-notice').textContent='正在读取战报';
  $('audit-detail-prev').disabled=true;$('audit-detail-next').disabled=true;$('audit-detail-page-label').textContent='';
  try{
    const data=await bridge.apiGet('comparison/friendly-limits/reports',{team:target.query.team,month:target.query.start.slice(0,7),
      day:target.row.date,opponent:target.row.opponent,offset:auditDetailOffset,limit:30});
    if(version!==auditDetailSequence || target!==auditDetailTarget || !$('audit-detail').open || view!=='audit')return;
    target.total=data.total;
    $('audit-detail-records').replaceChildren(...data.records.map(report=>{
      const card=node('article',null,'library-report'),top=node('div',null,'report-identity');
      top.append(node('strong',`#${report.id} · 胜方 ${report.winner}`),copyButton(report.body));
      card.append(top,node('p',`${report.team_a} VS ${report.team_b} · ${report.match_time}`,'audit-detail-meta'),node('pre',report.body));return card;
    }));
    $('audit-detail-notice').hidden=data.records.length>0;
    $('audit-detail-notice').textContent=data.total?'此页没有战报，请切换页码。':'对应战报已不存在，请关闭后重新检查。';
    $('audit-detail-page-label').textContent=`共 ${data.total} 份 · 第 ${Math.floor(data.offset/data.limit)+1} / ${Math.max(1,Math.ceil(data.total/data.limit))} 页`;
    $('audit-detail-prev').disabled=!data.offset;$('audit-detail-next').disabled=data.offset+data.limit>=data.total;
  }catch(error){
    if(version!==auditDetailSequence || target!==auditDetailTarget || !$('audit-detail').open)return;
    $('audit-detail-records').replaceChildren();$('audit-detail-notice').hidden=false;$('audit-detail-notice').textContent=error.message;
  }finally{if(version===auditDetailSequence){auditDetailLoading=false;$('audit-detail-records').inert=false;}}
}
$('audit-detail-close').addEventListener('click',()=>$('audit-detail').close());
$('audit-detail').addEventListener('close',()=>{auditDetailSequence++;auditDetailTarget=null;auditDetailLoading=false;$('audit-detail-records').replaceChildren();$('audit-detail-title').textContent='对应战报';$('audit-detail-notice').textContent='';$('audit-detail-page-label').textContent='';});
$('audit-detail-prev').addEventListener('click',()=>{if(!auditDetailLoading && auditDetailOffset){auditDetailOffset-=30;loadAuditReports();}});
$('audit-detail-next').addEventListener('click',()=>{if(!auditDetailLoading && auditDetailTarget && auditDetailOffset+30<auditDetailTarget.total){auditDetailOffset+=30;loadAuditReports();}});
function activateView(next) {
  const management=['teams','members'].includes(next);
  if(management && pageRole!=='master')return;
  view=next;managementSequence++;managementOffset=0;auditSequence++;auditLoading=false;
  if($('audit-detail').open)$('audit-detail').close();
  $('management-view').hidden=!management;
  document.querySelector('.query-card').hidden=management || view==='audit';
  $('audit-query').hidden=view!=='audit';$('audit-view').hidden=view!=='audit';
  document.querySelector('.selection-bar').hidden=management || view==='audit';
  document.querySelector('.report-layout').hidden=management;
  document.querySelector('.footnote').hidden=management || view==='audit';
  if(management){
    document.querySelectorAll('#view-tabs button').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-pressed',String(b.dataset.view===view));});
    librarySequence++;$('management-keyword').value='';
    $('management-title').textContent=view==='teams'?'队伍管理':'队员管理';
    $('management-description').textContent=view==='teams'?'查看战队资料与绑定群，设置多名管理员。':'按战队管理角色、QQ号及已有参赛ID。';
    $('management-team-label').hidden=view!=='members';
    $('management-keyword').placeholder=view==='teams'?'搜索战队名':'搜索角色、QQ或参赛ID';
    $('management-add').textContent=view==='teams'?'设置管理员':'新增队员';
    if(view==='members' && !$('management-team').value)$('management-team').value=$('team').value;
    loadManagement();syncSidePaging();return;
  }
 libraryOffset=0; librarySequence++;
  document.querySelectorAll('#view-tabs button').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-pressed',String(b.dataset.view===view));});
  $('comparison-view').hidden=view!=='comparison'; $('library-view').hidden=!['local','cloud'].includes(view);
  if(view==='audit'){
    if(!$('audit-team').value)$('audit-team').value=$('team').value;
    if(dbReady && $('audit-team').value && $('audit-month').value){auditOffset=0;loadAudit();}
    else $('audit-start').disabled=!dbReady;
    syncSidePaging();return;
  }
  document.querySelector('.advanced').hidden=view!=='comparison'; $('site-team').parentElement.hidden=view==='local';
  $('source-url').disabled=view!=='comparison';$('publish-start').disabled=view!=='comparison';$('publish-end').disabled=view!=='comparison';$('site-team').disabled=view==='local';
  $('query-title').textContent=view==='comparison'?'选择核对范围':view==='local'?'筛选本地上传战报':'筛选云端归档战报';
  $('query-description').textContent=view==='comparison'?'默认查询上个月的友谊战报':view==='local'?'包含本地上传的友谊和踢馆记录':'按网站ID独立保存，不计入本地战绩';
  $('query-hint').textContent=view==='comparison'?'完整拉取后保存云端副本，本地战报不变。':'跨页保留勾选，可批量私聊发送到QQ。';
  $('start').textContent=view==='comparison'?'开始核对 ↗':'刷新列表'; setRunning(running);
  $('local-kind-label').hidden=view!=='local'; $('fetch-cloud').hidden=view!=='cloud';
  $('library-title').textContent=view==='local'?'本地战报':'云端战报';
  $('library-description').textContent=view==='local'?'按本地比赛编号显示，不依赖是否抓取过网站。':'完整拉取后按云端ID保存一份；再次拉取刷新原记录。';
  if (view!=='comparison' && dbReady && $('team').value && $('month').value) loadLibrary();
  else if (view==='comparison' && jobId && !lastResult) refresh();
  syncSidePaging();
}
document.querySelectorAll('#view-tabs button').forEach(b=>b.addEventListener('click',()=>activateView(b.dataset.view)));
function storedTime(value) { return value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(Number(value)*1000)) : '未记录'; }
async function loadLibrary() {
  if (!['local','cloud'].includes(view)) return;
  const selectedView=view, version=++librarySequence;
  $('start').disabled=true; notice();libraryData=null;
  // Keep the previous cards until the new page arrives; clearing them here collapses
  // the document and makes both scrollY and the sticky right rail jump during I/O.
  $('library-records').inert=true;$('library-records').setAttribute('aria-busy','true');
  $('library-view').setAttribute('aria-busy','true');
  $('library-prev').disabled=true;$('library-next').disabled=true;syncSidePaging();
  $('library-empty').hidden=$('library-records').children.length>0;
  $('library-empty').querySelector('h3').textContent='正在读取战报';$('library-empty').querySelector('p').textContent='';
  try {
    const data=await bridge.apiGet(`comparison/${view==='local'?'local-reports':'cloud-reports'}`,{
      team:$('team').value.trim(),site_team:view==='cloud'?$('site-team').value.trim():'',month:$('month').value,
      kind:$('local-kind').value,outcome:$('report-outcome').value,offset:libraryOffset,limit:30});
    if (version!==librarySequence || selectedView!==view) return;
    libraryData=data;
    $('library-scope').textContent=`${data.query.team} · ${data.query.start} 至 ${data.query.end} · 共 ${data.total} 份`;
    $('library-records').replaceChildren(...data.records.map(r=>{
      const card=node('article',null,'library-report'), top=node('div',null,'report-meta');
      const identity=node('div',null,'report-identity');identity.append(selectLabel(r),node('strong',`${r.team_a || '待解析'} VS ${r.team_b || '待解析'}`));
      top.append(identity,copyButton(r.body));
      const kind=r.kind==='raid'?'踢馆':r.kind==='friendly'?'友谊':'待解析';
      const status=r.source==='local'?(r.export_sent?'已标记发送':'未标记发送'):'云端归档';
      const meta=node('div',null,'report-info');
      meta.append(node('span',`${kind} · ${status}`),node('span',`比赛 ${r.match_time || '日期未知'}`),
        node('span',`${r.source==='local'?'上传':'发布'}人 ${r.publisher || '未记录'}`),
        node('span',`${r.source==='local'?'上传':'最近抓取'} ${storedTime(r.uploaded_at)}`));
      if(r.publish_date)meta.append(node('span',`发布 ${r.publish_date}`));
      card.append(top,meta,node('pre',r.body));return card;
    }));
    $('library-empty').hidden=data.records.length>0;$('library-empty').querySelector('h3').textContent='暂无记录';$('library-empty').querySelector('p').textContent='当前战队和月份没有符合条件的战报。'; $('library-pagination').hidden=!data.total;
    $('library-page-label').textContent=`第 ${libraryOffset+1}–${Math.min(libraryOffset+data.limit,data.total)} 项，共 ${data.total} 项`;
    $('library-prev').disabled=!libraryOffset; $('library-next').disabled=libraryOffset+data.limit>=data.total;
    selectionChanged();
  } catch(e) {
    if(version===librarySequence){
      notice(e.message);$('library-records').replaceChildren();$('library-pagination').hidden=true;
      $('library-empty').hidden=false;$('library-empty').querySelector('h3').textContent='未能读取战报';
      $('library-empty').querySelector('p').textContent='请检查范围或连接后重新刷新。';
    }
  } finally {
    if(version===librarySequence){
      $('library-records').inert=false;$('library-records').setAttribute('aria-busy','false');
      $('library-view').setAttribute('aria-busy','false');setRunning(running);syncSidePaging();
    }
  }
}
$('report-outcome').addEventListener('change',()=>{
  offset=0;libraryOffset=0;
  if(view==='comparison'){if(jobId)refresh();}
  else if(dbReady && $('team').value && $('month').value)loadLibrary();
});
$('local-kind').addEventListener('change',()=>{libraryOffset=0;loadLibrary();});
$('library-prev').addEventListener('click',()=>{libraryOffset=Math.max(0,libraryOffset-30);loadLibrary();});
$('library-next').addEventListener('click',()=>{if(libraryData&&libraryOffset+30<libraryData.total){libraryOffset+=30;loadLibrary();}});
$('library-select-all').addEventListener('change',()=>{for(const r of libraryData?.records||[]){if($('library-select-all').checked)selection.set(selectKey(r),r);else selection.delete(selectKey(r));}selectionChanged();});
$('fetch-cloud').addEventListener('click',()=>{activateView('comparison');$('query-form').requestSubmit();});

function dispatchNotice(text='') {$('dispatch-notice').textContent=text;$('dispatch-notice').hidden=!text;}
function invalidateDispatch() {if(dispatchRunning)return;dispatchId='';$('dispatch-confirm').disabled=true;$('dispatch-retry').hidden=true;$('dispatch-preview').replaceChildren();$('dispatch-results').replaceChildren();}
async function loadAdmins() {
  invalidateDispatch();$('dispatch-admins').replaceChildren();
  if($('dispatch-mode').value!=='admins'||!$('dispatch-team').value.trim())return;
  try {
    const data=await bridge.apiGet('comparison/dispatch/admins',{team:$('dispatch-team').value.trim()});
    $('dispatch-admins').replaceChildren(...data.admins.map(qq=>{const label=node('label',null,'report-select'),input=node('input');input.type='checkbox';input.value=qq;label.append(input,node('span',qq));return label;}));
    if(!data.admins.length)$('dispatch-admins').append(node('p','该战队尚未设置管理员。','hint'));
  }catch(e){dispatchNotice(e.message);}
}
async function openDispatch(historyOnly=false) {
  dispatchNotice();$('dispatch-dialog').showModal();$('dispatch-prepare').disabled=true;
  try{
    const data=await bridge.apiGet('comparison/dispatch/options');
    const active=data.history.find(batch=>batch.status==='running');
    dispatchRunning=!!active;
    if(!historyOnly&&!active)invalidateDispatch();
    const options=[];
    for(const connection of data.connections){for(const account of (connection.accounts.length?connection.accounts:[''])){const option=node('option',`${connection.label}${account?' · QQ '+account:''}`);option.value=JSON.stringify({platform_id:connection.id,self_id:account});options.push(option);}}
    $('dispatch-platform').replaceChildren(...options);
    if(!options.length){const option=node('option','没有可用的OneBot连接');option.value='';$('dispatch-platform').append(option);dispatchNotice('需要已连接的 SnowLuma／OneBot 机器人才能发送QQ私聊。');}
    $('dispatch-teams').replaceChildren(...data.admin_teams.map(team=>{const option=node('option');option.value=team;return option;}));
    const empty=node('option','最近发送批次');empty.value='';$('dispatch-history').replaceChildren(empty,...data.history.map(batch=>{const option=node('option',`${storedTime(batch.created_at)} · ${batch.report_count}份 · ${dispatchLabels[batch.status] || '未知状态'}`);option.value=batch.id;return option;}));
    if(!$('dispatch-team').value)$('dispatch-team').value=$('team').value;
    $('dispatch-prepare').disabled=!selection.size || !options.length || dispatchRunning;
    if(active){dispatchId=active.id;await pollDispatch(true);}
    else if(historyOnly && data.history.length){dispatchId=data.history[0].id;await pollDispatch(true);}
  }catch(e){dispatchNotice(e.message);}
}
$('send-qq').addEventListener('click',()=>openDispatch(false));$('dispatch-history-open').addEventListener('click',()=>openDispatch(true));
$('dispatch-close').addEventListener('click',()=>{$('dispatch-dialog').close();clearTimeout(dispatchTimer);});
$('dispatch-dialog').addEventListener('close',()=>clearTimeout(dispatchTimer));
$('dispatch-mode').addEventListener('change',()=>{invalidateDispatch();const admins=$('dispatch-mode').value==='admins';$('dispatch-custom-label').hidden=admins;$('dispatch-team-label').hidden=!admins;$('dispatch-admins').hidden=!admins;if(admins)loadAdmins();});
$('dispatch-team').addEventListener('change',loadAdmins);
for(const id of ['dispatch-qqs','dispatch-platform'])$(id).addEventListener('input',invalidateDispatch);
$('dispatch-admins').addEventListener('change',invalidateDispatch);
$('dispatch-history').addEventListener('change',()=>{if($('dispatch-history').value){dispatchId=$('dispatch-history').value;pollDispatch(true);}});
$('dispatch-prepare').addEventListener('click',async()=>{
  $('dispatch-prepare').disabled=true;dispatchNotice();
  try{
    const connection=JSON.parse($('dispatch-platform').value || '{}');
    const plan=await bridge.apiPost('comparison/dispatch/prepare',{...connection,reports:[...selection.values()].map(r=>({source:r.source,id:r.id,home_team:r.home_team || ''})),
      target_mode:$('dispatch-mode').value,qq_ids:$('dispatch-qqs').value,admin_team:$('dispatch-team').value.trim(),admin_ids:[...$('dispatch-admins').querySelectorAll('input:checked')].map(i=>i.value)});
    dispatchId=plan.id;renderDispatch(plan,true);
  }catch(e){dispatchNotice(e.message);}finally{$('dispatch-prepare').disabled=!selection.size || dispatchRunning;}
});
function renderDispatch(plan,preview=false) {
  dispatchRunning=plan.status==='running';$('dispatch-confirm').disabled=plan.status!=='prepared';
  $('dispatch-stop').hidden=!dispatchRunning;$('dispatch-retry').hidden=dispatchRunning || !(plan.counts.failed+plan.counts.cancelled);
  for(const id of ['dispatch-platform','dispatch-mode','dispatch-qqs','dispatch-team','dispatch-history'])$(id).disabled=dispatchRunning;
  $('dispatch-prepare').disabled=!selection.size || dispatchRunning;
  const summary=node('p',`${dispatchLabels[plan.status] || '未知状态'} · 机器人 QQ ${plan.self_id} · ${plan.report_count}份战报 → ${plan.recipients.join('、')}`);
  if(preview){
    const contents=node('details'), title=node('summary','查看本次将发送的原文');contents.append(title);
    for(const report of plan.reports){const section=node('article',null,'report-block');section.append(node('strong',`${report.source==='cloud'?'云端':'本地'} #${report.id} · ${report.team_a} VS ${report.team_b}`),node('pre',report.body));contents.append(section);}
    $('dispatch-preview').replaceChildren(summary,contents,node('p',plan.status==='prepared'?'请核对接收QQ和原文后点击“确认发送”。':'本批次保留的发送原文快照。','hint'));
  }else $('dispatch-preview').replaceChildren(summary);
  const results=plan.entries.map(e=>{const labels={pending:'等待发送',sending:'发送中',sent:'已确认成功',failed:'失败',unknown:'结果未确认',cancelled:'未发送'};return node('p',`QQ ${e.qq} · 第${e.start+1}–${e.start+e.count}份：${labels[e.status]}${e.error?' · '+e.error:''}`);});
  $('dispatch-results').replaceChildren(...results);
  if(plan.counts.unknown)dispatchNotice('存在未确认的发送结果，请先到QQ核实；重试按钮不会重发这些部分。');
}
async function pollDispatch(preview=false){
  clearTimeout(dispatchTimer);if(!dispatchId)return;const id=dispatchId,version=++dispatchSequence;
  try{const data=await bridge.apiGet(`comparison/dispatch/${id}`,{preview:preview?'1':'0'});if(id!==dispatchId||version!==dispatchSequence)return;renderDispatch(data,data.reports.some(r=>typeof r.body==='string'));if(dispatchRunning&&$('dispatch-dialog').open)dispatchTimer=setTimeout(pollDispatch,1200);}catch(e){dispatchNotice(e.message);}
}
async function confirmDispatch(retry=false){
  if(!dispatchId)return;$('dispatch-confirm').disabled=true;$('dispatch-retry').disabled=true;dispatchNotice();
  try{const data=await bridge.apiPost(`comparison/dispatch/${dispatchId}/send`,{retry});renderDispatch(data);if(dispatchRunning)dispatchTimer=setTimeout(pollDispatch,700);}catch(e){dispatchNotice(e.message);await pollDispatch();}finally{$('dispatch-retry').disabled=false;}
}
$('dispatch-confirm').addEventListener('click',()=>confirmDispatch());$('dispatch-retry').addEventListener('click',()=>confirmDispatch(true));
$('dispatch-stop').addEventListener('click',async()=>{try{renderDispatch(await bridge.apiPost(`comparison/dispatch/${dispatchId}/cancel`,{}));clearTimeout(dispatchTimer);}catch(e){dispatchNotice(e.message);}});

let resetAccountId='',accountsGeneration=0;
function accountsNotice(text=''){$('accounts-notice').textContent=text;$('accounts-notice').hidden=!text;}
async function loadAccounts(){
  const data=await bridge.apiGet('comparison/accounts');
  $('accounts-list').replaceChildren(...data.accounts.map(account=>{
    const row=node('article',null,'account-row'),identity=node('div');
    identity.append(node('strong',account.username),node('span',`QQ ${account.qq} · ${account.enabled?'密码登录已启用':'密码登录已停用'}`,'hint'));
    const buttons=node('div',null,'account-actions');
    for(const [action,label] of [...(account.initial_password?[['initial','查看初始密码']]:[]),['reset','重置密码'],[account.enabled?'disable':'enable',account.enabled?'停用密码':'启用密码']]){
      const button=node('button',label,'secondary');button.type='button';
      button.addEventListener('click',async()=>{
        if(!credentialTransport){accountsNotice('请通过HTTPS查看或修改账号密码。');return;}
        if(action==='reset'){resetAccountId=account.id;$('account-reset-title').textContent=`重置 ${account.username} 的密码`;$('account-reset').hidden=false;$('account-reset-password').focus();return;}
        button.disabled=true;accountsNotice();const generation=accountsGeneration;
        try{
          const result=await bridge.apiPost(`comparison/accounts/${account.id}`,{action});
          if(action==='initial'){
            if(generation!==accountsGeneration || !$('accounts-dialog').open)return;
            const box=$('account-initial');box.replaceChildren(node('p',`账号：${result.username}`));box.hidden=false;
            if(result.password){
              box.append(node('p',`初始密码：${result.password}`));
              const copy=copyButton(`账号：${result.username}\n初始密码：${result.password}`,'账号密码');copy.textContent='复制账号密码';box.append(copy);
            }else box.append(node('p','密码已修改，初始密码不再可用。'));
          }else{$('account-initial').replaceChildren();$('account-initial').hidden=true;await loadAccounts();}
        }
        catch(error){accountsNotice(error.message);button.disabled=false;}
      });buttons.append(button);
    }
    row.append(identity,buttons);return row;
  }));
}
$('accounts-open').addEventListener('click',async()=>{
  if(!credentialTransport){notice('请通过HTTPS查看或修改账号密码。');return;}
  $('accounts-dialog').showModal();accountsNotice();
  try{await loadAccounts();}catch(error){accountsNotice(error.message);}
});
function clearAccountSecrets(){
  accountsGeneration++;$('account-reset-title').textContent='';
  $('account-initial').replaceChildren();$('account-initial').hidden=true;
  $('account-reset-password').value='';$('account-reset').hidden=true;resetAccountId='';
}
$('accounts-close').addEventListener('click',()=>{$('accounts-dialog').close();clearAccountSecrets();});
$('accounts-dialog').addEventListener('close',clearAccountSecrets);
$('accounts-sync').addEventListener('click',async()=>{
  const button=$('accounts-sync');button.disabled=true;accountsNotice();clearAccountSecrets();
  try{const result=await bridge.apiPost('comparison/accounts',{action:'sync'});await loadAccounts();accountsNotice(`账号已同步，新增 ${result.created} 个；已有密码保持不变。`);}
  catch(error){accountsNotice(error.message);}finally{button.disabled=false;}
});
$('account-reset').addEventListener('submit',async event=>{
  event.preventDefault();if(!credentialTransport){accountsNotice('请通过HTTPS查看或修改账号密码。');return;}const button=event.submitter;button.disabled=true;accountsNotice();
  try{await bridge.apiPost(`comparison/accounts/${resetAccountId}`,{action:'reset',password:$('account-reset-password').value});$('account-reset-password').value='';$('account-reset').hidden=true;clearAccountSecrets();await loadAccounts();accountsNotice('密码已重置，原密码会话已失效。');}
  catch(error){accountsNotice(error.message);}finally{button.disabled=false;}
});
function managementNotice(text='',id='management-notice'){$(id).textContent=text;$(id).hidden=!text;}
function managementButton(label,handler){const button=node('button',label,'secondary');button.type='button';button.addEventListener('click',handler);return button;}
async function loadManagement(){
  if(pageRole!=='master' || !['teams','members'].includes(view))return;
  const version=++managementSequence,selected=view;
  $('management-refresh').disabled=true;$('management-add').disabled=!dbReady;
  $('management-prev').disabled=true;$('management-next').disabled=true;managementNotice();
  const team=$('management-team').value.trim();
  if(view==='members' && !team){$('management-records').replaceChildren();$('management-count').textContent='';$('management-pagination').hidden=true;managementNotice('请先选择战队喵。');$('management-refresh').disabled=false;return;}
  $('management-records').inert=true;$('management-records').setAttribute('aria-busy','true');
  try{
    const data=await bridge.apiGet(`comparison/management/${view}`,{team,keyword:$('management-keyword').value.trim(),offset:managementOffset,limit:30});
    if(version!==managementSequence || view!==selected || pageRole!=='master')return;
    managementTotal=data.total;
    $('management-count').textContent=`共 ${data.total} ${view==='teams'?'支战队':'名队员'}`;
    $('management-records').replaceChildren(...data.records.map(row=>{
      const card=node('article',null,'management-card'),body=node('div',null,'management-identity'),actions=node('div',null,'management-actions');
      if(view==='teams'){
        body.append(node('h3',row.team),node('p',`管理员：${row.admins.join('、') || '未设置'}`),node('p',`${row.member_count} 名角色 · ${row.player_count} 个参赛ID`,'hint'));
        const groups=node('div',null,'management-tags');
        for(const group of row.groups)groups.append(node('span',`${group.group_id} · ${group.chat_type || '未设用途'}${group.banned?' · 已禁用':''}`,'management-tag'));
        if(!row.groups.length)groups.append(node('span','暂无绑定群','hint'));body.append(groups);
        actions.append(managementButton('设置管理员',()=>openAdmin(row)),managementButton('管理队员',()=>{$('management-team').value=row.team;activateView('members');}));
        if(!Array.from($('management-teams').options).some(o=>o.value===row.team)){const option=node('option');option.value=row.team;$('management-teams').append(option);}
      }else{
        body.append(node('h3',row.name),node('p',`QQ：${row.qq_id || '未绑定'}`,'hint'));
        const ids=node('div',null,'management-tags');for(const player of row.players)ids.append(node('span',player,'management-tag'));
        if(!row.players.length)ids.append(node('span','暂无参赛ID','hint'));body.append(ids);
        actions.append(managementButton('编辑队员',()=>openMember({...row,team})),managementButton('管理ID',()=>openPlayers({...row,team})));
      }
      card.append(body,actions);return card;
    }));
    $('management-empty').hidden=data.records.length>0;
    $('management-pagination').hidden=!data.total;
    $('management-page-label').textContent=`第 ${Math.floor(managementOffset/30)+1} / ${Math.max(1,Math.ceil(data.total/30))} 页`;
    $('management-prev').disabled=!managementOffset;$('management-next').disabled=managementOffset+30>=data.total;
  }catch(error){if(version===managementSequence){$('management-records').replaceChildren();$('management-count').textContent='';$('management-empty').hidden=true;$('management-pagination').hidden=true;managementNotice(error.message);}}
  finally{if(version===managementSequence){$('management-records').inert=false;$('management-records').setAttribute('aria-busy','false');$('management-refresh').disabled=false;}}
}
$('management-search').addEventListener('submit',event=>{event.preventDefault();managementOffset=0;loadManagement();});
$('management-prev').addEventListener('click',()=>{if(managementOffset){managementOffset-=30;loadManagement();}});
$('management-next').addEventListener('click',()=>{if(managementOffset+30<managementTotal){managementOffset+=30;loadManagement();}});
$('management-add').addEventListener('click',()=>{if(pageRole!=='master')return;if(view==='teams')openAdmin();else if($('management-team').value.trim())openMember({team:$('management-team').value.trim()});else managementNotice('请先选择战队喵。');});
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.close).close()));
function setAdminPickerMode(mode){
  if(mode!=='manual')$('management-admin-manual-qq').value='';
  document.querySelectorAll('[data-admin-mode]').forEach(button=>{const active=button.dataset.adminMode===mode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
  $('management-admin-member-picker').hidden=mode!=='members';$('management-admin-manual-picker').hidden=mode!=='manual';
}
function adminPickerControls(){
  $('management-admin-save').disabled=!adminReady || adminSaving;
  $('management-admin-manual-add').disabled=!adminReady || adminSaving;
  $('management-admin-manual-qq').disabled=!adminReady || adminSaving;
}
function renderAdminSelection(){
  $('management-admin-selected-count').textContent=`${adminSelected.size} 人`;
  $('management-admin-selected-empty').hidden=adminSelected.size>0;
  $('management-admin-selected').replaceChildren(...Array.from(adminSelected,([qq,name])=>{
    const chip=node('span',null,'admin-chip');chip.setAttribute('role','listitem');chip.append(node('span',name?`${name} · ${qq}`:qq));
    const remove=managementButton('×',()=>{adminSelected.delete(qq);renderAdminSelection();renderAdminCandidates();});remove.setAttribute('aria-label',`移除管理员 ${name || qq}`);chip.append(remove);return chip;
  }));
}
function addSelectedAdmin(qq,name=''){
  if(!adminReady || adminSaving)return false;
  if(!adminSelected.has(qq) && adminSelected.size>=100){managementNotice('管理员最多添加100人喵。','management-admin-notice');return false;}
  adminSelected.set(qq,name || adminSelected.get(qq) || '');managementNotice('','management-admin-notice');renderAdminSelection();renderAdminCandidates();return true;
}
function addManualAdmin(){
  const input=$('management-admin-manual-qq'),qq=input.value.trim();
  if(!/^[1-9][0-9]{0,19}$/.test(qq)){managementNotice('请填写一个有效QQ号喵。','management-admin-notice');input.focus();return false;}
  if(!addSelectedAdmin(qq))return false;input.value='';return true;
}
let adminMemberRows=[];
function renderAdminCandidates(){
  $('management-admin-candidates').replaceChildren(...adminMemberRows.map(member=>{
    const row=node('article',null,'admin-candidate'),identity=node('div');identity.append(node('strong',member.name),node('span',member.qq_id?`QQ：${member.qq_id}`:'未绑定QQ','hint'));
    const valid=/^[1-9][0-9]{0,19}$/.test(member.qq_id || ''),selected=adminSelected.has(member.qq_id);
    const add=managementButton(selected?'已添加':valid?'添加':'未绑定QQ',()=>addSelectedAdmin(member.qq_id,member.name));add.disabled=!valid || selected || adminSaving;
    row.append(identity,add);return row;
  }));
  if(!adminMemberRows.length && adminReady){
    const empty=node('div',null,'admin-candidate-empty');empty.append(node('p','未找到对应成员喵。','hint'));
    empty.append(managementButton('手动添加QQ',()=>{setAdminPickerMode('manual');$('management-admin-manual-qq').focus();}));$('management-admin-candidates').append(empty);
  }
}
function resetAdminPickerTeam(){
  const team=$('management-admin-team').value.trim().toUpperCase();
  if(team!==adminTeam){
    adminTeam=team;adminTarget={team,admins:[]};adminExpectedKnown=false;adminReady=false;adminMemberOffset=0;adminMemberTotal=0;adminMemberRows=[];adminSelected.clear();
    $('management-admin-search').value='';$('management-admin-manual-qq').value='';renderAdminSelection();$('management-admin-candidates').replaceChildren();$('management-admin-candidate-page').textContent='';
    $('management-admin-candidate-prev').disabled=true;$('management-admin-candidate-next').disabled=true;adminPickerControls();
  }
  return team;
}
async function loadAdminMembers(){
  clearTimeout(adminMemberTimer);
  if(pageRole!=='master' || !$('management-admin-dialog').open || adminSaving)return;
  const team=resetAdminPickerTeam(),version=++adminMemberSequence,target=adminTarget;
  if(!team){$('management-admin-candidate-status').textContent='请先选择战队喵。';return;}
  $('management-admin-candidate-status').textContent='正在加载战队成员…';$('management-admin-candidates').inert=true;
  $('management-admin-candidate-prev').disabled=true;$('management-admin-candidate-next').disabled=true;
  try{
    const data=await bridge.apiGet('comparison/management/members',{team,keyword:$('management-admin-search').value.trim(),offset:adminMemberOffset,limit:20,include_admins:'1'});
    if(version!==adminMemberSequence || target!==adminTarget || !$('management-admin-dialog').open || pageRole!=='master')return;
    if(!adminExpectedKnown){adminTarget.admins=[...data.admins];adminSelected.clear();for(const qq of data.admins)adminSelected.set(qq,'');adminExpectedKnown=true;}
    adminReady=true;adminMemberRows=data.records;adminMemberTotal=data.total;
    for(const member of data.records)if(member.qq_id && adminSelected.has(member.qq_id))adminSelected.set(member.qq_id,member.name);
    renderAdminSelection();renderAdminCandidates();adminPickerControls();
    $('management-admin-candidate-status').textContent=`共 ${data.total} 名角色，点击“添加”加入管理员名单。`;
    $('management-admin-candidate-page').textContent=`第 ${Math.floor(adminMemberOffset/20)+1} / ${Math.max(1,Math.ceil(data.total/20))} 页`;
    $('management-admin-candidate-prev').disabled=!adminMemberOffset;$('management-admin-candidate-next').disabled=adminMemberOffset+20>=data.total;
  }catch(error){if(version===adminMemberSequence && target===adminTarget){adminMemberRows=[];$('management-admin-candidates').replaceChildren();$('management-admin-candidate-status').textContent=error.message;managementNotice(error.message,'management-admin-notice');}}
  finally{if(version===adminMemberSequence && target===adminTarget)$('management-admin-candidates').inert=false;}
}
function scheduleAdminMembers(teamChanged=false){
  if(adminSaving)return;
  clearTimeout(adminMemberTimer);adminMemberSequence++;adminMemberOffset=0;
  if(teamChanged)resetAdminPickerTeam();
  $('management-admin-candidates').inert=true;$('management-admin-candidate-prev').disabled=true;$('management-admin-candidate-next').disabled=true;
  adminMemberTimer=setTimeout(loadAdminMembers,250);
}
function clearAdminPicker(){
  clearTimeout(adminMemberTimer);adminMemberSequence++;adminSelected.clear();adminMemberRows=[];adminTarget=null;adminExpectedKnown=false;adminTeam='';adminReady=false;adminSaving=false;adminMemberOffset=0;adminMemberTotal=0;
  $('management-admin-form').inert=false;$('management-admin-candidates').inert=false;
  for(const id of ['management-admin-team','management-admin-search','management-admin-manual-qq'])$(id).value='';
  $('management-admin-candidates').replaceChildren();$('management-admin-candidate-status').textContent='';$('management-admin-candidate-page').textContent='';
  renderAdminSelection();adminPickerControls();setAdminPickerMode('members');managementNotice('','management-admin-notice');
}
function openAdmin(row){
  if(pageRole!=='master')return;
  clearAdminPicker();adminTeam=(row?.team || '').toUpperCase();adminTarget={team:adminTeam,admins:[...(row?.admins || [])]};adminExpectedKnown=!!row;
  $('management-admin-team').value=row?.team || '';$('management-admin-team').readOnly=!!row;
  for(const qq of adminTarget.admins)adminSelected.set(qq,'');renderAdminSelection();$('management-admin-dialog').showModal();loadAdminMembers();
}
document.querySelectorAll('[data-admin-mode]').forEach(button=>button.addEventListener('click',()=>setAdminPickerMode(button.dataset.adminMode)));
$('management-admin-team').addEventListener('input',()=>scheduleAdminMembers(true));
$('management-admin-team').addEventListener('change',()=>loadAdminMembers());
$('management-admin-team').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();loadAdminMembers();}});
$('management-admin-search').addEventListener('input',()=>scheduleAdminMembers());
$('management-admin-search-button').addEventListener('click',()=>{adminMemberOffset=0;loadAdminMembers();});
$('management-admin-search').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();adminMemberOffset=0;loadAdminMembers();}});
$('management-admin-manual-add').addEventListener('click',addManualAdmin);
$('management-admin-manual-qq').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();addManualAdmin();}});
$('management-admin-candidate-prev').addEventListener('click',()=>{if(adminMemberOffset){adminMemberOffset-=20;loadAdminMembers();}});
$('management-admin-candidate-next').addEventListener('click',()=>{if(adminMemberOffset+20<adminMemberTotal){adminMemberOffset+=20;loadAdminMembers();}});
$('management-admin-dialog').addEventListener('close',()=>{if(!$('management-admin-dialog').open)clearAdminPicker();});
$('management-admin-form').addEventListener('submit',async event=>{
  event.preventDefault();if(pageRole!=='master' || !adminTarget || !adminReady || adminSaving)return;
  if($('management-admin-manual-qq').value.trim() && !addManualAdmin())return;
  const epoch=managementEpoch,target=adminTarget;adminSaving=true;$('management-admin-form').inert=true;adminPickerControls();managementNotice('','management-admin-notice');
  const payload={team:adminTeam,qqs:Array.from(adminSelected.keys()),expected:[...target.admins]};
  try{const data=await bridge.apiPost('comparison/management/teams',payload);if(epoch!==managementEpoch || target!==adminTarget)return;$('management-admin-dialog').close();await loadManagement();managementNotice(data.message);}
  catch(error){if(epoch===managementEpoch && target===adminTarget)managementNotice(error.message,'management-admin-notice');}
  finally{if(target===adminTarget){adminSaving=false;$('management-admin-form').inert=false;adminPickerControls();}}
});
function openMember(row){
  if(pageRole!=='master')return;
  memberTarget=row;$('management-member-title').textContent=row.id?'编辑队员':'新增队员';$('management-member-team').textContent=`战队：${row.team}`;
  $('management-member-name').value=row.name || '';$('management-member-qq').value=row.qq_id || '';$('management-member-qq-hint').hidden=!row.id;
  managementNotice('','management-member-notice');$('management-member-dialog').showModal();
}
$('management-member-form').addEventListener('submit',async event=>{
  event.preventDefault();if(pageRole!=='master' || !memberTarget)return;
  const epoch=managementEpoch,target=memberTarget,button=event.submitter;button.disabled=true;managementNotice('','management-member-notice');
  const payload={team:target.team,action:target.id?'edit':'create',id:target.id,name:$('management-member-name').value.trim(),qq:$('management-member-qq').value.trim(),expected:{name:target.name,qq_id:target.qq_id || ''}};
  try{await bridge.apiPost('comparison/management/members',payload);if(epoch!==managementEpoch)return;$('management-member-dialog').close();await loadManagement();managementNotice('队员资料已保存喵。');}
  catch(error){if(epoch===managementEpoch)managementNotice(error.message,'management-member-notice');}finally{button.disabled=false;}
});
function openPlayers(row){
  if(pageRole!=='master')return;playerTarget={...row,players:[...row.players]};playerOffset=0;
  $('management-player-title').textContent=`${row.name} · 管理参赛ID`;$('management-player-keyword').value='';$('management-player-unbound').checked=false;
  $('management-player-owned').textContent=`已绑定：${row.players.join('、') || '暂无'}`;
  managementNotice('','management-player-notice');$('management-player-dialog').showModal();loadManagementPlayers();
}
async function loadManagementPlayers(){
  if(!playerTarget || pageRole!=='master' || !$('management-player-dialog').open)return;
  const version=++playerSequence,target=playerTarget;managementNotice('','management-player-notice');
  $('management-player-prev').disabled=true;$('management-player-next').disabled=true;$('management-player-records').inert=true;
  try{
    const data=await bridge.apiGet('comparison/management/players',{team:target.team,keyword:$('management-player-keyword').value.trim(),unbound:$('management-player-unbound').checked?'1':'0',offset:playerOffset,limit:30});
    if(version!==playerSequence || target!==playerTarget || !$('management-player-dialog').open)return;
    $('management-player-records').replaceChildren(...data.records.map(row=>{
      const card=node('article',null,'management-card'),body=node('div',null,'management-identity');
      body.append(node('strong',row.player_name),node('p',row.user_id?`归属：${row.user_name || row.user_id}${row.qq_id?' · '+row.qq_id:''}`:'未绑定','hint'));
      const mine=row.user_id===target.id,button=managementButton(mine?'解绑':row.user_id?'转绑':'绑定',()=>{
        if(!mine && row.user_id){confirmPlayer={row,target,version:managementEpoch};$('management-confirm-text').textContent=`将「${row.player_name}」从「${row.user_name || row.user_id}」转绑给「${target.name}」？`;$('management-confirm-dialog').showModal();}
        else changePlayer(row,target,button);
      });card.append(body,button);return card;
    }));
    if(!data.records.length)$('management-player-records').append(node('p','未找到参赛ID喵。','hint'));
    $('management-player-page-label').textContent=`共 ${data.total} 个 · 第 ${Math.floor(playerOffset/30)+1} / ${Math.max(1,Math.ceil(data.total/30))} 页`;
    $('management-player-prev').disabled=!playerOffset;$('management-player-next').disabled=playerOffset+30>=data.total;
  }catch(error){if(version===playerSequence){$('management-player-records').replaceChildren();managementNotice(error.message,'management-player-notice');}}
  finally{if(version===playerSequence)$('management-player-records').inert=false;}
}
async function changePlayer(row,target,button){
  if(pageRole!=='master' || playerTarget!==target)return;
  const epoch=managementEpoch;button.disabled=true;const mine=row.user_id===target.id;
  try{
    await bridge.apiPost('comparison/management/players',{team:target.team,player:row.player_name,action:mine?'unbind':'bind',id:target.id,expected_owner:row.user_id});
    if(epoch!==managementEpoch || playerTarget!==target)return;
    if(mine)target.players=target.players.filter(p=>p!==row.player_name);else if(!target.players.includes(row.player_name))target.players.push(row.player_name);
    $('management-player-owned').textContent=`已绑定：${target.players.join('、') || '暂无'}`;
    await loadManagementPlayers();await loadManagement();
  }catch(error){if(epoch===managementEpoch)managementNotice(error.message,'management-player-notice');}finally{button.disabled=false;}
}
$('management-player-search').addEventListener('submit',event=>{event.preventDefault();playerOffset=0;loadManagementPlayers();});
$('management-player-prev').addEventListener('click',()=>{if(playerOffset){playerOffset-=30;loadManagementPlayers();}});
$('management-player-next').addEventListener('click',()=>{playerOffset+=30;loadManagementPlayers();});
$('management-confirm-yes').addEventListener('click',async()=>{const pending=confirmPlayer;if(!pending || pending.version!==managementEpoch)return;$('management-confirm-dialog').close();await changePlayer(pending.row,pending.target,$('management-confirm-yes'));});
$('management-confirm-dialog').addEventListener('close',()=>{confirmPlayer=null;});
$('management-player-dialog').addEventListener('close',()=>{playerSequence++;playerTarget=null;$('management-player-records').replaceChildren();$('management-player-owned').textContent='';});
function clearManagement(){
  clearAdminPicker();
  managementEpoch++;managementSequence++;playerSequence++;pageRole='';managementOffset=0;managementTotal=0;adminTarget=null;memberTarget=null;playerTarget=null;confirmPlayer=null;
  document.querySelectorAll('[data-master-only]').forEach(el=>{el.hidden=true;});
  for(const id of ['management-admin-dialog','management-member-dialog','management-player-dialog','management-confirm-dialog'])if($(id).open)$(id).close();
  for(const id of ['management-records','management-player-records','management-teams'])$(id).replaceChildren();
  for(const id of ['management-team','management-keyword','management-admin-team','management-admin-search','management-admin-manual-qq','management-member-name','management-member-qq','management-player-keyword'])$(id).value='';
  for(const id of ['management-count','management-page-label','management-member-team','management-player-title','management-player-owned','management-player-page-label','management-confirm-text'])$(id).textContent='';
  $('management-view').hidden=true;
  for(const id of ['management-notice','management-admin-notice','management-member-notice','management-player-notice'])managementNotice('',id);
}

function clearSession(){
  credentialTransport=false;
  clearManagement();
  clearTimeout(timer);clearTimeout(dispatchTimer);sequence++;librarySequence++;dispatchSequence++;
  auditSequence++;auditDetailSequence++;auditData=undefined;auditOffset=0;auditLoading=false;auditDetailTarget=null;auditDetailLoading=false;clearAuditSummary();
  jobId='';lastResult=undefined;libraryData=undefined;libraryOffset=0;offset=0;dispatchId='';
  running=false;comparisonLoading=false;dispatchRunning=false;dbReady=false;selection.clear();
  for(const id of ['library-records','records','site-detail','local-detail','dispatch-preview','dispatch-results','accounts-list','audit-records','audit-detail-records'])$(id).replaceChildren();
  for(const id of ['detail','dispatch-dialog','accounts-dialog','audit-detail'])if($(id).open)$(id).close();
  clearAccountSecrets();
  for(const id of ['team','site-team','audit-team'])$(id).value='';
  $('audit-empty').hidden=false;$('audit-empty').querySelector('h3').textContent='等待检查';$('audit-empty').querySelector('p').textContent='选择战队与月份后开始检查。';$('audit-pagination').hidden=true;$('audit-only').checked=true;$('audit-start').disabled=true;
  $('accounts-open').hidden=true;$('download').disabled=true;selectionChanged();setRunning(false);syncSidePaging();
}
async function initializePage(){
  const data=await options(true);
  $('run-message').textContent=dbReady?'选择范围，开始一次核对。':'插件数据库尚未连接。';
  const active=data.history.find(j=>['pending','running'].includes(j.status));
  if(active){jobId=active.id;setRunning(true);await refresh();}
  else if(data.history.length)jobId=data.history[0].id;
  activateView('local');
}
window.addEventListener('battle-report-session-expired',clearSession);
window.addEventListener('battle-report-session-changed',()=>{clearSession();initializePage().catch(error=>notice(error.message));});

if (!bridge) {
  notice('请从 AstrBot 后台的插件详情页打开“战报核对”。'); $('run-message').textContent = '未连接 AstrBot 后台';
} else {
  try {
    await bridge.ready();
    await initializePage();
  } catch(e) {notice(e.message); $('run-message').textContent = '无法读取后台状态';}
}
