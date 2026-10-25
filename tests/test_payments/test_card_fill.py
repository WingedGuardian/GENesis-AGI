"""browser_fill_card's rules (genesis.payments.card_fill) and its vault client.

No browser here: argument checks, the amount match, the values typed, the
handover filter, the config switch, and the release client's one-line protocol
against a real unix socket. The only card number is Stripe's public test Visa.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from genesis import _config_overlay
from genesis.payments import card_fill as cf
from genesis.payments import release_client as rc

PAN = "4242424242424242"
REF = "card_" + "0a" * 6
FIELDS = {"number": "#n", "expiry": {"selector": "#e", "frames": ["iframe"]}}


def _v(**over):
    kw = dict(
        card_ref=REF,
        amount="$19.99",
        fields=FIELDS,
        pay="#pay",
        expiry_format="MM/YY",
        extra_fields=None,
    )
    kw.update(over)
    return cf.validate(**kw)


def test_validate_keeps_kinds_and_extras_in_their_fixed_order():
    a = _v(
        fields={"cvc": "#c", **FIELDS},
        extra_fields={
            "city": {"selector": "#ci", "value": "Paris"},
            "email": {"selector": "#m", "value": "a@b.c"},
        },
    )
    assert list(a.fields) == ["number", "expiry", "cvc"]
    assert a.fields["expiry"] == (("iframe",), "#e")
    assert a.pay == ((), "#pay")
    assert list(a.extra) == ["email", "city"]
    assert "a@b.c" not in repr(a) and "Paris" not in repr(a)


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"card_ref": "card_xyz"}, "bad_card_ref"),
        ({"amount": ""}, "bad_amount"),
        ({"amount": "order 123456789012"}, "bad_amount"),
        # No number at all: generic page text would pass as "the total".
        ({"amount": "Total"}, "bad_amount"),
        ({"amount": "USD"}, "bad_amount"),
        ({"amount": "$"}, "bad_amount"),
        ({"expiry_format": "YY/MM"}, "bad_expiry_format"),
        ({"fields": {"cvc": "#c"}}, "bad_fields"),
        ({"fields": {"number": "#n", "exp_month": "#m"}}, "bad_fields"),
        (
            {"fields": {"number": "#n", "expiry": "#e", "exp_month": "#m", "exp_year": "#y"}},
            "bad_fields",
        ),
        ({"fields": {"number": f"#{PAN}"}}, "bad_fields"),
        ({"fields": {"number": {"selector": "#n", "frames": ["f"] * 5}}}, "bad_fields"),
        ({"fields": {"number": {"selector": "#n", "x": 1}}}, "bad_fields"),
        ({"pay": None}, "bad_pay"),
        ({"pay": {"selector": ""}}, "bad_pay"),
        ({"extra_fields": {"shoe": {"selector": "#s", "value": "x"}}}, "bad_extra_fields"),
        (
            {"extra_fields": {"email": {"selector": "#s"}}},
            "no_billing_value",
        ),  # no value and no billing config
        (
            {"extra_fields": {"phone": {"selector": "#s", "value": "+1 555 0100 1234 5678"}}},
            "bad_extra_fields",
        ),
        (
            {"extra_fields": {"company": {"selector": "#s", "value": f"x {PAN}"}}},
            "bad_extra_fields",
        ),
        ({"extra_fields": {"city": {"selector": "#s", "value": "a\nb"}}}, "bad_extra_fields"),
        (
            {
                "fields": {"number": "#n", "name": "#nm"},
                "extra_fields": {"name": {"selector": "#s", "value": "x"}},
            },
            "bad_extra_fields",
        ),
    ],
)
def test_validate_refuses_with_a_fixed_code(over, code):
    with pytest.raises(cf.Refused) as e:
        _v(**over)
    assert e.value.code == code
    assert PAN not in str(e.value)


# Playwright selector syntax that can step into another frame, or into any
# engine other than plain CSS, is refused wherever a selector is taken: a
# field, a frame in a chain, Pay, an extra field. Plain CSS stays accepted.
_ENTER_FRAME = "iframe#x >> internal:control=enter-frame >> #num"


@pytest.mark.parametrize(
    "sel",
    [
        _ENTER_FRAME,
        "#a >> #b",
        "internal:control=enter-frame",
        "frame=card >> #num",
        "css=#num",
        "xpath=//input",
        "text=Pay",
        " internal:role=button",
        # Playwright's own part grammar: spaces around "=", a capture "*",
        # implicit XPath, and implicit (quoted) text.
        "xpath =//iframe",
        "internal:chain =x",
        "*css=#num",
        "//input",
        "(//input)[1]",
        "..",
        '"Pay"',
        "'Pay'",
        # JS trim() strips a BOM that Python's strip() keeps.
        "\ufeffxpath=//input",
        "\ufeff//input",
    ],
)
@pytest.mark.parametrize("where", ["field", "frame", "pay", "extra"])
def test_a_selector_that_is_not_plain_css_is_refused(sel, where):
    over, code = {
        "field": ({"fields": {"number": sel}}, "bad_fields"),
        "frame": ({"fields": {"number": {"selector": "#n", "frames": [sel]}}}, "bad_fields"),
        "pay": ({"pay": sel}, "bad_pay"),
        "extra": (
            {"extra_fields": {"email": {"selector": sel, "value": "a@b.c"}}},
            "bad_extra_fields",
        ),
    }[where]
    with pytest.raises(cf.Refused) as e:
        _v(**over)
    assert e.value.code == code


@pytest.mark.parametrize(
    "sel",
    [
        "#cardNumber",
        "iframe[src*='checkout-inner']",
        "[data-frame=card]",
        "input[name=x]",
        "a > b",
        "x:has(> y)",
        "*",
        "* > input",
    ],
)
def test_plain_css_selectors_are_accepted(sel):
    a = _v(fields={"number": sel, "expiry": "#e", "cvc": "#c"})
    assert a.fields["number"] == ((), sel)


@pytest.mark.parametrize(
    ("amount", "text", "found"),
    [
        ("$19.99", "Total $19.99 due", True),
        ("19.99", "Total 19.99", True),
        ("€ 1 234,50", "Summe: € 1 234,50", True),
        ("19.99", "Total 119.99", False),
        ("19.99", "Total 19.990", False),
        ("19.99", "Total 1,19.99", False),
        ("19.99", "Total 19.99.", True),
        ("19.99", "no total", False),
        (" ", "anything", False),
    ],
)
def test_amount_on_page(amount, text, found):
    assert cf.amount_on_page(amount, text) is found


def test_values_for_names_only_the_tapped_kinds():
    card = {
        "number": PAN,
        "exp_month": 3,
        "exp_year": 2031,
        "cvc": "123",
        "label": "x",
        "last4": "4242",
    }
    assert cf.values_for(card, ["number", "expiry"], "MM/YY") == {"number": PAN, "expiry": "03/31"}
    assert cf.values_for(card, ["expiry"], "MM/YYYY") == {"expiry": "03/2031"}
    assert cf.values_for(card, ["expiry"], "MMYY") == {"expiry": "0331"}
    assert cf.values_for(card, ["exp_month", "exp_year"], "MM/YYYY") == {
        "exp_month": "03",
        "exp_year": "2031",
    }
    assert cf.values_for(card, ["exp_year"], "MM/YY") == {"exp_year": "31"}
    assert cf.reply_keys(["number", "expiry", "cvc"]) == {
        "number",
        "exp_month",
        "exp_year",
        "cvc",
        "label",
        "last4",
    }


@pytest.mark.parametrize(
    ("kind", "sent", "got", "ok"),
    [
        ("number", PAN, "4242 4242 4242 4242", True),
        ("number", PAN, "4242", False),
        ("exp_month", "03", "3", True),
        ("exp_year", "31", "2031", True),
        ("exp_year", "31", "2032", False),
        ("name", "Ab Cd", " ab cd ", True),
        ("cvc", "123", None, False),
        ("cvc", "123", "abc123", False),
        ("number", PAN, "4242-4242-4242-4242", True),
        ("expiry", "03/31", "03 / 31", True),
        ("expiry", "03/31", "0x3/31", False),
        ("exp_month", "01", "101", False),
        ("exp_month", "01", "01", True),
        ("exp_year", "2034", "2134", False),
        ("exp_year", "2034", "34", True),
        ("exp_year", "34", "2034", True),
    ],
)
def test_value_landed(kind, sent, got, ok):
    assert cf.value_landed(kind, sent, got) is ok


def test_filter_state_keeps_the_merchant_site_and_card_frames_only():
    state = {
        "cookies": [
            {"name": "a", "domain": ".example.com"},
            {"name": "b", "domain": "shop.example.com"},
            {"name": "c", "domain": "js.pay.example.net"},
            {"name": "d", "domain": ".pay.example.net"},
            {"name": "e", "domain": "other.example.net"},
            {"name": "f", "domain": "example.com.evil.org"},
            {"name": "g", "domain": ""},
        ],
        "origins": [
            {"origin": "https://shop.example.com"},
            {"origin": "https://cdn.example.com"},
            {"origin": "https://js.pay.example.net"},
            {"origin": "https://pay.example.net"},
            {"origin": "https://evil.org"},
        ],
    }
    out = cf.filter_state(state, "example.com", {"https://js.pay.example.net"})
    assert [c["name"] for c in out["cookies"]] == ["a", "b", "c", "d"]
    assert [o["origin"] for o in out["origins"]] == [
        "https://shop.example.com",
        "https://cdn.example.com",
        "https://js.pay.example.net",
    ]


def test_filter_state_by_ip_host_is_exact():
    state = {
        "cookies": [{"name": "a", "domain": "192.0.2.5"}, {"name": "b", "domain": "2.5"}],
        "origins": [],
    }
    assert [c["name"] for c in cf.filter_state(state, "192.0.2.5", set())["cookies"]] == ["a"]


def test_registrable_domain_uses_the_bundled_suffix_list():
    from genesis.browser.profile import registrable_domain

    assert registrable_domain("a.shop.example.co.uk") == "example.co.uk"
    assert registrable_domain("me.github.io") == "me.github.io"
    assert registrable_domain("127.0.0.1") == "127.0.0.1"
    assert registrable_domain("localhost") == "localhost"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "payments.yaml").write_text("card_fill:\n  enabled: false\n")
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("GENESIS_CARD_FILL_DISABLED", raising=False)
    user = _config_overlay._user_config_dir()
    user.mkdir(parents=True, exist_ok=True)
    return user / "payments.local.yaml"


def test_card_fill_is_off_by_default_and_honours_the_overlay_key(repo, monkeypatch):
    assert cf.card_fill_enabled() is False
    repo.write_text("card_fill:\n  enabled: true\n")  # the key the test harness writes
    assert cf.card_fill_enabled() is True
    monkeypatch.setenv("GENESIS_CARD_FILL_DISABLED", "1")
    assert cf.card_fill_enabled() is False


@pytest.mark.parametrize(
    "text", ["card_fill:\n  enabled: 'true'\n", "card_fill: true\n", "- x\n", "card_fill: [\n"]
)
def test_card_fill_is_off_unless_exactly_true(repo, text):
    repo.write_text(text)
    assert cf.card_fill_enabled() is False


@pytest.mark.parametrize(
    ("env", "on"),
    [
        ({}, False),
        ({"DEBUG": "pw:*"}, True),
        ({"DEBUG": "*"}, True),
        ({"DEBUG": "-pw:*,app"}, False),
        ({"DEBUG": "pw:channel"}, True),
        ({"DEBUG": "pw:server"}, True),
        ({"DEBUG": "pw:channel:command"}, True),
        ({"DEBUG": "p*"}, True),
        ({"DEBUG": "*:server*"}, True),  # matches pw:server:channel
        ({"DEBUG": "*:channel"}, True),
        ({"DEBUG": "app:*"}, True),  # any wildcard: the namespace list is not closed
        ({"DEBUG": "?w:api"}, True),
        ({"DEBUG": "app,other"}, False),
        ({"PWDEBUG": "0"}, True),
        # Firefox's own logging: HTTP modules record what a page sends.
        ({"MOZ_LOG": "nsHttp:5"}, True),
        ({"MOZ_LOG_FILE": "/x/log"}, True),
        ({"MOZ_LOG": ""}, True),
        # The older names Firefox still reads when the current ones are unset.
        ({"MOZ_LOG_MODULES": "nsHttp:5"}, True),
        ({"NSPR_LOG_MODULES": "nsHttp:5"}, True),
        ({"NSPR_LOG_FILE": "/x/log"}, True),
    ],
)
def test_playwright_debug_on(monkeypatch, env, on):
    for v in ("DEBUG", "DEBUGP", "PWDEBUG", "DEBUG_FILE", *cf._FIREFOX_LOG_VARS):
        monkeypatch.delenv(v, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cf.playwright_debug_on() is on


def test_result_passes_only_fixed_parts():
    out = cf.result(
        "failed_before_pay", "h", field="cvc", filled=["number", PAN], reason=f"x {PAN}"
    )
    assert out == {
        "code": "failed_before_pay",
        "status": cf.TEXTS["failed_before_pay"],
        "host": "h",
        "field": "cvc",
        "filled": ["number"],
    }
    assert cf.result("field_mismatch:cvc")["status"] == cf.TEXTS["field_mismatch"]
    assert cf.result("no_such_code")["status"] == cf.TEXTS["internal_error"]
    assert (
        cf.result("release_refused", reason="no such active card")["reason"]
        == "no such active card"
    )


# ---------------------------------------------------------------------------
# release_client: one JSON line each way, the write side never half-closed
# ---------------------------------------------------------------------------


async def test_every_op_keeps_its_write_side_open_until_the_reply(tmp_path, monkeypatch):
    seen: list[tuple[dict, bool]] = []

    async def handle(reader, writer):
        req = json.loads(await reader.readline())
        await asyncio.sleep(0.05)
        seen.append((req, reader.at_eof()))
        writer.write(b'{"ok": true, "nonce": "n"}\n')
        await writer.drain()
        writer.close()

    path = tmp_path / "s.sock"
    server = await asyncio.start_unix_server(handle, path=str(path))
    monkeypatch.setattr(rc, "vault_socket_path", lambda: path)
    try:
        await rc.request_release(
            REF,
            "shop.example.com",
            "T",
            "$1",
            "a" * 64,
            {"number": "https://x.example"},
            "Pay",
            ["email"],
        )
        await rc.release_status("n")
        await rc.claim("n", "s")
        await rc.cancel("n")
    finally:
        server.close()
    assert [r["op"] for r, _ in seen] == [
        "release_request",
        "release_status",
        "release_claim",
        "release_cancel",
    ]
    assert all(eof is False for _, eof in seen)
    assert seen[0][0]["button_label"] == "Pay" and seen[0][0]["also_filled"] == ["email"]


async def test_a_bad_or_missing_reply_is_a_fixed_code(tmp_path, monkeypatch):
    async def handle(reader, writer):
        await reader.readline()
        writer.write(f"not json {PAN}\n".encode())
        await writer.drain()
        writer.close()

    path = tmp_path / "s.sock"
    server = await asyncio.start_unix_server(handle, path=str(path))
    monkeypatch.setattr(rc, "vault_socket_path", lambda: path)
    try:
        with pytest.raises(rc.ReleaseClientError) as e:
            await rc.claim("n", "s")
    finally:
        server.close()
    assert e.value.code == "bad_reply" and e.value.__context__ is None
    monkeypatch.setattr(rc, "vault_socket_path", lambda: tmp_path / "absent.sock")
    with pytest.raises(rc.ReleaseClientError) as e:
        await rc.release_status("n")
    assert e.value.code == "vault_unavailable"


@pytest.mark.parametrize(
    ("origin", "ok"),
    [
        ("https://shop.example.com", True),
        ("https://shop.example.com:8443", True),
        ("https://xn--bcher-kva.example", True),
        ("https://shop.example.com.", True),  # the vault strips a trailing dot too
        ("http://shop.example.com", False),
        # Hosts the vault's release refuses ("bad host") are refused here first.
        ("http://127.0.0.1:8000", False),
        ("http://localhost", False),
        ("http://app.localhost:3000", False),
        ("https://localhost", False),
        ("https://127.0.0.1", False),
        ("https://[::1]", False),
        ("https://intranet", False),
        ("http://localhost.example.com", False),
        (None, False),
    ],
)
def test_secure_origin(origin, ok):
    assert cf.secure_origin(origin) is ok


@pytest.mark.parametrize(
    "origin",
    [
        "https://shop.example.com",
        "https://shop.example.com.",
        "https://xn--bcher-kva.example",
        "https://127.0.0.1",
    ],
)
def test_secure_origin_agrees_with_the_vaults_host_rule(origin):
    from genesis.payments import vault_store

    host = origin.split("://", 1)[1]
    try:
        vault_ok = bool(vault_store.normalise_merchant(host))
    except vault_store.VaultError:
        vault_ok = False
    assert cf.secure_origin(origin) is vault_ok


async def test_a_claim_waits_past_the_ordinary_op_timeout(tmp_path, monkeypatch):
    async def handle(reader, writer):
        await reader.readline()
        await asyncio.sleep(0.3)  # a claim's audit row behind a busy database
        writer.write(b'{"ok": true}\n')
        await writer.drain()
        writer.close()

    path = tmp_path / "s.sock"
    server = await asyncio.start_unix_server(handle, path=str(path))
    monkeypatch.setattr(rc, "vault_socket_path", lambda: path)
    monkeypatch.setattr(rc, "_OP_TIMEOUT_S", 0.1)
    monkeypatch.setattr(rc, "_CLAIM_TIMEOUT_S", 5.0)
    try:
        assert (await rc.claim("n", "s"))["ok"] is True
        with pytest.raises(rc.ReleaseClientError):
            await rc.release_status("n")
    finally:
        server.close()


def test_card_fill_prefs_turn_the_accessibility_tree_off():
    # Owner ruling Q14: a same-user AT-SPI client read the typed card number
    # through the accessibility tree; the pref stops Camoufox exposing it.
    assert cf.CARD_FILL_PREFS["accessibility.force_disabled"] == 1


def test_steps_and_consent_are_kept_in_order_as_specs():
    a = _v(
        steps=["#cart", {"selector": "#add", "frames": ["iframe#p"]}],
        consent=[{"selector": "#agree", "frames": ["iframe"]}],
    )
    assert a.steps == (((), "#cart"), (("iframe#p",), "#add"))
    assert a.consent == ((("iframe",), "#agree"),)
    assert _v().steps == () and _v().consent == ()


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"steps": "#cart"}, "bad_steps"),
        ({"steps": ["#a"] * (cf.MAX_STEPS + 1)}, "bad_steps"),
        ({"steps": ["xpath=//button"]}, "bad_steps"),
        ({"steps": [{"selector": "#a", "value": "x"}]}, "bad_steps"),
        ({"consent": ["#a", "#b", "#c"]}, "bad_consent"),
        ({"consent": [{"selector": "#a", "frames": "iframe"}]}, "bad_consent"),
        ({"fields": {"number": "#n", "first_name": "#f"}}, "bad_fields"),
        ({"fields": {"number": "#n", "last_name": "#l"}}, "bad_fields"),
        (
            {"extra_fields": {"city": {"selector": "#c", "frames": ["x"], "value": "A", "y": 1}}},
            "bad_extra_fields",
        ),
        ({"extra_fields": {"city": {"value": "A"}}}, "bad_extra_fields"),
    ],
)
def test_new_arguments_refuse_with_a_fixed_code(over, code):
    with pytest.raises(cf.Refused) as e:
        _v(**over)
    assert e.value.code == code


def test_a_framed_extra_field_keeps_its_frames():
    a = _v(
        extra_fields={"city": {"selector": "#c", "frames": ["iframe#card"], "value": "Testville"}}
    )
    assert a.extra["city"] == ((("iframe#card",), "#c"), "Testville")


def test_split_name_and_the_values_typed_for_the_name_kinds():
    assert cf.split_name("Ada Q Example") == ("Ada Q", "Example")
    assert cf.split_name("Single") == ("", "Single")
    assert cf.split_name("  ") == ("", "")
    card = {"name": "Ada Q Example"}
    assert cf.values_for(card, ["first_name", "last_name"], "MM/YY") == {
        "first_name": "Ada Q",
        "last_name": "Example",
    }
    assert cf.reply_keys(["first_name", "last_name"]) == {"label", "last4", "name"}


def test_an_extra_field_without_a_value_takes_it_from_the_billing_overlay(repo):
    repo.write_text("card_fill:\n  billing:\n    city: Testville\n    region: TS\n")
    a = _v(extra_fields={"city": {"selector": "#c"}, "region": {"selector": "#r", "value": "XX"}})
    assert a.extra["city"][1] == "Testville"
    assert a.extra["region"][1] == "XX"  # a given value wins
    with pytest.raises(cf.Refused) as e:
        _v(extra_fields={"postal_code": {"selector": "#p"}})
    assert e.value.code == "no_billing_value"


def test_a_billing_value_still_passes_the_value_checks(repo):
    repo.write_text(f"card_fill:\n  billing:\n    company: '{PAN}'\n")
    with pytest.raises(cf.Refused) as e:
        _v(extra_fields={"company": {"selector": "#c"}})
    assert e.value.code == "bad_extra_fields"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"amount_max": "$25.00"}, "bad_amount_max"),  # needs save
        ({"amount_max": "$10.00", "save": "#s"}, "bad_amount_max"),  # below the subtotal
        ({"amount_max": "lots", "save": "#s"}, "bad_amount_max"),
        ({"total": "#t"}, "bad_save"),  # needs save
    ],
)
def test_the_ceiling_arguments_refuse_with_a_fixed_code(over, code):
    with pytest.raises(cf.Refused) as e:
        _v(**over)
    assert e.value.code == code


def test_a_ceiling_with_save_is_kept():
    a = _v(save={"selector": "#s", "frames": ["iframe"]}, amount_max="$25.00", total="#t")
    assert a.save == (("iframe",), "#s") and a.amount_max == "$25.00" and a.total == ((), "#t")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Pay $123.45", "123.45"),
        ("$1,123.45", "1123.45"),
        ("Total 12", "12.00"),
        ("Pay $--.--", None),
        ("$3.00 and $5.00", None),
        ("123.4", None),
        ("-$25.00", None),  # a negative amount is never a total
        ("$-25.00", None),
        ("$ \u221225.00", None),
        ("1" * 300 + ".11", None),  # too long to be a price
        ("1 234,56 \u20ac", None),  # space-grouped digits are refused, never misread
        ("1\u00a0234,56 \u20ac", None),
        (None, None),
    ],
)
def test_money_reads_one_price(text, value):
    got = cf.money(text)
    assert (str(got) if got is not None else None) == value


def test_label_shape_masks_only_the_price():
    assert cf.label_shape("Pay $--.--") == cf.label_shape("Pay $123.45")
    assert cf.label_shape("Pay $123.45") != cf.label_shape("Buy now $123.45")


def test_save_needs_an_amount_the_total_can_be_checked_against():
    with pytest.raises(cf.Refused) as e:
        _v(amount="19,99 EUR", save="#s")
    assert e.value.code == "bad_amount"


def test_a_long_digit_run_is_refused_without_scanning_it():
    import time

    start = time.monotonic()
    assert cf.money("1" * 200_000 + ".1") is None
    assert time.monotonic() - start < 0.5


def test_a_price_just_under_the_cap_is_still_read():
    text = " " * (cf.MONEY_MAX - 6) + "$19.99"
    assert len(text) == cf.MONEY_MAX and str(cf.money(text)) == "19.99"


def test_currency_marks():
    assert cf.currency("Pay $123.45") == {"$"}
    assert cf.currency("123.45 EUR") == {"EUR"}
    assert cf.currency("123.45") == frozenset()
    assert cf.currency(None) == frozenset()


def test_label_shape_keeps_the_currency_symbol():
    assert cf.label_shape("Pay $123.45") != cf.label_shape("Pay \u20ac123.45")


def test_save_needs_the_amount_with_its_currency():
    with pytest.raises(cf.Refused) as e:
        _v(amount="19.99", save="#s")
    assert e.value.code == "bad_amount"


def test_a_ceiling_in_another_currency_is_refused():
    with pytest.raises(cf.Refused) as e:
        _v(save="#s", amount_max="\u20ac25.00")
    assert e.value.code == "bad_amount_max"

