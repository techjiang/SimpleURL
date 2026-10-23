/* SimpleURL 前端逻辑 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const state = {
    page: 1,
    pageSize: 10,
    q: '',
    tag: '',
    status: '',
    sort: 'created_desc',
    total: 0,
    token: sessionStorage.getItem('su_token') || '',
  };
  let summary = { total_links: 0, total_clicks: 0, access_required: false };
  let editing = null;
  // 站点访问模式快照，来自 /api/access
  const access = { mode: 'open', admin_mode: false, is_admin: false, can_create: true, can_browse: true };

  /* ---------- 工具 ---------- */
  const escapeHtml = (str) =>
    String(str == null ? '' : str).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let toastTimer = null;
  function toast(msg) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2400);
  }

  function setStatus(msg, kind) {
    const el = $('#create-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  const fmtDate = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  };

  /* 带令牌的请求：统一在这里拼 query / header */
  function withToken(path, options) {
    const opts = options || {};
    if (state.token) {
      const sep = path.includes('?') ? '&' : '?';
      path += `${sep}token=${encodeURIComponent(state.token)}`;
    }
    return api(path, opts);
  }

  // 写请求统一带 CSRF 令牌（后端双提交校验，见 app/core/middleware.py）
  function withCsrf(options) {
    const meta = document.querySelector('meta[name="csrf-token"]');
    const token = meta ? meta.content : '';
    const headers = Object.assign({}, options && options.headers);
    if (token) headers['X-CSRF-Token'] = token;
    return Object.assign({}, options, { headers });
  }

  async function api(path, options) {
    const res = await fetch(path, withCsrf(options));
    if (res.status === 403) {
      showGate();
      throw new Error('访问令牌校验失败');
    }
    if (res.status === 401 && access.admin_mode && !access.is_admin) {
      throw new Error('站点已设为仅管理员可使用，请先登录后台');
    }
    let data = {};
    try { data = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) throw new Error(data.message || `请求失败（${res.status}）`);
    return data;
  }

  /* ---------- 访问模式 ---------- */
  async function loadAccess() {
    try {
      const data = await api('/api/access');
      Object.assign(access, data);
    } catch (_) {
      Object.assign(access, { mode: 'open', admin_mode: false, is_admin: false, can_create: true, can_browse: true });
    }
    applyAccess();
    return access;
  }

  function applyAccess() {
    const locked = access.admin_mode && !access.is_admin;
    document.body.dataset.mode = access.admin_mode ? 'admin' : 'open';
    document.body.dataset.auth = access.is_admin ? 'true' : 'false';

    const banner = $('#mode-banner');
    if (banner) {
      banner.hidden = false;
      banner.classList.toggle('locked', locked);
      $('#mode-text').textContent = locked
        ? '仅管理员可使用：外部访客只能打开已生成的短链接，创建与管理需登录后台。'
        : (access.admin_mode
            ? '仅管理员可使用：你已登录，管理功能可用。'
            : '开放外部可使用：任何人都能创建与管理短链接。');
    }
    const bar = $('#create');
    if (bar) bar.hidden = locked;
    const board = $('#list');
    if (board) board.classList.toggle('locked', locked);
  }

  /* ---------- 访问令牌门禁 ---------- */
  function showGate() {
    const gate = $('#gate');
    if (gate) gate.hidden = false;
  }

  function hideGate() {
    const gate = $('#gate');
    if (gate) gate.hidden = true;
  }

  function bindGate() {
    const form = $('#gate-form');
    if (!form) return;
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const v = ($('#gate-token').value || '').trim();
      if (!v) return;
      state.token = v;
      sessionStorage.setItem('su_token', v);
      hideGate();
      loadSummary();
      loadLinks();
    });
  }

  /* ---------- 创建 ---------- */
  function bindCreate() {
    const form = $('#create-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('#submit-btn');
      const payload = {
        target: $('#target').value.trim(),
        code: $('#code').value.trim() || undefined,
        title: $('#title').value.trim() || undefined,
        tags: $('#tags').value.trim() || undefined,
        expires_at: $('#expires_at').value || undefined,
      };
      if (!payload.target) { setStatus('请先粘贴一个链接', 'err'); return; }
      btn.disabled = true;
      setStatus('正在生成…');
      try {
        const link = await withToken('/api/links', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        showResult(link);
        setStatus('创建成功', 'ok');
        form.reset();
        loadSummary();
        loadLinks();
      } catch (err) {
        setStatus(err.message, 'err');
        toast(err.message);
      } finally {
        btn.disabled = false;
      }
    });
  }

  function showResult(link) {
    const box = $('#result');
    if (!box) return;
    const a = $('#result-link');
    a.href = link.short_url;
    a.textContent = link.short_url;
    $('#result-qr').src = '/api/qr/' + encodeURIComponent(link.code) + tokenQuery();
    $('#result-tags').innerHTML = (link.tags || [])
      .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
    box.hidden = false;
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function tokenQuery() {
    return state.token ? `?token=${encodeURIComponent(state.token)}` : '';
  }

  async function loadSummary() {
    try {
      summary = await withToken('/api/stats/summary');
      if (summary.access_required && !state.token) showGate();
    } catch (_) {
      summary = { total_links: 0, total_clicks: 0 };
    }
  }

  function bindCopy() {
    const btn = $('#copy-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      const text = ($('#result-link').textContent || '').trim();
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove();
      }
      btn.textContent = '已复制 ✓';
      toast('已复制到剪贴板');
      setTimeout(() => { btn.textContent = '复制'; }, 1600);
    });
  }

  /* ---------- 列表 ---------- */
  function badgeOf(link) {
    if (link.expired) return '<span class="badge exp">已过期</span>';
    return link.enabled ? '<span class="badge on">已启用</span>' : '<span class="badge off">已停用</span>';
  }

  function rowOf(link) {
    const tags = (link.tags || []).map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
    const title = link.title ? `<span class="title-cell">${escapeHtml(link.title)}</span>` : '';
    const tq = tokenQuery();
    const locked = access.admin_mode && !access.is_admin;
    const actions = locked
      ? `<span class="hint">只读</span>`
      : `<a class="btn tiny ghost" href="/${encodeURIComponent(link.code)}${tq}" target="_blank" rel="noopener">访问</a>
        <button class="btn tiny ghost" data-act="edit" data-code="${escapeHtml(link.code)}">编辑</button>
        <button class="btn tiny ghost" data-act="toggle" data-code="${escapeHtml(link.code)}">${link.enabled ? '停用' : '启用'}</button>
        <button class="btn tiny danger" data-act="del" data-code="${escapeHtml(link.code)}">删除</button>`;
    return `<tr>
      <td><a class="code-cell" href="/s/${encodeURIComponent(link.code)}${tq}" title="查看统计">/${escapeHtml(link.code)}</a>${title}</td>
      <td class="target-cell" title="${escapeHtml(link.target)}">${escapeHtml(link.target)}</td>
      <td class="clicks-cell">${link.clicks}</td>
      <td>${tags || '<span class="hint">—</span>'}</td>
      <td>${badgeOf(link)}</td>
      <td class="created-cell">${fmtDate(link.created_at)}</td>
      <td>${actions}</td>
    </tr>`;
  }

  async function loadLinks() {
    const body = $('#links-body');
    if (!body) return;
    const params = new URLSearchParams({ page: state.page, page_size: state.pageSize, sort: state.sort });
    if (state.q) params.set('q', state.q);
    if (state.tag) params.set('tag', state.tag);
    if (state.status) params.set('status', state.status);
    try {
      const data = await withToken('/api/links?' + params.toString());
      state.total = data.total;
      body.innerHTML = data.items.map(rowOf).join('');
      $('#empty').hidden = data.items.length > 0;
      $('#empty').querySelector('p').textContent = '还没有链接，先创建一个吧';
      const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
      $('#summary').innerHTML = [
        `<span class="chip">共 ${data.total} 条</span>`,
        `<span class="chip">总点击 ${summary.total_clicks}</span>`,
        `<span class="chip">第 ${data.page}/${pages} 页</span>`,
      ].join('');
      $('#page-info').textContent = `第 ${data.page} 页 / 共 ${pages} 页`;
      $('#prev-btn').disabled = data.page <= 1;
      $('#next-btn').disabled = data.page * state.pageSize >= data.total;
    } catch (err) {
      if (access.admin_mode && !access.is_admin) {
        body.innerHTML = '';
        state.total = 0;
        $('#empty').hidden = false;
        $('#empty').querySelector('p').textContent = '站点已设为仅管理员可使用，登录后台后查看链接库';
        $('#page-info').textContent = '';
        $('#prev-btn').disabled = true;
        $('#next-btn').disabled = true;
        $('#summary').innerHTML = '';
        return;
      }
      toast(err.message);
    }
  }

  function bindBoard() {
    const body = $('#links-body');
    if (!body) return;
    let t = null;
    $('#search-input').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { state.q = e.target.value.trim(); state.page = 1; loadLinks(); }, 260);
    });
    $('#status-filter').addEventListener('change', (e) => { state.status = e.target.value; state.page = 1; loadLinks(); });
    $('#sort-select').addEventListener('change', (e) => { state.sort = e.target.value; state.page = 1; loadLinks(); });
    $('#page-size').addEventListener('change', (e) => {
      state.pageSize = parseInt(e.target.value, 10) || 10;
      state.page = 1;
      loadLinks();
    });
    $('#prev-btn').addEventListener('click', () => { if (state.page > 1) { state.page -= 1; loadLinks(); } });
    $('#next-btn').addEventListener('click', () => { state.page += 1; loadLinks(); });

    const exportBtn = $('#export-btn');
    if (exportBtn) {
      exportBtn.addEventListener('click', () => {
        const params = new URLSearchParams();
        if (state.q) params.set('q', state.q);
        if (state.tag) params.set('tag', state.tag);
        if (state.status) params.set('status', state.status);
        params.set('sort', state.sort);
        if (state.token) params.set('token', state.token);
        window.location.href = '/api/links/export?' + params.toString();
      });
    }

    body.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const code = btn.dataset.code;
      try {
        if (btn.dataset.act === 'del') {
          if (!confirm(`确定删除 /${code} ？该操作不可恢复`)) return;
          await withToken('/api/links/' + encodeURIComponent(code), { method: 'DELETE' });
          toast(`已删除 /${code}`);
        } else if (btn.dataset.act === 'edit') {
          const detail = await withToken('/api/links/' + encodeURIComponent(code));
          openEdit(detail);
          return;
        } else {
          const current = await withToken('/api/links/' + encodeURIComponent(code));
          await withToken('/api/links/' + encodeURIComponent(code), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !current.enabled }),
          });
          toast(`/${code} 已${current.enabled ? '停用' : '启用'}`);
        }
        loadSummary();
        loadLinks();
      } catch (err) {
        toast(err.message);
      }
    });
  }

  /* ---------- 编辑弹窗 ---------- */
  function openEdit(link) {
    editing = link;
    const modal = $('#edit-modal');
    if (!modal) return;
    $('#edit-code').textContent = '/' + link.code;
    $('#edit-target').value = link.target || '';
    $('#edit-title').value = link.title || '';
    $('#edit-tags').value = (link.tags || []).join(', ');
    $('#edit-expires').value = link.expires_at ? toLocalInput(link.expires_at) : '';
    $('#edit-status').textContent = '';
    $('#edit-status').className = 'status';
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function closeEdit() {
    const modal = $('#edit-modal');
    if (!modal) return;
    modal.hidden = true;
    document.body.style.overflow = '';
    editing = null;
  }

  /* ISO 字符串 -> datetime-local 需要的本地 naive 串 */
  function toLocalInput(isoStr) {
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function bindEdit() {
    const modal = $('#edit-modal');
    if (!modal) return;
    modal.addEventListener('click', (e) => {
      if (e.target.dataset.close) closeEdit();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !modal.hidden) closeEdit();
    });
    $('#edit-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!editing) return;
      const payload = {
        target: $('#edit-target').value.trim(),
        title: $('#edit-title').value.trim() || null,
        tags: $('#edit-tags').value.trim() || null,
      };
      if (!payload.target) { setEditStatus('目标链接不能为空', 'err'); return; }
      const exp = $('#edit-expires').value;
      payload.expires_at = exp ? new Date(exp).toISOString() : null;
      const saveBtn = $('#edit-save');
      saveBtn.disabled = true;
      setEditStatus('保存中…');
      try {
        await withToken('/api/links/' + encodeURIComponent(editing.code), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast(`/${editing.code} 已更新`);
        closeEdit();
        loadSummary();
        loadLinks();
      } catch (err) {
        setEditStatus(err.message, 'err');
      } finally {
        saveBtn.disabled = false;
      }
    });
  }

  function setEditStatus(msg, kind) {
    const el = $('#edit-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  /* ---------- 图表 ---------- */
  function renderChart(el, daily) {
    const max = Math.max(1, ...daily.map((d) => d.count));
    el.innerHTML = daily
      .map((d) => `<div class="bar" title="${d.date}：${d.count} 次点击"><i style="height:${(d.count / max) * 100}%"></i><b>${d.date.slice(5)}</b></div>`)
      .join('');
  }

  /* ---------- 启动 ---------- */
  document.addEventListener('DOMContentLoaded', async () => {
    bindCreate(); bindCopy(); bindBoard(); bindEdit(); bindGate();
    await loadAccess();
    loadSummary(); loadLinks();
  });

  window.SimpleURL = { renderChart };
})();
