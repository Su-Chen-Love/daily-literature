(() => {
  "use strict";
  const appearanceKey = "daily-literature.appearance.v1";
  const normalizeAppearance = (value) => ({
    font: ["system","serif","humanist","mono"].includes(value?.font) ? value.font : "system",
    theme: ["sage","paper","sand","mist"].includes(value?.theme) ? value.theme : "sage",
  });
  const readAppearance = () => {
    try { return normalizeAppearance(JSON.parse(localStorage.getItem(appearanceKey))); }
    catch (_) { return normalizeAppearance(null); }
  };
  const applyAppearance = (value) => {
    const preferences = normalizeAppearance(value);
    document.documentElement.dataset.font = preferences.font;
    document.documentElement.dataset.theme = preferences.theme;
    return preferences;
  };
  // This self-hosted script runs in the head, before the page is painted.
  applyAppearance(readAppearance());
  const onReady = () => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const toast = document.getElementById("toast");
  const announce = (message, error = false) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("error", error);
    toast.hidden = false;
    clearTimeout(announce.timer);
    announce.timer = setTimeout(() => { toast.hidden = true; }, 4200);
  };
  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf, ...(options.headers || {})},
    });
    const data = await response.json().catch(() => ({error: "服务器返回了无法识别的响应"}));
    if (!response.ok) throw Object.assign(new Error(data.error || `请求失败（${response.status}）`),{status:response.status});
    return data;
  };

  const appearanceDialog = document.getElementById("personal-center");
  const showAppearance = (value) => {
    const preferences = applyAppearance(value);
    appearanceDialog?.querySelectorAll('input[name="appearance-font"], input[name="appearance-theme"]').forEach((input) => {
      const key = input.name === "appearance-font" ? "font" : "theme";
      input.checked = input.value === preferences[key];
    });
    return preferences;
  };
  const saveAppearance = async (value) => {
    const preferences = showAppearance(value);
    const status = appearanceDialog?.querySelector("[data-appearance-status]");
    try {
      if(accountMode){await persistAccount({appearance:preferences});}
      localStorage.setItem(appearanceKey, JSON.stringify(preferences));
      if (status) status.textContent = "";
    } catch (_) {
      if (status) status.textContent = accountMode?"设置未同步，请重新打开个人中心核对后重试":"已应用；浏览器暂时无法保存偏好";
    }
  };
  showAppearance(readAppearance());

  appearanceDialog?.addEventListener("change", (event) => {
    if (!event.target.matches('input[name="appearance-font"], input[name="appearance-theme"]')) return;
    saveAppearance({font: appearanceDialog.querySelector('input[name="appearance-font"]:checked')?.value,
                    theme: appearanceDialog.querySelector('input[name="appearance-theme"]:checked')?.value});
  });
  window.addEventListener("storage", (event) => { if (event.key === appearanceKey) showAppearance(readAppearance()); });

  const center = document.getElementById('personal-center');
  const accountMode=document.body.dataset.siteMode==='cloud';
  let accountState=null,accountWrite=Promise.resolve(),accountReady=false,accountReadSerial=0;
  const accountPendingFields=new Map();
  const personalKey = 'daily-literature.personal.v1';
  const normalizePersonal = value => ({name:typeof value?.name==='string'?value.name.slice(0,40):'',tagline:typeof value?.tagline==='string'?value.tagline.slice(0,80):'',avatar:typeof value?.avatar==='string'&&value.avatar.length<=400000&&(/^preset:0[1-8]$/.test(value.avatar)||/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(value.avatar))?value.avatar:''});
  const readPersonal = () => {try{return normalizePersonal(JSON.parse(localStorage.getItem(personalKey)))}catch(_){return normalizePersonal(null)}};
  const paintAvatar = (element, value) => {
    if(!element)return; element.replaceChildren();
    if(value.avatar){const image=document.createElement('img');image.src=value.avatar.startsWith('preset:')?'/static/images/avatars/avatar-'+value.avatar.slice(7)+'.webp':value.avatar;image.alt='';if(value.avatar.startsWith('preset:'))image.className='avatar-preset-image';element.append(image)}
    else element.textContent=Array.from(value.name)[0]||'文';
  };
  const renderPersonal = value => {
    document.querySelectorAll('[data-personal-avatar]').forEach(el=>paintAvatar(el,value));
    document.querySelectorAll('[data-personal-name]').forEach(el=>el.textContent=value.name||'个人中心');
    document.querySelectorAll('[data-personal-subtitle]').forEach(el=>el.hidden=!value.name);
  };
  let avatarRead=0;
  let draftPersonal=readPersonal();renderPersonal(draftPersonal);
  const personalForm=center?.querySelector('[data-personal-form]');
  const avatarPicker=center?.querySelector('[data-avatar-picker]');
  const avatarOpen=center?.querySelector('[data-avatar-open]');
  let avatarBefore='';
  const closeAvatarPicker=(cancel=false,focus=true)=>{
    if(!avatarPicker||avatarPicker.hidden)return;
    if(cancel){avatarRead++;draftPersonal={...draftPersonal,avatar:avatarBefore};paintAvatar(center.querySelector('[data-avatar-preview]'),draftPersonal);}
    avatarPicker.hidden=true;avatarOpen.setAttribute('aria-expanded','false');if(focus)avatarOpen.focus();
  };
  avatarOpen?.addEventListener('click',()=>{
    if(!avatarPicker.hidden){closeAvatarPicker(true);return;}
    avatarBefore=draftPersonal.avatar;avatarPicker.hidden=false;avatarOpen.setAttribute('aria-expanded','true');
    avatarPicker.querySelectorAll('[name="avatar-preset"]').forEach(input=>input.checked=input.value===draftPersonal.avatar);
    (avatarPicker.querySelector(':checked')||avatarPicker.querySelector('input')).focus();
  });
  avatarPicker?.addEventListener('click',event=>{if(event.target.name==='avatar-preset'&&event.target.value===draftPersonal.avatar)closeAvatarPicker();});
  avatarPicker?.addEventListener('change',event=>{if(event.target.name!=='avatar-preset')return;avatarRead++;draftPersonal={...draftPersonal,avatar:event.target.value};paintAvatar(center.querySelector('[data-avatar-preview]'),draftPersonal);closeAvatarPicker();queuePersonalSave();});
  center?.querySelector('[data-avatar-cancel]')?.addEventListener('click',()=>closeAvatarPicker(true));

  center?.addEventListener('cancel',event=>{if(avatarPicker&&!avatarPicker.hidden){event.preventDefault();closeAvatarPicker(true);}});
  const resetPersonalForm=()=>{closeAvatarPicker(false,false);avatarRead++;if(personalForm)personalForm.dataset.dirty='false';draftPersonal=readPersonal();if(!personalForm)return;personalForm.elements.display_name.value=draftPersonal.name;personalForm.elements.tagline.value=draftPersonal.tagline;paintAvatar(center.querySelector('[data-avatar-preview]'),draftPersonal);center.querySelector('[data-personal-status]').textContent='';};
  resetPersonalForm();
  personalForm?.elements.display_name.addEventListener('input',()=>paintAvatar(center.querySelector('[data-avatar-preview]'),{...draftPersonal,name:personalForm.elements.display_name.value}));
  let personalTimer,personalRevision=0,personalComposing=false;
  const pendingPersonalKey='daily-literature.personal.pending.v1';
  const retryPersonal=center?.querySelector('[data-personal-retry]');
  async function savePersonal(force=false){
    clearTimeout(personalTimer);if(personalComposing||!personalForm||(!force&&!retryPersonal.hidden))return;
    const revision=personalRevision;
    const value=normalizePersonal({...draftPersonal,name:personalForm.elements.display_name.value.trim(),tagline:personalForm.elements.tagline.value.trim()});
    try{
      if(accountMode)await persistAccount({personal:value});
      else{localStorage.setItem(personalKey,JSON.stringify(value));renderPersonal(value);}
      if(revision===personalRevision){try{localStorage.removeItem(pendingPersonalKey);}catch(_){}draftPersonal=value;personalForm.dataset.dirty='false';center.querySelector('[data-personal-status]').textContent='';retryPersonal.hidden=true;}
    }catch(error){center.querySelector('[data-personal-status]').textContent=error.message||'同步失败，请重试';retryPersonal.hidden=false;}
  }
  function queuePersonalSave(){personalRevision++;try{const value=normalizePersonal({...draftPersonal,name:personalForm.elements.display_name.value.trim(),tagline:personalForm.elements.tagline.value.trim()});localStorage.setItem(pendingPersonalKey,JSON.stringify({base:accountState?.personal||null,value}));}catch(_){}personalForm.dataset.dirty='true';clearTimeout(personalTimer);if(!personalComposing)personalTimer=setTimeout(savePersonal,650);}
  personalForm?.addEventListener('submit',event=>{event.preventDefault();savePersonal();});
  for(const input of personalForm?.querySelectorAll('input[name="display_name"],input[name="tagline"]')||[]){
    input.addEventListener('compositionstart',()=>{personalComposing=true;clearTimeout(personalTimer);});
    input.addEventListener('compositionend',()=>{personalComposing=false;queuePersonalSave();});
    input.addEventListener('input',queuePersonalSave);input.addEventListener('blur',()=>{if(personalForm.dataset.dirty==='true')savePersonal();});
  }
  retryPersonal?.addEventListener('click',async()=>{if(accountMode&&!accountReady)await loadAccount();await savePersonal(true);});
  window.addEventListener('pagehide',()=>{
    if(!accountMode||!accountReady||personalForm?.dataset.dirty!=='true'||personalComposing||!retryPersonal.hidden)return;
    const value=normalizePersonal({...draftPersonal,name:personalForm.elements.display_name.value.trim(),tagline:personalForm.elements.tagline.value.trim()});
    const fields={};for(const key of Object.keys(value))if(value[key]!==accountState.personal?.[key])fields[key]=value[key];
    if(Object.keys(fields).length)fetch('/api/account/settings',{method:'POST',keepalive:true,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({base_version:accountState.version,patch:{personal:fields}})}).catch(()=>{});
  });
  center?.addEventListener('close',()=>{if(personalForm.dataset.dirty==='true')savePersonal();});
  center?.querySelector('[data-avatar-file]')?.addEventListener('change',async event=>{
    const file=event.target.files?.[0],status=center.querySelector('[data-personal-status]'),token=++avatarRead;if(!file)return;

    try{
      if(!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size>3*1024*1024)throw Error('请选择不超过 3MB 的 PNG、JPEG 或 WebP 图片');
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('图片读取失败'));reader.readAsDataURL(file)});
      const image=new Image();image.src=data;await image.decode();if(token!==avatarRead)return;
      const canvas=document.createElement('canvas');canvas.width=canvas.height=160;
      const scale=Math.max(160/image.width,160/image.height);canvas.getContext('2d').drawImage(image,(160-image.width*scale)/2,(160-image.height*scale)/2,image.width*scale,image.height*scale);
      draftPersonal={...draftPersonal,avatar:canvas.toDataURL('image/png')};paintAvatar(center.querySelector('[data-avatar-preview]'),draftPersonal);status.textContent='';closeAvatarPicker();queuePersonalSave();
    }catch(error){if(token===avatarRead)status.textContent=error.message}finally{event.target.value='';}
  });
  center?.querySelector('[data-avatar-clear]')?.addEventListener('click',()=>{avatarRead++;closeAvatarPicker(false,false);draftPersonal={...draftPersonal,avatar:''};paintAvatar(center.querySelector('[data-avatar-preview]'),draftPersonal);queuePersonalSave()});
  function cacheAccount(state){
    accountState=state;accountReady=true;
    if(state.personal){try{localStorage.setItem(personalKey,JSON.stringify(state.personal));}catch(_){}if(![...accountPendingFields].some(([key,count])=>key.startsWith('personal.')&&count>1))renderPersonal(normalizePersonal(state.personal));}
    if(state.appearance){try{localStorage.setItem(appearanceKey,JSON.stringify(state.appearance));}catch(_){}if(![...accountPendingFields].some(([key,count])=>key.startsWith('appearance.')&&count>0))showAppearance(state.appearance);}
  }
  async function persistAccount(patch){
    if(!accountReady||!accountState)throw Error('账号设置尚未加载，请稍后重新打开个人中心');
    const base=structuredClone(accountState),changes={};
    for(const [group,values] of Object.entries(patch)){const changed={};for(const [field,value] of Object.entries(values))if(value!==base[group]?.[field]||accountPendingFields.get(group+'.'+field))changed[field]=value;if(Object.keys(changed).length)changes[group]=changed;}
    if(!Object.keys(changes).length)return accountState;
    const pendingKeys=Object.entries(changes).flatMap(([group,fields])=>Object.keys(fields).map(field=>group+'.'+field));
    pendingKeys.forEach(key=>accountPendingFields.set(key,(accountPendingFields.get(key)||0)+1));
    const operation=accountWrite.catch(()=>{}).then(async()=>{
      try{const result=await api('/api/account/settings',{method:'POST',body:JSON.stringify({base_version:accountState.version,patch:changes})});cacheAccount(result);return result;}
      catch(error){
        if(error.status!==409)throw error;
        const latest=await api('/api/account/settings');
        const conflict=Object.entries(changes).some(([group,fields])=>Object.keys(fields).some(field=>latest[group]?.[field]!==base[group]?.[field]&&latest[group]?.[field]!==fields[field]));
        if(conflict){accountState=latest;throw Error('另一台设备已修改相同设置，当前输入已保留。请核对后点击重试同步。');}
        accountState=latest;const result=await api('/api/account/settings',{method:'POST',body:JSON.stringify({base_version:latest.version,patch:changes})});cacheAccount(result);return result;
      }
    }).finally(()=>pendingKeys.forEach(key=>{const count=accountPendingFields.get(key)-1;if(count)accountPendingFields.set(key,count);else accountPendingFields.delete(key);}));accountWrite=operation;return operation;
  }
  async function loadAccount(migrate=false){
    if(!accountMode)return;
    const status=center.querySelector('[data-personal-status]'),serial=++accountReadSerial;
    try{
      await accountWrite.catch(()=>{});const state=await api('/api/account/settings');if(serial!==accountReadSerial||state.version<(accountState?.version||0))return;cacheAccount(state);
      let pending;try{pending=JSON.parse(localStorage.getItem(pendingPersonalKey));}catch(_){}
      if(pending?.value){
        const fields={};for(const [key,value] of Object.entries(normalizePersonal(pending.value)))if(value!==pending.base?.[key])fields[key]=value;
        const conflict=Object.keys(fields).some(key=>state.personal?.[key]!==pending.base?.[key]&&state.personal?.[key]!==fields[key]);
        if(conflict){draftPersonal=normalizePersonal(pending.value);personalForm.elements.display_name.value=draftPersonal.name;personalForm.elements.tagline.value=draftPersonal.tagline;paintAvatar(center.querySelector('[data-avatar-preview]'),draftPersonal);personalForm.dataset.dirty='true';retryPersonal.hidden=false;throw Error('另一台设备更新了相同资料，未同步输入已保留，请核对后重试');}
        if(Object.keys(fields).length)await persistAccount({personal:fields});localStorage.removeItem(pendingPersonalKey);
      }
      if(migrate){const patch={};
        if(!state.personal){try{if(localStorage.getItem(personalKey))patch.personal=readPersonal();}catch(_){}}
        if(!state.appearance){try{if(localStorage.getItem(appearanceKey))patch.appearance=readAppearance();}catch(_){}}
        if(Object.keys(patch).length)await persistAccount(patch);
      }
      if(center.open&&personalForm.dataset.dirty!=='true'&&avatarPicker.hidden)resetPersonalForm();
    }catch(error){if(!accountState)accountReady=false;status.textContent='账号设置读取或同步失败：'+error.message;}
  }

  const formatTime=value=>{if(!value)return '尚无记录';const date=new Date(value);return Number.isFinite(date.getTime())?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date)+'（北京时间）':'尚无记录'};
  const setTime=(selector,value)=>{const el=document.querySelector(selector);if(el){el.textContent=formatTime(value);el.title=value||''}};
  const showSync = status => {
    let state=status.status==='failed'?'failed':status.running?'running':status.pending?'pending':status.status==='disabled'?'disabled':status.synced_at?'succeeded':'unknown';
    const label=state==='failed'?'同步失败':state==='running'?'正在同步':state==='pending'?'等待本地接收':state==='succeeded'?'最近同步成功':state==='disabled'?'未配置同步':'同步状态尚未确认';
    document.querySelectorAll('[data-sync-message]').forEach(el=>el.textContent=status.message||label);
    if('synced_at'in status||'remote_synced_at'in status)setTime('[data-center-sync-time]',status.synced_at||status.remote_synced_at);
  };
  let checkRequest=0;
  const refreshChecks=async()=>{
    const token=++checkRequest,button=center?.querySelector('[data-refresh-checks]'),error=center?.querySelector('[data-center-error]');if(button)button.disabled=true;if(error)error.hidden=true;
    try{const data=await api('/api/center/status');if(token!==checkRequest)return;
      const content=center.querySelector('[data-center-checks-content]');content.innerHTML=data.html;
      setTime('[data-center-local-time]',data.local_checked_at);setTime('[data-center-import-time]',data.last_import);showSync(data.sync);
    }catch(e){if(error){error.textContent='检查暂时无法读取，可稍后重试。';error.hidden=false}}
    finally{if(button)button.disabled=false}
  };
  const selectCenterTab=name=>{
    if(!['profile','appearance','checks'].includes(name))name='profile';
    center?.querySelectorAll('[data-center-tab]').forEach(el=>{const active=el.dataset.centerTab===name;el.setAttribute('aria-selected',String(active));el.tabIndex=active?0:-1});
    center?.querySelectorAll('[data-center-panel]').forEach(el=>el.hidden=el.dataset.centerPanel!==name);
    if(name==='checks')refreshChecks();
  };
  const openCenter=name=>{if(!center)return;if(personalForm.dataset.dirty!=='true')resetPersonalForm();selectCenterTab(name);if(!center.open)center.showModal();loadAccount();};
  document.querySelectorAll('[data-open-center]').forEach(el=>el.addEventListener('click',()=>openCenter(el.dataset.openCenter)));
  center?.querySelectorAll('[data-center-tab]').forEach(el=>el.addEventListener('click',()=>selectCenterTab(el.dataset.centerTab)));
  center?.querySelector('.center-tabs')?.addEventListener('keydown',event=>{
    if(!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End'].includes(event.key)||!event.target.matches('[role="tab"]'))return;
    event.preventDefault();const tabs=[...center.querySelectorAll('[role="tab"]')],index=tabs.indexOf(event.target),next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(['ArrowDown','ArrowRight'].includes(event.key)?1:-1)+tabs.length)%tabs.length;selectCenterTab(tabs[next].dataset.centerTab);tabs[next].focus();
  });
  center?.querySelector('[data-refresh-checks]')?.addEventListener('click',refreshChecks);
  const requestedCenter=new URL(location.href).searchParams.get('center');
  if(['profile','appearance','checks'].includes(requestedCenter))openCenter(requestedCenter);
  loadAccount(true);
  window.addEventListener('focus',()=>{if(!center?.open)loadAccount();});
  api('/api/sync/status').then(showSync).catch(()=>showSync({status:'unknown',message:'暂时无法读取同步状态'}));
  window.addEventListener('storage',event=>{if(event.key===personalKey){renderPersonal(readPersonal());if(!center?.open)resetPersonalForm();}});

  document.querySelectorAll("[data-sync-now]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    const label = document.querySelector("[data-sync-message]");
    try {
      const result = await api("/api/sync", {method: "POST", body: "{}"});
      if (label) label.textContent = result.message;
      announce(result.message);
      showSync({pending:result.status!=='disabled',status:result.status,message:result.message});
      let attempts = 0;
      const poll = async () => {
        try {
          const status = await api("/api/sync/status");
          showSync(status);
          if (status.pending && ++attempts < 100) setTimeout(poll, 3500);
        } catch (_) { /* Saved requests remain queued during a network interruption. */ }
      };
      setTimeout(poll, 2000);
    } catch (error) { showSync({status:"failed",message:"同步请求未发出，请稍后重试。"}); announce(error.message, true); }
    finally { button.disabled = false; }
  }));

  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => {
    button.closest("dialog")?.close();
  }));

  const manualDialog = document.getElementById("manual-paper-dialog");
  document.querySelector("[data-open-manual-paper]")?.addEventListener("click", () => manualDialog?.showModal());
  document.querySelector("[data-manual-paper-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    const values = new FormData(form);
    submit.disabled = true;
    try {
      const result = await api("/api/papers/manual", {method: "POST", body: JSON.stringify({
        apa_citation: values.get("apa_citation"), abstract: values.get("abstract"),
      })});
      form.reset(); manualDialog?.close(); announce(result.message);
    } catch (error) { announce(error.message, true); }
    finally { submit.disabled = false; }
  });

  document.querySelectorAll(".paper-details").forEach((details) => {
    const summary = details.querySelector(":scope > summary");
    details.addEventListener("toggle", () => {
      if (summary) {
        const label = summary.querySelector("span");
        if (label) label.textContent = details.open ? "收起" : "详情";
        else summary.textContent = details.open ? "收起详情" : "展开详情";
      }
    });
  });

  document.querySelectorAll("[data-manual-abstract-form]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const values = new FormData(form);
    submit.disabled = true;
    try {
      const result = await api("/api/papers/abstract", {method: "POST", body: JSON.stringify({
        identity: values.get("identity"), abstract: values.get("abstract"), source_url: values.get("source_url"),
      })});
      announce(result.message);
      location.reload();
    } catch (error) { announce(error.message, true); }
    finally { submit.disabled = false; }
  }));

  const matchingCards = (identity) => [...document.querySelectorAll("[data-paper-identity]")]
    .filter((card) => card.dataset.paperIdentity === identity);
  const syncFavorite = (card, favorite) => {
    const button = card.querySelector("[data-favorite]");
    if (button) {
      button.setAttribute("aria-pressed", String(favorite));
      button.classList.toggle("is-favorite", favorite);
      const label = button.querySelector("[data-favorite-label]");
      if (label) label.textContent = favorite ? "已收藏" : "收藏";
      button.setAttribute("aria-label", favorite ? "取消收藏" : "收藏文献");
    }
    const checkbox = card.querySelector('input[name="favorite"]');
    if (checkbox) checkbox.checked = favorite;
    const star = card.querySelector(".feedback-heading .star");
    if (star) star.hidden = !favorite;
  };
  const setCardBusy = (identity, busy) => matchingCards(identity).forEach((card) => {
    card.querySelectorAll('[data-favorite], [data-feedback-form] button[type="submit"]').forEach((button) => { button.disabled = busy; });
  });

  const orderRecommendationCards = (list) => {
    if (!list) return;
    const cards = [...list.querySelectorAll(":scope > .paper-card")];
    const pending = cards.filter((card) => !card.dataset.interest);
    const interested = cards.filter((card) => card.dataset.interest === "interested");
    const notInterested = cards.filter((card) => card.dataset.interest === "not_interested");
    [...pending, ...interested, ...notInterested].forEach((card) => list.append(card));
  };

  document.querySelectorAll("[data-feedback-form]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const values = new FormData(form);
    const payload = {
      identity: values.get("identity"),
      interest: values.get("interest"),
      reason: values.get("reason"),

      favorite: values.get("favorite") === "on",
    };
    setCardBusy(payload.identity, true);
    try {
      const result = await api("/api/feedback", {method: "POST", body: JSON.stringify(payload)});
      matchingCards(payload.identity).forEach((card) => {
        card.dataset.interest = result.interest || "";
        card.dataset.feedbackReason = payload.reason || "";
        const status = card.querySelector("[data-feedback-status]");
        if (status) {
          status.textContent = result.status_label;
          status.hidden = !result.status_label;
          status.className = `completion-status ${result.interest || "neutral"}`;
        }
        const otherForm = card.querySelector("[data-feedback-form]");
        if (otherForm) {
          otherForm.elements.interest.value = result.interest || "";
          otherForm.elements.reason.value = payload.reason;
        }
        syncFavorite(card, payload.favorite);
        const list = card.closest("[data-today-list], [data-history-list]");
        if (list) {
          card.classList.add("is-completing");
          orderRecommendationCards(list);
          setTimeout(() => card.classList.remove("is-completing"), 780);
        }
      });
      announce(result.message);
    } catch (error) { announce(error.message, true); }
    finally { setCardBusy(payload.identity, false); }
  }));

  document.addEventListener("click", async (event) => {
    const feedback = event.target.closest("[data-open-feedback]");
    if (feedback) {
      const card = feedback.closest("[data-paper-identity]");
      const details = card?.querySelector(".paper-details");
      if (details) details.open = true;
      requestAnimationFrame(() => card?.querySelector('[data-feedback-form] select[name="interest"]')?.focus());
      return;
    }
    const favorite = event.target.closest("[data-favorite]");
    if (favorite) {
      event.preventDefault();
      const next = favorite.getAttribute("aria-pressed") !== "true";
      setCardBusy(favorite.dataset.identity, true);
      try {
        const result = await api("/api/favorite", {method: "POST", body: JSON.stringify({identity: favorite.dataset.identity, favorite: next})});
        matchingCards(favorite.dataset.identity).forEach((card) => syncFavorite(card, result.favorite));
        announce(result.message);
      } catch (error) { announce(error.message, true); }
      finally { setCardBusy(favorite.dataset.identity, false); }
    }
  });

  document.querySelectorAll("[data-inline-pdf-form]").forEach((form) => {
    const trigger = form.querySelector("[data-inline-pdf-trigger]");
    const input = form.querySelector("[data-inline-pdf-file]");
    const name = form.querySelector("[data-inline-pdf-name]");
    const submit = form.querySelector("[data-inline-pdf-submit]");
    trigger?.addEventListener("click", () => input?.click());
    input?.addEventListener("change", () => {
      const file = input.files?.[0];
      if (!file || !name || !submit) return;
      name.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
      name.hidden = false;
      submit.hidden = false;
      submit.textContent = form.dataset.hasFulltext === "true" ? "更新上传" : "确认上传";
      trigger.textContent = "重新选择 PDF";
    });
  });

  const sourceRoot = document.querySelector("[data-source-search]");
  if (sourceRoot) {
    const input = sourceRoot.querySelector("#source-query");
    const list = sourceRoot.querySelector("[data-source-status]")?.nextElementSibling;
    const status = sourceRoot.querySelector("[data-source-status]");
    const spinner = sourceRoot.querySelector("[data-source-spinner]");
    let timer = 0, active = -1, controller = null, previewToken = "";
    const options = () => [...list.querySelectorAll('[role="option"]:not([aria-disabled="true"])')];
    const selectIndex = (index) => {
      const items = options();
      active = items.length ? (index + items.length) % items.length : -1;
      items.forEach((item, i) => {
        item.classList.toggle("active", i === active);
        item.setAttribute("aria-selected", String(i === active));
      });
      if (active >= 0) {
        input.setAttribute("aria-activedescendant", items[active].id);
        items[active].scrollIntoView({block: "nearest"});
      } else input.removeAttribute("aria-activedescendant");
    };
    const preview = async (id) => {
      status.textContent = "正在读取真实作品预览…";
      try {
        const result = await api("/api/sources/preview", {method: "POST", body: JSON.stringify({candidate_id: id})});
        previewToken = result.token;
        const dialog = document.getElementById("source-preview-dialog");
        dialog.querySelector("[data-preview-title]").textContent = `确认添加“${result.source.name}”`;
        const official = result.source.official_status === "verified"
          ? `官网卷期将由 ${result.source.official_provider} 核验最近两期。`
          : "目前先使用 14 天 API 采集；系统会提示后续补充官网适配。";
        dialog.querySelector("[data-preview-summary]").textContent = `从 ${result.since} 起找到 ${result.total_results} 个结果。${official}`;
        const samples = dialog.querySelector("[data-preview-list]");
        samples.replaceChildren(...(result.samples.length ? result.samples : [{title: "当前时间窗没有样例", venue: "可稍后重试"}]).map((item) => {
          const li = document.createElement("li");
          const strong = document.createElement("strong"); strong.textContent = item.title;
          const span = document.createElement("span"); span.textContent = [item.venue, item.doi].filter(Boolean).join(" · ");
          li.append(strong, span); return li;
        }));
        dialog.showModal(); status.textContent = "预览已就绪，确认后才会保存。";
      } catch (error) { status.textContent = error.message; announce(error.message, true); }
    };
    const render = (items, message) => {
      list.replaceChildren();
      items.forEach((item, index) => {
        const button = document.createElement("button");
        button.type = "button"; button.role = "option"; button.id = `source-option-${index}`;
        button.dataset.id = item.candidate_id; button.setAttribute("aria-selected", "false");
        button.setAttribute("aria-disabled", String(Boolean(item.added))); button.disabled = Boolean(item.added);
        const title = document.createElement("strong"); title.textContent = item.name + (item.added ? "（已添加）" : "");
        const meta = document.createElement("span");
        const official = item.official_status === "verified" ? `官网：${item.official_provider}` : "官网适配：待补充";
        meta.textContent = [item.source_type, item.publisher, item.issn && `ISSN ${item.issn}`, item.openalex_id && `OpenAlex ${item.openalex_id}`, official, item.match_basis].filter(Boolean).join(" · ");
        button.append(title, meta); button.addEventListener("click", () => preview(item.candidate_id)); list.append(button);
      });
      list.hidden = items.length === 0; input.setAttribute("aria-expanded", String(items.length > 0));
      status.textContent = message || (items.length ? `找到 ${items.length} 个候选` : "没有找到可验证的来源");
      active = -1; input.removeAttribute("aria-activedescendant");
    };
    input.addEventListener("input", () => {
      clearTimeout(timer); controller?.abort();
      const query = input.value.trim();
      if (query.length < 2) { render([], "请输入至少 2 个字符"); return; }
      spinner.hidden = false; status.textContent = "搜索中…";
      timer = setTimeout(async () => {
        controller = new AbortController();
        try {
          const response = await fetch(`/api/sources/search?q=${encodeURIComponent(query)}`, {signal: controller.signal});
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "搜索失败");
          render(data.items || [], data.message);
        } catch (error) {
          if (error.name !== "AbortError") render([], `${error.message}。请稍后重试。`);
        } finally { spinner.hidden = true; }
      }, 320);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); selectIndex(active + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); selectIndex(active - 1); }
      else if (event.key === "Enter" && active >= 0) { event.preventDefault(); options()[active]?.click(); }
      else if (event.key === "Escape") {
        list.hidden = true; active = -1; input.setAttribute("aria-expanded", "false");
        input.removeAttribute("aria-activedescendant");
      }
    });
    document.querySelector("[data-confirm-source]")?.addEventListener("click", async (event) => {
      event.target.disabled = true;
      try {
        const result = await api("/api/sources/confirm", {method: "POST", body: JSON.stringify({token: previewToken})});
        announce(result.message); location.reload();
      } catch (error) { announce(error.message, true); event.target.disabled = false; }
    });
    document.querySelectorAll("[data-remove-source]").forEach((button) => button.addEventListener("click", async () => {
      const accepted = confirm(`移除“${button.dataset.name}”将停止未来监测。历史论文默认保留，也不会删除收藏、反馈或 PDF。继续吗？`);
      if (!accepted) return;
      button.disabled = true;
      try {
        const result = await api("/api/sources/remove", {method: "POST", body: JSON.stringify({name: button.dataset.name})});
        announce(result.message); location.reload();
      } catch (error) { announce(error.message, true); button.disabled = false; }
    }));
  }


  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", onReady, {once: true});
  else onReady();
})();
