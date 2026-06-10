# Use Home Assistant base image (required for Home Assistant addons)
# Format: ghcr.io/home-assistant/{arch}-base:latest
# Override with: --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest

# Build stage - compile native deps then install Python packages
FROM ${BUILD_FROM} AS builder

# Set shell (use sh for Alpine, bash will be installed)
SHELL ["/bin/sh", "-o", "pipefail", "-c"]

# Install build dependencies (no LLVM — we skip numba/llvmlite, incompatible with Py3.14)
RUN \
    apk add --no-cache \
        bash \
        python3 \
        python3-dev \
        py3-pip \
        py3-wheel \
        py3-setuptools \
        curl \
        gcc \
        g++ \
        libstdc++

# Switch to bash after installation
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Copy app for later staging
RUN mkdir -p /tmp/genesis_voice_bridge
COPY app/ /tmp/genesis_voice_bridge/app/

# Install dependencies via pip — deterministic path that skips native deps.
# numba/llvmlite/onnxruntime/resampy are incompatible with Python 3.14 in
# HA base image and unused at runtime (we use server-side SemanticTurnDetection,
# not local Silero VAD or resampy resampler).
WORKDIR /tmp
ENV PIP_BREAK_SYSTEM_PACKAGES=1
# Step 1: Install pipecat without dependencies
RUN pip3 install --no-cache-dir --break-system-packages --no-deps \
    "pipecat-ai[openai,websocket]==1.3.0"
# Step 2: Install all runtime deps we actually need (excluding native-compiled ones)
RUN pip3 install --no-cache-dir --break-system-packages \
    openai "websockets>=13.0" "fastapi>=0.115.0" "uvicorn[standard]>=0.23.0" \
    python-dotenv httpx \
    aiohttp aiofiles pydantic loguru numpy Pillow protobuf \
    nltk Markdown soxr pyloudnorm docstring_parser
# Stub onnxruntime — pipecat imports it for local SmartTurn VAD but we use
# server-side SemanticTurnDetection. No wheels exist for Alpine/musl.
RUN PYTHON_SITE=$(python3 -c "import site; print(site.getsitepackages()[0])") \
    && mkdir -p "$PYTHON_SITE/onnxruntime" \
    && cat > "$PYTHON_SITE/onnxruntime/__init__.py" << 'STUB'
"""Stub onnxruntime — real package unavailable on Alpine musl.
Local SmartTurn VAD is disabled; server-side SemanticTurnDetection is used instead.
"""

class _StubMeta(type):
    """Metaclass that returns stubs for any class-level attribute access."""
    def __getattr__(cls, name):
        return _Stub()

class _Stub(metaclass=_StubMeta):
    """Callable stub that accepts any args and returns itself."""
    def __init__(self, *args, **kwargs): pass
    def __call__(self, *args, **kwargs): return self
    def __getattr__(self, name): return _Stub()
    def __iter__(self): return iter([])
    def __bool__(self): return False
    def __int__(self): return 0
    def __str__(self): return ""

InferenceSession = _Stub
SessionOptions = _Stub
GraphOptimizationLevel = _Stub
ExecutionMode = _Stub
get_available_providers = lambda: []
STUB
# Cleanup
RUN rm -rf genesis_voice_bridge/

# Stage installed packages into a known directory for clean COPY
RUN mkdir -p /staging/site-packages /staging/bin \
    && PYTHON_SITE=$(python3 -c "import site; print(site.getsitepackages()[0])") \
    && cp -r "$PYTHON_SITE"/* /staging/site-packages/ \
    && cp -r /usr/local/bin/* /staging/bin/ 2>/dev/null || true

# Runtime stage - minimal image without build tools
FROM ${BUILD_FROM}

# Set shell (use sh for Alpine, bash will be installed)
SHELL ["/bin/sh", "-o", "pipefail", "-c"]

# Install only runtime dependencies (bash first for compatibility)
RUN \
    apk add --no-cache \
        bash \
        python3 \
        py3-pip \
        curl \
        ffmpeg \
        alsa-lib \
        alsa-utils \
        libstdc++

# Switch to bash after installation
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Copy installed Python packages from builder staging directory
# Uses standard COPY --from (no BuildKit required) for HA Supervisor compatibility
COPY --from=builder /staging/site-packages/ /tmp/site-packages/
COPY --from=builder /staging/bin/ /usr/local/bin/
RUN \
    PYTHON_SITE=$(python3 -c "import site; print(site.getsitepackages()[0])") \
    && echo "Installing packages to: $PYTHON_SITE" \
    && mkdir -p "$PYTHON_SITE" \
    && cp -r /tmp/site-packages/* "$PYTHON_SITE"/ \
    && rm -rf /tmp/site-packages

# Copy application code (ARG invalidates cache on rebuild)
ARG CACHE_BUST=1
COPY app/ /app/

# Copy root filesystem
COPY root/ /
RUN chmod a+x /run.sh

# Set entrypoint
CMD ["/run.sh"]
