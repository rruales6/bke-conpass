"""Seasonal campaign passes (Phase 14): rows, labels, dates, and Google's validity window."""
from __future__ import annotations

import json
import re

import pytest
from conpass_common.providers.google_wallet import GoogleWalletProvider, date_range
from conpass_common.providers.wallet import PassContent, build_pass_content


def _provider() -> GoogleWalletProvider:
    g = GoogleWalletProvider.__new__(GoogleWalletProvider)
    g._issuer_id = "ISSUER"
    return g


def _content(**over) -> PassContent:
    base = dict(
        card_id="c1", program_id="p1", merchant_id="m1", program_type="seasonal_discount",
        program_name="Mamá merece más", merchant_name="Café Vecino", holder_name="María T.",
        opaque_token="tok", rewards_available=1, reward_text="en toda la carta",
        occasion="Día de la Madre", discount_percent=20,
        valid_from="2026-05-01", valid_until="2026-05-12", language="es",
    )
    base.update(over)
    return PassContent(**base)


def _referenced(rows: list[dict]) -> set[str]:
    return set(re.findall(r"textModulesData\['(\w+)'\]", json.dumps(rows)))


@pytest.mark.parametrize("over", [
    {}, {"discount_percent": None, "reward_text": "2x1 en postres"},
    {"reward_text": None}, {"holder_name": None},
])
def test_every_template_cell_is_bound(over):
    g, c = _provider(), _content(**over)
    ids = {m["id"] for m in g._text_modules(c)}
    assert _referenced(g._card_rows(c)) == ids


def test_front_rows_and_labels_es():
    g = _provider()
    mods = {m["id"]: (m["header"], m["body"]) for m in g._text_modules(_content())}
    assert mods["hdr1"] == ("DESCUENTO", "20 %")
    assert mods["hdr2"] == ("VÁLIDA", "1–12 may")
    assert mods["hdr3"] == ("ESTADO", "Disponible")
    assert mods["sec0"] == ("BENEFICIO", "en toda la carta")


def test_text_only_offer_drops_the_percent_cell():
    g = _provider()
    c = _content(discount_percent=None, reward_text="2x1 en postres")
    assert "twoItems" in g._card_rows(c)[0]
    assert "hdr1" not in {m["id"] for m in g._text_modules(c)}


def test_used_after_one_redemption():
    g = _provider()
    mods = {m["id"]: m["body"] for m in g._text_modules(_content(rewards_redeemed=1))}
    assert mods["hdr3"] == "Usado"


def test_english_labels():
    g = _provider()
    mods = {m["id"]: (m["header"], m["body"])
            for m in g._text_modules(_content(language="en"))}
    assert mods["hdr1"] == ("DISCOUNT", "20%")
    assert mods["hdr3"] == ("STATUS", "Available")
    assert mods["sec0"][0] == "OFFER"


def test_object_kicker_and_validity_window():
    obj = _provider()._object_payload(_content())
    assert obj["subheader"]["defaultValue"]["value"] == "Campaña de temporada · Día de la Madre"
    assert obj["validTimeInterval"] == {
        "start": {"date": "2026-05-01T00:00:00-05:00"},
        "end": {"date": "2026-05-12T23:59:59-05:00"},
    }


def test_non_seasonal_pass_has_no_validity_window():
    obj = _provider()._object_payload(_content(program_type="loyalty_stamps",
                                               stamps_for_reward=8))
    assert "validTimeInterval" not in obj
    assert obj["subheader"]["defaultValue"]["value"] == "Tarjeta de fidelización"


@pytest.mark.parametrize("start,end,lang,expected", [
    ("2026-05-01", "2026-05-12", "es", "1–12 may"),
    ("2026-05-10", "2026-05-10", "es", "10 may"),
    ("2026-04-28", "2026-05-12", "es", "28 abr – 12 may"),
    ("2026-12-28", "2027-01-03", "en", "28 Dec – 3 Jan"),
    (None, "2026-05-12", "es", None),
])
def test_date_range(start, end, lang, expected):
    assert date_range(start, end, lang) == expected


def test_build_pass_content_maps_seasonal_columns():
    from datetime import date
    card = {"id": "c1", "merchant_id": "m1", "opaque_token": "t", "rewards_available": 1}
    program = {"id": "p1", "type": "seasonal_discount", "name": "X", "reward": "2x1",
               "occasion": "Navidad", "discount_percent": 15,
               "valid_from": date(2026, 12, 20), "valid_until": "2026-12-26"}
    c = build_pass_content(card, program, {"business_name": "Café"})
    assert (c.occasion, c.discount_percent, c.valid_from, c.valid_until) == (
        "Navidad", 15, "2026-12-20", "2026-12-26")
