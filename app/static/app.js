/* 局域网文件传输 —— 前端逻辑
 *
 * 分工：
 *   发送：选文件/文件夹 → 展开清单 → 容量预检 → 逐个流式上传（带进度）
 *   接收：任务列表实时刷新，速度进度由服务端推送
 *   WebSocket 失败时自动降级为轮询，保证任何环境都能用
 */
'use strict';

const App = (() => {

  // ------------------------------------------------------ 状态

  const state = {
    status: null,          // /api/status 结果
    drives: [],
    recvDir: '',
    queue: [],             // 待发送项 [{path, rel_path, size, file?}]
    tasks: [],
    summary: {},
    es: null,              // EventSource
    polling: null,
    uploading: false,
    token: '',             // 房间令牌
    isOwner: false,        // 是不是房主
    roomCode: '',          // 仅房主可见
    targetPeer: '',        // 定向收件人
    authed: false,
  };

  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------ 令牌存取

  const TOKEN_KEY = 'lanfile_room_token';

  function loadToken() {
    // 优先级：URL hash（本机自动登录）> localStorage
    const m = /[#&]token=([^&]+)/.exec(location.hash || '');
    if (m) {
      const t = decodeURIComponent(m[1]);
      try { localStorage.setItem(TOKEN_KEY, t); } catch (e) { /* 忽略 */ }
      // 用完就把 hash 清掉，别留在地址栏里被截图泄露
      try { history.replaceState(null, '', location.pathname); } catch (e) { /* 忽略 */ }
      return t;
    }
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }

  function saveToken(t) {
    state.token = t;
    try { localStorage.setItem(TOKEN_KEY, t); } catch (e) { /* 忽略 */ }
  }

  function clearToken() {
    state.token = '';
    state.authed = false;
    try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* 忽略 */ }
  }

  // ------------------------------------------------------ 工具

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    const u = ['KB', 'MB', 'GB', 'TB', 'PB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(1) + ' ' + u[i];
  }

  function fmtSpeed(n) {
    return fmtSize(n) + '/s';
  }

  function fmtEta(sec) {
    if (sec === null || sec === undefined || !isFinite(sec)) return '—';
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + ' 秒';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h + ' 时 ' + m + ' 分';
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(msg, kind) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast' + (kind ? ' ' + kind : '');
    clearTimeout(toast._tm);
    toast._tm = setTimeout(() => t.classList.add('hidden'), 3200);
  }

  async function api(path, opts) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, (opts || {}).headers || {});
    if (state.token) headers['X-Room-Token'] = state.token;

    const r = await fetch(path, Object.assign({}, opts || {}, { headers }));
    let data = {};
    try {
      data = await r.json();
    } catch (e) {
      if (r.status === 401) { showGate('登录已过期，请重新输入房间码'); return { ok: false }; }
      return { ok: false, error: '服务端返回异常 (HTTP ' + r.status + ')' };
    }
    // 401 一律回到输码界面 —— 令牌失效 / 被房主踢出都走这里
    if (r.status === 401 && data.code === 'NEED_ROOM_CODE') {
      clearToken();
      showGate();
      return { ok: false, needCode: true };
    }
    return data;
  }

  // ------------------------------------------------------ 初始化

  async function init() {
    bindEvents();
    state.token = loadToken();

    // 先问服务端「我是否已认证」，未认证就弹输码界面
    const ping = await fetch('/api/ping', {
      headers: state.token ? { 'X-Room-Token': state.token } : {},
    }).then((r) => r.json()).catch(() => null);

    if (!ping) {
      toast('无法连接本机服务，请确认程序正在运行', 'err');
      return;
    }

    if (!ping.authed) {
      clearToken();
      showGate();
      return;
    }

    await afterAuth();
  }

  async function afterAuth() {
    state.authed = true;
    $('gate').classList.add('hidden');
    await loadStatus();
    await loadSettings();
    await loadRoom();
    connectUpdates();
    refreshDriveSpace();
    setInterval(refreshDriveSpace, 15000);
    if (!state._driveTimer) state._driveTimer = true;
  }

  // ------------------------------------------------------ 房间码登录

  function showGate(msg) {
    $('gate').classList.remove('hidden');
    const err = $('gateErr');
    if (msg) {
      err.textContent = msg;
      err.classList.remove('hidden');
    } else {
      err.classList.add('hidden');
    }
    const inp = $('gateCode');
    inp.value = '';
    setTimeout(() => inp.focus(), 80);
  }

  async function doJoin() {
    const code = $('gateCode').value.trim();
    const name = $('gateName').value.trim();
    const err = $('gateErr');
    const btn = $('gateBtn');

    if (!code) {
      err.textContent = '请输入房间码';
      err.classList.remove('hidden');
      return;
    }

    btn.disabled = true;
    btn.textContent = '连接中…';
    try {
      const r = await fetch('/api/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code, nickname: name }),
      });
      const data = await r.json();
      if (!data.ok) {
        err.textContent = data.error || '连接失败';
        err.classList.remove('hidden');
        $('gateCode').value = '';
        $('gateCode').focus();
        return;
      }
      saveToken(data.token);
      err.classList.add('hidden');
      await afterAuth();
      toast('已连接', 'ok');
    } catch (e) {
      err.textContent = '网络异常，请重试';
      err.classList.remove('hidden');
    } finally {
      btn.disabled = false;
      btn.textContent = '连接';
    }
  }

  // ------------------------------------------------------ 房间信息

  async function loadRoom() {
    const r = await api('/api/room');
    if (!r.ok) return;
    state.isOwner = !!r.is_owner;
    state.roomCode = r.room_code || '';
    state.targetPeer = r.target_peer || '';
    state.allowGuests = r.allow_guests !== false;
    state.members = r.members || [];
    state.myNickname = r.nickname || state.myNickname || '';
    state.myPeer = r.peer || state.myPeer || '';
    $('btnRoom').textContent = state.isOwner ? '房间成员' :
      (state.myNickname ? '我：' + state.myNickname : '房间成员');
  }

  function bindEvents() {
    $('btnQr').onclick = showQrModal;
    $('btnRoom').onclick = showRoomModal;
    $('btnSettings').onclick = showSettingsModal;
    $('btnScan').onclick = scanDevices;
    $('btnPickFiles').onclick = () => $('fileInput').click();
    $('btnPickFolder').onclick = () => $('folderInput').click();
    $('btnBrowse').onclick = showBrowseModal;
    $('fileInput').onchange = (e) => {
      addFilesFromInput(Array.from(e.target.files || []), false);
      e.target.value = '';
    };
    $('folderInput').onchange = (e) => {
      addFilesFromInput(Array.from(e.target.files || []), true);
      e.target.value = '';
    };
    $('btnSendAll').onclick = sendAll;
    $('btnClearQueue').onclick = () => { state.queue = []; renderQueue(); };
    $('btnChangeDir').onclick = showDirPickerModal;
    $('btnOpenDir').onclick = openRecvDir;
    $('btnClearTasks').onclick = clearTasks;
    $('modalClose').onclick = closeModal;
    $('modal').onclick = (e) => { if (e.target.id === 'modal') closeModal(); };
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

    // 房间码登录
    $('gateBtn').onclick = doJoin;
    $('gateCode').onkeydown = (e) => { if (e.key === 'Enter') doJoin(); };
    $('gateName').onkeydown = (e) => { if (e.key === 'Enter') doJoin(); };
    $('gateCode').oninput = () => {
      // 自动转大写、剥离分隔符，用户怎么输都行
      const v = $('gateCode').value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 6);
      if (v !== $('gateCode').value) $('gateCode').value = v;
    };

    setupDropZone();
  }

  // ------------------------------------------------------ 拖拽

  function setupDropZone() {
    const dz = $('dropZone');
    const stop = (e) => { e.preventDefault(); e.stopPropagation(); };

    ['dragenter', 'dragover'].forEach((ev) =>
      dz.addEventListener(ev, (e) => { stop(e); dz.classList.add('over'); }));

    ['dragleave', 'drop'].forEach((ev) =>
      dz.addEventListener(ev, (e) => { stop(e); dz.classList.remove('over'); }));

    dz.addEventListener('drop', async (e) => {
      const items = e.dataTransfer.items;
      // 优先用 webkitGetAsEntry 拿到目录结构（Chrome/Edge）
      if (items && items.length && items[0].webkitGetAsEntry) {
        const entries = [];
        for (const it of items) {
          const en = it.webkitGetAsEntry && it.webkitGetAsEntry();
          if (en) entries.push(en);
        }
        if (entries.length) {
          toast('正在读取拖入的文件…');
          const files = [];
          for (const en of entries) await walkEntry(en, '', files);
          addFileObjects(files);
          return;
        }
      }
      addFileObjects(Array.from(e.dataTransfer.files || []));
    });

    // 整个页面拖拽也拦住，避免浏览器直接打开文件
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => e.preventDefault());
  }

  // 递归读取拖入的目录（拖文件夹时浏览器不自动展开，得手动走 entry API）
  function walkEntry(entry, prefix, out) {
    return new Promise((resolve) => {
      if (entry.isFile) {
        entry.file((f) => {
          out.push({ file: f, rel: prefix + entry.name });
          resolve();
        }, () => resolve());
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        const all = [];
        const readBatch = () => {
          reader.readEntries(async (batch) => {
            if (!batch.length) {
              for (const en of all) await walkEntry(en, prefix + entry.name + '/', out);
              resolve();
              return;
            }
            all.push(...batch);
            readBatch();
          }, () => resolve());
        };
        readBatch();
      } else {
        resolve();
      }
    });
  }

  function addFilesFromInput(files, isFolder) {
    const items = files.map((f) => ({
      file: f,
      // webkitRelativePath 会带上文件夹名，正好保留目录结构
      rel: (isFolder && f.webkitRelativePath) ? f.webkitRelativePath : f.name,
    }));
    addFileObjects(items);
  }

  function addFileObjects(items) {
    if (!items.length) return;
    let added = 0;
    for (const it of items) {
      const size = it.file.size || 0;
      // 去重：同路径同大小视为重复
      if (state.queue.some((q) => q.rel_path === it.rel && q.size === size)) continue;
      state.queue.push({ rel_path: it.rel, size: size, file: it.file });
      added++;
    }
    renderQueue();
    if (added) toast('已添加 ' + added + ' 个文件', 'ok');
    else toast('文件已在列表中');
  }

  function renderQueue() {
    const box = $('sendQueue');
    const list = $('queueList');
    if (!state.queue.length) {
      box.classList.add('hidden');
      return;
    }
    box.classList.remove('hidden');
    const total = state.queue.reduce((s, q) => s + q.size, 0);
    $('queueInfo').textContent =
      state.queue.length + ' 个文件，共 ' + fmtSize(total);

    list.innerHTML = state.queue.map((q, i) => `
      <li>
        <span class="fname" title="${esc(q.rel_path)}">${esc(q.rel_path)}</span>
        <span class="fsize">${fmtSize(q.size)}</span>
        <button class="fdel" data-i="${i}" title="移除" type="button">×</button>
      </li>`).join('');

    list.querySelectorAll('.fdel').forEach((b) => {
      b.onclick = () => {
        state.queue.splice(Number(b.dataset.i), 1);
        renderQueue();
      };
    });
  }

  // ------------------------------------------------------ 状态与更新

  async function loadStatus() {
    const s = await api('/api/status');
    if (!s.ok) { toast('无法连接本机服务', 'err'); return; }
    state.status = s;
    state.recvDir = s.recv_dir;
    const host = s.hostname ? s.hostname + ' · ' : '';
    $('connInfo').textContent =
      host + s.local_ip + ':' + s.port + '  ·  局域网内其他设备访问此地址';
    renderRecvDir();
  }

  async function loadSettings() {
    const s = await api('/api/settings');
    if (s.ok) {
      state.recvDir = s.recv_dir;
      state.drives = s.drives || [];
      renderRecvDir();
    }
  }

  function renderRecvDir() {
    $('recvDir').textContent = state.recvDir || '—';
    $('recvDir').title = state.recvDir || '';
  }

  async function refreshDriveSpace() {
    const r = await api('/api/drives');
    if (!r.ok) return;
    state.drives = r.drives || [];
    // 找到接收目录所在分区
    const cur = state.recvDir.toLowerCase();
    let drive = state.drives.find((d) => cur.startsWith(d.path.toLowerCase()));
    if (!drive && state.drives.length) drive = state.drives[0];
    if (!drive) return;

    const pct = 100 - drive.free_pct;
    const bar = $('capBar');
    bar.style.width = Math.min(100, pct) + '%';
    bar.className = 'cap-fill' + (drive.free_pct < 5 ? ' err' : drive.free_pct < 15 ? ' warn' : '');
    $('capText').textContent =
      '可用 ' + fmtSize(drive.free) + ' / 共 ' + fmtSize(drive.total) +
      '（' + drive.free_pct + '%）';

    const warn = $('capWarn');
    if (drive.free_pct < 5) {
      warn.textContent = '磁盘剩余空间不足 5%，接收大文件可能失败，建议更换保存目录';
      warn.classList.remove('hidden');
    } else {
      warn.classList.add('hidden');
    }
  }

  // 实时更新：优先 SSE（兼容性最好），失败则轮询
  function connectUpdates() {
    try {
      // EventSource 不能带自定义请求头，靠登录时写入的 Cookie 认证。
      // 服务端会在无有效 Cookie 时返回 401，此时降级为轮询（轮询能带令牌头）。
      const es = new EventSource('/ws');
      state.es = es;
      es.onmessage = (e) => {
        try { applySnapshot(JSON.parse(e.data)); } catch (err) { /* 忽略坏包 */ }
        // SSE 一旦通上，就停掉兜底轮询
        if (state.polling) { clearInterval(state.polling); state.polling = null; }
      };
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) {
          es.close();
          state.es = null;
          startPolling();
        } else {
          // 连接中/重连中：先启用轮询保证界面不断更
          startPolling();
        }
      };
    } catch (err) {
      startPolling();
    }
  }

  function startPolling() {
    if (state.polling) return;
    state.polling = setInterval(async () => {
      const s = await api('/api/tasks');
      applySnapshot(s);
    }, 800);
  }

  function applySnapshot(snap) {
    if (!snap || !snap.tasks) return;
    state.tasks = snap.tasks;
    state.summary = snap.summary || {};
    if (snap.recv_dir) state.recvDir = snap.recv_dir;
    renderTasks();
    renderSummary();

    // 房间状态可能被别人改动（房主改码/踢人），定期同步
    if (snap.auth) {
      if (snap.auth.allow_guests === false && !state.isOwner) {
        // 房主关闭了外部连接 —— 立刻退回登录界面
        clearToken();
        showGate('房主已关闭外部连接');
        return;
      }
      state.targetPeer = snap.auth.target_peer || state.targetPeer;
    }
  }

  function renderSummary() {
    const s = state.summary;
    $('upSpeed').textContent = fmtSpeed(s.upload_speed || 0);
    $('downSpeed').textContent = fmtSpeed(s.download_speed || 0);
    $('activeCount').textContent = (s.active || 0) + (s.pending || 0);
  }

  const STATUS_TEXT = {
    running: '传输中', done: '已完成', failed: '失败',
    canceled: '已取消', pending: '等待中', paused: '已暂停',
  };

  function renderTasks() {
    const list = $('taskList');
    const empty = $('emptyTasks');

    if (!state.tasks.length) {
      empty.classList.remove('hidden');
      list.innerHTML = '';
      return;
    }
    empty.classList.add('hidden');

    // 倒序：最新的在最上面
    const tasks = state.tasks.slice().reverse();

    list.innerHTML = tasks.map((t) => {
      const isUp = t.direction === 'upload';
      const dirText = isUp ? '接收' : '发送';
      const dirCls = isUp ? 'up' : 'down';
      const pct = Math.max(0, Math.min(100, t.progress || 0));
      const fillCls = t.status === 'done' ? 'done' : t.status === 'failed' ? 'failed' : '';
      const canCancel = t.status === 'running' || t.status === 'pending';

      let meta = `<span>${esc(t.transferred_text)} / ${esc(t.size_text)}</span>`;
      if (t.status === 'running' || t.status === 'done') {
        meta += `<span>速度 <b>${fmtSpeed(t.speed)}</b></span>`;
      }
      if (t.status === 'running' && t.eta != null) {
        meta += `<span>剩余 <b>${fmtEta(t.eta)}</b></span>`;
      }
      if (isUp && t.final_path) {
        meta += `<span>已存至 <b>${esc(shortPath(t.final_path))}</b></span>`;
      }

      return `
        <li class="task" data-id="${esc(t.task_id)}">
          <div class="task-head">
            <span class="task-dir ${dirCls}">${dirText}</span>
            <span class="task-name" title="${esc(t.rel_path)}">${esc(t.name || t.rel_path)}</span>
            <span class="task-size">${esc(t.size_text)}</span>
            <span class="task-status ${t.status}">${STATUS_TEXT[t.status] || t.status}</span>
            ${canCancel ? `<button class="btn small danger" data-cancel="${esc(t.task_id)}" type="button">取消</button>` : ''}
          </div>
          <div class="task-bar"><div class="task-fill ${fillCls}" style="width:${pct}%"></div></div>
          <div class="task-meta">${meta}<span>进度 <b>${pct.toFixed(1)}%</b></span></div>
          ${t.error ? `<div class="task-err">${esc(t.error)}</div>` : ''}
        </li>`;
    }).join('');

    list.querySelectorAll('[data-cancel]').forEach((b) => {
      b.onclick = async () => {
        const r = await api('/api/tasks/' + b.dataset.cancel + '/cancel', { method: 'POST' });
        if (r.ok) toast('已取消该任务');
        else toast(r.error || '取消失败', 'err');
      };
    });
  }

  function shortPath(p) {
    if (!p) return '';
    const parts = p.split(/[\\/]/);
    return parts.length <= 3 ? p : '…/' + parts.slice(-2).join('/');
  }

  async function clearTasks() {
    const r = await api('/api/tasks/clear', { method: 'POST' });
    if (r.ok) {
      toast('已清理 ' + r.removed + ' 条记录');
      const s = await api('/api/tasks');
      applySnapshot(s);
    }
  }

  // ------------------------------------------------------ 发送

  async function sendAll() {
    if (state.uploading) { toast('正在发送中，请稍候'); return; }
    if (!state.queue.length) { toast('请先选择文件'); return; }

    const total = state.queue.reduce((s, q) => s + q.size, 0);

    // ① 容量预检：先问服务端「这台机器的接收目录够不够」
    const pre = await api('/api/precheck', {
      method: 'POST',
      body: JSON.stringify({ save_dir: state.recvDir, items: state.queue.map((q) => ({ size: q.size })) }),
    });

    if (!pre.ok) {
      // 空间不足 —— 弹出明确的提示，让用户决定
      showModal({
        title: '磁盘空间不足',
        body: `<p style="margin-bottom:12px">${esc(pre.message)}</p>
               <p style="font-size:12px;color:var(--text-2)">
                 本次共 ${state.queue.length} 个文件，合计 ${esc(pre.need_text)}。<br>
                 目标目录：<code>${esc(state.recvDir)}</code>
               </p>`,
        buttons: [
          { text: '更换保存目录', cls: 'btn', onClick: () => { closeModal(); showDirPickerModal(); } },
          { text: '仍然发送（可能失败）', cls: 'btn ghost', onClick: () => { closeModal(); doUpload(total); } },
        ],
      });
      return;
    }

    doUpload(total);
  }

  async function doUpload(totalBytes) {
    state.uploading = true;
    $('btnSendAll').disabled = true;

    const items = state.queue.slice();
    let okCount = 0, failCount = 0;
    let sentBytes = 0;
    const t0 = performance.now();

    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      $('queueInfo').textContent =
        '正在发送 ' + (i + 1) + '/' + items.length + '：' + it.rel_path;

      const res = await uploadOne(it, (sent) => {
        const done = sentBytes + sent;
        const sp = done / Math.max((performance.now() - t0) / 1000, 0.001);
        $('queueInfo').textContent =
          '正在发送 ' + (i + 1) + '/' + items.length + '  ·  ' +
          fmtSize(done) + ' / ' + fmtSize(totalBytes) + '  ·  ' + fmtSpeed(sp);
      });

      if (res.ok) { okCount++; sentBytes += it.size; }
      else if (res.canceled) { failCount++; break; }
      else { failCount++; }
    }

    state.uploading = false;
    $('btnSendAll').disabled = false;

    if (failCount === 0) {
      state.queue = [];
      renderQueue();
      toast('全部发送完成：' + okCount + ' 个文件', 'ok');
    } else {
      toast('发送结束：成功 ' + okCount + '，失败 ' + failCount, 'err');
      renderQueue();
    }
    const s = await api('/api/tasks');
    applySnapshot(s);
  }

  // 单个文件流式上传：用 XHR 而不是 fetch，因为要 upload.onprogress
  function uploadOne(item, onProgress) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload', true);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      // 自定义头传元信息：服务端据此走流式接收分支
      xhr.setRequestHeader('X-Rel-Path', encodeURIComponent(item.rel_path));
      xhr.setRequestHeader('X-File-Size', String(item.size));
      xhr.timeout = 0;

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded);
      };

      xhr.onload = () => {
        let r = {};
        try { r = JSON.parse(xhr.responseText || '{}'); } catch (e) { /* 忽略 */ }
        if (xhr.status >= 200 && xhr.status < 300 && r.ok !== false) {
          resolve({ ok: true, task_id: r.task_id });
        } else if (r.error && /取消/.test(r.error)) {
          resolve({ ok: false, canceled: true });
        } else {
          resolve({ ok: false, error: r.error || ('HTTP ' + xhr.status) });
        }
      };
      xhr.onerror = () => resolve({ ok: false, error: '网络错误，连接中断' });
      xhr.ontimeout = () => resolve({ ok: false, error: '传输超时' });

      xhr.send(item.file);
    });
  }

  // ------------------------------------------------------ 局域网扫描

  async function scanDevices() {
    const btn = $('btnScan');
    btn.disabled = true;
    $('scanInfo').textContent = '扫描中…';
    $('deviceList').innerHTML = '';

    const r = await api('/api/scan?quick=1');
    btn.disabled = false;

    if (!r.ok) {
      $('scanInfo').textContent = '扫描失败：' + (r.error || '未知错误');
      return;
    }
    $('scanInfo').textContent =
      '网段 ' + r.network + ' · 发现 ' + r.devices.length + ' 台 · 耗时 ' + r.elapsed + 's';

    if (!r.devices.length) {
      $('deviceList').innerHTML =
        '<li style="color:var(--text-3);font-size:12px">未发现其他设备（可能被防火墙阻挡）</li>';
      return;
    }
    $('deviceList').innerHTML = r.devices.map((d) => `
      <li>
        <span class="dev-dot"></span>
        <span class="dev-ip">${esc(d.ip)}</span>
        <span class="dev-meta">${esc(d.mac || '—')} ${d.hostname && d.hostname !== d.ip ? '· ' + esc(d.hostname) : ''}</span>
      </li>`).join('');
  }

  // ------------------------------------------------------ 弹窗

  let modalButtons = [];

  function showModal(opt) {
    $('modalTitle').textContent = opt.title || '';
    $('modalBody').innerHTML = opt.body || '';
    const foot = $('modalFoot');
    foot.innerHTML = '';
    modalButtons = opt.buttons || [];
    modalButtons.forEach((b, i) => {
      const el = document.createElement('button');
      el.className = b.cls || 'btn';
      el.type = 'button';
      el.textContent = b.text;
      el.onclick = () => b.onClick && b.onClick();
      foot.appendChild(el);
    });
    if (opt.onRender) opt.onRender();
    $('modal').classList.remove('hidden');
  }

  function closeModal() {
    $('modal').classList.add('hidden');
    $('modalBody').innerHTML = '';
  }

  function showRoomModal() {
    if (!state.isOwner) {
      // 普通成员只能看到自己的身份信息
      showModal({
        title: '我的连接信息',
        body: `<div class="field">
                 <label>我的称呼</label>
                 <div class="path-value" style="font-size:13px">${esc(state.myNickname || '未设置')}</div>
               </div>
               <div class="field">
                 <label>我的 IP</label>
                 <div class="path-value">${esc(state.myPeer || '—')}</div>
               </div>
               <p style="font-size:12px;color:var(--text-3)">
                 把你上面的 IP 告诉房主，房主可以指定只接收你的文件。
               </p>`,
        buttons: [{ text: '关闭', cls: 'btn ghost', onClick: closeModal }],
      });
      return;
    }

    // 房主视图：房间码 + 成员 + 定向开关
    const members = state.members || [];
    const memberRows = members.map((m) => {
      const tags = [];
      if (m.is_owner) tags.push('<span class="member-tag owner">主机</span>');
      if (m.peer === state.targetPeer) tags.push('<span class="member-tag target">指定收件人</span>');
      return `<li>
        <span class="member-dot ${m.online ? 'online' : ''}"></span>
        <span class="member-meta">
          <span class="member-name">${esc(m.nickname)}</span>
          <span class="member-ip">${esc(m.peer)} · ${m.online ? '在线' : '已离线'}</span>
        </span>
        ${tags.join('')}
        ${!m.is_owner ? `<button class="btn small ghost" data-kick="${esc(m.peer)}" type="button">踢出</button>` : ''}
        ${!m.is_owner && m.peer !== state.targetPeer
          ? `<button class="btn small" data-target="${esc(m.peer)}" type="button">只收他的</button>` : ''}
      </li>`;
    }).join('') || '<li style="color:var(--text-3);font-size:12px">暂无其他成员连接</li>';

    showModal({
      title: '房间管理',
      body: `
        <div class="field">
          <label>房间码（告诉想互传的人）</label>
          <div class="code-display">${esc(state.roomCode || '—')}</div>
          <p style="font-size:12px;color:var(--text-3);margin-top:8px">
            没有这个码的人无法查看或传输任何文件。换码后已连接的成员会全部掉线。
          </p>
        </div>
        <div style="display:flex;gap:8px;margin-bottom:18px">
          <button class="btn small ghost" id="btnRotate" type="button">更换房间码</button>
          <button class="btn small ghost" id="btnCopyCode" type="button">复制房间码</button>
        </div>

        <div class="switch-row">
          <div>
            <div class="switch-label">允许他人连接</div>
            <div class="switch-hint">关闭后只有本机能使用，其他设备一律进不来</div>
          </div>
          <input type="checkbox" id="chkAllow" ${state.allowGuests ? 'checked' : ''}>
        </div>

        <div class="switch-row">
          <div>
            <div class="switch-label">定向接收（只收指定的人）</div>
            <div class="switch-hint">${state.targetPeer
              ? '当前只接收 ' + esc(state.targetPeer) + ' 发来的文件'
              : '当前接收任何持码者发来的文件'}</div>
          </div>
          ${state.targetPeer
            ? '<button class="btn small ghost" id="btnClearTarget" type="button">取消定向</button>'
            : ''}
        </div>

        <h3 style="font-size:13px;font-weight:500;margin:18px 0 10px">
          已连接成员（${members.filter((m) => !m.is_owner).length} 人）
        </h3>
        <ul class="member-list">${memberRows}</ul>`,
      buttons: [{ text: '关闭', cls: 'btn ghost', onClick: closeModal }],
      onRender: () => {
        const rot = $('btnRotate');
        if (rot) rot.onclick = async () => {
          const r = await api('/api/room/rotate', { method: 'POST' });
          if (r.ok) {
            state.roomCode = r.room_code;
            $('modalBody').querySelector('.code-display').textContent = r.room_code;
            state.members = (state.members || []).filter((m) => m.is_owner);
            toast('房间码已更换，旧成员已掉线', 'ok');
          }
        };
        const cp = $('btnCopyCode');
        if (cp) cp.onclick = () => {
          const code = state.roomCode;
          if (navigator.clipboard) {
            navigator.clipboard.writeText(code).then(
              () => toast('房间码已复制：' + code, 'ok'),
              () => toast('复制失败，请手动记录：' + code));
          } else {
            toast('房间码：' + code);
          }
        };
        const chk = $('chkAllow');
        if (chk) chk.onchange = async () => {
          const r = await api('/api/room/allow', {
            method: 'POST', body: JSON.stringify({ allow: chk.checked }),
          });
          if (r.ok) {
            state.allowGuests = r.allow_guests;
            toast(r.allow_guests ? '已允许他人连接' : '已关闭外部连接', 'ok');
            if (!r.allow_guests) state.members = (state.members || []).filter((m) => m.is_owner);
          }
        };
        const clr = $('btnClearTarget');
        if (clr) clr.onclick = async () => {
          const r = await api('/api/room/target', {
            method: 'POST', body: JSON.stringify({ peer: '*' }),
          });
          if (r.ok) { state.targetPeer = ''; toast('已取消定向，恢复接收所有人'); closeModal(); }
        };
        $('modalBody').querySelectorAll('[data-target]').forEach((b) => {
          b.onclick = async () => {
            const r = await api('/api/room/target', {
              method: 'POST', body: JSON.stringify({ peer: b.dataset.target }),
            });
            if (r.ok) {
              state.targetPeer = r.target_peer;
              toast('已设置：只接收 ' + r.target_peer + ' 的文件', 'ok');
              closeModal();
            }
          };
        });
        $('modalBody').querySelectorAll('[data-kick]').forEach((b) => {
          b.onclick = async () => {
            const r = await api('/api/room/kick', {
              method: 'POST', body: JSON.stringify({ peer: b.dataset.kick }),
            });
            if (r.ok) { toast('已踢出 ' + r.kicked + ' 个连接'); closeModal(); }
          };
        });
      },
    });
  }

  function showQrModal() {
    const s = state.status;
    if (!s) { toast('状态未就绪', 'err'); return; }
    const url = s.url;
    showModal({
      title: '手机扫码连接',
      body: `<div class="qr-wrap">
               <canvas id="qrCanvas"></canvas>
               <div class="qr-url">${esc(url)}</div>
               <p class="qr-tip">让手机连上同一个 WiFi，用相机扫码或浏览器打开上面的地址</p>
             </div>`,
      buttons: [{ text: '关闭', cls: 'btn ghost', onClick: closeModal }],
      onRender: () => drawQr($('qrCanvas'), url),
    });
  }

  // 纯 Canvas 画二维码（QR 算法简化版：用服务端未提供时给出可读提示）
  function drawQr(canvas, text) {
    // 服务端不生成图片，这里用在线生成接口不可靠 → 退化为展示地址 + 提示
    // 若需要真二维码，使用 /api/qr 端点（见 server 扩展）
    const img = new Image();
    img.onload = () => {
      const ctx = canvas.getContext('2d');
      canvas.width = img.width; canvas.height = img.height;
      ctx.drawImage(img, 0, 0);
    };
    img.onerror = () => {
      const ctx = canvas.getContext('2d');
      canvas.width = 220; canvas.height = 220;
      ctx.fillStyle = '#F1F3F5';
      ctx.fillRect(0, 0, 220, 220);
      ctx.fillStyle = '#8B929A';
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('QR 生成失败', 110, 104);
      ctx.fillText('请手动输入下方地址', 110, 126);
    };
    img.src = '/api/qr?text=' + encodeURIComponent(text) + '&t=' + Date.now();
  }

  function showSettingsModal() {
    showModal({
      title: '设置',
      body: `
        <div class="field">
          <label>接收文件的保存目录</label>
          <input type="text" id="setRecvDir" value="${esc(state.recvDir)}">
        </div>
        <p style="font-size:12px;color:var(--text-3)">
          对方发来的文件会保存到这里。改用不同的磁盘分区可以避免把系统盘占满。
        </p>`,
      buttons: [
        { text: '浏览目录', cls: 'btn ghost', onClick: () => { closeModal(); showDirPickerModal(); } },
        {
          text: '保存', cls: 'btn', onClick: async () => {
            const v = $('setRecvDir').value.trim();
            const r = await api('/api/settings', {
              method: 'POST', body: JSON.stringify({ recv_dir: v }),
            });
            if (r.ok) {
              state.recvDir = r.recv_dir;
              renderRecvDir(); refreshDriveSpace();
              closeModal(); toast('保存目录已更新', 'ok');
            } else {
              toast(r.error || '设置失败', 'err');
            }
          },
        },
      ],
    });
  }

  function showDirPickerModal() {
    showModal({
      title: '选择保存目录',
      body: `<p style="font-size:12px;color:var(--text-2);margin-bottom:12px">
               点选一个磁盘分区，或直接输入完整路径</p>
             <div class="field">
               <input type="text" id="pickDir" value="${esc(state.recvDir)}">
             </div>
             <div class="drive-grid" id="driveGrid"></div>`,
      buttons: [
        {
          text: '确定', cls: 'btn', onClick: async () => {
            const v = $('pickDir').value.trim();
            if (!v) { toast('请输入路径'); return; }
            const r = await api('/api/settings', {
              method: 'POST', body: JSON.stringify({ recv_dir: v }),
            });
            if (r.ok) {
              state.recvDir = r.recv_dir; renderRecvDir(); refreshDriveSpace();
              closeModal(); toast('已切换到 ' + r.recv_dir, 'ok');
            } else toast(r.error || '无法使用该目录', 'err');
          },
        },
      ],
      onRender: () => {
        const g = $('driveGrid');
        g.innerHTML = state.drives.map((d) => {
          const pct = 100 - d.free_pct;
          const cls = d.free_pct < 5 ? 'err' : d.free_pct < 15 ? 'warn' : '';
          return `<div class="drive-item" data-path="${esc(d.path)}">
            <span class="drive-label">${esc(d.label)}</span>
            <div class="drive-meter">
              <div class="drive-bar"><div class="${cls}" style="width:${Math.min(100, pct)}%"></div></div>
              <div class="drive-info">可用 ${fmtSize(d.free)} / ${fmtSize(d.total)}（${d.free_pct}%）</div>
            </div>
          </div>`;
        }).join('');
        g.querySelectorAll('.drive-item').forEach((el) => {
          el.onclick = () => {
            g.querySelectorAll('.drive-item').forEach((x) => x.classList.remove('sel'));
            el.classList.add('sel');
            $('pickDir').value = el.dataset.path + 'LANFileTransfer';
          };
        });
      },
    });
  }

  // 浏览本机目录（选择要发送的文件）
  function showBrowseModal() {
    const picked = new Set();

    const render = async (path) => {
      const r = path
        ? await api('/api/browse?path=' + encodeURIComponent(path))
        : await api('/api/browse');
      if (!r.ok) {
        $('brPath').textContent = '无法访问：' + (r.error || '');
        return;
      }
      currentBrowsePath = r.path;
      $('brPath').textContent = r.path;

      // 防御：接口可能因权限/并发返回缺字段，别让整个渲染崩掉
      const drives = Array.isArray(r.drives) ? r.drives : [];
      const entries = Array.isArray(r.entries) ? r.entries : [];
      let html = '';
      if (drives.length) {
        html += drives.map((d) => `
          <li class="is-dir" data-goto="${esc(d.path)}">
            <span class="br-icon dir"></span>
            <span class="br-name">${esc(d.label)}</span>
            <span class="br-size">${fmtSize(d.free)} 可用</span>
          </li>`).join('');
      }
      if (r.parent && r.parent !== r.path) {
        html += `<li data-goto="${esc(r.parent)}">
            <span class="br-icon dir"></span>
            <span class="br-name">.. 返回上级</span>
          </li>`;
      }
      html += entries.map((e) => `
        <li class="${e.is_dir ? '' : 'is-file'}" data-${e.is_dir ? 'goto' : 'file'}="${esc(e.path)}">
          <input type="checkbox" class="br-check" data-path="${esc(e.path)}"
                 data-size="${e.size}" data-rel="${esc(e.name)}"
                 ${picked.has(e.path) ? 'checked' : ''}>
          <span class="br-icon ${e.is_dir ? 'dir' : 'file'}"></span>
          <span class="br-name">${esc(e.name)}</span>
          <span class="br-size">${e.is_dir ? '' : e.size_text}</span>
        </li>`).join('');

      $('brList').innerHTML = html || '<li style="color:var(--text-3)">（空目录）</li>';

      $('brList').querySelectorAll('[data-goto]').forEach((el) => {
        el.onclick = (ev) => {
          if (ev.target.classList.contains('br-check')) return;
          render(el.dataset.goto);
        };
      });
      $('brList').querySelectorAll('.br-check').forEach((cb) => {
        cb.onclick = (ev) => ev.stopPropagation();
        cb.onchange = () => {
          if (cb.checked) picked.add(cb.dataset.path);
          else picked.delete(cb.dataset.path);
          updatePickedInfo();
        };
      });
    };

    let currentBrowsePath = '';
    const updatePickedInfo = () => {
      const box = $('brPicked');
      if (!box) return;
      if (!picked.size) { box.textContent = '未选择文件'; return; }
      box.textContent = '已选择 ' + picked.size + ' 个文件';
    };

    showModal({
      title: '浏览本机文件',
      body: `<div class="br-path" id="brPath">加载中…</div>
             <ul class="br-list" id="brList"></ul>`,
      buttons: [
        { text: '返回上级', cls: 'btn ghost', onClick: () => {
            const p = $('brPath').textContent.replace(/[\\/][^\\/]*$/, '');
            if (p && p !== $('brPath').textContent) render(p);
          } },
        { text: '选定并加入队列', cls: 'btn', onClick: () => {
            if (!picked.size) { toast('请先勾选文件'); return; }
            const paths = Array.from(picked);
            addPathsToList(paths);
            closeModal();
          } },
      ],
      onRender: () => {
        const row = document.createElement('div');
        row.id = 'brPicked';
        row.style.cssText = 'font-size:12px;color:var(--text-3);margin-top:10px';
        row.textContent = '未选择文件';
        $('modalBody').appendChild(row);
        render(currentBrowsePath || null);
      },
    });
  }

  // 把本机路径交给服务端展开成清单（保留文件夹结构），加入待发送队列
  async function addPathsToList(paths) {
    const r = await api('/api/send/list', {
      method: 'POST', body: JSON.stringify({ paths: paths }),
    });
    if (!r.ok) { toast(r.error || '读取失败', 'err'); return; }
    let added = 0;
    for (const it of r.items) {
      if (state.queue.some((q) => q.rel_path === it.rel_path && q.size === it.size)) continue;
      state.queue.push({ rel_path: it.rel_path, size: it.size, localPath: it.path });
      added++;
    }
    renderQueue();
    toast('已添加 ' + added + ' 个文件（共 ' + r.total_text + '）', 'ok');
  }

  async function openRecvDir() {
    const r = await api('/api/open_folder', {
      method: 'POST', body: JSON.stringify({ path: state.recvDir }),
    });
    if (!r.ok) toast(r.error || '打开失败', 'err');
  }

  // ------------------------------------------------------ 启动

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return {
    state, sendAll, scanDevices, toast, doJoin, showGate,
    get queue() { return state.queue; },
  };
})();

// 必须显式挂到 window —— 内联 onclick / 浏览器控制台调用依赖它
window.App = App;
