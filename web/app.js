'use strict';
// Only touch the DOM when markup changes, so card glow/sheen animations are not restarted every poll.
function setHTML(el,html){if(el._html!==html){el._html=html;el.innerHTML=html;}}
const $ = id => document.getElementById(id);
let snapshot = null, csrf = '', connected = false, busy = false, loading = false, remoteCtx = {local: true, full: true};
// Set when the user presses refresh; background polls never touch the button.
let manualRefresh = null, manualRefreshAt = 0;
let switchTarget = null, codexTarget = null, claudeTarget = null;
// Which account family the home screen shows; a per-viewer convenience only.
let provider = (() => {try {const p=localStorage.getItem('agy.provider');return p === 'codex' || p === 'claude' ? p : 'gemini';} catch {return 'gemini';}})();
let draft = null, draftRevision = null, dirty = false;
const clone = value => JSON.parse(JSON.stringify(value));
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = n => typeof n === 'number' && Number.isFinite(n) ? n.toLocaleString('zh-HK', {maximumFractionDigits:2}) + '%' : '未知';
const date = value => {if (!value) return '—'; const d = new Date(typeof value === 'number' ? value * 1000 : value);return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-HK', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});};
const errors = {AGY_CONVERSATION_PRUNED_NOT_SWITCHED:'未轉：有個開咗好耐嘅 agy 仲開住，佢嘅對話已經俾 agy 清理咗（只保留最新約 500 個），而家關咗佢個對話就會永久冇咗。請自己喺嗰個 agy 打 /quit 或者保存好要嘅內容，再轉一次。',AGY_WORKING_NOT_SWITCHED:'未轉：有個 agy 做緊嘢。等佢做完，或者揀「等閒置先轉」。',AGY_UNREADABLE_NOT_SWITCHED:'未轉：有個 agy 開住，但讀唔到佢嘅資料，冇辦法安全關閉。請自己喺嗰個 agy 打 /quit，再轉一次。',REMOTE_OFF:'手機遙控未開。',INVALID_PUBLIC_ADDRESS:'公開網址唔啱，例如 abc.ngrok-free.app。',TOO_MANY_ATTEMPTS:'試得太多次，請一分鐘後再試。',NOT_ALLOWED_REMOTELY:'手機冇權做呢樣。要喺 Mac 嘅設定 →「手機遙控」開「已配對手機可以加帳號、改設定」。',PAIRING_REQUIRED:'呢部裝置未配對或者已經被撤銷，請喺 Mac 重新配對。',CLAUDE_LIVE_NOT_ENROLLED:'而家 Claude Code 登入緊嘅帳號未登記，請先加入，費事轉走之後冇咗佢。',CLAUDE_ACTIVATION_ROLLED_BACK:'轉換失敗，已還原返原本嘅 Claude 帳號。',CLAUDE_ROLLBACK_FAILED_CHECK_CURRENT:'轉換失敗，而且未能確認還原，請檢查 Claude Code 登入。',AGY_NO_SIGNIN_URL:'agy 冇顯示 Google 登入連結（可能 agy 更新咗畫面），已還原原本帳號。請話我知。',AGY_LOGIN_SCREEN_CHANGED:'agy 登入畫面同預期唔同（可能 agy 更新咗），已還原原本帳號。請話我知。','Load failed':'連線中斷咗，請檢查網絡再試一次。','Failed to fetch':'連線中斷咗，請檢查網絡再試一次。',ALREADY_ENROLLED:'呢個帳號已經加咗，請用另一個帳號登入。',AGY_BUSY_TRY_LATER:'agy／Antigravity 做緊嘢，等佢做完先加帳號。',SIGN_IN_NOT_COMPLETED:'登入未完成：授權碼錯咗或者過咗期，請取消後重新開始。',INVALID_AUTH_CODE:'呢個唔似授權碼，請成段複製再貼。',ENROLLMENT_IN_PROGRESS:'已經有一個加帳號流程進行中，請完成或者取消佢。',INVALID_LABEL:'帳號名稱格式唔啱。',ADD_START_FAILED:'開始加帳號失敗，原本帳號冇改動。',ADD_FINISH_FAILED:'加帳號失敗，已轉返原本帳號。',LIVE_NOT_ENROLLED:'目前登入緊嘅帳號未加入管理，唔可以加新帳號。',CLOSE_TIMEOUT_NOT_SWITCHED:'限時內未能關閉所有程式（例如自己開住嘅 agy／Codex 對話），未有轉帳號；原本開住嘅 app 已重開。',CLOSE_TIMEOUT_NOT_ADDED:'關唔到 agy／Antigravity（例如你喺 terminal 自己開住嘅 agy 對話），未有開始加帳號，乜都冇改。請先閂咗佢再試。',CODEX_LIVE_NOT_ENROLLED:'目前嘅 Codex 登入未加入管理。請先喺 Mac 行 codex-account enroll LABEL。',CODEX_PROFILE_BROKEN:'呢個 Codex 帳號嘅登入資料損壞，請用私人瀏覽視窗重新登入。',CODEX_ACTIVATION_ROLLED_BACK:'Codex 帳號轉換失敗，已還原原本帳號。',CODEX_ROLLBACK_FAILED_CHECK_CURRENT:'Codex 帳號轉換失敗而且未能還原，請喺 Mac 行 codex-account current 檢查。',CODEX_SWITCH_FAILED:'Codex 帳號轉換失敗，原本帳號未有改動。',CODEX_TIMEOUT:'Codex 帳號轉換超時，請檢查目前帳號。',CONFIG_CONFLICT_RELOAD_REQUIRED:'其他裝置已修改設定。請放棄草稿、讀取最新設定後再儲存。',SERVICE_UNAVAILABLE:'暫時連唔到本機服務。畫面保留上次資料，操作已停用。',CSRF_REQUIRED:'服務已重新啟動。已重新連線，請再試一次。',SELECT_AT_LEAST_ONE_KNOWN_PROFILE:'請至少選擇一個帳號。',SWITCH_BUSY:'已有帳號轉換進行中，請等完成。',ACCOUNT_EXHAUSTED:'呢個帳號已用盡額度，等 reset 後先可以轉。',LIVE_IDENTITY_UNKNOWN:'目前帳號身份未能確認，請先喺 Mac 檢查登入。'};

// Floating notice; success messages fade after 4 s, errors stay until the next action.
function announce(text, error = false) {const dlg=error&&document.querySelector('dialog[open]');if(dlg){let p=dlg.querySelector('.dialog-error');if(!p){p=document.createElement('p');p.className='dialog-error';p.setAttribute('role','alert');(dlg.firstElementChild||dlg).append(p);dlg.addEventListener('close',()=>p.remove(),{once:true});}p.textContent=text;p.hidden=!text;} // a modal dialog covers the floating notice
  const el=$(error?'failure':'message');el.textContent=text;el.hidden=!text;const key=error?'failureTimer':'messageTimer';clearTimeout(announce[key]);if(text)announce[key]=setTimeout(()=>{el.hidden=true;},error?8000:4000);}
async function request(url, options={}) {const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(100000),...options});let data;try{data=await response.json();}catch{throw new Error('SERVICE_UNAVAILABLE');}if(!response.ok)throw new Error(data.error||'ACTION_FAILED');return data;}
async function load() {if(loading || busy)return;loading=true;renderControls();try{const data=await request('/agy/api/status');snapshot=data.status;csrf=data.csrf;remoteCtx=data.remote||remoteCtx;$('remoteOpen').hidden=!remoteCtx.local;connected=true;if(!dirty){draft=clone(snapshot.config);draftRevision=snapshot.revision;fillForm();}render();}catch(e){if(e.message==='PAIRING_REQUIRED'){location.replace('/agy/pair');return;}connected=false;if(snapshot)render();else{$('connection').textContent='連線中斷';$('summary').textContent='未能連接本機服務，正在重試。';}renderControls();}finally{loading=false;renderControls();}}
async function action(method, params={}) {if(busy||!connected)return false;busy=true;renderControls();announce('');announce('',true);try{await request('/agy/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-AGY-CSRF':csrf},body:JSON.stringify({method,params})});return true;}catch(e){announce(errors[e.message]||`操作未完成：${e.message}`,true);return false;}finally{busy=false;await load();renderControls();}}
function quotaMarkup(window, title, stale) {const n=window?.remaining_percent;const valid=typeof n==='number'&&Number.isFinite(n)&&n>=0&&n<=100;const tone=!valid?'unknown':n>70?'green':n>=30?'orange':'red';return `<div class="quota" data-tone="${tone}"><div class="quota-head"><span>${title}</span><strong>${valid?pct(n):'—'}</strong></div><div class="battery" role="meter" aria-label="${title}剩餘${valid?pct(n):'未知'}" ${valid?`aria-valuemin="0" aria-valuemax="100" aria-valuenow="${n}"`:''}><i></i></div><small class="reset-time">重設 ${esc(date(window?.reset_at))}</small>${stale?'<small>資料待更新</small>':''}</div>`;}
function render() {if(!snapshot)return;const s=snapshot;$('connection').textContent=connected?'已連接':'連線中斷';$('connection').title=connected?'已連接本機服務':'連線中斷';$('connection').classList.toggle('online',connected);$('summary').textContent=connected?(((n,name,active)=>n?`目前使用 ${active||'身份未確認'} · ${n} 個${name}帳號 · 每 ${s.config.poll_seconds} 秒查詢用量`:`未有${name}帳號 · 撳「＋ 加帳號」開始`)(...(provider==='claude'?[Object.keys(s.claude?.profiles||{}).length,' Claude ',s.claude?.active]:provider==='codex'?[Object.keys(s.codex?.profiles||{}).length,' Codex ',s.codex?.active]:[Object.keys(s.profiles).length,' Antigravity ',s.active]))):'以下係上次讀取資料，暫時未能確認最新用量。';$('updated').textContent=`更新於 ${date(s.last_refresh_finished||Object.values(s.profiles).map(r=>r.updated_at).filter(Boolean).sort().at(-1))}`;
$('automationHint').textContent=s.recovery_required?'上次轉換結果未確認，自動輪轉已暫停':s.paused_until>Date.now()/1000?`暫停至 ${date(s.paused_until)}；quota 查詢照常`:s.config.enabled?`${s.config.mode==='notify'?'只通知':'自動轉帳號'}${s.next_scheduled_at?' · 下次 '+date(s.next_scheduled_at):''}`:'已關閉自動輪轉，quota 查詢照常';
setHTML($('accounts'),Object.entries(s.profiles).map(([label,r])=>{const g=r.groups?.find(g=>g.family==='gemini');const active=label===s.active;const stale=!connected||r.status!=='OK';const used=!active&&!stale&&geminiSpent(s,label);const cool=!active&&!stale&&!used&&geminiCooling(s,label);return `<article class="card ${active?'active':''} ${used?'exhausted':''}"><div class="card-top"><span class="account-identity"><span class="account-name">${esc(r.email||label)}</span><small class="account-alias">${esc(label)}${r.email?'':' · Email 未提供'}</small></span><span class="card-flags">${active&&!stale?activityChip(s.activity?.gemini):`<span class="badge ${stale?'bad':used?'spent':cool?'cooling':''}">${stale?'資料待更新':used||(cool?`5 小時已用盡 · ${cool}`:label===s.recommended?'建議候選':'可用')}</span>`}</span></div>${quotaMarkup(g?.windows?.['5h'],'5h',stale)}${quotaMarkup(g?.windows?.weekly,'每週',stale)}<div class="card-bottom"><button data-use="${esc(label)}" class="${active?'in-use':'secondary'}" ${active?'disabled':''}>${active?'使用中':'⇄ 切換'}</button><div class="freshness">${esc(r.status==='OK'?'最近查詢 '+date(r.updated_at):'查詢狀態 '+r.status+' · '+(r.error||'等候更新'))}</div></div></article>`;}).join('')||emptyCard('Antigravity'));
renderCodex(s);renderClaude(s);detectSwitch(s);renderAdd();renderManage();
{const n=Object.keys(s.profiles).length||3;$('accounts').style.setProperty('--rows',n);$('accounts').classList.toggle('dense',n>3);requestAnimationFrame(fitCards);}
document.querySelectorAll('.battery').forEach(bar=>{bar.firstElementChild.style.width=(Number(bar.getAttribute('aria-valuenow'))||0)+'%';});
$('resetDetails').innerHTML='<h2>配額重設時間</h2>'+Object.entries(s.profiles).map(([label,r])=>{const w=r.groups?.find(g=>g.family==='gemini')?.windows;return `<p>${esc(label)}<br>5h：${esc(date(w?.['5h']?.reset_at))}<br>每週：${esc(date(w?.weekly?.reset_at))}<br>查詢：${esc(date(r.updated_at))} · ${esc(r.status)}</p>`;}).join('');
$('other').innerHTML=Object.entries(s.profiles).map(([label,r])=>{const g=r.groups?.find(g=>g.family==='claude_gpt');return `<div>${esc(label)} · 5 小時剩餘 ${pct(g?.windows?.['5h']?.remaining_percent)} · 每週剩餘 ${pct(g?.windows?.weekly?.remaining_percent)}${r.status!=='OK'?'（資料待更新）':''}</div>`;}).join('');
const names={claude_switched:'Claude 帳號轉換完成',claude_switch_failed:'Claude 帳號轉換失敗',claude_warmed:'已喚醒 Claude 帳號（開始 5 小時倒數）',claude_warm_failed:'喚醒 Claude 帳號失敗',claude_warmup_changed:'Claude 自動喚醒設定已更新',claude_account_removed:'已移除 Claude 帳號',agy_closed:'轉帳號時關咗閒置嘅 agy（用通知入面嘅指令接返對話）',account_removed:'已移除帳號',account_added:'已加入帳號',park_countdown_started:'每週低過 15%，倒數後收起做後備',manual_dropped:'目標帳號已用盡，取消排隊轉換',warmed:'已喚醒帳號（開始 5 小時倒數）',warm_failed:'喚醒帳號失敗',warmup_changed:'自動喚醒設定已更新',codex_switched:'Codex 帳號轉換完成',codex_auto_changed:'Codex 用盡自動轉設定已更新',codex_all_exhausted:'Codex 所有帳號都已用盡',codex_switch_failed:'Codex 帳號轉換失敗',config_changed:'設定已更新',switched:'帳號轉換完成',switch_started:'正在轉換帳號',switch_failed:'帳號轉換失敗',drift:'偵測到目前帳號改變',paused:'已暫停自動輪轉',resumed:'已恢復輪轉排程',rotation_cancelled:'已取消今次輪轉',rotation_recommended:'建議輪轉（只通知）',countdown_started:'用量耗盡，開始轉換倒數',recovery_acknowledged:'已確認目前帳號',policy_error:'輪轉規則執行失敗'};
$('events').innerHTML=[...(s.gemini_events||s.events.filter(e=>!e.kind.startsWith('codex_')))].reverse().map(e=>`<li><span>${esc(names[e.kind]||e.kind)}${e.selected?' · '+esc(e.selected):''}${e.error?' · '+esc(e.error):''}</span><time>${date(e.time)}</time></li>`).join('')||'<li>暫時未有輪轉活動</li>';
const p=s.pending;const states={WAITING_FOR_IDLE:'等候 agy 工作完成',COUNTDOWN:'即將關閉 agy 並轉帳號',NO_READY_PROFILE:'暫時冇可用嘅候選帳號',ALL_EXHAUSTED:'參與帳號嘅容量已用盡',NOTIFY_ONLY:'已觸發輪轉條件（只通知）',READY:'準備轉帳號'};
$('pending').hidden=!p&&!s.recovery_required&&!s.config_error;
$('pending').innerHTML=s.recovery_required?'上次轉換結果未確認。請確認目前帳號後，先恢復自動輪轉。 <button data-recovery>確認目前帳號</button>':s.config_error?`設定儲存異常：${esc(s.config_error)}`:p?`${esc(states[p.state]||p.state)}${p.target?' → '+esc(p.target):''}${p.reason==='manual'?'（手動排隊）':p.reason==='urgent'?'（6 小時內重設，優先用）':''}${p.deadline?' · '+Math.max(0,Math.ceil(p.deadline-Date.now()/1000))+' 秒':''} <button data-cancel class="secondary">取消今次</button>`:'';
renderControls();}
function renderControls() {const blocked=!connected||busy||loading;const s=snapshot;$('codexAutoSwitch').disabled=!connected||busy||!s?.codex;$('codexAutoSwitch').setAttribute('aria-checked',String(!!s?.codex?.auto));$('codexAutoContinue').disabled=!connected||busy||!s?.codex;$('codexAutoContinue').setAttribute('aria-checked',String(!!s?.codex?.autocontinue));for(const [id,p] of [['geminiWarmup','gemini'],['codexWarmup','codex'],['claudeWarmup','claude']]){$(id).disabled=!connected||busy||!s?.warmup;$(id).setAttribute('aria-checked',String(!!s?.warmup?.[p]));}document.querySelectorAll('[data-codex-use]').forEach(b=>{const label=b.dataset.codexUse,active=label===s?.codex?.active,pending=label===codexTarget,used=!active&&s?.codex?.profiles?.[label]?.status==='OK'&&codexSpent(s.codex.profiles[label]),cool=!active&&!used&&s?.codex?.profiles?.[label]?.status==='OK'&&codexCooling(s.codex.profiles[label]);b.disabled=!connected||busy||!!s?.codex?.switching||active;b.classList.toggle('in-use',active&&!pending);b.classList.toggle('switching',pending);b.textContent=pending?'切換中':active?'使用中':'⇄ 切換';});$('master').disabled=blocked||!s;$('master').setAttribute('aria-checked',String(!!s?.config.enabled));$('master').textContent='';$('master').setAttribute('aria-label','自動輪轉');if(manualRefresh!==null&&s&&((!s.refreshing&&s.last_refresh_finished!==manualRefresh)||Date.now()-manualRefreshAt>90000))manualRefresh=null;const updating=manualRefresh!==null;$('refresh').disabled=!connected||updating;$('refresh').classList.toggle('updating',updating);$('refresh').querySelector('span').textContent=updating?'更新中':'更新用量';$('pause').disabled=blocked||!s?.config.enabled;$('pause').textContent=s?.paused_until>Date.now()/1000?'恢復輪轉':'暫停 30 分鐘';document.querySelectorAll('[data-claude-use]').forEach(b=>{const label=b.dataset.claudeUse,active=label===s?.claude?.active,pending=label===claudeTarget;b.disabled=!connected||busy||!!s?.claude?.switching||active;b.classList.toggle('in-use',active&&!pending);b.classList.toggle('switching',pending);b.textContent=pending?'切換中':active?'使用中':'⇄ 切換';});document.querySelectorAll('[data-use]').forEach(b=>{const active=b.dataset.use===s?.active,used=!active&&s?.profiles?.[b.dataset.use]?.status==='OK'&&geminiSpent(s,b.dataset.use),cool=!active&&!used&&s?.profiles?.[b.dataset.use]?.status==='OK'&&geminiCooling(s,b.dataset.use);b.disabled=!connected||busy||s?.switching||active;b.classList.toggle('in-use',active);const pending=b.dataset.use===switchTarget; b.classList.toggle('switching',pending);b.setAttribute('aria-busy',String(pending));b.textContent=pending?'切換中':active?'使用中':'⇄ 切換';});const conflict=dirty&&draftRevision!==s?.revision;$('conflict').hidden=!conflict;$('save').disabled=blocked||!dirty||conflict;$('defaults').disabled=blocked;$('reloadDraft').disabled=blocked||!dirty;$('dirtyDot').hidden=!dirty;$('draftStatus').textContent=conflict?'設定有衝突':dirty?'有未儲存修改':'設定已同步';document.querySelectorAll('[data-cancel],[data-recovery]').forEach(b=>b.disabled=blocked||s?.switching);}
const booleans=['time_enabled','usage_enabled','manual_confirm','close_app','notify'];const numbers=['remaining_below','poll_seconds','cooldown_seconds','countdown_seconds'];
function fillForm(){if(!draft)return;for(const key of booleans)$(key).checked=draft[key];for(const key of [...numbers,'family','mode'])$(key).value=draft[key];$('minutes').value=draft.hourly_minutes.join(',');$('five').checked=draft.windows.includes('5h');$('weekly').checked=draft.windows.includes('weekly');$('participants').innerHTML=Object.keys(snapshot.profiles).map(label=>`<label class="check"><input type="checkbox" data-participant="${esc(label)}" ${draft.participants.includes(label)?'checked':''}>${esc(label)}</label>`).join('');}
function readForm(){const d=clone(draft);for(const k of booleans)d[k]=$(k).checked;for(const k of numbers)d[k]=Number($(k).value);for(const k of ['family','mode'])d[k]=$(k).value;const raw=$('minutes').value.split(/[,，]/).map(v=>v.trim());if(raw.some(v=>!/^\d{1,2}$/.test(v)))throw new Error('分鐘請輸入 0–59，例如 15 或 15,45。');d.hourly_minutes=raw.map(Number);if(d.hourly_minutes.some(v=>v>59)||new Set(d.hourly_minutes).size!==d.hourly_minutes.length)throw new Error('分鐘必須介乎 0–59，而且唔可以重複。');d.windows=[$('five').checked?'5h':null,$('weekly').checked?'weekly':null].filter(Boolean);d.participants=[...document.querySelectorAll('[data-participant]:checked')].map(e=>e.dataset.participant);if(!d.windows.length||!d.participants.length)throw new Error('請至少選擇一個用量視窗同一個帳號。');return d;}
$('settingsForm').addEventListener('input',()=>{dirty=true;renderControls();});
$('settingsForm').addEventListener('submit',async e=>{e.preventDefault();let config;try{config=readForm();}catch(err){announce(err.message,true);return;}if(await action('config.set',{config,revision:draftRevision})){dirty=false;draft=clone(snapshot.config);draftRevision=snapshot.revision;fillForm();renderControls();announce('設定已儲存。');}});
$('reloadDraft').onclick=()=>{draft=clone(snapshot.config);draftRevision=snapshot.revision;dirty=false;fillForm();renderControls();announce('',true);};
$('defaults').onclick=async()=>{if(busy||!connected)return;busy=true;renderControls();try{const d=await request('/agy/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-AGY-CSRF':csrf},body:JSON.stringify({method:'config.defaults'})});draft=d.result;draft.enabled=snapshot.config.enabled;dirty=true;fillForm();announce('預設值已載入草稿，儲存後先會生效。');}catch{announce('未能載入預設值。',true);}finally{busy=false;renderControls();}};
$('master').onclick=async()=>{await action('enabled.set',{enabled:!snapshot.config.enabled});};
$('pause').onclick=async()=>{await action(snapshot.paused_until>Date.now()/1000?'resume':'pause',{seconds:1800});};
$('refresh').onclick=async()=>{manualRefresh=snapshot?.last_refresh_finished||'';manualRefreshAt=Date.now();renderControls();if(await action('refresh',{provider}))announce(`已要求更新 ${provider==='claude'?'Claude':provider==='codex'?'Codex':'Antigravity'} 帳號用量。`);else{manualRefresh=null;renderControls();}};
// Page transition: the incoming panel slides in from the side it comes from, un-blurring (iOS-like spring).
// Only panels animate: a transform on <main> would re-anchor the fixed bottom nav inside it.
function slideIn(panel,fromRight){if(!panel)return;panel.classList.remove('page-in-left','page-in-right');void panel.offsetWidth;panel.classList.add(fromRight?'page-in-right':'page-in-left');panel.addEventListener('animationend',()=>panel.classList.remove('page-in-left','page-in-right'),{once:true});}
function tab(settings){const was=document.body.classList.contains('settings-open');document.body.classList.toggle('settings-open',settings);document.querySelector('.hero').hidden=settings;$('homePanel').hidden=settings;$('settingsPanel').hidden=!settings;$('openSettings').setAttribute('aria-expanded',String(settings));if(was!==settings)slideIn(settings?$('settingsPanel'):$('homePanel'),settings);}
function settingsSection(details){$('settingsForm').hidden=details;$('details').hidden=!details;$('rotationSection').classList.toggle('selected',!details);$('detailsTab').classList.toggle('selected',details);}
$('detailsTab').onclick=()=>settingsSection(true);$('rotationSection').onclick=()=>settingsSection(false);
// Phone remote control, opened from the phone icon next to the connection light (on this Mac only).
async function localCall(method,params={}){return (await request('/agy/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-AGY-CSRF':csrf},body:JSON.stringify({method,params})})).result;}
let remoteInfo=null,pairTimer=null,pairPoll=null;
function stamp(t){return t?date(new Date(t*1000).toISOString()):'—';}
function renderRemote(){const r=remoteInfo;if(!r)return;$('remoteEnabled').checked=r.enabled;$('remoteOptions').hidden=!r.enabled;$('pairStart').disabled=!r.enabled;
  $('remoteLan').checked=r.lan;$('lanAddr').textContent=r.lan_addresses.length?`手機開 http://${r.lan_addresses[0]}:${r.port}`:'呢部 Mac 而家未連到 Wi-Fi／區域網絡。';
  $('tailscaleRow').hidden=!r.tailscale_addresses.length&&!r.tailscale;$('remoteTailscale').checked=r.tailscale;$('tailscaleAddr').textContent=r.tailscale_addresses.length?`手機（都開住 Tailscale）開 http://${r.tailscale_addresses[0]}:${r.port}`:'Tailscale 而家未連線。';
  if(document.activeElement!==$('remotePublic'))$('remotePublic').value=r.public;const wrong=r.ngrok.find(t=>t.port&&t.port!==r.tunnel_port),ng=r.ngrok.find(t=>t.port===r.tunnel_port&&t.host!==r.public);$('ngrokHint').hidden=!wrong&&!ng;
  $('ngrokHint').innerHTML=wrong?`<span class="warn-text">你嘅 ngrok（${esc(wrong.host)}）指緊 ${esc(String(wrong.port))}，唔係 ${r.tunnel_port}：請改用 <b>ngrok http ${r.tunnel_port}</b>，否則手機會一直被拒絕。</span>`:ng?`偵測到 ngrok：<button type="button" class="text" data-use-ngrok="${esc(ng.host)}">用 ${esc(ng.host)}</button>`:'';
  $('remoteFull').checked=r.full;
  $('deviceList').innerHTML=r.devices.map(d=>`<li><div>${esc(d.name||'裝置')}<small>配對於 ${esc(stamp(d.paired_at))} · 最近使用 ${esc(stamp(d.last_used))}</small></div><button type="button" class="manage-remove" data-device-remove="${esc(d.id)}">移除</button></li>`).join('')||'<li><small>未有已配對嘅裝置。</small></li>';}
async function loadRemote(){try{remoteInfo=await localCall('remote.info');renderRemote();}catch(e){announce(errors[e.message]||`未能讀取手機遙控設定：${e.message}`,true);}}
async function setRemote(values,done){try{const r=await localCall('remote.set',values);remoteInfo=r;renderRemote();announce(r.restarting?'已更改，控制台重新啟動緊，幾秒後會自動連返。':(done||'已更改。'));}catch(err){renderRemote();announce(errors[err.message]||err.message,true);}}
function showRemoteDialog(){if(!remoteCtx.local||$('remoteDialog').open)return;$('remoteDialog').showModal();loadRemote();}
window.openRemote=async()=>{if(!csrf)await load();showRemoteDialog();};  // the Mac app's 配對手機 menu item
$('remoteOpen').onclick=showRemoteDialog;
if(location.hash==='#remote'){history.replaceState(null,'',location.pathname);window.openRemote();}
$('remoteClose').onclick=()=>$('remoteDialog').close();
$('remoteEnabled').onchange=e=>setRemote({enabled:e.target.checked},e.target.checked?'已開啟手機遙控，揀連接方式再撳「配對手機」。':'已關閉手機遙控，只有呢部 Mac 可以用控制台。');
$('remoteLan').onchange=e=>setRemote({lan:e.target.checked});
$('remoteTailscale').onchange=e=>setRemote({tailscale:e.target.checked});
$('remoteFull').onchange=e=>setRemote({full:e.target.checked},e.target.checked?'已配對手機而家可以加帳號同改設定。':'已配對手機而家只可以睇用量、轉帳號同暫停。');
$('remotePublicSave').onclick=()=>setRemote({public:$('remotePublic').value},'已儲存公開網址。');
$('remoteDialog').addEventListener('click',e=>{const ng=e.target.closest('[data-use-ngrok]');if(ng){$('remotePublic').value=ng.dataset.useNgrok;setRemote({public:ng.dataset.useNgrok},'已儲存公開網址。');return;}
  const b=e.target.closest('[data-device-remove]');if(b)localCall('devices.remove',{id:b.dataset.deviceRemove}).then(r=>{remoteInfo=r;renderRemote();announce('已移除，嗰部裝置要重新配對先用得返。');}).catch(err=>announce(errors[err.message]||err.message,true));});
function pairQR(url){const qr=qrcode(0,'M');qr.addData(url);qr.make();return qr.createDataURL(6,2);}
function stopPair(){clearInterval(pairTimer);clearInterval(pairPoll);pairTimer=pairPoll=null;}
$('pairStart').onclick=async()=>{let p;try{p=await localCall('pair.start');}catch(err){announce(errors[err.message]||err.message,true);return;}
  const known=new Set((remoteInfo?.devices||[]).map(d=>d.id));const dlg=$('pairDialog'),body=$('pairBody');
  if(!p.urls.length){body.innerHTML='<p>未揀連接方式：請喺「手機遙控」開「同一個 Wi-Fi」或者「Tailscale」，或者填公開網址。</p>';dlg.showModal();return;}
  const show=url=>{body.querySelector('img').src=pairQR(`${url}/agy/pair#${p.code}`);$('pairManual').textContent=`${url}/agy/pair`;};
  body.innerHTML=`<img alt="配對 QR code">${p.urls.length>1?`<select id="pairChoice" aria-label="手機用嘅連接方式">${p.urls.map(u=>`<option>${esc(u)}</option>`).join('')}</select>`:''}<p class="pair-code">${esc(p.code)}</p><p class="muted">或者喺手機瀏覽器開 <b id="pairManual"></b>，輸入上面嘅配對碼。</p><p class="muted" id="pairLeft"></p>`;
  show(p.urls[0]);if($('pairChoice'))$('pairChoice').onchange=e=>show(e.target.value);
  const tick=()=>{const left=Math.max(0,Math.round(p.expires-Date.now()/1000));$('pairLeft').textContent=left?`${Math.floor(left/60)}:${String(left%60).padStart(2,'0')} 後失效 · 只可以用一次`:'已失效，請取消再撳一次「配對手機」。';};
  tick();stopPair();pairTimer=setInterval(tick,1000);
  pairPoll=setInterval(async()=>{try{const info=await localCall('remote.info');const added=info.devices.find(d=>!known.has(d.id));if(added){remoteInfo=info;renderRemote();stopPair();dlg.close();announce(`${added.name||'裝置'} 已配對。`);}}catch{}},2000);
  dlg.showModal();};
$('pairClose').onclick=()=>$('pairDialog').close();
$('pairDialog').addEventListener('close',()=>{stopPair();localCall('pair.cancel').catch(()=>{});});
$('openSettings').onclick=()=>tab(true);$('closeSettings').onclick=()=>tab(false);
async function confirmSwitch(label){const dialog=$('confirm');$('confirm').querySelector('h2').textContent=`轉去 ${label}？`;$('confirmText').textContent='即時轉：即刻關閉所有 agy 工作（包括其他 agent），轉完重開 Antigravity。\n\n等閒置先轉：等冇工作做緊先轉，唔會中斷任何嘢，quota 用盡都照等。開住但閒置嘅 agy 會被關，用 --conversation 接返。';dialog.returnValue='cancel';dialog.showModal();return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue),{once:true}));}
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b||b.disabled)return;if(b.dataset.use){const label=b.dataset.use;const choice=snapshot.config.manual_confirm?await confirmSwitch(label):'now';if(choice==='idle'){await action('use',{label,wait_idle:true});return;}if(choice!=='now')return;switchTarget=label;renderControls();const ok=await action('use',{label,now:true});switchTarget=null;renderControls();if(ok&&connected&&snapshot.active===label&&!snapshot.switching&&!snapshot.recovery_required)announce(`已成功切換至 ${label}。`);else if(ok)announce('轉換已提交，但目前身份未確認，請等候更新。',true);}else if(b.hasAttribute('data-cancel'))await action('cancel');else if(b.hasAttribute('data-recovery'))await action('recovery.ack');});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
load();setInterval(()=>{if(!document.hidden)load();},5000);


// Codex: manual switching only; same close → switch → reopen flow as Gemini's 即時轉.
// Used up: nothing to switch to until the window resets (weekly first, it lasts longer).
// weekly empty = dead until the weekly reset (grey); 5 h empty = back within hours (normal card, shows when).
const spent = w => { const z = v => typeof v === 'number' && v <= 0; return z(w?.weekly?.remaining_percent) ? '每週已用盡' : ''; };
const cooling = w => { const v = w?.['5h']?.remaining_percent; if (typeof v !== 'number' || v > 0) return ''; const r = w['5h'].reset_at; const t = typeof r === 'number' ? new Date(r * 1000) : new Date(r); return isNaN(t) ? '稍後' : t.toLocaleTimeString('zh-HK', { hour: '2-digit', minute: '2-digit', hour12: false }); };
const geminiCooling = (s, label) => cooling(s?.profiles?.[label]?.groups?.find(g => g.family === 'gemini')?.windows);
const codexCooling = r => cooling({ '5h': r?.['5h'] });
const geminiSpent = (s, label) => spent(s?.profiles?.[label]?.groups?.find(g => g.family === 'gemini')?.windows);
const codexSpent = r => spent({ '5h': r?.['5h'], weekly: r?.weekly });
const codexBadge = r => r.active ? '使用中' : r.status === 'OK' ? '可用' : r.status === 'TOKEN_REVOKED' ? '需要重新登入' : r.status === 'TOKEN_EXPIRED' ? '登入已過期' : '查詢失敗';
function renderCodex(s) {const c=s.codex||{profiles:{}};const rows=Object.entries(c.profiles||{});$('codexUpdated').textContent=c.updated_at?'更新於 '+date(c.updated_at):'';
const empty=c.error==='CODEX_NOT_CONFIGURED'||!rows.length?emptyCard('Codex'):'';
const failure=c.error&&rows.length?`<article class="card notice">Codex 用量暫時讀取失敗（${esc(c.error)}），以下係上次資料。</article>`:'';
renderCodexProgress(s);
{const n=rows.length;$('codexAccounts').style.setProperty('--rows',n||1);$('codexAccounts').classList.toggle('dense',n>2);$('codexPanel').classList.toggle('many',n>2);} // 3+ accounts: compact cards, keep 3 progress rows visible
setHTML($('codexAccounts'),failure+(rows.map(([label,r])=>{const active=label===c.active;const stale=!connected||r.status!=='OK';const used=!active&&r.status==='OK'&&codexSpent(r);const cool=!active&&r.status==='OK'&&!used&&codexCooling(r);return `<article class="card ${active?'active':''} ${used?'exhausted':''}"><div class="card-top"><span class="account-identity"><span class="account-name">${esc(r.email||label)}</span><small class="account-alias">${esc(label)}${r.plan?' · ChatGPT '+esc(r.plan):''}</small></span><span class="card-flags">${active&&r.status==='OK'?activityChip(s.activity?.codex):`<span class="badge ${r.status!=='OK'?'bad':used?'spent':cool?'cooling':''}">${esc(used||(cool?`5 小時已用盡 · ${cool}`:(label===c.recommended&&r.status==='OK'?'建議候選':codexBadge({...r,active}))))}</span>`}</span></div>${quotaMarkup(r['5h'],'5h',stale)}${quotaMarkup(r.weekly,'每週',stale)}<div class="card-bottom">${r.status==='TOKEN_REVOKED'||r.status==='TOKEN_EXPIRED'?`<button class="secondary relogin" data-add-open data-add-label="${esc(label)}">重新登入</button>`:`<button data-codex-use="${esc(label)}" class="${active?'in-use':'secondary'}" ${active?'disabled':''}>${active?'使用中':'⇄ 切換'}</button>`}</div></article>`;}).join('')||empty));}
const PROVIDERS=['gemini','codex','claude'],PANELS={gemini:'homePanel',codex:'codexPanel',claude:'claudePanel'};
function setProvider(next){if(!PROVIDERS.includes(next))next='gemini';const changed=next!==provider;const before=PROVIDERS.indexOf(provider);provider=next;try{localStorage.setItem('agy.provider',next);}catch{}const other=next!=='gemini';if(other)tab(false);document.body.classList.toggle('provider-codex',other);$('codexPanel').hidden=next!=='codex';$('claudePanel').hidden=next!=='claude';const i=PROVIDERS.indexOf(next);document.querySelector('.provider-tabs').style.setProperty('--provider-x',i*100+'%');for(const b of document.querySelectorAll('[data-provider]'))b.setAttribute('aria-selected',String(b.dataset.provider===next));render();if(changed)slideIn($(PANELS[next]),i>before);}
async function confirmCodex(label){const dialog=$('codexConfirm');dialog.querySelector('h2').textContent=`轉去 ${label}？`;$('codexConfirmText').textContent='會先關閉 ChatGPT app 同所有 Codex CLI（包括其他 agent 行緊嘅），轉完重開 ChatGPT app。\n\nCodex CLI 唔會自動重開，要自己再開。';dialog.returnValue='cancel';dialog.showModal();return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue),{once:true}));}
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b||b.disabled)return;if(b.dataset.provider){setProvider(b.dataset.provider);return;}if(b.id==='geminiWarmup'||b.id==='codexWarmup'||b.id==='claudeWarmup'){const p=b.id==='geminiWarmup'?'gemini':b.id==='claudeWarmup'?'claude':'codex';if(await action('warmup.set',{provider:p,enabled:!snapshot.warmup[p]}))announce(snapshot.warmup[p]?'已開啟自動喚醒閒置帳號。':'已關閉自動喚醒閒置帳號。');return;}if(b.id==='codexAutoContinue'){if(await action('codex.autocontinue.set',{enabled:!snapshot.codex.autocontinue}))announce(snapshot.codex.autocontinue?'已開啟轉帳號後自動接續。':'已關閉轉帳號後自動接續。');return;}if(b.id==='codexAutoSwitch'){if(await action('codex.auto.set',{enabled:!snapshot.codex.auto}))announce(snapshot.codex.auto?'已開啟 Codex 用盡自動轉。':'已關閉 Codex 用盡自動轉。');return;}if(b.dataset.claudeUse){const label=b.dataset.claudeUse;claudeTarget=label;renderControls();const ok=await action('claude.use',{label});claudeTarget=null;renderControls();if(ok&&snapshot?.claude?.active===label)announce(`已切換 Claude 至 ${label}，行緊嘅 Claude Code 約 30 秒內跟住轉。`);return;}if(!b.dataset.codexUse)return;const label=b.dataset.codexUse;const choice=snapshot?.config.manual_confirm?await confirmCodex(label):'now';if(choice!=='now')return;codexTarget=label;renderControls();const ok=await action('codex.use',{label});codexTarget=null;renderControls();if(ok&&snapshot?.codex?.active===label)announce(`已切換 Codex 至 ${label}，ChatGPT app 已重開。`);});
setProvider(provider);

// Liquid-glass selector thumb: clear glass that magnifies the labels under it, following the finger.
function glassThumb(track, thumb, buttons){
  const lens=document.createElement('span');lens.className='thumb-lens';lens.setAttribute('aria-hidden','true');
  thumb.classList.add('glass-thumb');thumb.append(lens);
  let raf=0,loop=0;
  function sync(){raf=0;const t=track.getBoundingClientRect(),b=thumb.getBoundingClientRect();if(!t.width||!b.width)return;
    const cs=getComputedStyle(track),border=parseFloat(getComputedStyle(thumb).borderLeftWidth)||0;
    lens.style.width=t.width+'px';lens.style.paddingInline=cs.paddingLeft;lens.style.gridTemplateColumns=`repeat(${buttons.length},1fr)`;
    const centre=b.left-t.left+b.width/2;
    lens.style.transformOrigin=`${centre}px 50%`;
    // Magnify up to 1.2x, but never beyond what fits inside the glass (long labels like "Antigravity").
    const centreX=b.left+b.width/2,under=buttons.reduce((best,x)=>{const r=x.getBoundingClientRect(),d=Math.abs(r.left+r.width/2-centreX);return !best||d<best.d?{x,d}:best;},null).x;
    const range=document.createRange();range.selectNodeContents(under);const textWidth=range.getBoundingClientRect().width||1;
    const zoom=Math.max(1,Math.min(1.2,(b.width-10)/textWidth));
    lens.style.transform=`translateX(${t.left-b.left-border}px) scale(${zoom})`;
    setHTML(lens,buttons.map(x=>`<span class="${x.getAttribute('aria-selected')==='true'||x.classList.contains('selected')?'on':''}">${esc(x.textContent.replace('●','').trim())}</span>`).join(''));
    // Same face and weight as the real label (only bigger), and the real label is masked out under the glass
    // so there is never a second copy behind the magnified one.
    const L=b.left-t.left,R=L+b.width;
    buttons.forEach((x,i)=>{const bx=x.getBoundingClientRect(),span=lens.children[i];if(span){const f=getComputedStyle(x);span.style.fontWeight=f.fontWeight;span.style.fontSize=f.fontSize;span.style.fontFamily=f.fontFamily;span.style.letterSpacing=f.letterSpacing;}
      const l=L-(bx.left-t.left),r=R-(bx.left-t.left),m=`linear-gradient(90deg,#000 ${l}px,transparent ${l}px,transparent ${r}px,#000 ${r}px)`;x.style.maskImage=m;x.style.webkitMaskImage=m;});}
  const kick=()=>{if(!raf)raf=requestAnimationFrame(sync);};
  const run=()=>{sync();loop=requestAnimationFrame(run);};
  const start=()=>{cancelAnimationFrame(loop);run();};
  const stop=()=>{cancelAnimationFrame(loop);start();setTimeout(()=>{cancelAnimationFrame(loop);kick();},320);};
  thumb.addEventListener('transitionrun',start);thumb.addEventListener('transitionend',()=>{cancelAnimationFrame(loop);kick();});
  new ResizeObserver(kick).observe(track);addEventListener('resize',kick);kick();
  return {kick,start,stop};
}
// Provider tabs drag exactly like the bottom tab bar.
const providerBar=document.querySelector('.provider-tabs');
const providerLens=glassThumb(providerBar,providerBar.querySelector('.provider-thumb'),[$('providerGemini'),$('providerCodex'),$('providerClaude')]);
let providerDrag=null,providerDragged=false;
providerBar.addEventListener('pointerdown',e=>{if(e.button!==0)return;providerLens.start();const r=providerBar.getBoundingClientRect();providerDrag={id:e.pointerId,x:e.clientX,left:r.left,width:r.width};providerDragged=false;try{providerBar.setPointerCapture(e.pointerId);}catch{}providerBar.classList.add('dragging');});
providerBar.addEventListener('pointermove',e=>{if(!providerDrag||e.pointerId!==providerDrag.id)return;if(Math.abs(e.clientX-providerDrag.x)>5)providerDragged=true;const n=PROVIDERS.length,t=Math.max(0,Math.min(n-1,(e.clientX-providerDrag.left-providerDrag.width/(2*n))/(providerDrag.width/n)));providerBar.style.setProperty('--provider-x',t*100+'%');});
function finishProvider(e,cancel=false){if(!providerDrag)return;providerLens.stop();const n=PROVIDERS.length,i=Math.max(0,Math.min(n-1,Math.floor((e.clientX-providerDrag.left)/(providerDrag.width/n))));const next=cancel?provider:PROVIDERS[i];providerDrag=null;providerBar.classList.remove('dragging');setProvider(next);}
providerBar.addEventListener('pointerup',e=>finishProvider(e));providerBar.addEventListener('pointercancel',e=>finishProvider(e,true));
providerBar.addEventListener('click',e=>{if(providerDragged){e.preventDefault();e.stopImmediatePropagation();providerDragged=false;}},true);
document.addEventListener('click',()=>providerLens.kick());

// Claude Code accounts: usage and warm-up (switching comes once a second account exists).
function renderClaude(s){const c=s.claude||{profiles:{}};const rows=Object.entries(c.profiles||{});$('claudeUpdated').textContent=c.updated_at?'更新於 '+date(c.updated_at):'';
  const empty=c.error==='CLAUDE_NOT_CONFIGURED'||!rows.length?emptyCard('Claude'):'';
  const failure=c.error&&rows.length?`<article class="card notice">Claude 用量暫時讀取失敗（${esc(c.error)}），以下係上次資料。</article>`:'';
  {const n=rows.length;$('claudeAccounts').style.setProperty('--rows',n||1);$('claudeAccounts').classList.toggle('dense',n>2);$('claudePanel').classList.toggle('many',n>2);}
  setHTML($('claudeAccounts'),failure+(rows.map(([label,r])=>{const active=label===c.active;const stale=!connected||r.status!=='OK'||!!r.stale;const used=r.status==='OK'&&codexSpent(r);const cool=!active&&r.status==='OK'&&!used&&codexCooling(r);
    return `<article class="card ${active?'active':''} ${used&&!active?'exhausted':''}" data-claude-label="${esc(label)}"><div class="card-top"><span class="account-identity"><span class="account-name">${esc(r.email||label)}</span><small class="account-alias">${esc(label)}${r.plan?' · Claude '+esc(r.plan):''}</small></span><span class="card-flags">${active&&r.status==='OK'?activityChip(s.activity?.claude):`<span class="badge ${r.status!=='OK'?'bad':used?'spent':cool?'cooling':''}">${esc(r.status!=='OK'?(r.status==='RELOGIN_REQUIRED'||r.status==='TOKEN_EXPIRED'?'需要重新登入':'查詢失敗'):used||(cool?`5 小時已用盡 · ${cool}`:label===c.recommended?'建議候選':'可用'))}</span>`}</span></div>${quotaMarkup(r['5h'],'5h',stale)}${quotaMarkup(r.weekly,'每週',stale)}<div class="card-bottom"><button data-claude-use="${esc(label)}" class="${active?'in-use':'secondary'}" ${active?'disabled':''}>${active?'使用中':'⇄ 切換'}</button></div></article>`;}).join('')||empty));
  const past=[...(s.claude_events||[])].reverse().map(e=>{const text={claude_warmed:`已喚醒 ${e.selected||''} · 開始 5 小時倒數`,claude_warm_failed:`喚醒 ${e.selected||''} 失敗`,claude_warmup_changed:e.enabled?'開啟自動喚醒':'關閉自動喚醒',claude_account_removed:`已移除 ${e.selected||''}`,claude_switched:`${e.source==='automatic'?'自動':'手動'}轉去 ${e.selected||''}`,claude_switch_failed:`轉帳號失敗 · ${errors[e.error]||e.error||''}`}[e.kind]||e.kind;const tone=e.kind.endsWith('failed')?'warn':e.kind==='claude_warmed'||e.kind==='claude_switched'?'ok':'info';return `<li class="step ${tone}"><span class="dot"></span><div><b>${esc(text)}</b><small>${esc(date(e.time))}</small></div></li>`;});
  setHTML($('claudeProgress'),past.join('')||'<li class="step empty"><div><small>未有紀錄。</small></div></li>');}

// Switch success: pop + green burst on the newly active card and a glass toast; manual or automatic.
let lastActive={gemini:null,codex:null,claude:null};
function celebrate(attr,label,name){requestAnimationFrame(()=>{const card=document.querySelector(`[${attr}="${CSS.escape(label)}"]`)?.closest('.card');
  if(card){card.classList.remove('just-switched');void card.offsetWidth;card.classList.add('just-switched');setTimeout(()=>card.classList.remove('just-switched'),2700);}
  let t=$('switchToast');if(!t){t=document.createElement('div');t.id='switchToast';t.setAttribute('role','status');document.body.append(t);}
  t.innerHTML=`<span class="tick" aria-hidden="true">✓</span><span>已切換至 <strong>${esc(name)}</strong></span>`;t.classList.remove('show');void t.offsetWidth;t.classList.add('show');
  try{navigator.vibrate?.(25);}catch{}});}
function detectSwitch(s){const g=s.active,c=s.codex?.active,cl=s.claude?.active;
  if(cl){if(lastActive.claude&&cl!==lastActive.claude)celebrate('data-claude-label',cl,`Claude · ${cl}`);lastActive.claude=cl;}
  // Only real changes between two known accounts; first load and reconnects stay quiet.
  if(g){if(lastActive.gemini&&g!==lastActive.gemini)celebrate('data-use',g,`Antigravity · ${g}`);lastActive.gemini=g;}
  if(c){if(lastActive.codex&&c!==lastActive.codex)celebrate('data-codex-use',c,`Codex · ${c}`);lastActive.codex=c;}}

// Switch/continue progress for Codex: running continues first, then the recent Codex timeline.
const codexStep = e => ({
  codex_switched:[e.source==='automatic'?'ok':'ok',`${e.source==='automatic'?'自動':'手動'}轉去 ${e.selected||''}`],
  codex_switch_failed:['warn',`轉帳號失敗 · ${errors[e.error]||e.error||''}`],
  codex_all_exhausted:['warn','所有帳號都已用盡，未有轉'],
  codex_auto_changed:['info',e.enabled?'開啟用盡自動轉':'關閉用盡自動轉'],
  codex_continue_started:['info',`開始接續 · ${e.project||''}`],
  codex_continue_done:['ok',`接續完成 · ${e.project||''}${e.summary?' — '+e.summary:''}`],
  codex_continue_interrupted:['warn',`接續中斷 · ${e.project||''}`],
  codex_autocontinue:['info',`自動接續 · ${e.project||''}（${e.selected||''}）`],
  reset:['warn',`重設 Antigravity · 轉返 ${e.selected||''}`],
  codex_reset:['warn',`重設 Codex · 轉返 ${e.selected||''}`],
  claude_reset:['warn',`重設 Claude · 轉返 ${e.selected||''}`],
  codex_app_reopened:['warn','ChatGPT app 唔喺度，已自動重開'],
  codex_app_reopen_failed:['bad','ChatGPT app 重開失敗'],
  codex_autocontinue_failed:['warn',`自動接續失敗 · ${e.project||''}`],
  codex_autocontinue_changed:['info',e.enabled?'開啟自動接續':'關閉自動接續'],
  codex_warmed:['ok',`已喚醒 ${e.selected||''} · 開始 5 小時倒數`],
  codex_account_removed:['info',`已移除 ${e.selected||''}`],
  codex_warm_failed:['warn',`喚醒 ${e.selected||''} 失敗`],
  codex_warmup_changed:['info',e.enabled?'開啟自動喚醒':'關閉自動喚醒']}[e.kind]||['info',e.kind]);
function renderCodexProgress(s){const running=(s.codex_watches||[]).filter(w=>w.status==='running');
  $('codexProgressHint').textContent=running.length?`${running.length} 個對話接續中`:'';
  const live=running.map(w=>`<li class="step live"><span class="dot"></span><div><b>接續中 · ${esc(w.project)}</b><small>開始 ${esc(date(w.started_at))} · 做完會自動釋放對話並通知</small></div></li>`);
  const past=[...(s.codex_events||[])].reverse().map(e=>{const [tone,text]=codexStep(e);return `<li class="step ${tone}"><span class="dot"></span><div><b>${esc(text)}</b><small>${esc(date(e.time))}</small></div></li>`;});
  setHTML($('codexProgress'),[...live,...past].join('')||'<li class="step empty"><div><small>未有轉帳號紀錄。</small></div></li>');}

// Live account activity (from the daemon's 5 s probe): working / idle; unknown shows nothing.
function activityChip(a){if(a?.exhausted)return '<span class="activity spent"><i></i>已用盡</span>';if(!a||a.working==null)return '';return a.working?'<span class="activity working"><i></i>工作中</span>':'<span class="activity idle"><i></i>閒置</span>';}

// Add an account from the phone: Antigravity = open Google sign-in, paste the code; Codex = device code.
let addLabel=null, addWasEnrolling=false;
function nextLabel(p){const pre=p==='codex'?'CODEX_':p==='claude'?'CLAUDE_':'GEMINI_';const used=Object.keys(p==='codex'?(snapshot?.codex?.profiles||{}):p==='claude'?(snapshot?.claude?.profiles||{}):(snapshot?.profiles||{}));for(const c of 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')if(!used.includes(pre+c))return pre+c;return pre+'X';}
function renderAdd(){const d=$('addDialog');if(!d.open)return;const e=snapshot?.enrolling;const p=e?.provider||provider;const name=p==='codex'?'Codex':p==='claude'?'Claude':'Antigravity';$('addTitle').textContent=`加 ${name} 帳號`;
  if(!e){if(addWasEnrolling){const done=[...(snapshot?.events||[])].reverse().find(x=>x.kind==='account_added'||x.kind==='account_add_failed'||x.kind==='account_add_cancelled');addWasEnrolling=false;if(done?.kind==='account_added'){setHTML($('addBody'),`<p class="add-ok">✓ 已加入 ${esc(done.selected)}</p>`);setTimeout(()=>d.open&&d.close(),1800);return;}}
    const note=p==='claude'?'會喺背景開一個獨立嘅 Claude 登入，<b>唔會影響你而家用緊嘅 Claude Code</b>。':p==='codex'?'會喺背景開 Codex 登入，唔會影響而家用緊嘅帳號。<br><b>請用私人瀏覽分頁登入，唔好登出其他帳號</b>（登出會令嗰個帳號失效）。':'會暫停自動輪轉、<b>關閉 Antigravity 同 agy 大約一分鐘</b>，登入完自動轉返而家嘅帳號並重開 app。<br>建議用私人瀏覽分頁登入。';
    setHTML($('addBody'),`<label class="add-field">帳號名稱<input id="addLabelInput" value="${esc(addLabel||nextLabel(p))}" autocapitalize="characters" spellcheck="false"></label><p class="add-note">${note}</p><div class="add-actions"><button type="button" class="secondary" data-add-action="close">取消</button><button type="button" data-add-action="start">開始</button></div>`);return;}
  addWasEnrolling=true;
  if(e.stage==='starting'){setHTML($('addBody'),'<p class="add-wait"><span class="spin"></span>準備緊登入…</p><div class="add-actions"><button type="button" class="secondary" data-add-action="cancel">取消</button></div>');return;}
  if(e.provider==='gemini'||e.provider==='claude'){const claude=e.provider==='claude';setHTML($('addBody'),`<ol class="add-steps"><li><a class="add-link" href="${esc(e.url)}" target="_blank" rel="noopener">${claude?'開啟 Claude 登入':'開啟 Google 登入'} ↗</a><small>用要加做 ${esc(e.label)} 嘅帳號登入，撳「${claude?'Authorize':'允許'}」</small></li><li>網頁會顯示授權碼，成段複製<label class="add-field"><input id="addCode" placeholder="${claude?'…#…':'4/0A…'}" autocomplete="off" spellcheck="false"></label></li></ol><div class="add-actions"><button type="button" class="secondary" data-add-action="cancel">取消</button><button type="button" data-add-action="finish">完成</button></div>`,true);return;}
  setHTML($('addBody'),`<ol class="add-steps"><li><a class="add-link" href="${esc(e.url)}" target="_blank" rel="noopener">開啟 OpenAI 登入 ↗</a><small>用私人瀏覽分頁，登入要加做 ${esc(e.label)} 嘅帳號</small></li><li>輸入呢個代碼<button type="button" class="add-code" data-add-action="copy">${esc(e.code)}</button><small>撳代碼可以複製 · 15 分鐘內有效</small></li></ol><p class="add-wait"><span class="spin"></span>登入完會自動完成</p><div class="add-actions"><button type="button" class="secondary" data-add-action="cancel">取消</button></div>`);}
function emptyCard(name){return `<article class="card empty-state"><strong>未有 ${name} 帳號</strong><p>加入第一個帳號，用瀏覽器登入就得。帳號只會儲存喺呢部 Mac。</p><button type="button" class="add-account" data-manage-add>＋ 加帳號</button></article>`;}
function openAdd(label){addLabel=label||null;addWasEnrolling=!!snapshot?.enrolling;$('addDialog').showModal();renderAdd();}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-add-open],[data-add-action]');if(!b)return;
  if(b.hasAttribute('data-add-open')){openAdd(b.dataset.addLabel);return;}
  const act=b.dataset.addAction,p=snapshot?.enrolling?.provider||provider;
  if(act==='close'){$('addDialog').close();return;}
  if(act==='copy'){try{await navigator.clipboard.writeText(snapshot.enrolling.code);b.textContent='已複製';}catch{}return;}
  if(act==='start'){const label=$('addLabelInput').value.trim().toUpperCase();addWasEnrolling=true;b.disabled=true;if(!await action('accounts.add.start',{provider:p,label})){addWasEnrolling=false;b.disabled=false;}renderAdd();return;}
  if(act==='finish'){const code=$('addCode').value.trim();b.disabled=true;await action('accounts.add.finish',{code});renderAdd();return;}
  if(act==='cancel'){await action('accounts.add.cancel',{});addWasEnrolling=false;renderAdd();}});
// Swipe left/right anywhere (except draggable bars, dialogs, fields and scrolling lists) to switch Antigravity / Codex.
// The provider glass follows the finger live, like dragging the tab bar itself; release decides.
let swipe=null;
function endSwipe(t,cancel){const sw=swipe;swipe=null;if(!sw)return;if(sw.live){providerLens.stop();providerBar.classList.remove('dragging');}
  const dx=t?t.clientX-sw.x:0,dy=t?t.clientY-sw.y:0;
  const go=!cancel&&Math.abs(dx)>=60&&Math.abs(dx)>=Math.abs(dy)*1.5&&(sw.live||Date.now()-sw.at<700);
  const cur=PROVIDERS.indexOf(provider),next=go?PROVIDERS[Math.max(0,Math.min(PROVIDERS.length-1,cur+(dx<0?1:-1)))]:provider;
  if(next===provider){if(sw.live)setProvider(provider);return;}
  setProvider(next);}
document.addEventListener('touchstart',e=>{const t=e.touches[0];swipe=null;if(e.touches.length!==1||document.querySelector('dialog[open]')||t.clientX<24||t.clientX>innerWidth-24||e.target.closest('.provider-tabs,nav,dialog,input,select,textarea,.settings-sections'))return;swipe={x:t.clientX,y:t.clientY,at:Date.now(),base:PROVIDERS.indexOf(provider),live:false};},{passive:true});
document.addEventListener('touchmove',e=>{if(!swipe)return;const t=e.touches[0],dx=t.clientX-swipe.x,dy=t.clientY-swipe.y;
  if(!swipe.live){if(Math.abs(dy)>12&&Math.abs(dy)>Math.abs(dx)){swipe=null;return;}if(Math.abs(dx)<12||Math.abs(dx)<Math.abs(dy)*1.5)return;swipe.live=true;providerLens.start();providerBar.classList.add('dragging');}
  const x=Math.max(0,Math.min(PROVIDERS.length-1,swipe.base-dx/(innerWidth*.6)));providerBar.style.setProperty('--provider-x',x*100+'%');},{passive:true});
document.addEventListener('touchend',e=>endSwipe(e.changedTouches[0],false),{passive:true});
document.addEventListener('touchcancel',()=>endSwipe(null,true),{passive:true});
// Cards share the free height (grid 1fr); when each one has room, use the larger type instead of the dense one.
function fitCards(){const box=$('accounts'),card=box.querySelector('.card');const h=card?card.offsetHeight:0;if(!h)return;box.classList.toggle('roomy',h>=112);} // layout height: ignores the page-transition scale; hidden (0) keeps the last state
addEventListener('resize',()=>requestAnimationFrame(fitCards));
// Tap a floating notice to dismiss it at once.
for(const id of ['message','failure'])$(id).addEventListener('click',()=>{$(id).hidden=true;});
// Account manager: list, remove (two taps, never the live account), and the entry to add one.
let removeArmed=null,removeTimer=0;
function renderManage(){const d=$('manageDialog');if(!d.open||!snapshot)return;const codex=provider!=='gemini',src=provider==='claude'?snapshot.claude:snapshot.codex;$('manageTitle').textContent=`${provider==='claude'?'Claude':codex?'Codex':'Antigravity'} 帳號管理`;
  const rows=codex?Object.entries(src?.profiles||{}).map(([l,r])=>({label:l,email:r.email,live:l===src?.active,status:r.status==='OK'?(codexSpent(r)||`5h ${pctShort(r['5h']?.remaining_percent)} · 每週 ${pctShort(r.weekly?.remaining_percent)}`):codexBadge(r)}))
    :Object.entries(snapshot.profiles||{}).map(([l,r])=>{const w=r.groups?.find(g=>g.family==='gemini')?.windows;return {label:l,email:r.email,live:l===snapshot.active,status:r.status==='OK'?(geminiSpent(snapshot,l)||`5h ${pctShort(w?.['5h']?.remaining_percent)} · 每週 ${pctShort(w?.weekly?.remaining_percent)}`):'資料待更新'};});
  setHTML($('manageBody'),`<ul class="manage-list">${rows.sort((a,b)=>a.label.localeCompare(b.label)).map(r=>`<li><div><b>${esc(r.label)}</b><small>${esc(r.email||'Email 未提供')}</small><small class="manage-status">${esc(r.status)}</small></div>${r.live?'<span class="manage-live">使用中</span>':`<button type="button" class="manage-remove ${removeArmed===r.label?'armed':''}" data-manage-remove="${esc(r.label)}">${removeArmed===r.label?'確定移除？':'移除'}</button>`}</li>`).join('')||'<li><small>未有帳號</small></li>'}</ul><p class="add-note">移除只係 Rotator 唔再用呢個帳號，Google / OpenAI / Anthropic 帳號本身唔受影響。用緊嘅帳號要先轉走先可以移除。</p>`);}
const pctShort=v=>typeof v==='number'?`${Math.round(v)}%`:'—';
const RESET_TEXT={gemini:['重設 Antigravity','會清走卡住嘅轉換狀態，關晒所有 agy（包括停唔到嘅），轉返第一個帳號，再更新用量。行緊嘅 agy 對話會中斷。'],codex:['重設 Codex','會清走卡住嘅狀態，關晒 ChatGPT app 同 Codex CLI，轉返第一個帳號，再重開 ChatGPT（remote control 會自動重新連）。'],claude:['重設 Claude','會清走查詢退避狀態，轉返第一個帳號，再即刻查用量。行緊嘅 Claude Code 約 30 秒內自動跟住轉，唔會被關。']};
document.addEventListener('click',async e=>{const b=e.target.closest('[data-reset]');if(!b||b.disabled)return;const p=b.dataset.reset,dlg=$('resetConfirm');dlg.querySelector('h2').textContent=RESET_TEXT[p][0];$('resetConfirmText').textContent=RESET_TEXT[p][1];dlg.returnValue='';dlg.showModal();await new Promise(r=>dlg.addEventListener('close',r,{once:true}));if(dlg.returnValue!=='reset')return;b.disabled=true;const ok=await action('provider.reset',{provider:p});b.disabled=false;if(ok)announce(`${RESET_TEXT[p][0]}完成。`);});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-manage-open],[data-manage-close],[data-manage-add],[data-manage-remove]');if(!b||b.disabled)return;
  if(b.hasAttribute('data-manage-open')){removeArmed=null;$('manageDialog').showModal();renderManage();return;}
  if(b.hasAttribute('data-manage-close')){$('manageDialog').close();return;}
  if(b.hasAttribute('data-manage-add')){$('manageDialog').close();openAdd();return;}
  const label=b.dataset.manageRemove;
  if(removeArmed!==label){removeArmed=label;clearTimeout(removeTimer);removeTimer=setTimeout(()=>{removeArmed=null;renderManage();},4000);renderManage();return;}
  removeArmed=null;clearTimeout(removeTimer);b.disabled=true;
  if(await action('accounts.remove',{provider,label}))announce(`已移除 ${label}`);renderManage();});
