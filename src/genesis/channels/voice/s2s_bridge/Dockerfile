# Use Home Assistant base image (required for Home Assistant addons)
# Format: ghcr.io/home-assistant/{arch}-base:latest
# Override with: --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest

# Build stage - compile native deps then install Python packages
FROM ${BUILD_FROM} AS builder

# Set shell (use sh for Alpine, bash will be installed)
SHELL ["/bin/sh", "-o", "pipefail", "-c"]

# Install build dependencies + LLVM 20 from Alpine edge (required by llvmlite 0.47.0)
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
        make \
        cmake \
        libstdc++ \
    && apk add --no-cache --repository=http://dl-cdn.alpinelinux.org/alpine/edge/main \
        llvm20-dev llvm20-static \
    && ln -sf /usr/lib/llvm20/bin/llvm-config /usr/bin/llvm-config \
    && /usr/bin/llvm-config --version

# Switch to bash after installation
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install Poetry
RUN \
    pip3 install --no-cache-dir --break-system-packages poetry \
    && poetry config virtualenvs.create false \
    && poetry config virtualenvs.in-project false

# Copy Poetry configuration files
COPY pyproject.toml poetry.lock* /tmp/
RUN mkdir -p /tmp/genesis_voice_bridge
COPY app/ /tmp/genesis_voice_bridge/app/

# Install dependencies — pre-install llvmlite from source (needs LLVM),
# then let Poetry handle the rest
WORKDIR /tmp
ENV PIP_BREAK_SYSTEM_PACKAGES=1
ENV POETRY_VIRTUALENVS_CREATE=false
RUN LLVM_CONFIG=/usr/bin/llvm-config \
    pip3 install --no-cache-dir --break-system-packages --no-build-isolation \
    "llvmlite==0.47.0"
RUN LLVM_CONFIG=/usr/bin/llvm-config \
    poetry install --only=main --no-root --no-interaction --no-ansi \
    && rm -f pyproject.toml \
    && rm -rf genesis_voice_bridge/

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
