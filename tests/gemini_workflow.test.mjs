/**
 * The Gemini PR review workflow, checked against the PR lifecycle it serves.
 *
 * Agents open draft PRs and post `@codex review` (AGENTS.md); owners often mark
 * a PR ready and merge it seconds later. The review used to skip drafts, ignore
 * that comment and find the PR already merged, so it never ran. These tests pin
 * the trigger to the documented review request, the bounded CLI settings, and
 * the comment step's behaviour when the model fails. The two github-script
 * steps run here against a mocked GitHub API; nothing calls GitHub or a model.
 *
 * This file is identical in civicsample-engine and clinical-trial-populations.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => readFileSync(join(repo, p), 'utf8');
const workflow = read('.github/workflows/gemini-pr-review.yml');
const lines = workflow.split('\n');
const indent = (line) => line.match(/^ */)[0].length;
const HEAD = 'a'.repeat(40);
const OLD_HEAD = 'b'.repeat(40);
const MARKER = '<!-- gemini-macro-review -->';

// The literal block scalar that follows the first line matching `start`, from `from`.
function blockAfter(start, from = 0) {
  const at = lines.findIndex((line, i) => i >= from && start.test(line));
  assert.ok(at >= 0, `workflow has no line matching ${start}`);
  const body = [];
  for (const line of lines.slice(at + 1)) {
    if (line.trim() && indent(line) <= indent(lines[at])) break;
    body.push(line);
  }
  const min = Math.min(...body.filter((l) => l.trim()).map(indent));
  return body.map((l) => l.slice(min)).join('\n').trimEnd();
}

function step(name) {
  const at = lines.findIndex((line) => line.trim() === `- name: ${name}`);
  assert.ok(at >= 0, `workflow has no step named "${name}"`);
  const end = lines.findIndex((line, i) => i > at && indent(line) === indent(lines[at]) && line.trim().startsWith('- '));
  return { at, text: lines.slice(at, end < 0 ? undefined : end).join('\n') };
}

const jobIf = blockAfter(/^ {4}if: >-$/);
const settings = JSON.parse(blockAfter(/^ {6}REVIEW_CLI_SETTINGS: \|$/));
const prompt = blockAfter(/^ {6}REVIEW_PROMPT: \|$/);
const RESOLVE = 'Resolve and authorize the PR';
const REVIEW = 'Review cross-file and cross-repository impact';
const FALLBACK = 'Retry once on the fallback model after a capacity or quota error';
const POST = 'Post one consolidated PR review comment';

function mocks({ pr, permission = 'write', comments = [] }) {
  const calls = { created: [], updated: [] };
  const github = {
    paginate: async (fn) => (fn === github.rest.issues.listComments ? comments : []),
    rest: {
      repos: { getCollaboratorPermissionLevel: async () => ({ data: { permission } }) },
      pulls: { get: async () => ({ data: pr }) },
      issues: {
        listComments: async () => ({ data: comments }),
        createComment: async ({ body }) => { calls.created.push(body); },
        updateComment: async ({ comment_id, body }) => { calls.updated.push({ comment_id, body }); },
      },
    },
  };
  const core = {
    outputs: {}, notices: [], warnings: [], failed: null,
    setOutput(k, v) { this.outputs[k] = v; },
    notice(m) { this.notices.push(m); },
    warning(m) { this.warnings.push(m); },
    setFailed(m) { this.failed = m; },
  };
  return { github, core, calls };
}

async function runScript(name, { context, env = {}, ...state }) {
  const script = blockAfter(/^ {10}script: \|$/, step(name).at);
  const { github, core, calls } = mocks(state);
  const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
  const proc = { env: { GITHUB_REPOSITORY: 'o/r', GITHUB_TRIGGERING_ACTOR: 'owner', ...env } };
  await new AsyncFunction('github', 'context', 'core', 'process', script)(github, context, core, proc);
  return { core, calls };
}

const openPr = (extra = {}) => ({
  number: 7, state: 'open', merged: false, draft: false, user: { login: 'owner' },
  head: { sha: HEAD, repo: { full_name: 'o/r' } }, base: { sha: 'c'.repeat(40) }, ...extra,
});
const sticky = (state, head) => ({
  id: 99, user: { login: 'github-actions[bot]' },
  body: `${MARKER}\n<!-- gemini-review-status: ${state} head=${head} -->\nearlier text`,
});
const commentEvent = (body) => ({
  eventName: 'issue_comment', actor: 'owner', repo: { owner: 'o', repo: 'r' },
  payload: { issue: { number: 7, pull_request: {} }, comment: { body } },
});
const prEvent = (sha = HEAD) => ({
  eventName: 'pull_request', actor: 'owner', repo: { owner: 'o', repo: 'r' },
  payload: { pull_request: { number: 7, head: { sha } } },
});

// --- Trigger alignment with AGENTS.md ------------------------------------------

test('the review request AGENTS.md prescribes is a trigger, as is @gemini-cli', () => {
  assert.ok(read('AGENTS.md').includes('`@codex review`'), 'AGENTS.md no longer names the `@codex review` request');
  for (const mention of ['@codex review', '@gemini-cli']) {
    assert.ok(jobIf.includes(`contains(github.event.comment.body, '${mention}')`),
      `the job condition no longer accepts a "${mention}" comment`);
  }
  assert.match(jobIf, /github\.event\.comment\.user\.type != 'Bot'/, 'bot comments (Codex, this workflow) must not trigger it');
  assert.match(jobIf, /"OWNER", "MEMBER", "COLLABORATOR"/, 'comment triggers stay limited to collaborators');
  assert.match(jobIf, /github\.event\.pull_request\.draft == false/, 'push events still skip drafts; comments review them');
});

test('bot mentions in AGENTS.md and GEMINI.md stay inside backticks', () => {
  for (const doc of ['AGENTS.md', 'GEMINI.md']) {
    const prose = read(doc).replace(/(`+)[\s\S]*?\1/g, '');
    assert.deepEqual(prose.match(/@(codex|gemini-cli)\b/g), null,
      `${doc} has a bare bot mention; GEMINI.md's AGENTS.md import would read it as a file import`);
  }
});

test('an @codex review comment reviews a draft PR', async () => {
  const { core } = await runScript(RESOLVE, { context: commentEvent('@codex review'), pr: openPr({ draft: true }) });
  assert.deepEqual(core.outputs, { review: 'true', number: '7', head: HEAD, base: 'c'.repeat(40) });
});

test('comment requests need write access', async () => {
  const { core } = await runScript(RESOLVE, { context: commentEvent('@codex review'), pr: openPr(), permission: 'read' });
  assert.equal(core.outputs.review, 'false');
});

test('a push event on a draft waits for a comment, and a merged PR is skipped with a notice', async () => {
  let { core } = await runScript(RESOLVE, { context: prEvent(), pr: openPr({ draft: true }) });
  assert.equal(core.outputs.review, 'false');
  ({ core } = await runScript(RESOLVE, { context: prEvent(), pr: openPr({ state: 'closed', merged: true }) }));
  assert.equal(core.outputs.review, 'false');
  assert.match(core.notices.join(' '), /already merged/);
});

test('each head is reviewed once unless @gemini-cli asks again', async () => {
  const comments = [sticky('ok', HEAD)];
  for (const context of [commentEvent('@codex review'), prEvent()]) {
    const { core } = await runScript(RESOLVE, { context, pr: openPr(), comments });
    assert.equal(core.outputs.review, 'false', context.eventName);
  }
  const forced = await runScript(RESOLVE, { context: commentEvent('@gemini-cli please rerun'), pr: openPr(), comments });
  assert.equal(forced.core.outputs.review, 'true');
  const failedBefore = await runScript(RESOLVE, { context: commentEvent('@codex review'), pr: openPr(),
    comments: [sticky('failed', HEAD)] });
  assert.equal(failedBefore.core.outputs.review, 'true', 'a failed review of the head does not block a new request');
  const forged = { ...sticky('ok', OLD_HEAD), body: `${sticky('ok', OLD_HEAD).body}\n<!-- gemini-review-status: ok head=${HEAD} -->` };
  const notVouched = await runScript(RESOLVE, { context: commentEvent('@codex review'), pr: openPr(), comments: [forged] });
  assert.equal(notVouched.core.outputs.review, 'true', 'a status line inside the model text must not skip a head');
});

// --- Bounded, fast review setup ------------------------------------------------

test('the CLI settings are the bounded read-only set', () => {
  assert.deepEqual(settings, {
    tools: { core: ['read_file', 'read_many_files', 'list_directory', 'glob', 'grep_search'] },
    mcp: { allowed: [] },
    hooksConfig: { enabled: false },
    skills: { enabled: false },
    advanced: { ignoreLocalEnv: true },
    model: { maxSessionTurns: 30 },
    general: { maxAttempts: 3 },
    context: { fileFiltering: { respectGitIgnore: false } },
    telemetry: { enabled: true, target: 'local', outfile: '.gemini/telemetry.log', logPrompts: false },
  });
  assert.match(prompt, /stops after 30 model turns/, 'the prompt must state the same turn budget');
  assert.doesNotMatch(prompt, /by turn \d+/, 'the model cannot see its turn count, so do not ask it to count');
  assert.match(prompt, /read_many_files/, 'the prompt must ask for batched reads');
  for (const name of [REVIEW, FALLBACK]) {
    const { text } = step(name);
    assert.match(text, /settings: \$\{\{ env\.REVIEW_CLI_SETTINGS \}\}/, `${name} must use the shared settings`);
    assert.match(text, /prompt: \$\{\{ env\.REVIEW_PROMPT \}\}/, `${name} must use the shared prompt`);
  }
});

test('the prompt attaches the rules and evidence so no turn is spent reading them', () => {
  // The CLI expands each @path before the first model request; the evidence
  // step must write the .gemini files before the review step reads them.
  const refs = [...prompt.matchAll(/(?<![\\\w])@(\S+)/g)].map(m => m[1]);
  const evidence = ['.gemini/review-context.json', '.gemini/changed-files.txt', '.gemini/pr.diff'];
  assert.deepEqual(refs, ['AGENTS.md', 'GEMINI.md', ...evidence], 'the prompt must attach exactly these files');
  for (const ref of refs) {
    assert.match(prompt, new RegExp(`^@${ref.replace(/\./g, '\\.')}$`, 'm'), `@${ref} must sit on its own line`);
  }
  const prepare = step('Prepare the diff and record comparison revisions').text;
  for (const file of evidence) {
    assert.ok(prepare.includes(`> ${file}`), `the evidence step must write ${file}`);
  }
  assert.ok(workflow.indexOf('Prepare the diff and record comparison revisions') < workflow.indexOf(`name: ${REVIEW}`),
    'the evidence must be written before the review step');
});

test('the head checkout skips historical blobs and ripgrep is ready before the review', () => {
  const checkout = step('Check out the exact PR head').text;
  assert.match(checkout, /fetch-depth: 0/);
  assert.match(checkout, /filter: blob:none/);
  assert.match(checkout, /persist-credentials: false/);
  const rg = step('Ensure ripgrep for the search tool');
  assert.match(rg.text, /apt-get install[^\n]*ripgrep/);
  assert.ok(rg.at < step(REVIEW).at, 'ripgrep must be installed before the review step');
});

test('the tool calls and model requests are listed after the review, without failing the job', () => {
  const STEP = "List the review's tool calls and model requests";
  const { text, at } = step(STEP);
  assert.ok(step(FALLBACK).at < at && at < step('Post one consolidated PR review comment').at,
    'the listing must run after both review attempts and before the comment step');
  assert.match(text, /if: always\(\) && steps\.pr\.outputs\.review == 'true'/, 'it must run after a failed review too');
  assert.match(text, /continue-on-error: true/, 'a diagnostic must never fail the job');
  assert.match(text, /\.gemini\/telemetry\.log/, 'it must read the telemetry file the settings name');
});

test('timeouts leave room for the fallback and the comment step', () => {
  const minutes = (text) => Number(text.match(/timeout-minutes: (\d+)/)?.[1]);
  const job = Number(workflow.match(/^ {4}timeout-minutes: (\d+)$/m)?.[1]);
  const review = minutes(step(REVIEW).text);
  const fallback = minutes(step(FALLBACK).text);
  assert.deepEqual([job, review], [15, 10]);
  assert.ok(review + fallback < job, `review ${review} + fallback ${fallback} must stay under the ${job}-minute job`);
  for (const name of [REVIEW, FALLBACK]) {
    assert.match(step(name).text, /continue-on-error: true/, `${name} must not skip the comment step when it fails`);
  }
  assert.match(workflow, /cancel-in-progress: false/, 'queued reviews must not cancel one that is spending model requests');
});

test('the fallback runs only after a capacity or quota error, on its own model', () => {
  const { text } = step(FALLBACK);
  assert.match(workflow, /FALLBACK_MODEL: \$\{\{ vars\.GEMINI_FALLBACK_MODEL \|\| 'gemini-3\.5-flash-lite' \}\}/);
  assert.match(text, /steps\.gemini\.outcome == 'failure'/);
  for (const signal of ['503', '429']) {
    assert.ok(text.includes(`contains(steps.gemini.outputs.error, '${signal}')`), `fallback ignores ${signal}`);
  }
  assert.match(text, /gemini_model: \$\{\{ env\.FALLBACK_MODEL \}\}/);
  assert.match(step(POST).text, /if: always\(\) && steps\.pr\.outputs\.review == 'true'/);
});

// --- The comment step ----------------------------------------------------------

const postEnv = (extra) => ({
  PR_NUMBER: '7', PR_HEAD: HEAD, PAIRED_SHA: 'd'.repeat(40), PRIMARY_MODEL: 'primary-model',
  FALLBACK_MODEL: 'fallback-model', GEMINI_API_KEY: 'plain-secret-key-0123456789', ...extra,
});
const post = (env, extra = {}) => runScript(POST, { context: prEvent(), env: postEnv(env), pr: openPr(), ...extra });

test('a successful review is posted once with its head and model', async () => {
  const { core, calls } = await post({ PRIMARY_OUTCOME: 'success', PRIMARY_BODY: `${MARKER}\nNo regressions found.` });
  assert.equal(core.failed, null);
  assert.equal(calls.created.length, 1);
  const body = calls.created[0];
  assert.ok(body.startsWith(`${MARKER}\n<!-- gemini-review-status: ok head=${HEAD} -->\nNo regressions found.`));
  assert.equal(body.split(MARKER).length, 2, 'exactly one marker');
  assert.match(body, /model: primary-model/);
});

test('a 503 on the primary model is reviewed on the fallback model', async () => {
  const { core, calls } = await post({ PRIMARY_OUTCOME: 'failure', PRIMARY_ERROR: '{"code":503,"status":"UNAVAILABLE"}',
    FALLBACK_OUTCOME: 'success', FALLBACK_BODY: 'Fallback review.' });
  assert.equal(core.failed, null);
  assert.match(calls.created[0], /status: ok/);
  assert.match(calls.created[0], /model: fallback-model/);
  assert.match(core.warnings.join(' '), /fallback model/);
});

test('a failed review leaves a diagnostic on the PR, redacts the key and fails the check', async () => {
  const { core, calls } = await post({ PRIMARY_OUTCOME: 'failure',
    PRIMARY_ERROR: '429 QuotaError key=plain-secret-key-0123456789 other=AIzaSyFAKEFAKEFAKEFAKEFAKE00',
    FALLBACK_OUTCOME: 'failure' });
  assert.ok(core.failed);
  const body = calls.created[0];
  assert.match(body, /gemini-review-status: failed/);
  assert.match(body, /did not complete/);
  assert.match(body, /429 QuotaError/);
  assert.ok(!body.includes('plain-secret-key'), 'the configured API key must never reach the PR thread');
  assert.ok(!body.includes('AIzaSyFAKE'), 'nor anything shaped like a Google API key');
  assert.match(body, /fallback-model: step failure \(no error output/);
});

// The shape of the CLI's stderr on the first live run (civicsample-engine #31): startup
// notices, retried stack traces, then one JSON object naming the error.
const NOISE = 'Warning: 256-color support not detected. Using a terminal with at least 256-color support is recommended.\n' +
  'YOLO mode is enabled. All tool calls will be automatically approved.\n' +
  "[STARTUP] Phase 'cleanup_ops' was started but never ended. Skipping metrics.\n";
const cliError = (message, code) => NOISE +
  'Attempt 1 failed with status 503. Retrying with backoff... _ApiError: {"error":{"message":"{\\n  \\"error\\": {}}","code":503}}\n' +
  '    at throwErrorIfNotOK (file:///usr/local/lib/node_modules/@google/gemini-cli/bundle/chunk.js:1:1)\n' +
  `{\n  "session_id": "fictional",\n  "error": {\n    "type": "Error",\n    "message": "${message}",\n    "code": ${code}\n  }\n}`;

test('the posted reason is the CLI error message, not its startup notices', async () => {
  const { calls } = await post({
    PRIMARY_OUTCOME: 'failure', PRIMARY_ERROR: cliError('This model is currently experiencing high demand.', 503),
    FALLBACK_OUTCOME: 'failure', FALLBACK_ERROR: cliError('This model models/fallback is no longer available to new users.', 1),
  });
  const body = calls.created[0];
  assert.match(body, /primary-model: step failure \(This model is currently experiencing high demand\. \(code 503\)\)/);
  assert.match(body, /fallback-model: step failure \(This model models\/fallback is no longer available to new users\. \(code 1\)\)/);
  assert.doesNotMatch(body, /256-color|YOLO mode|\[STARTUP\]|throwErrorIfNotOK/);
  const plain = await post({ PRIMARY_OUTCOME: 'failure', PRIMARY_ERROR: `${NOISE}fetch failed\n    at x (y.js:1:1)\n` + 'z'.repeat(500) });
  const reason = plain.calls.created[0];
  assert.doesNotMatch(reason, /256-color|YOLO mode|\[STARTUP\]/);
  assert.match(reason, /…z{400}\)/, 'without a JSON error, the tail is kept, not the head');
});

test('a timeout with no output still reports, and an empty review counts as a failure', async () => {
  let { core, calls } = await post({ PRIMARY_OUTCOME: 'failure' });
  assert.ok(core.failed);
  assert.match(calls.created[0], /may have timed out/);
  ({ core, calls } = await post({ PRIMARY_OUTCOME: 'success', PRIMARY_BODY: MARKER }));
  assert.ok(core.failed);
  assert.match(calls.created[0], /empty review/);
});

test('a failure keeps a completed review of the same head and replaces an older head', async () => {
  let { core, calls } = await post({ PRIMARY_OUTCOME: 'failure' }, { comments: [sticky('ok', HEAD)] });
  assert.ok(core.failed);
  assert.deepEqual([calls.created, calls.updated], [[], []]);
  const forged = { ...sticky('ok', OLD_HEAD), body: `${sticky('ok', OLD_HEAD).body}\n<!-- gemini-review-status: ok head=${HEAD} -->` };
  ({ core, calls } = await post({ PRIMARY_OUTCOME: 'failure' }, { comments: [forged] }));
  assert.equal(calls.updated.length, 1, 'a status line inside the model text does not protect a review of this head');
  ({ core, calls } = await post({ PRIMARY_OUTCOME: 'failure' }, { comments: [sticky('ok', OLD_HEAD)] }));
  assert.ok(core.failed);
  assert.equal(calls.updated.length, 1);
  assert.match(calls.updated[0].body, /status: failed/);
  assert.match(calls.updated[0].body, new RegExp(`last completed review was of head ${OLD_HEAD}`));
});

test('a review for a superseded head or a closed PR is discarded without failing', async () => {
  for (const pr of [openPr({ head: { sha: OLD_HEAD, repo: { full_name: 'o/r' } } }), openPr({ state: 'closed' })]) {
    const { core, calls } = await post({ PRIMARY_OUTCOME: 'failure' }, { pr });
    assert.equal(core.failed, null);
    assert.deepEqual([calls.created, calls.updated], [[], []]);
  }
});

// --- GEMINI.md states the workflow's actual values ------------------------------

test('GEMINI.md describes the triggers, budget and fallback the workflow uses', () => {
  const flat = read('GEMINI.md').replace(/\s+/g, ' ');
  for (const claim of ['`@codex review`', '`@gemini-cli`', '`GEMINI_FALLBACK_MODEL`', '`gemini-3.5-flash-lite`',
    '30 turns', 'telemetry', '`read_many_files`', '10-minute timeout within a 15-minute job', 'queue rather than cancel',
    'at most 3 attempts per request']) {
    assert.ok(flat.includes(claim), `GEMINI.md no longer says ${claim}; keep it and the workflow together`);
  }
});
