"""Story 4.20: places finer than a half-cell -- fractions, crossings, height
bands and nested names -- and the one Inferno table five splits read.

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
    with pytest.raises(SettingsError, match="whole area's own name"):
        load_callouts(["de_nuke"], path)


# -- The shipped table -------------------------------------------------------


_NEW_SPLITS = {
    # Story 4.28: Inferno's two sites read the one table too.
    "de_inferno": ("Banana", "Apartments", "TopofMid", "BombsiteB", "BombsiteA"),
    "de_anubis": ("Canal",),
    "de_dust2": ("UnderA",),
}
#: The splits whose parts are held to the header's junction rule and whose
#: bands to the band rule, keyed ``<map>`` or ``<map>.<area>`` -> the areas
#: reading one table, the first writing it: Story 4.20's, since Story 4.23
#: Nuke's lobby, which is on the yard's guide grid and so not in
#: :data:`_NEW_SPLITS`, and since Story 4.24 Nuke's four lower-floor
#: splits, each with a table of its own.
_PART_SPLITS = {
    **_NEW_SPLITS,
    "de_nuke": ("Lobby",),
    "de_nuke.BombsiteB": ("BombsiteB",),
    "de_nuke.Ramp": ("Ramp",),
    "de_nuke.Tunnels": ("Tunnels",),
    "de_nuke.Observation": ("Observation",),
    # Story 4.26: Dust2's sites and what lies around them, each a table of
    # its own on UnderA's grid (dust2-taulukot-2026-10-01.md section 5).
    **{
        f"de_dust2.{area}": (area,)
        for area in (
            "BombsiteA", "ARamp", "ExtendedA", "LongA", "BombsiteB", "BDoors",
            "MidDoors",
        )
    },
    # Story 4.27: Ancient's sites on MainHall's grid
    # (ancient-taulukot-2026-10-01.md section 5). CTSpawn reads BombsiteA's
    # table (parts_from): his temple and his elbow hold both areas' positions.
    "de_ancient.BombsiteA": ("BombsiteA", "CTSpawn"),
    **{
        f"de_ancient.{area}": (area,)
        for area in ("BombsiteB", "TSideLower", "Alley", "SideEntrance")
    },
    # Story 4.28: Inferno's two transit areas, each a table of its own on
    # Banana's grid (inferno-taulukot-2026-10-02.md section 5).
    "de_inferno.Pit": ("Pit",),
    "de_inferno.Ruins": ("Ruins",),
}


def _map_of(key: str) -> str:
    """A :data:`_PART_SPLITS` key's map."""
    return key.split(".")[0]


def _on_floor(ticks: pl.DataFrame, split: CellSplit) -> pl.DataFrame:
    """The ticks on the split's floor (Story 4.24): a band is measured on
    the floor its regions are drawn on, never on the other floor's ticks
    projected through this floor's fit."""
    low, high = split.floor_band
    return ticks.filter((pl.col("z") >= low) & (pl.col("z") < high))


def _header_clause(pattern: str) -> re.Match:
    """A clause of the header's band rule, read from the one place it is
    written (callouts.toml's header)."""
    from test_cell_split import _header_text

    clause = re.search(pattern, _header_text())
    assert clause, f"the header's band rule states no {pattern!r}"
    return clause


def _carried_statement() -> re.Pattern:
    """THE CARRIED BAND (Story 4.24, the lead's decision 26), read from the
    header: when he names one level in a half-cell whose ticks the rule
    cannot separate, the band is measured on the half-cells the rule admits
    and carried to the others by his word, and the source says so in the
    header's own words -- which become the pattern a source is read with."""
    clause = _header_clause(
        r"THE CARRIED BAND .*? the source says so \('([^'<]*)<half-cells>([^']*)'\)"
        r", and the carried half-cells are his words"
    )
    return re.compile(
        re.escape(clause[1])
        + r"([A-P][0-9]+[a-d](?:, [A-P][0-9]+[a-d])*)"
        + re.escape(clause[2])
    )


def _carried_cells(part) -> set[str]:
    """The half-cells a part's source says its band is carried to."""
    carried = _carried_statement().search(part.source)
    return set(carried[1].split(", ")) if carried else set()


def _measured_rects(split: CellSplit, part, band, *, carried=False) -> list:
    """The rectangles of a part's regions with this band that the band is
    measured on: all of them, but for the half-cells its source says the
    band is carried to -- or, with ``carried``, only those."""
    left_out = _carried_cells(part)
    return [
        split._region_rect(part, region)
        for region in part.regions
        if region.z == band and (region.cell in left_out) == carried
    ]


def test_every_carried_band_is_carried_to_his_own_half_cells() -> None:
    """The header's carried band, as shipped: a source that carries a band
    names only half-cells its part holds with that band, his quoted words
    write each of them, and the part is not inferred for it -- the cells
    are his, only the measurement is not theirs. Nuke's shelf and ladder in
    I11a are the one case."""
    from test_cell_split import _HIS_WORDS, _free_cells

    found = {}
    for map_name, table in _table().items():
        for area, entry in table.items():
            for part in entry.split.parts if entry.split else []:
                cells = _carried_cells(part)
                if not cells:
                    continue
                found[(map_name, area, part.callout)] = cells
                # The docstring's claim, asserted (the architect review).
                assert part.confidence != "inferred", part.callout
                banded = {r.cell for r in part.regions if r.z is not None}
                assert cells <= banded, (part.callout, cells)
                written = {
                    c for q in _HIS_WORDS.findall(part.source) for c in _free_cells(q)
                }
                assert cells <= written, (part.callout, cells)
    assert found == {
        ("de_nuke", "Lobby", "hattuhylly"): {"I11a"},
        ("de_nuke", "Lobby", "hutladder"): {"I11a"},
        # Story 4.25 (review round 1): the ring's band, carried to D6d.
        ("de_nuke", "BombsiteB", "b rafters"): {"D6d"},
        # Story 4.28 (the lead's decision #5): ykkonen's band, carried to
        # kakkonen's two corners, on each split that reads the one table.
        **{
            ("de_inferno", area, "kakkonen"): {"G6a", "G5c"}
            for area in ("Banana", "Apartments", "TopofMid", "BombsiteB", "BombsiteA")
        },
    }


def test_the_five_inferno_splits_read_one_table() -> None:
    """A13: *"Boilerin kävimme jo apsien kohdalla"* -- one table, written on
    Banana's split, read by Apartments' and TopofMid's, and since Story 4.28
    by both sites' (the lead's decision #1): 36 parts of 2026-09-28 and 16
    of 2026-10-02."""
    inferno = _table()["de_inferno"]
    banana = inferno["Banana"].split.parts
    for area in ("Apartments", "TopofMid", "BombsiteB", "BombsiteA"):
        assert inferno[area].split.parts == banana, area
    assert len(banana) == 52


@pytest.mark.parametrize(
    ("map_name", "area"),
    [(m, a) for m, areas in _NEW_SPLITS.items() for a in areas]
    + [
        tuple(key.split("."))
        for key in _PART_SPLITS
        if key.startswith(("de_dust2.", "de_ancient.", "de_inferno."))
    ]
    + [("de_ancient", "CTSpawn")],
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
        # Story 4.28: his ruins and pit on the sites' table.
        "ruins": "Ruins", "pit": "Pit",
    },
    "de_anubis": {"bridge": "Bridge", "stairs": "TStairs", "connector": "Connector"},
    "de_dust2": {
        "a site": "BombsiteA", "ramp": "ARamp", "ct spawn": "CTSpawn",
        "short stairs": "ShortStairs", "short a": "ExtendedA",
        # Story 4.26: his ikkuna and B ovet, on the B side's splits.
        "window": "Hole", "b doors": "BDoors",
        # Review round 1 (item 18): long below ct cross on UnderA's split.
        "long a": "LongA",
    },
    # Story 4.27: his ramppia, on BombsiteB's split and TSideLower's.
    "de_ancient": {"ramp": "Ramp"},
}


@pytest.mark.parametrize("map_name", sorted(_MERGES))
def test_the_merged_parts_are_exactly_the_places_named_so(map_name: str) -> None:
    table = _table()[map_name]
    by_callout = {e.callout: a for a, e in table.items() if e.callout}
    parts = {
        p.callout: p
        for entry in table.values()
        if entry.split is not None
        for p in entry.split.parts
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
    a holds nothing in H3a; balcony carries no band; and the two bands the
    band rule drops (H5d, underpass) are whole half-cells."""
    table = _table()
    short = next(
        p for p in table["de_dust2"]["UnderA"].split.parts if p.callout == "short a"
    )
    held = set(short.cells) | {r.cell for r in short.regions}
    assert "H3a" not in held
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


def test_his_answer_to_the_e8a_tie_is_two_explicit_halves() -> None:
    """Section 'His answers to the two review ties': E8a's right half is
    canal and its left half connector -- each a fraction, so no tie
    decides."""
    anubis = {p.callout: p for p in _table()["de_anubis"]["Canal"].split.parts}
    ties = "section 'His answers to the two review ties, afternoon 2026-09-28'"

    def halves(part):
        return {(r.x, r.y) for r in part.regions if r.cell == "E8a"}

    assert halves(anubis["canal"]) == {((0.5, 1.0), (0.0, 1.0))}
    assert halves(anubis["connector"]) == {((0.0, 0.5), (0.0, 1.0))}
    for part in (anubis["canal"], anubis["connector"]):
        assert ties in part.source, part.callout


def test_h3b_is_elevator_but_for_shorts_raised_level_in_its_top_part() -> None:
    """Section 'His answers after Story 4.20', answer 1: in H3b's top part
    (y [0, 0.5], his unquantified "vajaa puolet") short a holds only the
    raised level, above its measured band edge; everything else in H3b --
    the ground of the top part and all of the bottom -- is elevator, which
    comes after short a and yields to it."""
    split = _table()["de_dust2"]["UnderA"].split
    parts = {p.callout: p for p in split.parts}
    after = "section 'His answers after Story 4.20, evening 2026-09-28'"
    (region,) = [r for r in parts["short a"].regions if r.cell == "H3b"]
    assert region.y == (0.0, 0.5) and region.z is not None and region.z[1] == INF
    assert "H3b" in parts["elevator"].cells and parts["elevator"].broad
    names = [p.callout for p in split.parts]
    assert names.index("short a") < names.index("elevator")
    for callout in ("short a", "elevator"):
        assert after in parts[callout].source, callout
    from test_cell_split import _at_pixel, _centre

    # The half-cell's centre by the naming his answer defines, read on its
    # own (not through CellSplit.index_of), then a quarter up and down.
    px, py = _centre(split, "H3b")
    quarter = split.cell[1] / 2 / 4
    x, top, _ = _at_pixel(split, px, py - quarter)
    _, bottom, _ = _at_pixel(split, px, py + quarter)
    edge = region.z[0]
    assert split.part_at(x, top, edge).callout == "short a"
    assert split.part_at(x, top, edge - 0.01).callout == "elevator"
    assert split.part_at(x, bottom, edge + 50.0).callout == "elevator"


def test_the_size_rule_is_his_and_no_source_calls_it_the_leads() -> None:
    """His answer after Story 4.20, answer 2 ("Sopii"): the header gives the
    size rule to him and cites the section, keeps his unquantified "vajaa
    puolet" out of it (that half is a reading of one place, in its own
    source), and no part source still calls it the lead's rule."""
    from test_cell_split import _header_text

    header = _header_text()
    assert re.search(
        r"THE SIZE RULE FOR SIZES HE DID NOT QUANTIFY, HIS SINCE 2026-09-28: "
        r"proposed by the lead .*? and confirmed by him "
        r"\(puoliruudut-vastaus-2-2026-09-28\.md section 'His answers after "
        r"Story 4.20, evening 2026-09-28', answer 2: 'Sopii'\)",
        header,
    ), "the header does not give the size rule to him"
    rule = header[header.index("THE SIZE RULE") : header.index("parts_from = ")]
    assert "vajaa" not in rule
    sized = 0
    for map_name, table in _table().items():
        for entry in table.values():
            for part in entry.split.parts if entry.split else []:
                assert "lead's rule" not in part.source, (map_name, part.callout)
                if "sized by the" in part.source:
                    assert "sized by the size rule he confirmed" in part.source
                    sized += 1
    assert sized, "no source rests on the size rule"


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


# -- Story 4.23: lobby's levels, Crane as rafters, I3a -----------------------


def test_lobby_is_his_whole_room_with_his_shelf_and_ladder_inside_it() -> None:
    """Decisions 1 and 2: lobby stays lobby -- not coarse, unmarked, the
    whole area for its rule rows -- and its split holds two finer places:
    hattuhylly, the shelf level of I10c and I11c between two measured edges
    (a closed band, Story 4.23's extended rule), and hutladder, the ladder in
    I11a above its edge. A Lobby position on the ground, anywhere else in
    the room, or on the first steps stays lobby, and nothing is
    inherited."""
    from pappascout.domain.aggregate import _route_place
    from test_cell_split import _at_pixel, _centre

    table = _table()["de_nuke"]
    lobby = table["Lobby"]
    assert lobby.callout == "lobby" and not lobby.coarse and lobby.junction
    split = lobby.split
    assert [p.callout for p in split.parts] == ["hattuhylly", "hutladder"]
    shelf, ladder = split.parts
    # Story 4.24: his cells since 2026-09-29, "hattuhylly I10c, I11a, I11c",
    # the band measured on I10c and I11c and carried to I11a by his word;
    # the ladder is I11a above the shelf.
    assert (shelf.confidence, ladder.confidence) == ("stated", "stated")
    assert {(r.cell, r.z) for r in shelf.regions} == {
        ("I10c", (-332.6, -216.29)), ("I11a", (-332.6, -216.29)),
        ("I11c", (-332.6, -216.29)),
    }
    assert [(r.cell, r.z) for r in ladder.regions] == [("I11a", (-216.29, INF))]
    assert "band is carried to I11a by his word" in shelf.source
    assert "band is carried to I11a by his word" in ladder.source
    assert "'hattuhylly I10c, I11a, I11c." in shelf.source
    assert split.inherited == {}
    x, y, _ = _at_pixel(split, *_centre(split, "I11a"))
    assert split.part_at(x, y, -216.29).callout == "hutladder"
    assert split.part_at(x, y, -100.0).callout == "hutladder"
    assert split.part_at(x, y, -216.3).callout == "hattuhylly"
    assert split.part_at(x, y, -287.97).callout == "hattuhylly"
    assert split.part_at(x, y, -332.61) is None
    for cell in ("I10c", "I11a", "I11c"):
        x, y, _ = _at_pixel(split, *_centre(split, cell))
        assert split.part_at(x, y, -332.6).callout == "hattuhylly", cell
        assert split.part_at(x, y, -288.0).callout == "hattuhylly", cell
        above = split.part_at(x, y, -216.29)
        if cell == "I11a":
            assert above.callout == "hutladder", cell
        else:
            assert above is None, cell
        assert split.part_at(x, y, -332.61) is None, cell
    for cell in ("I10c", "I11a", "I11c", "H11b"):
        x, y, _ = _at_pixel(split, *_centre(split, cell))
        for z in (-416.0, -380.0):
            assert split.part_at(x, y, z) is None, (cell, z)
            place = _route_place("Lobby", table, frozenset(), (x, y, z))
            assert (place.label, place.flag) == ("lobby", None), (cell, z)
    assert "first steps" in split.source and "stay lobby" in split.source
    whole = _route_place("Lobby", table)
    assert (whole.label, whole.flag) == ("lobby", None)


def test_a_height_band_holds_where_a_player_stands_and_no_detonation(
    tmp_path: Path,
) -> None:
    """The lead's decision in the Story 4.23 review (edge F1): a band is
    measured on players' ticks, so it holds a tick, a death and a throw, and
    never a grenade's detonation -- 14 Lobby detonations burst in mid-air at
    z -311.9 to -328.1, below the shelf's floor (-287.97), and were counted
    as hattuhylly. A detonation falls to the next claim with no band: on a
    small split the broad whole half-cell, on the shipped lobby its rest."""
    from pappascout.domain.aggregate import EVENT_AREA_COLUMNS, named_rows
    from test_cell_split import _at_pixel, _centre

    split = _load(
        tmp_path,
        _part("raised", regions='{ cell = "A1a", z = [10.0, inf], words = "w" }')
        + _part("floor", '"A1a"', broad=True),
    )
    assert _at(split, 5.0, 2.5, 20.0) == "raised"
    assert split.part_at(5.0, -2.5, 20.0, stands=False).callout == "floor"
    assert split.part_at(5.0, -2.5, 0.0, stands=False).callout == "floor"
    table = _table()["de_nuke"]
    lobby = table["Lobby"].split
    x, y, _ = _at_pixel(lobby, *_centre(lobby, "I10c"))

    def named(kind: str | None) -> str:
        row = {"area": "Lobby", "x": x, "y": y, "z": -320.0}
        if kind is not None:
            row["event_kind"] = kind
        (renamed,) = named_rows([row], EVENT_AREA_COLUMNS, table)
        return renamed["area"]

    assert named(None) == named("grenade_thrown") == "hattuhylly"
    assert named("grenade_detonate") == "lobby"


def test_crane_is_his_rafters_merged_with_catwalk_and_rafters() -> None:
    """Decision 3: the game's Crane is his rafters, stated with his hedge
    kept in the source; the merge rule holds its junction to rafters'."""
    table = _table()["de_nuke"]
    assert {a for a, e in table.items() if e.callout == "rafters"} == {
        "Catwalk", "Rafters", "Crane"
    }
    crane = table["Crane"]
    assert crane.confidence == "stated"
    assert crane.junction == table["Rafters"].junction
    assert "vastaukset-2026-09-29.md" in crane.source
    assert "'Tuo crane alue on varmaan A siten puolella oleva rafter'" in crane.source


def test_elevator_is_stated_since_he_confirmed_the_bottom_left_corner() -> None:
    """Decision 4: his answer 3 confirms A27, so elevator carries no mark."""
    from pappascout.domain.aggregate import _uncertain_callouts

    dust2 = _table()["de_dust2"]
    parts = {p.callout: p for p in dust2["UnderA"].split.parts}
    elevator = parts["elevator"]
    assert elevator.confidence == "stated"
    assert "'Kyllä juurikin I3a:n vasen alanurkka'" in elevator.source
    corner = next(r for r in elevator.regions if r.cell == "I3a")
    assert (corner.x, corner.y) == ((0.0, 0.5), (0.5, 1.0))
    assert "elevator" not in _uncertain_callouts(dust2)


#: A part's junction basis, as its source ends (the header's junction rule).
_BASIS = re.compile(r"Junction basis: (.*)$")


def _stated_basis(part) -> str:
    match = _BASIS.search(part.source)
    assert match, part.callout
    return match[1]


@pytest.mark.parametrize("key", sorted(_PART_SPLITS))
def test_every_new_parts_junction_follows_its_stated_basis(key: str) -> None:
    """Pins every Story 4.20 part's junction flag to the header's rule,
    derived and not copied: the flag is the junction of the game area its
    basis names -- the area it is the same place as, the split areas whose
    positions it holds (which must agree), the game area its region lies in,
    his name for a whole area, or the part it lies inside. Whether the
    basis itself is true is the archive's to say, below. Since Story 4.23
    also the lobby's hattuhylly and hutladder, since Story 4.24 the parts of
    Nuke's lower floor."""
    table = _table()[_map_of(key)]
    parts = {p.callout: p for p in table[_PART_SPLITS[key][0]].split.parts}
    for callout, part in parts.items():
        basis = _stated_basis(part)
        his = _his_junction().match(basis)
        if his:
            # Story 4.26 (the header's HIS WORD FIRST): his own quoted words
            # make the part a junction, and they are its junction_source.
            assert part.junction, callout
            assert f"his words: '{his[1]}'" in (part.junction_source or ""), callout
            continue
        if basis == _passage():
            # Story 4.18's passage, written in the header in Story 4.27:
            # transit although its split area is a junction (vent).
            expected = False
        elif basis.startswith("inside "):
            other = re.match(r"inside (.+?), whose basis it shares", basis)[1]
            expected = parts[other].junction
        elif basis.startswith("holds positions of "):
            areas = re.match(r"holds positions of ([A-Za-z, ]+)", basis)[1].split(", ")
            flags = {table[a].junction for a in areas}
            assert len(flags) == 1, callout
            (expected,) = flags
        else:
            area = re.search(r"the game's (\w+)|the whole of (\w+)", basis)
            assert area, (callout, f"a basis of no known form: {basis!r}")
            expected = table[area[1] or area[2]].junction
        assert part.junction == expected, (callout, basis)
        assert (part.junction_source is not None) == part.junction, callout


def _passage() -> str:
    """The basis of A PASSAGE (Story 4.18, written in Story 4.27): a part that
    is a way to somewhere, not a place -- read from the header where the
    clause is written."""
    return _header_clause(
        r"A PASSAGE \(Story 4\.18.*? Its basis reads '([^']*)'"
    )[1]


def _his_junction() -> re.Pattern:
    """A basis his own words decide (the header's HIS WORD FIRST, Story
    4.26), read from the header the way the carried band is: the form the
    header writes, with his words quoted where it says <his words>, up to
    the split areas, which follow the match."""
    clause = _header_clause(
        r'HIS WORD FIRST .*? Its basis then reads '
        r'"([^"<]*)\'<his words>\'([^"<]*)<areas>"'
    )
    return re.compile(re.escape(clause[1]) + r"'([^']*)'" + re.escape(clause[2]))


#: The parts his own words make a junction (Story 4.26, review round 1):
#: a closed set, so a basis rewritten into the form -- or out of it --
#: fails by name.
_HIS_JUNCTIONS = {("de_dust2", "ct cross")}


#: The parts whose basis is A PASSAGE (Story 4.27, Winston's review): a
#: closed set, as :data:`_HIS_JUNCTIONS`, so a basis rewritten into the form
#: -- a junction made transit by a sentence -- fails by name.
_PASSAGES = {("de_ancient", "vent")}


def test_the_parts_that_are_a_passage_are_a_closed_set() -> None:
    found = set()
    for map_name, table in _table().items():
        for entry in table.values():
            for part in entry.split.parts if entry.split else []:
                match = _BASIS.search(part.source)
                if match and match[1] == _passage():
                    found.add((map_name, part.callout))
    assert found == _PASSAGES


def _guide_picture() -> re.Pattern:
    """A basis read off the guide picture (the header's THE GUIDE PICTURE,
    Story 4.28): the form the header writes, with the game area where it
    says <area>."""
    clause = _header_clause(
        r'THE GUIDE PICTURE .*? its basis then reads "([^"<]*)<area>([^"<]*)"'
    )
    return re.compile(re.escape(clause[1]) + r"(\w+)" + re.escape(clause[2]))


#: The parts whose junction basis is the guide picture, and the game area it
#: names (Story 4.28, review round 1): a closed set, as the passages and his
#: word, so a basis that names another area, or a part that takes the form
#: unlisted, fails by name -- one sentence decides these parts' flags.
_GUIDE_PICTURE = {
    ("de_dust2", "dog"): "BombsiteB",
    ("de_dust2", "toka kulma"): "BombsiteB",
    ("de_ancient", "ct laatikko"): "Alley",
    ("de_ancient", "short nurkka"): "SideEntrance",
    ("de_inferno", "dark"): "BombsiteB",
    ("de_inferno", "headshot boksi"): "BombsiteA",
    ("de_inferno", "longbox"): "BombsiteA",
}


def test_the_parts_read_off_the_guide_picture_are_a_closed_set() -> None:
    found = {}
    for map_name, table in _table().items():
        for entry in table.values():
            for part in entry.split.parts if entry.split else []:
                match = _BASIS.search(part.source)
                read = _guide_picture().search(match[1]) if match else None
                if read:
                    found[(map_name, part.callout)] = read[1]
                    assert part.junction == table[read[1]].junction, part.callout
    assert found == _GUIDE_PICTURE


#: The structured forms a junction_source and a basis share (review round
#: 1, #19, and the architect review), each read to what it names: the split
#: areas it holds ("A, B" or "A and B"), the game area its region lies in
#: with the live-tick counts, the area it is the same place as, or the part
#: it lies inside. The guide picture's form is read from the header.
_FORMS = (
    ("holds", re.compile(r"^holds positions of ([A-Z]\w*(?:(?:, | and )[A-Z]\w*)*)")),
    ("lies", re.compile(
        r"^holds none of the split area's positions; its region lies in the game's "
        r"(\w+) \((\d+) of the (\d+) live ticks"
    )),
    ("same", re.compile(r"^the same place as the game's (\w+)")),
    ("inside", re.compile(r"^inside (.+?)(?: \('[^']*'\))?, whose basis it shares")),
)


def _reading(text: str) -> tuple | None:
    """The structured form a junction_source or basis is written in, and
    what it names; ``None`` for free prose."""
    guide = _guide_picture().search(text)
    if guide:
        return ("guide", guide[1])
    for form, pattern in _FORMS:
        found = pattern.match(text)
        if found:
            if form == "holds":
                return (form, frozenset(re.split(", | and ", found[1])))
            return (form, found.groups())
    return None


#: How many parts of each registered split carry a junction_source in a
#: structured form, re-pinned by running: a pattern that stops matching
#: drops a count and fails here.
_SOURCE_FORMS_CHECKED = {
    "de_ancient.Alley": 0, "de_ancient.BombsiteA": 9, "de_ancient.BombsiteB": 5,
    "de_ancient.SideEntrance": 1, "de_ancient.TSideLower": 1, "de_anubis": 4,
    "de_dust2": 7, "de_dust2.ARamp": 1, "de_dust2.BDoors": 3, "de_dust2.BombsiteA": 2,
    "de_dust2.BombsiteB": 9, "de_dust2.ExtendedA": 1, "de_dust2.LongA": 2,
    "de_dust2.MidDoors": 2, "de_inferno": 26, "de_inferno.Pit": 0,
    "de_inferno.Ruins": 0, "de_nuke": 2, "de_nuke.BombsiteB": 1,
    "de_nuke.Observation": 0, "de_nuke.Ramp": 0, "de_nuke.Tunnels": 0,
}


@pytest.mark.parametrize("key", sorted(_PART_SPLITS))
def test_a_junction_source_in_the_basis_form_names_the_basis_areas(key: str) -> None:
    """Review round 1 (#19) and the architect review: a junction_source in
    one of the structured forms (:data:`_FORMS`, and the guide picture's)
    names what the part's basis names, in the same form -- so a
    junction_source rewritten apart from its basis fails here. The basis
    itself is held to the archive
    (test_every_stated_junction_basis_is_what_the_archive_shows).

    **Free prose is not checked**, by form: *"the whole <Area> was a junction
    before the split ..."* (a split never removes its area's junction),
    *"a bomb site (...): the part holds positions of <Area>"* (Nuke's lower
    floor), *"his name for the whole of <Area> ..."*, and a junction_source
    that quotes his words or a document (*"his words '...'"*,
    *"alueomistus-pohja ..."*, *"vastaus-...md section ..."*)."""
    table = _table()[_map_of(key)]
    checked = 0
    for part in table[_PART_SPLITS[key][0]].split.parts:
        read = _reading(part.junction_source or "")
        if read is None:
            continue
        assert read == _reading(_stated_basis(part)), (part.callout, read)
        checked += 1
    assert checked == _SOURCE_FORMS_CHECKED[key], checked


def test_the_parts_his_words_make_junctions_are_a_closed_set() -> None:
    found = set()
    for map_name, table in _table().items():
        for entry in table.values():
            for part in entry.split.parts if entry.split else []:
                # A part with no basis (Nuke's yard, Story 4.17) has no
                # his-word one either.
                match = _BASIS.search(part.source)
                if match and _his_junction().match(match[1]):
                    found.add((map_name, part.callout))
    assert found == _HIS_JUNCTIONS


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


def _band_rule_of_the_header() -> tuple[float, float, int]:
    """The band rule's three numbers, read from the callouts.toml header --
    the one place the rule is written (Story 4.23)."""
    from test_cell_split import _header_text

    rule = re.search(
        r"each cut gap is at least (\d+) units .*? and at least ([\d.]+) times "
        r"the largest gap not cut; the levels between the cuts must then each "
        r"hold at least (\d+) ticks",
        _header_text(),
    )
    assert rule, "the header states no band rule"
    return float(rule[1]), float(rule[2]), int(rule[3])


#: The band rule of the callouts.toml header: a cut gap at least this many
#: units (twice the game's 18-unit step) and this many times the largest gap
#: not cut, and at least this many ticks on every level.
BAND_MIN_GAP, BAND_MIN_RATIO, BAND_MIN_TICKS = _band_rule_of_the_header()


def _levels(z) -> tuple[bool, list[tuple[float, float, float]], list[list[float]]]:
    """The header's band rule on heights (extended in Story 4.23): whether
    they show two levels or more, the cut gaps as (low end, high end,
    midpoint) bottom to top, and the levels between the cuts. The FEWEST of
    the largest gaps are cut such that each is at least BAND_MIN_GAP and
    BAND_MIN_RATIO times the largest gap not cut; every level then needs
    BAND_MIN_TICKS. With one cut this is Story 4.20's two-level rule."""
    values = sorted(float(v) for v in z)
    gaps = sorted(
        ((b - a, a, b) for a, b in zip(values, values[1:])), reverse=True
    )
    for k in range(1, len(gaps) + 1):
        smallest = gaps[k - 1][0]
        if smallest < BAND_MIN_GAP:
            break
        uncut = gaps[k][0] if k < len(gaps) else 0.0
        if smallest >= BAND_MIN_RATIO * uncut:
            cuts = sorted((a, b, (a + b) / 2) for _, a, b in gaps[:k])
            bounds = [-INF, *(mid for _, _, mid in cuts), INF]
            levels = [
                [v for v in values if low <= v < high]
                for low, high in zip(bounds, bounds[1:])
            ]
            return all(len(lv) >= BAND_MIN_TICKS for lv in levels), cuts, levels
    return False, [], [values]


def test_the_band_rule_reads_the_header_and_keeps_the_two_level_rule() -> None:
    """The numbers are the header's; one cut is Story 4.20's rule (5 ticks a
    side, 36 units, 1.5 times the second gap); and where the largest gap
    alone fails, the fewest cuts that pass are taken."""
    assert (BAND_MIN_GAP, BAND_MIN_RATIO, BAND_MIN_TICKS) == (36.0, 1.5, 5)
    passes, cuts, _ = _levels([0.0] * 5 + [100.0] * 5)
    assert passes and [mid for _, _, mid in cuts] == [50.0]
    assert not _levels([0.0] * 4 + [100.0] * 5)[0]
    assert not _levels([0.0] * 5 + [35.0] * 5)[0]
    three = [0.0] * 5 + [100.0] * 5 + [190.0] * 5 + [230.0] * 5
    passes, cuts, levels = _levels(three)
    assert passes and [mid for _, _, mid in cuts] == [50.0, 145.0]
    assert [len(lv) for lv in levels] == [5, 5, 10]
    # Exactly 1.5 times the largest gap not cut is enough: 150 against 100.
    passes, cuts, _ = _levels([0.0] * 5 + [150.0] * 3 + [250.0] * 3)
    assert passes and [mid for _, _, mid in cuts] == [75.0]
    assert not _levels([0.0] * 5 + [149.0] * 3 + [249.0] * 3)[0]
    # Two equal gaps with a lone tick between: the fewest cuts leave a
    # level of one tick, so the place fails.
    assert not _levels([0.0] * 5 + [100.0] + [200.0] * 5)[0]


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


def _band_statement(source: str, edge: float) -> str:
    """The part of a source that states one band edge: from "band edge
    <edge> is its midpoint" to the next band's statement or the end, so one
    band cannot read another band's words."""
    at = source.index(f"band edge {edge} is its midpoint")
    following = source.find("band edge ", at + 1)
    return source[at : following if following != -1 else len(source)]


#: The bands the band rule admits, per map, as (part, half-cell or
#: crossing): every place he names by height that passes the rule on the
#: archive (zbands-mitattu-2026-09-28.md). The archive test holds the table
#: to exactly this set, so a band removed or added fails there as well as
#: in the unit tests -- and the rule is checked on each.
_BANDED = {
    "de_inferno": {
        ("short boost", ("I11c", "I11d", "I12a", "I12b")),
        ("bridge", "E11c"), ("bridge", "E12a"),
        # Story 4.28 (inferno-taulukot-2026-10-02.md section 3): ykkonen's
        # box top, carried to kakkonen's corners, and the window frames.
        ("ykkönen", ("G5d", "G6b", "G6a", "G5c")),
        ("kakkonen", "G6a"), ("kakkonen", "G5c"),
        ("short boost", "J12a"),
    },
    "de_anubis": {
        ("mid doors window", "F7d"), ("mid doors window", "G7c"),
        ("bridge", "F7a"), ("bridge", "F7c"), ("bridge", "F8a"), ("bridge", "F8c"),
    },
    "de_dust2": {
        ("short a", "G3b"), ("short a", "G3d"), ("short a", "H3b"),
        ("ct spawn", "G3b"), ("ct spawn", "G3d"),
    },
    # Story 4.23: his hutLadder and hattuhylly, measured 2026-09-29; since
    # Story 4.24 the shelf is his in I11a too, its band carried there.
    "de_nuke": {
        ("hutladder", "I11a"), ("hattuhylly", "I10c"), ("hattuhylly", "I11c"),
        ("hattuhylly", "I11a"),
    },
    # Story 4.24: b rafters over the site floor (bsite-taulukot-2026-09-29.md
    # section 3); since Story 4.25 his whole U with the top tenth of row 7,
    # the ring's band carried to D6d by his word.
    "de_nuke.BombsiteB": {
        ("b rafters", cell)
        for cell in (
            "D4d", "D5b", "D5d", "D6b", "D6c", "D6d", "C6d",
            "C4c", "C5a", "C5c", "C6a", "C6c", "C7b", "D7a", "D7b",
        )
    },
    "de_nuke.Ramp": set(),
    "de_nuke.Tunnels": set(),
    "de_nuke.Observation": set(),
    # Story 4.26: short a's three bands are the same place's on BombsiteA's
    # split; no other place of the Dust2 sites passes the rule with two
    # names (dust2-taulukot-2026-10-01.md section 3).
    "de_dust2.BombsiteA": {("short a", "G3b"), ("short a", "G3d"), ("short a", "H3b")},
    "de_dust2.ARamp": set(),
    "de_dust2.ExtendedA": set(),
    "de_dust2.LongA": set(),
    # Review round 1 (A3): the window, whole C2b and C2d above its edge.
    "de_dust2.BombsiteB": {
        ("window", "C2b"), ("window", "C2d"),
        # His answer of 2026-10-02: the stack's top over B2d and C2c.
        ("double stack", "B2d"), ("double stack", "C2c"),
    },
    "de_dust2.BDoors": {("window", "C2b"), ("window", "C2d")},
    "de_dust2.MidDoors": set(),
    # Story 4.27: boosti, his high level only ("Vain korkea taso"), is the
    # one Ancient site place that passes the rule
    # (ancient-taulukot-2026-10-01.md section 3).
    "de_ancient.BombsiteA": {("boosti", "D6a"), ("boosti", "D6b")},
    "de_ancient.BombsiteB": set(),
    "de_ancient.TSideLower": set(),
    "de_ancient.Alley": set(),
    "de_ancient.SideEntrance": set(),
    "de_inferno.Pit": set(),
    "de_inferno.Ruins": set(),
}


@pytest.mark.archive
@pytest.mark.parametrize("key", sorted(_PART_SPLITS))
def test_every_height_band_passes_the_band_rule_at_its_largest_gap(
    key: str,
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
    boost: Quad), the source must say the direction rests on his words.

    **Since Story 4.24** a band is measured on its split's floor only, and a
    band carried to a half-cell by his word (the header's carried band,
    :func:`_carried_statement`) is measured
    on the half-cells the rule admits; the carried half-cells must be his
    own words' (``_check_his_words`` in ``test_cell_split.py``). A half-open
    band whose edge is a closed band's edge is read at the closed band's
    measurement, below."""
    root = require_parsed()
    split = _table()[_map_of(key)][_PART_SPLITS[key][0]].split
    ticks = _on_floor(_live_ticks(root, _map_of(key)), split)
    banded = {
        (p.callout, r.cell or tuple(r.crossing))
        for p in split.parts
        for r in p.regions
        if r.z is not None
    }
    assert banded == _BANDED[key], banded ^ _BANDED[key]
    if not banded:
        return
    edges: dict[float, list] = {}
    parts: dict[float, set] = {}
    closed: dict[str, tuple[float, float]] = {}
    for part in split.parts:
        for band in {r.z for r in part.regions if r.z is not None}:
            rects = _measured_rects(split, part, band)
            own = _measured_rects(split, part, band, carried=True)
            if own:
                # A band is carried only where the half-cell's own ticks
                # cannot give it (the header's carried band): measured there
                # alone, no finite edge of the band is a cut. hutLadder's
                # I11a cuts only at -337.36 (the ladder's foot, Story 4.23).
                _, own_cuts, _ = _levels(_in_rects(ticks, split, own)["z"])
                for edge in band:
                    assert not any(
                        abs(edge - mid) < 0.01 for _, _, mid in own_cuts
                    ), (part.callout, edge, own_cuts)
            for edge in band:
                if edge in (INF, -INF):
                    continue
                edges.setdefault(edge, []).extend(rects)
                parts.setdefault(edge, set()).add(part.callout)
                # Each banded part says which level it is, and it must be
                # the side of the edge its own band lies on -- or, for a
                # closed band (Story 4.23), the middle.
                if INF not in band and -INF not in band:
                    closed[part.callout] = band
                    side = "middle"
                else:
                    side = "upper" if band[0] == edge else "lower"
                assert f"is the {side} level" in part.source, (part.callout, side)
    assert edges, key
    by_callout = {p.callout: p for p in split.parts}
    # A BAND CARRIED FROM ANOTHER PART'S REGION (the header; the architect
    # review): a part with no region of its own to measure its band on takes
    # the edge another part's region gives, and its source names that part
    # as the one it is measured on.
    for part in split.parts:
        for band in {r.z for r in part.regions if r.z is not None}:
            if _measured_rects(split, part, band):
                continue
            for edge in band:
                if edge in (INF, -INF):
                    continue
                givers = {
                    other.callout
                    for other in split.parts
                    if other is not part
                    and any(
                        edge in b and _measured_rects(split, other, b)
                        for b in {r.z for r in other.regions if r.z is not None}
                    )
                }
                assert givers, (part.callout, edge)
                assert any(f"measured on {g}'s" in part.source for g in givers), (
                    part.callout, edge, givers
                )
    closed_edges = {edge for band in closed.values() for edge in band}
    if closed_edges:
        # The header's clause (Story 4.24), read where it is written.
        _header_clause(
            r"A HALF-OPEN BAND WHOSE EDGE IS A CLOSED BAND'S EDGE is read at "
            r"the closed band's measurement"
        )
    for edge, rects in edges.items():
        place = _in_rects(ticks, split, rects)
        passes, cuts, _ = _levels(place["z"])
        assert passes, (edge, cuts)
        assert any(
            low < edge < high and abs(edge - mid) < 0.01 for low, high, mid in cuts
        ), (edge, cuts)
        if edge in closed_edges:
            # The closed band's levels, checked below, are this edge's too
            # (the ladder above the shelf, Story 4.24).
            continue
        # A half-open band is Story 4.20's: exactly one cut, so the extended
        # rule re-derives it unchanged.
        assert len(cuts) == 1, (edge, cuts)
        for side, rows in (
            ("upper", place.filter(pl.col("z") >= edge)),
            ("lower", place.filter(pl.col("z") < edge)),
        ):
            top = rows.group_by("area").len().sort("len", descending=True).row(0)
            majority = top[0]
            for callout in parts[edge]:
                statement = _band_statement(by_callout[callout].source, edge)
                pattern = (
                    r"The upper level is the game's (\w+)"
                    if side == "upper"
                    else r"the lower the game's (\w+)"
                ) + r"(?: \((\d+) of (\d+))?"
                stated = re.search(pattern, statement)
                if stated:
                    assert stated[1] == majority, (callout, side, majority)
                    # Story 4.25 (review round 1): the counts a source
                    # states, "(n of m", are the level's, re-derived.
                    if stated[2] is not None:
                        assert (int(stated[2]), int(stated[3])) == (
                            top[1], rows.height
                        ), (callout, side, top[1], rows.height)
                elif side == "upper" or "direction rests on his words" not in statement:
                    # Every band names the game area of both its levels,
                    # except where one area holds both and his words decide.
                    raise AssertionError((callout, f"names no {side} level"))
        above = place.filter(pl.col("z") >= edge).group_by("area").len().sort("len")["area"][-1]
        below = place.filter(pl.col("z") < edge).group_by("area").len().sort("len")["area"][-1]
        if above == below:
            assert all(
                "direction rests on his words" in by_callout[c].source
                for c in parts[edge]
            ), edge
    for callout, band in closed.items():
        # A middle level (Story 4.23): its edges are two consecutive cuts,
        # and its source names every level's dominant game area, bottom to
        # top, with the counts the rule's levels hold.
        source = by_callout[callout].source
        place = _in_rects(
            ticks, split, _measured_rects(split, by_callout[callout], band)
        )
        _, cuts, _ = _levels(place["z"])
        mids = [round(mid, 2) for _, _, mid in cuts]
        assert any(
            (mids[i], mids[i + 1]) == band for i in range(len(mids) - 1)
        ), (callout, band, mids)
        bounds = [-INF, *(mid for _, _, mid in cuts), INF]
        found = []
        for low, high in zip(bounds, bounds[1:]):
            rows = place.filter((pl.col("z") >= low) & (pl.col("z") < high))
            top = rows.group_by("area").len().sort("len", descending=True).row(0)
            found.append((top[0], top[1], rows.height))
        stated = re.search(r"The levels, bottom to top, are the game's (.*?); ", source)
        assert stated, callout
        named = [
            (area, int(n), int(m))
            for area, n, m in re.findall(r"(\w+) \((\d+) of (\d+)", stated[1])
        ]
        assert named == found, (callout, found)
        if any(a[0] == b[0] for a, b in zip(found, found[1:])):
            assert "rests on his words" in source, callout


#: The places he names by height that the rule refuses a band, each with the
#: part whose source says so: (map, the part, half-cells, a y range of them).
#: With no half-cells given, the place is the part's own claim, its cells and
#: its regions. An entry on fewer than 10 ticks is LATENT UNTIL THE ARCHIVE
#: GROWS: a level needs 5, so it can hardly pass, and the test is a guard
#: for the day it might.
_ONE_LEVEL = {
    "balcony": ("de_inferno", "balcony", None, None),
    "H5d (ct boost's gap)": ("de_inferno", "ct boost", ["H5d"], (0.0, 1.0)),
    "underpass": ("de_inferno", "underpass", ["F11b", "F11d"], (0.0, 1.0)),
    "t balcony over F11d": ("de_inferno", "t balcony", ["F11d"], (0.0, 1.0)),
    "H3a's top edge (A28)": ("de_dust2", "short a", ["H3a"], (0.0, 0.25)),
    # Story 4.24: the vent's knee. (D6d, refused a band in Story 4.24,
    # carries the ring's since Story 4.25; that its rest alone cannot give
    # the ring's edge is checked where carried bands are, in
    # test_every_height_band_passes_the_band_rule_at_its_largest_gap.)
    "the vent's knee height": ("de_nuke.Tunnels", "vent", None, None),
    # Story 4.26: the Dust2 boxes he names by height, and the scaffold
    # against the slope (dust2-taulukot-2026-10-01.md section 3).
    "big box's corner, the box top alone": (  # 4 ticks: latent
        "de_dust2.BombsiteB", "big box", None, None
    ),
    "the B car's roof": ("de_dust2.BombsiteB", "b auto", None, None),
    "b boost's two boxes": (  # 8 ticks: latent until the archive grows
        "de_dust2.BombsiteB", "b boost", None, None
    ),
    "raksatelineet over b slope": ("de_dust2.BDoors", "raksatelineet", None, None),
    # Review round 1 (A5): goose's floor against the stairs and the site.
    "goose in I2b": ("de_dust2.BombsiteA", "goose", ["I2b"], (0.0, 1.0)),
    "goose in I2a": ("de_dust2.BombsiteA", "goose", ["I2a"], (0.0, 1.0)),
    # Story 4.27: the Ancient places he names by height that the rule
    # refuses (ancient-taulukot-2026-10-01.md section 3): the temple floor
    # against the ground he drops to, the site boxes, and the box at the
    # ramp's foot.
    "kynttilä's temple floor": ("de_ancient.BombsiteA", "kynttilä", None, None),
    "brokyssä below the temple in D4d": (
        "de_ancient.BombsiteA", "brokyssä", None, None
    ),
    "the site boxes in E5d": ("de_ancient.BombsiteA", "site boksit", None, None),
    "the box at the ramp's foot": (
        "de_ancient.BombsiteB", "ramp", ["K9a"], (0.0, 1.0)
    ),
    # Story 4.28: the Inferno boxes he names by height, the truck and the
    # fountain's rim (inferno-taulukot-2026-10-02.md section 3).
    "coffin's top": ("de_inferno", "coffin", None, None),
    "the fountain's rim": ("de_inferno", "fountain", None, None),
    "ykkos boksi's top": ("de_inferno", "ykkös boksi", None, None),
    "coldzera boksi's top": ("de_inferno", "coldzera boksi", None, None),
    "the truck's top": ("de_inferno", "truck", None, None),
}


@pytest.mark.archive
@pytest.mark.parametrize("place", sorted(_ONE_LEVEL))
def test_every_place_built_without_a_band_fails_the_band_rule(place: str) -> None:
    """The other direction: every place he names by height that is built
    without a band fails the rule on the archive, and its part's source says
    it fails. The balcony's place is its own regions."""
    key, callout, cells, y = _ONE_LEVEL[place]
    split = _table()[_map_of(key)][_PART_SPLITS[key][0]].split
    part = next(p for p in split.parts if p.callout == callout)
    if cells is None:
        rects = [_half_cell(split, c) for c in part.cells] + [
            split._region_rect(part, r) for r in part.regions
        ]
    else:
        rects = [_half_cell(split, c, y) for c in cells]
    ticks = _on_floor(_live_ticks(require_parsed(), _map_of(key)), split)
    place_ticks = _in_rects(ticks, split, rects)
    assert not _levels(place_ticks["z"])[0], place
    assert "fails the band rule" in part.source or "NOT BUILT" in part.source, place


@pytest.mark.archive
@pytest.mark.parametrize("key", sorted(_PART_SPLITS))
def test_every_stated_junction_basis_is_what_the_archive_shows(key: str) -> None:
    """The basis each source states, re-derived: *holds positions of A, B*
    is exactly the split areas whose positions the lookup gives the part;
    *its region lies in the game's X (n of the m live ticks)* is the most
    common game area among the map's live ticks the part's own regions take,
    with those counts; *no live tick of the map lies in it* is none."""
    root = require_parsed()
    table = _table()[_map_of(key)]
    split = table[_PART_SPLITS[key][0]].split
    ticks = _on_floor(_live_ticks(root, _map_of(key)), split)
    holds: dict[str, set] = {}
    taken: dict[tuple[str, str], int] = {}
    for area in _PART_SPLITS[key]:
        area_split = table[area].split
        for x, y, z in ticks.filter(pl.col("area") == area).select("x", "y", "z").iter_rows():
            part = area_split.part_at(x, y, z)
            if part is not None:  # the rest of a lobby-like area (Story 4.23)
                holds.setdefault(part.callout, set()).add(area)
                taken[(part.callout, area)] = taken.get((part.callout, area), 0) + 1
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
        his = _his_junction().match(basis)
        if his:
            # His word decides the flag; what it holds is still the archive's.
            basis = "holds positions of " + basis[his.end():]
        # Story 4.26 (review round 1): every "takes N <Area> positions" a
        # source states about this split's own area is the lookup's count.
        for n, area in re.findall(r"\btakes (\d+) (\w+) positions?\b", part.source):
            if area in _PART_SPLITS[key]:
                found = taken.get((part.callout, area), 0)
                assert found == int(n), (part.callout, area, found)
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


@pytest.mark.archive
def test_the_a_cars_roof_passes_the_rule_and_carries_no_band_by_decision() -> None:
    """Story 4.26, the lead's decision #12: the band rule passes on his A car
    -- J3d, J4b and J4d's top quarter -- but the roof and the ground beside
    it are both his auto, so no band is built. The edge the source states is
    re-derived here, so the claim cannot outlive the archive."""
    split = _table()["de_dust2"]["LongA"].split
    auto = next(p for p in split.parts if p.callout == "a auto")
    assert all(r.z is None for r in auto.regions)
    rects = [_half_cell(split, c) for c in auto.cells] + [
        split._region_rect(auto, r) for r in auto.regions
    ]
    ticks = _on_floor(_live_ticks(require_parsed(), "de_dust2"), split)
    passes, cuts, levels = _levels(_in_rects(ticks, split, rects)["z"])
    assert passes and [round(mid, 2) for _, _, mid in cuts] == [22.33]
    assert [len(level) for level in levels] == [24, 5]
    assert "edge 22.33" in auto.source and "no band is built" in auto.source
    # The measurement the source writes out, re-derived (review round 1).
    ((low, high, _),) = cuts
    stated = (
        f"({len(levels[0])} ticks at z {min(levels[0]):.0f}..{max(levels[0]):.0f}, "
        f"{len(levels[1])} at {min(levels[1]):.0f}..{max(levels[1]):.0f}, the largest "
        f"gap {high - low:.2f} from {low:.2f} to {high:.2f}"
    )
    assert stated in auto.source, stated
