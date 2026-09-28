export const adminHtml = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>模型设置 · HappyChat</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f2;--panel:#fff;--text:#202925;--muted:#69736e;--line:#dce2de;--accent:#176b50}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,sans-serif}header{border-bottom:1px solid var(--line);background:var(--panel);padding:18px max(24px,calc((100vw - 1040px)/2));display:flex;justify-content:space-between;gap:16px}a{color:var(--accent)}main{max-width:1040px;margin:44px auto;padding:0 24px}h1{font-size:32px;letter-spacing:-1px;line-height:1.2;margin:12px 0}h2{font-size:18px;margin:0 0 16px}.eyebrow{color:var(--accent);letter-spacing:2px;font-size:12px;font-weight:700}.muted{color:var(--muted)}.box{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:24px;margin:24px 0}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}button,select{font:inherit;border:1px solid var(--line);border-radius:7px;padding:8px 12px;background:var(--panel);color:var(--text)}button{cursor:pointer}button:disabled{opacity:.45;cursor:default}.primary{background:var(--accent);color:#fff;border-color:var(--accent)}select{width:100%;margin:10px 0}input[type=checkbox]{accent-color:var(--accent);width:17px;height:17px;vertical-align:middle}label{cursor:pointer}details{border-top:1px solid var(--line);padding:16px 0}summary{cursor:pointer}.group-head{display:flex;gap:14px;align-items:center;justify-content:space-between;margin-bottom:10px}.group-name{font-weight:650;overflow-wrap:anywhere}.models{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;margin:16px 0}.models label{overflow-wrap:anywhere;display:flex;gap:8px;align-items:flex-start}.status{min-height:26px;white-space:pre-wrap}.error{color:#b43333}.actions{position:sticky;bottom:0;background:var(--panel);border-top:1px solid var(--line);padding:18px 0;display:flex;justify-content:flex-end;gap:12px}.pill{font-size:12px;border:1px solid var(--line);border-radius:30px;padding:3px 9px}.hidden{display:none}#groups:empty:after{content:'当前没有可展示的聊天模型。';color:var(--muted)}@media(prefers-color-scheme:dark){:root{--bg:#151b18;--panel:#1d2521;--text:#e4eee7;--muted:#a2b2a8;--line:#344139;--accent:#389875}}@media(max-width:600px){main{margin-top:28px;padding:0 16px}.box{padding:18px}.group-head{flex-wrap:wrap}.models{grid-template-columns:1fr}}
</style></head><body><header><strong>HappyChat <span class="muted">/ 管理</span></strong><a href="/">返回聊天</a></header><main><div class="eyebrow">MODEL SETTINGS</div><h1>模型与分组</h1><p class="muted">选择用户可以使用的聊天模型，安排分组顺序，并指定新聊天的默认模型。</p><p id="status" class="status" role="status" aria-live="polite">正在检查管理员身份…</p><a id="login" class="hidden" href="/auth">登录 HappyChat</a><form id="settings" class="hidden"><section class="box"><div class="toolbar"><h2>默认模型</h2><span class="pill">平台统一设置</span></div><label for="default-model">新聊天预选模型</label><select id="default-model"></select><p class="muted">用户保存的个人默认模型优先。停用模型不再出现在聊天列表中，旧会话调用它也会被拒绝。</p></section><section class="box"><div class="toolbar"><h2>分组顺序与模型范围</h2><button id="refresh" type="button">刷新可用模型</button></div><p class="muted">用 ↑ ↓ 调整顺序。停用 default 分组可避免用户误选；此设置不会增加账户余额。</p><div id="groups"></div><p class="muted">保存后，新出现的分组默认不开放。图片生成配置由原有设置管理。</p></section><div class="actions"><button id="reload" type="button">重新加载</button><button id="save" class="primary" type="submit">保存设置</button></div></form></main><script type="module" src="/admin/happychat-models.js"></script></body></html>`;

export const adminScript = `
const $ = (id) => document.getElementById(id);
let catalog = [], groups = [], defaultModel = '', busy = false, dirty = false;
function status(message, error = false) { $('status').textContent = message; $('status').classList.toggle('error', error); }
async function api(method = 'GET', body, refresh = false) {
  const token = localStorage.getItem('token');
  const response = await fetch('/api/happychat/admin/models' + (refresh ? '?refresh=true' : ''), { method, credentials: 'same-origin', headers: { ...(token ? { Authorization: 'Bearer ' + token } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
  const data = await response.json();
  if (!response.ok) { if (response.status === 401 || response.status === 403) $('login').classList.remove('hidden'); throw new Error(data.detail || '请求失败'); }
  return data;
}
function setBusy(value) { busy = value; $('settings').inert = value; $('save').disabled = value; $('refresh').disabled = value; $('reload').disabled = value; }
function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
function groupOf(model) { return model.group || (model.id.includes('::') ? model.id.split('::')[0] : 'default'); }
function available() { return groups.flatMap(g => !g.enabled ? [] : catalog.filter(m => groupOf(m) === g.id && (g.models === null || g.models.includes(m.id)))); }
function defaults() {
  const select = $('default-model'); select.replaceChildren(); const empty = element('option', '自动选择排序后的第一个可用模型'); empty.value = ''; select.append(empty);
  for (const model of available()) { const option = element('option', model.name || model.id); option.value = model.id; select.append(option); }
  if (defaultModel && !available().some(m => m.id === defaultModel)) { const option = element('option', '当前默认模型已停用：' + defaultModel); option.value = defaultModel; option.disabled = true; select.append(option); }
  select.value = defaultModel;
}
function changed() { dirty = true; status('有未保存的修改'); defaults(); }
function render() {
  $('groups').replaceChildren();
  groups.forEach((group, index) => {
    const section = element('details'); section.open = true;
    const summary = element('summary', group.id); section.append(summary);
    const head = element('div', undefined, 'group-head');
    const enableLabel = element('label'); const enabled = element('input'); enabled.type = 'checkbox'; enabled.checked = group.enabled; enabled.addEventListener('change', () => { group.enabled = enabled.checked; changed(); }); enableLabel.append(enabled, document.createTextNode(' 启用此分组')); head.append(enableLabel);
    const controls = element('div'); for (const [delta, label] of [[-1, '↑'], [1, '↓']]) { const button = element('button', label); button.type = 'button'; button.setAttribute('aria-label', group.id + (delta < 0 ? '上移' : '下移')); button.disabled = index + delta < 0 || index + delta >= groups.length; button.addEventListener('click', () => { [groups[index], groups[index + delta]] = [groups[index + delta], groups[index]]; changed(); render(); }); controls.append(button); } head.append(controls); section.append(head);
    const allLabel = element('label'); const all = element('input'); all.type = 'checkbox'; all.checked = group.models === null; all.addEventListener('change', () => { group.models = all.checked ? null : catalog.filter(m => groupOf(m) === group.id).map(m => m.id); changed(); render(); }); allLabel.append(all, document.createTextNode(' 自动开放此分组的新模型')); section.append(allLabel);
    const list = element('div', undefined, 'models');
    for (const model of catalog.filter(m => groupOf(m) === group.id)) { const label = element('label'); const check = element('input'); check.type = 'checkbox'; check.checked = group.models === null || group.models.includes(model.id); check.disabled = group.models === null; check.addEventListener('change', () => { group.models = check.checked ? [...group.models, model.id] : group.models.filter(id => id !== model.id); changed(); }); label.append(check, document.createTextNode(model.name || model.id)); list.append(label); }
    if (!list.childElementCount) list.append(element('p', '此分组当前没有健康的聊天模型。', 'muted')); section.append(list); $('groups').append(section);
  }); defaults();
}
async function load(refresh = false) {
  if (dirty && !confirm('重新加载会丢弃未保存的修改，继续吗？')) return;
  setBusy(true);
  try { const data = await api('GET', undefined, refresh === true); catalog = data.catalog; const policy = data.policy; groups = policy.groups.map(g => ({...g})); for (const id of [...new Set(catalog.map(groupOf))]) if (!groups.some(g => g.id === id)) groups.push({id, enabled: !policy.configured, models: null}); defaultModel = policy.default_model; dirty = false; render(); $('settings').classList.remove('hidden'); $('login').classList.add('hidden'); status('设置已加载'); } catch (error) { status(error.message, true); } finally { setBusy(false); }
}
$('default-model').addEventListener('change', () => { defaultModel = $('default-model').value; changed(); });
$('settings').addEventListener('submit', async (event) => { event.preventDefault(); if (busy) return; setBusy(true); try { await api('PUT', {configured: true, groups, default_model: defaultModel}); dirty = false; status('已保存。新聊天使用新的默认设置，模型列表在短缓存窗口后更新。'); } catch (error) { status(error.message, true); } finally { setBusy(false); } });
$('refresh').addEventListener('click', () => load(true)); $('reload').addEventListener('click', load);
window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
load();
`;
