# Use Home Assistant base image (required for Home Assistant addons)
# Format: ghcr.io/home-assistant/{arch}-base:latest
# Override with: --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest

# Build stage - contains all build tools
FROM ${BUILD_FROM} AS builder

# Set shell (use sh for Alpine, bash will be installed)
SHELL ["/bin/sh", "-o", "pipefail", "-c"]

# Install build dependencies
# Install LLVM 15 from Alpine 3.17 repository (required by llvmlite 0.44.0)
# This works with hassio-addons/base images which may have newer LLVM versions
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
    && echo "Installing LLVM 15 from Alpine 3.17 repository (required for llvmlite 0.44.0)..." \
    && apk add --no-cache --repository=http://dl-cdn.alpinelinux.org/alpine/v3.17/main \
        llvm15-dev llvm15-static clang15 \
    && ln -sf /usr/lib/llvm15/bin/llvm-config /usr/bin/llvm-config \
    && /usr/bin/llvm-config --version \
    && echo "LLVM 15 installed successfully"

# Switch to bash after installation
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install Poetry
RUN \
    pip3 install --no-cache-dir --break-system-packages poetry \
    && poetry config virtualenvs.create false \
    && poetry config virtualenvs.in-project false

# Copy Poetry configuration files and app directory for package installation
COPY pyproject.toml poetry.lock* /tmp/
RUN mkdir -p /tmp/openai_realtime_voice_agent
COPY app/ /tmp/openai_realtime_voice_agent/app/

# Install dependencies using Poetry
WORKDIR /tmp
ENV PIP_BREAK_SYSTEM_PACKAGES=1
ENV POETRY_VIRTUALENVS_CREATE=false
# Find and set LLVM_CONFIG path
RUN \
    LLVM_CONFIG_PATH=$(which llvm-config || find /usr/bin /usr/local/bin -name llvm-config* 2>/dev/null | head -1) \
    && echo "LLVM_CONFIG_PATH=$LLVM_CONFIG_PATH" \
    && export LLVM_CONFIG="${LLVM_CONFIG_PATH:-/usr/bin/llvm-config}" \
    && echo "Using LLVM_CONFIG=$LLVM_CONFIG" \
    && rm -f poetry.lock* \
    && LLVM_CONFIG="$LLVM_CONFIG" POETRY_VIRTUALENVS_CREATE=false poetry install --only=main --no-root --no-interaction --no-ansi \
    && rm -f pyproject.toml \
    && rm -rf openai_realtime_voice_agent/

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
        alsa-utils

# Switch to bash after installation
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Copy installed Python packages from builder stage
# Find Python site-packages directory and copy packages
RUN \
    --mount=from=builder,source=/usr/lib,target=/builder-lib \
    --mount=from=builder,source=/usr/local/bin,target=/builder-bin \
    PYTHON_SITE=$(python3 -c "import site; print(site.getsitepackages()[0])") \
    && echo "Target site-packages: $PYTHON_SITE" \
    && find /builder-lib -type d -name "site-packages" | while read src_dir; do \
        echo "Copying from $src_dir to $PYTHON_SITE"; \
        mkdir -p "$PYTHON_SITE"; \
        cp -r "$src_dir"/* "$PYTHON_SITE"/ 2>/dev/null || true; \
    done \
    && if [ -d "/builder-bin" ]; then \
        cp -r /builder-bin/* /usr/local/bin/ 2>/dev/null || true; \
    fi

# Copy application code
COPY app/ /app/

# Copy root filesystem
COPY root/ /
RUN chmod a+x /run.sh

# Set entrypoint
CMD ["/run.sh"]
