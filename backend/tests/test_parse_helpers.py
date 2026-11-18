"""Pure parsing helpers: no database needed."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from vct.pipeline.parse import (
    VetoStep,
    best_of_from_score,
    find_patch,
    is_tier1_event,
    parse_duration,
    parse_utc_timestamp,
    parse_veto_text,
    to_decimal,
    to_int,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("13", 13), (13, 13), ("+3", 3), ("74%", 74), ("", None), (None, None), ("TBD", None)],
)
def test_to_int(value: object, expected: int | None) -> None:
    assert to_int(value) == expected


def test_to_decimal() -> None:
    assert to_decimal("1.23") == Decimal("1.23")
    assert to_decimal("74%") == Decimal("74")
    assert to_decimal("") is None
    assert to_decimal("n/a") is None


def test_parse_duration() -> None:
    assert parse_duration("41:07") == 41 * 60 + 7
    assert parse_duration("1:02:33") == 3753
    assert parse_duration("") is None
    assert parse_duration("soon") is None


def test_parse_utc_timestamp() -> None:
    assert parse_utc_timestamp("2026-03-10 18:00:00") == datetime(2026, 3, 10, 18, tzinfo=UTC)
    assert parse_utc_timestamp("") is None


def test_find_patch() -> None:
    assert find_patch("Tuesday, March 10th 6:00 PM PDT Patch 12.04") == "12.04"
    assert find_patch("", None, "no patch here") is None


@pytest.mark.parametrize(
    ("s1", "s2", "bo"), [(1, 0, 1), (2, 0, 3), (1, 2, 3), (3, 2, 5), (0, 0, None), (None, 1, None)]
)
def test_best_of_from_score(s1: int | None, s2: int | None, bo: int | None) -> None:
    assert best_of_from_score(s1, s2) == bo


def test_parse_veto_text() -> None:
    steps = parse_veto_text("ALP ban Bind; BRV pick Lotus; 100 Thieves ban Split; Sunset remains")
    assert steps == [
        VetoStep("ALP", "ban", "Bind"),
        VetoStep("BRV", "pick", "Lotus"),
        VetoStep("100 Thieves", "ban", "Split"),
        VetoStep(None, "decider", "Sunset"),
    ]
    assert parse_veto_text(None) == []


@pytest.mark.parametrize(
    ("name", "tier1"),
    [
        ("Champions Tour 2024: Americas Stage 1", True),
        ("Champions Tour 2024: Masters Madrid", True),
        ("Champions Tour 2023: Champions Los Angeles", True),
        ("VCT 2026: EMEA Kickoff", True),
        ("Valorant Champions 2025", True),
        ("Valorant Masters Toronto 2025", True),
        ("Challengers 2026: North America Stage 1", False),
        ("Champions Tour 2024: Americas Ascension", False),
        ("Game Changers 2025: Championship", False),
        ("Red Bull Home Ground #5", False),
    ],
)
def test_is_tier1_event(name: str, tier1: bool) -> None:
    assert is_tier1_event(name) is tier1
