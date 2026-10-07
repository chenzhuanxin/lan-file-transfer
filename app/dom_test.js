/**
 * dom_test.js —— 前端 DOM 与交互测试（jsdom）
 *
 * 重点验证（踩过的坑）：
 *   1. window.App 必须存在 —— 否则内联 onclick 全部报错，jsdom 不报这个错
 *   2. render() 重绘后旧元素引用失效，每次交互前重新 querySelector
 *   3. localStorage 在 file:// 下 jsdom 会抛异常，需预注入内存版
 *   4. 前端逻辑的核心算法（大小格式化/ETA/去重/状态渲染）是否真能跑
 */
'use strict';

const fs = require('fs');
const path = require('path');

const APP_DIR = path.join('D:', 'lan-file-transfer', 'app');
let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (e) {
  console.error('需要 jsdom：cd 到 node workspace 后安装');
  process.exit(2);
}

const failures = [];
const passes = [];

function check(name, cond, extra) {
  if (cond) { passes.push(name); console.log('  ✓ ' + name); }
  else { failures.push(name); console.log('  ✗ ' + name + (extra ? '  ' + extra : '')); }
}

console.log('='.repeat(60));
console.log('前端 DOM 与交互测试');
console.log('='.repeat(60));

// ---------------------------------------------------------- 准备 HTML
const html = fs.readFileSync(path.join(APP_DIR, 'static', 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(APP_DIR, 'static', 'app.js'), 'utf8');

// localStorage 必须在 head 开头注入（jsdom 在无 origin 环境会抛 DOMException）
const lsShim = `<script>
(function(){
  const store = {};
  const mem = {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k,v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
    clear: () => { for (const k in store) delete store[k]; },
    key: i => Object.keys(store)[i] || null,
    get length(){ return Object.keys(store).length; }
  };
  try { Object.defineProperty(window, 'localStorage', { value: mem, configurable: true }); }
  catch(e) { /* 已有则忽略 */ }
  // 预置房间令牌，让 App 启动即视为已登录，跳过输码界面
  mem.setItem('lanfile_room_token', 'test-owner-token');
  window.__lsShim = mem;
})();
</script>`;

const patchedHtml = html
  .replace('<head>', '<head>' + lsShim)
  // 关键：把外部 app.js 内联进来，jsdom 不会自动加载外部 <script src>
  .replace('<script src="/static/app.js"></script>', '<script>' + appJs + '</script>');

const vc = new VirtualConsole();
const jsdomErrors = [];
vc.on('jsdomError', (e) => {
  const msg = String(e.message || e);
  // jsdom 不支持这两个 API，与本项目代码无关，过滤掉
  if (/Not implemented|Could not parse CSS|URL\.revokeObjectURL|window\.scrollTo/.test(msg)) return;
  jsdomErrors.push(msg);
});
vc.on('error', (m) => jsdomErrors.push(String(m)));

const dom = new JSDOM(patchedHtml, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: vc,
  url: 'http://127.0.0.1:9000/',
});
const { window } = dom;
const { document } = window;

// 拦截网络请求，避免测试真的打服务端
const fetchCalls = [];
window.fetch = (url, opts) => {
  fetchCalls.push({ url, opts });
  // 按端点返回合理假数据
  let body = { ok: true };
  if (url.startsWith('/api/ping')) {
    // 已认证状态：让测试直接跳过输码环节（输码流程另有专项测试）
    body = { ok: true, app: '局域网文件传输', version: '1.1.0',
             need_code: true, authed: true, allow_guests: true };
  } else if (url.startsWith('/api/room')) {
    body = { ok: true, is_owner: true, allow_guests: true, target_peer: '',
             guest_count: 1, room_code: 'A3F9K2',
             members: [
               { peer: '127.0.0.1', nickname: '主机', is_owner: true,
                 created_at: 0, last_seen: 9999999999, uploaded: 0,
                 downloaded: 0, online: true },
               { peer: '192.168.1.50', nickname: '同事小王', is_owner: false,
                 created_at: 0, last_seen: 9999999999, uploaded: 100,
                 downloaded: 0, online: true },
             ] };
  } else if (url.startsWith('/api/status')) {
    body = {
      ok: true, app: '局域网文件传输', version: '1.0.0',
      local_ip: '192.168.1.3', port: 9000, url: 'http://192.168.1.3:9000',
      interfaces: [{ ip: '192.168.1.3', name: '以太网', score: 115, virtual: false, speed_mbps: 1000 }],
      hostname: 'DESKTOP-TEST', platform: 'win32',
      recv_dir: 'C:\\Users\\gongq\\Downloads\\LANFileTransfer',
      recv_free: 18082058240, uptime: 12.3,
    };
  } else if (url.startsWith('/api/settings')) {
    body = {
      ok: true, recv_dir: 'C:\\Users\\gongq\\Downloads\\LANFileTransfer',
      drives: driveFixture(), default_dir: 'C:\\Users\\gongq\\Downloads\\LANFileTransfer',
    };
  } else if (url.startsWith('/api/drives')) {
    body = { ok: true, drives: driveFixture() };
  } else if (url.startsWith('/api/tasks')) {
    body = { ok: true, summary: { active: 0, pending: 0, done: 0, failed: 0,
      upload_speed: 0, download_speed: 0, total_tasks: 0 }, tasks: [], recv_dir: '' };
  } else if (url.startsWith('/api/precheck')) {
    body = { ok: true, need: 1048576, need_text: '1.0 MB', free: 18082058240,
      free_text: '16.8 GB', shortfall: 0, shortfall_text: '0 B',
      message: '空间充足', file_count: 1 };
  } else if (url.startsWith('/api/scan')) {
    body = { ok: true, network: '192.168.1.0/24', local_ip: '192.168.1.3',
      devices: [
        { ip: '192.168.1.1', mac: '50:e2:4e:52:21:48', hostname: 'router', source: 'arp' },
        { ip: '192.168.1.2', mac: 'c8:bf:4c:41:ac:a7', hostname: '', source: 'arp' },
      ], elapsed: 1.2 };
  } else if (url.startsWith('/api/browse')) {
    body = { ok: true, path: 'C:\\Users\\gongq', parent: 'C:\\Users',
      entries: [
        { name: 'Documents', path: 'C:\\Users\\gongq\\Documents', is_dir: true, size: 0, size_text: '0 B' },
        { name: '测试.txt', path: 'C:\\Users\\gongq\\测试.txt', is_dir: false, size: 2048, size_text: '2.0 KB' },
      ], drives: driveFixture() };
  } else if (url.startsWith('/api/send/list')) {
    body = { ok: true, items: [{ rel_path: '测试.txt', size: 2048, path: 'C:\\Users\\gongq\\测试.txt' }],
      total: 2048, total_text: '2.0 KB', errors: [] };
  }
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
};

function driveFixture() {
  return [
    { label: 'C:\\', path: 'C:\\', total: 193273528320, used: 178000000000,
      free: 15273528320, free_pct: 7.9, is_default: true },
    { label: 'D:\\', path: 'D:\\', total: 500107862016, used: 90000000000,
      free: 410107862016, free_pct: 82.0, is_default: false },
    { label: 'E:\\', path: 'E:\\', total: 62277025792, used: 500000000,
      free: 61777025792, free_pct: 99.2, is_default: false },
  ];
}

// EventSource 模拟（jsdom 没有）
window.EventSource = function (url) {
  this.url = url;
  this.readyState = 1;
  this.close = () => { this.readyState = 2; };
  window.__es = this;
};
window.EventSource.CLOSED = 2;

// XHR 模拟（上传用）
class FakeXHR {
  constructor() { this.upload = {}; this.headers = {}; this.status = 200; }
  open(m, u) { this.method = m; this.url = u; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send(body) {
    window.__lastXHR = this;
    setTimeout(() => {
      this.responseText = JSON.stringify({ ok: true, task_id: 'abc123' });
      this.status = 200;
      if (this.upload.onprogress) {
        this.upload.onprogress({ lengthComputable: true, loaded: body ? body.size : 0 });
      }
      if (this.onload) this.onload();
    }, 0);
  }
}
window.XMLHttpRequest = FakeXHR;

// ============================================================ 测试开始

setTimeout(async () => {
  const App = window.App;

  console.log('\n[1] 全局挂载（最容易漏的坑）');
  check('window.App 已定义', typeof window.App !== 'undefined',
        '若失败则所有内联 onclick 报 ReferenceError');
  check('App 暴露预期方法', !!(App && App.state && App.sendAll && App.scanDevices));

  console.log('\n[2] 页面结构完整性');
  const needIds = ['connInfo', 'upSpeed', 'downSpeed', 'activeCount', 'dropZone',
    'fileInput', 'folderInput', 'btnPickFiles', 'btnPickFolder', 'btnBrowse',
    'btnSendAll', 'btnClearQueue', 'queueList', 'sendQueue', 'recvDir',
    'btnChangeDir', 'btnOpenDir', 'capBar', 'capText', 'capWarn',
    'btnScan', 'deviceList', 'scanInfo', 'taskList', 'emptyTasks',
    'btnClearTasks', 'modal', 'modalTitle', 'modalBody', 'modalFoot',
    'modalClose', 'toast', 'btnQr', 'btnSettings'];
  const missing = needIds.filter((id) => !document.getElementById(id));
  check('所有关键元素存在', missing.length === 0,
        missing.length ? '缺少: ' + missing.join(', ') : '');

  console.log('\n[3] 初始化后状态填充');
  await new Promise((r) => setTimeout(r, 400));
  check('顶部连接信息已填充', /192\.168\.1\.3/.test(document.getElementById('connInfo').textContent),
        '实际: ' + document.getElementById('connInfo').textContent);
  check('接收目录已显示', document.getElementById('recvDir').textContent.includes('LANFileTransfer'),
        '实际: ' + document.getElementById('recvDir').textContent);
  check('磁盘容量已计算', document.getElementById('capText').textContent.includes('可用'),
        '实际: ' + document.getElementById('capText').textContent);
  const capW = document.getElementById('capBar').style.width;
  check('容量条宽度已设置', capW && capW !== '0%' && capW !== '', 'width=' + capW);

  console.log('\n[4] 文件添加到待发送队列');
  // 模拟选择文件：直接调 App 内部路径（通过 input change）
  const fi = document.getElementById('fileInput');
  const fakeFiles = [
    { name: '报告.pdf', size: 3 * 1024 * 1024 },
    { name: '数据.csv', size: 512 * 1024 },
  ];
  Object.defineProperty(fi, 'files', { value: fakeFiles, configurable: true });
  fi.dispatchEvent(new window.Event('change'));
  await new Promise((r) => setTimeout(r, 50));

  check('队列已显示', !document.getElementById('sendQueue').classList.contains('hidden'));
  check('队列项数正确 = 2', document.getElementById('queueList').children.length === 2,
        '实际 ' + document.getElementById('queueList').children.length);
  check('队列汇总含总大小', /2 个文件/.test(document.getElementById('queueInfo').textContent),
        '实际: ' + document.getElementById('queueInfo').textContent);

  console.log('\n[5] 重复添加应去重');
  const fi2 = document.getElementById('fileInput');
  Object.defineProperty(fi2, 'files', { value: fakeFiles, configurable: true });
  fi2.dispatchEvent(new window.Event('change'));
  await new Promise((r) => setTimeout(r, 50));
  // 注意：重绘后必须重新 querySelector，旧引用已失效
  check('重复文件被去重（仍为 2 项）',
        document.getElementById('queueList').children.length === 2,
        '实际 ' + document.getElementById('queueList').children.length);

  console.log('\n[6] 删除队列项（重绘后重新取元素）');
  const delBtn = document.querySelector('#queueList .fdel');
  check('删除按钮存在', !!delBtn);
  if (delBtn) {
    delBtn.click();
    await new Promise((r) => setTimeout(r, 30));
    const after = document.getElementById('queueList').children.length;
    check('删除后剩 1 项', after === 1, '实际 ' + after);
  }

  console.log('\n[7] 局域网设备扫描');
  document.getElementById('btnScan').click();
  await new Promise((r) => setTimeout(r, 120));
  const devItems = document.getElementById('deviceList').children.length;
  check('扫描出 2 台设备', devItems === 2, '实际 ' + devItems);
  check('扫描信息含网段', /192\.168\.1\.0\/24/.test(document.getElementById('scanInfo').textContent),
        document.getElementById('scanInfo').textContent);

  console.log('\n[8] 任务列表渲染（含进度/速度/状态）');
  const now = Date.now() / 1000;
  const es = window.__es;
  check('实时推送通道已建立(EventSource)', !!es);
  if (!es) { console.log('  跳过任务渲染测试'); }
  else es.onmessage({
    data: JSON.stringify({
      type: 'update',
      recv_dir: 'C:\\Users\\gongq\\Downloads\\LANFileTransfer',
      summary: { active: 1, pending: 0, done: 1, failed: 0,
        upload_speed: 12 * 1024 * 1024, download_speed: 3.5 * 1024 * 1024, total_tasks: 2 },
      tasks: [
        { task_id: 't1', direction: 'upload', rel_path: '影片/电影.mp4', name: '电影.mp4',
          size: 1073741824, size_text: '1.0 GB', transferred: 536870912,
          transferred_text: '512.0 MB', speed: 12 * 1024 * 1024, status: 'running',
          error: '', progress: 50.0, eta: 44.7, peer: '192.168.1.2',
          final_path: '', sha256: '' },
        { task_id: 't2', direction: 'download', rel_path: '文档.pdf', name: '文档.pdf',
          size: 2048, size_text: '2.0 KB', transferred: 2048, transferred_text: '2.0 KB',
          speed: 3.5 * 1024 * 1024, status: 'done', error: '', progress: 100,
          eta: null, peer: '192.168.1.1', final_path: '', sha256: 'abc' },
      ],
    }),
  });
  await new Promise((r) => setTimeout(r, 120));

  const tasks = document.querySelectorAll('#taskList .task');
  check('渲染出 2 条任务', tasks.length === 2, '实际 ' + tasks.length);
  check('空状态已隐藏', document.getElementById('emptyTasks').classList.contains('hidden'));

  const txt = document.getElementById('taskList').textContent;
  check('显示"接收"方向标签', txt.includes('接收'));
  check('显示"发送"方向标签', txt.includes('发送'));
  check('显示"传输中"状态', txt.includes('传输中'));
  check('显示"已完成"状态', txt.includes('已完成'));
  check('显示速度数值', /MB\/s/.test(txt), '速度文本缺失');
  check('显示剩余时间(ETA)', /剩余/.test(txt), 'ETA 缺失');
  check('显示进度百分比', /50\.0%/.test(txt), '进度缺失');

  const fills = document.querySelectorAll('#taskList .task-fill');
  // 任务倒序渲染：第 1 条是下载(100%)，第 2 条是上传(50%)
  const upFill = fills[1];
  check('进度条宽度已设置', upFill && upFill.style.width === '50%',
        'width=' + (upFill ? upFill.style.width : 'N/A'));
  check('完成任务的进度条为 100%', fills[0] && fills[0].style.width === '100%',
        'width=' + (fills[0] ? fills[0].style.width : 'N/A'));

  check('顶部上传速度已更新',
        document.getElementById('upSpeed').textContent.includes('MB/s'),
        document.getElementById('upSpeed').textContent);
  check('顶部下载速度已更新',
        document.getElementById('downSpeed').textContent.includes('MB/s'),
        document.getElementById('downSpeed').textContent);
  check('进行中数量 = 1', document.getElementById('activeCount').textContent === '1',
        document.getElementById('activeCount').textContent);

  console.log('\n[9] 取消按钮');
  const cancelBtn = document.querySelector('#taskList [data-cancel]');
  check('运行中任务有取消按钮', !!cancelBtn);

  console.log('\n[10] 容量不足弹窗逻辑');
  // 保存原始 fetch 以便后续测试恢复（否则后面步骤的假数据会缺失）
  const realFetch = window.fetch;
  window.fetch = (url, opts) => {
    if (url.startsWith('/api/precheck')) {
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          ok: false, need: 5 * 1024 ** 3, need_text: '5.0 GB',
          free: 15273528320, free_text: '14.2 GB', shortfall: 5368709120,
          shortfall_text: '5.0 GB',
          message: '空间不足：需要 5.0 GB（含 2% 余量），目标盘仅剩 14.2 GB，还差 5.0 GB',
          file_count: 1,
        }),
      });
    }
    return realFetch(url, opts);
  };
  window.App.state.queue.push({ rel_path: '大影片.mp4', size: 5 * 1024 ** 3, file: { size: 5 * 1024 ** 3 } });
  await window.App.sendAll();
  await new Promise((r) => setTimeout(r, 120));
  const modalShown = !document.getElementById('modal').classList.contains('hidden');
  check('容量不足时弹出提示框', modalShown);
  const modalTxt = document.getElementById('modalBody').textContent;
  check('弹窗含"空间不足"说明', /空间不足/.test(modalTxt), modalTxt.slice(0, 80));
  check('弹窗含所需/可用数字', /5\.0 GB/.test(modalTxt) && /14\.2 GB/.test(modalTxt));
  const btns = document.querySelectorAll('#modalFoot button');
  check('弹窗有 2 个处理选项', btns.length === 2, '实际 ' + btns.length);
  check('提供"更换保存目录"选项',
        Array.from(btns).some((b) => /更换保存目录/.test(b.textContent)));

  console.log('\n[11] 目录选择弹窗 & 磁盘列表');
  document.getElementById('modalClose').click();
  document.getElementById('btnChangeDir').click();
  await new Promise((r) => setTimeout(r, 80));
  const driveItems = document.querySelectorAll('#driveGrid .drive-item');
  check('列出 3 个磁盘分区', driveItems.length === 3, '实际 ' + driveItems.length);
  check('分区含可用空间信息', /可用/.test(document.getElementById('driveGrid').textContent));
  if (driveItems.length) {
    driveItems[0].click();
    check('点击分区后填入路径',
          document.getElementById('pickDir').value.includes('LANFileTransfer'),
          document.getElementById('pickDir').value);
  }
  document.getElementById('modalClose').click();

  console.log('\n[12] 二维码弹窗');
  document.getElementById('btnQr').click();
  await new Promise((r) => setTimeout(r, 60));
  check('二维码弹窗打开', !document.getElementById('modal').classList.contains('hidden'));
  check('显示连接地址', /192\.168\.1\.3:9000/.test(document.getElementById('modalBody').textContent),
        document.getElementById('modalBody').textContent.slice(0, 100));
  document.getElementById('modalClose').click();

  console.log('\n[13] 目录浏览器');
  document.getElementById('btnBrowse').click();
  await new Promise((r) => setTimeout(r, 120));
  const brItems = document.querySelectorAll('#brList li');
  check('目录浏览有内容', brItems.length > 0, '实际 ' + brItems.length);
  check('含复选勾选框', document.querySelectorAll('.br-check').length > 0);
  document.getElementById('modalClose').click();

  console.log('\n[14] 清理与空状态');
  window.fetch = (url) => Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve({
      ok: true, summary: { active: 0, pending: 0, done: 0, failed: 0,
        upload_speed: 0, download_speed: 0, total_tasks: 0 }, tasks: [], recv_dir: '',
    }),
  });
  document.getElementById('btnClearTasks').click();
  await new Promise((r) => setTimeout(r, 80));
  check('任务清空后显示空状态',
        !document.getElementById('emptyTasks').classList.contains('hidden'));


  console.log('\n[14.5] 房间码登录界面');
  // 重新构建一个"未认证"的 DOM，验证输码流程
  {
    const html2 = fs.readFileSync(path.join(APP_DIR, 'static', 'index.html'), 'utf8');
    const lsShim2 = `<script>
      (function(){
        const store = {};
        const mem = {
          getItem: k => (k in store ? store[k] : null),
          setItem: (k,v) => { store[k] = String(v); },
          removeItem: k => { delete store[k]; },
          clear: () => { for (const k in store) delete store[k]; },
          key: i => Object.keys(store)[i] || null,
          get length(){ return Object.keys(store).length; }
        };
        Object.defineProperty(window, 'localStorage', { value: mem, configurable: true });
      })();
    </script>`;
    const patched2 = html2
      .replace('<head>', '<head>' + lsShim2)
      .replace('<script src="/static/app.js"></script>', '<script>' + appJs + '</script>');

    const vc2 = new VirtualConsole();
    const errs2 = [];
    vc2.on('jsdomError', (e) => {
      const m = String(e.message || e);
      if (/Not implemented|Could not parse CSS|revokeObjectURL|scrollTo/.test(m)) return;
      errs2.push(m);
    });
    const dom2 = new JSDOM(patched2, {
      runScripts: 'dangerously', pretendToBeVisual: true,
      virtualConsole: vc2, url: 'http://127.0.0.1:9000/',
    });
    const w2 = dom2.window;
    const d2 = w2.document;
    w2.EventSource = function (u) { this.url = u; this.close = () => {}; };
    w2.EventSource.CLOSED = 2;

    // 未认证：/api/ping 返回 authed:false
    w2.fetch = (url) => Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(
        url.startsWith('/api/ping')
          ? { ok: true, app: '局域网文件传输', version: '1.1.0',
              need_code: true, authed: false, allow_guests: true }
          : { ok: false }
      ),
    });

    await new Promise((r) => setTimeout(r, 300));

    check('未认证时显示输码界面',
          !d2.getElementById('gate').classList.contains('hidden'),
          'gate 未显示');
    check('输码框存在', !!d2.getElementById('gateCode'));
    check('输码框自动聚焦',
          d2.activeElement && d2.activeElement.id === 'gateCode',
          d2.activeElement ? d2.activeElement.id : 'none');
    check('提示需向发起人索取房间码',
          /索取|房间码/.test(d2.querySelector('.gate-sub').textContent),
          d2.querySelector('.gate-sub').textContent);

    // 输入错码
    w2.fetch = (url) => {
      if (url.startsWith('/api/join')) {
        return Promise.resolve({
          ok: false, status: 403,
          json: () => Promise.resolve({ ok: false, error: '房间码不正确，还可尝试 9 次' }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    };
    d2.getElementById('gateCode').value = 'WRONG1';
    d2.getElementById('gateBtn').click();
    await new Promise((r) => setTimeout(r, 300));

    const errBox = d2.getElementById('gateErr');
    check('错码时显示错误提示', !errBox.classList.contains('hidden'));
    check('错误文案含剩余次数', /还可尝试/.test(errBox.textContent), errBox.textContent);
    check('错码后仍停留在输码界面',
          !d2.getElementById('gate').classList.contains('hidden'));
    check('错码后清空输入框',
          d2.getElementById('gateCode').value === '',
          'value=' + d2.getElementById('gateCode').value);

    // 输入正确码
    w2.fetch = (url) => {
      let body = { ok: true };
      if (url.startsWith('/api/join')) {
        body = { ok: true, token: 'new-token-abc', nickname: '同事小王', peer: '192.168.1.50' };
      } else if (url.startsWith('/api/ping')) {
        body = { ok: true, authed: true, need_code: true };
      } else if (url.startsWith('/api/room')) {
        body = { ok: true, is_owner: false, allow_guests: true, target_peer: '',
                 nickname: '同事小王', peer: '192.168.1.50' };
      } else if (url.startsWith('/api/status')) {
        body = { ok: true, app: '局域网文件传输', version: '1.1.0',
                 local_ip: '192.168.1.50', port: 9000, url: 'http://192.168.1.50:9000',
                 interfaces: [], hostname: 'PC-B', platform: 'win32',
                 recv_dir: 'D://收件', recv_free: 1, uptime: 1 };
      } else if (url.startsWith('/api/settings')) {
        body = { ok: true, recv_dir: 'D://收件', drives: [], default_dir: '' };
      } else if (url.startsWith('/api/drives')) {
        body = { ok: true, drives: [] };
      } else if (url.startsWith('/api/tasks')) {
        body = { ok: true, summary: {}, tasks: [], recv_dir: '' };
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    };
    d2.getElementById('gateCode').value = 'a3f-9k2';
    d2.getElementById('gateBtn').click();
    await new Promise((r) => setTimeout(r, 400));

    check('正确码后输码界面隐藏',
          d2.getElementById('gate').classList.contains('hidden'));
    check('令牌已保存到 localStorage',
          w2.localStorage.getItem('lanfile_room_token') === 'new-token-abc',
          String(w2.localStorage.getItem('lanfile_room_token')));
    check('进入主界面并加载状态',
          /192\.168\.1\.50/.test(d2.getElementById('connInfo').textContent),
          d2.getElementById('connInfo').textContent);
    check('普通成员按钮显示自己身份',
          /同事小王/.test(d2.getElementById('btnRoom').textContent),
          d2.getElementById('btnRoom').textContent);
    check('输码流程无 JS 异常', errs2.length === 0, errs2.slice(0, 2).join(' | '));
  }

  console.log('\n[15] 无未捕获 JS 错误');
  check('页面运行无 JS 异常', jsdomErrors.length === 0,
        jsdomErrors.length ? jsdomErrors.slice(0, 3).join(' | ') : '');

  // ---------------------------------------------------------- 汇总
  console.log('\n' + '='.repeat(60));
  console.log(`通过 ${passes.length} 项，失败 ${failures.length} 项`);
  if (failures.length) {
    console.log('\n失败项：');
    failures.forEach((f) => console.log('  - ' + f));
    console.log('='.repeat(60));
    process.exit(1);
  }
  console.log('全部前端测试通过');
  console.log('='.repeat(60));
  process.exit(0);
}, 200);
