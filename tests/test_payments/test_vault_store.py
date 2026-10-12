"""Tests for the encrypted card vault store.

Only Stripe's public test numbers appear here, built from parts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
from datetime import UTC, datetime

import pytest
from pyrage import decrypt, x25519
from pyrage import passphrase as age_passphrase

from genesis.payments import vault_store as vs
from genesis.security.card_shapes import luhn_ok

PASSPHRASE = "quartz lantern ember noodle 41"
VISA = "4242" * 4  # Stripe's public test Visa


def _amex() -> str:
    """A Luhn-valid 15-digit 37-prefix number made at test time (no card literal)."""
    body = "37" + "0" * 12
    return next(body + c for c in "0123456789" if luhn_ok(body + c))


def _future() -> str:
    """An expiry two years out, computed per test (never frozen at import)."""
    return f"12/{(datetime.now(UTC).year + 2) % 100:02d}"


# A stand-in for scrypt that has the shape of a sealed key (header, stanza,
# 43-character MAC, 106-byte payload), so the vault reads as intact.
FAKE_SEALED = (
    b"age-encryption.org/v1\n-> scrypt "
    + b"c2FsdA 18\nd3JhcA\n--- "
    + b"m" * 43
    + b"\n"
    + b"p" * 106
)


@pytest.fixture(scope="module")
def _sealed_vault(tmp_path_factory):
    """One real init (scrypt costs seconds), copied fresh into each test."""
    path = tmp_path_factory.mktemp("sealed") / "payment_vault"
    vs.init_vault(PASSPHRASE, path)
    return path


@pytest.fixture
def vault(_sealed_vault, tmp_path):
    path = tmp_path / "payment_vault"
    shutil.copytree(_sealed_vault, path)
    os.chmod(path, 0o700)
    return path


def _identity(path):
    sealed = (path / vs.IDENTITY).read_bytes()
    return x25519.Identity.from_str(age_passphrase.decrypt(sealed, PASSPHRASE).decode())


def _mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def _add(path, number=VISA, **kw):
    kw.setdefault("label", "test")
    kw.setdefault("merchant", "shop.example.com")
    kw.setdefault("single_use", False)
    return vs.add_card(vs.parse_card(number, _future(), "123"), path=path, **kw)


class TestInit:
    def test_layout_and_modes(self, _sealed_vault):
        path = _sealed_vault
        assert _mode(path) == 0o700
        for name in (vs.IDENTITY, vs.RECIPIENT, vs.CARDS):
            assert _mode(path / name) == 0o600, name
        assert (path / vs.RECIPIENT).read_text().startswith("age1")
        assert not list(path.glob(".*.tmp"))
        # The identity opens with the passphrase and matches the recipient.
        identity = _identity(path)
        assert str(identity.to_public()) == (path / vs.RECIPIENT).read_text().strip()

    def test_refuses_a_weak_passphrase_and_creates_nothing(self, tmp_path):
        path = tmp_path / "v"
        with pytest.raises(vs.VaultError, match="guessable"):
            vs.init_vault("password1", path)
        assert not path.exists()

    def test_refuses_an_initialised_vault(self, vault):
        before = (vault / vs.IDENTITY).read_bytes()
        with pytest.raises(vs.VaultError, match="already initialised"):
            vs.init_vault(PASSPHRASE, vault)
        assert (vault / vs.IDENTITY).read_bytes() == before

    @pytest.mark.parametrize("killed_at", [vs.RECIPIENT, vs.IDENTITY])
    def test_a_run_killed_before_the_identity_lands_reads_uninitialised_and_reruns(
        self, tmp_path, monkeypatch, killed_at
    ):
        monkeypatch.setattr(age_passphrase, "encrypt", lambda data, pw: FAKE_SEALED)
        path = tmp_path / "v"
        real_replace, real_link = os.replace, os.link

        def dying(real):
            def call(src, dst, **kw):  # killed as this file is about to land
                if str(dst).endswith(killed_at):
                    raise KeyboardInterrupt
                real(src, dst, **kw)

            return call

        monkeypatch.setattr(vs.os, "replace", dying(real_replace))
        monkeypatch.setattr(vs.os, "link", dying(real_link))
        with pytest.raises(KeyboardInterrupt):
            vs.init_vault(PASSPHRASE, path)
        assert not vs.is_initialised(path)
        monkeypatch.setattr(vs.os, "replace", real_replace)
        monkeypatch.setattr(vs.os, "link", real_link)
        vs.init_vault(PASSPHRASE, path)
        assert vs.is_initialised(path)
        assert not list(path.glob(".*.tmp"))

    def test_tightens_a_loose_existing_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(age_passphrase, "encrypt", lambda data, pw: FAKE_SEALED)
        path = tmp_path / "v"
        path.mkdir(mode=0o755)
        os.chmod(path, 0o755)
        vs.init_vault(PASSPHRASE, path)
        assert _mode(path) == 0o700


class TestInitRefusals:
    @pytest.fixture(autouse=True)
    def _fast_seal(self, monkeypatch):
        monkeypatch.setattr(age_passphrase, "encrypt", lambda data, pw: FAKE_SEALED)

    def test_scores_a_passphrase_longer_than_72_characters(self, tmp_path):
        long = " ".join(["quartz lantern ember noodle"] * 3)
        assert len(long) > 72
        vs.init_vault(long, tmp_path / "v")
        assert vs.is_initialised(tmp_path / "v")

    def test_refuses_an_absurdly_long_passphrase(self, tmp_path):
        with pytest.raises(vs.VaultError, match="longer than"):
            vs.init_vault("x" * (vs.MAX_PASSPHRASE_LENGTH + 1), tmp_path / "v")

    def test_refuses_to_orphan_existing_cards(self, vault):
        _add(vault)
        recipient = (vault / vs.RECIPIENT).read_text()
        (vault / vs.IDENTITY).unlink()  # e.g. an attempt to "reset the passphrase"
        with pytest.raises(vs.VaultError, match="1 card"):
            vs.init_vault(PASSPHRASE, vault)
        assert (vault / vs.RECIPIENT).read_text() == recipient

    @pytest.mark.parametrize("shape", ["directory", "dangling symlink", "no recipient"])
    def test_refuses_any_existing_identity(self, vault, shape):
        (vault / vs.IDENTITY).unlink()
        if shape == "directory":
            (vault / vs.IDENTITY).mkdir()
        elif shape == "dangling symlink":
            (vault / vs.IDENTITY).symlink_to(vault / "nowhere")
        else:
            (vault / vs.IDENTITY).write_bytes(b"x")
            (vault / vs.RECIPIENT).unlink()
        with pytest.raises(vs.VaultError, match="incomplete"):
            vs.init_vault(PASSPHRASE, vault)
        with pytest.raises(vs.VaultError, match="incomplete"):
            _add(vault)

    THEIRS = b"age1" + b"q" * 58 + b"\n"  # a hand-copied vault's public key (shape only)

    def test_a_hand_copy_landing_after_our_recipient_keeps_its_own(self, tmp_path, monkeypatch):
        path = tmp_path / "v"
        real_link = os.link

        def copy_lands_first(src, dst, **kw):  # their whole pair lands just before our link
            if str(dst).endswith(vs.IDENTITY):
                (path / vs.IDENTITY).write_bytes(b"theirs")
                (path / vs.RECIPIENT).write_bytes(self.THEIRS)
            return real_link(src, dst, **kw)

        monkeypatch.setattr(vs.os, "link", copy_lands_first)
        with pytest.raises(vs.VaultError, match="appeared while this init ran"):
            vs.init_vault(PASSPHRASE, path)
        assert (path / vs.IDENTITY).read_bytes() == b"theirs"
        assert (path / vs.RECIPIENT).read_bytes() == self.THEIRS
        assert not list(path.glob(".*.tmp"))

    @pytest.mark.parametrize("their_recipient", [THEIRS, None])
    def test_a_hand_copy_landing_before_our_recipient_is_left_untouched(
        self, tmp_path, monkeypatch, their_recipient
    ):
        path = tmp_path / "v"
        real_create = vs._create_cards_db

        def copy_lands_before_our_recipient(dfd, *meta):
            real_create(dfd, *meta)
            (path / vs.IDENTITY).write_bytes(b"theirs")
            if their_recipient is not None:
                (path / vs.RECIPIENT).write_bytes(their_recipient)

        monkeypatch.setattr(vs, "_create_cards_db", copy_lands_before_our_recipient)
        with pytest.raises(vs.VaultError, match="appeared while this init ran"):
            vs.init_vault(PASSPHRASE, path)
        assert (path / vs.IDENTITY).read_bytes() == b"theirs"
        # Seen before this run's recipient was written: their files stay exactly as they were.
        if their_recipient is None:
            assert not (path / vs.RECIPIENT).exists()
            with pytest.raises(vs.VaultError, match="incomplete"):
                vs.check_ready(path)
        else:
            assert (path / vs.RECIPIENT).read_bytes() == their_recipient

    def test_a_key_pair_that_does_not_match_is_refused_at_retire(self, vault):
        ref = _add(vault)
        other = x25519.Identity.generate()
        (vault / vs.RECIPIENT).write_text(str(other.to_public()) + "\n")
        # check_ready now refuses it first, from the pair recorded at init.
        with pytest.raises(vs.VaultError, match="recipient.txt is not the public key"):
            vs.retire_card(ref, PASSPHRASE, vault)

    @pytest.mark.parametrize("keep", [33, 40, 150, 255])
    def test_a_truncated_identity_is_damage(self, vault, keep):
        data = (vault / vs.IDENTITY).read_bytes()
        assert len(data) == 256
        (vault / vs.IDENTITY).write_bytes(data[:keep])
        with pytest.raises(vs.VaultError, match="not a whole passphrase-sealed age key"):
            vs.check_ready(vault)

    def test_a_recipient_with_a_bad_checksum_is_an_error_in_status(self, vault):
        text = (vault / vs.RECIPIENT).read_text().strip()
        flipped = text[:-1] + ("q" if text[-1] != "q" else "p")
        (vault / vs.RECIPIENT).write_text(flipped + "\n")
        assert "recipient.txt" in vs.vault_status(vault)["error"]

    def test_a_cards_table_of_another_shape_is_foreign(self, tmp_path, monkeypatch):
        monkeypatch.setattr(age_passphrase, "encrypt", lambda data, pw: FAKE_SEALED)
        path = tmp_path / "v"
        path.mkdir(mode=0o700)
        with sqlite3.connect(path / vs.CARDS) as conn:
            conn.execute("CREATE TABLE cards (card_ref TEXT PRIMARY KEY, status TEXT)")
            conn.execute(f"PRAGMA user_version = {vs.SCHEMA_VERSION}")
        with pytest.raises(vs.VaultError, match="not an empty vault card database"):
            vs.init_vault(PASSPHRASE, path)

    def test_writes_go_to_the_locked_directory_even_if_another_is_moved_onto_the_path(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "v"
        placed = tmp_path / "placed"
        real_create = vs._create_cards_db

        def swap_mid_init(dfd, *meta):  # e.g. a restore places a vault while this one runs
            placed.mkdir()
            (placed / vs.IDENTITY).write_bytes(b"restored")
            (placed / vs.RECIPIENT).write_bytes(b"age1restored\n")
            path.rename(tmp_path / "old")
            placed.rename(path)
            real_create(dfd, *meta)

        monkeypatch.setattr(vs, "_create_cards_db", swap_mid_init)
        with pytest.raises(vs.VaultError, match="moved onto"):
            vs.init_vault(PASSPHRASE, path)
        assert sorted(p.name for p in path.iterdir()) == [vs.IDENTITY, vs.RECIPIENT]
        assert (path / vs.IDENTITY).read_bytes() == b"restored"
        assert (path / vs.RECIPIENT).read_bytes() == b"age1restored\n"

    def test_a_card_holding_db_copied_in_before_publication_is_not_adopted(
        self, tmp_path, monkeypatch, vault
    ):
        _add(vault)
        path = tmp_path / "v"
        real_check = vs.check_can_init
        calls = []

        def copy_after_the_last_check(p=None):
            real_check(p)
            calls.append(1)
            if len(calls) == 2:  # under the lock, after the re-check: a full cards.db lands
                shutil.copy(vault / vs.CARDS, path / vs.CARDS)

        monkeypatch.setattr(vs, "check_can_init", copy_after_the_last_check)
        with pytest.raises(vs.VaultError, match="sealed to an earlier key"):
            vs.init_vault(PASSPHRASE, path)
        assert not (path / vs.IDENTITY).exists()

    def test_makes_an_existing_empty_cards_db_private(self, tmp_path):
        path = tmp_path / "v"
        path.mkdir(mode=0o700)
        sqlite3.connect(path / vs.CARDS).close()
        os.chmod(path / vs.CARDS, 0o644)
        vs.init_vault(PASSPHRASE, path)
        assert _mode(path / vs.CARDS) == 0o600
        vs.check_ready(path)  # an add would not be refused as not private

    def test_refuses_a_regular_file_at_the_vault_path_before_any_prompt(self, tmp_path):
        path = tmp_path / "v"
        path.write_text("not a vault")
        with pytest.raises(vs.VaultError, match="not a directory you own"):
            vs.check_can_init(path)
        with pytest.raises(vs.VaultError, match="not a directory you own"):
            vs.init_vault(PASSPHRASE, path)


class TestParseCard:
    def test_accepts_spaced_and_hyphenated(self):
        for typed in (
            VISA,
            "4242 4242 4242 4242",
            "4242-4242-4242-4242",
            "4242\u00a04242\u00a04242\u00a04242",
        ):
            card = vs.parse_card(typed, _future(), "123")
            assert card["number"] == VISA and card["brand"] == "visa"
        assert vs.parse_card(_amex(), _future(), "1234")["brand"] == "amex"

    @pytest.mark.parametrize(
        "number", [VISA[:-1] + "1", "4242" * 3, "1234" * 4, VISA + "x", "٤" * 16, ""]
    )
    def test_refuses_a_bad_number_without_echoing_it(self, number):
        with pytest.raises(vs.VaultError) as exc:
            vs.parse_card(number, _future(), "123")
        assert "4242" not in str(exc.value)

    def test_expiry(self):
        year = datetime.now(UTC).year + 2
        assert vs.parse_card(VISA, f"01/{year}", "123")["exp_year"] == year
        assert vs.parse_card(VISA, f"12 / {year % 100}", "123")["exp_month"] == 12
        for bad in ("13/30", "1/30", "12-30", "", "12/3"):
            with pytest.raises(vs.VaultError, match="MM/YY"):
                vs.parse_card(VISA, bad, "123")
        with pytest.raises(vs.VaultError, match="expired"):
            vs.parse_card(VISA, "01/20", "123")

    def test_security_code_length_by_brand(self):
        with pytest.raises(vs.VaultError, match="security code"):
            vs.parse_card(VISA, _future(), "1234")
        with pytest.raises(vs.VaultError, match="security code"):
            vs.parse_card(_amex(), _future(), "123")
        with pytest.raises(vs.VaultError, match="security code"):
            vs.parse_card(VISA, _future(), "12a")


class TestAddAndList:
    def test_details_exist_only_encrypted(self, vault):
        ref = _add(vault, name="A Person", postal_code="00000")
        raw = (vault / vs.CARDS).read_bytes()
        assert VISA.encode() not in raw
        [card] = vs.list_cards(vault)
        assert card["card_ref"] == ref and card["last4"] == VISA[-4:]
        assert card["brand"] == "visa" and card["status"] == "active"
        assert "blob" not in card and not any("exp" in k for k in card)
        with sqlite3.connect(vault / vs.CARDS) as conn:
            (blob,) = conn.execute("SELECT blob FROM cards").fetchone()
        secret = json.loads(decrypt(blob, [_identity(vault)]))
        assert secret == {
            "card_ref": ref,
            "merchant": "shop.example.com",
            "single_use": False,
            "number": VISA,
            "exp_month": 12,
            "exp_year": int(_future()[-2:]) + 2000,
            "v": 1,
            "cvc": "123",
            "name": "A Person",
            "postal_code": "00000",
        }

    def test_needs_a_scope(self, vault):
        with pytest.raises(vs.VaultError, match="merchant scope"):
            _add(vault, merchant=None, single_use=False)
        assert vs.list_cards(vault) == []
        _add(vault, merchant=None, single_use=True)
        assert vs.list_cards(vault)[0]["single_use"] == 1

    def test_merchant_is_a_bare_host(self, vault):
        _add(vault, merchant=" Shop.Example.COM. ")
        assert vs.list_cards(vault)[0]["merchant"] == "shop.example.com"
        for bad in (
            "https://shop.example.com",
            "shop",
            "shop.example.com/pay",
            "a b.com",
            "shop.example.com\n.",
        ):
            with pytest.raises(vs.VaultError, match="bare host"):
                _add(vault, merchant=bad)

    def test_refuses_a_card_number_in_the_clear_label(self, vault):
        with pytest.raises(vs.VaultError, match="label") as exc:
            _add(vault, label=f"card {VISA}")
        assert VISA not in str(exc.value)
        assert vs.list_cards(vault) == []

    def test_refuses_a_card_number_in_the_clear_merchant(self, vault):
        with pytest.raises(vs.VaultError, match="merchant") as exc:
            _add(vault, merchant=f"{VISA}.com")
        assert VISA not in str(exc.value)

    def test_refuses_a_vault_whose_files_are_not_private(self, vault):
        os.chmod(vault / vs.CARDS, 0o644)
        with pytest.raises(vs.VaultError, match="not private"):
            _add(vault)

    @pytest.mark.parametrize("label", ["", "   ", "a\x1b[2Jb", "line\nbreak", "x" * 101])
    def test_label_must_be_short_and_printable(self, vault, label):
        with pytest.raises(vs.VaultError, match="printable"):
            _add(vault, label=label)

    @pytest.mark.parametrize("target", ["dangling", "loose file"])
    def test_a_symlinked_cards_db_is_refused(self, vault, target):
        db = vault / vs.CARDS
        db.unlink()
        if target == "loose file":
            victim = vault.parent / "victim.db"
            victim.write_bytes(b"")
            db.symlink_to(victim)
        else:
            db.symlink_to(vault / "nowhere")
        with pytest.raises(vs.VaultError):
            vs.list_cards(vault)
        with pytest.raises(vs.VaultError):
            _add(vault)

    def test_garbage_recipient_is_a_vault_error(self, vault):
        (vault / vs.RECIPIENT).write_text("not a key\n")
        with pytest.raises(vs.VaultError, match="recipient.txt"):
            _add(vault)
        assert vs.list_cards(vault) == []

    def test_formats_are_versioned(self, vault):
        _add(vault)
        with sqlite3.connect(vault / vs.CARDS) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (1,)
            (blob,) = conn.execute("SELECT blob FROM cards").fetchone()
        assert json.loads(decrypt(blob, [_identity(vault)]))["v"] == 1

    def test_a_newer_format_is_refused(self, vault):
        with sqlite3.connect(vault / vs.CARDS) as conn:
            conn.execute("PRAGMA user_version = 2")
        with pytest.raises(vs.VaultError, match="format version 2"):
            vs.list_cards(vault)

    def test_punycode_merchant(self, vault):
        _add(vault, merchant="shop.xn--p1ai")
        assert vs.list_cards(vault)[0]["merchant"] == "shop.xn--p1ai"

    def test_needs_an_initialised_vault(self, tmp_path):
        with pytest.raises(vs.VaultError, match="not initialised"):
            _add(tmp_path / "none")

    def test_the_table_refuses_an_unscoped_card(self, vault):
        _add(vault)
        with sqlite3.connect(vault / vs.CARDS) as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cards (card_ref, label, brand, last4, merchant, single_use,"
                " status, created_at, blob) VALUES ('x', 'l', 'visa', '4242', NULL, 0,"
                " 'active', 't', x'00')"
            )


class TestRetire:
    @pytest.mark.parametrize("ref", [VISA, "card_xyz", "card_" + "0" * 12])
    def test_malformed_or_unknown_ref_is_refused_without_echo(self, vault, ref):
        _add(vault)
        with pytest.raises(vs.VaultError) as exc:
            vs.retire_card(ref, PASSPHRASE, vault)
        assert VISA not in str(exc.value)

    def test_retire_refuses_a_vault_whose_files_are_not_private(self, vault):
        ref = _add(vault)
        os.chmod(vault / vs.CARDS, 0o644)
        with pytest.raises(vs.VaultError, match="not private"):
            vs.retire_card(ref, PASSPHRASE, vault)
        os.chmod(vault / vs.CARDS, 0o600)
        assert vs.list_cards(vault)[0]["status"] == "active"

    def test_wrong_passphrase_changes_nothing(self, vault):
        ref = _add(vault)
        with pytest.raises(vs.VaultError, match="wrong passphrase"):
            vs.retire_card(ref, "not it", vault)
        assert vs.list_cards(vault)[0]["status"] == "active"

    def test_retire_erases_the_blob(self, vault):
        ref = _add(vault)
        with sqlite3.connect(vault / vs.CARDS) as conn:
            (blob,) = conn.execute("SELECT blob FROM cards").fetchone()
        vs.retire_card(ref, PASSPHRASE, vault)
        [card] = vs.list_cards(vault)
        assert card["status"] == "retired" and card["retired_at"]
        with sqlite3.connect(vault / vs.CARDS) as conn:
            assert conn.execute("SELECT blob FROM cards").fetchone() == (None,)
        # secure_delete: the ciphertext does not linger in a free page.
        raw = (vault / vs.CARDS).read_bytes()
        assert blob[:64] not in raw and blob[-64:] not in raw
        with pytest.raises(vs.VaultError, match="no active card"):
            vs.retire_card(ref, PASSPHRASE, vault)


class TestStatus:
    def test_counts_and_modes(self, vault):
        _add(vault)
        status = vs.vault_status(vault)
        assert status["initialised"] and status["cards"] == {"active": 1, "retired": 0}
        assert all(f["private"] for f in status["files"].values())
        os.chmod(vault / vs.CARDS, 0o644)
        assert vs.vault_status(vault)["files"][vs.CARDS]["private"] is False

    def test_corrupt_db_is_reported_not_raised(self, vault):
        (vault / vs.CARDS).write_bytes(b"not a database" * 100)
        status = vs.vault_status(vault)
        assert status["cards"] is None and "unreadable" in status["error"]
        with pytest.raises(vs.VaultError, match="unreadable"):
            vs.list_cards(vault)

    def test_a_locked_db_says_so_rather_than_damaged(self, vault, monkeypatch):
        class Locked:
            def execute(self, *a):
                raise sqlite3.OperationalError("database is locked")

            def close(self):
                pass

        monkeypatch.setattr(vs.sqlite3, "connect", lambda *a, **k: Locked())
        with pytest.raises(vs.VaultError, match="could not be read or written .database is locked"):
            vs.list_cards(vault)

    def test_a_missing_cards_db_in_an_initialised_vault_is_data_loss(self, vault):
        (vault / vs.CARDS).unlink()  # lost, or left out of a restore
        status = vs.vault_status(vault)
        assert status["cards"] is None and "cards.db is missing" in status["error"]
        for call in (vs.list_cards, vs.check_ready):
            with pytest.raises(vs.VaultError, match="cards.db is missing"):
                call(vault)
        with pytest.raises(vs.VaultError, match="cards.db is missing"):
            _add(vault)
        assert not (vault / vs.CARDS).exists()  # never recreated empty

    def test_a_cards_table_of_another_shape_is_refused_not_queried(self, vault):
        (vault / vs.CARDS).unlink()
        with sqlite3.connect(vault / vs.CARDS) as conn:  # a cards table of another shape
            conn.execute("CREATE TABLE cards (x)")
            conn.execute(f"PRAGMA user_version = {vs.SCHEMA_VERSION}")
        os.chmod(vault / vs.CARDS, 0o600)
        with pytest.raises(vs.VaultError, match="not a vault card database"):
            vs.list_cards(vault)
        status = vs.vault_status(vault)
        assert status["cards"] is None and "not a vault card database" in status["error"]

    def test_a_constraint_error_is_not_called_damage(self):
        exc = vs._translate(sqlite3.IntegrityError("UNIQUE constraint failed"))
        assert "refused the operation" in str(exc) and "damaged" not in str(exc)

    @pytest.mark.parametrize(
        ("damage", "says"),
        [
            ("cards.db mode 000", "could not be read or written"),
            ("cards.db 0 bytes", "not a vault card database (empty)"),
            ("identity garbage", "not a whole passphrase-sealed age key"),
            ("identity mode 000", "identity.age cannot be read"),
            ("identity missing, cards remain", "identity.age is missing"),
            ("recipient missing", "incomplete"),
        ],
    )
    def test_every_damaged_state_is_an_error_and_writes_nothing(self, vault, damage, says):
        _add(vault)
        target = {"cards.db": vault / vs.CARDS, "identity": vault / vs.IDENTITY}.get(
            damage.split()[0], vault / vs.RECIPIENT
        )
        if damage.endswith("mode 000"):
            os.chmod(target, 0o000)
        elif damage.endswith("0 bytes"):
            target.write_bytes(b"")
        elif damage.endswith("garbage"):
            target.write_bytes(b"not an age file")
        else:
            target.unlink()
        before = sorted(
            (p.name, p.stat().st_size) for p in vault.iterdir() if os.access(p, os.R_OK)
        )
        try:
            status = vs.vault_status(vault)
            assert status["cards"] is None and says in status["error"], status
            with pytest.raises(vs.VaultError, match=re.escape(says)):
                vs.check_ready(vault)
            after = sorted(
                (p.name, p.stat().st_size) for p in vault.iterdir() if os.access(p, os.R_OK)
            )
            assert after == before  # status and check_ready never repair or stamp a file
        finally:
            if target.exists():
                os.chmod(target, 0o600)

    def test_an_unreadable_root_is_refused_before_any_prompt(self, vault):
        os.chmod(vault, 0o000)
        try:
            with pytest.raises(vs.VaultError, match="chmod 700"):
                vs.check_can_init(vault)
            assert "chmod 700" in vs.vault_status(vault)["error"]
        finally:
            os.chmod(vault, 0o700)

    def test_init_refuses_a_foreign_database_at_cards_db_and_leaves_it_alone(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(age_passphrase, "encrypt", lambda data, pw: FAKE_SEALED)
        path = tmp_path / "v"
        path.mkdir(mode=0o700)
        with sqlite3.connect(path / vs.CARDS) as conn:
            conn.execute("CREATE TABLE other (x)")
        before = (path / vs.CARDS).read_bytes()
        with pytest.raises(vs.VaultError, match="not an empty vault card database"):
            vs.init_vault(PASSPHRASE, path)
        assert (path / vs.CARDS).read_bytes() == before
        assert not (path / vs.IDENTITY).exists()

    @pytest.mark.parametrize("damage", [b"\xff\xfe not utf-8", b"age1 not a key", None])
    def test_a_damaged_recipient_is_refused_by_check_ready(self, vault, damage):
        if damage is None:
            os.chmod(vault / vs.RECIPIENT, 0o000)
        else:
            (vault / vs.RECIPIENT).write_bytes(damage)
        try:
            with pytest.raises(vs.VaultError, match="recipient.txt"):
                vs.check_ready(vault)
        finally:
            os.chmod(vault / vs.RECIPIENT, 0o600)

    def test_a_file_at_the_vault_path(self, tmp_path):
        path = tmp_path / "v"
        path.write_text("x")
        status = vs.vault_status(path)
        assert status["cards"] is None and "not a directory you own" in status["error"]
        with pytest.raises(vs.VaultError, match="not a directory you own"):
            vs.list_cards(path)

    def test_no_vault(self, tmp_path):
        status = vs.vault_status(tmp_path / "none")
        assert status == {
            "path": str(tmp_path / "none"),
            "initialised": False,
            "cards": {"active": 0, "retired": 0},
            "files": {},
            "error": None,
        }


def _add_visa(path, **kw):
    card = vs.parse_card(VISA, _future(), "123")
    return vs.add_card(card, label="t", merchant=None, single_use=True, path=path, **kw)


class TestRoundTwoFindings:
    def test_a_bad_checksum_is_an_error_even_without_pyrage(self, vault, monkeypatch):
        import importlib.util

        text = (vault / vs.RECIPIENT).read_text().strip()
        flipped = text[:-1] + ("q" if text[-1] != "q" else "p")
        (vault / vs.RECIPIENT).write_text(flipped + "\n")
        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda name, *a: None if name == "pyrage" else real(name)
        )
        assert "recipient.txt" in vs.vault_status(vault)["error"]

    def test_a_vault_directory_replaced_after_the_checks_is_never_written(
        self, vault, tmp_path, monkeypatch
    ):
        other = tmp_path / "other"
        shutil.copytree(vault, other)
        real = vs.check_ready

        def swap_after_check(path=None):
            real(path)
            vault.rename(tmp_path / "moved")
            other.rename(vault)

        monkeypatch.setattr(vs, "check_ready", swap_after_check)
        with pytest.raises(vs.VaultError, match="replaced"):
            _add_visa(vault)
        assert vs.list_cards(vault) == [] and vs.list_cards(tmp_path / "moved") == []

    def test_a_cards_db_swapped_for_a_symlink_at_open_is_refused(
        self, vault, tmp_path, monkeypatch
    ):
        decoy = tmp_path / "decoy.db"
        shutil.copy(vault / vs.CARDS, decoy)
        real = vs.sqlite3.connect

        def swap_then_connect(target, *a, **kw):
            if "cards.db" in str(target) and "mode=rw" in str(target):
                (vault / vs.CARDS).rename(tmp_path / "real.db")
                (vault / vs.CARDS).symlink_to(decoy)
            return real(target, *a, **kw)

        monkeypatch.setattr(vs.sqlite3, "connect", swap_then_connect)
        with pytest.raises(vs.VaultError, match="changed while it was being opened"):
            _add_visa(vault)

    def test_a_vault_directory_replaced_before_a_retire_writes_is_never_written(
        self, vault, tmp_path, monkeypatch
    ):
        ref = _add_visa(vault)
        other = tmp_path / "other"
        shutil.copytree(vault, other)
        real = vs.verify_passphrase

        def swap_after_verify(passphrase, path=None):
            real(passphrase, path)
            vault.rename(tmp_path / "moved")
            other.rename(vault)

        monkeypatch.setattr(vs, "verify_passphrase", swap_after_verify)
        with pytest.raises(vs.VaultError, match="replaced"):
            vs.retire_card(ref, PASSPHRASE, vault)
        for path in (vault, tmp_path / "moved"):
            assert [c["status"] for c in vs.list_cards(path)] == ["active"]

    def test_an_encryption_failure_is_a_vault_error(self, vault, monkeypatch):
        import pyrage

        def broken(data, recipients):
            raise RuntimeError("library failure")

        monkeypatch.setattr(pyrage, "encrypt", broken)
        with pytest.raises(vs.VaultError, match="could not be encrypted"):
            _add_visa(vault)
        assert vs.list_cards(vault) == []


class TestIdentityDigest:
    """init records sha256(identity.age); a same-length corruption is caught without the passphrase."""

    def _flip_one_payload_byte(self, vault):
        data = bytearray((vault / vs.IDENTITY).read_bytes())
        data[-5] ^= 0x01  # inside the 106-byte payload: length and shape unchanged
        (vault / vs.IDENTITY).write_bytes(bytes(data))

    def test_a_fresh_vault_records_the_digest_and_is_healthy(self, vault):
        import hashlib

        with sqlite3.connect(vault / vs.CARDS) as conn:
            (digest,) = conn.execute(
                "SELECT value FROM vault_meta WHERE key = 'identity_sha256'"
            ).fetchone()
        assert digest == hashlib.sha256((vault / vs.IDENTITY).read_bytes()).hexdigest()
        assert vs.vault_status(vault)["error"] is None

    def test_a_same_length_corruption_is_an_error_and_add_refuses(self, vault):
        self._flip_one_payload_byte(vault)
        assert "not the key this vault was created with" in vs.vault_status(vault)["error"]
        with pytest.raises(vs.VaultError, match="not the key this vault was created with"):
            _add_visa(vault)
        assert vs.list_cards(vault) == []

    def test_an_identity_from_another_vault_is_refused(self, vault, tmp_path):
        other = tmp_path / "other"
        vs.init_vault(PASSPHRASE, other)
        (vault / vs.IDENTITY).write_bytes((other / vs.IDENTITY).read_bytes())
        assert "not the key this vault was created with" in vs.vault_status(vault)["error"]

    def test_a_vault_with_no_recorded_digest_is_refused(self, vault):
        with sqlite3.connect(vault / vs.CARDS) as conn:
            conn.execute("DELETE FROM vault_meta")
        assert "records no digest" in vs.vault_status(vault)["error"]

    def test_a_cards_db_without_the_meta_table_is_foreign(self, vault):
        with sqlite3.connect(vault / vs.CARDS) as conn:
            conn.execute("DROP TABLE vault_meta")
        assert "not a vault card database" in vs.vault_status(vault)["error"]

    def test_a_swapped_recipient_is_an_error_and_add_refuses(self, vault, tmp_path):
        other = tmp_path / "other"
        vs.init_vault(PASSPHRASE, other)
        (vault / vs.RECIPIENT).write_bytes((other / vs.RECIPIENT).read_bytes())
        assert "recipient.txt is not the public key" in vs.vault_status(vault)["error"]
        with pytest.raises(vs.VaultError, match="recipient.txt is not the public key"):
            _add_visa(vault)
        assert vs.list_cards(vault) == []

    def test_a_malformed_recorded_value_is_an_error_not_a_crash(self, vault):
        with sqlite3.connect(vault / vs.CARDS) as conn:
            conn.execute(
                "UPDATE vault_meta SET value = ? WHERE key = 'identity_sha256'", ("\u00e9" * 64,)
            )
        assert "not the key this vault was created with" in vs.vault_status(vault)["error"]

    def test_a_rerun_after_a_killed_init_records_the_new_key(self, tmp_path, monkeypatch):
        """A first init killed after cards.db landed, then a rerun with a different key."""
        path = tmp_path / "v"
        real_link = os.link
        calls = {"n": 0}

        def killed_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt  # the kill, after cards.db and its digest landed
            return real_link(*args, **kwargs)

        monkeypatch.setattr(vs.os, "link", killed_once)
        with pytest.raises(KeyboardInterrupt):
            vs.init_vault(PASSPHRASE, path)
        assert not vs.is_initialised(path)
        vs.init_vault(PASSPHRASE, path)  # a new identity, so a new digest
        assert vs.vault_status(path)["error"] is None
        _add_visa(path)
        assert len(vs.list_cards(path)) == 1


class TestChecksHeldThroughTheWrite:
    """#3253 round 1: what was checked is what gets used."""

    def test_the_blob_names_its_row_and_scope(self, vault):
        ref = vs.add_card(
            vs.parse_card(VISA, _future(), "123"),
            label="t",
            merchant=None,
            single_use=True,
            path=vault,
        )
        with sqlite3.connect(vault / vs.CARDS) as conn:
            (blob,) = conn.execute("SELECT blob FROM cards").fetchone()
        secret = json.loads(decrypt(blob, [_identity(vault)]))
        assert (secret["card_ref"], secret["merchant"], secret["single_use"]) == (ref, None, True)

    def test_a_cards_db_swapped_in_after_the_checks_takes_no_card(
        self, vault, tmp_path, monkeypatch
    ):
        # Another vault's database: same shape, a different recorded key.
        decoy = tmp_path / "decoy.db"
        shutil.copy(vault / vs.CARDS, decoy)
        with sqlite3.connect(decoy) as conn:
            conn.execute(
                "UPDATE vault_meta SET value = ? WHERE key = 'recipient'",
                (str(x25519.Identity.generate().to_public()),),
            )
        real = vs._insert_card

        def swap_after_sealing(*args):
            (vault / vs.CARDS).rename(tmp_path / "moved.db")
            decoy.rename(vault / vs.CARDS)
            return real(*args)

        monkeypatch.setattr(vs, "_insert_card", swap_after_sealing)
        with pytest.raises(vs.VaultError, match="changed during the add"):
            _add_visa(vault)
        for db in (vault / vs.CARDS, tmp_path / "moved.db"):
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT COUNT(*) FROM cards").fetchone() == (0,)

    def test_a_cards_db_with_the_same_recipient_but_another_identity_takes_no_card(
        self, vault, tmp_path, monkeypatch
    ):
        decoy = tmp_path / "decoy.db"
        shutil.copy(vault / vs.CARDS, decoy)
        with sqlite3.connect(decoy) as conn:
            conn.execute(
                "UPDATE vault_meta SET value = ? WHERE key = 'identity_sha256'", ("0" * 64,)
            )
        real = vs._insert_card

        def swap_after_sealing(*args):
            (vault / vs.CARDS).rename(tmp_path / "moved.db")
            decoy.rename(vault / vs.CARDS)
            return real(*args)

        monkeypatch.setattr(vs, "_insert_card", swap_after_sealing)
        with pytest.raises(vs.VaultError, match="changed during the add"):
            _add_visa(vault)
        with sqlite3.connect(vault / vs.CARDS) as conn:
            assert conn.execute("SELECT COUNT(*) FROM cards").fetchone() == (0,)

    def test_the_init_preflight_never_opens_a_symlinked_cards_db(self, tmp_path):
        path = tmp_path / "v"
        path.mkdir(mode=0o700)
        outside = tmp_path / "outside.db"
        with sqlite3.connect(outside) as conn:  # an empty database, which would pass as empty
            conn.execute("CREATE TABLE t (x)")
            conn.execute("DROP TABLE t")
        before = (outside.read_bytes(), outside.stat().st_mtime_ns)
        (path / vs.CARDS).symlink_to(outside)
        with pytest.raises(vs.VaultError, match="not a regular file you own"):
            vs.check_can_init(path)
        assert (outside.read_bytes(), outside.stat().st_mtime_ns) == before

    @pytest.mark.parametrize(
        "extra",
        [
            "CREATE TRIGGER keep AFTER UPDATE ON cards BEGIN SELECT 1; END",
            "CREATE VIEW v AS SELECT card_ref FROM cards",
            "CREATE INDEX by_label ON cards (label)",
        ],
        ids=["trigger", "view", "explicit index"],
    )
    def test_a_vault_database_with_extra_objects_is_foreign(self, vault, extra):
        with sqlite3.connect(vault / vs.CARDS) as conn:
            conn.execute(extra)
        assert "unexpected tables, indexes, views or triggers" in vs.vault_status(vault)["error"]
        with pytest.raises(vs.VaultError, match="not a vault card database"):
            _add_visa(vault)

    def test_the_init_preflight_refuses_a_cards_db_swapped_at_connect(self, tmp_path, monkeypatch):
        path = tmp_path / "v"
        path.mkdir(mode=0o700)
        with sqlite3.connect(path / vs.CARDS) as conn:  # an empty database
            conn.execute("CREATE TABLE t (x)")
            conn.execute("DROP TABLE t")
        real = vs.sqlite3.connect

        def swap_then_connect(target, *a, **kw):
            if "mode=rw" in str(target):
                (path / vs.CARDS).rename(tmp_path / "moved.db")
                shutil.copy(tmp_path / "moved.db", path / vs.CARDS)
            return real(target, *a, **kw)

        monkeypatch.setattr(vs.sqlite3, "connect", swap_then_connect)
        with pytest.raises(vs.VaultError, match="changed while it was being opened"):
            vs.check_can_init(path)

    @pytest.mark.parametrize("op", ["add", "retire"])
    def test_a_write_holds_its_transaction_from_the_checks_to_the_statement(
        self, vault, monkeypatch, op
    ):
        """Another writer cannot add a trigger or rewrite the binding after the checks."""
        ref = _add_visa(vault)
        target = "_insert_card" if op == "add" else "_retire_row"
        real_write, real_shape = getattr(vs, target), vs._shape
        armed, seen = {"on": False}, []

        def write_armed(*args):
            armed["on"] = True
            return real_write(*args)

        def shape_then_meddle(conn):
            shape = real_shape(conn)
            if armed["on"]:
                armed["on"] = False
                try:
                    with sqlite3.connect(vault / vs.CARDS, timeout=0.1) as other:
                        other.execute(
                            "CREATE TRIGGER keep AFTER UPDATE ON cards BEGIN SELECT 1; END"
                        )
                    seen.append("changed")
                except sqlite3.OperationalError as exc:
                    seen.append(str(exc))
            return shape

        monkeypatch.setattr(vs, target, write_armed)
        monkeypatch.setattr(vs, "_shape", shape_then_meddle)
        monkeypatch.setattr(vs, "verify_passphrase", lambda pw, path=None: None)
        if op == "add":
            _add_visa(vault)
        else:
            vs.retire_card(ref, PASSPHRASE, vault)
        assert seen == ["database is locked"]

    def test_init_refuses_a_cards_db_swapped_before_sqlite_opens_it(self, tmp_path, monkeypatch):
        path = tmp_path / "v"
        decoy = tmp_path / "decoy.db"
        real = vs.sqlite3.connect

        def swap_then_connect(target, *a, **kw):
            if str(target).startswith("/proc/self/fd/") and str(target).endswith(vs.CARDS):
                decoy.write_bytes(b"")
                decoy.rename(path / vs.CARDS)
            return real(target, *a, **kw)

        monkeypatch.setattr(vs.sqlite3, "connect", swap_then_connect)
        with pytest.raises(vs.VaultError, match="changed while init was opening it"):
            vs.init_vault(PASSPHRASE, path)
        assert not vs.is_initialised(path)
        assert (path / vs.CARDS).stat().st_size == 0  # nothing was written into the swap

    def test_a_damaged_tail_past_the_read_cap_is_refused_not_cut_off(self, vault):
        """A prefix that is a valid key must not pass for the whole file."""
        text = (vault / vs.RECIPIENT).read_text().strip()
        (vault / vs.RECIPIENT).write_text(text + " " * 5000 + "DAMAGED TAIL")
        assert "larger than a vault file" in vs.vault_status(vault)["error"]
        with pytest.raises(vs.VaultError, match="larger than a vault file"):
            _add_visa(vault)

    def test_an_oversized_recipient_file_is_refused_without_loading_it(self, vault):
        import tracemalloc

        text = (vault / vs.RECIPIENT).read_text().strip()
        (vault / vs.RECIPIENT).write_text(text + "\n" + "x" * (16 << 20))
        tracemalloc.start()
        try:
            assert "recipient.txt" in vs.vault_status(vault)["error"]
            with pytest.raises(vs.VaultError, match="recipient.txt"):
                _add_visa(vault)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < (4 << 20)  # a whole-file read would hold 16 MiB at once
