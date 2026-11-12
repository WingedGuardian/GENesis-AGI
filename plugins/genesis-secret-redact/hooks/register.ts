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
// withheld with a notice naming this plugin. Errored tool calls (`isError`) are returned as they came, because
// a hook cannot answer in that form; their text is redacted where the row is stored and sent (`session.append`).
//
// Monitor and background Bash output also reach records this mod cannot rewrite; bin/stream_wrap.py (a
// PreToolUse hook in this plugin) redacts those at the source. The `.catch` handling follows the pattern of
// the secret-shield mod (Singh-AP/awesome-claude-mods, MIT); no code is copied from it.

type Pair = [text: string, placeholder: string]

let pairs: Pair[] = []
let raws = new Map<string, string>() // placeholder -> the value as written in the file (never another spelling)
let secretsPath = ''
let secretsReal = '' // the secrets file with symlinks resolved, for the restore-on-write target check
let stamp = '' // mtimeMs:size of secretsPath, taken BEFORE the last load read it
let warned = false

const WITHHELD = '[genesis-secret-redact could not redact this output, so it was withheld; the plugin is failing: check `claude --debug` output]'

function repoRoot(pluginRoot: string): string {
  return pluginRoot.replace(/\/+$/, '').replace(/\/plugins\/[^/]+$/, '')
}

function redactText(s: string): string {
  for (const [text, placeholder] of pairs) if (s.includes(text)) s = s.split(text).join(placeholder)
  return s
}

function restoreText(s: string): string {
  for (const [placeholder, raw] of raws) if (s.includes(placeholder)) s = s.split(placeholder).join(raw)
  return s
}

function deep(x: any, f: (s: string) => string): any {
  if (typeof x === 'string') return f(x)
  if (Array.isArray(x)) return x.map(i => deep(i, f))
  if (x && typeof x === 'object') {
    const o: any = {}
    for (const k of Object.keys(x)) o[k] = deep(x[k], f)
    return o
  }
  return x
}

// Whether a write's target is the secrets file, by resolved path: a symlink (of any name) or another spelling of
// the path that missed the check would write placeholder text over the real values. One `realpath` run per
// Edit/Write, which is cheap next to the write itself.
async function isSecretsFile($: any, target: unknown): Promise<boolean> {
  if (typeof target !== 'string' || !secretsPath) return false
  if (target === secretsPath || target === secretsReal) return true
  const run = await $.process.run(['realpath', '-m', '--', target], { timeoutMs: 5000 }).catch(() => undefined)
  if (!run || run.exitCode !== 0) {
    // Cannot tell: placeholders stay literal (a write that then targets the secrets file would store them as text).
    $.ui.log(`genesis-secret-redact: could not resolve ${target}; placeholders in this write are not restored`)
    return false
  }
  return run.stdout.trim() === secretsReal
}

// Rewrite a large output Claude Code saved to its session's tool-results folder. Only such a path: a tool result
// that merely names some other file must not get that file rewritten.
async function cleanSaved($: any, path: string): Promise<void> {
  if (!/\/tool-results\/[^/]+$/.test(path)) return
  try {
    const text = await $.fs.read(path)
    const clean = redactText(text)
    if (clean !== text) await $.fs.write(path, clean)
  } catch (err) {
    // The model's copy is still redacted (and any Read of that file is redacted here); the disk copy is not.
    $.ui.log(`genesis-secret-redact: could not clean saved output ${path} (${(err as Error)?.message ?? err})`)
  }
}

// The fallback when a structured walk throws: redact the JSON text and parse it back.
function viaJson(x: any): any {
  return JSON.parse(redactText(JSON.stringify(x)))
}

async function statStamp($: any, path: string): Promise<string> {
  const st = await $.fs.stat(path).catch(() => undefined)
  return st && st.kind === 'file' ? `${st.mtimeMs}:${st.size}` : 'absent'
}

// The install's venv interpreter when there is one (it has python-dotenv, the runtime's own parser), else python3.
async function python($: any, root: string): Promise<string> {
  const venv = `${root}/.venv/bin/python`
  const st = await $.fs.stat(venv).catch(() => undefined)
  return st && st.kind === 'file' ? venv : 'python3'
}

async function load($: any): Promise<void> {
  const root = repoRoot($.plugin.root)
  // Stamp first: a write that lands while the loader reads makes the next check differ and reload again.
  const before = secretsPath ? await statStamp($, secretsPath) : ''
  const run = await $.process.run([await python($, root), '-I', `${$.plugin.root}/bin/known_values.py`, '--json', '--repo-root', root], { timeoutMs: 20000 })
  if (run.exitCode !== 0) throw new Error(`known_values exited ${run.exitCode}`)
  const parsed = JSON.parse(run.stdout)
  type Needle = { var: string; text: string; raw: boolean }
  pairs = parsed.needles.map((n: Needle) => [n.text, `[secrets.env:${n.var}]`] as Pair)
  raws = new Map(parsed.needles.filter((n: Needle) => n.raw).map((n: Needle) => [`[secrets.env:${n.var}]`, n.text]))
  stamp = secretsPath === parsed.path && before ? before : await statStamp($, parsed.path)
  secretsPath = parsed.path
  secretsReal = parsed.realpath ?? parsed.path
}

let loading: Promise<void> | null = null // one loader run at a time; concurrent events share it

// Reload when the secrets file changed since the last load (a key added or rotated mid-session).
async function fresh($: any): Promise<void> {
  try {
    if (!secretsPath || (await statStamp($, secretsPath)) !== stamp) {
      loading ??= load($).finally(() => {
        loading = null
      })
      await loading
    }
  } catch (err) {
    // Keep the last values that loaded: they are still secrets. Without any, this session is unredacted.
    if (!warned) {
      warned = true
      const state = pairs.length ? 'keeping the values loaded earlier' : 'this session is NOT redacted'
      $.ui.log(`genesis-secret-redact: cannot load secret values, ${state} (${(err as Error)?.message ?? err})`)
    }
  }
}

export function register(on: any) {
  on('session.start', async ($: any, e: any, next: any) => {
    await fresh($)
    return next(e)
  })

  on('prompt.submit', async ($: any, e: any, next: any) => {
    await fresh($)
    return typeof e.text === 'string' ? next({ ...e, text: redactText(e.text) }) : next(e)
  }).catch(($: any, e: any, next: any) => next(e)) // a failure here never blocks a prompt

  on('tool.call', async ($: any, e: any, next: any) => {
    await fresh($)
    let call = e
    // A write to the secrets file itself (onboarding adds or replaces a key): turn placeholders the model copied
    // from a redacted read back into the values, so the file keeps them. Nowhere else: a placeholder written to
    // any other file stays literal, or docs and tests about this plugin would receive real keys.
    if ((e.tool === 'Edit' || e.tool === 'Write' || e.tool === 'MultiEdit') && raws.size && (await isSecretsFile($, e.file_path))) {
      call = { ...deep(e, restoreText), tool: e.tool, file_path: e.file_path }
    }
    const ran = await next(call)
    if (ran.deny !== undefined || !pairs.length) return ran
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
    let result: any
    let context: any = ran.context
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
  }).catch(async ($: any, e: any, next: any) => {
    // The result goes through unredacted (its stored row is still redacted by session.append); say so.
    $.ui.log(`genesis-secret-redact: could not redact a ${e.tool} result (${next.error?.kind}: ${next.error?.message ?? ''})`)
    return next(e)
  })

  on('session.append', async ($: any, e: any, next: any) => {
    await fresh($)
    if (!pairs.length) return next(e)
    let content: any
    try {
      content = deep(e.message.content, redactText)
    } catch {
      content = viaJson(e.message.content)
    }
    return next({ ...e, message: { ...e.message, content } })
  }).catch(($: any, e: any, next: any) => {
    // As secret-shield: a row that could not be scanned keeps its shape, never its text.
    if (next.called) return next(e)
    const content = Array.isArray(e.message?.content)
      ? e.message.content.map((b: any) => (b && b.type === 'text' ? { ...b, text: WITHHELD } : b))
      : WITHHELD
    return next({ ...e, message: { ...e.message, content } })
  })
}
