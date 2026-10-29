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
  // 热门榜里有没有「我建的那条」（服务端凭回执认出来的）
  let boardHasMine = false;
  // 站点访问模式快照，来自 /api/access
  const access = {
    mode: 'open', admin_mode: false, is_admin: false, can_create: true, can_browse: true,
    access_required: false, access_granted: true,
  };

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
    let data = {};
    try { data = await res.json(); } catch (_) { /* 非 JSON 响应原样报错 */ }
    if (res.status === 401) {
      // 只有真正开了访问令牌的站点才搞门禁，否则这里弹出来的门根本没法过
      if (access.access_required) showGate();
      if (access.admin_mode && !access.is_admin) {
        throw new Error('站点已设为仅管理员可使用，请先登录后台');
      }
    }
    if (res.status === 403 && access.access_required) showGate();
    if (!res.ok) {
      // 把状态码挂在错误上：调用方要区分「回执作废了」和「网抖了一下」，
      // 只能靠它——消息文本是拿来给人看的，不该被当成判断依据
      const err = new Error(data.message || `请求失败（${res.status}）`);
      err.status = res.status;
      throw err;
    }
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
    // locked = 访客不能建（仅管理员模式，或口令太弱被收权）
    const locked = !access.can_create;
    document.body.dataset.mode = access.admin_mode ? 'admin' : 'open';
    document.body.dataset.auth = access.is_admin ? 'true' : 'false';

    const banner = $('#mode-banner');
    if (banner) {
      banner.hidden = false;
      banner.classList.toggle('locked', locked);
      $('#mode-text').textContent = access.locked
        ? '站点口令过弱，创建已锁定：请管理员更换 ADMIN_TOKEN（≥12 位随机串）后重试。'
        : (locked
            ? '仅管理员可使用：外部访客只能打开已生成的短链接，创建与管理需登录后台。'
            : (access.admin_mode
                ? '仅管理员可使用：你已登录，管理功能可用。'
                : '开放外部可使用：任何人都能创建短链接，链接库与统计仅管理员可见。'));
    }
    // 站点公告：后台「设置 → 站点公告」写什么就显示什么，留空整条不出现
    const notice = $('#notice-bar');
    if (notice) {
      const text = (access.announcement || '').trim();
      notice.hidden = !text;
      if (text) $('#notice-text').textContent = text;
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
      loadAccess();
      loadSummary();
      loadLinks();
    });
  }

  /* ---------- 创建前校验 ---------- */
  function setCodeStatus(msg, kind) {
    const el = $('#code-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'field-status' + (kind ? ' ' + kind : '');
    // 从「已被占用」改成「可以用」这类状态变化时，把上一次提交留下的红字收掉：
    // 短码已经换好了，底下还挂着「已被占用，换一个试试」，看着像怎么改都不行
    if (kind === 'ok' || !msg) {
      const status = $('#create-status');
      if (status && status.classList.contains('err')) {
        status.textContent = '';
        status.className = 'status';
      }
    }
  }

  // 短码边打边查：字符集与长度本地挡掉，占用情况问后端，省一次白填
  function bindCodeCheck() {
    const input = $('#code');
    if (!input) return;
    let timer = null;
    let seq = 0;
    // 短码被占用时提交键不装死：系统会当场换一个可用短码
    input.addEventListener('input', () => { input.dataset.taken = ''; });
    input.addEventListener('input', () => {
      const code = input.value.trim();
      clearTimeout(timer);
      if (!code) { setCodeStatus(''); return; }
      if (!/^[A-Za-z0-9_-]+$/.test(code)) { setCodeStatus('只能用字母、数字、- 和 _', 'err'); return; }
      if (code.length < 3) { setCodeStatus('至少 3 个字符', 'err'); return; }
      setCodeStatus('检查中…');
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const data = await api('/api/codes/' + encodeURIComponent(code) + '/available');
          if (mine !== seq) return;  // 慢一步的旧响应不许覆盖新结论
          setCodeStatus(data.message ? (data.available ? '可以用 ✓' : data.message) : '', data.available ? 'ok' : 'err');
          input.dataset.taken = data.available ? '' : '1';
        } catch (_) {
          if (mine === seq) setCodeStatus('');
        }
      }, 280);
    });
  }

  // 目标地址查重：同一页面已经收过了，就把现成的短码递过去
  function bindDupCheck() {
    const input = $('#target');
    const box = $('#dup-hint');
    const list = $('#dup-list');
    if (!input || !box || !list) return;
    // 查重只给管理员：结果是「别人也建过」的短码，访客发请求只会换回 401，
    // 控制台红一行，而他本来也看不到别人的链接
    if (!access.is_admin) { box.hidden = true; return; }
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
          const data = await api('/api/duplicate?target=' + encodeURIComponent(value));
          if (mine !== seq) return;
          if (!data.items || !data.items.length) { hide(); return; }
          list.innerHTML = data.items.map((it) => `
            <li>
              <a class="mono" href="/${encodeURIComponent(it.code)}" target="_blank" rel="noopener">/${escapeHtml(it.code)}</a>
              <span class="hint">${it.clicks} 次点击${it.expired ? ' · 已过期' : it.enabled ? '' : ' · 已停用'}</span>
              <button class="btn tiny ghost" type="button" data-copy="${escapeHtml(location.origin + '/' + it.code)}">复制</button>
            </li>`).join('');
          box.hidden = false;
        } catch (_) {
          if (mine === seq) hide();  // 访客、离线都别打扰，查重是锦上添花
        }
      }, 500);
    });

    list.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-copy]');
      if (!btn) return;
      await writeClipboard(btn.dataset.copy);
      toast('已复制短链接');
    });
  }

  /* ---------- 找回回执 ----------
     回执服务端只存哈希，丢了就真没了。留过线索的人凭线索换一张新的：
     只重签最近一条，且 1 小时 3 次，够丢的人捞回来，不够拿来扫别人的库。 */
  function bindRecover() {
    const open = $('#recover-btn');
    const box = $('#recover');
    if (open && box) {
      open.addEventListener('click', () => {
        box.hidden = !box.hidden;
        if (!box.hidden) $('#recover-clue').focus();
      });
    }
    // 空状态里那条「回执丢了？」：点了先把它露出来，再滚过去——
    // 不然按钮像是没反应（面板在上面，当前屏看不到）
    const emptyOpen = $('#empty-recover');
    if (emptyOpen && box) {
      emptyOpen.addEventListener('click', () => {
        box.hidden = false;
        box.scrollIntoView({ behavior: 'smooth', block: 'center' });
        const clue = $('#recover-clue');
        if (clue) setTimeout(() => clue.focus(), 260);
      });
    }
    const form = $('#recover-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const clue = $('#recover-clue').value.trim();
      if (!clue) return;
      const btn = $('#recover-submit');
      btn.disabled = true;
      setRecoverStatus('正在查找…');
      try {
        const data = await withToken('/api/claim/recover', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ clue }),
        });
        setRecoverStatus('找到了：' + data.code + '，回执已换新', 'ok');
        showResult({
          code: data.code,
          short_url: data.short_url,
          stats_url: data.stats_url,
          claim_token: data.claim_token,
          title: data.title,
          tags: [],
          title_source: null,
          reused: true,
        });
        rememberClaimCode(data.code);
        loadGuestLinks();
      } catch (err) {
        setRecoverStatus(err.message, 'err');
      } finally {
        btn.disabled = false;
      }
    });
  }

  function setRecoverStatus(msg, kind) {
    const el = $('#recover-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
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
        note: (($('#note') || {}).value || '').trim() || undefined,
        tags: $('#tags').value.trim() || undefined,
        // datetime-local 给的是「用户眼里的时间」，原样发；由后端按时区换算成 UTC。
        // 前端自己转 ISO 反而会多绕一圈，还容易把「本地 23:59」发成别的时刻。
        expires_at: localInputToIso($('#expires_at').value),
        clue: (($('#clue') || {}).value || '').trim() || undefined,
        reuse: Boolean(($('#reuse') || {}).checked) || undefined,
        // 留空 = 不限制。不能发 0 之外的空串，后端会当成非法整数
        daily_limit: intOrUndefined($('#daily_limit')),
      };
      if (!payload.target) { setStatus('请先粘贴一个链接', 'err'); return; }
      // 短码被占这件事，边打边查时就已经告诉过他了。真正提交到这里时
      // 不该一整屏都白填 —— 后端现在自己换码并把原码带回来（swapped_from），
      // 这里只负责说清楚「换过了」
      btn.disabled = true;
      setStatus('正在生成…');
      try {
        const link = await withToken('/api/links', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        // 兼容旧后端：换码信息优先取服务端的 swapped_from
        link._code_swapped = link.swapped_from || link._code_swapped;
        showResult(link);
        rememberClaimCode(link.code);
        loadGuestLinks();
        // 「创建成功」原来挂在按钮右边，紧挨着「回执丢了？」，读起来像第三个按钮的说明。
        // 结果卡片就在下面，戳一句状态在卡片头上更顺。
        if (link._code_swapped) {
          toast(`短码已被占用，已改用 /${link.code}`);
        } else if (link.reused) {
          toast(`已复用现成的短码 /${link.code}`);
        }
        setStatus('');
        form.reset();
        setCodeStatus('');
        const dup = $('#dup-hint');
        if (dup) dup.hidden = true;
        loadSummary();
        loadTags();
        loadLinks();
      } catch (err) {
        setStatus(err.message, 'err');
        toast(err.message);
      } finally {
        btn.disabled = false;
      }
    });
  }

  function showResult(link, showResultFresh = true) {
    const box = $('#result');
    if (!box) return;
    const a = $('#result-link');
    a.href = link.short_url;
    a.textContent = link.short_url;
    // 创建者拿到的是带领取码的统计地址，未登录也能看自己这一条
    const claim = link.claim_token ? `t=${encodeURIComponent(link.claim_token)}` : '';
    const qs = [tokenQuery().slice(1), claim].filter(Boolean).join('&');
    $('#result-qr').src = '/api/qr/' + encodeURIComponent(link.code) + (qs ? '?' + qs : '');
    const receipt = $('#receipt');
    if (receipt) {
      const hasClaim = Boolean(link.claim_token);
      receipt.hidden = !hasClaim;
      if (hasClaim) {
        const rl = $('#receipt-link');
        rl.href = link.stats_url;
        rl.textContent = link.stats_url;
      }
    }
    const titleHint = $('#result-title-hint');
    if (titleHint) {
      const map = {
        fetched: '标题已自动抓取',
        fallback: '目标页抓不到标题，已用地址兜底',
        manual: '标题按你填的来',
        off: '未开启自动取标题',
      };
      // 没有标题就别留一行解释：卡片里已经有短链接和二维码，
      // 再补一句「目标页抓不到标题」只是把失败念一遍。
      // 「标题按你填的来」也不用说——那是他自己刚打的字。
      const text = link.title && map[link.title_source] && link.title_source !== 'manual'
        ? map[link.title_source]
        : '';
      titleHint.textContent = text;
      titleHint.hidden = !text;
    }
    $('#result-tags').innerHTML = (link.tags || [])
      .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
    // 二维码右边那块只放「这一条自己是什么」：标题、备注、标签。
    // 以前只挂一句「标题已自动抓取」——没标题没标签时那里就孤零零一句话，
    // 既没说抓到了什么，也没说为什么要告诉我。
    const facts = $('.result-facts');
    if (facts) {
      const lead = facts.querySelector('.result-title');
      if (lead) lead.remove();
      if (link.title) {
        facts.insertAdjacentHTML(
          'afterbegin',
          `<p class="result-title">${escapeHtml(link.title)}</p>`,
        );
      }
      // 没标题没标签就整块收起来，别在二维码右边留一片空白
      facts.classList.toggle('is-empty', !link.title && !(link.tags || []).length);
    }
    renderSelfTools(link, showResultFresh);
    box.hidden = false;
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  /* 刚建完的那条，创建者能当场续期 / 停用 / 删除。
     以前这块只有「复制」和「二维码」：填错一个字就得重新建一条，
     过两天链接自己到期了也只能干看着。 */
  let currentClaim = { code: '', token: '' };
  // 回执只对「刚建出来的那一条」有效。列表里那几行走 cookie 里的回执，
  // 这里不能拿旧 token 去拼——拼错了服务端就当成别人的链接，一口回绝。
  let showingFresh = false;

  function renderSelfTools(link, fresh) {
    const box = $('#result-self');
    if (!box) return;
    const hasClaim = fresh && Boolean(link.claim_token);
    currentClaim = hasClaim ? { code: link.code, token: link.claim_token } : { code: link.code, token: '' };
    showingFresh = hasClaim;
    box.hidden = !hasClaim;
    if (!hasClaim) return;
    $('#result-target').textContent = link.target || '—';
    $('#result-expiry').textContent = link.expires_at
      ? '到 ' + fmtDate(link.expires_at)
      : '永不过期';
    // 备注是建的时候自己写的那句话，建完得能看见——否则这个字段等于白填
    const noteLine = $('#result-note-line');
    if (noteLine) {
      noteLine.hidden = !link.note;
      $('#result-note').textContent = link.note || '';
    }
    const toggle = $('#result-toggle');
    toggle.dataset.enabled = link.enabled ? '1' : '0';
    toggle.textContent = link.enabled ? '停用' : '重新启用';
    setSelfStatus('');
  }

  /* 结果卡上的那条被改过之后，把它自己的那几行跟着刷新一遍。
     没有回执（管理员在后台改的、或者卡片不是这一条）就什么都不做。 */
  async function refreshSelfTools(code) {
    if (!code || !$('#result-self')) return;
    try {
      const link = await api('/api/links/' + encodeURIComponent(code) + '/self');
      renderSelfTools({ ...link, claim_token: currentClaim.token }, true);
    } catch (_) {
      // 刷新失败就让卡面停在旧值：这只是一块附带的说明，不该因此报错
    }
  }

  function setSelfStatus(msg, kind) {
    const el = $('#result-self-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  async function selfCall(path, method, body) {
    // path 里可能自带 query（`/disable?enabled=false`），回执参数得接着往后拼
    const t = currentClaim.token ? `t=${encodeURIComponent(currentClaim.token)}` : '';
    let url = `/api/links/${encodeURIComponent(currentClaim.code)}/self${path}`;
    if (t) url += (path.includes('?') ? '&' : '?') + t;
    return withToken(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  function bindSelfTools() {
    const renew = $('#result-renew');
    if (!renew) return;
    renew.addEventListener('click', async () => {
      renew.disabled = true;
      setSelfStatus('续期中…');
      try {
        const data = await selfCall('/renew', 'POST', {
          days: Number(($('#result-days') || {}).value) || 30,
          enabled: true,
        });
        $('#result-expiry').textContent = data.expires_at ? '到 ' + fmtDate(data.expires_at) : '永不过期';
        setSelfStatus('已续期，到期时间往后推了', 'ok');
        toast('已续期');
      } catch (err) {
        setSelfStatus(err.message, 'err');
      } finally {
        renew.disabled = false;
      }
    });

    const toggle = $('#result-toggle');
    toggle.addEventListener('click', async () => {
      const next = toggle.dataset.enabled !== '1';
      toggle.disabled = true;
      setSelfStatus(next ? '启用中…' : '停用中…');
      try {
        const data = await selfCall(`/disable?enabled=${next ? 'true' : 'false'}`, 'POST');
        toggle.dataset.enabled = data.enabled ? '1' : '0';
        toggle.textContent = data.enabled ? '停用' : '重新启用';
        setSelfStatus(data.enabled ? '已重新启用' : '已停用：别人打不开了，凭回执你还能进来', 'ok');
        loadLinks();
      } catch (err) {
        setSelfStatus(err.message, 'err');
      } finally {
        toggle.disabled = false;
      }
    });

    const del = $('#result-del');
    del.addEventListener('click', async () => {
      if (!confirm('删除后这条短链接立刻失效，且无法恢复。确定删除？')) return;
      del.disabled = true;
      setSelfStatus('删除中…');
      try {
        await selfCall('', 'DELETE');
        setSelfStatus('已删除', 'ok');
        toast('已删除');
        const box = $('#result');
        if (box) box.hidden = true;
        if (!access.is_admin) forgetClaimCode();
        loadLinks();
      } catch (err) {
        setSelfStatus(err.message, 'err');
      } finally {
        del.disabled = false;
      }
    });
  }

  function tokenQuery() {
    return state.token ? `?token=${encodeURIComponent(state.token)}` : '';
  }

  async function loadSummary() {
    // 访客读不到链接库，总点击数对他没有意义，也没必要为此去撞一次 401
    // ——那会在控制台留下一条红色错误，看起来像页面坏了
    if (!access.is_admin) {
      summary = { total_links: 0, total_clicks: 0, access_required: access.access_required };
      return summary;
    }
    try {
      summary = await withToken('/api/stats/summary');
    } catch (_) {
      summary = { total_links: 0, total_clicks: 0 };
    }
    return summary;
  }

  /* 榜单是动态渲染的，用事件委托：不然每刷一次就要重挂一批监听 */
  function bindHotboard() {
    const list = $('#hotboard-list');
    if (!list) return;
    list.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-copy]');
      if (!btn) return;
      e.preventDefault();
      const mark = btn.querySelector('.copy-mark');
      try {
        await writeClipboard(btn.dataset.copy);
        if (btn.dataset.busy) return;
        btn.dataset.busy = '1';
        if (mark) {
          const original = mark.textContent;
          mark.textContent = '已复制 ✓';
          setTimeout(() => { mark.textContent = original; delete btn.dataset.busy; }, 1600);
        } else {
          const original = btn.textContent;
          btn.textContent = '已复制 ✓';
          setTimeout(() => { btn.textContent = original; delete btn.dataset.busy; }, 1600);
        }
      } catch (_) {
        toast('复制失败，请手动选中', 'err');
      }
    });
  }

  /* 首页热门榜：只显示短码、标题和点击数，不含目标地址。
     刻意不做成「链接库的预览」——访客没有看全站的视野，
     他只需要一眼看到「这个站上大家在用什么」，落地页才不至于空荡荡。 */
  async function loadBoard() {
    const box = $('#hotboard');
    const list = $('#hotboard-list');
    if (!box || !list) return;
    let items = [];
    try {
      const data = await withToken('/api/board');
      items = (data && data.items) || [];
    } catch (_) {
      // 榜单是锦上添花，拉不到就整块不显示，别在首页顶一个红条
      box.hidden = true;
      return;
    }
    if (!items.length) { box.hidden = true; return; }
    // 榜单里有没有「我建的那条」——访客面板的空态要靠它换一句话
    boardHasMine = items.some((item) => item.owner);
    const origin = location.origin;
    // 一行只留三样东西：名次、链接、点击数。短码本身就是「要复制的东西」，
    // 让它自己就是复制按钮，比在行尾再挂一个单独的「复制」少一次目光往返。
    // 以前的排法是标题一行、短码一行、点击数靠右、复制再一个按钮，
    // 1440 下中间空出大半屏，三样信息被拉成了一根橡皮筋。
    list.innerHTML = items.map((item, idx) => {
      const url = `${origin}/${item.code}`;
      const title = item.title ? escapeHtml(item.title) : '未命名链接';
      const tags = (item.tags || []).map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
      const mine = item.owner
        ? '<span class="hotboard-mine" title="凭回执认出来的：这条是你建的">我的</span>'
        : '';
      return `<li class="hotboard-item">
        <span class="hotboard-rank">${idx + 1}</span>
        <div class="hotboard-main">
          <a class="hotboard-title" href="${escapeHtml(url)}" target="_blank" rel="noopener">${title}</a>
          <div class="hotboard-meta">
            <button class="hotboard-code copy-code" type="button" data-copy="${escapeHtml(url)}"
              title="复制 ${escapeHtml(url)}">/${escapeHtml(item.code)}<span class="copy-mark" aria-hidden="true">复制</span></button>
            ${mine}${tags}
          </div>
        </div>
        <span class="hotboard-clicks" title="累计点击 ${item.clicks} 次">${humanCount(item.clicks)}<em>次</em></span>
      </li>`;
    }).join('');
    box.hidden = false;
    // 榜单渲染完才知道「有没有我那条」，空态文案跟着更新一次。
    // 只在空态正显示的时候刷 —— 否则会把已经有内容的「我的链接」重画成说明板。
    const empty = $('#empty');
    if ($('#list')?.classList.contains('is-guest') && empty && !empty.hidden) updateGuestEmpty(true);
  }

  /* 点击数上万之后还写全数字，一格就撑不住了 */
  function humanCount(n) {
    const num = Number(n) || 0;
    if (num < 1000) return String(num);
    if (num < 1000000) return (num / 1000).toFixed(num < 10000 ? 1 : 0).replace(/\.0$/, '') + 'k';
    return (num / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  }

  /* 写剪贴板：https 下用 Clipboard API，http 或旧浏览器退回 execCommand */
  async function writeClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
  }

  /* 复制按钮的通用行为：从某个元素取文本，复制后给 1.6 秒反馈 */
  async function copyFrom(btn, selector) {
    const el = $(selector);
    const text = (el && (el.textContent || '').trim()) || '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
    }
    const original = btn.textContent;
    btn.textContent = '已复制 ✓';
    toast('已复制到剪贴板');
    setTimeout(() => { btn.textContent = original; }, 1600);
  }

  function bindCopy() {
    const receiptBtn = $('#receipt-copy');
    if (receiptBtn) receiptBtn.addEventListener('click', () => copyFrom(receiptBtn, '#receipt-link'));
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
    if (!link.enabled) return '<span class="badge off">已停用</span>';
    // 额度用尽的链接「启用着但进不去」，只显示「已启用」等于骗人
    if (link.daily_exhausted) {
      return `<span class="badge warn" title="今天已用满 ${link.daily_limit} 次，明天零点重置">额度用尽</span>`;
    }
    return '<span class="badge on">已启用</span>';
  }

  /* 点击那一列后面挂一句额度账：限量链接最想知道的不是「点了多少」，
     而是「还剩几次」。不显示就等于要人去算。 */
  function quotaMark(link) {
    if (!link.daily_limit) return '';
    const left = Math.max(0, link.daily_limit - (link.daily_used || 0));
    const cls = left === 0 ? 'quota-mark used' : 'quota-mark';
    return `<span class="${cls}" title="今日已用 ${link.daily_used} / ${link.daily_limit}">额度 ${link.daily_used}/${link.daily_limit}</span>`;
  }

  const DAY = 86400000;

  /* 过期那一列：能说出「还剩几天」比列一个日期有用得多 */
  function expiryCell(link) {
    if (!link.expires_at) return '<span class="hint exp-none">永不过期</span>';
    const end = new Date(link.expires_at).getTime();
    if (Number.isNaN(end)) return '<span class="hint">—</span>';
    const left = end - Date.now();
    if (left <= 0) return '<span class="exp-soon">已过期</span>';
    // 「还剩几天」按向上取整算，但最后一天要说清是几小时：
    // 一个两小时后到期的链接写「不到 1 天」，和明天此刻到期看起来一样
    const days = Math.ceil(left / DAY);
    const hours = Math.floor(left / 3600000);
    const text = days > 1 ? `还剩 ${days} 天` : (hours >= 1 ? `还剩 ${hours} 小时` : '不到 1 小时');
    const cls = days <= 7 ? 'exp-soon' : 'hint';
    // 「还剩 120 天」说不清什么时候到期，把日期一并给出来
    return `<span class="${cls}">${text}</span><span class="hint exp-date">${escapeHtml(fmtDate(link.expires_at))}</span>`;
  }

  function rowOf(link) {
    const tags = (link.tags || []).map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
    const title = link.title ? `<span class="title-cell">${escapeHtml(link.title)}</span>` : '';
    // 备注是「这条链接在干什么」的自述，标题常常是抓来的、跟自己无关，所以单独一行
    const note = link.note ? `<span class="note-cell" title="${escapeHtml(link.note)}">${escapeHtml(link.note)}</span>` : '';
    // 访客板面里那条是自己建的，回执在 cookie 里；这里顺手把回执带进链接，
    // 省得统计页和二维码又各丢一次凭据
    const tq = link.claim_token
      ? `${tokenQuery()}${tokenQuery() ? '&' : '?'}t=${encodeURIComponent(link.claim_token)}`
      : tokenQuery();
    const locked = access.admin_mode && !access.is_admin;
    // 前端列表只留「访问 / 编辑 / 删除」，停用、批量这类管理动作归后台，
    // 前台一屏塞四个按钮，格子一窄就全变三个点。
    // 访客板面里的行是他自己那条，能改能删；管理员那边多一个「访问」
    const actions = locked
      ? `<span class="hint">只读</span>`
      : `<a class="btn tiny ghost" href="/${encodeURIComponent(link.code)}${tq}" target="_blank" rel="noopener">访问</a>
        <button class="btn tiny ghost" data-act="edit" data-code="${escapeHtml(link.code)}">编辑</button>
        <button class="btn tiny danger" data-act="del" data-code="${escapeHtml(link.code)}">删除</button>`;
    // data-label 供窄屏卡片式布局定位，桌面端不显示
    return `<tr>
      <td data-label="短码"><a class="code-cell" href="/s/${encodeURIComponent(link.code)}${tq}" title="查看统计">/${escapeHtml(link.code)}</a>${title}${note}</td>
      <td data-label="目标" class="target-cell" title="${escapeHtml(link.target)}">${escapeHtml(link.target)}</td>
      <td data-label="点击" class="clicks-cell">${link.clicks}${link.pending_clicks ? `<span class="hint pending-mark" title="其中 ${link.pending_clicks} 次还在内存缓冲里，几秒内落库">待结算</span>` : ''}${quotaMark(link)}</td>
      <td data-label="标签">${tags || '<span class="hint">—</span>'}</td>
      <td data-label="状态">${badgeOf(link)}</td>
      <td data-label="过期">${expiryCell(link)}</td>
      <td data-label="创建" class="created-cell">${fmtDate(link.created_at)}</td>
      <td data-label="操作" class="col-ops"><div class="ops">${actions}</div></td>
    </tr>`;
  }

  /* 链接库只给管理员。访客看到的是「创建后凭回执看自己那条」的说明板 */
  /* 访客这边最近一条还有回执的短码。回执是 cookie（`su_claim`，HttpOnly），
     脚本读不到，只有服务端知道它认不认——所以这里让服务端试一次再说。
     试成功了就把这一条摆到「我的链接」下面，省得人翻聊天记录找回执。 */
  const GUEST_LAST_KEY = 'simpleurl:last-claim-code';

  function rememberClaimCode(code) {
    try { if (code) localStorage.setItem(GUEST_LAST_KEY, code); } catch (_) { /* 隐私模式下存不了就算了 */ }
  }

  /* 回执一旦作废（自助删除、换新），本地记着的那个短码就是个死入口：
     留着它，下次一进首页就去请求一个永远 401 的接口，然后「我的链接」
     永远是空的——而刚建的那条还在。所以作废的那一刻就得一起忘掉。
     留空即忘，调用点不用各自去删 localStorage。 */
  function forgetClaimCode() {
    try { localStorage.removeItem(GUEST_LAST_KEY); } catch (_) { /* 忽略 */ }
  }

  async function loadGuestLinks() {
    const body = $('#links-body');
    if (!body) return;
    let code = '';
    try { code = localStorage.getItem(GUEST_LAST_KEY) || ''; } catch (_) { code = ''; }
    if (!code) { body.innerHTML = ''; updateGuestEmpty(true); return; }
    try {
      const link = await api('/api/links/' + encodeURIComponent(code) + '/self');
      body.innerHTML = rowOf({ ...link, claim_token: null });
      updateGuestEmpty(false);
      $('#empty').hidden = true;
    } catch (err) {
      // 回执过期、被换新、或者换了台设备：都退回说明板，不吓人。
      // 但只有服务端**明确回绝**（401/404）才算作废——把网络抖动、
      // 5xx 也当成「回执没了」，会把这台设备上唯一的入口直接抹掉，
      // 而那张回执其实还好好的。
      body.innerHTML = '';
      if (err && (err.status === 401 || err.status === 404)) forgetClaimCode();
      updateGuestEmpty(true);
    }
  }

  function updateGuestEmpty(show) {
    const empty = $('#empty');
    if (!empty) return;
    empty.hidden = !show;
    if (!show) return;
    // 这段文案以前只是「说明这里为什么是空的」，看完还是不知道该干什么。
    // 访客手上那张回执丢了是常态（换了设备、清了缓存），所以直接把路给出来。
    // 榜上已经标出「我的」了，就别再说「这里什么都没有」——
    // 一句「你那条在上面」比一句解释有用。
    empty.querySelector('p').textContent = access.admin_mode
      ? '站点仅管理员可使用，登录后台后查看自己建过的链接'
      : (boardHasMine
        ? '你刚建的那条就在上面的热门榜里，「我的」标记那条。点短码即可复制，打开统计看点击。'
        : '这里放的是你建过的链接。没登录时只记得最近那一条的凭据——凭回执能看它、也能改它。');
    // 空着的时候最需要那条出口，别让人再滚回上面去找「回执丢了？」
    const rec = document.querySelector('#empty-recover');
    if (rec) rec.hidden = Boolean(access.admin_mode);
  }

  function renderGuestBoard() {
    const body = $('#links-body');
    if (body) body.innerHTML = '';
    const list = $('#list');
    if (list) list.classList.add('is-guest');
    const head = document.querySelector('#list .board-head h2');
    if (head) head.textContent = '我的链接';
    updateGuestEmpty(true);
    loadGuestLinks();
    ['#summary', '#page-info'].forEach((sel) => { const el = $(sel); if (el) el.innerHTML = ''; });
    ['#prev-btn', '#next-btn', '#export-btn', '#search-input', '#status-filter', '#sort-select', '#page-size'].forEach((sel) => {
      const el = $(sel); if (el) el.disabled = true;
    });
    // 访客没有链接库，翻页控件留着只是给人点着玩
    const pager = document.querySelector('.pager');
    if (pager) pager.hidden = true;
  }

  async function loadLinks() {
    // 工具页、统计页也加载了这份脚本，但页面上没有链接库。
    // 没有就先退出去：不然一进 /tools 控制台就报一个空指针，
    // 看着像页面坏了，实际什么都没坏。
    const list = $('#list');
    if (!list) return;
    const pager = document.querySelector('.pager');
    if (!access.is_admin) { renderGuestBoard(); return; }
    list.classList.remove('is-guest');
    const boardTitle = document.querySelector('#list .board-head h2');
    if (boardTitle) boardTitle.textContent = '链接库';
    if (pager) pager.hidden = false;
    const body = $('#links-body');
    if (!body) return;
    const params = new URLSearchParams({ page: state.page, page_size: state.pageSize, sort: state.sort });
    if (state.q) params.set('q', state.q);
    if (state.tag) params.set('tag', state.tag);
    if (state.status) params.set('status', state.status);
    // 访客没有链接库：这两条接口对他是 403/401，控制台白响两声
    if (!access.is_admin) {
      renderGuestBoard();
      return;
    }
    try {
      const data = await withToken('/api/links?' + params.toString());
      state.total = data.total;
      body.innerHTML = data.items.map(rowOf).join('');
      const empty = $('#empty');
      empty.hidden = data.items.length > 0;
      empty.querySelector('p').textContent = '还没有链接，先创建一个吧';
      // 数据有了，「链接库仅管理员可见」那条说明就不能继续挂着
      if (data.items.length) empty.hidden = true;
      const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
      $('#summary').innerHTML = [
        `<span class="chip">共 ${data.total} 条</span>`,
        `<span class="chip">总点击 ${summary.total_clicks || 0}</span>`,
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

  /* 标签筛选下拉：选项来自库里的真实标签，不再写死几个假选项 */
  async function loadTags() {
    const sel = $('#tag-filter');
    if (!sel) return;
    // 标签下拉是给链接库筛的，访客既没有库也没有这个下拉，别去问一个他拿不到的接口
    if (!access.is_admin) {
      sel.innerHTML = '<option value="">全部标签</option>';
      return;
    }
    try {
      const data = await withToken('/api/tags');
      const items = data.items || [];
      const current = state.tag;
      sel.innerHTML = '<option value="">全部标签</option>' + items
        .map((t) => `<option value="${escapeHtml(t.tag)}">${escapeHtml(t.tag)}（${t.count}）</option>`)
        .join('');
      // 刷新后把选中的标签还原，别让筛选条件在翻页/重载时丢掉
      sel.value = current;
      if (sel.value !== current) { sel.value = ''; state.tag = ''; }
      sel.disabled = false;
    } catch (_) {
      sel.innerHTML = '<option value="">全部标签</option>';
    }
  }

  /* 从别的页面点「链接库」进来时是 `/#list`：浏览器在脚本渲染列表之前就去找锚点，
     找不到就停在页面顶部，看着像没跳。DOM 稳了再补一次滚动 */
  function scrollToHash() {
    if (!location.hash) return;
    const el = document.querySelector(location.hash);
    if (el) el.scrollIntoView({ block: 'start' });
  }

  function bindBoard() {
    const body = $('#links-body');
    if (!body) return;
    let t = null;
    $('#search-input').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { state.q = e.target.value.trim(); state.page = 1; loadLinks(); }, 260);
    });
    const tagSel = $('#tag-filter');
    if (tagSel) tagSel.addEventListener('change', (e) => { state.tag = e.target.value; state.page = 1; loadLinks(); });
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
      // 管理员操作全量接口；访客只能碰自己那条，走 self 接口（凭 cookie 里的回执）
      const base = access.is_admin
        ? '/api/links/' + encodeURIComponent(code)
        : '/api/links/' + encodeURIComponent(code) + '/self';
      try {
        if (btn.dataset.act === 'del') {
          // 管理员删的直接进回收站，30 天内能捞回来；访客自助删除仍是一次性的
          const hint = access.is_admin
            ? '（会先进回收站，30 天内可恢复）'
            : '（不可恢复）';
          if (!confirm(`确定删除 /${code} ？${hint}`)) return;
          await withToken(base, { method: 'DELETE' });
          toast(access.is_admin ? `/${code} 已进回收站` : `已删除 /${code}`);
        } else if (btn.dataset.act === 'edit') {
          if (!access.is_admin) { await openGuestEdit(code); return; }
          const detail = await withToken(base);
          openEdit(detail, false);
          return;
        } else {
          const current = await withToken(base);
          await withToken(base, {
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
  /* 访客点自己那条的「编辑」时，用的还是这个弹窗，只是请求打到 self 接口上 */
  let editingGuest = false;

  async function openGuestEdit(code) {
    try {
      const link = await api('/api/links/' + encodeURIComponent(code) + '/self');
      openEdit(link, true);
      // 访客改不了启停，弹窗里那两个按钮就藏起来
      document.querySelectorAll('#edit-modal [data-admin-only]').forEach((el) => { el.hidden = true; });
    } catch (err) {
      toast(err.message);
    }
  }

  /* 传 guest=true 表示这是访客改自己那条：弹窗照旧，接口换成 self。
     别靠 link.claim_token 判——列表里那几行本来就没有它，鉴权靠的是 cookie。 */
  function openEdit(link, guest) {
    editing = link;
    editingGuest = Boolean(guest);
    const modal = $('#edit-modal');
    if (!modal) return;
    $('#edit-code').textContent = '/' + link.code;
    $('#edit-target').value = link.target || '';
    $('#edit-title').value = link.title || '';
    $('#edit-note').value = link.note || '';
    $('#edit-tags').value = (link.tags || []).join(', ');
    $('#edit-expires').value = link.expires_at ? toLocalInput(link.expires_at) : '';
    const dailyInput = $('#edit-daily');
    if (dailyInput) {
      dailyInput.value = link.daily_limit ? String(link.daily_limit) : '';
      // 访客凭回执改不了额度，把输入框锁上而不是让他改完发现没生效
      dailyInput.disabled = Boolean(guest);
      const st = $('#edit-daily-status');
      if (st) {
        st.textContent = guest
          ? '额度由管理员设置'
          : (link.daily_limit ? `今日已用 ${link.daily_used} 次` : '0 表示不限制');
        st.className = 'field-status' + (guest ? ' hint' : '');
      }
    }
    $('#edit-status').textContent = '';
    $('#edit-status').className = 'status';
    document.querySelectorAll('#edit-modal [data-admin-only]').forEach((el) => { el.hidden = editingGuest; });
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function closeEdit() {
    const modal = $('#edit-modal');
    if (!modal) return;
    modal.hidden = true;
    document.body.style.overflow = '';
    editing = null;
    editingGuest = false;
  }

  /* datetime-local 的本地串 -> 带时区偏移的 ISO。
     `2026-12-31T23:59` 交给 `new Date()` 在各家浏览器上会被当成 UTC 或本地，
     Safari 上干脆 Invalid Date。自己补偏移，语义就只有一种：用户填的就是他眼里的时间。 */
  function localInputToIso(value) {
    if (!value) return undefined;
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return undefined;
    const pad = (n) => String(n).padStart(2, '0');
    const off = -d.getTimezoneOffset();
    const sign = off >= 0 ? '+' : '-';
    const abs = Math.abs(off);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:00`
      + `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
  }

  /* 数字输入框 -> 非负整数 / undefined。
     空串表示「不限制」，但空串直接发出去会被后端当非法整数，所以这里统一收口。 */
  function intOrUndefined(el) {
    if (!el) return undefined;
    const raw = String(el.value || '').trim();
    if (!raw) return undefined;
    const num = Number(raw);
    if (!Number.isFinite(num) || num < 0) return undefined;
    return Math.floor(num);
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
        note: $('#edit-note').value.trim() || null,
        tags: $('#edit-tags').value.trim() || null,
      };
      if (!payload.target) { setEditStatus('目标链接不能为空', 'err'); return; }
      // 留空表示清除过期时间（显式 null），填了就是本地时间
      payload.expires_at = localInputToIso($('#edit-expires').value) || null;
      // 每日上限只有管理员能改：访客的 self 接口不收这个字段
      const daily = intOrUndefined($('#edit-daily'));
      if (!editingGuest && daily !== undefined) payload.daily_limit = daily;
      const saveBtn = $('#edit-save');
      saveBtn.disabled = true;
      setEditStatus('保存中…');
      try {
        // 管理员走管理接口；访客凭回执走 self 接口，两者字段一致
        const url = editingGuest
          ? '/api/links/' + encodeURIComponent(editing.code) + '/self'
          : '/api/links/' + encodeURIComponent(editing.code);
        await withToken(url, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast(`/${editing.code} 已更新`);
        closeEdit();
        loadSummary();
        loadLinks();
        // 改完额度，顶部那张结果卡还是旧的：把额度从 1 提到 20 之后，
        // 上面依旧挂着「额度用尽」——同一屏里两个说法，人只会信那个红的。
        refreshSelfTools(editing.code);
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
  // 纵轴刻度：原来只画柱子，不给任何量纲。1 次和 100 次看起来是同一种东西，
  // 一根顶天的柱子到底是 3 还是 300，只能靠 hover 猜。
  // 刻度用「好看的步长」（1/2/5×10ⁿ），上限永远包住最大值。
  // 原来固定 3 段 + Math.round 取步长：峰值 55 会算成 60，最高的柱子直接顶满，
  // 和 60 长得一模一样，只能靠 hover 才知道差 5。现在步长只从 1/2/5/10 里取
  // （取不小于的档，不四舍五入），并且必须让 max 装进 ≤ count 段里。
  function axisTicks(max, count = 4) {
    const value = Math.max(1, Math.ceil(max));
    const nice = [1, 2, 5, 10];
    let step = 1;
    let top = 1;
    // 从小到大试：第一个「段数不超过 count」的组合就是想要的
    for (let mag = 1; mag <= 1e12; mag *= 10) {
      for (const n of nice) {
        const candidate = n * mag;
        const span = Math.ceil(value / candidate) * candidate;
        if (span / candidate <= count) {
          step = candidate;
          top = span;
          return { top, ticks: buildTicks(top, step) };
        }
      }
    }
    return { top: value, ticks: buildTicks(value, value) };
  }

  function buildTicks(top, step) {
    const ticks = [];
    for (let v = 0; v <= top + 1e-9; v += step) ticks.push(Math.round(v));
    return ticks;
  }

  function renderChart(el, daily) {
    const peak = Math.max(0, ...daily.map((d) => d.count));
    // 一根柱子都没有：画出来就是一条贴着底边的虚线，看着像图表没加载完
    if (!peak) {
      el.innerHTML = '<div class="chart-empty">这段时间还没有点击</div>';
      return;
    }
    const { top, ticks } = axisTicks(peak);
    // 刻度数字都是 translateY(50%) 居中的：贴底那格有一半落到框外，
    // 顶上那格同样会探出去——被上面的标题压掉上半截，只剩一道横。
    // 首尾两格各让出半个字高，中间几格照旧。
    const axisPad = 6;
    const labels = chartLabels(el.parentElement.clientWidth, daily);
    // 柱高按刻度上限算，不再按最大值——否则「最高的一根」永远是 100%，
    // 加上坐标轴之后数字会和视觉对不上
    const bars = daily
      .map((d, i) => {
        // 有数据的柱子保底 2%：1 次点击在 160px 高的柱区里只有 3.2px，
        // 刚好看得见；保底再放大就会失真——几次点击的柱子和十几次的一样高
        const h = d.count ? Math.max(2, (d.count / top) * 100) : 0;
        // 零点不画虚线：30 天里有 29 天没点击时，那是一排贴底的短横，
        // 看着像图表没渲染完。零柱本来就没有高度，留个空位对齐就够了
        const fill = d.count ? `<i style="height:${h}%"></i>` : '<i class="zero"></i>';
        // 有标签才叫日期行：14 天宽屏下每根都有日期，30 天窄屏下只有几根有
        const day = labels.has(i) ? `<b>${labels.get(i)}</b>` : '';
        return `<div class="bar${d.count ? '' : ' is-zero'}${day ? ' has-label' : ''}" title="${d.date}：${d.count} 次点击">${fill}${day}</div>`;
      })
      .join('');
    el.innerHTML =
      `<div class="chart-axis" aria-hidden="true">${ticks
        .slice()
        .reverse()
        .map((v) => {
          const pct = (v / top) * 100;
          // 顶部那格往上让、底部那格往下让，其余照旧居中。
          // 用 >= 1 而不是真值判断：零刻度是 0，`v ? ... : ...` 会把它
          // 也当成「中间的格子」，贴底那半截数字就探到框外去了。
          const shift = v === top ? -axisPad : (v >= 1 ? 0 : axisPad);
          return `<span style="bottom:${pct}%;${shift ? `transform:translateY(${shift}px)` : ''}">${v}</span>`;
        })
        .join('')}</div><div class="chart-bars">${bars}</div>`;
    // 柱子撑出容器了（手机上 30 天放不下）：默认停在最右端。
    // 不这么做的话，一打开看到的是 30 天前那段——那几天大多是零，
    // 图上一片空白，而「今天的点击」恰好在滚动区外面。
    scrollChartToLatest(el);
  }

  function scrollChartToLatest(el) {
    const wrap = el.parentElement;
    if (!wrap) return;
    const axis = el.querySelector('.chart-axis');
    const keepAxis = () => {
      if (axis) axis.style.transform = `translateX(${wrap.scrollLeft}px)`;
    };
    const scrollable = wrap.scrollWidth > wrap.clientWidth + 1;
    // 「左右滑动」这句只在真滚得动时出现，桌面端不该白占一行
    const tip = wrap.parentElement && wrap.parentElement.querySelector('.chart-swipe');
    if (tip) tip.hidden = !scrollable;
    if (!scrollable) return;
    // 柱子撑出去了：窄屏该先看到「今天」，而不是 30 天前那几根零。
    // 但纵轴刻度留在左边不动，否则那块数值就跟柱子一起滚没了。
    // 边距补丁：日期挂在柱子底下，会往外溢出几像素，去掉它反而把滚动条去掉、
    // 让 scrollWidth 刚好等于 clientWidth。
    if (wrap.scrollLeft > 0 && !wrap.dataset.snapped) {
      wrap.dataset.snapped = '1';
      wrap.scrollLeft = wrap.scrollWidth;
    }
    if (!wrap.dataset.axisBound) {
      wrap.dataset.axisBound = '1';
      wrap.addEventListener('scroll', keepAxis, { passive: true });
    }
    wrap.scrollLeft = wrap.scrollWidth;
    keepAxis();
  }

  // 柱子宽度够就每根都标日期，不够就抽稀；窄到抽稀也放不下（手机竖屏 30 根柱子），
  // 只留首尾两天——中间全是糊字，不如让人先看懂趋势形状。
  function chartLabels(width, daily) {
    const out = new Map();
    const last = daily.length - 1;
    // 柱子连缝留 26px：日期不居中、从柱右沿往回写，「10-25」实测 29px，
    // 错开一格正好够看
    // 每根柱子额外吃 6px 的间距（.chart-bars 的 gap）。只按标签宽算的话
    // 30 根柱子会在 390px 上挤出十几像素的横向滚动——图上看着好好的，
    // 但整页多了一条能左右晃的缝
    const per = 26;
    if (width / Math.max(1, daily.length) >= per * 2.2) {
      daily.forEach((d, i) => out.set(i, d.date.slice(5)));
      return out;
    }
    const step = Math.ceil(daily.length / Math.max(2, Math.floor(width / (per * 2.2))));
    daily.forEach((d, i) => {
      if (i === 0 || i === last || i % step === 0) out.set(i, d.date.slice(5));
    });
    return out;
  }

  /* ---------- 启动 ---------- */
  // 从后台（新标签页）切回来时刷新一次：那边删了/改了链接，这边还是旧的会误导人
  document.addEventListener('DOMContentLoaded', async () => {
    bindCreate(); bindCopy(); bindSelfTools(); bindBoard(); bindEdit(); bindGate(); bindRecover();
    bindHotboard();
    bindCodeCheck(); bindDupCheck();
    await loadAccess();
    // 本站开了访问令牌而这位还没过门禁：先把门摆出来，别先拉一屏空数据再弹窗
    if (access.access_required && !access.access_granted) {
      showGate();
      return;
    }
    // 没有链接库的页面（工具页、统计页）根本不需要这几个请求，
    // 少发三个 GET，也少三条可能报错的地方
    if ($('#list')) {
      // 榜单先拉：「我的链接」空着时的文案要知道榜上有没有自己那条。
      // 两个请求本来就并行，只是别再让空态文案抢在榜单前面写下来。
      loadBoard();
      loadSummary(); loadTags(); loadLinks();
      scrollToHash();
    }
  });

  window.SimpleURL = { renderChart, chartLabels, axisTicks, scrollChartToLatest };
})();
