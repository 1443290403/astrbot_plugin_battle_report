const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;
let jobId = '', category = 'matched', offset = 0, timer, sequence = 0, dbReady = false, running = false;
let lastResult, comparisonLoading = false;
let view = 'local', libraryOffset = 0, libraryData, librarySequence = 0;
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
function copyButton(text) {
  const button = node('button','复制全文','secondary copy-report'); button.type='button';
  button.addEventListener('click',async()=>{
    try {
      // Keep the synchronous path in the original user click, including HTTP/sandbox.
      if(!legacyCopy(text)) {
        if(!navigator.clipboard?.writeText)throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(text);
      }
      button.textContent='已复制'; button.setAttribute('aria-label','已复制战报全文');
    } catch {
      button.textContent='复制失败'; button.setAttribute('aria-label','复制战报全文失败');
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
  if (initial) {
    $('month').value = data.month; monthDates();
    $('teams').replaceChildren(...data.teams.map(t => {const e = node('option'); e.value = t; return e;}));
    if (data.teams.length === 1) $('team').value = data.teams[0];
  }
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
  $('category-note').textContent = (job.categories || []).find(c => c.key === category)?.note || '';
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
  const comparison = view === 'comparison', data = comparison ? lastResult : libraryData;
  const ready = comparison ? !comparisonLoading && data?.job.status === 'completed' : !!data;
  const limit = data?.limit || 30, pages = Math.ceil((data?.total || 0) / limit);
  return {comparison, data, ready, limit, pages, current:Math.floor((data?.offset || 0) / limit) + 1};
}
function jumpToPage(page) {
  const state = pagingState();
  if (!state.ready || page < 1 || page > state.pages || page === state.current) return;
  if (state.comparison) {offset = (page-1)*state.limit; refresh();}
  else {libraryOffset = (page-1)*state.limit; loadLibrary();}
}
function syncPageNumbers(state) {
  const panel = $('page-numbers');
  panel.setAttribute('aria-busy', String(!state.ready));
  if (!state.ready) {for (const button of panel.children) button.disabled=true; return;}
  panel.hidden = !state.pages;
  const scope = state.comparison ? `${view}:${jobId}:${category}` : `${view}:${JSON.stringify(state.data.query)}:${$('local-kind').value}`;
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
    button.setAttribute('aria-controls',state.comparison?'records':'library-records');
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
  const state=pagingState(), {comparison,ready}=state, prefix = comparison ? '' : 'library-';
  for (const direction of ['prev','next']) {
    const button = $(`side-${direction}`);
    button.disabled = !ready || $(`${prefix}pagination`).hidden || $(`${prefix}${direction}`).disabled;
    button.setAttribute('aria-controls', comparison ? 'records' : 'library-records');
  }
  syncPageNumbers(state);
}
for (const direction of ['prev','next']) $('side-'+direction).addEventListener('click',()=>{
  // Reuse the current tab's existing pagination action and boundaries.
  $(`${view === 'comparison' ? '' : 'library-'}${direction}`).click();
});
async function refresh() {
  clearTimeout(timer); if (!jobId) return;
  const selected = jobId, version = ++sequence;
  comparisonLoading = true; syncSidePaging();
  try {
    const data = await bridge.apiGet(`comparison/jobs/${selected}`,{category,offset,limit:30});
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
function activateView(next) {
  view=next; libraryOffset=0; librarySequence++;
  document.querySelectorAll('#view-tabs button').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-pressed',String(b.dataset.view===view));});
  $('comparison-view').hidden=view!=='comparison'; $('library-view').hidden=view==='comparison';
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
  if (view==='comparison') return;
  const selectedView=view, version=++librarySequence;
  $('start').disabled=true; notice();libraryData=null;$('library-records').replaceChildren();$('library-pagination').hidden=true; syncSidePaging();
  $('library-empty').hidden=false;$('library-empty').querySelector('h3').textContent='正在读取战报';$('library-empty').querySelector('p').textContent='';
  try {
    const data=await bridge.apiGet(`comparison/${view==='local'?'local-reports':'cloud-reports'}`,{
      team:$('team').value.trim(),site_team:view==='cloud'?$('site-team').value.trim():'',month:$('month').value,
      kind:$('local-kind').value,offset:libraryOffset,limit:30});
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
  } catch(e) {if(version===librarySequence){notice(e.message);$('library-empty').querySelector('h3').textContent='未能读取战报';$('library-empty').querySelector('p').textContent='请检查范围或连接后重新刷新。';}} finally {if(version===librarySequence){setRunning(running);syncSidePaging();}}
}
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

if (!bridge) {
  notice('请从 AstrBot 后台的插件详情页打开“战报核对”。'); $('run-message').textContent = '未连接 AstrBot 后台';
} else {
  try {
    await bridge.ready();
    const data = await options(true);
    $('run-message').textContent = dbReady ? '选择范围，开始一次核对。' : '插件数据库尚未连接。';
    const active = data.history.find(j => ['pending','running'].includes(j.status));
    if (active) {jobId = active.id; setRunning(true); await refresh();}
    else if (data.history.length) jobId = data.history[0].id;
    activateView('local');
  } catch(e) {notice(e.message); $('run-message').textContent = '无法读取后台状态';}
}
