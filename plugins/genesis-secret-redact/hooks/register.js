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
// Not covered here: Monitor and background Bash output also reach records Claude Code keeps itself and this mod
// cannot rewrite (queued notifications, task output files). The `.catch` handling follows the pattern of the
// secret-shield mod (Singh-AP/awesome-claude-mods, MIT); no code is copied from it.
//
// Plain JavaScript, so tests/test_plugins/test_secret_redact_mod.py can run it under node against a fake engine.

let pairs = [] // [text, placeholder], longest text first: every spelling of every value this session has held
let restores = new Map() // placeholder -> the text it stands for in the secrets file itself
let secretsPath = ''
let secretsReal = '' // the secrets file with symlinks resolved, for the restore-on-write target check
let home = '' // Claude Code's file tools expand a leading `~` (measured on 2.1.299), so the target check must too
let stamp = '' // mtimeMs:size of secretsPath, taken BEFORE the last load read it
let warned = false
let retryAt = 0 // no load before this time: a loader that keeps failing must not run on every event
let reported = '' // the overwritten keys last logged, so each set is logged once

const RETRY_MS = 30_000
const WRITES = new Set(['Edit', 'Write', 'MultiEdit'])
const WITHHELD = '[genesis-secret-redact could not redact this output, so it was withheld; the plugin is failing: check `claude --debug` output]'

function repoRoot(pluginRoot) {
  return pluginRoot.replace(/\/+$/, '').replace(/\/plugins\/[^/]+$/, '')
}

function redactText(s) {
  for (const [text, placeholder] of pairs) if (s.includes(text)) s = s.split(text).join(placeholder)
  return s
}

function restoreText(s) {
  for (const [placeholder, text] of restores) if (s.includes(placeholder)) s = s.split(placeholder).join(text)
  return s
}

function deep(x, f) {
  if (typeof x === 'string') return f(x)
  if (Array.isArray(x)) return x.map(i => deep(i, f))
  if (x && typeof x === 'object') {
    const o = {}
    for (const k of Object.keys(x)) o[k] = deep(x[k], f)
    return o
  }
  return x
}

// A path as the file tools will open it: `~` expanded, then symlinks and `..` resolved. undefined when it cannot
// be resolved. `realpath` prints the path and one newline; only that newline is removed, because a path that
// differs from the secrets file by trailing whitespace is a different file.
async function resolved($, target) {
  if (home && (target === '~' || target.startsWith('~/'))) target = home + target.slice(1)
  const run = await $.process.run(['realpath', '-m', '--', target], { timeoutMs: 5000 }).catch(() => undefined)
  return run && run.exitCode === 0 ? run.stdout.replace(/\n$/, '') : undefined
}

// Whether a write's target is the secrets file, by resolved path: a symlink (of any name) or another spelling of
// the path that missed the check would write placeholder text over the real values. One `realpath` run per
// Edit/Write, which is cheap next to the write itself.
async function isSecretsFile($, target) {
  if (typeof target !== 'string' || !secretsPath) return false
  if (target === secretsPath || target === secretsReal) return true
  const real = await resolved($, target)
  // Cannot tell: placeholders stay literal (a write that then targets the secrets file would store them as text).
  if (real === undefined) $.ui.log(`genesis-secret-redact: could not resolve ${target}; placeholders in this write are not restored`)
  return real === secretsReal
}

// Rewrite a large output Claude Code saved to its session's tool-results folder. Only a path that RESOLVES to
// such a file: a tool result that merely names some other file must not get that file rewritten, and a link
// from a tool-results folder to the secrets file must never get the values there replaced by placeholders.
async function cleanSaved($, path) {
  if (!/\/tool-results\/[^/]+$/.test(path)) return
  try {
    const real = await resolved($, path)
    if (real === undefined || real === secretsReal || !/\/tool-results\/[^/]+$/.test(real)) return
    const text = await $.fs.read(real)
    const clean = redactText(text)
    if (clean !== text) await $.fs.write(real, clean)
  } catch (err) {
    // The model's copy is still redacted (and any Read of that file is redacted here); the disk copy is not.
    $.ui.log(`genesis-secret-redact: could not clean saved output ${path} (${err?.message ?? err})`)
  }
}

// The fallback when a structured walk throws: redact the JSON text and parse it back.
function viaJson(x) {
  return JSON.parse(redactText(JSON.stringify(x)))
}

async function statStamp($, path) {
  const st = await $.fs.stat(path).catch(() => undefined)
  return st && st.kind === 'file' ? `${st.mtimeMs}:${st.size}` : 'absent'
}

// The install's venv interpreter when there is one (it has python-dotenv, the runtime's own parser), else python3.
async function python($, root) {
  const venv = `${root}/.venv/bin/python`
  const st = await $.fs.stat(venv).catch(() => undefined)
  return st && st.kind === 'file' ? venv : 'python3'
}

async function load($) {
  const root = repoRoot($.plugin.root)
  // Stamp first: a write that lands while the loader reads makes the next check differ and reload again.
  const before = secretsPath ? await statStamp($, secretsPath) : ''
  const run = await $.process.run([await python($, root), '-I', `${$.plugin.root}/bin/known_values.py`, '--json', '--repo-root', root], { timeoutMs: 20000 })
  if (run.exitCode !== 0) throw new Error(`known_values exited ${run.exitCode}`)
  if (run.isStdoutTruncated) throw new Error('known_values printed more than Claude Code keeps')
  const parsed = JSON.parse(run.stdout)
  const current = parsed.needles.map(n => [n.text, n.placeholder])
  // A value this session held stays redacted after it leaves the file: a rotated-out key is usually still valid
  // where it was issued, and the very call that replaced it echoes it back in its result (measured: an Edit).
  const texts = new Set(current.map(([text]) => text))
  pairs = [...current, ...pairs.filter(([text]) => !texts.has(text))].sort((a, b) => b[0].length - a[0].length)
  restores = new Map(Object.entries(parsed.restore))
  stamp = secretsPath === parsed.path && before ? before : await statStamp($, parsed.path)
  secretsPath = parsed.path
  secretsReal = parsed.realpath ?? parsed.path
  home = parsed.home ?? ''
  warned = false // a later failure is a new event and is logged again
  // A write that does not go through Edit/Write (`sed -i`, a heredoc) gets no restore, so placeholder text a
  // session copied from a redacted read can land in the secrets file as a value. That key is gone; say which.
  const overwritten = (parsed.overwritten ?? []).join(', ')
  if (overwritten && overwritten !== reported) $.ui.log(`genesis-secret-redact: the secrets file holds placeholder text, not a value, for ${overwritten}; restore those keys from a backup`)
  reported = overwritten
}

let loading = null // one loader run at a time; concurrent events share it

// Reload when the secrets file changed since the last load (a key added or rotated mid-session).
async function fresh($) {
  try {
    if (!secretsPath || (await statStamp($, secretsPath)) !== stamp) {
      if (Date.now() < retryAt) return
      loading ??= load($).finally(() => {
        loading = null
      })
      await loading
    }
  } catch (err) {
    retryAt = Date.now() + RETRY_MS
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
    // A write to the secrets file itself (onboarding adds or replaces a key): turn placeholders the model copied
    // from a redacted read back into the text they stood for there, so the file keeps its values. Nowhere else: a
    // placeholder written to any other file stays literal, or docs and tests about this plugin would receive
    // real keys.
    if (WRITES.has(e.tool) && restores.size && (await isSecretsFile($, e.file_path))) call = deep(e, restoreText)
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
    if (typeof persisted === 'string') await cleanSaved($, persisted)
    if (ran.isError === true) {
      // A hook cannot answer in the errored form, and core keeps the error text in the transcript's record of the
      // call. When that text holds a value, answer with the same text redacted as a `deny`, which the model
      // receives as the call's error result (the tool has already run; nothing is undone or repeated).
      const text = typeof ran.text === 'string' ? ran.text : typeof ran.result === 'string' ? ran.result : undefined
      if (text === undefined) return ran
      for (const m of text.matchAll(/\/\S*\/tool-results\/[^\s"'<>]+/g)) await cleanSaved($, m[0])
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
    // The result goes through unredacted (its stored row is still redacted by session.append); say so.
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
