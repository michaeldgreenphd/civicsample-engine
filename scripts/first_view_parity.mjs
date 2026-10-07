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
 * Every other function app.js declares at the top level runs as an inert
 * stub that records each call (autoStubs): a site change that adds a call
 * from one of the pieces to a helper of its own (a chart added to the
 * Overview) does not stop the check. So do the functions stubbed by name
 * (ELSEWHERE), which the default view always calls. Whenever any stub ran,
 * the site code runs a second time with every stub returning true instead
 * of nothing: a function whose answer the Overview uses (a new exclusion,
 * `if (isX(study)) return false;`, is a no-op on undefined) changes what the
 * second run sees, and halves of the stubs that ran are then flipped alone to
 * name the ones whose answer matters. Answers can cancel out (`if (A() &&
 * !B(study)) return false;` excludes nothing either way), so when both runs
 * see the same, the stubs whose answer the Overview may read (any mention
 * but a lone call statement, answerReadFrom; none on site main today) are
 * flipped in combination as well: every combination of up to four, else
 * each one alone and all but each one. When every run sees the same and the
 * numbers agree, the check passes (exit 0); the report names the stubs that
 * ran, and a note on stderr names the ones found in app.js (the ELSEWHERE
 * ones run every time and are not noted). When a run with answers flipped
 * differs, the numbers differ with one of app.js's stubs run, or the site
 * code throws with one run, the check could not run faithfully: the
 * difference may be the stub's, so it is exit 2, never a mismatch (exit 1). A rename or
 * removal of a piece, and a call to a name app.js never declares as a
 * function, stay exit 2 as before. What no run sees: a function whose
 * answer matters only as some value other than nothing or true
 * (`=== 'withdrawn'`); a rule over five or more read stubs that needs two
 * or more true while two or more others answer nothing; and one that
 * repaints an Overview number on the real page.
 *
 * The full-record file is read item by item (a week's is about 1.3 GB, over
 * V8's string limit), plain or gzipped, as src/full_records.py reads it.
 * --records may be given more than once: the weekly publish passes the
 * site's staged parts (data/demographics.partN.json.gz, each a container of
 * the same shape with its own "data" array), and their records are counted
 * together, in the order given. The files must be one run: a file whose
 * extracted_at or pipeline_commit is not the first file's is an input error.
 *
 * Usage:
 *   node scripts/first_view_parity.mjs --records demographics.json[.gz] [--records more.json.gz ...]
 *        --summary dashboard-summary.json [--app path/to/app.js --index path/to/index.html]
 *     The site files default to the excerpt in tests/fixtures/site_overview/;
 *     pass the site's own app.js and index.html to check against them.
 *     Prints a JSON report; exits 0 when everything matches, 1 on a mismatch,
 *     2 when an input cannot be read or the site code cannot run, or cannot
 *     run faithfully (below).
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

// What renderDashboard and initFilters call outside the Overview today, kept
// by name because the vendored excerpt holds the pieces only; any other
// top-level function of the site's own app.js is stubbed by autoStubs below.
// None paints the Overview's numbers. Each stub returns nothing, which is
// what the site's own function returns (site main, 2026-10-07: none has a
// `return` with a value outside its nested callbacks), and every call the
// pieces make to one throws the value away. Each is still treated exactly
// as an automatic stub: its calls are recorded (the report's
// predeclared_stubs_run), and it returns true in the second run, so a site
// change that starts using one's answer (`if (updateActiveFilters(study))
// return false;`) is caught rather than answered with the stub's nothing.
// None is given a value that would keep it out of that run: should the
// Overview come to need one's answer, give it here a stub that mirrors the
// site's function, or run it (APP_PIECES). They are not noted on stderr when
// they run, since the default view always runs them.
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
// What the check itself gives the site's code (siteRuntime): never stubbed.
const PROVIDED = ['document', 'window', 'console', 'requestAnimationFrame', 'cancelAnimationFrame', 'Chart',
    'sgActive', 'data', 'dashboardSummary', 'charts', 'sgV2Filters', 'COLORS', 'CHART_ASPECT_RATIO'];
const PREDECLARED = new Set(ELSEWHERE);

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

// A top-level declaration starts a line: app.js indents everything nested,
// as the site tests' fnSource ("\n}\n") already relies on. A function is a
// `function NAME(` (async and generator ones too), or a const/let/var bound
// to a function expression or an arrow, `NAME = (a, b) =>` or `NAME = a =>`.
// An arrow that is called on the spot, `NAME = (() => { ... })()`, is a value,
// not a function, and is left alone ([^()] stops at its inner parenthesis).
const TOP_FUNCTION = /^(async[ \t]+)?function\b[ \t]*\*?[ \t]*([A-Za-z_$][\w$]*)[ \t]*\(/gm;
const TOP_FUNCTION_VALUE = /^(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*=[ \t]*(async\b[ \t]*)?(?:function\b|\([^()]*\)[ \t]*=>|[A-Za-z_$][\w$]*[ \t]*=>)/gm;
const TOP_NAME = /^(?:(?:async[ \t]+)?function\b[ \t]*\*?[ \t]*|(?:const|let|var|class)[ \t]+)([A-Za-z_$][\w$]*)/gm;

// Every function app.js declares at the top level that the check does not
// run, as [{ name, async }] by name: what a piece may call besides the fixed
// list. Left out: the pieces' own names (their declarations are the site's
// code), the fixed list, the filters that must throw on the default view,
// and what the check provides. A name app.js binds some other way (a
// top-level value, a class, an object's method, window.NAME = ...) is not
// seen, and a call to it still stops the check as a name not defined.
export function autoStubs(app, pieces = APP_PIECES.map((p) => slicePiece(app, p, 'app.js'))) {
    const own = new Set([...PROVIDED, ...ELSEWHERE, ...AWAY_FROM_DEFAULT]);
    for (const piece of pieces) for (const m of piece.matchAll(TOP_NAME)) own.add(m[1]);
    const found = new Map();
    for (const m of app.matchAll(TOP_FUNCTION)) if (!own.has(m[2])) found.set(m[2], !!m[1]);
    for (const m of app.matchAll(TOP_FUNCTION_VALUE)) if (!own.has(m[1])) found.set(m[1], !!m[2]);
    return [...found].sort(([a], [b]) => (a < b ? -1 : 1)).map(([name, isAsync]) => ({ name, async: isAsync }));
}

// Which of the named stubs the code may read the answer of: every one it
// mentions anywhere but on a line that throws the answer away. Such a line is
// a lone call statement, `NAME(args);` (perhaps after `if (...)`, `else` or
// `await`, perhaps with a comment after the semicolon; the arguments hold no
// parentheses outside plain strings), after a line that ends a statement or
// opens or closes a block (`;`, `{`, `}`, `)`), so it cannot continue an
// expression. `typeof NAME` reads no answer, and a whole-line `//` comment is
// skipped. Anything else that names a stub, a call whose value is kept or
// tested, a reference passed on (`rows.filter(NAME)`), a call inside a
// template or another call, or a line the scan cannot place, counts as read:
// a wrong guess that way costs runs only. app.js's code is read line by line
// as written (the site's style: one statement a line); a statement split
// oddly, a `for` header with its test alone on a line, could pass for a lone
// call, and its stub would then answer true only with every other.
export function answerReadFrom(names, code) {
    const lines = code.split(/\r?\n/);
    const read = new Set();
    let ended = true;       // the line before ends a statement or opens or closes a block
    for (const line of lines) {
        const bare = line.trim();
        if (!bare || bare.startsWith('//')) continue;
        for (const name of names) {
            if (read.has(name)) continue;
            const id = name.replace(/\$/g, '\\$');
            const mention = new RegExp(`(?<![\\w$.])${id}(?![\\w$])`, 'g');
            const rest = line.replace(new RegExp(`(?<![\\w$.])typeof[ \\t]+${id}(?![\\w$])`, 'g'), 'typeof _');
            const count = (rest.match(mention) || []).length;
            if (!count) continue;
            const arg = `(?:[^()'"\`\\n]|'[^'\\\\\\n]*'|"[^"\\\\\\n]*")*`;
            const call = new RegExp(`^[ \\t]*(?:(?:\\}[ \\t]*)?else[ \\t]+)?(?:if[ \\t]*\\(.*\\)[ \\t]*)?`
                + `(?:await[ \\t]+)?${id}[ \\t]*\\(${arg}\\)[ \\t]*;[ \\t]*(?:\\/\\/.*)?$`);
            // The call itself is the line's one mention, the condition before it naming the stub only by typeof.
            if (!(ended && count === 1 && call.test(rest))) read.add(name);
        }
        ended = /[;{})]$/.test(bare.replace(/[ \t]*\/\/[^'"`]*$/, ''));
    }
    return names.filter((n) => read.has(n)).sort();
}

// Sets of the stubs whose answers are read, each flipped to true while the
// rest answer nothing: every combination of up to four (the run with them all
// true has been made), else each one alone and all but each one. Smallest first.
function flipSets(names) {
    if (names.length < 2) return [];
    if (names.length <= 4) {
        const sets = [];
        for (let mask = 1; mask < (1 << names.length) - 1; mask++) sets.push(names.filter((_, i) => mask & (1 << i)));
        return sets.sort((a, b) => a.length - b.length);
    }
    return [...names.map((n) => [n]), ...names.map((n) => names.filter((m) => m !== n))];
}

function readSite(appPath, indexPath) {
    const app = readFileSync(appPath, 'utf8'), index = readFileSync(indexPath, 'utf8');
    const source = /^\/\/ Copied unchanged from (.+?):?$/m.exec(app.slice(0, 600));
    const pieces = APP_PIECES.map((p) => slicePiece(app, p, appPath));
    return {
        pieces,
        stubs: autoStubs(app, pieces),
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

// Several files of one run (the staged parts) read as one: their records in
// the order given, and the first file's stamps, which every other must share.
async function readRecordFiles(files) {
    let full = null;
    for (const file of files) {
        const one = await readFullRecords(file);
        if (full === null) { full = one; continue; }
        if (one.extracted_at !== full.extracted_at || one.pipeline_commit !== full.pipeline_commit) {
            throw new InputError(`${file} is from another run (${one.extracted_at}, ${one.pipeline_commit}) `
                + `than ${files[0]} (${full.extracted_at}, ${full.pipeline_commit})`);
        }
        for (const r of one.records) full.records.push(r);
    }
    return full;
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

// answerTrue: the stubs that return true; every other returns nothing (an
// async one returns a promise of its answer).
function siteRuntime(site, answerTrue) {
    const { document, byId, controls } = buildDocument(site.markup);
    const charts = [];
    let sgOn = false;
    const sandbox = {
        document, window: {}, console: { log() {}, info() {}, debug() {}, warn() {}, error() {} },
        requestAnimationFrame: (fn) => { fn(); return 1; }, cancelAnimationFrame() {},
        Chart: class { constructor(canvas, config) { charts.push({ canvas, config }); } destroy() {} },
        sgActive: () => sgOn,
    };
    // Every stub, the fixed ones and the site's other functions alike: inert
    // unless named in answerTrue, and each call is recorded. A plain
    // function, so `new NAME()` works too; an async one returns a promise.
    const stubbedRan = new Set();
    const stub = (name, isAsync) => function () {
        stubbedRan.add(name);
        const answer = answerTrue.has(name) ? true : undefined;
        return isAsync ? Promise.resolve(answer) : answer;
    };
    for (const name of ELSEWHERE) sandbox[name] = stub(name, false);
    for (const { name, async: isAsync } of site.stubs) sandbox[name] = stub(name, isAsync);
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
        stubbedRan: () => [...stubbedRan].sort(),
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
    // filesOnly: the check compares the files alone and runs none of the
    // site's code, so a stub can never be why it fails.
    const filesOnly = new Set();
    const ok = (name, pass, detail, files = false) => {
        if (files) filesOnly.add(name);
        checks.push({ check: name, ok: !!pass, ...(pass ? {} : { detail }) });
        return pass;
    };
    const summary = JSON.parse(readFileSync(summaryFile, 'utf8'));
    const block = summary.firstView;
    if (!block || typeof block !== 'object') throw new InputError(`${summaryFile} has no firstView block`);
    const shape = shapeProblems(block);
    ok('the block is well formed', !shape.length, shape, true);
    if (shape.length) return { checks, filesOnly };

    const { records } = full;
    ok('stamps: the records, the block and the summary are one run',
       full.extracted_at === block.extracted_at && block.extracted_at === summary.extracted_at
       && full.pipeline_commit === block.pipeline_commit && block.pipeline_commit === summary.pipeline_commit,
       { records: [full.extracted_at, full.pipeline_commit], block: [block.extracted_at, block.pipeline_commit],
         summary: [summary.extracted_at, summary.pipeline_commit] }, true);
    ok('the summary counted the same records', summary.totalStudies === records.length,
       { summary: summary.totalStudies, records: records.length }, true);

    const site = readSite(appPath, indexPath);
    const inert = siteChecks(site, block, records, new Set());
    if (inert.error) {
        // The site code threw after one of app.js's functions ran as a stub:
        // what the stub returned may be what it threw on. With only the fixed
        // stubs run, it is the site code's own error, as before.
        const found = inert.stubbed.filter((n) => !PREDECLARED.has(n));
        if (!found.length) throw inert.error;
        throw new InputError(`could not run faithfully: ${callsNote(found)}, which the parity does not run, `
            + `and the site code then threw: ${inert.error && inert.error.message}`);
    }
    for (const c of inert.checks) checks.push(c);
    for (const name of inert.filesOnly) filesOnly.add(name);
    let ran = inert.stubbed, answerMatters = null, answerMattersFor = [], answerRead = [], thrown = null;
    if (ran.length) {
        // A stub ran: does the Overview use what it returns? Every stub,
        // fixed or found, answers true in the second run.
        const everyStub = new Set([...ELSEWHERE, ...site.stubs.map((s) => s.name)]);
        const truthy = siteChecks(site, block, records, everyStub);
        ran = [...new Set([...ran, ...truthy.stubbed])].sort();
        const differs = (run) => (run.error ? 'the site code threw' : firstDifference(inert.seen, run.seen));
        // Which: halve a set of stubs that differs, flipping each half on
        // its own, down to the single stubs whose answer changes the
        // Overview (a few runs, not one per stub: the default view runs two
        // dozen fixed ones). A set that differs only as a whole is named
        // whole.
        const blame = (names, known) => {
            if (!known && differs(siteChecks(site, block, records, new Set(names))) === null) return [];
            if (names.length === 1) return names;
            const mid = names.length >> 1;
            const found = [...blame(names.slice(0, mid), false), ...blame(names.slice(mid), false)];
            return found.length ? found : names;
        };
        answerMatters = differs(truthy);
        answerRead = answerReadFrom(ran, site.pieces.join('\n'));
        if (answerMatters !== null) {
            // app.js's own stubs go first, so a half holds one kind where it can.
            const order = [...ran.filter((n) => !PREDECLARED.has(n)), ...ran.filter((n) => PREDECLARED.has(n))];
            answerMattersFor = blame(order, true).sort();
            thrown = truthy.error;
        } else {
            // Both runs the same, but answers can cancel out: a rule
            // `if (A() && !B(study))` excludes nothing when both answer
            // nothing and nothing when both answer true. The stubs whose
            // answer the Overview may read are flipped in combination too,
            // the rest answering nothing; every other stub's answer is
            // thrown away, so the site's code as it stands costs no run.
            for (const set of flipSets(answerRead)) {
                const run = siteChecks(site, block, records, new Set(set));
                answerMatters = differs(run);
                if (answerMatters === null) continue;
                answerMattersFor = blame(set, true).sort();
                thrown = run.error;
                break;
            }
        }
    }
    if (thrown) {
        const one = answerMattersFor.length === 1;
        throw new InputError(`could not run faithfully: ${stubNote(answerMattersFor)}, and with ${one ? 'it' : 'them'} `
            + `stubbed to return true the site code threw: ${thrown && thrown.message}`);
    }
    return {
        checks, filesOnly, site: inert.result, site_code: site.source,
        stubbed: ran.filter((n) => !PREDECLARED.has(n)), predeclared: ran.filter((n) => PREDECLARED.has(n)),
        answerMatters, answerMattersFor, answerRead,
    };
}

// One run of the site's code over the records and the block, with the stubs
// in answerTrue returning true and every other nothing. seen is everything
// the run's checks saw, passed or not, to compare one run with another;
// error, what the site code threw (an input error is thrown on).
function siteChecks(site, block, records, answerTrue) {
    const checks = [], filesOnly = new Set(), seen = [];
    const ok = (name, pass, detail, files = false) => {
        if (files) filesOnly.add(name);
        seen.push([name, JSON.stringify([!!pass, detail ?? null])]);
        checks.push({ check: name, ok: !!pass, ...(pass ? {} : { detail }) });
        return pass;
    };
    const s = siteRuntime(site, answerTrue);
    s.context.__records = records;
    try {
        runSite(s, block, records, ok);
    } catch (err) {
        if (err instanceof InputError) throw err;
        return { checks, filesOnly, seen, result: null, stubbed: s.stubbedRan(), error: err };
    }
    seen.push(['the site\'s counts and painted text', JSON.stringify(s.result)]);
    return { checks, filesOnly, seen, result: s.result, stubbed: s.stubbedRan(), error: null };
}

// The first check two runs saw differently, or null when they saw the same.
function firstDifference(a, b) {
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
        if (!a[i] || !b[i]) return 'which checks ran';
        if (a[i][0] !== b[i][0]) return 'which checks ran';
        if (a[i][1] !== b[i][1]) return a[i][0];
    }
    return null;
}

const callsNote = (names) => `the Overview now calls ${names.join(', ')}`;

// Stubs the check blames, by kind: app.js's functions the parity does not
// run, and the fixed stubs, which the Overview always called.
function stubNote(names) {
    const found = names.filter((n) => !PREDECLARED.has(n)), fixed = names.filter((n) => PREDECLARED.has(n));
    const parts = [];
    if (found.length) parts.push(`${callsNote(found)}, which the parity does not run`);
    if (fixed.length) {
        parts.push(`the Overview uses what ${fixed.join(', ')} return${fixed.length === 1 ? 's' : ''}, `
            + 'which the parity stubs to return nothing (ELSEWHERE)');
    }
    return parts.join('; ');
}

// The site's code over the records and the block: every check below runs it.
function runSite(s, block, records, ok) {

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
       { trials: block.trials, not_counted: nc, records: records.length }, true);
    ok('absence: not interventional, by the site\'s filter with study type All',
       nc.not_interventional === allTypes - block.trials, { block: nc.not_interventional, site: allTypes - block.trials });
    ok('absence: no results date', nc.no_results_date === noDate, { block: nc.no_results_date, records: noDate }, true);
    ok('absence: a results date that is not a year from 2009 on', notFrom === records.length - allTypes - noDate,
       { block: notFrom, site: records.length - allTypes - noDate });

    s.result = { ...fromRecords, newest_results_year: newest, by_results_year: siteYears,
                 painted: Object.fromEntries(Object.entries(painted).map(([id, v]) => [id, v.html || v.text])) };
}

// ── command line ───────────────────────────────────────────────────────────

function parseArgs(argv) {
    const out = { excerpt: false, records: [] };
    for (let i = 0; i < argv.length; i++) {
        const a = argv[i];
        if (a === '--excerpt') { out.excerpt = true; continue; }
        const m = /^--(records|summary|app|index|source|out-dir)$/.exec(a);
        if (!m || i + 1 >= argv.length) throw new InputError(`unknown or incomplete argument ${a}`);
        if (m[1] === 'records') out.records.push(argv[++i]);
        else out[m[1]] = argv[++i];
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
    if (!a.records.length || !a.summary) throw new InputError('needs --records and --summary');
    const appPath = a.app || path.join(EXCERPT_DIR, EXCERPT_APP);
    const indexPath = a.index || path.join(EXCERPT_DIR, EXCERPT_INDEX);
    const t0 = Date.now();
    const full = await readRecordFiles(a.records);
    const t1 = Date.now();
    const result = check(a.summary, appPath, indexPath, full);
    const failed = result.checks.filter((c) => !c.ok);
    const stubbed = result.stubbed ?? [];
    // A difference is not a data mismatch unless a check of the files alone
    // fails too, when it may be a stub's: the numbers differing with one of
    // app.js's functions run as a stub, or the Overview changing when the
    // stubs that ran (fixed ones too) return true instead of nothing. The
    // fixed stubs' nothing is the site's own answer, so with those alone run
    // and the second run the same, differing numbers are a mismatch.
    const answerMatters = result.answerMatters ?? null;
    const blamed = [...new Set([...(result.answerMattersFor ?? []), ...(failed.length ? stubbed : [])])].sort();
    const unfaithful = blamed.length > 0 && failed.every((c) => !result.filesOnly.has(c.check));
    const code = unfaithful ? 2 : failed.length ? 1 : 0;
    const one = blamed.length === 1;
    const fix = `make the parity run ${one ? 'it' : 'them'} (APP_PIECES) or stub ${one ? 'it' : 'them'} `
        + 'with what the Overview needs (ELSEWHERE)';
    // The stubs whose answers are read that still answered nothing in the
    // run that differed (empty when every one was flipped).
    const still = (result.answerRead ?? []).filter((n) => !(result.answerMattersFor ?? []).includes(n));
    const changes = `the Overview changes when ${one ? 'it returns' : 'they return'} true instead`
        + (still.length ? ` and ${still.join(', ')} still return${still.length === 1 ? 's' : ''} nothing` : '')
        + ` (first difference: ${answerMatters})`;
    const report = {
        ok: code === 0,
        could_not_run: !unfaithful ? null
            : failed.length ? `${stubNote(blamed)}: with ${one ? 'it' : 'them'} stubbed inert, `
                + `${failed.length} of ${result.checks.length} checks differ`
                + (answerMatters !== null ? `, and ${changes}` : '')
                + `, so whether the block or the stub is wrong is unknown; ${fix}`
            : `${stubNote(blamed)}: the numbers agree with ${one ? 'it' : 'them'} stubbed to return nothing, `
                + `but ${changes}, so what ${one ? 'it returns' : 'they return'} decides `
                + `what the page shows and whether the block is right is unknown; ${fix}`,
        site_functions_stubbed: stubbed,
        predeclared_stubs_run: result.predeclared ?? [],
        answer_matters_for: result.answerMattersFor ?? [],
        answer_read_from: result.answerRead ?? [],
        records: { file: a.records.length === 1 ? a.records[0] : a.records, count: full.records.length, extracted_at: full.extracted_at, pipeline_commit: full.pipeline_commit },
        site_code: result.site_code ?? null,
        checked: result.checks.length,
        mismatches: failed,
        site: result.site ?? null,
        seconds: { read: (t1 - t0) / 1000, check: (Date.now() - t1) / 1000 },
    };
    console.log(JSON.stringify(report, null, 2));
    if (unfaithful) console.error(`first_view_parity: could not run faithfully: ${report.could_not_run}`);
    else if (stubbed.length) {
        // A pass with app.js's own functions stubbed is noted; the fixed
        // stubs run every time and are listed in the report only.
        const single = stubbed.length === 1;
        console.error(`first_view_parity: note: ${callsNote(stubbed)}, which the parity does not run; `
            + `${single ? 'it ran as an inert stub' : 'they ran as inert stubs'}`
            + (failed.length ? '.' : `, and the numbers agree whether ${single ? 'it returns' : 'they return'} nothing or true.`));
    }
    return code;
}

// Always run, unless an importer (the tests, for autoStubs) set this flag
// before importing: however the script is invoked, it never silently skips
// its check.
if (!globalThis.FIRST_VIEW_PARITY_NO_MAIN) {
    main().then((code) => { process.exitCode = code; }, (err) => {
        console.error(err instanceof InputError ? `first_view_parity: ${err.message}` : err);
        process.exitCode = 2;
    });
}
