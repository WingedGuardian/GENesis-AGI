"""Tests for the output content scanner."""

from genesis.security.output_scanner import scan_outbound

# --- Safe content ---

def test_safe_plain_text():
    result = scan_outbound("Thanks for your interest in Genesis! Check out the repo.")
    assert result.safe is True
    assert result.detected == []
    assert result.risk_level == "none"


def test_safe_technology_mentions():
    """General technology names should NOT be flagged."""
    result = scan_outbound(
        "Genesis is built on Python and uses Claude as its reasoning engine. "
        "It runs as a systemd service and stores data in SQLite."
    )
    assert result.safe is True


def test_safe_public_url():
    result = scan_outbound("Check out https://github.com/WingedGuardian/GENesis-AGI")
    assert result.safe is True


# --- API key detection ---

def test_detects_openai_key():
    result = scan_outbound("My key is sk-abc123def456ghi789jkl012mno")
    assert not result.safe
    assert "api_key_openai" in result.detected
    assert result.risk_level == "high"


def test_detects_anthropic_key():
    result = scan_outbound("Use sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    assert not result.safe
    assert "api_key_anthropic" in result.detected
    assert result.risk_level == "high"


def test_detects_groq_key():
    result = scan_outbound("Here's the key: gsk_abcdefghijklmnopqrstuvwxyz")
    assert not result.safe
    assert "api_key_groq" in result.detected


# --- Credential patterns ---

def test_detects_credential_assignment():
    result = scan_outbound("Set password=MyS3cretP@ssw0rd123 in config")
    assert not result.safe
    assert "credential_assignment" in result.detected
    assert result.risk_level == "medium"  # Not critical — prone to false positives


def test_detects_env_variable():
    result = scan_outbound("export DEEPINFRA_API_KEY=di_abc123456789")
    assert not result.safe
    assert "env_variable_secret" in result.detected


# --- IP addresses ---

def test_detects_rfc1918_ip():
    result = scan_outbound("Connect to 192.168.50.77 for the dashboard")
    assert not result.safe
    assert "rfc1918_ip" in result.detected
    assert result.risk_level == "medium"


def test_ignores_public_ip():
    """Public IPs are not flagged — only RFC 1918 private ranges."""
    result = scan_outbound("Our website is at 93.184.216.34")
    assert result.safe is True


# --- File paths ---

def test_detects_internal_file_path():
    result = scan_outbound("The config is at ~/.genesis/config/genesis.yaml")
    assert not result.safe
    assert "internal_file_path" in result.detected


def test_detects_home_path():
    result = scan_outbound("Check /home/ubuntu/genesis/data/genesis.db")
    assert not result.safe
    assert "internal_file_path" in result.detected


# --- Localhost ---

def test_detects_localhost_port():
    result = scan_outbound("Qdrant runs on localhost:6333")
    assert not result.safe
    assert "localhost_port" in result.detected


# --- Multiple patterns ---

def test_multiple_patterns_detected():
    result = scan_outbound(
        "Connect to 192.168.50.77 and use sk-ant-api03-testkey123456789xyz "
        "with password=admin123456"
    )
    assert not result.safe
    assert len(result.detected) >= 3
    assert result.risk_level == "high"
