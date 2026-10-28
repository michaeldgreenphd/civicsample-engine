const { chromium } = require('playwright');
(async () => {
  const out = process.argv[2]; const base = 'http://127.0.0.1:8011/?sg=v2';
  const browser = await chromium.launch();
  const results = {};
  for (const [name, vp] of [['desktop', { width: 1366, height: 900 }], ['mobile390', { width: 390, height: 844 }]]) {
    const ctx = await browser.newContext({ viewport: vp, deviceScaleFactor: 1, isMobile: name !== 'desktop', hasTouch: name !== 'desktop' });
    const page = await ctx.newPage();
    const errors = []; page.on('pageerror', e => errors.push(String(e))); page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    // The sandbox cannot reach the CDN: serve the same pinned versions from local copies, fonts as empty.
    const V = (process.env.CHARTJS_VENDOR_DIR || '') + '/';
    await page.route('**/chart.js@4.5.1/dist/chart.umd.min.js', r => r.fulfill({ path: V + 'chart.js-4.5.1/package/dist/chart.umd.min.js', contentType: 'application/javascript' }));
    await page.route('**/chartjs-plugin-datalabels@2.0.0/dist/chartjs-plugin-datalabels.min.js', r => r.fulfill({ path: V + 'chartjs-plugin-datalabels-2.0.0/package/dist/chartjs-plugin-datalabels.min.js', contentType: 'application/javascript' }));
    await page.route('https://fonts.googleapis.com/**', r => r.fulfill({ body: '', contentType: 'text/css' }));
    await page.route('https://fonts.gstatic.com/**', r => r.fulfill({ body: '', contentType: 'font/woff2' }));
    await page.goto(base, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    results[name] = { errors, pages: {} };
    const tab = async (t) => { await page.evaluate((t) => { const b = document.querySelector(`button.tab[data-tab="${t}"]`); if (b) b.click(); }, t); await page.waitForTimeout(2500); };
    await tab('sex');
    results[name].pages.sex = { analysisTable: await page.locator('#sg-analysis-table').innerText().catch(() => null) };
    await page.screenshot({ path: `${out}/${name}-sex.png`, fullPage: true });
    await tab('gender');
    results[name].pages.gender = { categories: await page.locator('#sg-gender-categories').innerText().catch(() => null) };
    await page.screenshot({ path: `${out}/${name}-gender.png`, fullPage: true });
    // The sex/gender pop-up for one record: the "Newer analysis" line lives here.
    await page.evaluate(() => showBreakdown('NCT99000010', 'sex'));
    await page.waitForTimeout(2500);
    const popup = await page.locator('body').innerText();
    results[name].pages.popup = { newerAnalysis: (popup.match(/Newer analysis[\s\S]{0,420}/) || [null])[0] };
    await page.screenshot({ path: `${out}/${name}-popup.png`, fullPage: false });
    await page.evaluate(() => { document.querySelectorAll('.modal, .modal-overlay').forEach(m => { m.style.display = 'none'; }); });
    await tab('studies');
    const rowFound = await page.locator('#studies-table-body tr', { hasText: 'NCT99000010' }).count();
    await page.evaluate(() => showStudyDetails('NCT99000010'));
    await page.waitForTimeout(3000);
    const body = await page.locator('body').innerText();
    results[name].pages.detail = {
      rowFound: rowFound > 0,
      hasEnrichmentSection: body.includes('Additional reviewed evidence'),
      enrichmentExcerpt: (body.match(/Additional reviewed evidence[\s\S]{0,420}/) || [null])[0],
      hasNewerAnalysis: body.includes('Newer analysis'),
      newerAnalysisExcerpt: (body.match(/Newer analysis[\s\S]{0,300}/) || [null])[0],
    };
    await page.screenshot({ path: `${out}/${name}-detail.png`, fullPage: true });
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify(results, null, 1));
})().catch(e => { console.error(e); process.exit(1); });
