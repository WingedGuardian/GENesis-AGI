// genesis-secret-redact: keep this install's secrets.env values out of everything a Claude Code session reads
// or keeps. A value is replaced by `[secrets.env:VAR]` in tool results (the model's copy and the transcript's),
// in every row the session stores, in a persisted large output, and in the typed prompt. The values and all
// their spellings come from one place, bin/known_values.py, run as a child process; they live only in this
// process's memory.
//
// Failure directions: if the values cannot be loaded, the session runs unredacted and says so (fail open,
// loudly). If redacting one tool result throws, a string-level replacement is tried; if that throws too, the
// result goes through unchanged and the failure is logged (a result must keep its tool's shape, or Claude Code
// refuses it and the tool call fails: measured). If redacting a stored row throws twice, that row's text is
// withheld with a notice naming this plugin. An errored tool call (`isError`) whose text holds a value is
// answered with the same text redacted (as a `deny`, the one form a hook can answer an error in).
//
// One refusal: a write to the secrets file that would store placeholder text as a value, and any write carrying
// placeholder text while which file is the secrets file is not known: before a first load, or since a reload
// failed (see tool.call).
//
// Not covered here: Monitor and background Bash output also reach records Claude Code keeps itself and this mod
// cannot rewrite (queued notifications, task output files). The `.catch` handling follows the pattern of the
// secret-shield mod (Singh-AP/awesome-claude-mods, MIT); no code is copied from it.
//
// Plain JavaScript, so tests/test_plugins/test_secret_redact_mod.py can run it under node against a fake engine.

let pairs = [] // [text, placeholder], longest text first: every spelling of every value this session has held
let restores = new Map() // placeholder -> text: `pairs` read backwards (one placeholder per text, so it is exact)
let secretsPath = ''
let secretsReal = '' // the secrets file with symlinks resolved, for the restore-on-write target check
let home = '' // Claude Code's file tools expand a leading `~` (measured on 2.1.299); the engine's stat does not
let watch = [] // every path that can change which file the resolver picks, or what it holds
let stamp = '' // mtimeMs:size of each watched path, taken BEFORE the last load read the file
let warned = false
let retryAt = 0 // no load before this time: a loader that keeps failing must not run on every event
// The last reload failed, so the secrets file may no longer be `secretsPath` (the resolver can have moved to a
// file created since): which file holds the values is unconfirmed until a load succeeds again.
let unconfirmed = false
let reported = '' // the overwritten keys last logged, so each set is logged once

const RETRY_MS = 30_000
const WRITES = new Set(['Edit', 'Write'])
const WITHHELD = '[genesis-secret-redact could not redact this output, so it was withheld; the plugin is failing: check `claude --debug` output]'
const PLACEHOLDER = /\[secrets\.env:[^\]\r\n]*\]/g
// A file a session saved: .../<session id>/tool-results/<name>, or deeper under that session's folder.
const SAVED = /\/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\/(?:[^/]+\/)*tool-results\/[^/]+$/

function repoRoot(pluginRoot) {
  return pluginRoot.replace(/\/+$/, '').replace(/\/plugins\/[^/]+$/, '')
}

// One pass each way: text a replacement produced is never matched again (a value may equal another variable's
// name, so a second pass would rewrite inside a placeholder). Placeholders this process issued are in the
// alternation too, mapped to themselves, so text that was redacted by one hook comes out of the next unchanged.
// Longest first, so the alternation takes the longest match at each position.
let matcher = null // RegExp of every issued placeholder and every text in `pairs`, rebuilt on each load
const byText = new Map()

function redactText(s) {
  return matcher ? s.replace(matcher, m => byText.get(m) ?? m) : s
}

function restoreText(s) {
  return s.replace(PLACEHOLDER, m => restores.get(m) ?? m)
}

function deep(x, f) {
  if (typeof x === 'string') return f(x)
  if (Array.isArray(x)) return x.map(i => deep(i, f))
  if (x && typeof x === 'object') {
    const o = {}
    for (const k of Object.keys(x)) o[f(k)] = deep(x[k], f) // a result keyed by a value holds it in the key
    return o
  }
  return x
}

// `.` and `..` taken out by name, as the file tools do before they open a path.
function tidy(path) {
  const out = []
  for (const part of path.split('/')) {
    if (part === '..' && out.length && out[out.length - 1] !== '..' && out[out.length - 1] !== '') out.pop()
    else if (part !== '.' && (part !== '' || !out.length)) out.push(part)
  }
  const joined = out.join('/') || (path.startsWith('/') ? '/' : '.')
  return path.length > 1 && path.endsWith('/') && joined !== '/' ? `${joined}/` : joined
}

// Where a path lands, as the file tools will open it: `~` expanded (the engine's stat leaves it alone), `..`
// taken out by name, then links resolved by the engine. A path that is not there yet lands in the nearest folder
// that is, plus the rest. undefined when nothing on the way can be read.
async function resolved($, target) {
  if (home && (target === '~' || target.startsWith('~/'))) target = home + target.slice(1)
  let path = tidy(target)
  let rest = ''
  for (;;) {
    const st = await $.fs.stat(path, { resolve: true }).catch(() => undefined)
    if (st?.realPath !== undefined) return st.realPath.replace(/\/$/, '') + rest
    if (path === '/' || path === '.') return undefined
    const cut = path.lastIndexOf('/')
    rest = (cut < 0 ? `/${path}` : path.slice(cut)) + rest
    path = cut < 0 ? '.' : cut === 0 ? '/' : path.slice(0, cut)
  }
}

// Whether a write's target is the secrets file, by where it lands: a symlink (of any name) or another spelling
// of the path that missed the check would write placeholder text over the real values.
async function isSecretsFile($, target) {
  if (typeof target !== 'string' || !secretsPath) return false
  if (target === secretsPath || target === secretsReal) return true
  const real = await resolved($, target)
  // Cannot tell: placeholders stay literal (a write that then targets the secrets file would store them as text).
  if (real === undefined) $.ui.log(`genesis-secret-redact: could not resolve ${target}; placeholders in this write are not restored`)
  return real === secretsReal
}

// Rewrite a large output Claude Code saved for a session; true when the file was one and is now clean. Only a
// file that LANDS in a session's tool-results folder: a tool result that merely names some other file must not
// get that file rewritten, and a link from such a folder to the secrets file must never get the values there
// replaced by placeholders.
async function cleanSaved($, path) {
  try {
    const st = await $.fs.stat(path, { resolve: true })
    if (st.kind !== 'file' || st.realPath === undefined || st.realPath === secretsReal || !SAVED.test(st.realPath)) return false
    const text = await $.fs.read(st.realPath)
    const clean = redactText(text)
    if (clean !== text) await $.fs.write(st.realPath, clean)
    return true
  } catch (err) {
    // The model's copy is still redacted (and any Read of that file is redacted here); the disk copy is not.
    $.ui.log(`genesis-secret-redact: could not clean saved output ${path} (${err?.message ?? err})`)
    return false
  }
}

// Clean the saved outputs an errored call names in its text. Claude Code ends such a path at the end of its
// line and a folder name may hold spaces, while other tools quote a path or follow it with punctuation: so on
// each line every place the path could start and end is tried, longest first, until one is a saved file.
async function cleanNamed($, text) {
  for (const line of text.split('\n')) {
    const at = line.indexOf('/tool-results/')
    if (at < 0) continue
    const starts = []
    for (let i = 0; i <= at; i++) if (line[i] === '/' && (i === 0 || /[\s"'(:=]/.test(line[i - 1]))) starts.push(i)
    const name = at + '/tool-results/'.length
    const stop = line.slice(name).search(/[\s"'<>]/)
    const ends = new Set()
    for (let end of [line.trimEnd().length, stop < 0 ? line.length : name + stop]) {
      ends.add(end)
      while (end > name && /[).,;:]/.test(line[end - 1])) end--
      ends.add(end)
    }
    let done = false
    for (const start of starts) {
      for (const end of [...ends].sort((a, b) => b - a)) {
        const candidate = line.slice(start, end)
        if (done || !SAVED.test(candidate)) continue
        const st = await $.fs.stat(candidate).catch(() => undefined)
        if (st?.kind === 'file') done = await cleanSaved($, candidate)
      }
    }
    if (!done) $.ui.log('genesis-secret-redact: an error named a saved output that was not found to clean; its disk copy may hold a value')
  }
}

// Placeholder text left in what an Edit or Write would store.
function leftover(call) {
  const written = [call.content, call.new_string].filter(t => typeof t === 'string')
  return [...new Set(written.flatMap(t => t.match(PLACEHOLDER) ?? []))]
}

function unknownTarget(left) {
  return `genesis-secret-redact refused this write: it holds placeholder text (${left.join(', ')}), and the plugin cannot confirm which file is the secrets file because loading its values ${secretsPath ? 'failed at the last reload' : 'has not succeeded yet'}. Until a load succeeds (it is retried at the next tool call once 30 seconds have passed), write without placeholder text, or retry the write then.`
}

function refusal(left) {
  return `genesis-secret-redact refused this write to the secrets file: the text it would write holds placeholder text (${left.join(', ')}), which would be stored in place of a value. A placeholder is turned back into its value only in the text an Edit replaces (old_string). Write real values only; to change one line, Edit that line; to add a line next to a line holding a secret, anchor the Edit on a line without one.`
}

// The fallback when a structured walk throws: redact the JSON text and parse it back.
function viaJson(x) {
  return JSON.parse(redactText(JSON.stringify(x)))
}

async function statStamp($, path) {
  const st = await $.fs.stat(path).catch(() => undefined)
  return st && st.kind === 'file' ? `${st.mtimeMs}:${st.size}` : 'absent'
}

async function stampOf($, paths) {
  return (await Promise.all(paths.map(p => statStamp($, p)))).join('|')
}

// The install's venv interpreter when there is one (it has python-dotenv, the runtime's own parser), else python3.
async function python($, root) {
  const venv = `${root}/.venv/bin/python`
  const st = await $.fs.stat(venv).catch(() => undefined)
  return st && st.kind === 'file' ? venv : 'python3'
}

async function runLoader($) {
  const root = repoRoot($.plugin.root)
  const run = await $.process.run([await python($, root), '-I', `${$.plugin.root}/bin/known_values.py`, '--json', '--repo-root', root], { timeoutMs: 20000 })
  if (run.exitCode !== 0) throw new Error(`known_values exited ${run.exitCode}`)
  if (run.isStdoutTruncated) throw new Error('known_values printed more than Claude Code keeps')
  return JSON.parse(run.stdout)
}

async function load($) {
  // Stamp first: a write that lands while the loader reads makes the next check differ and reload again. When
  // the loader names paths that were not stamped (the first load, or the resolver's choice moved), stamp them
  // and read again, so the stamp is never newer than the values it stands for.
  let before = watch.length ? await stampOf($, watch) : ''
  let parsed = await runLoader($)
  let paths = parsed.watch ?? [parsed.path]
  if (paths.join('\n') !== watch.join('\n')) {
    before = await stampOf($, paths)
    parsed = await runLoader($)
    paths = parsed.watch ?? [parsed.path]
  }
  const current = parsed.needles.map(n => [n.text, n.placeholder])
  // A value this session held stays redacted after it leaves the file: a rotated-out key is usually still valid
  // where it was issued, and the very call that replaced it echoes it back in its result (measured: an Edit).
  // Its placeholder may now name the new value, so it gets a name of its own here; nothing restored is ever
  // written (see tool.call), so a name that moved can only make an Edit's old_string fail to match.
  const texts = new Set(current.map(([text]) => text))
  const taken = new Set(current.map(([, placeholder]) => placeholder))
  // A held value keeps the name it was already shown under, so a placeholder the session copied earlier still
  // means the same text; only a name the current values now use is renamed.
  const gone = pairs.filter(([text]) => !texts.has(text))
  for (const [, placeholder] of gone) if (!taken.has(placeholder)) taken.add(placeholder)
  const held = []
  for (const [text, placeholder] of gone) {
    let name = placeholder
    if (current.some(([, p]) => p === placeholder)) {
      for (let n = 1; taken.has(name); n++) name = placeholder.replace(/\]$/, n === 1 ? '~earlier]' : `~earlier${n}]`)
      taken.add(name)
    }
    held.push([text, name])
  }
  pairs = [...current, ...held].sort((a, b) => b[0].length - a[0].length)
  restores = new Map(pairs.map(([text, placeholder]) => [placeholder, text]))
  byText.clear()
  for (const [text, placeholder] of pairs) byText.set(text, placeholder)
  for (const placeholder of restores.keys()) byText.set(placeholder, placeholder)
  const alternatives = [...byText.keys()].sort((a, b) => b.length - a.length)
  const escaped = alternatives.map(text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  matcher = escaped.length ? new RegExp(escaped.join('|'), 'g') : null
  watch = paths
  stamp = before
  secretsPath = parsed.path
  secretsReal = parsed.realpath ?? parsed.path
  home = parsed.home ?? ''
  warned = false // a later failure is a new event and is logged again
  unconfirmed = false
  // A write that does not go through Edit/Write (`sed -i`, a heredoc) gets no restore, so placeholder text a
  // session copied from a redacted read can land in the secrets file as a value. That key is gone; say which.
  const overwritten = (parsed.overwritten ?? []).join(', ')
  if (overwritten && overwritten !== reported) $.ui.log(`genesis-secret-redact: the secrets file holds placeholder text, not a value, for ${overwritten}; restore those keys from a backup`)
  reported = overwritten
}

let loading = null // one loader run at a time; concurrent events share it

// Reload when the secrets file changed since the last load (a key added or rotated mid-session), and after a
// failed reload whatever the stamps say: the watched paths can return to the last good stamps (a newly preferred
// file removed again) while the failure still leaves the target unconfirmed, and only a load clears that.
async function fresh($) {
  try {
    if (!watch.length || unconfirmed || (await stampOf($, watch)) !== stamp) {
      if (Date.now() < retryAt) return
      loading ??= load($).finally(() => {
        loading = null
      })
      await loading
    }
  } catch (err) {
    retryAt = Date.now() + RETRY_MS
    unconfirmed = true
    // Keep the last values that loaded: they are still secrets. Without any, this session is unredacted.
    if (!warned) {
      warned = true
      const state = pairs.length ? 'keeping the values loaded earlier' : 'this session is NOT redacted'
      $.ui.log(`genesis-secret-redact: cannot load secret values, ${state} (${err?.message ?? err})`)
    }
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await fresh($)
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    await fresh($)
    return typeof e.text === 'string' ? next({ ...e, text: redactText(e.text) }) : next(e)
  }).catch(($, e, next) => next(e)) // a failure here never blocks a prompt

  on('tool.call', async ($, e, next) => {
    await fresh($)
    let call = e
    // A write to the secrets file itself (onboarding adds or replaces a key). Placeholder text in what it would
    // WRITE is refused: stored, it would replace a key with text, and restoring it cannot be made safe (a name's
    // meaning moves when a key rotates, and a resumed session never saw the text an old name stood for). Only
    // the text an Edit MATCHES gets its placeholders turned back, so the model can name a line it read; a wrong
    // restore there fails the tool's exact match and changes nothing. Nowhere else: a placeholder written to any
    // other file stays literal, or docs and tests about this plugin would receive real keys.
    if (WRITES.has(e.tool) && (!secretsPath || unconfirmed) && leftover(e).length) {
      // No load has succeeded, or the last reload failed, so which file is the secrets file is not known
      // (SECRETS_PATH may name any file, and the resolver may have moved): every write carrying placeholder text
      // is refused until a load succeeds, since this one may replace a key with text.
      return { deny: unknownTarget(leftover(e)) }
    }
    if (WRITES.has(e.tool) && (await isSecretsFile($, e.file_path))) {
      const left = leftover(e) // owner rulings 2026-11-12: refuse, and restore only what an Edit matches
      if (left.length) return { deny: refusal(left) }
      if (typeof e.old_string === 'string') call = { ...e, old_string: restoreText(e.old_string) }
    }
    const ran = await next(call)
    if (ran.deny !== undefined) {
      // A refusal from further down may quote the call's input, which now holds restored values.
      const clean = typeof ran.deny === 'string' ? redactText(ran.deny) : ran.deny
      return clean === ran.deny ? ran : { ...ran, deny: clean }
    }
    // The tool may have changed the secrets file: reload before its result is redacted, so a key it added or
    // rotated and printed is redacted too.
    await fresh($)
    if (!pairs.length) return ran
    // A large output is saved to a file and the result carries only its head: the value may be in the tail, so
    // the file is cleaned whatever the head holds. An errored call names the file only in its error text.
    const persisted = ran.result && typeof ran.result === 'object' ? ran.result.persistedOutputPath : undefined
    if (typeof persisted === 'string' && !(await cleanSaved($, persisted))) {
      $.ui.log(`genesis-secret-redact: left ${persisted} as it is (not a file in a session's tool-results folder); it may hold a value`)
    }
    if (ran.isError === true) {
      // A hook cannot answer in the errored form, and core keeps the error text in the transcript's record of the
      // call. When that text holds a value, answer with the same text redacted as a `deny`, which the model
      // receives as the call's error result (the tool has already run; nothing is undone or repeated).
      const text = typeof ran.text === 'string' ? ran.text : typeof ran.result === 'string' ? ran.result : undefined
      if (text === undefined) return ran
      await cleanNamed($, text)
      const clean = redactText(text)
      return clean === text ? ran : { deny: clean }
    }
    let result
    let context = ran.context
    try {
      result = deep(ran.result, redactText)
      if (context !== undefined) context = deep(context, redactText)
    } catch {
      result = viaJson(ran.result)
      if (context !== undefined) context = viaJson(context)
    }
    const same = JSON.stringify(result) === JSON.stringify(ran.result) && JSON.stringify(context) === JSON.stringify(ran.context)
    if (same) return ran // nothing to redact: core's own record
    return context === undefined ? { result } : { result, context }
  }).catch(async ($, e, next) => {
    // Failed before the tool ran: a write carrying placeholder text could not be checked, so it is refused rather
    // than run as it is (it may be aimed at the secrets file).
    if (!next.called && WRITES.has(e.tool) && leftover(e).length) return { deny: refusal(leftover(e)) }
    // The result goes through unredacted; say so.
    $.ui.log(`genesis-secret-redact: could not redact a ${e.tool} result (${next.error?.kind}: ${next.error?.message ?? ''})`)
    return next(e)
  })

  on('session.append', async ($, e, next) => {
    await fresh($)
    if (!pairs.length) return next(e)
    let content
    try {
      content = deep(e.message.content, redactText)
    } catch {
      content = viaJson(e.message.content)
    }
    return next({ ...e, message: { ...e.message, content } })
  }).catch(($, e, next) => {
    // As secret-shield: a row that could not be scanned keeps its shape, never its text.
    if (next.called) return next(e)
    const content = Array.isArray(e.message?.content)
      ? e.message.content.map(b => (b && b.type === 'text' ? { ...b, text: WITHHELD } : b))
      : WITHHELD
    return next({ ...e, message: { ...e.message, content } })
  })
}
