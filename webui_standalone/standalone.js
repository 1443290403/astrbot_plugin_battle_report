/* Standalone transport: same-origin AstrBot authentication, no dashboard iframe. */
(() => {
  'use strict';
  const API = '/api/plug/astrbot_plugin_battle_report/';
  let token = '', authenticated = false, readyResolve;
  const ready = new Promise(resolve => { readyResolve = resolve; });
  const loopback = ['localhost','127.0.0.1','[::1]'].includes(location.hostname);
  const secure = location.protocol === 'https:' || loopback;
  const dialog = document.createElement('dialog');
  dialog.id = 'standalone-login';
  dialog.setAttribute('aria-labelledby','standalone-login-title');
  dialog.innerHTML = '<form id="standalone-login-form"><p class="eyebrow">BATTLE REPORT</p><h2 id="standalone-login-title">登录战报管理</h2><p class="hint">使用现有 AstrBot 管理员账号。</p><label>用户名<input id="standalone-username" autocomplete="username" maxlength="200" required></label><label>密码<input id="standalone-password" type="password" autocomplete="current-password" required></label><label>动态验证码或恢复码<input id="standalone-code" autocomplete="one-time-code" placeholder="未启用两步验证时留空"></label><p id="standalone-login-error" class="notice" role="alert" hidden></p><button id="standalone-login-submit" type="submit">登录</button></form>';
  document.body.append(dialog);
  dialog.addEventListener('cancel', event => event.preventDefault());
  const bar = document.createElement('aside');
  bar.className = 'standalone-session';
  bar.innerHTML = '<span>独立战报管理</span><button id="standalone-logout" type="button" class="secondary" hidden>退出登录</button>';
  document.body.prepend(bar);
  const $ = id => document.getElementById(id);
  function showLogin(message='') {
    $('standalone-login-error').textContent = message;
    $('standalone-login-error').hidden = !message;
    if (!dialog.open) dialog.showModal();
  }
  function expire() {
    token='';authenticated=false;$('standalone-logout').hidden=true;
    showLogin('登录已失效，请重新登录后重试刚才的操作。');
  }
  async function fetchResponse(path, options={}) {
    const controller = new AbortController(), timeout = setTimeout(()=>controller.abort(),30000);
    try {
      return await fetch(path,{...options,credentials:'same-origin',cache:'no-store',signal:controller.signal,
        headers:{Accept:'application/json',...(token?{Authorization:`Bearer ${token}`} : {}),...options.headers}});
    } finally { clearTimeout(timeout); }
  }
  async function readEnvelope(response) {
    let result;
    try { result=await response.json(); }
    catch { throw new Error('接口没有返回有效JSON，请检查 Nginx 反向代理。'); }
    if (!response.ok || result?.status==='error') throw new Error(result?.message || `请求失败（${response.status}）`);
    return result?.data ?? result;
  }
  function endpointUrl(endpoint,params={}) {
    if(typeof endpoint!=='string' || !/^comparison\/[a-zA-Z0-9/-]+$/.test(endpoint))throw new Error('接口路径无效。');
    const url=new URL(API+endpoint,location.origin);
    for(const [key,value] of Object.entries(params || {}))if(value!==undefined && value!==null)url.searchParams.set(key,String(value));
    return url;
  }
  async function request(endpoint,{params,body,method='GET',download=false}={}) {
    if(!authenticated){showLogin();throw new Error('请先登录。');}
    const response=await fetchResponse(endpointUrl(endpoint,params),{method,
      ...(method==='POST'?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body || {})}:{})});
    if(response.status===401){expire();throw new Error('登录已失效，请重新登录后重试。');}
    // No request, especially a POST, is replayed automatically after reauthentication.
    if(download){if(!response.ok)return readEnvelope(response);return response.blob();}
    return readEnvelope(response);
  }
  window.AstrBotPluginPage={
    ready:()=>ready,
    apiGet:(endpoint,params)=>request(endpoint,{params}),
    apiPost:(endpoint,body)=>request(endpoint,{method:'POST',body}),
    async download(endpoint,params,filename){
      const blob=await request(endpoint,{params,download:true});
      const url=URL.createObjectURL(blob), link=document.createElement('a');
      link.href=url;link.download=filename || 'battle-report.json';document.body.append(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    },
  };
  async function verifySession() {
    return readEnvelope(await fetchResponse(endpointUrl('comparison/options')));
  }
  function loggedIn(){
    authenticated=true;$('standalone-password').value='';$('standalone-code').value='';
    $('standalone-logout').hidden=false;if(dialog.open)dialog.close();readyResolve();
  }
  $('standalone-login-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!secure)return;
    const submit=$('standalone-login-submit');submit.disabled=true;
    try {
      const response=await fetchResponse('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        username:$('standalone-username').value.trim(),password:$('standalone-password').value,
        code:$('standalone-code').value.trim() || undefined,trust_device_flag:false,
      })});
      const data=await readEnvelope(response);
      token=typeof data?.token==='string'?data.token:'';
      await verifySession();loggedIn();
    } catch(error) {token='';showLogin(error.name==='AbortError'?'连接超时，请检查 AstrBot 是否在线。':error.message);}
    finally {submit.disabled=false;}
  });
  $('standalone-logout').addEventListener('click',async()=>{
    $('standalone-logout').disabled=true;
    try {
      const response=await fetchResponse('/api/auth/logout',{method:'POST'});
      await readEnvelope(response);token='';authenticated=false;location.reload();
    } catch(error) {$('standalone-logout').disabled=false;showLogin(`退出失败：${error.message}`);}
  });
  const dark=window.matchMedia('(prefers-color-scheme: dark)');
  const theme=()=>document.documentElement.setAttribute('data-theme',dark.matches?'dark':'light');
  theme();dark.addEventListener('change',theme);
  if(!secure){showLogin('请使用 HTTPS，或通过 SSH 转发后访问 localhost。');for(const input of $('standalone-login-form').elements)input.disabled=true;return;}
  verifySession().then(loggedIn).catch(()=>showLogin());
})();
