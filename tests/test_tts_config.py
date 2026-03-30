"""Tests for genesis.channels.tts_config — config loading, caching, sanitization."""


import yaml

from genesis.channels.tts_config import (
    ElevenLabsSettings,
    SanitizationSettings,
    TTSConfig,
    TTSConfigLoader,
    sanitize_for_speech,
)

# ── Config loading ──────────────────────────────────────────────────────


class TestTTSConfigLoader:
    def test_load_defaults_when_no_file(self, tmp_path):
        loader = TTSConfigLoader(path=tmp_path / "nonexistent.yaml")
        config = loader.load()
        assert isinstance(config, TTSConfig)
        assert config.provider == "elevenlabs"
        assert config.elevenlabs.stability == 0.85

    def test_load_from_yaml(self, tmp_path):
        cfg = tmp_path / "tts.yaml"
        cfg.write_text(yaml.dump({
            "provider": "fish_audio",
            "elevenlabs": {"stability": 0.5, "speed": 0.9},
            "sanitization": {"max_chars": 500},
        }))
        loader = TTSConfigLoader(path=cfg)
        config = loader.load()
        assert config.provider == "fish_audio"
        assert config.elevenlabs.stability == 0.5
        assert config.elevenlabs.speed == 0.9
        assert config.sanitization.max_chars == 500

    def test_mtime_caching(self, tmp_path):
        cfg = tmp_path / "tts.yaml"
        cfg.write_text(yaml.dump({"provider": "elevenlabs"}))
        loader = TTSConfigLoader(path=cfg)

        c1 = loader.load()
        c2 = loader.load()
        assert c1 is c2  # Same object — no re-parse

    def test_mtime_invalidation(self, tmp_path):
        import os
        import time

        cfg = tmp_path / "tts.yaml"
        cfg.write_text(yaml.dump({"elevenlabs": {"stability": 0.5}}))
        loader = TTSConfigLoader(path=cfg)
        c1 = loader.load()
        assert c1.elevenlabs.stability == 0.5

        # Ensure mtime changes (some filesystems have 1s granularity)
        time.sleep(0.05)
        cfg.write_text(yaml.dump({"elevenlabs": {"stability": 0.9}}))
        # Force mtime difference
        os.utime(cfg, (cfg.stat().st_mtime + 1, cfg.stat().st_mtime + 1))

        c2 = loader.load()
        assert c2.elevenlabs.stability == 0.9
        assert c1 is not c2

    def test_invalidate_forces_reload(self, tmp_path):
        cfg = tmp_path / "tts.yaml"
        cfg.write_text(yaml.dump({"provider": "elevenlabs"}))
        loader = TTSConfigLoader(path=cfg)
        c1 = loader.load()
        loader.invalidate()
        c2 = loader.load()
        assert c1 is not c2

    def test_env_var_fallback_for_voice_id(self, tmp_path, monkeypatch):
        cfg = tmp_path / "tts.yaml"
        cfg.write_text(yaml.dump({"elevenlabs": {"voice_id": ""}}))
        monkeypatch.setenv("TTS_VOICE_ID_ELEVENLABS", "env-voice-123")
        loader = TTSConfigLoader(path=cfg)
        config = loader.load()
        assert config.elevenlabs.voice_id == "env-voice-123"

    def test_yaml_voice_id_overrides_env(self, tmp_path, monkeypatch):
        cfg = tmp_path / "tts.yaml"
        cfg.write_text(yaml.dump({"elevenlabs": {"voice_id": "yaml-voice"}}))
        monkeypatch.setenv("TTS_VOICE_ID_ELEVENLABS", "env-voice")
        loader = TTSConfigLoader(path=cfg)
        config = loader.load()
        assert config.elevenlabs.voice_id == "yaml-voice"

    def test_env_only_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TTS_ELEVENLABS_STABILITY", "0.6")
        monkeypatch.setenv("TTS_ELEVENLABS_SPEED", "1.2")
        loader = TTSConfigLoader(path=tmp_path / "nope.yaml")
        config = loader.load()
        assert config.elevenlabs.stability == 0.6
        assert config.elevenlabs.speed == 1.2

    def test_voice_gate_default(self, tmp_path):
        cfg = tmp_path / "tts.yaml"
        cfg.write_text(yaml.dump({}))
        loader = TTSConfigLoader(path=cfg)
        config = loader.load()
        assert config.voice_gate_background is True

    def test_voice_gate_disabled(self, tmp_path):
        cfg = tmp_path / "tts.yaml"
        cfg.write_text(yaml.dump({"voice_gate": {"block_background_sessions": False}}))
        loader = TTSConfigLoader(path=cfg)
        config = loader.load()
        assert config.voice_gate_background is False


# ── Sanitization ────────────────────────────────────────────────────────


class TestSanitizeForSpeech:
    def test_strip_bold(self):
        assert sanitize_for_speech("**hello**") == "hello"

    def test_strip_italic(self):
        assert sanitize_for_speech("*hello*") == "hello"

    def test_strip_inline_code(self):
        assert sanitize_for_speech("`print()`") == "print()"

    def test_strip_code_block(self):
        text = "before\n```python\ncode\n```\nafter"
        result = sanitize_for_speech(text)
        assert "code" not in result
        assert "before" in result
        assert "after" in result

    def test_strip_link(self):
        assert sanitize_for_speech("[click here](https://example.com)") == "click here"

    def test_strip_heading(self):
        result = sanitize_for_speech("## Hello World")
        assert result == "Hello World"

    def test_strip_injection_chars(self):
        result = sanitize_for_speech("hello <script> $PATH {bad}")
        assert "<" not in result
        assert ">" not in result
        assert "$" not in result
        assert "{" not in result

    def test_truncation(self):
        settings = SanitizationSettings(max_chars=10)
        result = sanitize_for_speech("a" * 100, settings)
        assert len(result) == 10

    def test_no_strip_when_disabled(self):
        settings = SanitizationSettings(strip_markdown=False, max_chars=5000)
        result = sanitize_for_speech("**bold**", settings)
        assert result == "**bold**"

    def test_truncation_still_works_when_strip_disabled(self):
        settings = SanitizationSettings(strip_markdown=False, max_chars=5)
        result = sanitize_for_speech("**bold**", settings)
        assert len(result) == 5

    def test_blockquote_stripped(self):
        result = sanitize_for_speech("> quoted text")
        assert result == "quoted text"

    def test_hr_stripped(self):
        result = sanitize_for_speech("before\n---\nafter")
        assert "---" not in result

    def test_list_markers_stripped(self):
        result = sanitize_for_speech("- item one\n- item two")
        assert "- " not in result
        assert "item one" in result

    def test_numbered_list_stripped(self):
        result = sanitize_for_speech("1. first\n2. second")
        assert "1." not in result
        assert "first" in result

    def test_whitespace_collapsed(self):
        result = sanitize_for_speech("a\n\n\n\n\nb")
        assert "\n\n\n" not in result


# ── Dataclass defaults ──────────────────────────────────────────────────


class TestDataclassDefaults:
    def test_elevenlabs_defaults(self):
        el = ElevenLabsSettings()
        assert el.stability == 0.85
        assert el.similarity_boost == 0.7
        assert el.style == 0.3
        assert el.speed == 1.1
        assert el.model == "eleven_flash_v2_5"

    def test_tts_config_defaults(self):
        config = TTSConfig()
        assert config.provider == "elevenlabs"
        assert config.voice_gate_background is True
        assert config.sanitization.strip_markdown is True
        assert config.sanitization.max_chars == 2000
