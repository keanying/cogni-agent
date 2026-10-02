// 由 tests/test_chart_render.py 调用:用仓库里那份 ECharts(与 APP 同一构建)真把 option 画出来。
// 参数:argv[2] = options.json 路径,argv[3] = echarts.min.js 路径。输出一行 JSON 结果。
const fs = require('fs');
const path = require('path');
const pwPath = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = require(pwPath);
(async () => {
  const opts = JSON.parse(fs.readFileSync(process.argv[2], 'utf-8'));
  const echartsSrc = fs.readFileSync(process.argv[3], 'utf-8');
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1600, height: 1400 } });
  const logs = [];
  p.on('console', (m) => { if (['error', 'warning'].includes(m.type())) logs.push(m.text()); });
  p.on('pageerror', (e) => logs.push('pageerror: ' + e.message));
  await p.setContent('<html><body style="margin:0"></body></html>');
  await p.addScriptTag({ content: echartsSrc });
  const res = await p.evaluate(async (opts) => {
    const out = { errors: [], blank: [], aria: {} };
    const made = [];
    for (const [k, o] of Object.entries(opts)) {
      const d = document.createElement('div'); d.style.cssText = 'width:380px;height:260px'; document.body.appendChild(d);
      try {
        const ch = echarts.init(d, null, { renderer: 'canvas', locale: 'ZH' }); ch.setOption(o);
        made.push([k, d]);
      } catch (e) { out.errors.push(k + ': ' + e.message); }
    }
    // 桑基 / 旭日这类是从无到有的入场动画,第一帧是空的:等动画走完再量像素
    await new Promise((r) => setTimeout(r, 1500));
    for (const [k, d] of made) {
      const cv = d.querySelector('canvas'); const px = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      let n = 0; for (let i = 0; i < px.length; i += 16) if (px[i + 3] > 0 && (px[i] < 240 || px[i + 1] < 240 || px[i + 2] < 240)) n++;
      if (n < 50) out.blank.push(k);
      out.aria[k] = d.getAttribute('aria-label') || '';
    }
    return out;
  }, opts);
  res.console = logs;
  console.log(JSON.stringify(res));
  await b.close();
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
