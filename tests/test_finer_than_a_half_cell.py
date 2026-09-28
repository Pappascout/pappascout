"""Story 4.20: places finer than a half-cell -- fractions, crossings, height
bands and nested names -- and the one Inferno table three splits read.

The model is checked on small hand-written splits whose answers can be read
off the text; the shipped table is checked against his answers of
2026-09-28 (``puoliruudut-vastaus-2-2026-09-28.md``) where a test can hold
it, and its height bands against the archive (``-m archive``), where each
band was measured. The quotes-to-regions check of every shipped part lives
with the other splits' in ``tests/test_cell_split.py``
(``_check_his_words``).
"""

from __future__ import annotations

import re
from math import ceil
from pathlib import Path

import polars as pl
import pytest

from conftest import REAL_SETTINGS, require_parsed
from pappascout.domain.models import (
    CellSplit,
    load_callouts,
    load_settings,
)
from pappascout.errors import SettingsError

_POOL = load_settings(REAL_SETTINGS, env_files=()).league.map_pool
INF = float("inf")


def _table() -> dict:
    return load_callouts(_POOL)


# -- A small split: cells 20 x 10 px, half-cells 10 x 5, fit the identity ---


def _area(name: str = "Outside", callout: str = "outside") -> str:
    return (
        f'[de_nuke.{name}]\ncallout = "{callout}"\ncoarse = true\n'
        'junction = false\nneighbours = 0\nconfidence = "stated"\n'
        'source = "a test"\n'
    )


def _split(parts: str, name: str = "Outside", extra: str = "") -> str:
    return (
        f"[de_nuke.{name}.split]\n"
        'image = "a.png"\norigin = [0.0, 0.0]\n'
        'cell = [20.0, 10.0]\ncolumns = "ABCD"\nrows = 4\nsource = "a test"\n'
        f"{extra}"
        f"[de_nuke.{name}.split.fit]\nsx = 1.0\nsy = 1.0\nbx = 0.0\nby = 0.0\n"
        + parts.replace("<A>", name)
    )


def _part(callout: str, cells: str = "", regions: str = "", broad: bool = False) -> str:
    body = f'\n[[de_nuke.<A>.split.parts]]\ncallout = "{callout}"\n'
    if cells:
        body += f"cells = [{cells}]\n"
    if regions:
        body += f"regions = [{regions}]\n"
    if broad:
        body += "broad = true\n"
    return body + 'junction = false\nconfidence = "stated"\nsource = "a test"\n'


def _load(tmp_path: Path, parts: str) -> CellSplit:
    path = tmp_path / "callouts.toml"
    path.write_text(_area() + _split(parts), encoding="utf-8")
    return load_callouts(["de_nuke"], path)["de_nuke"]["Outside"].split


def _at(split: CellSplit, px: float, py: float, z: float = 0.0) -> str | None:
    """The callout at pixel (px, py): the identity fit's y is negated."""
    part = split.part_at(px, -py, z)
    return None if part is None else part.callout


# -- The lookup --------------------------------------------------------------


def test_a_fraction_before_a_broad_whole_takes_its_own_spot_only(
    tmp_path: Path,
) -> None:
    """*auto* is A1a's top quarter, *banana* the whole A1a after it, broad:
    the top quarter is auto's and the rest banana's; y is from the top."""
    split = _load(
        tmp_path,
        _part("auto", regions='{ cell = "A1a", y = [0.0, 0.25], words = "w" }')
        + _part("banana", '"A1a"', broad=True),
    )
    assert _at(split, 5.0, 1.0) == "auto"
    assert _at(split, 5.0, 1.3) == "banana"  # 1.25 is the edge, half-open
    assert _at(split, 5.0, 4.9) == "banana"


def test_a_height_band_decides_between_two_parts_on_one_spot(
    tmp_path: Path,
) -> None:
    """Two bands on one half-cell, split at 100: ``lo <= z < hi``, so 100
    itself is the upper part's. No broad is needed: the bands do not meet."""
    split = _load(
        tmp_path,
        _part("boost", regions='{ cell = "B1a", z = [100.0, inf], words = "w" }')
        + _part("ground", regions='{ cell = "B1a", z = [-inf, 100.0], words = "w" }'),
    )
    assert _at(split, 25.0, 2.0, 99.9) == "ground"
    assert _at(split, 25.0, 2.0, 100.0) == "boost"
    assert _at(split, 25.0, 2.0, 1e6) == "boost"


def test_a_closed_band_holds_its_low_end_and_not_its_high_end(
    tmp_path: Path,
) -> None:
    """``[10, 20)`` on B1a before a whole B1a after it (broad): 10 is the
    band's and 20 is not -- the upper edge is open, like a half-cell's."""
    split = _load(
        tmp_path,
        _part("mid", regions='{ cell = "B1a", z = [10.0, 20.0], words = "w" }')
        + _part("rest", '"B1a"', broad=True),
    )
    assert _at(split, 25.0, 2.0, 10.0) == "mid"
    assert _at(split, 25.0, 2.0, 19.99) == "mid"
    assert _at(split, 25.0, 2.0, 20.0) == "rest"
    assert _at(split, 25.0, 2.0, 9.99) == "rest"


def test_table_order_decides_and_not_the_kind_of_claim(tmp_path: Path) -> None:
    """A whole half-cell written before a region of it holds the region's
    spot too: the later, finer-looking region yields (it must be broad to
    load at all) and holds nothing. Reordered, the region wins its spot."""
    whole_first = _load(
        tmp_path,
        _part("whole", '"A1a"')
        + _part("edge", regions='{ cell = "A1a", y = [0.0, 0.25], words = "w" }', broad=True),
    )
    assert _at(whole_first, 5.0, 0.5) == "whole"
    edge_first = _load(
        tmp_path,
        _part("edge", regions='{ cell = "A1a", y = [0.0, 0.25], words = "w" }')
        + _part("whole", '"A1a"', broad=True),
    )
    assert _at(edge_first, 5.0, 0.5) == "edge"
    assert _at(edge_first, 5.0, 4.0) == "whole"


def test_a_position_outside_every_band_takes_the_nearest_named_half_cell(
    tmp_path: Path,
) -> None:
    """A band holds only its heights; below it the spot is nobody's, and the
    nearest rule runs over **whole** named half-cells only -- the banded
    region's own half-cell is not one of them."""
    split = _load(
        tmp_path,
        _part("boost", regions='{ cell = "B1a", z = [100.0, inf], words = "w" }')
        + _part("west", '"A1a"'),
    )
    assert _at(split, 25.0, 2.0, 50.0) == "west"
    assert "B1a" in split.inherited
    assert split.inherited["B1a"] == "west"


def test_a_crossing_is_a_box_on_the_point_where_its_half_cells_meet(
    tmp_path: Path,
) -> None:
    """A1d, B1c, A2b and B2a meet at (20, 10). Size 0.5 is a box half a
    half-cell wide (5 x 2.5 px); size 1 is one half-cell (10 x 5 px), the
    size of the postimerkki, ct box and dig precedents. There is no default:
    a crossing without a size is refused below."""
    words = 'words = "w"'
    small = _load(
        tmp_path,
        _part(
            "post",
            regions=f'{{ crossing = ["A1d", "B1c", "A2b", "B2a"], size = 0.5, {words} }}',
        )
        + _part("rest", '"A1a"'),
    )
    assert _at(small, 17.6, 8.8) == "post"
    assert _at(small, 22.4, 11.2) == "post"
    assert _at(small, 17.4, 10.0) == "rest"
    big = _load(
        tmp_path,
        _part(
            "post",
            regions=f'{{ crossing = ["A1d", "B1c", "A2b", "B2a"], size = 1.0, {words} }}',
        )
        + _part("rest", '"A1a"'),
    )
    assert _at(big, 15.1, 7.6) == "post"
    assert _at(big, 24.9, 12.4) == "post"
    assert _at(big, 14.9, 10.0) == "rest"


def test_two_half_cells_side_by_side_meet_on_an_edge_and_name_no_crossing(
    tmp_path: Path,
) -> None:
    with pytest.raises(SettingsError, match="do not meet at one point"):
        _load(
            tmp_path,
            _part("x", regions='{ crossing = ["A1a", "A1b"], size = 1.0, words = "w" }'),
        )


# -- The loader's refusals ---------------------------------------------------


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        (  # a region over an earlier part, not marked broad
            _part("a", '"A1a"')
            + _part("b", regions='{ cell = "A1a", x = [0.0, 0.5], words = "w" }'),
            "is not marked broad",
        ),
        (  # a whole half-cell twice, the later not broad
            _part("a", '"A1a"') + _part("b", '"A1a"'),
            "claimed by both",
        ),
        (  # one part naming a half-cell twice, broad or not
            _part("a", '"A1a", "A1a"', broad=True),
            "claimed by both",
        ),
        (  # broad but over nothing
            _part("a", '"A1a"') + _part("b", '"B1a"', broad=True),
            "overlaps no earlier part",
        ),
        (  # bands that meet are a double claim
            _part("a", regions='{ cell = "A1a", z = [0.0, 10.0], words = "w" }')
            + _part("b", regions='{ cell = "A1a", z = [5.0, 20.0], words = "w" }'),
            "is not marked broad",
        ),
        (
            _part("a", regions='{ cell = "A1a", crossing = ["A1d", "B2a"], size = 1.0, words = "w" }'),
            "exactly one of them",
        ),
        (_part("a", regions='{ words = "w" }'), "exactly one of them"),
        (_part("a", regions='{ cell = "A1a", x = [0.5, 0.5], words = "w" }'), "not a range"),
        (_part("a", regions='{ cell = "A1a", y = [0.0, 1.5], words = "w" }'), "not a range"),
        (_part("a", regions='{ cell = "A1a", z = [5.0, 5.0], words = "w" }'), "height band"),
        (_part("a", regions='{ cell = "A1a", size = 1.0, words = "w" }'), "size belongs"),
        (
            _part("a", regions='{ crossing = ["A1d", "B2a"], size = 3.0, words = "w" }'),
            r"not in \(0, 2\]",
        ),
        (_part("a", regions='{ crossing = ["A1d"], size = 1.0, words = "w" }'), "at least two"),
        (_part("a", regions='{ crossing = ["A1d", "B2a"], words = "w" }'), "has no size"),
        (
            _part("a", regions='{ crossing = ["A1d", "B2a"], size = 1.0, x = [0.0, 0.5], words = "w" }'),
            "a crossing has",
        ),
        (
            _part("a", regions='{ crossing = ["A1a", "A1a2"], size = 1.0, words = "w" }'),
            "not a half-cell",
        ),
        (_part("a", regions='{ cell = "E1a", words = "w" }'), "not a half-cell"),
        (_part("a", regions='{ cell = "A1a" }'), "words"),
    ],
)
def test_a_region_that_would_give_a_position_two_answers_is_refused(
    tmp_path: Path, parts: str, message: str
) -> None:
    with pytest.raises(SettingsError, match=message):
        _load(tmp_path, parts)


def test_the_largest_crossing_box_reaches_the_grid_border_and_no_further(
    tmp_path: Path,
) -> None:
    """Half-cells that meet at one point without sharing an edge meet at
    least one half-cell inside the border, so the largest box (size 2) on
    the innermost-but-one point reaches the grid's corner exactly -- why
    the model needs no "outside the grid" refusal for a crossing."""
    split = _load(
        tmp_path,
        _part(
            "a",
            regions='{ crossing = ["A1a", "A1d"], size = 2.0, words = "w" }',
        ),
    )
    assert _at(split, 0.0, 0.0) == "a"
    assert _at(split, 19.9, 9.9) == "a"


# -- One table for several splits (parts_from) -------------------------------


def _two_areas(tmp_path: Path, second: str) -> Path:
    path = tmp_path / "callouts.toml"
    path.write_text(
        _area("Outside", "outside") + _split(_part("a", '"A1a"') + _part("b", '"B1a"'))
        + "\n" + _area("Yard", "yard") + second,
        encoding="utf-8",
    )
    return path


def test_a_split_that_takes_parts_from_another_reads_the_same_table(
    tmp_path: Path,
) -> None:
    path = _two_areas(
        tmp_path, _split("", name="Yard", extra='parts_from = "Outside"\n')
    )
    table = load_callouts(["de_nuke"], path)["de_nuke"]
    one, two = table["Outside"].split, table["Yard"].split
    assert [p.callout for p in two.parts] == ["a", "b"]
    assert two.parts == one.parts
    assert two.parts[0] is not one.parts[0]  # a copy, not a shared object


@pytest.mark.parametrize(
    "second",
    [
        _split("", name="Yard", extra='parts_from = "Nowhere"\n'),
        _split(_part("c", '"C1a"'), name="Yard", extra='parts_from = "Outside"\n'),
    ],
)
def test_parts_from_needs_an_area_whose_split_writes_its_own(
    tmp_path: Path, second: str
) -> None:
    path = _two_areas(tmp_path, second)
    with pytest.raises(SettingsError, match="parts_from"):
        load_callouts(["de_nuke"], path)


def test_a_borrower_that_disagrees_with_its_lender_on_junction_is_refused(
    tmp_path: Path,
) -> None:
    """A shared part's junction is derived from the areas whose positions it
    holds, so a transit area cannot borrow a junction area's table."""
    lender = _area("Outside", "outside").replace(
        "junction = false", 'junction = true\njunction_source = "his words"'
    )
    path = tmp_path / "callouts.toml"
    path.write_text(
        lender + _split(_part("a", '"A1a"'))
        + "\n" + _area("Yard", "yard")
        + _split("", name="Yard", extra='parts_from = "Outside"\n'),
        encoding="utf-8",
    )
    with pytest.raises(SettingsError, match="disagrees with it on junction"):
        load_callouts(["de_nuke"], path)


def test_a_borrowed_part_named_like_the_borrowers_coarse_name_is_refused(
    tmp_path: Path,
) -> None:
    """The shared table is held to each borrowing area's own rules: a part
    may not print like that area's coarse name."""
    path = tmp_path / "callouts.toml"
    path.write_text(
        _area("Outside", "outside") + _split(_part("yard", '"A1a"'))
        + "\n" + _area("Yard", "yard")
        + _split("", name="Yard", extra='parts_from = "Outside"\n'),
        encoding="utf-8",
    )
    with pytest.raises(SettingsError, match="whole area's own coarse name"):
        load_callouts(["de_nuke"], path)


# -- The shipped table -------------------------------------------------------


_NEW_SPLITS = {
    "de_inferno": ("Banana", "Apartments", "TopofMid"),
    "de_anubis": ("Canal",),
    "de_dust2": ("UnderA",),
}


def test_the_three_inferno_splits_read_one_table() -> None:
    """A13: *"Boilerin kävimme jo apsien kohdalla"* -- one table, written on
    Banana's split, read by Apartments' and TopofMid's."""
    inferno = _table()["de_inferno"]
    banana = inferno["Banana"].split.parts
    for area in ("Apartments", "TopofMid"):
        assert inferno[area].split.parts == banana, area
    assert len(banana) == 36


@pytest.mark.parametrize(
    ("map_name", "area"),
    [(m, a) for m, areas in _NEW_SPLITS.items() for a in areas],
)
def test_the_new_grids_are_grid2s_and_cover_the_image(map_name: str, area: str) -> None:
    """As Ancient's (Story 4.18): origin (0, 0), one cell of 425 game units
    by the split's own fit, and the fewest whole cells covering the image
    whose size is in its file name."""
    split = _table()[map_name][area].split
    assert split.origin == (0.0, 0.0)
    assert split.cell == (425 * split.fit.sx, 425 * split.fit.sy)
    width, height = map(int, re.search(r"_(\d+)x(\d+)\.png$", split.image).groups())
    assert len(split.columns) == ceil(width / split.cell[0])
    assert split.rows == ceil(height / split.cell[1])
    assert split.zmin is None


def test_his_four_answered_readings_are_built_as_he_answered() -> None:
    """His answers of the same morning (section 'His answers to the four
    open readings, same morning'): G6b and H6a are cross on top and
    yläbanaani below; puolimuuri is G7c only; "I0d" is I9d; J12b's left half
    is long hall, which is called partsikäytävä, finer than apartments. No
    source still waits for an answer."""
    table = _table()
    parts = {p.callout: p for p in table["de_inferno"]["Banana"].split.parts}
    answers = "section 'His answers to the four open readings, same morning'"
    cross = {(r.cell, r.y) for r in parts["cross"].regions}
    assert cross == {("G6b", (0.0, 0.5)), ("H6a", (0.0, 0.5))}
    assert {"G6b", "H6a"} <= set(parts["yläbanaani"].cells)
    assert parts["puolimuuri"].cells == ["G7c"] and not parts["puolimuuri"].regions
    assert parts["close/brackets"].cells == ["I9c", "I9d", "I10a", "I10b"]
    assert "long hall" not in parts
    walk = parts["partsikäytävä"]
    assert ("J12b", (0.0, 0.5)) in {(r.cell, r.x) for r in walk.regions}
    names = list(parts)
    assert names.index("partsikäytävä") < names.index("apartments")
    assert parts["apartments"].broad
    for callout in ("cross", "puolimuuri", "close/brackets", "partsikäytävä"):
        assert answers in parts[callout].source, callout
    for map_name, table_of in table.items():
        for area, entry in table_of.items():
            for part in entry.split.parts if entry.split else []:
                assert "awaiting" not in part.source, (map_name, part.callout)


#: Every shipped part named like an existing game area of its map, and that
#: area -- the merges of the lead's A12, reported in Story 4.20.
_MERGES = {
    "de_inferno": {
        "t ramp": "TRamp", "alamidi": "LowerMid", "balcony": "Balcony",
        "back alley": "BackAlley", "second mid": "SecondMid",
        "underpass": "Underpass",
    },
    "de_anubis": {"bridge": "Bridge", "stairs": "TStairs", "connector": "Connector"},
    "de_dust2": {
        "a site": "BombsiteA", "ramp": "ARamp", "ct spawn": "CTSpawn",
        "short stairs": "ShortStairs", "short a": "ExtendedA",
    },
}


@pytest.mark.parametrize("map_name", sorted(_MERGES))
def test_the_merged_parts_are_exactly_the_places_named_so(map_name: str) -> None:
    table = _table()[map_name]
    by_callout = {e.callout: a for a, e in table.items() if e.callout}
    parts = {
        p.callout: p
        for area in _NEW_SPLITS[map_name]
        for p in table[area].split.parts
    }
    merged = {c: by_callout[c] for c in parts if c in by_callout}
    assert merged == _MERGES[map_name]
    for callout, area in merged.items():
        assert parts[callout].junction == table[area].junction, callout


def test_the_coarse_names_that_a_part_would_repeat_are_not_repeated() -> None:
    """Banana stays *banana* for a rule row on the whole area, so his
    broadest part is written *banaani*; Canal's own coarse name became the
    guide labels *canal/boat*, so his *canal* can be a part."""
    table = _table()
    assert table["de_inferno"]["Banana"].callout == "banana"
    inferno = {p.callout for p in table["de_inferno"]["Banana"].split.parts}
    assert "banaani" in inferno and "banana" not in inferno
    assert table["de_anubis"]["Canal"].callout == "canal/boat"
    assert "canal" in {p.callout for p in table["de_anubis"]["Canal"].split.parts}


def test_what_the_archive_shows_as_one_level_has_no_band() -> None:
    """A28 and the balcony (A15): measured one level, so not built -- short
    a holds nothing in H3a, and H3b's top half only by his tie answer, with
    no band; balcony carries no band; and the two bands the band rule
    drops (H5d, underpass) are whole half-cells."""
    table = _table()
    short = next(
        p for p in table["de_dust2"]["UnderA"].split.parts if p.callout == "short a"
    )
    held = set(short.cells) | {r.cell for r in short.regions}
    assert "H3a" not in held
    assert [(r.y, r.z) for r in short.regions if r.cell == "H3b"] == [((0.0, 0.5), None)]
    assert "NOT BUILT" in short.source
    inferno = {p.callout: p for p in table["de_inferno"]["Banana"].split.parts}
    assert inferno["ct boost"].cells == ["H5d"] and not inferno["ct boost"].regions
    assert "H5d" not in {r.cell for r in inferno["ct"].regions}
    assert inferno["underpass"].cells == ["F11b", "F11d"]
    assert not inferno["underpass"].regions
    balcony = next(
        p for p in table["de_inferno"]["Banana"].split.parts if p.callout == "balcony"
    )
    assert all(r.z is None for r in balcony.regions)


def test_his_answers_to_the_two_review_ties_are_explicit_fractions() -> None:
    """Section 'His answers to the two review ties': E8a's right half is
    canal and its left half connector; H3b's top half is short (short a)
    and its bottom half elevator -- each as a fraction, so no tie decides."""
    table = _table()
    anubis = {p.callout: p for p in table["de_anubis"]["Canal"].split.parts}
    dust2 = {p.callout: p for p in table["de_dust2"]["UnderA"].split.parts}
    ties = "section 'His answers to the two review ties, afternoon 2026-09-28'"

    def halves(part, cell):
        return {(r.x, r.y) for r in part.regions if r.cell == cell}

    assert halves(anubis["canal"], "E8a") == {((0.5, 1.0), (0.0, 1.0))}
    assert halves(anubis["connector"], "E8a") == {((0.0, 0.5), (0.0, 1.0))}
    assert halves(dust2["short a"], "H3b") == {((0.0, 1.0), (0.0, 0.5))}
    assert halves(dust2["elevator"], "H3b") == {((0.0, 1.0), (0.5, 1.0))}
    for part in (anubis["canal"], anubis["connector"], dust2["short a"], dust2["elevator"]):
        assert ties in part.source, part.callout
    split = table["de_dust2"]["UnderA"].split
    top = split._rect_of(split.index_of("H3b"))
    x = ((top[0] + top[2]) / 2 - split.fit.bx) / split.fit.sx
    y = (split.fit.by - (top[1] + (top[3] - top[1]) / 4)) / split.fit.sy
    assert split.part_at(x, y, 0.0).callout == "short a"


def test_reordering_a_splits_parts_moves_the_hash() -> None:
    """Table order decides a shared spot, so it is hashed (aggregate.py's
    hash prose says so)."""
    from pappascout.stages.aggregate import _params_hash

    settings = load_settings(REAL_SETTINGS, env_files=())
    table = _table()
    entry = table["de_anubis"]["Canal"]
    swapped = entry.split.model_copy(
        update={"parts": list(reversed(entry.split.parts))}
    )
    moved = {"de_anubis": {"Canal": entry.model_copy(update={"split": swapped})}}
    same = {"de_anubis": {"Canal": entry}}

    def digest(callouts):
        return _params_hash(
            settings.thresholds, settings.league, settings.aggregate, callouts
        )

    assert digest(moved) != digest(same)


#: A part's junction basis, as its source ends (the header's junction rule).
_BASIS = re.compile(r"Junction basis: (.*)$")


def _stated_basis(part) -> str:
    match = _BASIS.search(part.source)
    assert match, part.callout
    return match[1]


@pytest.mark.parametrize("map_name", sorted(_NEW_SPLITS))
def test_every_new_parts_junction_follows_its_stated_basis(map_name: str) -> None:
    """Pins every Story 4.20 part's junction flag to the header's rule,
    derived and not copied: the flag is the junction of the game area its
    basis names -- the area it is the same place as, the split areas whose
    positions it holds (which must agree), the game area its region lies in,
    his name for a whole area, or the part it lies inside. Whether the
    basis itself is true is the archive's to say, below."""
    table = _table()[map_name]
    parts = {p.callout: p for p in table[_NEW_SPLITS[map_name][0]].split.parts}
    for callout, part in parts.items():
        basis = _stated_basis(part)
        if basis.startswith("inside "):
            other = re.match(r"inside (.+?), whose basis it shares", basis)[1]
            expected = parts[other].junction
        elif basis.startswith("holds positions of "):
            areas = re.match(r"holds positions of ([A-Za-z, ]+)", basis)[1].split(", ")
            flags = {table[a].junction for a in areas}
            assert len(flags) == 1, callout
            (expected,) = flags
        else:
            area = re.search(r"the game's (\w+)|the whole of (\w+)", basis)
            expected = table[area[1] or area[2]].junction
        assert part.junction == expected, (callout, basis)
        assert (part.junction_source is not None) == part.junction, callout


# -- The bands, re-derived on the archive ------------------------------------


def _live_ticks(root: Path, map_name: str) -> pl.DataFrame:
    frames = []
    for ticks_path in sorted((root / "parsed").glob("*/ticks.parquet")):
        name = pl.read_parquet(ticks_path.with_name("match.parquet"))["map_name"][0]
        if name == map_name:
            frames.append(
                pl.read_parquet(ticks_path, columns=["x", "y", "z", "area", "is_alive"])
                .filter(pl.col("is_alive"))
            )
    return pl.concat(frames)


#: The band rule of the callouts.toml header: at least this many ticks on
#: each side of the largest gap, the gap at least this many units (twice the
#: game's 18-unit step), and at least this many times the second-largest.
BAND_MIN_TICKS, BAND_MIN_GAP, BAND_MIN_RATIO = 5, 36.0, 1.5


def _two_levels(z) -> tuple[bool, float, float, float]:
    """Whether heights show two levels by the band rule, and the largest
    gap's ends and midpoint."""
    values = sorted(float(v) for v in z)
    gaps = sorted(
        ((b - a, a, b) for a, b in zip(values, values[1:])), reverse=True
    )
    if not gaps:
        return False, 0.0, 0.0, 0.0
    gap, low, high = gaps[0]
    second = gaps[1][0] if len(gaps) > 1 else 0.0
    below = sum(1 for v in values if v <= low)
    passes = (
        below >= BAND_MIN_TICKS
        and len(values) - below >= BAND_MIN_TICKS
        and gap >= BAND_MIN_GAP
        and gap >= BAND_MIN_RATIO * second
    )
    return passes, low, high, (low + high) / 2


def test_the_band_rule_is_the_headers() -> None:
    """The three numbers above are the header's, read from it -- one place."""
    from test_cell_split import _header_text

    header = _header_text()
    rule = re.search(
        r"at least (\d+) ticks on each side of the largest gap in their z, the "
        r"gap at least (\d+) units .*? and at least ([\d.]+) times the "
        r"second-largest gap",
        header,
    )
    assert rule, "the header states no band rule"
    assert (int(rule[1]), float(rule[2]), float(rule[3])) == (
        BAND_MIN_TICKS, BAND_MIN_GAP, BAND_MIN_RATIO
    )


def _in_rects(ticks: pl.DataFrame, split: CellSplit, rects) -> pl.DataFrame:
    px = ticks["x"].to_numpy() * split.fit.sx + split.fit.bx
    py = -ticks["y"].to_numpy() * split.fit.sy + split.fit.by
    inside = sum(
        ((px >= r[0]) & (px < r[2]) & (py >= r[1]) & (py < r[3]) for r in rects),
    ) > 0
    return ticks.filter(pl.Series(inside))


def _half_cell(split: CellSplit, name: str, y=(0.0, 1.0)):
    x0, y0, x1, y1 = split._rect_of(split.index_of(name))
    return (x0, y0 + (y1 - y0) * y[0], x1, y0 + (y1 - y0) * y[1])


@pytest.mark.archive
@pytest.mark.parametrize("map_name", sorted(_NEW_SPLITS))
def test_every_height_band_passes_the_band_rule_at_its_largest_gap(
    map_name: str,
) -> None:
    """Spec 4.20 decision 2 and the header's band rule, re-derived: for every
    finite band edge of the map's splits, the live ticks of the map inside
    the regions that carry that edge show two levels by the rule, and the
    edge is the midpoint of the largest gap (``zbands-mitattu-2026-09-28
    .md``). A tick imported into the gap, a threshold typed by hand, or a
    band on too few ticks fails here.

    **The direction is derived**: a part whose source says *the upper level
    is the game's X* (or *the lower level*) must find X the most common game
    area on that side of the edge; where one area holds both sides (short
    boost: Quad), the source must say the direction rests on his words."""
    root = require_parsed()
    split = _table()[map_name][_NEW_SPLITS[map_name][0]].split
    ticks = _live_ticks(root, map_name)
    edges: dict[float, list] = {}
    parts: dict[float, set] = {}
    for part, rect, band in split._claims:
        for edge in band or ():
            if edge not in (INF, -INF):
                edges.setdefault(edge, []).append(rect)
                parts.setdefault(edge, set()).add(part.callout)
                # Each banded part says which level it is, and it must be
                # the side of the edge its own band lies on.
                side = "upper" if band[0] == edge else "lower"
                assert f"is the {side} level" in part.source, (part.callout, side)
    assert edges, map_name
    by_callout = {p.callout: p for p in split.parts}
    for edge, rects in edges.items():
        place = _in_rects(ticks, split, rects)
        passes, low, high, mid = _two_levels(place["z"])
        assert passes, (edge, low, high)
        assert low < edge < high and abs(edge - mid) < 0.01, (edge, low, high)
        for side, rows in (
            ("upper", place.filter(pl.col("z") >= edge)),
            ("lower", place.filter(pl.col("z") < edge)),
        ):
            majority = rows.group_by("area").len().sort("len", descending=True)["area"][0]
            for callout in parts[edge]:
                stated = re.search(
                    rf"The {side} level is the game's (\w+)", by_callout[callout].source
                )
                if stated:
                    assert stated[1] == majority, (callout, side, majority)
        above = place.filter(pl.col("z") >= edge).group_by("area").len().sort("len")["area"][-1]
        below = place.filter(pl.col("z") < edge).group_by("area").len().sort("len")["area"][-1]
        if above == below:
            assert all(
                "direction rests on his words" in by_callout[c].source
                for c in parts[edge]
            ), edge


#: The places he names by height that the rule refuses a band, each with the
#: part whose source says so: (map, the part, half-cells, a y range of them).
_ONE_LEVEL = {
    "balcony": ("de_inferno", "balcony", None, None),
    "H5d (ct boost's gap)": ("de_inferno", "ct boost", ["H5d"], (0.0, 1.0)),
    "underpass": ("de_inferno", "underpass", ["F11b", "F11d"], (0.0, 1.0)),
    "t balcony over F11d": ("de_inferno", "t balcony", ["F11d"], (0.0, 1.0)),
    "H3a's top edge (A28)": ("de_dust2", "short a", ["H3a"], (0.0, 0.25)),
}


@pytest.mark.archive
@pytest.mark.parametrize("place", sorted(_ONE_LEVEL))
def test_every_place_built_without_a_band_fails_the_band_rule(place: str) -> None:
    """The other direction: every place he names by height that is built
    without a band fails the rule on the archive, and its part's source says
    it fails. The balcony's place is its own regions."""
    map_name, callout, cells, y = _ONE_LEVEL[place]
    split = _table()[map_name][_NEW_SPLITS[map_name][0]].split
    part = next(p for p in split.parts if p.callout == callout)
    if cells is None:
        rects = [split._region_rect(part, r) for r in part.regions]
    else:
        rects = [_half_cell(split, c, y) for c in cells]
    place_ticks = _in_rects(_live_ticks(require_parsed(), map_name), split, rects)
    assert not _two_levels(place_ticks["z"])[0], place
    assert "fails the band rule" in part.source or "NOT BUILT" in part.source, place


@pytest.mark.archive
def test_h3bs_top_half_is_short_a_by_his_word_though_it_shows_two_levels() -> None:
    """The one exemption, named so it cannot pass in silence: his answer to
    the review tie makes H3b's top half short, with no band -- yet by the
    band rule it shows two levels (UnderA's positions below a gap, ExtendedA's
    and BombsiteA's ticks above). If the archive stops showing two levels,
    the exemption covers nothing and this fails."""
    split = _table()["de_dust2"]["UnderA"].split
    place = _in_rects(
        _live_ticks(require_parsed(), "de_dust2"), split, [_half_cell(split, "H3b", (0.0, 0.5))]
    )
    assert _two_levels(place["z"])[0]
    short = next(p for p in split.parts if p.callout == "short a")
    assert "shows two levels" in short.source


@pytest.mark.archive
@pytest.mark.parametrize("map_name", sorted(_NEW_SPLITS))
def test_every_stated_junction_basis_is_what_the_archive_shows(map_name: str) -> None:
    """The basis each source states, re-derived: *holds positions of A, B*
    is exactly the split areas whose positions the lookup gives the part;
    *its region lies in the game's X (n of the m live ticks)* is the most
    common game area among the map's live ticks the part's own regions take,
    with those counts; *no live tick of the map lies in it* is none."""
    root = require_parsed()
    table = _table()[map_name]
    ticks = _live_ticks(root, map_name)
    split = table[_NEW_SPLITS[map_name][0]].split
    holds: dict[str, set] = {}
    for area in _NEW_SPLITS[map_name]:
        area_split = table[area].split
        for x, y, z in ticks.filter(pl.col("area") == area).select("x", "y", "z").iter_rows():
            holds.setdefault(area_split.part_at(x, y, z).callout, set()).add(area)
    lies: dict[str, dict] = {}
    px = ticks["x"].to_numpy() * split.fit.sx + split.fit.bx
    py = -ticks["y"].to_numpy() * split.fit.sy + split.fit.by
    for i, (area, z) in enumerate(ticks.select("area", "z").iter_rows()):
        for part, rect, band in split._claims:
            if (
                rect[0] <= px[i] < rect[2] and rect[1] <= py[i] < rect[3]
                and (band is None or band[0] <= z < band[1])
            ):
                counts = lies.setdefault(part.callout, {})
                counts[area] = counts.get(area, 0) + 1
                break
    for part in split.parts:
        basis = _stated_basis(part)
        held = holds.get(part.callout, set())
        if basis.startswith("holds positions of "):
            areas = set(re.match(r"holds positions of ([A-Za-z, ]+)", basis)[1].split(", "))
            assert held == areas, (part.callout, held)
        elif basis.startswith("holds none"):
            assert not held, part.callout
            counts = lies.get(part.callout, {})
            stated = re.search(r"the game's (\w+) \((\d+) of the (\d+) live ticks", basis)
            if stated:
                top = max(counts, key=counts.get)
                assert (top, counts[top], sum(counts.values())) == (
                    stated[1], int(stated[2]), int(stated[3])
                ), part.callout
            else:
                assert "no live tick of the map lies in it" in basis, part.callout
                assert not counts, part.callout
