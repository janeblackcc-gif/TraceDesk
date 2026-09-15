'use strict';
const $ = selector => document.querySelector(selector);
const state = {me: null, kb: null, csrf: null, version: 0, query: null, conversation: null, docCursor: null, jobCursor: null, users: [], refreshing: false};
const titles = {workspace: '问答工作台', library: '知识库', jobs: '后台任务', manage: '团队管理'};
const statuses = {queued: '排队中', uploaded: '等待解析', parsing: '解析中', parsed: '解析完成 · 等待索引', indexing: '索引中', building: '索引中', running: '处理中', retry_wait: '等待重试', succeeded: '已完成', ready: '可查询', active: '当前索引', failed: '失败', cancelled: '已取消', superseded: '已被新版本替代', quarantined: '已隔离', deleted: '已删除', answered: '已生成回答', evidence_found: '已找到原文证据', no_evidence: '证据不足', partial: '仅返回原文证据', clarify: '请补充问题', needs_scope: '请核对资料版本'};
const types = {parse: '解析文档', index: '建立索引', reindex: '重建索引', query: '问答', gc: '回收已删除文档', eval: '评测'};
const activeStates = new Set(['queued', 'running', 'retry_wait']);
const errors = {AUTHENTICATION_REQUIRED: '登录已失效，请重新登录。', LOGIN_FAILED: '邮箱或密码不正确。', INVALID_CREDENTIALS: '邮箱或密码不正确。', QUEUE_FULL: '任务队列已满，请稍后重试。', AUTHORIZATION_CHANGED: '访问权限或资料版本已变化，请重新提问。', INDEX_REQUIRED: '请先完成当前知识库的索引。', INDEX_STALE: '模型已变化，请重建索引后再提问。', FORBIDDEN: '当前账号没有执行此操作的权限。', NOT_FOUND: '资源不存在，或你已没有访问权限。', SOURCE_GONE: '该来源已删除或被新版替代。', RESTORE_WINDOW_EXPIRED: '恢复窗口已结束。', DOCUMENT_NAME_CONFLICT: '已有同名文档，请先处理名称冲突。', PASSWORD_POLICY: '密码不符合要求，请使用至少 12 个字符。', BOOTSTRAP_CONSUMED: '工作空间已初始化，请直接登录。', INDEX_PARSE_PENDING: '请等待文档解析完成。'};
function node(tag, className = '', text = '') { const element = document.createElement(tag); element.className = className; element.textContent = text; return element; }
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => { $('#toast').hidden = true; }, 7000); }
function message(error) { return errors[error.code] || `${error.message || '操作失败'}${error.code ? ' · ' + error.code : ''}`; }
function clearSensitive() {
  state.version += 1; state.query = null; state.conversation = null;
  $('#conversation').replaceChildren(); $('#source-lines').replaceChildren(); $('#source-dialog').close();
  $('#query-progress').hidden = true; $('#ask-button').disabled = !state.kb;
}
function loggedOut() {
  clearSensitive(); state.me = null; state.kb = null; state.csrf = null; state.users = [];
  $('#team-shell').hidden = true; $('#login-screen').hidden = false;
  $('#document-rows').replaceChildren(); $('#job-rows').replaceChildren(); $('#member-select').replaceChildren(); $('#question').value = '';
  sessionStorage.removeItem('tracedesk-team-query');
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {}); const method = options.method || 'GET';
  if (!['GET', 'HEAD'].includes(method) && state.csrf) headers.set('X-CSRF-Token', state.csrf);
  let body = options.body;
  if (body && !(body instanceof FormData)) { headers.set('Content-Type', 'application/json'); body = JSON.stringify(body); }
  const response = await fetch('/api/v1' + path, {method, headers, body, credentials: 'same-origin', redirect: 'error', signal: AbortSignal.timeout(20000)});
  const csrf = response.headers.get('X-CSRF-Token'); if (csrf) state.csrf = csrf;
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})); const code = payload.error?.code || `HTTP_${response.status}`;
    if (response.status === 401 && !path.startsWith('/auth/login')) loggedOut();
    if (code === 'AUTHORIZATION_CHANGED') { clearSensitive(); sessionStorage.removeItem('tracedesk-team-query'); }
    const error = new Error(payload.error?.message || '请求未完成'); error.code = code; throw error;
  }
  if (response.status === 204) return null;
  return options.text ? response.text() : response.json();
}
const keyHeader = () => ({'Idempotency-Key': crypto.randomUUID()});
function on(selector, event, action) {
  $(selector).addEventListener(event, async e => { try { await action(e); } catch (error) { toast(message(error)); } });
}
function button(text, action, className = 'button compact secondary') {
  const result = node('button', className, text); result.type = 'button';
  result.addEventListener('click', async () => { result.disabled = true; try { await action(); } catch (error) { toast(message(error)); } finally { result.disabled = false; } });
  return result;
}
function chooseView(view) {
  document.querySelectorAll('.view').forEach(element => element.classList.toggle('active-view', element.id === `${view}-view`));
  document.querySelectorAll('[data-view]').forEach(element => element.classList.toggle('active', element.dataset.view === view));
  $('#page-title').textContent = titles[view];
  if (view === 'manage') loadUsers().catch(error => toast(message(error)));
}
const workspaceId = () => state.kb?.workspace_id || state.me?.workspaces.find(row => row.role === 'admin')?.id;
function applyRole() {
  const role = state.kb?.role; const canEdit = role === 'admin' || role === 'editor';
  $('#role-label').textContent = ({admin: '管理员', editor: '编辑者', viewer: '查看者'})[role] || '';
  $('#upload-panel').hidden = !canEdit; $('#rebuild-index').hidden = !canEdit;
  $('#deleted-control').hidden = role !== 'admin'; if (role !== 'admin') $('#show-deleted').checked = false;
  $('#manage-nav').hidden = !state.me?.workspaces.some(row => row.role === 'admin');
  $('#create-user-panel').hidden = !state.me?.user.is_system_admin;
  $('#ask-button').disabled = !state.kb || Boolean(state.query && activeStates.has(state.query.status));
  $('#signed-in-user').replaceChildren(node('span', '', state.me?.user.display_name || ''), node('small', '', '团队工作空间'));
  $('#index-state').textContent = state.kb ? (state.kb.active_index_generation_id ? '使用已激活的完整索引' : '尚无活动向量索引 · 可使用证据模式') : '请由管理员创建知识库或分配权限';
}
async function refreshSession() {
  const version = state.version; const me = await api('/me'); if (version !== state.version) return false;
  const previous = state.kb; state.me = me;
  const wanted = previous?.id || sessionStorage.getItem('tracedesk-team-kb');
  state.kb = me.knowledge_bases.find(row => row.id === wanted) || me.knowledge_bases[0] || null;
  if (previous && (state.kb?.id !== previous.id || state.kb.data_epoch !== previous.data_epoch || state.kb.role !== previous.role)) {
    clearSensitive(); sessionStorage.removeItem('tracedesk-team-query');
  }
  $('#kb-select').replaceChildren(...me.knowledge_bases.map(row => { const option = node('option', '', row.name); option.value = row.id; return option; }));
  if (state.kb) $('#kb-select').value = state.kb.id;
  else $('#kb-select').append(node('option', '', '尚无可访问的知识库'));
  $('#login-screen').hidden = true; $('#team-shell').hidden = false; applyRole(); return true;
}
async function refresh() {
  if (state.refreshing || !state.me) return; state.refreshing = true;
  try { if (await refreshSession()) await Promise.all([loadDocuments(), loadJobs()]); if (state.query) await pollQuery(); }
  finally { state.refreshing = false; }
}
async function login(email, password) {
  loggedOut(); await api('/auth/login', {method: 'POST', body: {email, password}});
  $('#login-password').value = ''; $('#login-error').textContent = '';
  await refreshSession(); await Promise.all([loadDocuments(), loadJobs()]);
}
on('#login-form', 'submit', async event => { event.preventDefault(); const submit = event.target.querySelector('button'); submit.disabled = true;
  try { await login($('#login-email').value, $('#login-password').value); }
  catch (error) { $('#login-error').textContent = message(error); $('#login-password').value = ''; }
  finally { submit.disabled = false; }
});
on('#show-bootstrap', 'click', () => { $('#bootstrap-form').hidden = !$('#bootstrap-form').hidden; });
on('#bootstrap-form', 'submit', async event => { event.preventDefault(); const body = Object.fromEntries(new FormData(event.target));
  await api('/auth/bootstrap', {method: 'POST', body}); event.target.reset(); event.target.hidden = true; await login(body.email, body.password);
});
on('#logout', 'click', async () => { try { await api('/auth/logout', {method: 'POST'}); } finally { loggedOut(); } });
on('#refresh', 'click', refresh);
on('#kb-select', 'change', async () => { clearSensitive(); sessionStorage.removeItem('tracedesk-team-query'); state.kb = state.me.knowledge_bases.find(row => row.id === $('#kb-select').value) || null;
  if (state.kb) sessionStorage.setItem('tracedesk-team-kb', state.kb.id); applyRole(); await Promise.all([loadDocuments(), loadJobs()]);
});
document.querySelectorAll('[data-view]').forEach(element => element.addEventListener('click', () => chooseView(element.dataset.view)));
on('#new-chat', 'click', () => { clearSensitive(); sessionStorage.removeItem('tracedesk-team-query'); $('#question').value = ''; $('#question').focus(); });
on('#ask-form', 'submit', async event => { event.preventDefault(); if (!state.kb || (state.query && activeStates.has(state.query.status))) return;
  const version = state.version; $('#ask-button').disabled = true;
  try { const accepted = await api('/queries', {method: 'POST', headers: keyHeader(), body: {knowledge_base_id: state.kb.id, question: $('#question').value, profile: $('#profile-select').value, method: 'hybrid', conversation_id: state.conversation}});
    if (version !== state.version) return; state.query = accepted; state.conversation = accepted.conversation_id;
    sessionStorage.setItem('tracedesk-team-query', JSON.stringify({id: accepted.query_id, kb: state.kb.id, user: state.me.user.id})); renderQuery(accepted); await loadJobs();
  } finally { applyRole(); }
});
on('#question', 'keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); $('#ask-form').requestSubmit(); } });
async function pollQuery() {
  const version = state.version; const id = state.query?.query_id; if (!id) return;
  try { const result = await api('/queries/' + id); if (version !== state.version || state.query?.query_id !== id) return; state.query = result; renderQuery(result); }
  catch (error) { if (error.code === 'NOT_FOUND' || error.code === 'FORBIDDEN') clearSensitive(); throw error; }
}
on('#cancel-query', 'click', async () => { if (state.query?.query_job_id) { await api('/jobs/' + state.query.query_job_id + '/cancel', {method: 'POST'}); await pollQuery(); } });
function renderQuery(result) {
  const pending = activeStates.has(result.status); $('#query-progress').hidden = !pending;
  $('#query-state').textContent = `${statuses[result.status] || result.status} · 可以切换页面查看后台任务`;
  $('#ask-button').disabled = pending || !state.kb; if (pending) return;
  const card = node('article', 'answer-card'); const header = node('div', 'answer-header');
  header.append(node('div', 'answer-avatar', 't'), node('strong', '', 'TraceDesk'), node('span', 'tag', statuses[result.status] || result.status)); card.append(header);
  card.append(node('p', 'small muted', result.question)); if (result.warning) card.append(node('div', 'notice', result.warning));
  for (const claim of result.claims) { const block = node('div', 'claim'); block.append(node('div', 'claim-text', claim.text));
    for (const cite of claim.citations) { const source = result.sources.find(row => row.id === cite.chunk_id); if (source) block.append(button(`[${source.rank}] ${source.filename} · P${source.page} ↗`, () => openSource(source, cite.quote), 'citation-button')); } card.append(block);
  }
  if (!result.claims.length && result.sources.length) { for (const source of result.sources.slice(0, 6)) { const block = node('div', 'claim'); block.append(node('p', 'team-source-text', source.text), button(`${source.filename} · P${source.page} ↗`, () => openSource(source, source.text), 'citation-button')); card.append(block); } }
  if (!result.claims.length && !result.sources.length) card.append(node('p', 'team-empty-answer', errors[result.error_code] || result.error_code || statuses[result.status] || result.status));
  if (result.sources.length) card.append(button('导出本次记录', async () => { const markdown = await api('/queries/' + result.query_id + '/export.md', {text: true}); const url = URL.createObjectURL(new Blob([markdown], {type: 'text/markdown;charset=utf-8'})); const link = node('a'); link.href = url; link.download = 'tracedesk-query.md'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }));
  $('#conversation').replaceChildren(card);
}
async function openSource(source, quote = '') {
  const version = state.version; const result = await api('/document-revisions/' + source.revision_id + '/source?page=' + source.page); if (version !== state.version) return;
  $('#source-title').textContent = result.filename; $('#source-meta').textContent = `第 ${result.page} 页`;
  const lines = result.text.split('\n'); const start = lines.slice(0, source.start_line - 1).reduce((sum, line) => sum + line.length + 1, 0);
  const end = lines.slice(0, source.end_line).join('\n').length; const found = quote ? result.text.indexOf(quote, start) : -1;
  const first = found >= start && found + quote.length <= end ? found : -1; let offset = 0;
  $('#source-lines').replaceChildren(...lines.map((line, index) => { const overlap = first >= 0 && offset < first + quote.length && offset + line.length > first;
    const row = node('div', 'source-line' + (overlap ? ' highlight' : '')); row.dataset.line = index + 1; const content = node('span');
    if (overlap) { const a = Math.max(0, first - offset), b = Math.min(line.length, first + quote.length - offset); content.append(document.createTextNode(line.slice(0, a)), node('mark', 'quote-mark', line.slice(a, b)), document.createTextNode(line.slice(b))); }
    else content.textContent = line || ' '; row.append(node('span', '', String(index + 1)), content); offset += line.length + 1; return row;
  })); $('#source-dialog').showModal();
}
on('#close-source', 'click', () => $('#source-dialog').close());
async function loadDocuments(more = false) {
  const version = state.version; if (!state.kb) { $('#document-rows').replaceChildren(); return; }
  const query = new URLSearchParams({include_deleted: String($('#show-deleted').checked)}); if (more && state.docCursor) query.set('cursor', state.docCursor);
  const result = await api(`/knowledge-bases/${state.kb.id}/documents?${query}`); if (version !== state.version) return;
  if (!more) $('#document-rows').replaceChildren(); state.docCursor = result.next_cursor; $('#more-documents').hidden = !state.docCursor;
  for (const document of result.items) { const row = node('tr'); const actions = node('div', 'team-action-group');
    const revision = document.active_revision_id || document.desired_revision_id;
    if (revision && !document.deleted_at) actions.append(button('查看原文', () => openSource({revision_id: revision, page: 1, start_line: 1, end_line: 1000000})));
    if (state.kb.role !== 'viewer' && !document.deleted_at) {
      actions.append(button('重新解析', async () => { await api(`/document-revisions/${document.desired_revision_id}/reparse`, {method: 'POST', headers: keyHeader()}); toast('已加入解析队列'); await refresh(); }));
      actions.append(button('删除', () => confirmDelete(document)));
    }
    if (document.deleted_at && state.kb.role === 'admin') actions.append(button('恢复', async () => { await api('/documents/' + document.id + '/restore', {method: 'POST'}); await refresh(); toast('文档已恢复'); }));
    const info = document.active_revision_id && document.active_revision_id !== document.desired_revision_id ? '旧版可用 · 新版处理中' : `修订 ${document.revision_no || '—'}`;
    const statusCell = node('td'); statusCell.append(node('span', 'tag team-status ' + document.state, statuses[document.state] || document.state));
    const actionCell = node('td'); actionCell.append(actions); row.append(node('td', '', document.name), node('td', 'small muted', info), statusCell, actionCell); $('#document-rows').append(row);
  } $('#library-empty').hidden = Boolean($('#document-rows').children.length);
}
async function confirmDelete(document) {
  $('#confirm-message').textContent = `删除 ${document.name} 后，该文档立即停止提供查询，管理员可在 7 天内恢复。`;
  const dialog = $('#confirm-dialog'); dialog.returnValue = ''; dialog.showModal();
  const confirmed = await new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'delete'), {once: true}));
  if (confirmed) { await api('/documents/' + document.id, {method: 'DELETE'}); await refresh(); toast('已删除，相关任务已撤销'); }
}
on('#confirm-cancel', 'click', () => $('#confirm-dialog').close('cancel')); on('#confirm-delete', 'click', () => $('#confirm-dialog').close('delete'));
on('#show-deleted', 'change', () => loadDocuments()); on('#more-documents', 'click', () => loadDocuments(true));
on('#upload-form', 'submit', async event => { event.preventDefault(); if (!state.kb) return; const submit = event.target.querySelector('button'); submit.disabled = true;
  try { await api('/knowledge-bases/' + state.kb.id + '/documents', {method: 'POST', body: new FormData(event.target), headers: keyHeader()}); event.target.reset(); toast('文档已上传，正在后台解析和索引'); await refresh(); }
  finally { submit.disabled = false; }
});
on('#rebuild-index', 'click', async () => { if (!state.kb) return; await api('/knowledge-bases/' + state.kb.id + '/index-generations', {method: 'POST', body: {force_rebuild: true}, headers: keyHeader()}); toast('已提交索引任务'); await loadJobs(); });
async function loadJobs(more = false) {
  const version = state.version; if (!state.kb) { $('#job-rows').replaceChildren(); return; }
  const query = new URLSearchParams({kb_id: state.kb.id}); if (more && state.jobCursor) query.set('cursor', state.jobCursor);
  const result = await api('/jobs?' + query); if (version !== state.version) return;
  if (!more) $('#job-rows').replaceChildren(); state.jobCursor = result.next_cursor; $('#more-jobs').hidden = !state.jobCursor;
  $('#active-jobs').textContent = String(result.items.filter(job => activeStates.has(job.state)).length);
  for (const job of result.items) { const row = node('tr'); const statusCell = node('td'); statusCell.append(node('span', 'tag team-status ' + job.state, statuses[job.state] || job.state));
    if (job.error_code) statusCell.append(node('small', 'team-error-code', errors[job.error_code] || job.error_code));
    const actionCell = node('td'); if (activeStates.has(job.state) && (state.kb.role !== 'viewer' || job.type === 'query')) actionCell.append(button('取消', async () => { await api('/jobs/' + job.id + '/cancel', {method: 'POST'}); await refresh(); }));
    row.append(node('td', '', types[job.type] || job.type), statusCell, node('td', 'small muted', new Date(job.created_at).toLocaleString()), node('td', '', String(job.attempt)), actionCell); $('#job-rows').append(row);
  } $('#jobs-empty').hidden = Boolean($('#job-rows').children.length);
}
on('#more-jobs', 'click', () => loadJobs(true));
async function loadUsers() { const workspace = workspaceId(); if (!workspace || $('#manage-nav').hidden) return; const version = state.version;
  const result = await api('/workspaces/' + workspace + '/users?limit=200'); if (version !== state.version) return; state.users = result.items;
  $('#member-select').replaceChildren(...result.items.filter(user => user.status === 'active').map(user => { const option = node('option', '', `${user.display_name} · ${user.email}`); option.value = user.id; return option; }));
}
on('#create-kb-form', 'submit', async event => { event.preventDefault(); const workspace = workspaceId(); if (!workspace) return;
  const created = await api('/workspaces/' + workspace + '/knowledge-bases', {method: 'POST', body: Object.fromEntries(new FormData(event.target))}); sessionStorage.setItem('tracedesk-team-kb', created.id); state.kb = null; event.target.reset(); await refresh(); toast('知识库已创建');
});
on('#create-user-form', 'submit', async event => { event.preventDefault(); await api('/users', {method: 'POST', body: Object.fromEntries(new FormData(event.target))}); event.target.reset(); await loadUsers(); toast('账号已创建，请为成员分配知识库权限'); });
on('#grant-form', 'submit', async event => { event.preventDefault(); if (!state.kb) return; const user = state.users.find(row => row.id === $('#member-select').value); if (!user) return;
  if (!user.workspace_role) await api(`/workspaces/${state.kb.workspace_id}/members/${user.id}`, {method: 'PUT', body: {role: 'member'}});
  await api(`/knowledge-bases/${state.kb.id}/members/${user.id}`, {method: 'PUT', body: {role: $('#member-role').value}}); await loadUsers(); toast('成员权限已更新');
});
on('#remove-grant', 'click', async () => { if (!state.kb || !$('#member-select').value) return; await api(`/knowledge-bases/${state.kb.id}/members/${$('#member-select').value}`, {method: 'DELETE'}); toast('知识库授权已撤销'); });
async function start() {
  try { await refreshSession(); const csrf = await api('/auth/csrf'); state.csrf = csrf.csrf_token;
    await Promise.all([loadDocuments(), loadJobs()]);
    const saved = JSON.parse(sessionStorage.getItem('tracedesk-team-query') || 'null');
    if (saved && saved.user === state.me.user.id && saved.kb === state.kb?.id) { state.query = {query_id: saved.id}; await pollQuery(); }
  } catch (error) { if (error.code !== 'AUTHENTICATION_REQUIRED') toast(message(error)); }
}
setInterval(() => { if (state.me && !document.hidden) refresh().catch(error => toast(message(error))); }, 4000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.me) refresh().catch(error => toast(message(error))); });
start();
