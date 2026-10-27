/* SimpleURL 前端逻辑 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const state = { page: 1, pageSize: 10, q: '', status: '', sort: 'created_desc', total: 0 };
  const selected = new Set();
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

    // 顶栏：管理员专属入口平时不出现，登录后才现身，别让访客看到一堆点不动的链接
    const tag = $('#mode-tag');
    const loginBtn = $('#console-login');
    const logoutBtn = $('#console-logout');
    const inviteBtn = $('#console-invite');
    document.querySelectorAll('[data-admin-only]').forEach((el) => { el.hidden = !isAdmin; });

    if (tag) {
      const locked = !isAdmin && access.mode !== 'public';
      tag.textContent = isAdmin ? '管理员' : access.mode_label;
      tag.className = 'console-tag' + (isAdmin ? ' admin' : locked ? ' locked' : '');
      // 开放模式下、又没登录时，这枚小圆点没什么信息量，收起来
      tag.hidden = !isAdmin && access.mode === 'public';
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

  // 短码预览：自定义填了显示自己填的，没填就提示自动生成——
  // 让人在按下回车前就知道最终会长什么样
  function bindPreview() {
    const code = $('#code');
    const out = $('#preview-url');
    if (!code || !out) return;
    const base = location.origin.replace(/^https?:\/\//, '') + '/';
    const render = () => { out.textContent = base + (code.value.trim() || '自动生成'); };
    code.addEventListener('input', render);
    render();
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
        password: ($('#password') && $('#password').value.trim()) || undefined,
        max_clicks: ($('#max_clicks') && $('#max_clicks').value)
          ? Number($('#max_clicks').value) : undefined,
        daily_clicks_limit: ($('#daily_clicks_limit') && $('#daily_clicks_limit').value)
          ? Number($('#daily_clicks_limit').value) : undefined,
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
        loadBoard();
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
    const map = {
      active: '<span class="badge on">已启用</span>',
      disabled: '<span class="badge off">已停用</span>',
      expired: '<span class="badge exp">已过期</span>',
      exhausted: '<span class="badge exp">次数用完</span>',
      daily: '<span class="badge warn">今日已满</span>',
    };
    const st = link.state || (link.expired ? 'expired' : link.enabled ? 'active' : 'disabled');
    return map[st] || map.active;
  }

  function metaOf(link) {
    const bits = [];
    if (link.locked) bits.push('口令');
    if (link.max_clicks) bits.push(`${link.clicks}/${link.max_clicks}`);
    if (link.daily_clicks_limit) bits.push(`今日 ${link.daily_used}/${link.daily_clicks_limit}`);
    if (link.expires_at) bits.push('限时');
    return bits.join(' · ');
  }

  function rowOf(link) {
    const tags = (link.tags || []).map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
    const title = link.title
      ? `<span class="title-cell" title="${escapeHtml(link.title)}">${escapeHtml(link.title)}</span>`
      : '';
    const meta = metaOf(link);
    return `<tr${selected.has(link.code) ? ' class="picked"' : ''}>
      <td class="col-check">
        <input type="checkbox" class="row-check" data-code="${escapeHtml(link.code)}"
               aria-label="选择 /${escapeHtml(link.code)}"${selected.has(link.code) ? ' checked' : ''} />
      </td>
      <td>
        <a class="code-cell" href="/s/${encodeURIComponent(link.code)}" title="查看统计">/${escapeHtml(link.code)}</a>
        ${title}
      </td>
      <td class="target-cell" title="${escapeHtml(link.target)}">${escapeHtml(link.target)}</td>
      <td class="clicks-cell">${link.clicks}${meta ? `<span class="sub-cell">${escapeHtml(meta)}</span>` : ''}</td>
      <td class="col-hide-sm">${tags || '<span class="hint">—</span>'}</td>
      <td class="col-hide-sm">${badgeOf(link)}</td>
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
        `<span class="chip">本页 ${data.items.length} 条</span>`,
      ].join('');
      // 换页后旧的选择已经不在眼前，留着只会误伤
      selected.clear();
      refreshBulkBar();
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
    $('#edit-max-clicks').value = link.max_clicks || '';
    $('#edit-daily').value = link.daily_clicks_limit || '';
    $('#edit-clear-daily').checked = false;
    $('#edit-clear-daily').disabled = !link.daily_clicks_limit;
    $('#edit-password').value = '';
    $('#edit-password').placeholder = link.locked ? '已设置口令，留空保持不变' : '留空表示不设口令';
    $('#edit-clear-password').checked = false;
    $('#edit-clear-password').disabled = !link.locked;
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
      const expires = $('#edit-expires').value;
      const maxClicks = $('#edit-max-clicks').value.trim();
      const dailyLimit = $('#edit-daily').value.trim();
      const password = $('#edit-password').value.trim();
      const payload = {
        target: $('#edit-target').value.trim(),
        title: $('#edit-title').value.trim(),
        tags: $('#edit-tags').value.trim(),
        // 清空要显式说一声：只传 null 等于「设为无」，后端分不清「没动」
        clear_expiry: !expires,
        clear_max_clicks: !maxClicks,
        clear_daily_limit: !dailyLimit || $('#edit-clear-daily').checked,
        clear_password: $('#edit-clear-password').checked,
      };
      if (expires) payload.expires_at = expires;
      if (maxClicks) payload.max_clicks = Number(maxClicks);
      if (dailyLimit && !$('#edit-clear-daily').checked) payload.daily_clicks_limit = Number(dailyLimit);
      if (password) payload.set_password = password;
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
        loadBoard();
      } catch (err) {
        $('#edit-status').textContent = err.message;
      } finally { btn.disabled = false; }
    });
  }

  const STATE_TEXT = {
    active: '已启用', disabled: '已停用', expired: '已过期', exhausted: '次数用完', daily: '今日已满',
  };

  function stateText(link) {
    return STATE_TEXT[link.state || (link.expired ? 'expired' : link.enabled ? 'active' : 'disabled')] || '已启用';
  }

  /* ---------- 批量操作 ---------- */
  function refreshBulkBar() {
    const bar = $('#bulk-bar');
    const count = $('#bulk-count');
    if (!bar) return;
    bar.hidden = selected.size === 0;
    if (count) count.textContent = `已选 ${selected.size} 条`;
    const all = $('#check-all');
    if (all) {
      const boxes = document.querySelectorAll('.row-check');
      all.checked = boxes.length > 0 && [...boxes].every((b) => b.checked);
      all.indeterminate = !all.checked && [...boxes].some((b) => b.checked);
    }
  }

  function clearSelection() {
    selected.clear();
    document.querySelectorAll('.row-check').forEach((b) => { b.checked = false; });
    document.querySelectorAll('tr.picked').forEach((tr) => tr.classList.remove('picked'));
    refreshBulkBar();
  }

  async function runBulk(action, label) {
    const codes = [...selected];
    if (!codes.length) return;
    if (action === 'delete' && !confirm(`确定删除这 ${codes.length} 条短链？该操作不可恢复`)) return;
    try {
      const data = await api('/api/links/bulk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ codes, action }),
      });
      const miss = data.missing?.length ? `，${data.missing.length} 条不存在` : '';
      toast(`${label} ${data.done} 条${miss}`);
      clearSelection();
      loadSummary();
      loadLinks();
      loadBoard();
    } catch (err) { toast(err.message); }
  }

  function bindBulk() {
    const body = $('#links-body');
    if (!body) return;
    body.addEventListener('change', (e) => {
      const box = e.target.closest('.row-check');
      if (!box) return;
      const tr = box.closest('tr');
      if (box.checked) { selected.add(box.dataset.code); tr?.classList.add('picked'); }
      else { selected.delete(box.dataset.code); tr?.classList.remove('picked'); }
      refreshBulkBar();
    });

    const all = $('#check-all');
    if (all) all.addEventListener('change', () => {
      document.querySelectorAll('.row-check').forEach((box) => {
        box.checked = all.checked;
        if (all.checked) selected.add(box.dataset.code); else selected.delete(box.dataset.code);
        box.closest('tr')?.classList.toggle('picked', all.checked);
      });
      refreshBulkBar();
    });

    $('#bulk-enable')?.addEventListener('click', () => runBulk('enable', '已启用'));
    $('#bulk-disable')?.addEventListener('click', () => runBulk('disable', '已停用'));
    $('#bulk-delete')?.addEventListener('click', () => runBulk('delete', '已删除'));
    $('#bulk-clear')?.addEventListener('click', clearSelection);
  }

  /* ---------- 热门榜 ---------- */
  async function loadBoard() {
    const box = $('#hot');
    const list = $('#hot-list');
    if (!box || !list) return;
    try {
      const data = await api('/api/board?limit=8');
      const items = data.items || [];
      if (!items.length) { box.hidden = true; return; }
      list.innerHTML = items.map((it, i) => `
        <li class="hot-item">
          <span class="hot-rank">${i + 1}</span>
          <a class="hot-code mono" href="/${encodeURIComponent(it.code)}" target="_blank"
             rel="noopener">/${escapeHtml(it.code)}</a>
          <span class="hot-title" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</span>
          <b class="hot-clicks">${it.clicks}</b>
          <button class="btn tiny ghost" type="button"
                  data-copy="${escapeHtml(location.origin + '/' + it.code)}">复制</button>
        </li>`).join('');
      box.hidden = false;
      const empty = $('#hot-empty');
      if (empty) empty.hidden = true;
    } catch (_) {
      // 热门榜是加分项，拿不到就不显示，别打扰创建主流程
      box.hidden = true;
    }
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
        l.clicks, stateText(l), fmtDate(l.created_at),
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
    if (!el) return;
    const rows = daily || [];
    const total = rows.reduce((sum, d) => sum + (d.count || 0), 0);
    if (!rows.length || total === 0) {
      el.innerHTML = '<div class="chart-empty">这段时间还没有点击</div>';
      return;
    }
    const max = Math.max(1, ...rows.map((d) => d.count));
    el.innerHTML = rows
      .map((d) => {
        const h = d.count ? Math.max(4, (d.count / max) * 100) : 0;
        return `<div class="bar${d.count ? '' : ' dim'}" title="${d.date}：${d.count} 次点击"><i style="height:${h}%"></i><b>${d.date.slice(5)}</b></div>`;
      })
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

  // 顶栏「链接库」跳到 /#list 时，列表是 JS 拉完才出现的，浏览器自己跳不准
  function scrollToHash() {
    const id = (location.hash || '').slice(1);
    if (!id) return;
    const el = document.getElementById(id);
    if (el && !el.hidden) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function bootstrap() {
    // 各块互不依赖，一个接口慢不该拖住整页；任何一个挂了也不影响其余部分
    await Promise.allSettled([loadAccess(), loadSummary(), loadAnnouncement(), loadBoard()]);
    if (access.is_admin) await loadLinks();
    scrollToHash();
  }

  document.addEventListener('DOMContentLoaded', () => {
    bindCreate(); bindCopy(); bindBoard(); bindConsole(); bindEditor();
    bindCodeCheck(); bindDupCheck(); bindPreview(); bindBulk();
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

  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('#hot-list button[data-copy]');
    if (!btn) return;
    await copyText(btn.dataset.copy);
    toast('已复制短链接');
  });

  window.SimpleURL = { renderChart };
})();
