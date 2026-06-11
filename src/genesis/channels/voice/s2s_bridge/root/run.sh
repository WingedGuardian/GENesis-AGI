#!/usr/bin/with-contenv bashio
set -e

# Read configuration from HA addon options
OPENAI_API_KEY=$(bashio::config 'openai_api_key')
WEBSOCKET_PORT=$(bashio::config 'websocket_port')
GENESIS_URL=$(bashio::config 'genesis_url')
GENESIS_TOKEN=$(bashio::config 'genesis_token')

# Turn detection settings (semantic VAD — replaces old threshold-based server_vad)
SEMANTIC_VAD_EAGERNESS=$(bashio::config 'semantic_vad_eagerness')

# Session management
SESSION_REUSE_TIMEOUT_SECONDS=$(bashio::config 'session_reuse_timeout_seconds')

# Audio recording (optional, for debugging)
ENABLE_RECORDING=$(bashio::config 'enable_recording')

# Validate required configuration
if [ -z "$OPENAI_API_KEY" ]; then
    bashio::log.error "openai_api_key is required but not set"
    exit 1
fi

# Export environment variables for the Python application
export OPENAI_API_KEY
export WEBSOCKET_PORT
export GENESIS_URL
export GENESIS_TOKEN

export SEMANTIC_VAD_EAGERNESS

export SESSION_REUSE_TIMEOUT_SECONDS
export ENABLE_RECORDING

# Disable Python output buffering for real-time logs
export PYTHONUNBUFFERED=1

# Start the Pipecat voice bridge
exec python3 -m app.main
