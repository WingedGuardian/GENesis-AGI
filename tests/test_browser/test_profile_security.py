"""Security-review findings on the cookie tools (browser/profile.py).

* A browser that opens the profile between the in-use check and the commit
  would write its in-memory cookies back over the edit: re-checked before
  commit, and the edit is rolled back.
* A cookie store that is a symlink is refused: it could point the edit at
  another database.
* export_state reads Chromium's schema only; any other profile used to export
  an empty list without saying so.
"""

from __future__ import annotations

import os

import pytest

from genesis.browser.profile import BrowserProfileManager, ProfileInUse

from .test_profile_manager import _camoufox_profile


def test_a_browser_opening_the_profile_mid_clear_rolls_the_edit_back(tmp_path, monkeypatch):
    mgr = _camoufox_profile(tmp_path / "camoufox-profile", [".x.com", ".netflix.com"])
    answers = iter([None, 4242])  # free at the first check, taken at commit
    monkeypatch.setattr(mgr, "running_pid", lambda: next(answers))
    with pytest.raises(ProfileInUse, match="nothing was changed"):
        mgr.clear_domain("x.com")
    assert {s.domain for s in mgr.get_info().sessions} == {"x.com", "netflix.com"}


def test_a_symlinked_cookie_store_is_not_edited(tmp_path):
    real = _camoufox_profile(tmp_path / "elsewhere", [".x.com"])
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    os.symlink(real.profile_dir / "cookies.sqlite", profile / "cookies.sqlite")
    mgr = BrowserProfileManager(profile, browser="camoufox")
    with pytest.raises(ValueError, match="symlink"):
        mgr.clear_domain("x.com")
    assert {s.domain for s in real.get_info().sessions} == {"x.com"}


def test_export_state_refuses_a_non_chromium_profile(tmp_path):
    mgr = _camoufox_profile(tmp_path / "camoufox-profile", [".x.com"])
    with pytest.raises(NotImplementedError):
        mgr.export_state(tmp_path / "state.json")
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("suffix", ["co.uk", "com.au", "github.io", ".CO.UK"])
def test_a_public_suffix_is_never_cleared(suffix):
    """Codex round 1: rejecting only single labels still let `co.uk` through,
    which matches bank.co.uk and shop.co.uk alike (a bulk logout)."""
    from genesis.browser.profile import normalize_domain

    with pytest.raises(ValueError, match="public suffix"):
        normalize_domain(suffix)


@pytest.mark.parametrize("domain", ["bank.co.uk", "user.github.io", "x.com", "localhost", "app.internal"])
def test_registrable_domains_still_clear(domain):
    from genesis.browser.profile import normalize_domain

    assert normalize_domain(domain) == domain


@pytest.mark.parametrize(
    ("domain", "ascii_form"),
    [
        ("bücher.example", "xn--bcher-kva.example"),
        ("Bücher.Example", "xn--bcher-kva.example"),
        # UTS46 non-transitional, as browsers store it; the stdlib codec
        # (IDNA 2003) would give strasse.de, a different site.
        ("straße.de", "xn--strae-oqa.de"),
    ],
)
def test_an_internationalized_domain_matches_its_ascii_cookie_host(domain, ascii_form):
    """Devin round 1: browsers store cookie hosts as ASCII IDNA labels, so a
    Unicode domain matched nothing and the logout reported zero."""
    from genesis.browser.profile import normalize_domain

    assert normalize_domain(domain) == ascii_form


def test_an_internationalized_domain_clears_from_the_profile_file(tmp_path):
    mgr = _camoufox_profile(
        tmp_path / "camoufox-profile", [".xn--bcher-kva.example", ".example.com"]
    )
    assert mgr.clear_domain("bücher.example") == 1
    assert {s.domain for s in mgr.get_info().sessions} == {"example.com"}


@pytest.mark.parametrize("suffix", ["公司.cn", "ｃｏ．ｕｋ"])
def test_an_internationalized_public_suffix_is_still_refused(suffix):
    """The suffix check runs on the encoded form: full-width ``ｃｏ．ｕｋ``
    is ``co.uk`` once encoded, and had no ASCII dot before."""
    from genesis.browser.profile import normalize_domain

    with pytest.raises(ValueError, match="public suffix"):
        normalize_domain(suffix)


@pytest.mark.parametrize("domain", ["x.com.", "X.COM.", "x.com。", "bücher.example."])
def test_a_trailing_dot_is_dropped(domain):
    """A trailing dot (FQDN spelling) matched no cookie host, so the clear
    reported zero while the cookies stayed."""
    from genesis.browser.profile import normalize_domain

    assert not normalize_domain(domain).endswith(".")
    assert normalize_domain(domain) in {"x.com", "xn--bcher-kva.example"}
