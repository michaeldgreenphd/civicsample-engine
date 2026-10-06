// Copied unchanged from michaeldgreenphd/clinical-trial-populations@c4e297ab45d61f2872eff5140cc9b91ddd0605d6:
// the pieces of the site's app.js that its Overview runs as it opens, in app.js order.
// Written by scripts/first_view_parity.mjs --excerpt; rewrite it that way when the site changes them.
// Nothing in this repository serves or runs it except that script, which slices it by name
// exactly as it slices the site's own app.js.
const YEAR_WINDOW_MIN = 2009;

// The latest results year in the dataset on screen: the summary's byYear
// keys for a summary (phones, aggregate archives), else the study records.
function datasetLatestYear() {
    let max = 0;
    if (dashboardSummary && dashboardSummary.byYear) {
        for (const y of Object.keys(dashboardSummary.byYear)) max = Math.max(max, parseInt(y, 10) || 0);
    } else if (Array.isArray(data)) {
        for (const s of data) max = Math.max(max, parseInt(s.results_date?.substring(0, 4), 10) || 0);
    }
    return max >= YEAR_WINDOW_MIN ? max : null;
}

// The fill bar, the two tooltips and their labels, from the inputs' values.
function paintYearSlider() {
    const ys = document.getElementById('year-start');
    const ye = document.getElementById('year-end');
    if (!ys || !ye) return;
    const min = parseInt(ys.min, 10), max = parseInt(ys.max, 10);
    const range = Math.max(1, max - min);
    const startPct = ((parseInt(ys.value, 10) - min) / range) * 100;
    const endPct = ((parseInt(ye.value, 10) - min) / range) * 100;
    const fill = document.getElementById('year-range-fill');
    if (fill) { fill.style.left = startPct + '%'; fill.style.width = (endPct - startPct) + '%'; }
    const startTip = document.getElementById('year-start-tooltip');
    const endTip = document.getElementById('year-end-tooltip');
    if (startTip) startTip.style.left = startPct + '%';
    if (endTip) endTip.style.left = endPct + '%';
    const sl = document.getElementById('year-start-label');
    const el = document.getElementById('year-end-label');
    if (sl) sl.textContent = ys.value;
    if (el) el.textContent = ye.value;
}

// Record the request from a thumb the reader (or a shared link) moved.
function noteYearChoice(el) {
    if (!el || !el.dataset) return;
    el.dataset.chosen = (el.id === 'year-end' && el.value === el.max) ? '' : el.value;
}

// A shared link's year is the request even where this window clamps the
// thumb: a link carries a year only when the view it was copied from had
// that bound. An end before the start leaves the thumbs' own clamp standing.
function noteYearFromLink(el, raw) {
    const n = parseInt(raw, 10);
    if (!el || !el.dataset || !Number.isFinite(n)) return;
    if (el.id === 'year-end' && n < yearWindowRequest().start) return;
    el.dataset.chosen = String(n);
}

// The request as numbers: { start, end }, end null for "no upper limit".
// Until anything is recorded, the thumbs speak for themselves.
function yearWindowRequest() {
    const ys = document.getElementById('year-start');
    const ye = document.getElementById('year-end');
    const year = (s) => { const n = parseInt(s, 10); return Number.isFinite(n) ? n : null; };
    const start = year(ys?.dataset?.chosen ?? ys?.value) ?? YEAR_WINDOW_MIN;
    let end = null;
    if (ye) {
        const chosen = ye.dataset?.chosen;
        end = chosen !== undefined ? year(chosen) : (ye.value === ye.max ? null : year(ye.value));
    }
    return { start, end };
}

// Size the window to the dataset on screen and put the thumbs at the
// reader's request, clamped to it. The Years chip names that range, so it
// is redrawn here: no switch path can leave it naming the old one.
function syncYearWindow() {
    const ys = document.getElementById('year-start');
    const ye = document.getElementById('year-end');
    const latest = datasetLatestYear();
    if (!ys || !ye || !latest) return;
    const want = yearWindowRequest();   // before max moves: an unrecorded thumb reads against the old end
    ys.max = ye.max = String(latest);
    const end = want.end === null ? latest : Math.min(want.end, latest);
    ye.value = String(end);
    ys.value = String(Math.min(want.start, end));
    paintYearSlider();
    if (typeof updateActiveFilters === 'function') updateActiveFilters();
}

// Both thumbs to the window's ends, and the request with them (Reset and
// the Years chip's ×).
function resetYearWindow() {
    const ys = document.getElementById('year-start');
    const ye = document.getElementById('year-end');
    if (!ys || !ye) return;
    ys.value = ys.min;
    ye.value = ye.max;
    noteYearChoice(ys);
    noteYearChoice(ye);
    paintYearSlider();
}

// The bounds the filters apply: end is Infinity when the reader asked for
// no upper limit, else the end thumb (the request clamped to the window).
// Top-level on purpose: any view that filters by the Year Range reads it,
// including views drawn before the window is synced.
function yearWindowEnds() {
    const ys = document.getElementById('year-start');
    const ye = document.getElementById('year-end');
    const start = parseInt(ys?.value, 10) || YEAR_WINDOW_MIN;
    const { end } = yearWindowRequest();
    return { start, end: end === null || !ye ? Infinity : parseInt(ye.value, 10) };
}

function initFilters() {
    // Populate condition and country dropdowns
    populateConditionsDropdown();
    populateCountriesDropdown();

    const filterIds = [
        'year-start', 'year-end', 'study-type', 'phase', 'sponsor-class',
        'intervention-model', 'masking', 'primary-purpose',
        'enrollment-type', 'healthy-volunteers', 'population-age', 'condition', 'condition-primary', 'condition-secondary', 'country',
        'fda-status',
        // ?sg=v2 (parser v2) filters; hidden unless the beta is on
        'sg-status', 'sg-reported-sex', 'sg-reported-gender', 'sg-reported-both', 'sg-glb', 'sg-participant-count'
    ];
    filterIds.forEach(id => {
        const element = document.getElementById(id);
        if (element) {
            element.addEventListener('change', () => {
                // When primary changes, update secondary dropdown options
                if (id === 'condition-primary') {
                    populateSecondaryConditionDropdown(element.value);
                }
                renderDashboard();
                updateActiveFilters();
                updateShareUrl();   // the address bar is the share link; keep it current
            });
        }
    });

    // AI study checkbox
    const aiCheckbox = document.getElementById('ai-study-filter');
    if (aiCheckbox) {
        aiCheckbox.addEventListener('change', () => {
            renderDashboard();
            updateActiveFilters();
        });
    }

    // Participant range inputs (fire on every keystroke)
    ['min-participants', 'max-participants'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('input', () => {
                renderDashboard();
                updateActiveFilters();
            });
        }
    });

    // Year dual-range slider with floating tooltip bubbles
    const yearStartInput = document.getElementById('year-start');
    const yearEndInput = document.getElementById('year-end');

    if (yearStartInput) {
        yearStartInput.addEventListener('input', (e) => {
            if (parseInt(e.target.value) > parseInt(yearEndInput.value)) {
                e.target.value = yearEndInput.value;
            }
            noteYearChoice(e.target);
            paintYearSlider();
        });
    }

    if (yearEndInput) {
        yearEndInput.addEventListener('input', (e) => {
            if (parseInt(e.target.value) < parseInt(yearStartInput.value)) {
                e.target.value = yearStartInput.value;
            }
            noteYearChoice(e.target);
            paintYearSlider();
        });
    }

    // The window's upper end from the data, then the fill + tooltip positions
    syncYearWindow();

    // Reset filters button
    const resetBtn = document.getElementById('reset-filters');
    if (resetBtn) {
        resetBtn.addEventListener('click', resetFilters);
    }

    // Toggle expanded filters
    const toggleBtn = document.getElementById('toggle-more-filters');
    const expandedSection = document.getElementById('expanded-filters');
    if (toggleBtn && expandedSection) {
        toggleBtn.addEventListener('click', () => {
            const isHidden = expandedSection.style.display === 'none';
            expandedSection.style.display = isHidden ? '' : 'none';
            toggleBtn.innerHTML = isHidden ? 'Show Fewer Filters &#9650;' : 'Show More Filters &#9660;';
        });
    }
}

function getFilteredData() {
    if (!data) return [];

    // Mobile summary mode: `data` is the pre-trimmed recent-studies list and
    // the filter controls are disabled. The compact records omit fields the
    // filters test (e.g. study_type), so running the filter chain below would
    // wrongly reject every row (the default Study Type of INTERVENTIONAL
    // matches nothing when study_type is undefined). Return the list as-is;
    // the table's own search box is applied separately in renderStudiesTable.
    if (dashboardSummary) return [...data];

    const { start: yearStart, end: yearEnd } = yearWindowEnds();
    const studyType = document.getElementById('study-type')?.value || 'all';
    const phase = document.getElementById('phase')?.value || 'all';
    const sponsorClass = document.getElementById('sponsor-class')?.value || 'all';
    const interventionModel = document.getElementById('intervention-model')?.value || 'all';
    const masking = document.getElementById('masking')?.value || 'all';
    const primaryPurpose = document.getElementById('primary-purpose')?.value || 'all';
    const enrollmentType = document.getElementById('enrollment-type')?.value || 'all';
    const healthyVolunteers = document.getElementById('healthy-volunteers')?.value || 'all';
    const populationAge = document.getElementById('population-age')?.value || 'all';
    const conditionFilter = document.getElementById('condition')?.value || 'all';
    const conditionPrimaryFilter = document.getElementById('condition-primary')?.value || 'all';
    const conditionSecondaryFilter = document.getElementById('condition-secondary')?.value || 'all';
    const countryFilter = document.getElementById('country')?.value || 'all';
    const aiOnly = document.getElementById('ai-study-filter')?.checked || false;
    const fdaStatus = document.getElementById('fda-status')?.value || 'all';
    sgV2Filters = sgReadFilters();

    return data.filter(study => {
        if (aiOnly && !isAIStudy(study)) return false;
        const year = parseInt(study.results_date?.substring(0, 4));
        if (isNaN(year) || year < yearStart || year > yearEnd) return false;
        if (studyType !== 'all' && study.study_type !== studyType) return false;
        if (phase !== 'all') {
            if (phase === 'NA') {
                // Match "NA" or empty/missing phase
                if (study.phase && study.phase !== 'NA') return false;
            } else {
                // Check if the selected phase is included in the study's phase(s)
                // Handles both single phases (e.g., "PHASE1") and combined (e.g., "PHASE1, PHASE2")
                const studyPhases = study.phase?.split(',').map(p => p.trim()) || [];
                if (!studyPhases.includes(phase)) return false;
            }
        }
        if (sponsorClass !== 'all' && study.sponsor_class !== sponsorClass) return false;
        if (interventionModel !== 'all' && study.intervention_model !== interventionModel) return false;
        if (masking !== 'all' && study.masking !== masking) return false;
        if (primaryPurpose !== 'all' && study.primary_purpose !== primaryPurpose) return false;
        if (enrollmentType !== 'all' && study.enrollment_type !== enrollmentType) return false;
        if (healthyVolunteers !== 'all') {
            const acceptsHealthy = study.healthy_volunteers === true;
            if (healthyVolunteers === 'true' && !acceptsHealthy) return false;
            if (healthyVolunteers === 'false' && acceptsHealthy) return false;
        }
        if (populationAge !== 'all') {
            if (getStudyPediatricStatus(study) !== populationAge) return false;
        }
        if (conditionFilter !== 'all') {
            const conditions = study.conditions || [];
            if (!conditions.includes(conditionFilter)) return false;
        }
        // Hierarchical condition category filter
        if (conditionPrimaryFilter !== 'all' || conditionSecondaryFilter !== 'all') {
            if (!studyMatchesConditionFilter(study, conditionPrimaryFilter, conditionSecondaryFilter)) return false;
        }
        if (countryFilter !== 'all') {
            const countries = study.countries || [];
            const countryNames = countries.map(c => c.country);
            if (!countryNames.includes(countryFilter)) return false;
        }

        // Participant count range — null/undefined enrollment is excluded
        // whenever either bound is active
        const minPart = document.getElementById('min-participants')?.value;
        const maxPart = document.getElementById('max-participants')?.value;
        if (minPart !== '' || maxPart !== '') {
            const enroll = study.enrollment;
            if (enroll == null) return false;
            if (minPart !== '' && enroll < parseInt(minPart, 10)) return false;
            if (maxPart !== '' && enroll > parseInt(maxPart, 10)) return false;
        }

        // FDA regulatory status filter. A preserved null means the sponsor
        // never reported oversight status — distinct from an explicit "No"
        // (extractions before mid-2026 coerced nulls to false, so the
        // Unreported option only matches data extracted after that fix).
        if (fdaStatus !== 'all') {
            const dr = study.is_fda_regulated_drug, dv = study.is_fda_regulated_device;
            if (fdaStatus === 'drug' && dr !== true) return false;
            if (fdaStatus === 'device' && dv !== true) return false;
            if (fdaStatus === 'unapproved' && study.is_unapproved_device !== true) return false;
            if (fdaStatus === 'unreported' && !(dr == null && dv == null)) return false;
            if (fdaStatus === 'non-regulated' && (dr === true || dv === true ||
                study.is_unapproved_device === true || (dr == null && dv == null))) return false;
        }

        // ?sg=v2: the parser-v2 filters compose with everything above. They
        // read the lean row (and the CSV join for gender_labeled_binary_only).
        if (sgV2Filters) {
            const r = sgRow(study);
            if (sgV2Filters.status !== 'all' && (!r || r.sex_report_status !== sgV2Filters.status)) return false;
            for (const [field, want] of sgV2Filters.bools) {
                const have = r ? r[field] : null;
                if (have !== want) return false;
            }
        }

        return true;
    });
}

function sgReadFilters() {
    if (typeof sgActive !== 'function' || !sgActive()) return null;
    const statusEl = document.getElementById('sg-status');
    const status = (statusEl && !statusEl.disabled && statusEl.value) || 'all';
    const bools = [];
    for (const [id, field] of [['sg-reported-sex', 'reported_sex'], ['sg-reported-gender', 'reported_gender'],
                               ['sg-reported-both', 'reported_both'], ['sg-glb', 'gender_labeled_binary_only'],
                               ['sg-participant-count', 'is_participant_count']]) {
        const el = document.getElementById(id);
        if (el && !el.disabled && (el.value === 'true' || el.value === 'false')) bools.push([field, el.value === 'true']);
    }
    if (status === 'all' && !bools.length) return null;
    return { status, bools };
}

function showDashboardSpinner() {
    const el = document.getElementById('dashboard-loading');
    if (el) el.style.display = 'flex';
}

function hideDashboardSpinner() {
    const el = document.getElementById('dashboard-loading');
    if (el) el.style.display = 'none';
}

function renderOverviewTileContext(total, raceCount, ethCount, bothCount) {
    const set = (id, text) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    };
    const y0 = document.getElementById('year-start');
    const y1 = document.getElementById('year-end');
    set('stat-sub-total', y0 && y1 && !dashboardSummary
        ? `results posted ${y0.value}\u2013${y1.value}`
        : 'trials with results posted');
    const n = (c) => `${c.toLocaleString()} of ${total.toLocaleString()} trials`;
    set('stat-sub-race', total ? n(raceCount) : '\u2014');
    set('stat-sub-ethnicity', total ? n(ethCount) : '\u2014');
    set('stat-sub-both', total ? n(bothCount) : '\u2014');
}

function renderOverviewFinding(total, raceCount, ethCount, bothCount) {
    const box = document.getElementById('overview-finding');
    const head = document.getElementById('finding-headline');
    const ctx = document.getElementById('finding-context');
    if (!box || !head || !ctx) return;

    if (!total) {
        box.hidden = false;
        head.textContent = 'No trials match these filters.';
        ctx.textContent = 'Widen the year range or clear a filter to see a result.';
        return;
    }

    // Below this many trials a percentage in a headline reads as a finding
    // when it is really two or three studies. The geography tab withholds an
    // estimand under its support floor rather than printing a fragile number;
    // this is the same discipline on a filtered cohort — the counts are
    // reported instead, and nothing is asserted.
    const HEADLINE_MIN_TRIALS = 100;

    if (total < HEADLINE_MIN_TRIALS) {
        const verb = (n) => (n === 1 ? 'reports' : 'report');
        head.innerHTML = `<strong>${raceCount.toLocaleString()}</strong> of ` +
            `${total.toLocaleString()} trials ${verb(raceCount)} race; ` +
            `<strong>${bothCount.toLocaleString()}</strong> ${verb(bothCount)} ` +
            'race and ethnicity together.';
        ctx.textContent = `Too few trials to state a rate — under ${HEADLINE_MIN_TRIALS}, ` +
            'counts are reported instead. Widen the filters for a percentage.';
        box.hidden = false;
        return;
    }

    const pct = (n) => (n / total) * 100;
    const race = pct(raceCount), eth = pct(ethCount), both = pct(bothCount);
    const gap = race - both;

    head.innerHTML = `<strong>${race.toFixed(1)}%</strong> of these trials report race, ` +
        `but only <strong>${both.toFixed(1)}%</strong> report race and ethnicity together.`;
    ctx.textContent = `${total.toLocaleString()} trials · ` +
        `${gap.toFixed(1)}-point gap · ethnicity reported by ${eth.toFixed(1)}%`;
    box.hidden = false;
}

function renderFilterSummary(total, unfiltered) {
    const el = document.getElementById('filter-summary-text');
    if (!el) return;

    // The phone view renders pre-computed aggregates of the whole dataset and
    // applies no filters, so reading the (desktop) controls here would claim
    // a narrowing that was never applied.
    if (unfiltered) {
        el.innerHTML = `<b>${escapeHtml(total.toLocaleString())}</b> trials \u00b7 ` +
            'the full dataset, unfiltered \u00b7 filters are a desktop feature';
        return;
    }
    const val = (id) => {
        const n = document.getElementById(id);
        if (!n) return null;
        const v = n.tagName === 'SELECT' ? (n.options[n.selectedIndex] || {}).text : n.value;
        return v == null ? null : String(v).trim();
    };
    const isAll = (v) => !v || /^all\b/i.test(v);
    const bold = (v) => `<b>${escapeHtml(v)}</b>`;

    const parts = [];
    if (typeof total === 'number') parts.push(`${bold(total.toLocaleString())} trials`);

    const type = val('study-type');
    if (!isAll(type) && type) parts.push(bold(type.toLowerCase()));

    const y0 = val('year-start'), y1 = val('year-end');
    if (y0 && y1) parts.push(`results posted ${bold(y0 + '\u2013' + y1)}`);

    // Only narrowed dimensions earn a phrase; "all sponsors, all purposes,
    // all conditions, all statuses" is four phrases that say nothing.
    const narrowed = [
        ['sponsor', 'sponsor'], ['purpose', 'purpose'],
        ['condition-primary', 'condition'], ['condition-secondary', 'subcategory'],
        ['fda-status', 'FDA status']
    ].filter(([id]) => !isAll(val(id)))
     .map(([id, noun]) => `${noun} ${bold(val(id))}`);

    if (narrowed.length) parts.push(...narrowed);
    else parts.push('all sponsors, purposes and conditions');

    el.innerHTML = parts.join(' \u00b7 ');
}

function renderDashboard() {
    if (!data && !dashboardSummary) return;

    showDashboardSpinner();

    // ── Mobile summary path: use pre-computed aggregates ──
    if (dashboardSummary) {
        const s = dashboardSummary;
        const t = s.totalStudies;
        document.getElementById('total-studies').textContent = t.toLocaleString();
        document.getElementById('race-reporting').textContent =
            t > 0 ? `${((s.cards.raceCount / t) * 100).toFixed(1)}%` : '0%';
        document.getElementById('ethnicity-reporting').textContent =
            t > 0 ? `${((s.cards.ethCount / t) * 100).toFixed(1)}%` : '0%';
        document.getElementById('both-reporting').textContent =
            t > 0 ? `${((s.cards.bothCount / t) * 100).toFixed(1)}%` : '0%';
        renderOverviewFinding(t, s.cards.raceCount, s.cards.ethCount, s.cards.bothCount);
        renderOverviewTileContext(t, s.cards.raceCount, s.cards.ethCount, s.cards.bothCount);
        renderFilterSummary(t, true);

        // All chart functions check dashboardSummary internally
        const stub = [];
        renderReportingTrends(stub);
        renderRaceDistribution(stub);
        renderRaceTrends(stub);
        renderRaceSubcategories('asian');
        renderRaceReportedParticipants(stub);
        renderRaceFullDistribution(stub);
        renderEthnicityDistribution(stub);
        renderEthnicityTrends(stub);
        renderEthnicitySubcategories(stub);
        renderEthnicityReportedParticipants(stub);
        renderEthnicityFullDistribution(stub);
        // ?sg=v2 replaces both of these tabs, so building the legacy charts
        // here would be eight Chart.js instances constructed only to be hidden.
        if (!sgActive()) {
            renderSexReportedParticipants(stub);
            renderSexFullDistribution(stub);
            renderSexDistribution(stub);
            renderSexTrends(stub);
            renderGenderReportedParticipants(stub);
            renderGenderFullDistribution(stub);
            renderGenderDistribution(stub);
            renderGenderTrends(stub);
        }
        sgAfterRender(stub);

        // A dataset switch with the FDA tab open: its tiles and chart follow
        // the summary on screen, rather than keep the previous dataset's.
        if (document.querySelector('.tab.active')?.dataset.tab === 'fda-oversight') renderFdaOversight(stub);

        // A dataset switch with the Studies tab open: the new dataset's rows
        // and its status row replace the old ones.
        refreshStudiesTab();

        requestAnimationFrame(() => hideDashboardSpinner());
        return;
    }

    // ── Desktop path: full per-study aggregation ──
    // ?sg=v2 first: the mode decides which v2 controls are live, and
    // getFilteredData() skips a disabled one. Leaving a pre-v2 or aggregate
    // snapshot with a v2 filter set, the controls were still disabled from the
    // archive when the data was filtered and were re-enabled afterwards, so
    // the charts came back unfiltered while the chips claimed otherwise. It
    // also unhides the v2 blocks before any chart is built into them.
    sgApplyMode();
    const filtered = getFilteredData();

    // Update stats
    document.getElementById('total-studies').textContent = filtered.length.toLocaleString();

    const raceCount = filtered.filter(s => s.race?.reported).length;
    const ethCount = filtered.filter(s => s.ethnicity?.reported).length;
    const bothCount = filtered.filter(s => s.race?.reported && s.ethnicity?.reported).length;

    document.getElementById('race-reporting').textContent =
        filtered.length > 0 ? `${((raceCount / filtered.length) * 100).toFixed(1)}%` : '0%';
    document.getElementById('ethnicity-reporting').textContent =
        filtered.length > 0 ? `${((ethCount / filtered.length) * 100).toFixed(1)}%` : '0%';
    document.getElementById('both-reporting').textContent =
        filtered.length > 0 ? `${((bothCount / filtered.length) * 100).toFixed(1)}%` : '0%';
    renderOverviewFinding(filtered.length, raceCount, ethCount, bothCount);
    renderOverviewTileContext(filtered.length, raceCount, ethCount, bothCount);
    renderFilterSummary(filtered.length);

    // Render only the Overview tab chart immediately (the visible tab).
    // All other tab charts are rendered on-demand when their tab is clicked
    // (the tab-click handler already calls the appropriate render functions).
    renderReportingTrends(filtered);

    // Determine which tab is currently active and render its charts
    const activeTab = document.querySelector('.tab.active')?.dataset.tab;
    if (activeTab === 'race') {
        renderRaceDistribution(filtered);
        renderRaceTrends(filtered);
        renderRaceSubcategories('asian');
        renderRaceReportedParticipants(filtered);
        renderRaceFullDistribution(filtered);
    } else if (activeTab === 'ethnicity') {
        renderEthnicityDistribution(filtered);
        renderEthnicityTrends(filtered);
        renderEthnicitySubcategories(filtered);
        renderEthnicityReportedParticipants(filtered);
        renderEthnicityFullDistribution(filtered);
    } else if (activeTab === 'sex') {
        // Skipped, not hidden, while parser v2 owns the tab: these four would
        // be rebuilt on every filter render over the full dataset.
        if (!sgActive()) {
            renderSexReportedParticipants(filtered);
            renderSexFullDistribution(filtered);
            renderSexDistribution(filtered);
            renderSexTrends(filtered);
        }
    } else if (activeTab === 'gender') {
        if (!sgActive()) {
            renderGenderReportedParticipants(filtered);
            renderGenderFullDistribution(filtered);
            renderGenderDistribution(filtered);
            renderGenderTrends(filtered);
        }
    } else if (activeTab === 'geography') {
        renderGeographyDashboard();
    } else if (activeTab === 'fda-oversight') {
        renderFdaOversight(filtered);
    }
    // ?sg=v2: swaps the Sex/Gender tabs to the parser-v2 blocks and renders
    // whichever of them is active (with zeros on a zero-result filter).
    sgAfterRender(filtered);

    // Update table if visible
    refreshStudiesTab();

    // Use requestAnimationFrame to hide spinner after paint
    requestAnimationFrame(() => hideDashboardSpinner());
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function renderReportingTrends(filtered) {
    const ctx = document.getElementById('reporting-trends-chart');
    if (!ctx) return;

    let byYear;
    if (dashboardSummary) {
        byYear = {};
        for (const [yr, v] of Object.entries(dashboardSummary.byYear)) {
            byYear[yr] = { total: v.total, race: v.race_reported, ethnicity: v.eth_reported, both: v.both_reported };
        }
    } else {
        byYear = {};
        filtered.forEach(study => {
            const year = study.results_date?.substring(0, 4);
            if (!year) return;
            if (!byYear[year]) byYear[year] = { total: 0, race: 0, ethnicity: 0, both: 0 };
            byYear[year].total++;
            if (study.race?.reported) byYear[year].race++;
            if (study.ethnicity?.reported) byYear[year].ethnicity++;
            if (study.race?.reported && study.ethnicity?.reported) byYear[year].both++;
        });
    }

    const years = Object.keys(byYear).sort();

    if (charts.reportingTrends) charts.reportingTrends.destroy();

    charts.reportingTrends = new Chart(ctx, {
        type: 'line',
        data: {
            labels: years,
            datasets: [
                {
                    label: 'Race',
                    data: years.map(y => (byYear[y].race / byYear[y].total) * 100),
                    borderColor: COLORS.reporting.race,
                    backgroundColor: COLORS.reporting.race + '1a',
                    tension: 0.3
                },
                {
                    label: 'Ethnicity',
                    data: years.map(y => (byYear[y].ethnicity / byYear[y].total) * 100),
                    borderColor: COLORS.reporting.ethnicity,
                    backgroundColor: COLORS.reporting.ethnicity + '1a',
                    tension: 0.3
                },
                {
                    label: 'Both',
                    data: years.map(y => (byYear[y].both / byYear[y].total) * 100),
                    borderColor: COLORS.reporting.both,
                    backgroundColor: COLORS.reporting.both + '1a',
                    tension: 0.3
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            aspectRatio: CHART_ASPECT_RATIO,
            scales: {
                y: {
                    beginAtZero: true,
                    max: 100,
                    title: { display: true, text: '% of Studies' }
                }
            },
            plugins: {
                legend: { position: 'top' }
            }
        }
    });
}
