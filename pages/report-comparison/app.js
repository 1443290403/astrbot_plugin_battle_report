const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;
let jobId = '', category = 'matched', offset = 0, timer, sequence = 0, dbReady = false, running = false;
let lastResult;
const labels = {pending:'准备中',running:'核对中',completed:'已完成',failed:'未完成',cancelled:'已取消',interrupted:'已中断'};
const rowNotes = {matched:'规范内容相同',missing_in_db:'本地未匹配',only_in_db:'本次网站范围未检出',
  db_duplicates:'本地多条相同内容',site_duplicates:'网站多条相同内容',parse_failed:'原文无法解析',
  out_of_range:'正文日期不在所选月份',other_team:'正文战队与搜索队名不符'};
const formatTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value)) : '';
const node = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = String(text); if (cls) e.className = cls; return e; };
function notice(text = '') { $('notice').textContent = text; $('notice').hidden = !text; }
function setRunning(value) { running = value; $('start').disabled = !dbReady || value; $('cancel').hidden = !value; $('history').disabled = value; }
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
  e.preventDefault(); if (running) return;
  notice(); $('start').disabled = true;
  try {
    const job = await bridge.apiPost('comparison/jobs', {team:$('team').value.trim(), site_team:$('site-team').value.trim(), month:$('month').value,
      source_url:$('source-url').value.trim(), publish_start:$('publish-start').value, publish_end:$('publish-end').value});
    category = 'matched'; offset = 0; jobId = job.id; setRunning(true); $('download').disabled = true;
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
      const td = node('td'); td.append(node('span', row[side].length ? row[side].map(r => `#${r.id}`).join('、') : '—', 'id-list'));
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
async function refresh() {
  clearTimeout(timer); if (!jobId) return;
  const selected = jobId, version = ++sequence;
  try {
    const data = await bridge.apiGet(`comparison/jobs/${selected}`,{category,offset,limit:30});
    if (version !== sequence || selected !== jobId) return;
    lastResult = data;
    const job = data.job, active = ['pending','running'].includes(job.status);
    $('run-status').textContent = labels[job.status] || job.status; $('run-message').textContent = job.message;
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
    const excluded = Object.values(summary.excluded || {}).reduce((a,b) => a + b, 0);
    if (excluded) notice(`已排除 ${excluded} 份踢馆等非友谊战报。`);
  } catch(e) {
    if (selected !== jobId || version !== sequence) return;
    notice(e.message); if (running) timer = setTimeout(refresh,3000);
  }
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
      const copy = node('button','复制原文','secondary');
      copy.addEventListener('click', async () => {try {await navigator.clipboard.writeText(r.body); copy.textContent = '已复制';} catch {copy.textContent = '请选择原文复制';}});
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
window.addEventListener('pagehide', () => clearTimeout(timer));
if (!bridge) {
  notice('请从 AstrBot 后台的插件详情页打开“战报核对”。'); $('run-message').textContent = '未连接 AstrBot 后台';
} else {
  try {
    await bridge.ready();
    const data = await options(true);
    $('run-message').textContent = dbReady ? '选择范围，开始一次核对。' : '插件数据库尚未连接。';
    const active = data.history.find(j => ['pending','running'].includes(j.status));
    if (active) {jobId = active.id; setRunning(true); await refresh();}
    else if (data.history.length) {jobId = data.history[0].id; await refresh();}
  } catch(e) {notice(e.message); $('run-message').textContent = '无法读取后台状态';}
}
