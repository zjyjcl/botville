#!/usr/bin/env node
/* 通过本机 Chrome DevTools Protocol 测量、检查并导出静态页面。 */
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { basename, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [designDirArg, chrome, only = ''] = process.argv.slice(2);
const designDir = resolve(designDirArg);
const pngDir = join(designDir, 'png');
const workRoot = await mkdtemp(join(designDir, '.export.'));
const report = { generated_at: new Date().toISOString(), network_policy: { blocked_url_patterns: ['*http://*', '*https://*'], local_protocol: 'file:' }, pages: [], passed: false };
const pause = ms => new Promise(resolvePause => setTimeout(resolvePause, ms));
const fail = message => { throw new Error(message); };
async function removeWhenReleased(path) { for (let n = 0; n < 12; n += 1) { try { await rm(path, { recursive: true, force: true }); return; } catch (error) { if (n === 11) throw error; await pause(150); } } }
async function freePort() { return new Promise((resolvePort, reject) => { const server = createServer(); server.once('error', reject); server.listen(0, '127.0.0.1', () => { const { port } = server.address(); server.close(error => error ? reject(error) : resolvePort(port)); }); }); }
async function fetchJson(url) { let last; for (let n = 0; n < 50; n += 1) { try { return await (await fetch(url)).json(); } catch (error) { last = error; await pause(80); } } throw last; }
async function openCdp(wsUrl) {
  const socket = new WebSocket(wsUrl); await new Promise((resolveOpen, reject) => { socket.addEventListener('open', resolveOpen, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  let nextId = 1; const waiting = new Map();
  socket.addEventListener('message', event => { const message = JSON.parse(event.data); const pending = waiting.get(message.id); if (!pending) return; waiting.delete(message.id); message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result); });
  return { command(method, params = {}) { const id = nextId++; socket.send(JSON.stringify({ id, method, params })); return new Promise((resolveCommand, reject) => waiting.set(id, { resolve: resolveCommand, reject })); }, close() { socket.close(); } };
}
function meta(source, key) { const match = source.match(new RegExp(`<meta\\s+name="${key}"\\s+content="(\\d+)"`)); if (!match) fail(`页面缺少 ${key}`); return Number(match[1]); }
async function inspect(cdp) {
  const expression = `(() => { const rect=e=>{const r=e.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}}; const outside=(root,items)=>items.filter(e=>{const r=e.getBoundingClientRect(),p=root.getBoundingClientRect();return r.left<p.left-.5||r.right>p.right+.5||r.top<p.top-.5||r.bottom>p.bottom+.5}).slice(0,24).map(e=>e.tagName.toLowerCase()+'.'+e.className); const phones=[...document.querySelectorAll('.phone')].map((p,index)=>{const s=p.querySelector('.screen'),b=p.querySelector('.topbar'),t=b?.querySelector('h2'),n=p.querySelector('.tabs');return{index,rect:rect(p),scrollWidth:p.scrollWidth,clientWidth:p.clientWidth,scrollHeight:p.scrollHeight,clientHeight:p.clientHeight,childOutside:outside(p,[...p.querySelectorAll('*')]),screen:s&&{scrollWidth:s.scrollWidth,clientWidth:s.clientWidth,scrollHeight:s.scrollHeight,clientHeight:s.clientHeight,childOutside:outside(s,[...s.querySelectorAll('*')])},title:t&&rect(t),topbar:b&&rect(b),tabs:n&&{labels:[...n.querySelectorAll('.tab')].map(x=>x.textContent.trim()),svgCount:n.querySelectorAll('svg').length}}}); const sheet=document.querySelector('.sheet'); return JSON.stringify({scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,sheet:sheet&&rect(sheet),phones,external:[...document.querySelectorAll('[src],[href]')].map(x=>x.getAttribute('src')||x.getAttribute('href')).filter(x=>/^https?:/i.test(x))}); })()`;
  const result = await cdp.command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); return JSON.parse(result.result.value);
}
function validate(value, expectedFrames, expectedWidth) {
  const issues = [];
  if (value.external.length) issues.push(`存在外部资源：${value.external.join(', ')}`);
  if (value.scrollWidth > expectedWidth + 1) issues.push(`页面横向溢出：${value.scrollWidth}px > ${expectedWidth}px`);
  if (value.phones.length !== expectedFrames) issues.push(`画框数为 ${value.phones.length}，预期 ${expectedFrames}`);
  for (const p of value.phones) {
    const label = `画框 ${p.index + 1}`;
    if (Math.abs(p.rect.width - 390) > .1 || Math.abs(p.rect.height - 844) > .1) issues.push(`${label} 尺寸为 ${p.rect.width}×${p.rect.height}，预期 390×844`);
    if (p.scrollWidth > p.clientWidth + 1) issues.push(`${label} 横向内容溢出`);
    if (p.scrollHeight > p.clientHeight + 1) issues.push(`${label} 内容溢出`);
    if (p.childOutside.length) issues.push(`${label} 子元素越出手机边界：${p.childOutside.join(', ')}`);
    if (p.screen && p.screen.scrollWidth > p.screen.clientWidth + 1) issues.push(`${label} 的 .screen 横向内容溢出`);
    if (p.screen && p.screen.scrollHeight > p.screen.clientHeight + 1) issues.push(`${label} 的 .screen 内容被截断`);
    if (p.screen?.childOutside.length) issues.push(`${label} 的 .screen 子元素越界：${p.screen.childOutside.join(', ')}`);
    if (!p.tabs) { issues.push(`${label} 缺少底部 Tab`); continue; }
    if (p.tabs.labels.length !== 4 || p.tabs.labels.join('|') !== '消息|通讯录|发现|我') issues.push(`${label} Tab 应为 消息/通讯录/发现/我，实际为 ${p.tabs.labels.join('/') || '空'}`);
    if (p.tabs.svgCount < 4) issues.push(`${label} Tab 缺少自绘 SVG 图标`);
    if (!p.title || !p.topbar || Math.abs((p.title.x + p.title.width / 2) - (p.topbar.x + p.topbar.width / 2)) > 2) issues.push(`${label} 顶部标题未居中`);
  }
  if (value.sheet && value.sheet.bottom > value.scrollHeight + 1) issues.push('页脚内容未包含在截图高度内');
  return issues;
}
async function exportOne(html) {
  const source = await readFile(html, 'utf8'); const frames = meta(source, 'design-frame-count'); const width = 64 + 390 * frames + 24 * (frames - 1); const profile = await mkdtemp(join(workRoot, 'profile-')); const port = await freePort();
  const args = ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-breakpad', '--disable-features=OptimizationHints,MediaRouter', '--metrics-recording-only', '--hide-scrollbars', '--force-device-scale-factor=1', '--proxy-server=direct://', '--proxy-bypass-list=*', `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, `--window-size=${width},900`, 'about:blank'];
  const child = spawn(chrome, args, { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, TMPDIR: workRoot } }); let cdp;
  try {
    const target = (await fetchJson(`http://127.0.0.1:${port}/json/list`)).find(item => item.type === 'page'); if (!target?.webSocketDebuggerUrl) fail('未取得 Chrome 页面调试端点'); cdp = await openCdp(target.webSocketDebuggerUrl); await cdp.command('Page.enable'); await cdp.command('Runtime.enable'); await cdp.command('Network.enable'); await cdp.command('Network.setBlockedURLs', { urls: ['*http://*', '*https://*'] }); await cdp.command('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false }); await cdp.command('Page.navigate', { url: pathToFileURL(html).href });
    for (let n = 0; n < 50; n += 1) { const ready = await cdp.command('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true }); if (ready.result.value === 'complete') break; await pause(50); }
    await pause(120); let page = await inspect(cdp); let height = Math.ceil(page.scrollHeight); await cdp.command('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false }); await pause(80); page = await inspect(cdp); height = Math.ceil(page.scrollHeight); const issues = validate(page, frames, width); const png = `png/${basename(html, '.html')}.png`;
    report.pages.push({ html: basename(html), png, width, height, frames, issues, passed: issues.length === 0 }); if (issues.length) fail(`${basename(html)}：${issues.join('；')}`);
    const shot = await cdp.command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, clip: { x: 0, y: 0, width, height, scale: 1 } }); await writeFile(join(designDir, png), Buffer.from(shot.data, 'base64')); console.log(`已导出：${png}（${width} × ${height}）`);
  } finally { cdp?.close(); child.kill('SIGTERM'); await pause(250); await removeWhenReleased(profile); }
}
try {
  await mkdir(pngDir, { recursive: true }); const all = (await readdir(designDir)).filter(name => name.endsWith('.html')).sort(); const selected = only ? all.filter(name => name === only || name === `${only}.html`) : all; if (!selected.length) fail(`未找到设计页：${only}`); for (const name of selected) await exportOne(join(designDir, name)); report.passed = report.pages.every(page => page.passed);
} catch (error) { report.error = error.message; console.error(`导出/检查失败：${error.message}`); process.exitCode = 1; }
finally { await writeFile(join(designDir, 'qa-report.json'), JSON.stringify(report, null, 2) + '\n'); await removeWhenReleased(workRoot); }
