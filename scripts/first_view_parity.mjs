#!/usr/bin/env node
/**
 * Check dashboard-summary.json's firstView block against the site's own code.
 *
 * firstView (src/first_view.py) is the Overview as it opens: study type
 * Interventional, results from 2009 on, every other filter at All. This runs
 * the site's app.js (the functions the Overview runs as it opens, sliced by
 * name the way the site's own tests slice them) in a vm, with a stub
 * document whose controls start from index.html's markup, over the same full
 * records the engine counted, and checks the block against what the site
 * computes and paints from them:
 *
 *   counts   the site's total and its race, ethnicity and both counts (what
 *            renderDashboard passes to renderOverviewFinding) are the block's;
 *            so is each results year's, with the site's year slider set to
 *            that one year; the trend chart's years are the block's years;
 *   painted  the Overview painted from the block by the site's summary painter
 *            (renderDashboard as phones run it) and by its tile-context,
 *            finding and filter-summary functions in desktop mode is the
 *            Overview the site paints from the records, string for string,
 *            and the trend chart's labels and three series are the same numbers;
 *   filter   the block's filter is the controls as startup leaves them once the
 *            records are in: the study type, the year window (yearWindowEnds),
 *            its end (datasetLatestYear) and every other control at no filter;
 *            ?sg=v2's filters, all at Any, change nothing;
 *   absence  trials plus not_counted is every record, and the site's filter
 *            with study type All splits not_counted's reasons the same way;
 *   stamps   the records, the block and the summary carry one extracted_at and
 *            pipeline_commit, and the summary counted as many records.
 *
 * The full-record file is read item by item (a week's is about 1.3 GB, over
 * V8's string limit), plain or gzipped, as src/full_records.py reads it.
 *
 * Usage:
 *   node scripts/first_view_parity.mjs --records demographics.json[.gz] --summary dashboard-summary.json
 *        [--app path/to/app.js --index path/to/index.html]
 *     The site files default to the excerpt in tests/fixtures/site_overview/;
 *     pass the site's own app.js and index.html to check against them.
 *     Prints a JSON report; exits 0 when everything matches, 1 on a mismatch,
 *     2 when an input cannot be read or the site code cannot run.
 *   node scripts/first_view_parity.mjs --excerpt --app <site app.js> --index <site index.html>
 *        --source "<repo>@<commit>" [--out-dir tests/fixtures/site_overview]
 *     Rewrites the excerpt from the site's files: the pieces below, unchanged.
 */
import { createHash } from 'node:crypto';
import { createReadStream, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { createGunzip } from 'node:zlib';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXCERPT_DIR = path.join(HERE, '..', 'tests', 'fixtures', 'site_overview');
const EXCERPT_APP = 'app_overview.js';
const EXCERPT_INDEX = 'index_overview.html';
const EXCERPT_SOURCE = 'SOURCE.json';
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

// What the Overview runs as it opens, in app.js order. ['fn', signature] is
// the site tests' fnSource: from the signature to the first "\n}\n" after it.
// ['between', from, to] is their between(): from `from` up to `to`.
const APP_PIECES = [
    ['between', 'const YEAR_WINDOW_MIN', 'function initFilters()'],   // the year window
    ['fn', 'function initFilters()'],
    ['fn', 'function getFilteredData()'],
    ['fn', 'function sgReadFilters()'],
    ['fn', 'function showDashboardSpinner()'],
    ['fn', 'function hideDashboardSpinner()'],
    ['fn', 'function renderOverviewTileContext('],
    ['fn', 'function renderOverviewFinding('],
    ['fn', 'function renderFilterSummary('],
    ['fn', 'function renderDashboard()'],
    ['fn', 'function escapeHtml('],
    ['fn', 'function renderReportingTrends('],
];
// index.html from the header (the tabs) to the end of the Overview's section:
// every element those functions read or write, the filter panel included.
const INDEX_SLICE = ['<header>', '<section id="overview"', '</section>'];

// What renderDashboard and initFilters call outside the Overview: inert here.
const ELSEWHERE = [
    'sgApplyMode', 'sgAfterRender', 'refreshStudiesTab', 'updateActiveFilters',
    'populateConditionsDropdown', 'populateCountriesDropdown', 'populateSecondaryConditionDropdown',
    'renderRaceDistribution', 'renderRaceTrends', 'renderRaceSubcategories', 'renderRaceReportedParticipants',
    'renderRaceFullDistribution', 'renderEthnicityDistribution', 'renderEthnicityTrends',
    'renderEthnicitySubcategories', 'renderEthnicityReportedParticipants', 'renderEthnicityFullDistribution',
    'renderSexReportedParticipants', 'renderSexFullDistribution', 'renderSexDistribution', 'renderSexTrends',
    'renderGenderReportedParticipants', 'renderGenderFullDistribution', 'renderGenderDistribution',
    'renderGenderTrends', 'renderFdaOversight', 'renderGeographyDashboard',
];
// Filters that apply only away from the default view. They throw here, so a
// site change that makes the default view run one fails the check.
const AWAY_FROM_DEFAULT = ['isAIStudy', 'getStudyPediatricStatus', 'studyMatchesConditionFilter', 'sgRow',
    'resetFilters', 'updateShareUrl'];

// The Overview's text, as renderDashboard and its helpers write it.
const TILES = ['total-studies', 'race-reporting', 'ethnicity-reporting', 'both-reporting'];
const SUB_LINES = ['stat-sub-race', 'stat-sub-ethnicity', 'stat-sub-both'];
const FINDING = ['finding-headline', 'finding-context'];
const NUMERATORS = ['trials_reporting_race', 'trials_reporting_ethnicity', 'trials_reporting_race_and_ethnicity'];

class InputError extends Error {}

// ── the site's files ───────────────────────────────────────────────────────

function slicePiece(text, [kind, from, to], label) {
    const at = text.indexOf(from);
    if (at < 0) throw new InputError(`${label} has no ${from}`);
    if (kind === 'fn') {
        const end = text.indexOf('\n}\n', at);
        if (end < 0) throw new InputError(`${label}: ${from} has no closing brace at the start of a line`);
        return text.slice(at, end + 2);
    }
    const end = text.indexOf(to, at);
    if (end < 0) throw new InputError(`${label} has no ${to} after ${from}`);
    return text.slice(at, end);
}

function sliceIndex(text, label) {
    const [from, section, close] = INDEX_SLICE;
    const at = text.indexOf(from);
    const open = at < 0 ? -1 : text.indexOf(section, at);
    const end = open < 0 ? -1 : text.indexOf(close, open);
    if (end < 0) throw new InputError(`${label} has no ${from} … ${section} … ${close}`);
    return text.slice(at, end + close.length);
}

function readSite(appPath, indexPath) {
    const app = readFileSync(appPath, 'utf8'), index = readFileSync(indexPath, 'utf8');
    const source = /^\/\/ Copied unchanged from (.+?):?$/m.exec(app.slice(0, 600));
    return {
        pieces: APP_PIECES.map((p) => slicePiece(app, p, appPath)),
        markup: sliceIndex(index, indexPath),
        source: source ? source[1] : `${appPath} and ${indexPath}`,
    };
}

function writeExcerpt(appPath, indexPath, source, outDir) {
    const site = readSite(appPath, indexPath);
    // The year window runs up to initFilters, so the two are written with
    // nothing between them; every piece slices back out unchanged.
    const head = [
        `// Copied unchanged from ${source}:`,
        '// the pieces of the site\'s app.js that its Overview runs as it opens, in app.js order.',
        '// Written by scripts/first_view_parity.mjs --excerpt; rewrite it that way when the site changes them.',
        '// Nothing in this repository serves or runs it except that script, which slices it by name',
        '// exactly as it slices the site\'s own app.js.',
        '',
    ].join('\n');
    const app = head + site.pieces[0] + site.pieces.slice(1).join('\n\n') + '\n';
    const index = `<!-- Copied unchanged from ${source}: index.html from its header through the Overview. ` +
        'Written by scripts/first_view_parity.mjs --excerpt. -->\n' + site.markup + '\n';
    mkdirSync(outDir, { recursive: true });
    writeFileSync(path.join(outDir, EXCERPT_APP), app);
    writeFileSync(path.join(outDir, EXCERPT_INDEX), index);
    const back = readSite(path.join(outDir, EXCERPT_APP), path.join(outDir, EXCERPT_INDEX));
    back.pieces.forEach((p, i) => {
        if (p !== site.pieces[i]) throw new InputError(`the excerpt does not slice back to ${APP_PIECES[i][1]}`);
    });
    if (back.markup !== site.markup) throw new InputError('the excerpt does not slice back to the markup');
    // The pins: the site's own files at that commit, and the excerpt written
    // from them. tests/test_first_view.py checks the excerpt against the second,
    // and, fetching the site's files at the commit, against the first, so the
    // excerpt cannot drift from the commit it names without failing CI.
    const pins = {
        about: 'Written by scripts/first_view_parity.mjs --excerpt with the excerpt beside it; never edited by hand.',
        source,
        site_files: { 'app.js': sha256(readFileSync(appPath)), 'index.html': sha256(readFileSync(indexPath)) },
        excerpt: { [EXCERPT_APP]: sha256(Buffer.from(app)), [EXCERPT_INDEX]: sha256(Buffer.from(index)) },
    };
    writeFileSync(path.join(outDir, EXCERPT_SOURCE), JSON.stringify(pins, null, 2) + '\n');
    return { app: path.join(outDir, EXCERPT_APP), index: path.join(outDir, EXCERPT_INDEX),
             source: path.join(outDir, EXCERPT_SOURCE), pieces: site.pieces.length };
}

// ── the full records ───────────────────────────────────────────────────────

// The container's members, with its "data" array read item by item: each
// item is parsed on its own, so no string ever holds the whole file.
async function readFullRecords(file) {
    const raw = createReadStream(file);
    const stream = file.endsWith('.gz') ? raw.pipe(createGunzip()) : raw;
    stream.setEncoding('utf8');
    const skeleton = [];          // the file's text without the data items
    let skLen = 0;
    const records = [];
    let item = [];
    let depth = 0, inString = false, escaped = false;
    let mode = 0;                 // 0 outside data, 1 in data between items, 2 in an item
    let memberStart = 0;          // where the current top-level member starts in the skeleton
    let sawData = false;
    const keep = (text) => { skeleton.push(text); skLen += text.length; };
    const finishItem = () => { records.push(JSON.parse(item.join(''))); item = []; };
    for await (const chunk of stream) {
        let mark = 0;
        for (let i = 0; i < chunk.length; i++) {
            const c = chunk.charCodeAt(i);
            if (inString) {
                if (escaped) escaped = false;
                else if (c === 92) escaped = true;            // backslash
                else if (c === 34) inString = false;          // quote
                continue;
            }
            if (mode === 1) {
                if (c === 93) { mode = 0; depth = 1; mark = i; continue; }   // ] ends data
                if (c === 44 || c === 32 || c === 10 || c === 13 || c === 9) continue;
                mode = 2; mark = i;                           // an item starts here
            }
            if (c === 34) { inString = true; continue; }
            if (c === 123 || c === 91) {                      // { [
                if (mode === 0 && depth === 0 && c === 123) memberStart = skLen + (i + 1 - mark);
                if (mode === 0 && depth === 1 && c === 91) {
                    const member = (skeleton.join('') + chunk.slice(mark, i)).slice(memberStart);
                    const key = /^\s*("(?:[^"\\]|\\.)*")\s*:\s*$/.exec(member);
                    if (key && JSON.parse(key[1]) === 'data') {
                        if (sawData) throw new InputError(`${file}: more than one data member`);
                        sawData = true;
                        keep(chunk.slice(mark, i + 1));
                        mark = i + 1; mode = 1; depth = 2;
                        continue;
                    }
                }
                depth++;
                continue;
            }
            if (c === 125 || c === 93) {                      // } ]
                if (mode === 2 && depth === 2) {
                    if (c !== 93) throw new InputError(`${file}: malformed data array`);
                    item.push(chunk.slice(mark, i)); finishItem();
                    mode = 0; depth = 1; mark = i;
                    continue;
                }
                depth--;
                continue;
            }
            if (c === 44) {                                   // ,
                if (mode === 2 && depth === 2) { item.push(chunk.slice(mark, i)); finishItem(); mode = 1; continue; }
                if (mode === 0 && depth === 1) memberStart = skLen + (i + 1 - mark);
            }
        }
        if (mode === 0) keep(chunk.slice(mark));
        else if (mode === 2) item.push(chunk.slice(mark));
    }
    if (mode !== 0 || depth !== 0 || inString) throw new InputError(`${file}: ends inside the JSON`);
    if (!sawData) throw new InputError(`${file}: no data array`);
    const container = JSON.parse(skeleton.join(''));
    return { records, extracted_at: container.extracted_at ?? null, pipeline_commit: container.pipeline_commit ?? null };
}

// ── a document for the site's code ─────────────────────────────────────────

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
const decode = (s) => s.replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) => {
    if (e[0] === '#') return String.fromCodePoint(e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
    return ENTITIES[e.toLowerCase()] ?? m;
});

function attributes(text) {
    const out = {};
    for (const m of text.matchAll(/([^\s"'>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) {
        out[m[1].toLowerCase()] = decode(m[2] ?? m[3] ?? m[4] ?? '');
    }
    return out;
}

function classList(names) {
    const set = new Set(names.split(/\s+/).filter(Boolean));
    return {
        contains: (c) => set.has(c), add: (...c) => c.forEach((x) => set.add(x)),
        remove: (...c) => c.forEach((x) => set.delete(x)),
        toggle: (c, force) => ((force ?? !set.has(c)) ? (set.add(c), true) : (set.delete(c), false)),
    };
}

function element(tag, attrs) {
    const style = {};
    for (const decl of (attrs.style || '').split(';')) {
        const [k, v] = decl.split(':').map((s) => s && s.trim());
        if (k && v !== undefined) style[k.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = v;
    }
    const dataset = {};
    for (const [k, v] of Object.entries(attrs)) {
        if (k.startsWith('data-')) dataset[k.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = v;
    }
    const listeners = {};
    return {
        tagName: tag.toUpperCase(), id: attrs.id ?? '', attrs, textContent: '', innerHTML: '',
        hidden: 'hidden' in attrs, disabled: 'disabled' in attrs, style, dataset, classList: classList(attrs.class || ''),
        listeners,
        addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
        removeEventListener() {},
        setAttribute(name, value) { attrs[name] = String(value); },
        getAttribute: (name) => (name in attrs ? attrs[name] : null),
        querySelector: () => null, querySelectorAll: () => [], appendChild: (child) => child,
    };
}

// A range input as a browser keeps one: its value stays a step of 1 inside
// [min, max], and moving min or max re-clamps it.
function rangeInput(el, attrs) {
    let min = attrs.min ?? '0', max = attrs.max ?? '100', value = attrs.value ?? '';
    const clamp = (v) => {
        const lo = parseFloat(min), hi = Math.max(parseFloat(max), lo), n = parseFloat(v);
        if (!Number.isFinite(n)) return String(lo + Math.round((hi - lo) / 2));
        return String(Math.min(Math.max(lo + Math.round(n - lo), lo), hi));
    };
    value = clamp(value);
    Object.defineProperties(el, {
        type: { value: 'range', enumerable: true },
        min: { get: () => min, set: (v) => { min = String(v); value = clamp(value); }, enumerable: true },
        max: { get: () => max, set: (v) => { max = String(v); value = clamp(value); }, enumerable: true },
        value: { get: () => value, set: (v) => { value = clamp(v); }, enumerable: true },
    });
}

function selectElement(el, options) {
    let index = Math.max(options.findIndex((o) => o.selected), options.length ? 0 : -1);
    Object.defineProperties(el, {
        options: { get: () => options, enumerable: true },
        selectedIndex: { get: () => index, set: (v) => { index = v >= 0 && v < options.length ? v : -1; }, enumerable: true },
        value: { get: () => (index >= 0 ? options[index].value : ''),
                 set: (v) => { index = options.findIndex((o) => o.value === String(v)); }, enumerable: true },
    });
    el.querySelector = (sel) => (sel === 'option' ? options[0] ?? null : null);
}

function buildDocument(markup) {
    const html = markup.replace(/<!--[\s\S]*?-->/g, (m) => ' '.repeat(m.length));
    const elements = [], byId = new Map();
    let panel = null;
    for (const m of html.matchAll(/<([a-zA-Z][\w-]*)(\s[^<>]*?)?\s*\/?>/g)) {
        const tag = m[1].toLowerCase(), attrs = attributes(m[2] || '');
        const el = element(tag, attrs);
        el.at = m.index;
        if (tag === 'input') {
            const type = (attrs.type || 'text').toLowerCase();
            if (type === 'range') rangeInput(el, attrs);
            else {
                el.type = type;
                el.value = attrs.value ?? (type === 'checkbox' ? 'on' : '');
                el.checked = 'checked' in attrs;
            }
        } else if (tag === 'select') {
            const body = html.slice(m.index + m[0].length, html.indexOf('</select>', m.index));
            const options = [...body.matchAll(/<option(\s[^>]*)?>([\s\S]*?)<\/option>/g)].map((o) => {
                const a = attributes(o[1] || '');
                const text = decode(o[2].replace(/<[^>]*>/g, '')).replace(/[\t\n\f\r ]+/g, ' ').trim();
                return { value: 'value' in a ? a.value : text, text, selected: 'selected' in a };
            });
            selectElement(el, options);
        }
        if (tag === 'section' && attrs.id === 'filters') panel = { from: m.index, to: html.indexOf('</section>', m.index) };
        elements.push(el);
        if (attrs.id && !byId.has(attrs.id)) byId.set(attrs.id, el);
    }
    const select = (sel) => {
        const m = /^((?:\.[\w-]+)+)$/.exec(sel);
        if (!m) throw new Error(`the stub document answers class selectors only, not ${sel}`);
        const names = m[1].split('.').filter(Boolean);
        return elements.filter((e) => names.every((c) => e.classList.contains(c)));
    };
    const document = {
        getElementById: (id) => byId.get(id) ?? null,
        querySelector: (sel) => select(sel)[0] ?? null,
        querySelectorAll: (sel) => select(sel),
        createElement(tag) {
            if (String(tag).toLowerCase() !== 'div') return element(String(tag), {});
            // escapeHtml's div: its markup is its text, escaped as a browser serialises a text node
            let text = '';
            return {
                set textContent(v) { text = String(v); }, get textContent() { return text; },
                get innerHTML() {
                    return text.replace(/&/g, '&amp;').replace(/\u00a0/g, '&nbsp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                },
            };
        },
    };
    const controls = panel ? elements.filter((e) => e.at > panel.from && e.at < panel.to
        && (e.tagName === 'SELECT' || e.tagName === 'INPUT')) : [];
    return { document, byId, controls };
}

// ── the site's code in a vm ────────────────────────────────────────────────

function siteRuntime(site) {
    const { document, byId, controls } = buildDocument(site.markup);
    const charts = [];
    let sgOn = false;
    const sandbox = {
        document, window: {}, console: { log() {}, info() {}, debug() {}, warn() {}, error() {} },
        requestAnimationFrame: (fn) => { fn(); return 1; }, cancelAnimationFrame() {},
        Chart: class { constructor(canvas, config) { charts.push({ canvas, config }); } destroy() {} },
        sgActive: () => sgOn,
    };
    for (const name of ELSEWHERE) sandbox[name] = () => {};
    for (const name of AWAY_FROM_DEFAULT) {
        sandbox[name] = () => { throw new Error(`${name} ran in the Overview's default view`); };
    }
    const context = vm.createContext(sandbox);
    vm.runInContext([
        'let data = null; let dashboardSummary = null; let charts = {}; let sgV2Filters = null;',
        "const COLORS = { reporting: { race: '#000000', ethnicity: '#000000', both: '#000000' } };",
        'const CHART_ASPECT_RATIO = undefined;',
        ...site.pieces,
        // The counts the site paints, as renderDashboard hands them over.
        'var __finding = [];',
        'const __siteFinding = renderOverviewFinding;',
        'renderOverviewFinding = function (...args) { __finding.push(args); return __siteFinding(...args); };',
    ].join('\n'), context, { filename: 'site-overview.js' });
    const run = (code) => vm.runInContext(code, context);
    return {
        context, byId, controls, run,
        setSg: (on) => { sgOn = on; },
        lastCounts: () => run('__finding.length ? __finding[__finding.length - 1].slice() : null'),
        chart: () => {
            const made = charts.filter((c) => c.canvas && c.canvas.id === 'reporting-trends-chart');
            if (!made.length) return null;
            const { data } = made[made.length - 1].config;
            return { labels: [...data.labels], series: data.datasets.map((d) => ({ label: d.label, data: [...d.data] })) };
        },
        text: (id) => {
            const el = byId.get(id);
            if (!el) throw new InputError(`index.html has no #${id}`);
            return { text: el.textContent, html: el.innerHTML };
        },
    };
}

// ── the comparison ─────────────────────────────────────────────────────────

const counts = (c) => ({ trials: c[0], ...Object.fromEntries(NUMERATORS.map((n, i) => [n, c[i + 1]])) });
const sameNumbers = (a, b) => a.length === b.length && a.every((x, i) => Object.is(x, b[i]));

// The block in the summary's shape, renamed only, for the summary painter.
function asSummary(block) {
    const one = (c) => ({ total: c.trials, race_reported: c.trials_reporting_race,
        eth_reported: c.trials_reporting_ethnicity, both_reported: c.trials_reporting_race_and_ethnicity });
    return {
        totalStudies: block.trials,
        cards: { raceCount: block.trials_reporting_race, ethCount: block.trials_reporting_ethnicity,
                 bothCount: block.trials_reporting_race_and_ethnicity },
        byYear: Object.fromEntries(Object.entries(block.by_results_year).map(([y, c]) => [y, one(c)])),
    };
}

function shapeProblems(block) {
    const out = [];
    const isCount = (v) => Number.isInteger(v) && v >= 0;
    const checkCounts = (c, where) => {
        if (!c || typeof c !== 'object') { out.push(`${where} is not an object`); return; }
        for (const k of ['trials', ...NUMERATORS]) if (!isCount(c[k])) out.push(`${where}.${k} is not a count`);
        for (const k of NUMERATORS) if (c[k] > c.trials) out.push(`${where}.${k} exceeds its trials`);
    };
    checkCounts(block, 'firstView');
    for (const k of NUMERATORS) {
        if (block.denominators?.[k] !== 'trials') out.push(`firstView.denominators.${k} does not name trials`);
    }
    const years = block.by_results_year && typeof block.by_results_year === 'object' ? block.by_results_year : {};
    if (years !== block.by_results_year) out.push('firstView.by_results_year is not an object');
    for (const [y, c] of Object.entries(years)) {
        if (!/^\d{4}$/.test(y)) out.push(`firstView.by_results_year has a key ${y} that is not a year`);
        checkCounts(c, `firstView.by_results_year.${y}`);
    }
    for (const k of ['trials', ...NUMERATORS]) {
        const sum = Object.values(years).reduce((s, c) => s + (c?.[k] ?? 0), 0);
        if (sum !== block[k]) out.push(`firstView.${k} is ${block[k]}, its years sum to ${sum}`);
    }
    if (!block.not_counted || !Object.values(block.not_counted).every(isCount)) out.push('firstView.not_counted is not a set of counts');
    if (!block.filter || typeof block.filter !== 'object') out.push('firstView.filter is missing');
    return out;
}

function check(summaryFile, appPath, indexPath, full) {
    const checks = [];
    const ok = (name, pass, detail) => { checks.push({ check: name, ok: !!pass, ...(pass ? {} : { detail }) }); return pass; };
    const summary = JSON.parse(readFileSync(summaryFile, 'utf8'));
    const block = summary.firstView;
    if (!block || typeof block !== 'object') throw new InputError(`${summaryFile} has no firstView block`);
    const shape = shapeProblems(block);
    ok('the block is well formed', !shape.length, shape);
    if (shape.length) return { checks };

    const { records } = full;
    ok('stamps: the records, the block and the summary are one run',
       full.extracted_at === block.extracted_at && block.extracted_at === summary.extracted_at
       && full.pipeline_commit === block.pipeline_commit && block.pipeline_commit === summary.pipeline_commit,
       { records: [full.extracted_at, full.pipeline_commit], block: [block.extracted_at, block.pipeline_commit],
         summary: [summary.extracted_at, summary.pipeline_commit] });
    ok('the summary counted the same records', summary.totalStudies === records.length,
       { summary: summary.totalStudies, records: records.length });

    const site = readSite(appPath, indexPath);
    const s = siteRuntime(site);
    s.context.__records = records;

    // 1. The desktop Overview from the records, as startup leaves it.
    s.run('data = __records; dashboardSummary = null; initFilters(); renderDashboard();');
    const fromRecords = counts(s.lastCounts());
    const painted = Object.fromEntries([...TILES, 'stat-sub-total', ...SUB_LINES, ...FINDING, 'filter-summary-text']
        .map((id) => [id, s.text(id)]));
    const findingShown = !s.byId.get('overview-finding').hidden;
    const chart = s.chart();
    const filtered = s.run('getFilteredData().length');
    for (const k of ['trials', ...NUMERATORS]) {
        ok(`counts: ${k}`, fromRecords[k] === block[k], { site: fromRecords[k], block: block[k] });
    }
    ok('counts: the site\'s filtered records are its total', filtered === fromRecords.trials, { filtered, total: fromRecords.trials });
    ok('counts: the trend chart lists the block\'s years',
       chart && JSON.stringify(chart.labels) === JSON.stringify(Object.keys(block.by_results_year).sort()),
       { chart: chart && chart.labels, block: Object.keys(block.by_results_year) });

    // 2. The filter the counts were made under is the page's.
    const yearWindow = s.run('(() => { const w = yearWindowEnds(); return [w.start, w.end === Infinity ? null : w.end]; })()');
    const newest = s.run('datasetLatestYear()');
    const ys = s.byId.get('year-start'), ye = s.byId.get('year-end');
    ok('filter: study type', s.byId.get('study-type').value === block.filter.study_type,
       { page: s.byId.get('study-type').value, block: block.filter.study_type });
    ok('filter: the year window', yearWindow[0] === block.filter.results_year_from && yearWindow[1] === block.filter.results_year_to,
       { page: yearWindow, block: [block.filter.results_year_from, block.filter.results_year_to] });
    ok('filter: the newest results year', newest === block.newest_results_year, { page: newest, block: block.newest_results_year });
    if (newest !== null) {
        ok('filter: the slider shows results_year_from to newest_results_year',
           ys.value === String(block.filter.results_year_from) && ye.value === String(block.newest_results_year)
           && ye.max === String(block.newest_results_year),
           { slider: [ys.value, ye.value, ye.max] });
    }
    // No filter: a select at All, a number box empty, a checkbox clear.
    const atNoFilter = (c) => (c.tagName === 'SELECT' ? c.value === 'all'
        : c.type === 'checkbox' ? !c.checked : c.type === 'range' || c.value === '');
    const narrowed = s.controls
        .filter((c) => !['study-type', 'year-start', 'year-end'].includes(c.id) && !atNoFilter(c))
        .map((c) => `#${c.id}`);
    ok('filter: every other control is at no filter', block.filter.other_filters === 'none' && !narrowed.length,
       { block: block.filter.other_filters, narrowed });
    s.setSg(true);
    const withSg = s.run('getFilteredData().length');
    s.setSg(false);
    ok('filter: ?sg=v2\'s filters at Any change nothing', withSg === fromRecords.trials, { sg: withSg, default: fromRecords.trials });

    // 3. The block painted by the site's summary painter, as phones paint a summary.
    s.context.__summary = asSummary(block);
    s.run('dashboardSummary = __summary; renderDashboard();');
    for (const id of [...TILES, ...SUB_LINES, ...FINDING]) {
        const got = s.text(id);
        ok(`painted: #${id}`, got.text === painted[id].text && got.html === painted[id].html, { records: painted[id], block: got });
    }
    ok('painted: the finding is shown', !s.byId.get('overview-finding').hidden === findingShown, {});
    const blockChart = s.chart();
    ok('painted: the trend chart\'s years', chart && blockChart && JSON.stringify(blockChart.labels) === JSON.stringify(chart.labels),
       { records: chart && chart.labels, block: blockChart && blockChart.labels });
    for (const [i, series] of (chart ? chart.series : []).entries()) {
        const other = blockChart && blockChart.series[i];
        ok(`painted: the trend chart's ${series.label} series`, other && other.label === series.label && sameNumbers(series.data, other.data),
           { records: series.data, block: other && other.data });
    }

    // 4. The block through the desktop painters: the lines that read the controls.
    const args = [block.trials, ...NUMERATORS.map((k) => block[k])].join(', ');
    s.run(`dashboardSummary = null; renderOverviewTileContext(${args}); renderOverviewFinding(${args}); renderFilterSummary(${block.trials});`);
    for (const id of ['stat-sub-total', ...SUB_LINES, ...FINDING, 'filter-summary-text']) {
        const got = s.text(id);
        ok(`painted on desktop: #${id}`, got.text === painted[id].text && got.html === painted[id].html, { records: painted[id], block: got });
    }

    // 5. Each results year: the site's own count with the slider on that year alone.
    const years = [...new Set([...Object.keys(block.by_results_year), ...(chart ? chart.labels : [])])].sort();
    const siteYears = {};
    for (const y of years) {
        s.run(`(() => { const ys = document.getElementById('year-start'), ye = document.getElementById('year-end');
            ye.value = '${y}'; ys.value = '${y}'; noteYearChoice(ys); noteYearChoice(ye);
            dashboardSummary = null; renderDashboard(); })()`);
        siteYears[y] = counts(s.lastCounts());
        const want = block.by_results_year[y] ?? { trials: 0, ...Object.fromEntries(NUMERATORS.map((k) => [k, 0])) };
        const same = ['trials', ...NUMERATORS].every((k) => siteYears[y][k] === want[k]);
        ok(`counts in ${y}`, same, { site: siteYears[y], block: block.by_results_year[y] ?? null });
    }

    // 6. Absence: what the default view leaves out, by the site's own filter.
    s.run('resetYearWindow(); document.getElementById(\'study-type\').value = \'all\';');
    const allTypes = s.run('getFilteredData().length');
    s.run(`document.getElementById('study-type').value = ${JSON.stringify(block.filter.study_type)};`);
    const nc = block.not_counted;
    const noDate = records.filter((r) => r.results_date == null || r.results_date === '').length;
    const notFrom = Object.entries(nc).find(([k]) => k.startsWith('results_year_not_from_'))?.[1];
    ok('absence: trials plus not_counted is every record',
       block.trials + Object.values(nc).reduce((a, b) => a + b, 0) === records.length,
       { trials: block.trials, not_counted: nc, records: records.length });
    ok('absence: not interventional, by the site\'s filter with study type All',
       nc.not_interventional === allTypes - block.trials, { block: nc.not_interventional, site: allTypes - block.trials });
    ok('absence: no results date', nc.no_results_date === noDate, { block: nc.no_results_date, records: noDate });
    ok('absence: a results date that is not a year from 2009 on', notFrom === records.length - allTypes - noDate,
       { block: notFrom, site: records.length - allTypes - noDate });

    return {
        checks,
        site: { ...fromRecords, newest_results_year: newest, by_results_year: siteYears,
                painted: Object.fromEntries(Object.entries(painted).map(([id, v]) => [id, v.html || v.text])) },
        site_code: site.source,
    };
}

// ── command line ───────────────────────────────────────────────────────────

function parseArgs(argv) {
    const out = { excerpt: false };
    for (let i = 0; i < argv.length; i++) {
        const a = argv[i];
        if (a === '--excerpt') { out.excerpt = true; continue; }
        const m = /^--(records|summary|app|index|source|out-dir)$/.exec(a);
        if (!m || i + 1 >= argv.length) throw new InputError(`unknown or incomplete argument ${a}`);
        out[m[1]] = argv[++i];
    }
    return out;
}

async function main() {
    const a = parseArgs(process.argv.slice(2));
    if (a.excerpt) {
        if (!a.app || !a.index || !a.source) throw new InputError('--excerpt needs --app, --index and --source');
        console.log(JSON.stringify(writeExcerpt(a.app, a.index, a.source, a['out-dir'] || EXCERPT_DIR), null, 2));
        return 0;
    }
    if (!a.records || !a.summary) throw new InputError('needs --records and --summary');
    const appPath = a.app || path.join(EXCERPT_DIR, EXCERPT_APP);
    const indexPath = a.index || path.join(EXCERPT_DIR, EXCERPT_INDEX);
    const t0 = Date.now();
    const full = await readFullRecords(a.records);
    const t1 = Date.now();
    const result = check(a.summary, appPath, indexPath, full);
    const failed = result.checks.filter((c) => !c.ok);
    const report = {
        ok: failed.length === 0,
        records: { file: a.records, count: full.records.length, extracted_at: full.extracted_at, pipeline_commit: full.pipeline_commit },
        site_code: result.site_code ?? null,
        checked: result.checks.length,
        mismatches: failed,
        site: result.site ?? null,
        seconds: { read: (t1 - t0) / 1000, check: (Date.now() - t1) / 1000 },
    };
    console.log(JSON.stringify(report, null, 2));
    return failed.length ? 1 : 0;
}

main().then((code) => { process.exitCode = code; }, (err) => {
    console.error(err instanceof InputError ? `first_view_parity: ${err.message}` : err);
    process.exitCode = 2;
});
