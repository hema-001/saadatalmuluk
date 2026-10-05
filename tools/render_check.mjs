/*
 * Renders index.html through Chrome DevTools Protocol and audits what it finds:
 *   - per-viewport overflow measurement (docSW vs docCW)
 *   - each page element whose right edge exceeds the viewport
 *   - accessible name of every link and button
 *   - screenshots for EN and AR at several widths
 *
 * Dev-only helper. The page itself has no build step and never loads this.
 *
 * Usage: node tools/render_check.mjs [path-to-index.html]
 */
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync, existsSync, readFileSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, resolve, extname, normalize, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const CHROME_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
];

const target = resolve(process.argv[2] ?? 'index.html');
const overHttp = process.argv.includes('--http');
if (!existsSync(target)) {
  console.error('not found: ' + target);
  process.exit(1);
}

const MIME = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.webp': 'image/webp', '.ico': 'image/x-icon',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
};

/* Serving over HTTP also proves the fonts and assets resolve with real
   relative-path + MIME handling, which file:// does not exercise. */
let httpServer = null;
let pageUrl;
const httpLog = [];
if (overHttp) {
  const rootDir = resolve(target, '..');
  const port = 8000 + Math.floor(Math.random() * 900);
  httpServer = createServer((req, res) => {
    const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
    const file = normalize(join(rootDir, rel));
    if (!file.startsWith(rootDir + sep) && file !== rootDir) {
      httpLog.push({ status: 403, path: rel });
      res.writeHead(403).end('forbidden');
      return;
    }
    if (!existsSync(file) || !statSync(file).isFile()) {
      httpLog.push({ status: 404, path: rel });
      res.writeHead(404, { 'content-type': 'text/plain' }).end('not found: ' + rel);
      return;
    }
    httpLog.push({ status: 200, path: rel, type: MIME[extname(file).toLowerCase()] ?? '?' });
    res.writeHead(200, {
      'content-type': MIME[extname(file).toLowerCase()] ?? 'application/octet-stream',
      'cache-control': 'no-store',
    });
    res.end(readFileSync(file));
  });
  await new Promise((r) => httpServer.listen(port, '127.0.0.1', r));
  pageUrl = `http://127.0.0.1:${port}/index.html`;
  console.log('# serving ' + rootDir + ' at ' + pageUrl);
} else {
  pageUrl = pathToFileURL(target).href;
}

const chromePath = CHROME_CANDIDATES.find((p) => existsSync(p));
if (!chromePath) {
  console.error('no Chrome/Edge found');
  process.exit(1);
}

const shotDir = join(tmpdir(), 'sm-shots');
mkdirSync(shotDir, { recursive: true });

const profileDir = join(tmpdir(), 'sm-cdp-profile-' + Date.now());
const port = 9000 + Math.floor(Math.random() * 900);

const chrome = spawn(chromePath, [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-extensions',
  '--disable-background-networking',
  '--hide-scrollbars',
  '--remote-debugging-port=' + port,
  '--user-data-dir=' + profileDir,
  'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe'] });

/* wait for the DevTools endpoint to come up */
const wsUrl = await new Promise((res, rej) => {
  let stderr = '';
  const timer = setTimeout(() => rej(new Error('chrome did not start in time: ' + stderr)), 25000);
  chrome.stderr.on('data', (d) => {
    stderr += d.toString();
    const m = stderr.match(/ws:\/\/[^\s]+/);
    if (m) { clearTimeout(timer); res(m[0]); }
  });
  chrome.on('exit', (code) => { clearTimeout(timer); rej(new Error('chrome exited ' + code + ': ' + stderr)); });
});

/* -------------------------------------------------- minimal CDP client */
let nextId = 1;
const pending = new Map();
const ws = new WebSocket(wsUrl);
await new Promise((res, rej) => {
  ws.addEventListener('open', res, { once: true });
  ws.addEventListener('error', rej, { once: true });
});
ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { res, rej } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
  }
});
function send(method, params = {}, sessionId) {
  const id = nextId++;
  return new Promise((res, rej) => {
    pending.set(id, { res, rej });
    ws.send(JSON.stringify(sessionId ? { id, method, params, sessionId } : { id, method, params }));
  });
}

const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
const S = (m, p) => send(m, p, sessionId);

await S('Page.enable');
await S('Runtime.enable');
await S('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }] });

const MEASURE = `(() => {
  const de = document.documentElement;
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right > window.innerWidth + 1 || r.left < -1) {
      offenders.push({
        tag: el.tagName.toLowerCase(),
        cls: String(el.className || '').slice(0, 50),
        left: Math.round(r.left),
        right: Math.round(r.right),
      });
    }
  }
  const unnamed = [];
  for (const el of document.querySelectorAll('a, button')) {
    const name = (el.getAttribute('aria-label') || el.innerText || el.textContent || '').trim();
    if (!name && !el.querySelector('img[alt]:not([alt=""])')) {
      unnamed.push(el.tagName.toLowerCase() + '.' + String(el.className || '').slice(0, 30));
    }
  }
  const imgNoAlt = [...document.querySelectorAll('img')].filter((i) => !i.hasAttribute('alt')).length;
  const fonts = [...document.fonts].map((f) => f.family + '/' + f.weight + '/' + f.status);
  const usedFamily = getComputedStyle(document.body).fontFamily;
  const wordmark = document.querySelector('.wordmark');
  const wordmarkFamily = wordmark ? getComputedStyle(wordmark).fontFamily : null;
  const cards = document.querySelectorAll('.link-card').length;
  return {
    vw: window.innerWidth,
    docSW: de.scrollWidth,
    docCW: de.clientWidth,
    bodySW: document.body.scrollWidth,
    bodySH: document.body.scrollHeight,
    lang: de.getAttribute('lang'),
    dir: de.getAttribute('dir'),
    cards,
    fonts,
    usedFamily,
    wordmarkFamily,
    unnamed,
    imgNoAlt,
    offenders: offenders.slice(0, 12),
  };
})()`;

async function evaluate(expression) {
  const r = await S('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: false });
  if (r.exceptionDetails) throw new Error('page JS error: ' + JSON.stringify(r.exceptionDetails));
  return r.result.value;
}

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 1080 },
  { name: 'laptop', width: 1280, height: 900 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'mobile', width: 390, height: 844 },
  { name: 'small', width: 320, height: 780 },
];

const report = { page: pageUrl, viewports: [], screenshots: [] };

async function resetEmulation() {
  await S('Emulation.clearDeviceMetricsOverride');
  await S('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }] });
}

for (const vp of VIEWPORTS) {
  for (const lang of ['en', 'ar']) {
    // clear any sticky mobile emulation first, then apply exactly this viewport
    await resetEmulation();
    await S('Emulation.setDeviceMetricsOverride', {
      width: vp.width,
      height: vp.height,
      deviceScaleFactor: 1,
      mobile: false,
      screenWidth: vp.width,
      screenHeight: vp.height,
    });
    await S('Page.navigate', { url: 'about:blank' });
    await new Promise((r) => setTimeout(r, 150));

    if (lang === 'ar') {
      // start from a clean origin so we can pre-seed the saved language,
      // exercising the real localStorage path rather than clicking after load
      await S('Page.navigate', { url: pageUrl });
      await new Promise((r) => setTimeout(r, 400));
      await evaluate(`localStorage.setItem('sm-lang','ar')`);
      await S('Page.navigate', { url: pageUrl });
      await new Promise((r) => setTimeout(r, 2200));
      // also click the toggle so the click handler itself is exercised
      await evaluate(`document.querySelector('[data-setlang="ar"]').click()`);
      await new Promise((r) => setTimeout(r, 400));
    } else {
      await S('Page.navigate', { url: pageUrl });
      await new Promise((r) => setTimeout(r, 200));
      await evaluate(`try{localStorage.removeItem('sm-lang')}catch(e){}`);
      await S('Page.navigate', { url: pageUrl });
      await new Promise((r) => setTimeout(r, 2200));
    }

    const m = await evaluate(MEASURE);
    m.viewport = vp.name;
    m.requestedWidth = vp.width;
    report.viewports.push(m);

    const shot = await S('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    const file = join(shotDir, `${lang}-${vp.name}.png`);
    writeFileSync(file, Buffer.from(shot.data, 'base64'));
    report.screenshots.push(file);
  }
}

/* full-page shots for the sizes that matter most */
for (const [name, width, lang] of [
  ['mobile-full', 390, 'en'],
  ['mobile-full-ar', 390, 'ar'],
  ['desktop-full', 1280, 'en'],
  ['desktop-full-ar', 1280, 'ar'],
]) {
  await resetEmulation();
  await S('Emulation.setDeviceMetricsOverride', {
    width, height: 900, deviceScaleFactor: 1, mobile: false, screenWidth: width, screenHeight: 900,
  });
  await S('Page.navigate', { url: pageUrl });
  await new Promise((r) => setTimeout(r, 900));
  await evaluate(`try{${lang === 'ar' ? `localStorage.setItem('sm-lang','ar')` : `localStorage.removeItem('sm-lang')`}}catch(e){}`);
  await S('Page.navigate', { url: pageUrl });
  await new Promise((r) => setTimeout(r, 2200));
  const metrics = await S('Page.getLayoutMetrics');
  const h = Math.ceil(metrics.cssContentSize.height);
  const shot = await S('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: true,
    clip: { x: 0, y: 0, width, height: h, scale: 1 },
  });
  const file = join(shotDir, `${name}.png`);
  writeFileSync(file, Buffer.from(shot.data, 'base64'));
  report.screenshots.push(file);
}

writeFileSync(join(shotDir, 'report.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));

if (overHttp) {
  const seen = new Map();
  for (const e of httpLog) {
    const k = e.status + ' ' + e.path;
    seen.set(k, (seen.get(k) ?? 0) + 1);
  }
  console.log('\n# HTTP responses (deduplicated)');
  for (const [k, n] of [...seen].sort()) console.log(`  ${k}  x${n}`);
  const bad = [...seen.keys()].filter((k) => !k.startsWith('200'));
  console.log(bad.length ? '# NON-200 RESPONSES: ' + bad.length : '# all asset requests returned 200');
}

ws.close();
chrome.kill();
if (httpServer) httpServer.close();
process.exit(0);
