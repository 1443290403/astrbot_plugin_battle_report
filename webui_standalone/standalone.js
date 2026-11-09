/* Same-origin transport with separate team sessions and AstrBot admin login. */
(() => {
  'use strict';
  const API='/api/plug/astrbot_plugin_battle_report/', TEAM='/battle-report/';
  let token='',authenticated=false,sessionMode='master',everLoggedIn=false,readyResolve,authEpoch=0;
  const ready=new Promise(resolve=>{readyResolve=resolve;});
  const loopback=['localhost','127.0.0.1','[::1]'].includes(location.hostname);
  const builtin=document.querySelector('meta[name="battle-report-entry"]')?.content==='builtin';
  const secure=builtin || location.protocol==='https:' || loopback;
  const dialog=document.createElement('dialog');
  dialog.id='standalone-login';dialog.setAttribute('aria-labelledby','standalone-login-title');
  dialog.innerHTML='<form id="standalone-login-form"><p class="eyebrow">BATTLE REPORT</p><h2 id="standalone-login-title">登录战报管理</h2><label>登录方式<select id="standalone-login-mode"><option value="master">AstrBot 管理员</option><option value="password">战队管理员账号密码</option><option value="code">QQ 登录码</option></select></label><p id="standalone-login-hint" class="hint"></p><label id="standalone-username-label">用户名<input id="standalone-username" autocomplete="username" maxlength="200" required></label><label id="standalone-password-label">密码<input id="standalone-password" type="password" autocomplete="current-password" required></label><label id="standalone-totp-label">动态验证码或恢复码<input id="standalone-code" autocomplete="one-time-code" placeholder="未启用两步验证时留空"></label><label id="standalone-login-code-label" hidden>一次性登录码<input id="standalone-login-code" autocomplete="one-time-code" maxlength="1024"></label><p id="standalone-login-error" class="notice" role="alert" hidden></p><button id="standalone-login-submit" type="submit">登录</button></form>';
  document.body.append(dialog);dialog.addEventListener('cancel',event=>event.preventDefault());
  const bar=document.createElement('aside');bar.className='standalone-session';
  bar.innerHTML='<span id="standalone-session-label">独立战报管理</span><button id="standalone-password-open" type="button" class="secondary" hidden>修改密码</button><button id="standalone-logout" type="button" class="secondary" hidden>退出登录</button>';
  document.body.prepend(bar);
  const passwordDialog=document.createElement('dialog');passwordDialog.id='standalone-password-dialog';
  passwordDialog.innerHTML='<form id="standalone-password-form"><div class="dialog-top"><h2>修改登录密码</h2><button id="standalone-password-close" type="button" class="secondary">关闭 ×</button></div><label>原密码<input id="standalone-old-password" type="password" autocomplete="current-password" required maxlength="256"></label><label>新密码<input id="standalone-new-password" type="password" autocomplete="new-password" required minlength="8" maxlength="256"></label><p id="standalone-password-error" class="notice" hidden></p><button type="submit">保存新密码</button></form>';
  document.body.append(passwordDialog);
  const $=id=>document.getElementById(id);
  function loginFields(){
    const mode=$('standalone-login-mode').value;
    $('standalone-username-label').hidden=mode==='code';$('standalone-password-label').hidden=mode==='code';
    $('standalone-username').required=mode!=='code';$('standalone-password').required=mode!=='code';
    $('standalone-totp-label').hidden=mode!=='master';$('standalone-login-code-label').hidden=mode!=='code';
    $('standalone-login-code').required=mode==='code';
    $('standalone-login-hint').textContent=mode==='master'?'使用现有 AstrBot 后台管理员账号。':mode==='password'?'账号默认是管理员QQ号，私聊机器人发送 /战报登录 获取初始密码。':'先私聊 SnowLuma／OneBot 机器人发送 /战报登录，再粘贴5分钟内有效的一次性登录码。';
  }
  $('standalone-login-mode').addEventListener('change',loginFields);loginFields();
  function showLogin(message=''){
    $('standalone-login-error').textContent=message;$('standalone-login-error').hidden=!message;
    if(!dialog.open)dialog.showModal();
  }
  function expire(){
    authEpoch++;
    if(passwordDialog.open)passwordDialog.close();
    token='';authenticated=false;$('standalone-logout').hidden=true;$('standalone-password-open').hidden=true;
    window.dispatchEvent(new Event('battle-report-session-expired'));
    showLogin('登录已失效，请重新登录后重试刚才的操作。');
  }
  async function fetchResponse(path,options={}){
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),30000);
    try{return await fetch(path,{...options,credentials:'same-origin',cache:'no-store',signal:controller.signal,
      headers:{Accept:'application/json',...(token?{Authorization:`Bearer ${token}`} : {}),...options.headers}});}
    finally{clearTimeout(timeout);}
  }
  async function readEnvelope(response){
    let result;
    try{result=await response.json();}catch{throw new Error('接口没有返回有效JSON，请检查访问地址或反向代理。');}
    if(!response.ok || result?.status==='error')throw new Error(result?.message || `请求失败（${response.status}）`);
    return result?.data ?? result;
  }
  function endpointUrl(endpoint,params={}){
    if(typeof endpoint!=='string' || !/^comparison\/[a-zA-Z0-9/-]+$/.test(endpoint))throw new Error('接口路径无效。');
    const url=new URL((sessionMode==='team'?TEAM+'api/':API)+endpoint,location.origin);
    for(const [key,value] of Object.entries(params || {}))if(value!==undefined && value!==null)url.searchParams.set(key,String(value));
    return url;
  }
  async function request(endpoint,{params,body,method='GET',download=false}={}){
    if(!authenticated){showLogin();throw new Error('请先登录。');}
    const epoch=authEpoch;
    const response=await fetchResponse(endpointUrl(endpoint,params),{method,
      ...(method==='POST'?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body || {})}:{})});
    if(epoch!==authEpoch)throw new Error('登录状态已变化，请重试。');
    if(response.status===401){expire();throw new Error('登录已失效，请重新登录后重试。');}
    // Never replay POSTs after authentication, including dispatch requests.
    const data=download && response.ok?await response.blob():await readEnvelope(response);
    if(epoch!==authEpoch)throw new Error('登录状态已变化，请重试。');
    return data;
  }
  window.AstrBotPluginPage={
    ready:()=>ready,apiGet:(endpoint,params)=>request(endpoint,{params}),
    apiPost:(endpoint,body)=>request(endpoint,{method:'POST',body}),
    async download(endpoint,params,filename){
      const blob=await request(endpoint,{params,download:true});
      const url=URL.createObjectURL(blob),link=document.createElement('a');
      link.href=url;link.download=filename || 'battle-report.json';document.body.append(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    },
  };
  async function verifySession(){return readEnvelope(await fetchResponse(endpointUrl('comparison/options')));}
  function loggedIn(actor={role:'master'}){
    authEpoch++;
    authenticated=true;
    for(const id of ['standalone-password','standalone-code','standalone-login-code'])$(id).value='';
    $('standalone-logout').hidden=false;$('standalone-password-open').hidden=!actor.password_login;
    $('standalone-session-label').textContent=actor.role==='team'?`${actor.username} · ${actor.teams.join('、')}`:'AstrBot 管理员';
    if(dialog.open)dialog.close();
    if(location.search)new URLSearchParams(location.search).has('login') && history.replaceState(null,'',location.pathname);
    readyResolve();
    if(everLoggedIn)window.dispatchEvent(new Event('battle-report-session-changed'));
    everLoggedIn=true;
  }
  $('standalone-login-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!secure)return;
    const submit=$('standalone-login-submit');submit.disabled=true;
    try{
      const mode=$('standalone-login-mode').value;
      sessionMode=mode==='master'?'master':'team';token='';
      const path=mode==='master'?'/api/auth/login':TEAM+'auth/'+(mode==='code'?'code-login':'login');
      const pasted=$('standalone-login-code').value.trim();
      const body=mode==='code'?{code:pasted.match(/brc_[A-Za-z0-9_-]{32}/)?.[0] || pasted}:{username:$('standalone-username').value.trim(),password:$('standalone-password').value,...(mode==='master'?{code:$('standalone-code').value.trim() || undefined,trust_device_flag:false}:{})};
      const data=await readEnvelope(await fetchResponse(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}));
      token=typeof data?.token==='string'?data.token:'';
      await verifySession();loggedIn(data.actor || {role:'master'});
    }catch(error){token='';showLogin(error.name==='AbortError'?'连接超时，请检查 AstrBot 是否在线。':error.message);}
    finally{submit.disabled=false;}
  });
  $('standalone-logout').addEventListener('click',async()=>{
    $('standalone-logout').disabled=true;
    try{
      await readEnvelope(await fetchResponse(sessionMode==='team'?TEAM+'auth/logout':'/api/auth/logout',{method:'POST'}));
      token='';authenticated=false;location.replace(location.pathname+'?login=1');
    }catch(error){$('standalone-logout').disabled=false;showLogin(`退出失败：${error.message}`);}
  });
  $('standalone-password-open').addEventListener('click',()=>{passwordDialog.showModal();});
  $('standalone-password-close').addEventListener('click',()=>passwordDialog.close());
  $('standalone-password-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try{
      const response=await fetchResponse(TEAM+'auth/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_password:$('standalone-old-password').value,new_password:$('standalone-new-password').value})});
      if(response.status===401){passwordDialog.close();expire();throw new Error('登录已失效，请重新登录。');}
      const data=await readEnvelope(response);
      token=data.token;authEpoch++;$('standalone-old-password').value='';$('standalone-new-password').value='';
      $('standalone-password-error').hidden=true;passwordDialog.close();
    }catch(error){$('standalone-password-error').textContent=error.message;$('standalone-password-error').hidden=false;}
    finally{button.disabled=false;}
  });
  passwordDialog.addEventListener('close',()=>{$('standalone-old-password').value='';$('standalone-new-password').value='';});
  const dark=window.matchMedia('(prefers-color-scheme: dark)');
  const theme=()=>document.documentElement.setAttribute('data-theme',dark.matches?'dark':'light');theme();dark.addEventListener('change',theme);
  if(!secure){showLogin('请使用 HTTPS，或通过 SSH 转发后访问 localhost。');for(const input of $('standalone-login-form').elements)input.disabled=true;return;}
  (async()=>{
    if(new URLSearchParams(location.search).has('login')){showLogin();return;}
    try{
      const actor=await readEnvelope(await fetchResponse(TEAM+'auth/session'));
      sessionMode='team';await verifySession();loggedIn(actor);return;
    }catch{sessionMode='master';token='';}
    try{await verifySession();loggedIn();}catch{showLogin();}
  })();
})();
