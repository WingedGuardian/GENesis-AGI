# Genesis Container Setup — Handoff for Claude Code

**Context:** This is a freshly launched Ubuntu 24.04 Incus container on a dedicated VM. It will host Agent Zero (AI agent framework) and Genesis v3 (custom extensions). The VM was set up after a previous incident where a container's I/O storm corrupted the host — hardening is already applied at the VM/container level (I/O limits, memory caps, disk quotas). This document covers what needs to happen inside the container.

**Container resources:** 6 vCPUs, 24GiB RAM (hard limit), 140GB disk, 500 process limit, unprivileged, no nesting.

**Ollama is running in a sibling container** at `${OLLAMA_URL:-localhost:11434}` — do NOT install Ollama here.

---

## Step 1: System Packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
  git python3 python3-venv python3-pip python3-dev \
  sqlite3 jq ripgrep curl wget unzip \
  build-essential gfortran libopenblas-dev cmake
```

## Step 2: Node.js 20

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node --version  # Should be 20.x
```

## Step 3: GitHub CLI

```bash
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install -y gh
gh auth login  # Interactive — authenticate with GitHub
```

## Step 4: tmpfs for /tmp

```bash
echo 'tmpfs /tmp tmpfs rw,nosuid,nodev,size=512M 0 0' | sudo tee -a /etc/fstab
sudo mount -t tmpfs -o rw,nosuid,nodev,size=512M tmpfs /tmp
```

## Step 5: Clone Repositories

```bash
cd ~

# Agent Zero framework
git clone --depth=1 https://github.com/frdel/agent-zero.git

# Genesis v3 project
git clone https://github.com/YOUR_GITHUB_USER/genesis.git genesis
```

## Step 6: Python Virtual Environment

```bash
cd ~/agent-zero
python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip setuptools wheel
pip install scipy --only-binary :all:
pip install flask-basicauth==0.2.0
pip install -r requirements.txt --only-binary scipy
pip install anthropic httpx
```

## Step 7: API Keys

**Ask the user for their API keys before creating this file.**

```bash
cat > ~/agent-zero/.env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
QDRANT_URL=http://localhost:6333
OLLAMA_URL=http://${OLLAMA_URL:-localhost:11434}
ALLOW_CLONE=false
EOF

chmod 600 ~/agent-zero/.env
```

## Step 8: Genesis v3 Setup

```bash
cd ~/genesis
# If genesis has its own requirements.txt:
pip install -r requirements.txt 2>/dev/null || true
```

## Step 9: Qdrant (Native Binary)

Install as a systemd service, bound to localhost only.

```bash
# Download latest Qdrant binary
curl -fsSL https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-unknown-linux-musl.tar.gz | tar xz
sudo mv qdrant /usr/local/bin/

# Create data directory
mkdir -p ~/qdrant-data

# Config: localhost only
cat > ~/qdrant-config.yaml <<'EOF'
service:
  host: 127.0.0.1
  http_port: 6333
  grpc_port: 6334
storage:
  storage_path: ${HOME}/qdrant-data
EOF

# Systemd service
sudo tee /etc/systemd/system/qdrant.service > /dev/null <<'UNIT'
[Unit]
Description=Qdrant Vector Database
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu
ExecStart=/usr/local/bin/qdrant --config-path ${HOME}/qdrant-config.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now qdrant
```

**Note:** Adjust `User` and paths if the container's default user is not `ubuntu`. Check with `whoami`.

### Verify Qdrant

```bash
curl -s http://localhost:6333/collections | jq .
# Expected: {"result":{"collections":[]},"status":"ok","time":...}
```

## Step 10: Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | sh
```

## Step 11: Verification

```bash
node --version          # 20.x
python3 --version       # 3.12.x or 3.10.x
gh --version            # 2.x
df -h /tmp              # tmpfs 512M
curl -s http://localhost:6333/collections | jq .status  # "ok"
curl -s http://${OLLAMA_URL:-localhost:11434}/api/tags | jq .      # Ollama reachable (once ollama container is set up)
source ~/agent-zero/.venv/bin/activate && python -c "import anthropic; print('SDK OK')"
```

---

## Application-Level Hardening (after Agent Zero code is working)

These fixes prevent the class of crash that killed the previous setup. Apply them to the Agent Zero / Genesis codebase once it's running.

### P0 — Must be done before any real workloads

#### Shell Tool Hardening

Find the file containing `create_subprocess_shell` or `asyncio.subprocess`:

1. Add `start_new_session=True` to `asyncio.create_subprocess_shell()`
2. Replace `process.kill()` with `os.killpg(os.getpgid(process.pid), signal.SIGKILL)`
3. Replace `process.communicate()` with a streaming reader that caps stdout+stderr at **512KB**
4. Add command allowlist — parse with `shlex.split()`, check executable against:
   `ls, cat, grep, find, wc, head, tail, python, python3, pip, pip3, gh, sqlite3, curl, wget, git, diff, patch, sort, uniq, awk, sed, date, echo, printf, test, stat, file, jq, bc, tr, cut, tee, mkdir, cp, mv, touch`
5. Log every blocked command

#### Registry-Level Resource Limiter

Find `class ToolRegistry` or the tool execution entry point:

1. Wrap every tool execution with `asyncio.wait_for(timeout)` — default 60s, overrides: git=120s, web_fetch=30s, web_search=15s
2. Truncate tool output to **50,000 chars** before returning to the LLM
3. Log all timeouts and truncations

### P1 — High-value, do after P0

#### DynamicTool Sandbox

Find `class DynamicTool` — ensure these dunder attrs are blocked:
`__class__`, `__bases__`, `__subclasses__`, `__mro__`, `__globals__`, `__code__`, `__builtins__`, `__import__`, `__loader__`, `__spec__`, `__qualname__`, `__func__`, `__self__`, `__wrapped__`, `__closure__`, `__dict__`, `__getattribute__`, `__init_subclass__`, `__traceback__`

#### Subagent Timeout

Find subagent execution code — wrap entire subagent task in `asyncio.wait_for(timeout=300)` (5 min wall-clock). On cancel, track and kill all child process groups.

#### File Tool Limits

Find file read/write functions:
- Read: reject files > **10MB** (`file_path.stat().st_size`)
- Write: reject content > **2MB** (`len(content.encode("utf-8"))`)

#### Web Tool Limits

Find `httpx` or web fetch code:
- Replace `client.get(url)` with `client.stream("GET", url)` + `aiter_bytes(8192)` with **10MB** byte cap
- Check `Content-Length` header for early rejection

#### Git Tool Limits

Find git clone code:
- Wrap in `loop.run_in_executor()` + `asyncio.wait_for(timeout=120)`
- GitHub API size pre-check: reject repos > **50MB**
- Default to `--depth=1` (shallow clone)
- Disable clone by default (`ALLOW_CLONE=false` in .env)

### Guardrail Logging (cross-cutting)

Create a shared helper and call it from every block path:

```python
import logging
logger = logging.getLogger("guardrails")

async def log_guardrail_block(tool: str, reason: str, value, threshold) -> None:
    logger.warning(
        "Guardrail block: tool=%s reason=%s value=%s threshold=%s",
        tool, reason, value, threshold,
    )
```

### Default Limits Reference

| Resource | Limit |
|----------|-------|
| File read | 10MB |
| File write | 2MB |
| Web fetch | 10MB |
| Git clone | 50MB |
| Shell stdout | 512KB |
| Tool output to LLM | 50K chars |
| Default tool timeout | 60s |
| Git clone timeout | 120s |
| Subagent timeout | 300s |
