'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {library: {documents: [], collections: []}, conversation: null, busy: false, view: 'workspace', report: null};
function el(tag, className = '', text = '') {const node = document.createElement(tag); node.className = className; if (text !== '') node.textContent = text; return node;}
function toast(message, error = false) {const node = $('#toast'); node.textContent = message; node.classList.toggle('error', error); node.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => {node.hidden = true;}, error ? 9000 : 5000);}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data; try {data = await response.json();} catch {throw new Error('服务返回异常响应，请确认本地服务正在运行。');}
  if (!response.ok) {const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data); throw new Error(detail);}
  return data;
}
function jsonPost(path, body = {}) {return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});}
function scope() {return {collection: $('#collection-select').value, version: $('#version-select').value};}
function resetConversation() {state.conversation = null; $('#conversation').replaceChildren(); $('#starter-section').hidden = false;}
function setBusy(busy) {
  state.busy = busy;
  $$('button, input, textarea, select').forEach(node => {if (node.id !== 'close-source') node.disabled = busy;});
}
async function operation(task) {if (state.busy) return; setBusy(true); try {return await task();} catch (error) {toast(error.message, true);} finally {setBusy(false);}}
function showView(view) {
  state.view = view;
  $$('.view').forEach(node => node.classList.toggle('active-view', node.id === `${view}-view`));
  $$('.nav-item').forEach(node => {node.classList.toggle('active', node.dataset.view === view); if (node.dataset.view === view) node.setAttribute('aria-current', 'page'); else node.removeAttribute('aria-current');});
  $('#page-title').textContent = {workspace: '问答工作台', library: '知识库', evaluation: '评测实验室', settings: '本地模型'}[view];
  if (view === 'evaluation' && !state.report) loadReport().catch(error => toast(error.message, true));
  if (view === 'library') updateLibraryScope();
  window.scrollTo({top: 0, behavior: 'instant'});
}
function addOption(select, value, label) {const option = el('option', '', label); option.value = value; select.append(option);}
function populateVersions(preferred) {
  const collection = state.library.collections.find(c => c.name === $('#collection-select').value);
  const select = $('#version-select'); select.replaceChildren();
  if (!collection) {addOption(select, '', '—'); return;}
  collection.versions.forEach(version => addOption(select, version, `${version}${version === collection.active_version ? ' · 当前' : ''}`));
  select.value = collection.versions.includes(preferred) ? preferred : collection.versions.includes(collection.active_version) ? collection.active_version : collection.versions[0];
  updateLibraryScope();
}
async function refreshLibrary() {
  const previous = scope(); state.library = await api('/api/library');
  const select = $('#collection-select'); select.replaceChildren();
  state.library.collections.filter(c => c.versions.length).forEach(c => addOption(select, c.name, c.name));
  if (!select.options.length) addOption(select, '', '尚未导入');
  if ([...select.options].some(o => o.value === previous.collection)) select.value = previous.collection;
  populateVersions(previous.version);
  $('#nav-doc-count').textContent = state.library.documents.length;
  $('#doc-total').textContent = `${state.library.documents.length} 个文件`;
  $('#demo-banner').hidden = state.library.documents.some(d => d.collection === 'Atlas 演示项目');
  $('#upload-collection').value = select.value;
  $('#upload-version').value = $('#version-select').value;
  const body = $('#document-rows'); body.replaceChildren();
  $('#library-empty').hidden = state.library.documents.length > 0;
  for (const doc of state.library.documents) {
    const tr = el('tr'); const file = el('td'); file.append(el('strong', '', doc.filename), el('small', '', `SHA ${doc.sha256.slice(0, 12)}…`));
    const scopeCell = el('td', '', doc.collection); scopeCell.append(el('small', '', doc.version));
    const status = el('td'); status.append(el('span', `status-badge ${doc.status}`, doc.status === 'ready' ? '可检索' : '已隔离'));
    const actions = el('td'); const remove = el('button', 'danger-button', '删除'); remove.type = 'button';
    remove.setAttribute('aria-label', `删除 ${doc.version} ${doc.filename}`);
    remove.addEventListener('click', () => {if (confirm(`删除 ${doc.filename}（${doc.version}）？\n分块、向量和本地追踪将清理。已导出的答案文件无法撤回。`)) operation(async () => {await api(`/api/documents/${doc.id}`, {method: 'DELETE'}); resetConversation(); await refreshLibrary(); toast('文档、分块和向量已删除。');});});
    actions.append(remove); tr.append(file, scopeCell, status, el('td', '', String(doc.chunks)), actions); body.append(tr);
  }
}
function updateLibraryScope() {const selected = scope(); $('#library-scope').textContent = `当前操作范围：${selected.collection || '未选择'} / ${selected.version || '未选择'}。可在问答工作台切换知识库和版本。`;
}
function modeChanged() {
  const ollama = $('#profile-select').value === 'ollama';
  $('#mode-description').textContent = ollama ? '仅连接本地 Ollama · ⌘ / Ctrl + Enter 发送' : '原文摘录，不是大模型生成 · ⌘ / Ctrl + Enter 发送';
  $('#ask-button').replaceChildren(document.createTextNode(ollama ? '生成回答 ' : '查找证据 '), el('span', '', '↑'));
  resetConversation();
}
function exportAnswer(result) {
  const sections = [`# ${result.question}`, `知识库：${result.collection}；版本：${result.version}；实际模式：${result.actual_profile}`, result.citation_check];
  for (const claim of result.claims) {
    sections.push(claim.text);
    claim.citations.forEach(cite => {const source = result.sources.find(s => s.id === cite.chunk_id); if (source) sections.push(`> 来源：${source.filename} · ${source.version} · 第 ${source.page} 页 · 解析行 ${source.start_line}—${source.end_line}\n> ${cite.quote.replaceAll('\n', '\n> ')}`);});
  }
  if (result.warning) sections.push(`提示：${result.warning}`);
  sections.push(`Trace ID：${result.trace_id}`);
  const blob = new Blob([sections.join('\n\n')], {type: 'text/markdown;charset=utf-8'});
  const url = URL.createObjectURL(blob); const anchor = el('a'); anchor.href = url; anchor.download = `TraceDesk-${result.version}-${result.trace_id.slice(0, 8)}.md`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function openSource(source, quote) {
  try {
    const result = await api(`/api/sources/${source.doc_id}?page=${source.page}`);
    $('#source-title').textContent = result.filename;
    $('#source-meta').textContent = `${result.collection} / ${result.version} · 第 ${result.page} 页 · 解析行 ${source.start_line}—${source.end_line}`;
    const full = result.lines.join('\n');
    const rangeStart = result.lines.slice(0, source.start_line - 1).reduce((sum, line) => sum + line.length + 1, 0);
    const rangeEnd = result.lines.slice(0, source.end_line).join('\n').length;
    const found = full.indexOf(quote, rangeStart);
    const first = found >= rangeStart && found + quote.length <= rangeEnd ? found : -1;
    const last = first + quote.length;
    let offset = 0; const container = $('#source-lines'); container.replaceChildren();
    result.lines.forEach((line, index) => {
      const overlap = first >= 0 && offset < last && offset + line.length > first;
      const row = el('div', `source-line${overlap ? ' highlight' : ''}`); row.dataset.line = index + 1;
      const content = el('span');
      if (overlap) {const a = Math.max(0, first - offset), b = Math.min(line.length, last - offset); content.append(document.createTextNode(line.slice(0, a)), el('mark', 'quote-mark', line.slice(a, b)), document.createTextNode(line.slice(b)));}
      else content.textContent = line || ' ';
      row.append(el('span', '', String(index + 1)), content); container.append(row); offset += line.length + 1;
    });
    $('#source-dialog').showModal();
    requestAnimationFrame(() => {$('.source-line.highlight', container)?.scrollIntoView({block: 'center'});});
  } catch (error) {toast(error.message, true);}
}
function renderAnswer(result) {
  const card = el('article', 'answer-card'); const header = el('div', 'answer-header');
  header.append(el('div', 'answer-avatar', 't'), el('strong', '', 'TraceDesk'), el('span', 'tag', result.actual_profile === 'evidence' ? '证据模式 · 原文摘录' : '本地 RAG · 模型生成'), el('span', 'tag', result.version)); card.append(header);
  card.append(el('p', 'answer-description', result.actual_profile === 'evidence' ? '以下是相关原文片段，可能尚不足以回答问题；不是大模型生成的结论。点击引用核验上下文。' : '以下回答由本地模型生成。引用已定位，但语义正确性仍需核验。'));
  if (result.warning) card.append(el('div', 'answer-warning', result.warning));
  let more;
  result.claims.forEach((claim, i) => {
    const block = el('div', 'claim'); block.append(el('div', 'claim-text', claim.text));
    claim.citations.forEach(cite => {
      const source = result.sources.find(s => s.id === cite.chunk_id); if (!source) return;
      const button = el('button', 'citation-button', `[${source.rank}] ${source.filename} · ${source.version} · P${source.page} : L${source.start_line}—${source.end_line} ↗`);
      button.addEventListener('click', () => openSource(source, cite.quote)); block.append(button);
    });
    if (i < 2) card.append(block);
    else {if (!more) {more = el('details', 'more-evidence'); more.append(el('summary', '', `展开其余 ${result.claims.length - 2} 条证据`)); card.append(more);} more.append(block);}
  });
  const footer = el('div', 'answer-footer'); const scoreNotice = result.secondary_channel === 'dense' ? 'BM25 + 稠密向量' : 'BM25 + 词项 TF-IDF';
  footer.append(el('span', '', `${scoreNotice} · ${result.sources.length} 个候选 · ${result.latency_ms} ms · 本次服务端耗时`));
  const actions = el('div'); const exportButton = el('button', 'text-button', '导出 Markdown'); exportButton.addEventListener('click', () => exportAnswer(result)); actions.append(exportButton); footer.append(actions); card.append(footer);
  const details = el('details', 'trace-details'); details.append(el('summary', '', `查看检索追踪 · ${result.trace_id.slice(0, 8)}`));
  const pre = el('pre', '', JSON.stringify({实际问题: result.effective_question, 知识库: result.collection, 版本: result.version, 实际模式: result.actual_profile, 向量模型: result.model_key || '未调用', 检索耗时ms: result.retrieval_ms, 引用检查边界: result.citation_check}, null, 2)); details.append(pre); card.append(details);
  return card;
}
async function submitQuestion(event) {
  event?.preventDefault(); const question = $('#question').value.trim(); const selected = scope();
  if (!question) {toast('请先输入一个问题。'); return;}
  if (!selected.collection) {toast('请先载入演示，或导入自己的文档。', true); return;}
  await operation(async () => {
    $('#starter-section').hidden = true; const area = $('#conversation'); const user = el('div', 'user-question'); user.append(el('span', '', question)); area.append(user);
    const loading = el('div', 'loading', $('#profile-select').value === 'ollama' ? '正在检索证据并等待本地模型…' : '正在所选版本中检索证据…'); area.append(loading);
    try {
      const result = await jsonPost('/api/ask', {...selected, question, profile: $('#profile-select').value, method: 'hybrid', conversation_id: state.conversation});
      state.conversation = result.conversation_id; loading.replaceWith(renderAnswer(result)); $('#question').value = '';
    } catch (error) {
      const failure = el('div', 'answer-warning', `本次请求未完成：${error.message}`); loading.replaceWith(failure); throw error;
    }
  });
}
function formatRate(value) {return value == null ? '未定义' : `${(value * 100).toFixed(1)}%`;}
function renderReport(report) {
  state.report = report; $('#eval-notice').textContent = report.notice + (report.challenge ? ` 困难集补充检查：${report.challenge.passed}/${report.challenge.total} 通过；同义改写 ${report.challenge.paraphrase.passed}/6，主题相近但不可答的问题拒答 ${report.challenge.hard_negative.passed}/6。简单集高分不代表真实使用质量。` : '');
  const metrics = $('#eval-metrics'); metrics.replaceChildren(); const summary = report.methods?.hybrid?.all;
  if (!summary) return;
  const cards = [['检索召回 Recall@5', formatRate(summary.recall_at_5), '词项融合 · 32 道可答题'], ['首个相关结果 MRR@5', summary.mrr_at_5.toFixed(3), '相关证据的排名质量'], ['版本 / 知识库越界', String(summary.scope_leaks), '本组合成集内的越界数'], ['真实大模型评测', '未运行', '不能据此推断生成质量']];
  cards.forEach(([label, value, note]) => {const card = el('div', 'metric-card'); card.append(el('div', 'metric-label', label), el('div', 'metric-value', value), el('div', 'metric-foot', note)); metrics.append(card);});
  const comparison = $('#eval-comparison'); comparison.replaceChildren();
  for (const [method, splits] of Object.entries(report.methods)) for (const split of ['all', 'test']) {
    const data = splits[split]; const row = el('tr');
    [ `${method === 'hybrid' ? '词项融合' : 'BM25'} / ${split}`, formatRate(data.recall_at_5), data.mrr_at_5.toFixed(3), data.ndcg_at_5.toFixed(3), formatRate(data.refusal_recall), `${data.latency_p95_ms} ms`].forEach(text => row.append(el('td', '', text))); comparison.append(row);
  }
  $('#eval-meta').textContent = `数据集 SHA256：${report.dataset_sha256.slice(0, 20)}… · ${report.config.split} · Python ${report.runtime.python} / ${report.runtime.os} · ${report.created_at_utc}`;
  const rows = $('#eval-rows'); rows.replaceChildren();
  report.rows.filter(row => row.method === 'hybrid').forEach(item => {const row = el('tr'); [item.question, `${item.split} · ${item.kind}`, item.version, item.status, formatRate(item.recall_at_5)].forEach(text => row.append(el('td', '', text))); rows.append(row);});
}
async function loadReport() {renderReport(await api('/api/evaluation'));}
$$('.nav-item').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
$('#new-chat').addEventListener('click', () => {resetConversation(); showView('workspace'); $('#question').focus();});
$('#collection-select').addEventListener('change', () => {populateVersions(); resetConversation();});
$('#version-select').addEventListener('change', () => {updateLibraryScope(); resetConversation();});
$('#profile-select').addEventListener('change', modeChanged);
$('#load-demo').addEventListener('click', () => operation(async () => {await jsonPost('/api/demo'); resetConversation(); await refreshLibrary(); $('#collection-select').value = 'Atlas 演示项目'; populateVersions('v2'); toast('已载入 8 份原创演示文档，当前选择 v2。');}));
$$('[data-question]').forEach(button => button.addEventListener('click', () => {$('#question').value = button.dataset.question; $('#question').focus();}));
$('#ask-form').addEventListener('submit', submitQuestion);
$('#question').addEventListener('keydown', event => {if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {event.preventDefault(); submitQuestion();}});
$('#upload-form').addEventListener('submit', event => {event.preventDefault(); const form = new FormData(event.currentTarget); const uploaded = form.get('file'); if (uploaded.size > 10 * 1024 * 1024) {toast('单个文件不能超过 10 MiB。', true); return;} operation(async () => {const result = await api('/api/documents', {method: 'POST', body: form}); resetConversation(); await refreshLibrary(); $('#collection-select').value = form.get('collection').trim(); populateVersions(form.get('version').trim()); toast(result.status === 'quarantined' ? '文档已隔离，不参与检索。请检查可疑指令后重新导入。' : result.duplicate ? '相同内容已存在，没有重复新增。' : '文档导入完成，文本索引已可查询。');});});
$('#activate-version').addEventListener('click', () => operation(async () => {const selected = scope(); if (!selected.collection) throw new Error('请先选择有文档的知识库。'); await jsonPost('/api/active-version', selected); resetConversation(); await refreshLibrary(); toast('当前版本已更新，会话已重置。');}));
$('#index-vectors').addEventListener('click', () => operation(async () => {const selected = scope(); if (!selected.collection) throw new Error('请先导入文档。'); toast('正在通过本地模型建立索引，页面将显示完成或失败结果。'); const result = await jsonPost('/api/index', selected); toast(`向量索引就绪：新增 ${result.indexed} 块 / 共 ${result.total} 块，维度 ${result.dimension}。`);}));
$('#run-eval').addEventListener('click', () => operation(async () => {renderReport(await jsonPost('/api/evaluate')); toast('合成集回归完成。此结果不包含真实模型测试。');}));
$('#check-models').addEventListener('click', () => operation(async () => {const result = await api('/api/models'); const status = $('#model-status'); status.textContent = result.ready ? `服务就绪 · ${result.embedding} + ${result.generation}` : result.online ? `服务在线，所需模型尚未齐备。已安装：${result.models.join('、') || '无'}` : result.message; toast(result.ready ? '本地模型均已安装，下一步建立向量索引。' : '模型尚未就绪，请查看状态说明。', !result.ready);}));
$('#close-source').addEventListener('click', () => $('#source-dialog').close());
$('#source-dialog').addEventListener('click', event => {if (event.target === $('#source-dialog') && event.clientX < $('#source-dialog').getBoundingClientRect().left) $('#source-dialog').close();});
refreshLibrary().catch(error => toast(error.message, true));
