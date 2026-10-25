/* SimpleURL 前端逻辑 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const state = { page: 1, pageSize: 10, q: '', status: '', sort: 'created_desc', total: 0 };
  let summary = { total_links: 0, total_clicks: 0 };
  let access = { mode: 'public', is_admin: false, can_create: true, mode_label: '开放外部可使用' };

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

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, options));
    let data = {};
    try { data = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) {
      const err = new Error(data.message || `请求失败（${res.status}）`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ---------- 访问模式 ---------- */
  async function loadAccess() {
    try {
      access = await api('/api/access');
    } catch (_) {
      access = { mode: 'public', is_admin: false, can_create: true, mode_label: '开放外部可使用' };
    }
    applyAccess();
    return access;
  }

  function applyAccess() {
    const isAdmin = !!access.is_admin;
    const board = $('#list');
    if (board) board.hidden = !isAdmin;

    // 顶栏控制台
    const badge = $('#mode-badge');
    const loginBtn = $('#console-login');
    const logoutBtn = $('#console-logout');
    const inviteBtn = $('#console-invite');
    if (badge) {
      badge.textContent = isAdmin ? '管理员' : access.mode_label;
      badge.className = 'badge mode' + (isAdmin ? ' admin' : access.mode === 'admin' ? ' locked' : '');
      badge.hidden = false;
    }
    if (loginBtn) loginBtn.hidden = isAdmin || !access.admin_login_enabled;
    if (logoutBtn) logoutBtn.hidden = !isAdmin;
    if (inviteBtn) inviteBtn.hidden = !(isAdmin && access.mode === 'invite');

    // 创建区
    const form = $('#create-form');
    const composer = $('#create');
    const bar = $('#access-bar');
    const text = $('#access-text');
    const inviteForm = $('#invite-form');
    if (form) {
      const blocked = !access.can_create;
      form.querySelectorAll('input, button').forEach((el) => { el.disabled = blocked; });
      form.hidden = blocked;
      // 顺带收起整个卡片，避免露出空壳
      if (composer) composer.classList.toggle('locked', blocked);
    }
    if (bar) {
      const notice = access.reason || '';
      const show = !access.can_create || (!!notice && isAdmin);
      bar.hidden = !show;
      if (text) text.textContent = notice;
    }
    if (inviteForm) inviteForm.hidden = !(access.invite_required && access.admin_login_enabled);
  }

  async function doLogin(secret) {
    await api('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': secret },
    });
    await bootstrap();
  }

  function bindConsole() {
    const loginBtn = $('#console-login');
    if (loginBtn) loginBtn.addEventListener('click', () => { location.href = '/admin/login'; });

    const logoutBtn = $('#console-logout');
    if (logoutBtn) logoutBtn.addEventListener('click', async () => {
      await api('/api/admin/logout', { method: 'POST' });
      toast('已退出登录');
      await bootstrap();
    });

    const inviteBtn = $('#console-invite');
    if (inviteBtn) inviteBtn.addEventListener('click', async () => {
      try {
        const data = await api('/api/admin/invite');
        await copyText(data.invite_code);
        toast('邀请码已复制：' + data.invite_code);
      } catch (err) { toast(err.message); }
    });

    const inviteForm = $('#invite-form');
    if (inviteForm) inviteForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const code = $('#invite-code').value.trim();
      if (!code) return;
      try {
        await api('/api/invite?code=' + encodeURIComponent(code), { method: 'POST' });
        toast('邀请码有效，可以创建了');
        await bootstrap();
      } catch (err) { toast(err.message); }
    });
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
    }
  }

  /* ---------- 创建 ---------- */
  function setCodeStatus(msg, kind) {
    const el = $('#code-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'field-status' + (kind ? ' ' + kind : '');
  }

  // 输入短码边打边查重，别让人填完一整屏才被告知「已被占用」
  function bindCodeCheck() {
    const input = $('#code');
    if (!input) return;
    let timer = null;
    let seq = 0;
    input.addEventListener('input', () => {
      const code = input.value.trim();
      clearTimeout(timer);
      if (!code) { setCodeStatus(''); return; }
      // 本地先挡一遍明显不合法的，省掉一次请求
      if (!/^[A-Za-z0-9_-]+$/.test(code)) { setCodeStatus('只能用字母、数字、- 和 _', 'err'); return; }
      if (code.length < 3) { setCodeStatus('至少 3 个字符', 'err'); return; }
      setCodeStatus('检查中…');
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const data = await api('/api/codes/' + encodeURIComponent(code) + '/available');
          if (mine !== seq) return;
          setCodeStatus(data.available ? '可以用 ✓' : data.message, data.available ? 'ok' : 'err');
        } catch (err) {
          if (mine === seq && err.status !== 401) setCodeStatus('', '');
        }
      }, 280);
    });
  }

  // 目标地址查重：同一页面已有短码时给一条顺手的复用提示
  function bindDupCheck() {
    const input = $('#target');
    const box = $('#dup-hint');
    const list = $('#dup-list');
    if (!input || !box || !list) return;
    let timer = null;
    let seq = 0;

    const hide = () => { box.hidden = true; list.innerHTML = ''; };
    input.addEventListener('input', () => {
      clearTimeout(timer);
      const value = input.value.trim();
      if (value.length < 8) { hide(); return; }
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const data = await api('/api/admin/duplicate?target=' + encodeURIComponent(value));
          if (mine !== seq) return;
          if (!data.items || !data.items.length) { hide(); return; }
          list.innerHTML = data.items.map((it) => `
            <li>
              <a class="mono" href="/${encodeURIComponent(it.code)}" target="_blank" rel="noopener">/${escapeHtml(it.code)}</a>
              <span class="hint">${it.clicks} 次点击${it.expired ? ' · 已过期' : it.enabled ? '' : ' · 已停用'}</span>
              <button class="btn tiny ghost" type="button" data-copy="${escapeHtml(location.origin + '/' + it.code)}">复制</button>
            </li>`).join('');
          box.hidden = false;
        } catch (err) {
          if (mine === seq) hide();  // 未登录 / 出错都不打扰
        }
      }, 500);
    });
    list.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-copy]');
      if (!btn) return;
      await copyText(btn.dataset.copy);
      toast('已复制短链接');
    });
  }

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
        const link = await api('/api/links', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        showResult(link);
        setStatus('创建成功', 'ok');
        form.reset();
        setCodeStatus('');
        const dup = $('#dup-hint');
        if (dup) dup.hidden = true;
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
    $('#result-qr').src = '/api/qr/' + encodeURIComponent(link.code);
    $('#result-tags').innerHTML = (link.tags || [])
      .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
    box.hidden = false;
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  async function loadSummary() {
    try {
      summary = await api('/api/stats/summary');
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
    return `<tr>
      <td><a class="code-cell" href="/s/${encodeURIComponent(link.code)}" title="查看统计">/${escapeHtml(link.code)}</a>${title}</td>
      <td class="target-cell" title="${escapeHtml(link.target)}">${escapeHtml(link.target)}</td>
      <td class="clicks-cell">${link.clicks}</td>
      <td>${tags || '<span class="hint">—</span>'}</td>
      <td>${badgeOf(link)}</td>
      <td>
        <a class="btn tiny ghost" href="/${encodeURIComponent(link.code)}" target="_blank" rel="noopener">访问</a>
        <button class="btn tiny ghost" data-act="edit" data-code="${escapeHtml(link.code)}">编辑</button>
        <button class="btn tiny ghost" data-act="toggle" data-code="${escapeHtml(link.code)}">${link.enabled ? '停用' : '启用'}</button>
        <button class="btn tiny danger" data-act="del" data-code="${escapeHtml(link.code)}">删除</button>
      </td>
    </tr>`;
  }

  async function loadLinks() {
    const body = $('#links-body');
    if (!body) return;
    const params = new URLSearchParams({ page: state.page, page_size: state.pageSize, sort: state.sort });
    if (state.q) params.set('q', state.q);
    if (state.status) params.set('status', state.status);
    try {
      const data = await api('/api/links?' + params.toString());
      state.total = data.total;
      body.innerHTML = data.items.map(rowOf).join('');
      $('#empty').hidden = data.items.length > 0;
      const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
      $('#summary').innerHTML = [
        `<span class="chip">共 ${data.total} 条</span>`,
        `<span class="chip">总点击 ${summary.total_clicks}</span>`,
        `<span class="chip">第 ${data.page}/${pages} 页</span>`,
      ].join('');
      $('#page-info').textContent = `第 ${data.page} 页 / 共 ${Math.max(1, Math.ceil(data.total / state.pageSize))} 页`;
      $('#prev-btn').disabled = data.page <= 1;
      $('#next-btn').disabled = data.page * state.pageSize >= data.total;
    } catch (err) {
      if (err.status === 401) { $('#list').hidden = true; return; }
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
    $('#prev-btn').addEventListener('click', () => { if (state.page > 1) { state.page -= 1; loadLinks(); } });
    $('#next-btn').addEventListener('click', () => { state.page += 1; loadLinks(); });

    const sizeSel = $('#page-size');
    if (sizeSel) sizeSel.addEventListener('change', (e) => {
      state.pageSize = Number(e.target.value) || 10; state.page = 1; loadLinks();
    });
    const exportBtn = $('#export-btn');
    if (exportBtn) exportBtn.addEventListener('click', exportCsv);

    body.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const code = btn.dataset.code;
      try {
        if (btn.dataset.act === 'edit') {
          await openEditor(code);
          return;
        }
        if (btn.dataset.act === 'del') {
          if (!confirm(`确定删除 /${code} ？该操作不可恢复`)) return;
          await api('/api/links/' + encodeURIComponent(code), { method: 'DELETE' });
          toast(`已删除 /${code}`);
        } else {
          const current = await api('/api/links/' + encodeURIComponent(code));
          await api('/api/links/' + encodeURIComponent(code), {
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
  let editing = null;

  async function openEditor(code) {
    let link;
    try {
      link = await api('/api/links/' + encodeURIComponent(code));
    } catch (err) { toast(err.message); return; }
    editing = link;
    const modal = $('#editor');
    if (!modal) return;
    $('#edit-code').textContent = '/' + link.code;
    $('#edit-target').value = link.target || '';
    $('#edit-title').value = link.title || '';
    $('#edit-tags').value = (link.tags || []).join(', ');
    $('#edit-expires').value = link.expires_at ? link.expires_at.slice(0, 16) : '';
    $('#edit-status').textContent = '';
    modal.hidden = false;
    $('#edit-target').focus();
  }

  function closeEditor() {
    const modal = $('#editor');
    if (modal) modal.hidden = true;
    editing = null;
  }

  function bindEditor() {
    const modal = $('#editor');
    if (!modal) return;
    $('#edit-cancel').addEventListener('click', closeEditor);
    $('#edit-cancel-2').addEventListener('click', closeEditor);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeEditor(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !modal.hidden) closeEditor();
    });

    $('#edit-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!editing) return;
      const btn = $('#edit-save');
      const payload = {
        target: $('#edit-target').value.trim(),
        title: $('#edit-title').value.trim(),
        tags: $('#edit-tags').value.trim(),
        // 清空过期时间要显式传 null，否则后端会当成「未提供」
        expires_at: $('#edit-expires').value ? $('#edit-expires').value : null,
      };
      btn.disabled = true;
      $('#edit-status').textContent = '保存中…';
      try {
        await api('/api/links/' + encodeURIComponent(editing.code), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast('已保存');
        closeEditor();
        loadLinks();
      } catch (err) {
        $('#edit-status').textContent = err.message;
      } finally { btn.disabled = false; }
    });
  }

  /* ---------- CSV 导出 ---------- */
  async function exportCsv() {
    const params = new URLSearchParams({ page: 1, page_size: 100, sort: state.sort });
    if (state.q) params.set('q', state.q);
    if (state.status) params.set('status', state.status);
    try {
      const data = await api('/api/links?' + params.toString());
      const head = ['短码', '目标地址', '标题', '标签', '点击', '状态', '创建时间'];
      const rows = data.items.map((l) => [
        l.code, l.target, l.title || '', (l.tags || []).join('|'),
        l.clicks, l.expired ? '已过期' : l.enabled ? '已启用' : '已停用', fmtDate(l.created_at),
      ]);
      const cell = (v) => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`;
      const csv = [head, ...rows].map((r) => r.map(cell).join(',')).join('\r\n');
      // 带 BOM，Excel 双击打开中文不乱码
      const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `simpleurl-links-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast(`已导出 ${rows.length} 条`);
    } catch (err) { toast(err.message); }
  }

  /* ---------- 图表 ---------- */
  function renderChart(el, daily) {
    const max = Math.max(1, ...daily.map((d) => d.count));
    el.innerHTML = daily
      .map((d) => `<div class="bar" title="${d.date}：${d.count} 次点击"><i style="height:${(d.count / max) * 100}%"></i><b>${d.date.slice(5)}</b></div>`)
      .join('');
  }

  /* ---------- 启动 ---------- */
  async function loadAnnouncement() {
    const el = document.getElementById('announcement');
    if (!el) return;
    try {
      const data = await api('/api/announcement');
      if (data.text) { el.textContent = data.text; el.hidden = false; }
    } catch (_) { /* 公告拿不到不影响主流程 */ }
  }

  async function bootstrap() {
    await loadAccess();
    await loadSummary();
    loadAnnouncement();
    if (access.is_admin) await loadLinks();
  }

  document.addEventListener('DOMContentLoaded', () => {
    bindCreate(); bindCopy(); bindBoard(); bindConsole(); bindEditor();
    bindCodeCheck(); bindDupCheck();
    const loginForm = $('#login-form');
    if (loginForm) {
      loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = $('#login-btn');
        const status = $('#login-status');
        btn.disabled = true;
        status.textContent = '验证中…';
        status.className = 'status';
        try {
          await doLogin($('#login-secret').value.trim());
          status.textContent = '登录成功，正在进入…';
          location.href = '/';
        } catch (err) {
          status.textContent = err.message;
          status.className = 'status err';
          btn.disabled = false;
        }
      });
    }
    bootstrap();
  });

  window.SimpleURL = { renderChart };
})();
