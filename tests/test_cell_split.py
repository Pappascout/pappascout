"""Story 4.17: a coarse game area split by position into the product owner's
callouts -- Nuke's yard, half-cell by half-cell on his guide image's grid.

The shipped cases are built from the table in ``src/pappascout/callouts.toml``
(its grid, fit, half-cells and boxes), never from values copied here: a
position is made by inverting the table's own fit at a half-cell's centre, so
the tests follow the table if it is corrected. The loader's refusals and the
nearest rule are checked on small hand-written splits whose answers can be
read off the text.
"""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

import pytest

from conftest import REAL_SETTINGS
from pappascout.domain.aggregate import (
    DEATH_AREA_COLUMNS,
    TICK_AREA_COLUMNS,
    _junction_path,
    _route_place,
    named_rows,
    places_for,
)
from pappascout.domain.models import (
    CALLOUT_TABLE_PATH,
    SPLIT_GRID_FIELDS,
    CellPart,
    CellRegion,
    CellSplit,
    load_callouts,
    load_settings,
)
from pappascout.errors import SettingsError
from pappascout.render import view as view_module
from pappascout.stages.aggregate import (
    HASHED_PART_FIELDS,
    HASHED_REGION_FIELDS,
    HASHED_SPLIT_FIELDS,
    _params_hash,
)

_POOL = load_settings(REAL_SETTINGS, env_files=()).league.map_pool


def _shipped() -> dict:
    return load_callouts(_POOL)["de_nuke"]


def _split() -> CellSplit:
    split = _shipped()["Outside"].split
    assert split is not None
    return split


def _every_split() -> dict[str, tuple[dict, CellSplit]]:
    """Every shipped split as ``"<map>.<area>"`` -> (its map's table, the
    split): Nuke's yard (Story 4.17) and Ancient's three (Story 4.18). A
    test of what every split must satisfy runs on each."""
    return {
        f"{map_name}.{area}": (table, entry.split)
        for map_name, table in load_callouts(_POOL).items()
        for area, entry in table.items()
        if entry.split is not None
    }


#: The shipped splits by key, for ``parametrize``.
SPLITS = sorted(_every_split())


def _at_pixel(split: CellSplit, px: float, py: float) -> tuple[float, float, float]:
    """A game position whose projection is ``(px, py)``, on the fitted floor
    (at any height where the split has no floor cut)."""
    return (
        (px - split.fit.bx) / split.fit.sx,
        (split.fit.by - py) / split.fit.sy,
        (split.zmin if split.zmin is not None else 0.0) + 100.0,
    )


def _centre(split: CellSplit, name: str) -> tuple[float, float]:
    """A half-cell's centre in image pixels, from the table's grid and the
    naming his answer defines (``nuke-piha-vastaus-2026-09-27.md``: a
    top-left, b top-right, c bottom-left, d bottom-right) -- read here on
    its own and not through :meth:`CellSplit.index_of`, so a lookup that
    misreads a quarter cannot agree with itself."""
    letter, row, quarter = name[0], int(name[1:-1]), name[-1]
    right = quarter in "bd"
    bottom = quarter in "cd"
    return (
        split.origin[0]
        + split.cell[0] * split.columns.index(letter)
        + split.cell[0] / 2 * (right + 0.5),
        split.origin[1]
        + split.cell[1] * (row - 1)
        + split.cell[1] / 2 * (bottom + 0.5),
    )


def _in_a_box(split: CellSplit, px: float, py: float) -> bool:
    return any(
        b[0] <= px < b[2] and b[1] <= py < b[3]
        for part in split.parts
        for b in part.boxes
    )


# -- The cell lookup ---------------------------------------------------------


@pytest.mark.parametrize("key", SPLITS)
def test_every_named_half_cell_places_its_centre_under_its_callout(
    key: str,
) -> None:
    """Each half-cell a table names, at its centre, is counted under the
    row that names it -- unless another part's box overrides that spot.
    postimerkki's box straddles the corner of four half-cells and reaches
    exactly one of their centres (K12d's, its top-left corner: a box is
    half-open like a half-cell), so on Nuke one centre is the box's and
    every other is checked. dig's box on Ancient reaches H8d's centre the
    same way, but H8d is dig's own, so nothing there is overridden."""
    _, split = _every_split()[key]
    checked = 0
    boxed = []
    for part in split.parts:
        for name in part.cells:
            px, py = _centre(split, name)
            if any(
                b[0] <= px < b[2] and b[1] <= py < b[3]
                for other in split.parts
                if other is not part
                for b in other.boxes
            ):
                boxed.append(name)
                continue
            # Since Story 4.20 a finer part earlier in the table may hold the
            # centre (the broad part names the half-cell too); the claimant
            # is read by the test's own geometry.
            x, y, z = _at_pixel(split, px, py)
            assert split.part_at(x, y, z) is _claimant(split, px, py, z), name
            checked += 1
    assert boxed == (["K12d"] if key == "de_nuke.Outside" else [])
    assert checked == sum(len(part.cells) for part in split.parts) - len(boxed)


def _half_cell_rect(split: CellSplit, name: str) -> tuple[float, ...]:
    cx, cy = _centre(split, name)
    w, h = split.cell[0] / 2, split.cell[1] / 2
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _region_rect(split: CellSplit, region) -> tuple[float, ...]:
    """A region's rectangle as the test reads the table: a fraction of the
    half-cell from :func:`_centre`, or a box ``size`` half-cells wide on the
    one corner every named half-cell shares -- not through the model."""
    w, h = split.cell[0] / 2, split.cell[1] / 2
    if region.cell is not None:
        x0, y0, _, _ = _half_cell_rect(split, region.cell)
        return (
            x0 + region.x[0] * w, y0 + region.y[0] * h,
            x0 + region.x[1] * w, y0 + region.y[1] * h,
        )
    corners = None
    for name in region.crossing:
        x0, y0, x1, y1 = _half_cell_rect(split, name)
        these = {
            (round(x, 6), round(y, 6)) for x in (x0, x1) for y in (y0, y1)
        }
        corners = these if corners is None else corners & these
    (px, py), = corners
    size = region.size
    return (px - size * w / 2, py - size * h / 2, px + size * w / 2, py + size * h / 2)


def _claimant(split: CellSplit, px: float, py: float, z: float) -> CellPart | None:
    """The part the table gives a spot, read in table order by the test:
    a box first, then the first part whose half-cell or region (and band)
    holds it; ``None`` where the nearest rule decides."""
    for part in split.parts:
        if any(b[0] <= px < b[2] and b[1] <= py < b[3] for b in part.boxes):
            return part
    for part in split.parts:
        rects = [(_half_cell_rect(split, n), None) for n in part.cells] + [
            (_region_rect(split, r), r.z) for r in part.regions
        ]
        for (x0, y0, x1, y1), band in rects:
            inside = band is None or band[0] <= z < band[1]
            if x0 <= px < x1 and y0 <= py < y1 and inside:
                return part
    return None


def _spots(split: CellSplit, part: CellPart) -> list[tuple[float, float, float]]:
    """A game position at the centre of each half-cell and region of a part,
    at a height inside the region's band."""
    spots = [
        _at_pixel(split, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in part.boxes
    ]
    for name in part.cells:
        spots.append(_at_pixel(split, *_centre(split, name)))
    for region in part.regions:
        x0, y0, x1, y1 = _region_rect(split, region)
        x, y, z = _at_pixel(split, (x0 + x1) / 2, (y0 + y1) / 2)
        if region.z is not None:
            low, high = region.z
            z = high - 1.0 if low == float("-inf") else low + 1.0
        spots.append((x, y, z))
    return spots


@pytest.mark.parametrize("key", SPLITS)
def test_every_region_places_its_centre_under_the_first_part_that_claims_it(
    key: str,
) -> None:
    """Story 4.20: at each half-cell's and region's centre, inside its band,
    the position is counted under the first part in table order that claims
    the spot -- and **every part wins at least one of its own spots**, so a
    part the finer ones before it cover completely (a table in the wrong
    order) fails here by name."""
    _, split = _every_split()[key]
    for part in split.parts:
        won = 0
        for x, y, z in _spots(split, part):
            px, py = split.pixel(x, y)
            expected = _claimant(split, px, py, z)
            assert expected is not None, part.callout
            assert split.part_at(x, y, z) is expected, part.callout
            won += expected is part
        assert won, part.callout


def test_a_half_cell_name_round_trips_and_a_foreign_one_is_refused() -> None:
    """``K13b`` is column K's right half, row 13's top half; a name off the
    grid (column Q, row 18, quarter e) is refused, not placed."""
    split = _split()
    for name in ("A1a", "K13b", "P17d", "F11c"):
        assert split.name_of(split.index_of(name)) == name
    column = split.columns.index("K")
    assert split.index_of("K13b") == (2 * column + 1, 2 * 12)
    for name in ("Q1a", f"A{split.rows + 1}a", "K13e", "k13b", "K0a"):
        with pytest.raises(ValueError, match="not a half-cell"):
            split.index_of(name)


def test_a_position_below_the_floor_or_without_coordinates_is_not_split() -> None:
    """The image draws Nuke's upper floor; below ``zmin``, or with a
    coordinate missing, the split has no answer and the area's coarse name
    stands."""
    split = _split()
    assert split.zmin is not None  # Nuke's fit is of the upper floor only
    x, y, _ = _at_pixel(split, *_centre(split, "I14b"))
    assert split.part_at(x, y, split.zmin - 1.0) is None
    assert split.part_at(x, y, split.zmin).callout == "t red"
    assert split.part_at(None, y, 0.0) is None
    assert split.part_at(x, y, None) is None


# -- The override boxes ------------------------------------------------------


def _box_of(callout: str) -> tuple[float, float, float, float]:
    (box,) = next(p for p in _split().parts if p.callout == callout).boxes
    return box


def test_postimerkki_is_the_top_edge_where_k13b_and_l13a_meet() -> None:
    """His words: *"K13b ja L13a ruutujen yläreunan risteyskohta"* -- a
    point on an edge. The box is centred on that point, where the K/L column
    border meets row 13's top edge, and straddles it as ct box straddles its
    own: half a half-cell to each side and above and below -- read from the
    table's own grid (review item 10)."""
    split = _split()
    x0, y0, x1, y1 = _box_of("postimerkki")
    column_l = split.columns.index("L")
    border = split.origin[0] + split.cell[0] * column_l
    top = split.origin[1] + split.cell[1] * 12
    assert (x0 + x1) / 2 == pytest.approx(border)
    assert (y0 + y1) / 2 == pytest.approx(top)
    assert x1 - x0 == pytest.approx(split.cell[0] / 2)
    assert y1 - y0 == pytest.approx(split.cell[1] / 2)


def test_ct_box_straddles_the_edge_where_m10c_and_m11a_meet() -> None:
    """His words: *"CT box on enemmänkin M10c ja M11a risteyksessä"*: the box
    spans the two half-cells' shared edge and reaches into each."""
    split = _split()
    x0, y0, x1, y1 = _box_of("ct box")
    column_m = split.columns.index("M")
    edge = split.origin[1] + split.cell[1] * 10
    assert y0 < edge < y1
    assert x0 == pytest.approx(split.origin[0] + split.cell[0] * column_m)
    assert x1 == pytest.approx(x0 + split.cell[0] / 2)


@pytest.mark.parametrize(
    ("box_callout", "beside", "beneath"),
    [("postimerkki", "K13b", "ct red"), ("postimerkki", "L13a", "garage")],
)
def test_a_box_overrides_the_half_cell_beneath_it_and_no_further(
    box_callout: str, beside: str, beneath: str
) -> None:
    """Inside the box: the box's callout. In the same half-cell below the
    box: the half-cell's own ("K13b ... loput CT rediä", "L13a loput
    garagea")."""
    split = _split()
    x0, y0, x1, y1 = _box_of(box_callout)
    assert split.part_at(
        *_at_pixel(split, (x0 + x1) / 2, (y0 + y1) / 2)
    ).callout == box_callout
    cx, _ = _centre(split, beside)
    bottom = split.origin[1] + split.cell[1] / 2 * (split.index_of(beside)[1] + 1)
    assert split.part_at(*_at_pixel(split, cx, bottom - 0.5)).callout == beneath


def test_ct_box_overrides_the_nearest_rule_it_sits_in() -> None:
    """M10c and M11a are named by nobody but the box, so outside it they
    inherit from their nearest named half-cell; inside it they are ct box."""
    split = _split()
    x0, y0, x1, y1 = _box_of("ct box")
    assert split.part_at(
        *_at_pixel(split, (x0 + x1) / 2, (y0 + y1) / 2)
    ).callout == "ct box"
    cx, _ = _centre(split, "M10c")
    top_of_m10c = split.origin[1] + split.cell[1] * 9.5 + 0.5
    assert not _in_a_box(split, cx, top_of_m10c)
    assert split.part_at(*_at_pixel(split, cx, top_of_m10c)).callout == (
        split.inherited["M10c"]
    )


# -- The nearest rule --------------------------------------------------------


def _toml_split(parts: str, area_extra: str = "") -> str:
    return (
        '[de_nuke.Outside]\ncallout = "outside"\ncoarse = true\n'
        'junction = false\nneighbours = 0\nconfidence = "stated"\n'
        f'source = "a test"\n{area_extra}\n'
        "[de_nuke.Outside.split]\n"
        'image = "a.png"\nzmin = -500.0\norigin = [0.0, 0.0]\n'
        'cell = [20.0, 10.0]\ncolumns = "ABCD"\nrows = 4\nsource = "a test"\n'
        "[de_nuke.Outside.split.fit]\nsx = 1.0\nsy = 1.0\nbx = 0.0\nby = 0.0\n"
        + parts
    )


def _part(callout: str, cells: str = "", boxes: str = "") -> str:
    body = f'\n[[de_nuke.Outside.split.parts]]\ncallout = "{callout}"\n'
    if cells:
        body += f"cells = [{cells}]\n"
    if boxes:
        body += f"boxes = [{boxes}]\n"
    return body + 'junction = false\nconfidence = "stated"\nsource = "a test"\n'


def _load(tmp_path: Path, text: str) -> CellSplit:
    path = tmp_path / "callouts.toml"
    path.write_text(text, encoding="utf-8")
    return load_callouts(["de_nuke"], path)["de_nuke"]["Outside"].split


def test_an_unnamed_half_cell_takes_the_nearest_named_one_by_centre(
    tmp_path: Path,
) -> None:
    """Half-cells are 10 x 5 px here (cells 20 x 10), and the distance is
    between centres in pixels, so the grid's own proportions count. Named:
    *west* A1a (centre 5, 2.5), *east* B1a (25, 2.5), *south* A3a (5, 22.5).

    * A1c (5, 7.5): 5 px from *west*, far from the others -> west;
    * A2c (5, 17.5): 5 px from *south*, 15 px from *west* -> south;
    * B2a (25, 12.5): 10 px from *east*, 22.4 px from the others -> east;
    * A1b (15, 2.5): 10 px from *west* and 10 px from *east*, a tie, which
      goes to the part first in the table -- and to *east* when the table
      lists *east* first.
    """
    parts = (
        _part("west", '"A1a"') + _part("east", '"B1a"') + _part("south", '"A3a"')
    )
    split = _load(tmp_path, _toml_split(parts))
    inherited = split.inherited
    assert inherited["A1c"] == "west"
    assert inherited["A2c"] == "south"
    assert inherited["B2a"] == "east"
    assert inherited["A1b"] == "west"
    named = {"A1a", "B1a", "A3a"}
    assert set(inherited) | named == {
        split.name_of((c, r)) for c in range(8) for r in range(8)
    }
    assert not set(inherited) & named
    swapped = _load(
        tmp_path,
        _toml_split(
            _part("east", '"B1a"') + _part("west", '"A1a"') + _part("south", '"A3a"')
        ),
    )
    assert swapped.inherited["A1b"] == "east"


def test_a_position_off_the_grid_takes_the_nearest_named_half_cell(
    tmp_path: Path,
) -> None:
    """The rule is the same off the image: a position far left of column A
    takes the nearest named half-cell, not an error."""
    split = _load(
        tmp_path, _toml_split(_part("west", '"A1a"') + _part("east", '"D4d"'))
    )
    assert split.part_at(-500.0, -2.0, 0.0).callout == "west"
    assert split.part_at(900.0, -80.0, 0.0).callout == "east"


@pytest.mark.parametrize("key", SPLITS)
def test_the_shipped_inheritance_covers_the_grid_without_a_named_half_cell(
    key: str,
) -> None:
    """*"Liitä loput lähimpään"* on Nuke and *"mainitsematon lähimmällä"* on
    Ancient, written out: every half-cell of the grid is named by the table
    or inherited, never both."""
    _, split = _every_split()[key]
    named = {name for part in split.parts for name in part.cells}
    inherited = set(split.inherited)
    assert not named & inherited
    assert named | inherited == {
        split.name_of((c, r))
        for c in range(2 * len(split.columns))
        for r in range(2 * split.rows)
    }
    assert set(split.inherited.values()) <= {part.callout for part in split.parts}


# -- The loader's refusals ---------------------------------------------------


@pytest.mark.parametrize(
    ("parts", "extra", "message"),
    [
        (_part("a", '"A1a"') + _part("b", '"A1a"'), "", "claimed by both"),
        (_part("a", '"E1a"'), "", "not a half-cell"),
        (_part("a", '"A5a"'), "", "not a half-cell"),
        (_part("a", '"A1a"') + _part("a", '"B1a"'), "", "more than one row"),
        (
            _part("a", boxes="[0.0, 0.0, 5.0, 5.0]")
            + _part("b", boxes="[4.0, 4.0, 9.0, 9.0]"),
            "",
            "overlap",
        ),
        (_part("a", boxes="[5.0, 0.0, 5.0, 5.0]"), "", "x0 < x1"),
        (_part("a"), "", "neither a half-cell nor a box"),
        (_part("outside", '"A1a"'), "", "whole area's own coarse name"),
    ],
)
def test_a_split_that_would_give_a_position_two_answers_is_refused(
    tmp_path: Path, parts: str, extra: str, message: str
) -> None:
    with pytest.raises(SettingsError, match=message):
        _load(tmp_path, _toml_split(parts, extra))


def test_only_a_coarse_area_is_split(tmp_path: Path) -> None:
    text = _toml_split(_part("a", '"A1a"')).replace("coarse = true\n", "")
    with pytest.raises(SettingsError, match="not coarse"):
        _load(tmp_path, text)


def test_a_part_must_agree_with_the_area_of_its_name(tmp_path: Path) -> None:
    """A part named like another area is that place, so it must agree on
    junction -- as two areas of one callout must."""
    other = (
        '\n[de_nuke.Mini]\ncallout = "main"\njunction = true\n'
        'junction_source = "his words"\nneighbours = 0\n'
        'confidence = "stated"\nsource = "a test"\n'
    )
    path = tmp_path / "callouts.toml"
    path.write_text(_toml_split(_part("main", '"A1a"')) + other, encoding="utf-8")
    with pytest.raises(SettingsError, match="disagree on junction"):
        load_callouts(["de_nuke"], path)


def test_the_shipped_table_claims_no_half_cell_twice_and_names_his_places() -> (
    None
):
    """The Ask First guard of the spec, on the shipped table: no half-cell in
    two rows, and every name of his list as the loader normalises it."""
    split = _split()
    cells = [name for part in split.parts for name in part.cells]
    assert len(cells) == len(set(cells))
    assert [part.callout for part in split.parts] == [
        " ".join(name.split()).lower()
        for name in (
            "t spawn", "outsideLobby", "Tladder", "Toutside", "kontakti",
            "t red", "glaive", "main", "mainin takana", "cross", "secret top",
            "secret entrance", "secret stairs", "porattava", "ct red",
            "postimerkki", "garage", "garagen takana", "ct piha",
            "hellin portaat", "ct box", "lockers", "hell",
        )
    ]
    for part in split.parts:
        assert part.confidence == "stated", part.callout
        assert "nuke-piha-vastaus-2026-09-27.md" in part.source, part.callout


def test_a_part_named_like_an_area_is_that_place() -> None:
    """main, garage, lockers, hell and t spawn are existing Nuke places: the
    part agrees with the area on junction, so the counts merge."""
    table = _shipped()
    by_callout = {e.callout: e for e in table.values() if e.callout}
    shared = [p for p in _split().parts if p.callout in by_callout]
    assert {p.callout for p in shared} == {
        "main", "garage", "lockers", "hell", "t spawn"
    }
    for part in shared:
        assert part.junction == by_callout[part.callout].junction, part.callout


def test_the_junctions_among_the_new_places_are_his_contested_ones() -> None:
    """kontakti, t red, ct red and ct piha are junctions; every other new
    place is transit (spec decision 9)."""
    table = _shipped()
    existing = {e.callout for e in table.values() if e.callout}
    new = {p.callout: p.junction for p in _split().parts if p.callout not in existing}
    assert {name for name, junction in new.items() if junction} == {
        "kontakti", "t red", "ct red", "ct piha"
    }


# -- The split reaches the statistics and the routes -------------------------


def _row_at(split: CellSplit, half_cell: str, **extra) -> dict:
    x, y, z = _at_pixel(split, *_centre(split, half_cell))
    return {"area": "Outside", "x": x, "y": y, "z": z, **extra}


@pytest.mark.parametrize("key", SPLITS)
def test_a_statistic_and_a_route_step_name_one_position_alike(key: str) -> None:
    """The one lookup: a tick renamed for the statistics and the same
    position as a route step carry the same callout, for every part the
    table names by half-cell, on every shipped split."""
    table, split = _every_split()[key]
    area = key.split(".")[1]
    for part in split.parts:
        # Since Story 4.20 every region too, inside its band; the answer is
        # the spot's first claimant, which is not always the part itself.
        for x, y, z in _spots(split, part):
            row = {"area": area, "x": x, "y": y, "z": z}
            (named,) = named_rows([row], TICK_AREA_COLUMNS, table)
            position = (row["x"], row["y"], row["z"])
            tokens, _ = _junction_path(
                [6.0], {6.0: (area, position)}, None, table
            )
            px, py = split.pixel(x, y)
            expected = _claimant(split, px, py, z).callout
            assert named["area"] == tokens[0].label == expected, part.callout
            assert row["area"] == area  # a copy: the rules' row unmoved


def test_a_death_is_split_at_the_victims_and_the_attackers_own_positions() -> None:
    """Deaths carry two positions; each area column is read with its own."""
    table = _shipped()
    split = _split()
    victim = _row_at(split, "J14c")
    attacker = _row_at(split, "L11c")
    row = {
        "victim_area": "Outside",
        "victim_x": victim["x"], "victim_y": victim["y"], "victim_z": victim["z"],
        "attacker_area": "Outside",
        "attacker_x": attacker["x"], "attacker_y": attacker["y"],
        "attacker_z": attacker["z"],
    }
    (named,) = named_rows([row], DEATH_AREA_COLUMNS, table)
    assert (named["victim_area"], named["attacker_area"]) == ("t red", "ct piha")
    tokens, _ = _junction_path(
        [6.0, 9.0],
        {6.0: ("Outside", (attacker["x"], attacker["y"], attacker["z"]))},
        ("Outside", 8.0, (victim["x"], victim["y"], victim["z"])),
        table,
    )
    assert [(t.fate, t.label) for t in tokens] == [
        ("seen", "ct piha"), ("died", "t red")
    ]


def test_a_part_flagged_by_its_confidence_is_marked_like_an_area(
    tmp_path: Path,
) -> None:
    """A part that is only a guess prints ``inferred``, and a part is never
    coarse."""
    text = _toml_split(_part("a", '"A1a"')).replace(
        'callout = "a"\ncells = ["A1a"]\njunction = false\nconfidence = "stated"',
        'callout = "a"\ncells = ["A1a"]\njunction = false\nconfidence = "guess"',
    )
    path = tmp_path / "callouts.toml"
    path.write_text(text, encoding="utf-8")
    table = load_callouts(["de_nuke"], path)["de_nuke"]
    place = _route_place("Outside", table, frozenset({"a"}), (1.0, -1.0, 0.0))
    assert (place.label, place.flag) == ("a", "inferred")
    (outside,) = places_for(table, ["Outside"])
    assert [(p.callout, p.flag) for p in outside.parts] == [("a", "inferred")]


# -- The rules still read the whole area -------------------------------------


def test_a_rule_row_on_the_split_area_prints_its_coarse_name() -> None:
    """A rule measured the whole ``Outside``, so its row names the whole
    area: *outside (karkea)*. Without a position the lookup gives the same
    name, and so does a statistic row that carries none."""
    table = _shipped()
    (outside,) = [p for p in places_for(table, []) if p.area == "Outside"]
    assert (outside.callout, outside.flag) == ("outside", "coarse")
    assert [p.callout for p in outside.parts] == [
        p.callout for p in _split().parts
    ]
    place = _route_place("Outside", table)
    assert (place.label, place.flag) == ("outside", "coarse")
    (named,) = named_rows([{"area": "Outside"}], TICK_AREA_COLUMNS, table)
    assert named["area"] == "outside"
    places = view_module._Places(
        map_name="de_nuke",
        by_area={p.area: p for p in places_for(table, [])},
        flag_of={},
        merged=frozenset(),
    )
    assert places.ruled("Outside", view_module._Flags()) == (
        f"outside{view_module.ROUTE_COARSE_MARK}"
    )


# -- The parameter hash ------------------------------------------------------



def _digest(callouts: dict) -> str:
    settings = load_settings(REAL_SETTINGS, env_files=())
    return _params_hash(
        settings.thresholds, settings.league, settings.aggregate, callouts
    )


def _with_split(table: dict, split: CellSplit) -> dict:
    entry = table["de_nuke"]["Outside"]
    return {"de_nuke": {"Outside": entry.model_copy(update={"split": split})}}


def _with_part(table: dict, **update) -> dict:
    split = table["de_nuke"]["Outside"].split
    parts = [split.parts[0].model_copy(update=update), *split.parts[1:]]
    return _with_split(table, split.model_copy(update={"parts": parts}))


def _hash_table(tmp_path: Path, parts: str) -> dict:
    path = tmp_path / "callouts.toml"
    path.write_text(_toml_split(parts), encoding="utf-8")
    return load_callouts(["de_nuke"], path)


def test_every_hashed_split_and_part_field_moves_the_hash(tmp_path: Path) -> None:
    """Each field of :data:`HASHED_SPLIT_FIELDS` and :data:`HASHED_PART_FIELDS`
    moves which callout a position is counted under, so each re-runs
    ``aggregate`` (AD-3) -- one variant per field, and the variants must
    name exactly the hashed fields, so a field dropped from a list fails
    here by name."""
    table = _hash_table(
        tmp_path, _part("a", '"A1a"') + _part("b", boxes="[9.0, 9.0, 12.0, 12.0]")
    )
    split = table["de_nuke"]["Outside"].split
    same = _digest(table)
    split_variants = {
        "fit": {"fit": split.fit.model_copy(update={"sx": 1.5})},
        "zmin": {"zmin": -400.0},
        "origin": {"origin": (1.0, 0.0)},
        "cell": {"cell": (20.0, 11.0)},
        "columns": {"columns": "ABCE"},
        "rows": {"rows": 5},
    }
    part_variants = {
        "callout": {"callout": "c"},
        "cells": {"cells": ["A1b"]},
        "boxes": {"boxes": [(1.0, 1.0, 2.0, 2.0)]},
        "regions": {"regions": [CellRegion(cell="A2a", words="w")]},
        "broad": {"broad": True},
        "junction": {"junction": True, "junction_source": "his words"},
        "confidence": {"confidence": "guess"},
    }
    assert set(split_variants) == set(HASHED_SPLIT_FIELDS)
    assert set(part_variants) == set(HASHED_PART_FIELDS)
    for field, update in split_variants.items():
        moved = _with_split(table, split.model_copy(update=update))
        assert _digest(moved) != same, field
    for field, update in part_variants.items():
        assert _digest(_with_part(table, **update)) != same, field
    # A region's geometry and band are hashed, its words are not (Story 4.20).
    base = CellRegion(cell="A2a", words="w")
    with_region = _with_part(table, regions=[base])
    region_variants = {
        "cell": {"cell": "A2b"},
        "crossing": {"cell": None, "crossing": ["A1d", "B2a"]},
        "x": {"x": (0.0, 0.5)},
        "y": {"y": (0.5, 1.0)},
        "size": {"cell": None, "crossing": ["A1d", "B2a"], "size": 1.0},
        "z": {"z": (0.0, 10.0)},
    }
    assert set(region_variants) == set(HASHED_REGION_FIELDS)
    assert set(CellRegion.model_fields) == set(HASHED_REGION_FIELDS) | {"words"}
    for field, update in region_variants.items():
        moved = _with_part(table, regions=[base.model_copy(update=update)])
        if field == "size":
            crossing = _with_part(
                table, regions=[base.model_copy(update=region_variants["crossing"])]
            )
            assert _digest(moved) != _digest(crossing), field
            continue
        assert _digest(moved) != _digest(with_region), field
    reworded = _with_part(table, regions=[base.model_copy(update={"words": "v"})])
    assert _digest(reworded) == _digest(with_region)


def test_no_provenance_of_a_split_moves_the_hash(tmp_path: Path) -> None:
    """The split's image and source, and a part's source, note and
    junction_source, are provenance: editing them does not re-run
    ``aggregate``. Every field of the two models is either hashed or listed
    here, so a new field has to be placed on one side."""
    table = _hash_table(
        tmp_path,
        _part("a", '"A1a"').replace(
            "junction = false", 'junction = true\njunction_source = "his words"'
        ),
    )
    split = table["de_nuke"]["Outside"].split
    same = _digest(table)
    split_provenance = {"image": "b.png", "source": "another"}
    part_provenance = {
        "source": "another",
        "note": "a note",
        "junction_source": "other words",
    }
    assert set(CellSplit.model_fields) == (
        set(HASHED_SPLIT_FIELDS) | set(split_provenance) | {"parts"}
    )
    assert set(CellPart.model_fields) == (
        set(HASHED_PART_FIELDS) | set(part_provenance) | {"coarse"}
    )
    for field, value in split_provenance.items():
        moved = _with_split(table, split.model_copy(update={field: value}))
        assert _digest(moved) == same, field
    for field, value in part_provenance.items():
        assert _digest(_with_part(table, **{field: value})) == same, field


# -- The table's cells are his words (review item B1) ------------------------


#: A half-cell as his tables write it, with his range form ``K11a–d``.
_WRITTEN = re.compile(r"\b([A-P])([1-9][0-9]*)([a-d])(?:–([a-d]))?\b")
#: A table row a part's source quotes: ``section '<section>', row '<row>'``.
#: Story 4.17's rows are in nuke-piha-vastaus's FINAL table, Story 4.18's in
#: the tables of puoliruudut-vastaus.
_ROW = re.compile(r"section '([^']*)', row '([^']*)'")


def _quoted_rows(source: str) -> list[tuple[str, str]]:
    """Every table row a part's source quotes, as (callout, the half-cells
    written), in the order quoted.

    Two table shapes are quoted. ``callout | half-cells`` is a table of
    places (``ct red | K12b, ...``; ``T-kuutio (his *split*) | D10b, ...``,
    whose parenthesis is the table's gloss and not the name). ``half-cell |
    was | **callout** ...`` is Story 4.18's Nuke table of the inherited
    half-cells he named, the name in bold.
    """
    rows = []
    for _, row in _ROW.findall(source):
        columns = row.split(" | ")
        if len(columns) == 3:
            bold = re.search(r"\*\*([^*]+)\*\*", columns[2])
            assert bold, row
            rows.append((bold[1], columns[0]))
        else:
            assert len(columns) == 2, row
            callout = re.sub(r"\s*\(.*\)\s*$", "", columns[0])
            rows.append((callout, columns[1]))
    assert rows, source
    return rows


def _written_cells(text: str) -> list[str]:
    cells = []
    for letter, row, first, last in _WRITTEN.findall(text):
        span = "abcd"["abcd".index(first) : "abcd".index(last or first) + 1]
        cells.extend(f"{letter}{row}{quarter}" for quarter in span)
    return cells


@pytest.mark.parametrize("key", SPLITS)
def test_every_parts_cells_are_the_half_cells_its_quoted_row_names(
    key: str,
) -> None:
    """Each part's source quotes its table rows verbatim, and the half-cells
    those rows name, in order, begin with exactly the part's ``cells`` -- so
    a typo in a half-cell has to be made twice, in the words and in the
    list. A part with a box names, after or instead of its cells, the
    half-cells the box meets (postimerkki's K13b and L13a, dig's H9b and
    I9a): every one of them must share area with a box of the part, and a
    part names such half-cells exactly when it has a box. Every quoted row's
    callout is the part's."""
    _, split = _every_split()[key]
    for part in split.parts:
        if not _ROW.search(part.source):
            # Story 4.20: his verbatim answer, quoted -- not a table row.
            _check_his_words(split, part)
            continue
        named = []
        for callout, written in _quoted_rows(part.source):
            assert " ".join(callout.split()).lower() == part.callout, callout
            named.extend(_written_cells(written))
        assert named[: len(part.cells)] == part.cells, part.callout
        rest = named[len(part.cells) :]
        assert bool(rest) == bool(part.boxes), part.callout
        for name in rest:
            column, index = split.index_of(name)
            x0 = split.origin[0] + split.cell[0] / 2 * column
            y0 = split.origin[1] + split.cell[1] / 2 * index
            assert any(
                box[0] < x0 + split.cell[0] / 2 and x0 < box[2]
                and box[1] < y0 + split.cell[1] / 2 and y0 < box[3]
                for box in part.boxes
            ), name


#: His words a Story 4.20 source quotes: ``his words: '<verbatim>'``.
_HIS_WORDS = re.compile(r"his words: '([^']*)'")
#: A reading the source states: ``read '<as written>' as <half-cells>``.
_READ_AS = re.compile(
    r"read '([^']*)' as ([A-P][0-9]+[a-d](?:, [A-P][0-9]+[a-d])*)"
)
#: A half-cell as his free text writes it: any case (*h5d*, *H12B*), glued
#: to a Finnish ending (*E8b:hen*, *G11dja*), with his range form *F9a-d*.
_FREE_CELL = re.compile(
    r"(?<![A-Za-z0-9])([A-Pa-p])([1-9][0-9]*)([a-dA-D])(?:[-–]([a-d]))?(?![0-9])"
)
def _header_text() -> str:
    """The ``callouts.toml`` header as one line of prose, for a test that
    reads a rule from the one place it is written."""
    return " ".join(
        line.lstrip("#").strip()
        for line in CALLOUT_TABLE_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    )


def _edge_of_the_header() -> float:
    """The lead's size for an edge he did not quantify (A1), read from the
    one place it is written -- the header of ``callouts.toml`` -- so the
    test and the table cannot hold two values."""
    header = _header_text()
    rule = re.search(
        r"an edge he does not quantify \(yläreuna, alareuna, oikea reuna\) "
        r"is (\d+)/(\d+) of the half-cell on that side",
        header,
    )
    assert rule, "the header states no edge size"
    return int(rule[1]) / int(rule[2])


#: The lead's sizes where he gave none (A1, A2; callouts.toml header).
EDGE = _edge_of_the_header()
_WHOLE = (0.0, 1.0)
#: His fraction words -> (x, y) of the half-cell, y from the top; the first
#: listed term found in a region's words decides, so a longer term is
#: listed before a shorter one inside it. Each term is his own, from
#: puoliruudut-vastaus-2-2026-09-28.md; where he gave no size, EDGE or a
#: half by half corner (his own corner size, "puolet korkeudesta ja
#: leveydestä").
_FRACTION_WORDS: tuple[tuple[str, tuple[tuple[float, float], ...]], ...] = (
    # A3: "yläreuna crossia ja alapuolisko yläbanaania" -- the top is the
    # half the bottom half leaves (the lead's reading, not the edge rule).
    ("yläreuna crossia ja alapuolisko", (_WHOLE, (0.0, 0.5))),
    # His answer to the review tie: "E8a:n oikea reuna on canalia ja vasen
    # connectoria" -- the right is the half the left half leaves (the
    # lead's reading, not the edge rule).
    ("oikea reuna on canalia ja vasen", ((0.5, 1.0), _WHOLE)),
    ("yläpuolisko", (_WHOLE, (0.0, 0.5))),
    ("viidesosa ruudun oikeasta reunasta", ((0.8, 1.0), _WHOLE)),
    ("oikea reuna kuten h11d", ((0.8, 1.0), _WHOLE)),
    ("neljäsosa ruudusta", ((0.0, 0.25), _WHOLE)),
    ("oikean reunan kolmannes", ((2 / 3, 1.0), _WHOLE)),
    ("vasemman reunan kuudennes", ((0.0, 1 / 6), _WHOLE)),
    ("alin kuudennes", (_WHOLE, (5 / 6, 1.0))),
    ("alin kolmasosa", (_WHOLE, (2 / 3, 1.0))),
    ("yläreunan kolmannes", (_WHOLE, (0.0, 1 / 3))),
    ("ylin viidesosa", (_WHOLE, (0.0, 0.2))),
    # A27: his "vasen nurkka", read as the bottom-left.
    ("puolet korkeudesta ja leveydestä", ((0.0, 0.5), (0.5, 1.0))),
    ("vasemmassa alanurkassa", ((0.0, 0.5), (0.5, 1.0))),
    ("vasen yläkulma", ((0.0, 0.5), (0.0, 0.5))),
    ("vasen ylänurkka", ((0.0, 0.5), (0.0, 0.5))),
    ("oikea ylänurkka", ((0.5, 1.0), (0.0, 0.5))),
    ("yläoikeasta reunasta", ((0.5, 1.0), (0.0, 0.5))),
    ("oikea alanurkka", ((0.5, 1.0), (0.5, 1.0))),
    ("oikea alareuna", ((0.5, 1.0), (0.5, 1.0))),
    ("alaoikea nurkka", ((0.5, 1.0), (0.5, 1.0))),
    ("oikea puoli noin puolesta välistä", ((0.5, 1.0), _WHOLE)),
    ("oikeat puoliskot", ((0.5, 1.0), _WHOLE)),
    ("vasen puolisko", ((0.0, 0.5), _WHOLE)),
    ("vasen puoli", ((0.0, 0.5), _WHOLE)),
    ("alempi puolisko", (_WHOLE, (0.5, 1.0))),
    ("alapuolisko", (_WHOLE, (0.5, 1.0))),
    ("ylempi puolisko", (_WHOLE, (0.0, 0.5))),
    ("alareunat", (_WHOLE, (1 - EDGE, 1.0))),
    ("alareuna", (_WHOLE, (1 - EDGE, 1.0))),
    ("alin reuna", (_WHOLE, (1 - EDGE, 1.0))),
    ("yläreuna", (_WHOLE, (0.0, EDGE))),
    ("oikea reuna", ((1 - EDGE, 1.0), _WHOLE)),
)
#: Words that say the height separates the place.
_HEIGHT_WORDS = (
    "z koordinaat", "korkeammalla", "korkeampi", "alempana", "päällä", "buust"
)


def _free_cells(text: str) -> list[str]:
    cells = []
    for letter, row, first, last in _FREE_CELL.findall(text):
        first = first.lower()
        span = "abcd"["abcd".index(first) : "abcd".index(last or first) + 1]
        cells.extend(f"{letter.upper()}{row}{quarter}" for quarter in span)
    return cells


def _read(text: str, readings: list[tuple[str, str]]) -> str:
    for written, cells in readings:
        text = text.replace(written, f" {cells} ")
    return text


def _fraction_of(words: str) -> tuple[tuple[float, float], tuple[float, float]]:
    lower = words.lower()
    for term, (x, y) in _FRACTION_WORDS:
        if term in lower:
            return x, y
    return _WHOLE, _WHOLE


def _samples(x: tuple[float, float], y: tuple[float, float]) -> set:
    """The sample points of a fraction on a 60 x 60 lattice of the half-cell
    -- 60 is divisible by every size he used (1/2 ... 1/6) and by EDGE."""
    return {
        (i, j)
        for i in range(60)
        for j in range(60)
        if x[0] * 60 <= i + 0.5 < x[1] * 60 and y[0] * 60 <= j + 0.5 < y[1] * 60
    }


def _check_his_words(split: CellSplit, part: CellPart) -> None:
    """A Story 4.20 part held to his verbatim words, quoted in its source:

    * the half-cells his quoted words write (after the readings the source
      states, ``read '12d' as I2d``) are **exactly** the half-cells the part
      holds -- whole, in a fraction or in a crossing;
    * every region's ``words`` are a phrase of those quotes and write the
      region's own half-cell;
    * a fraction is the one his words say (:data:`_FRACTION_WORDS`, the
      lead's rule where he gave no size), and *mutta ei* (but not) makes it
      the rest of the half-cell -- checked on the union of the part's
      regions with those words;
    * a crossing is his *risteys*, and a box of one half-cell where he says
      *yhden ruudun kokoinen* (the size is required, with no default);
    * a height band stands on his words about height, and its finite edge
      is written in the source beside the measurement it comes from;
    * the part's callout is in his quoted words, or the source says the
      lead named it (``Named '<callout>' by the lead``).

    **The limit, stated** (the one Story 4.18 accepted for cells): his words
    exist twice in the repo only as the quote inside ``callouts.toml``, so a
    fraction is held to that quote and not to his answer document, which
    lives outside the repository. A misquote made identically in the quote
    and the geometry passes here.
    """
    quotes = _HIS_WORDS.findall(part.source)
    assert quotes, part.callout
    assert part.callout in " ".join(quotes).lower() or (
        f"Named '{part.callout}' by the lead" in part.source
    ), part.callout
    readings = _READ_AS.findall(part.source)
    for written, _ in readings:
        assert any(written in quote for quote in quotes), (part.callout, written)
    written_cells = {c for q in quotes for c in _free_cells(_read(q, readings))}
    held = set(part.cells)
    for region in part.regions:
        held |= {region.cell} if region.cell else set(region.crossing)
    assert written_cells == held, (part.callout, written_cells ^ held)
    groups: dict[tuple[str, str], list] = {}
    for region in part.regions:
        assert any(region.words in quote for quote in quotes), region.words
        named = set(_free_cells(_read(region.words, readings)))
        if region.crossing is not None:
            assert set(region.crossing) <= named, region.words
            assert "risteys" in region.words, region.words
            if "yhden ruudun kokoinen" in region.words:
                assert region.size == 1.0, region.words
        else:
            assert region.cell in named, (region.cell, region.words)
            groups.setdefault((region.cell, region.words), []).append(region)
        if region.z is not None:
            assert any(
                term in quote for quote in quotes for term in _HEIGHT_WORDS
            ), part.callout
            assert "zbands-mitattu-2026-09-28.md" in part.source, part.callout
            for edge in region.z:
                if edge not in (float("inf"), float("-inf")):
                    assert f"{edge}" in part.source, (part.callout, edge)
    for (cell, words), regions in groups.items():
        x, y = _fraction_of(words)
        expected = _samples(x, y)
        if "mutta ei" in words:
            expected = _samples(_WHOLE, _WHOLE) - expected
        actual = set().union(*(_samples(r.x, r.y) for r in regions))
        assert actual == expected, (part.callout, cell, words)


def test_the_word_reader_reads_his_free_text() -> None:
    """The readers :func:`_check_his_words` trusts, on his own spellings."""
    assert _free_cells("F9a-d, h5d:ssä, H12B, G11dja E8b:hen, 12d, I0d") == [
        "F9a", "F9b", "F9c", "F9d", "H5d", "H12b", "G11d", "E8b"
    ]
    assert _read("I3b ja 12d oikean", [("12d", "I2d")]).split() == [
        "I3b", "ja", "I2d", "oikean"
    ]
    assert _fraction_of("I9a oikea reuna miinus cubby osuus eli oikean "
                        "reunan kolmannes") == ((2 / 3, 1.0), _WHOLE)
    assert _fraction_of("H11d oikea reuna (ehkä viidesosa ruudun oikeasta "
                        "reunasta)") == ((0.8, 1.0), _WHOLE)
    assert _fraction_of("J12A (mutta vain oikea alareuna)") == (
        (0.5, 1.0), (0.5, 1.0)
    )
    assert _fraction_of("E12a kohdissa secondin päällä") == (_WHOLE, _WHOLE)
    assert len(_samples((0.0, EDGE), _WHOLE)) == 15 * 60


def test_the_row_reader_reads_his_range_form() -> None:
    """``K11a–d`` is four half-cells; the reader is what the test above
    trusts, so it is checked on its own."""
    assert _written_cells("K11a–d, L10c") == [
        "K11a", "K11b", "K11c", "K11d", "L10c"
    ]
    assert _written_cells("K13b (except its top edge)") == ["K13b"]
    assert _quoted_rows(
        "x section 'S', row 'T-kuutio (his *split*) | D10b, E10a'; and "
        "section 'N', row 'G12b | tladder | **toutside** (a gloss)'"
    ) == [("T-kuutio", "D10b, E10a"), ("toutside", "G12b")]


# -- Robustness (review items 7 and 9) ---------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_coordinate_is_no_position(bad: float) -> None:
    """NaN or an infinity in x, y or z is read like a missing coordinate:
    the coarse name stands, and nothing raises."""
    split = _split()
    x, y, z = _at_pixel(split, *_centre(split, "I14b"))
    assert split.part_at(bad, y, z) is None
    assert split.part_at(x, bad, z) is None
    assert split.part_at(x, y, bad) is None
    place = _route_place("Outside", _shipped(), frozenset(), (x, y, bad))
    assert place.label == "outside"


def test_a_half_cell_with_a_leading_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="not a half-cell"):
        _split().index_of("K013b")


@pytest.mark.parametrize("columns", ["ABCA", "abcd", "AB1D"])
def test_the_grid_columns_are_distinct_capital_letters(
    tmp_path: Path, columns: str
) -> None:
    text = _toml_split(_part("a", '"B1a"')).replace(
        'columns = "ABCD"', f'columns = "{columns}"'
    )
    with pytest.raises(SettingsError, match="distinct capital letters"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    "box",
    [
        "[70.0, 30.0, 90.0, 35.0]",
        "[-1.0, 0.0, 5.0, 5.0]",
        "[0.0, 30.0, 5.0, 41.0]",
        "[0.0, -2.0, 5.0, 5.0]",
    ],
)
def test_a_box_outside_the_grid_is_refused(tmp_path: Path, box: str) -> None:
    """The grid of :func:`_toml_split` is 80 x 40 px from (0, 0)."""
    with pytest.raises(SettingsError, match="outside the grid"):
        _load(tmp_path, _toml_split(_part("a", boxes=box)))


# -- Validators (review item B5) ---------------------------------------------


@pytest.mark.parametrize("box", ["[0.0, 5.0, 5.0, 5.0]", "[0.0, 6.0, 5.0, 5.0]"])
def test_a_box_whose_height_is_not_positive_is_refused(
    tmp_path: Path, box: str
) -> None:
    with pytest.raises(SettingsError, match="y0 < y1"):
        _load(tmp_path, _toml_split(_part("a", boxes=box)))


@pytest.mark.parametrize(
    ("one", "two"),
    [
        ("[0.0, 0.0, 5.0, 5.0]", "[2.0, 6.0, 4.0, 9.0]"),  # x overlaps only
        ("[0.0, 0.0, 5.0, 5.0]", "[6.0, 2.0, 9.0, 4.0]"),  # y overlaps only
        ("[0.0, 0.0, 5.0, 5.0]", "[5.0, 0.0, 9.0, 5.0]"),  # they touch
    ],
)
def test_boxes_that_share_no_area_load(tmp_path: Path, one: str, two: str) -> None:
    """Two boxes overlap only when they overlap on **both** axes."""
    split = _load(
        tmp_path, _toml_split(_part("a", boxes=one) + _part("b", boxes=two))
    )
    assert [p.callout for p in split.parts] == ["a", "b"]


@pytest.mark.parametrize(
    ("find", "replace"),
    [
        ("junction = false", "junction = true"),
        ("junction = false", 'junction = false\njunction_source = "his words"'),
    ],
)
def test_a_parts_junction_and_its_source_come_together(
    tmp_path: Path, find: str, replace: str
) -> None:
    text = _toml_split(_part("a", '"A1a"').replace(find, replace))
    with pytest.raises(SettingsError, match="junction and junction_source"):
        _load(tmp_path, text)


# -- The nearest metric (review item B6) -------------------------------------


def test_the_nearest_is_by_straight_line_distance(tmp_path: Path) -> None:
    """Half-cells are 10 x 5 px. From A1a's centre (5, 2.5), *down* A2c
    (5, 17.5) is 15 px away by either metric; *diagonal* A2b (15, 12.5) is
    14.1 px in a straight line but 20 px by Manhattan. The straight line
    wins, although *down* comes first in the table."""
    split = _load(
        tmp_path,
        _toml_split(_part("down", '"A2c"') + _part("diagonal", '"A2b"')),
    )
    assert split.inherited["A1a"] == "diagonal"


# -- The statistics reach events and deaths through the build (item B4) ------


def test_a_utility_event_and_a_death_in_the_yard_get_part_names() -> None:
    """Through ``build_report`` and not through :func:`named_rows`: the
    call sites pass each table's rows with their positions, so a throw, a
    detonation, a first death and a kill in ``Outside`` are counted under
    his yard callouts, each at its own position."""
    from test_aggregate import (
        OPPONENT,
        TEAM,
        branch,
        classified_row,
        death_row,
        event_rows,
        report_for,
        tick_row,
    )

    split = _split()

    def at(name: str) -> tuple[float, float, float]:
        return _at_pixel(split, *_centre(split, name))

    demo = "Nuke_vs_a"
    throw, target = event_rows(
        demo, 1, 1, "smoke", throw_area="Outside", detonate_area="Outside"
    )
    for event, name in ((throw, "G12a"), (target, "J13c")):
        event["x"], event["y"], event["z"] = at(name)
    victim = death_row(demo, 1, victim_area="Outside", attacker_area="Outside")
    kill = death_row(
        demo,
        1,
        victim="o1",
        victim_lineup=OPPONENT,
        victim_side="CT",
        victim_area="Outside",
        attacker="p1",
        attacker_lineup=TEAM,
        attacker_side="T",
        attacker_area="Outside",
        t_s=30.0,
    )
    for row, name, prefix in (
        (victim, "I14b", "victim"),
        (victim, "L11c", "attacker"),
        (kill, "K13d", "victim"),
        (kill, "H13d", "attacker"),
    ):
        row[f"{prefix}_x"], row[f"{prefix}_y"], row[f"{prefix}_z"] = at(name)
    report = report_for(
        [classified_row(demo, 1)],
        [tick_row(demo, 1, "p1", "Lobby")],
        events=[throw, target],
        deaths=[victim, kill],
        callouts={"de_nuke": _shipped()},
    )
    entry = branch(report, "de_nuke", "T", "pistol")
    assert [(use.throw_area, use.detonate_area) for use in entry.utility] == [
        ("toutside", "glaive")
    ]
    assert [area.area for area in entry.deaths.first_death_areas] == ["t red"]
    assert [area.area for area in entry.deaths.kills] == ["kontakti"]


@pytest.mark.parametrize("key", SPLITS)
def test_a_tie_on_the_shipped_grid_goes_to_the_table_order(key: str) -> None:
    """Every inherited half-cell of the shipped grid takes, among the named
    half-cells at the least distance, the first in the table -- the
    distance computed here in exact fractions of the table's cell size, so
    a tie is a tie. Found in the Story 4.17 review: G12b lies 21.1 px from
    both *tladder* (G11d) and *toutside* (G12d), and subtracting float
    centres let rounding noise pick *toutside*; the table lists *tladder*
    first -- until Story 4.18, when he named G12b *toutside* himself."""
    _, split = _every_split()[key]
    width = Fraction(str(split.cell[0])) / 2
    height = Fraction(str(split.cell[1])) / 2
    named = [
        (split.index_of(name), part.callout)
        for part in split.parts
        for name in part.cells
    ]
    for name, callout in split.inherited.items():
        column, row = split.index_of(name)
        distances = [
            (((c - column) * width) ** 2 + ((r - row) * height) ** 2, owner)
            for (c, r), owner in named
        ]
        least = min(distance for distance, _ in distances)
        first = next(owner for distance, owner in distances if distance == least)
        assert callout == first, name


# -- Story 4.18: Ancient by his cells ----------------------------------------


def _ancient() -> dict:
    return load_callouts(_POOL)["de_ancient"]


#: His callouts per Ancient area as he wrote them
#: (``puoliruudut-vastaus-2026-09-27.md``), before the loader normalises them.
_ANCIENT_PARTS = {
    "MainHall": ("a main", "hall", "HallLeft"),
    "Outside": ("T-kuutio", "T-elbow", "Outside main"),
    "SideEntrance": ("ruins", "vent", "dig"),
}


def test_ancient_splits_name_his_places_and_his_two_confirmed_readings() -> None:
    """His names through the loader's normalisation (the hyphen is kept),
    each part sourced from puoliruudut-vastaus. The two readings that
    correct what he wrote -- D7a for his second C7a, D9a-d for his F9a-d --
    were ``inferred`` until he confirmed them on 2026-09-28 (Story 4.20);
    now every part is ``stated``."""
    table = _ancient()
    parts = [p for area in _ANCIENT_PARTS for p in table[area].split.parts]
    for area, names in _ANCIENT_PARTS.items():
        assert [p.callout for p in table[area].split.parts] == [
            " ".join(n.split()).lower() for n in names
        ], area
    for part in parts:
        assert "puoliruudut-vastaus-2026-09-27.md" in part.source
    # Story 4.20: he confirmed both readings on 2026-09-28 (the answer's
    # section 'Verbatim', answer 1), so both are stated and quote him.
    confirmed = {
        p.callout: p.source
        for p in parts
        if "puoliruudut-vastaus-2-2026-09-28.md section 'Verbatim', answer 1"
        in p.source
    }
    assert set(confirmed) == {"a main", "outside main"}
    assert "'D7a kyllä a mainissa'" in confirmed["a main"]
    assert "'Kyllä outside main on D9a-d ja C9b,C9d'" in confirmed["outside main"]
    assert not any("Read as an assumption" in p.source for p in parts)
    assert all(p.confidence == "stated" for p in parts)


def test_a_split_does_not_remove_its_areas_junction() -> None:
    """MainHall, Outside and SideEntrance were each a junction as a whole,
    and a split must not remove that (implementation lead, spec 4.18
    review): a route through hall would otherwise lose *a main*. So every
    part he names as a place is a junction, and each junction's source says
    so; the parts he describes as a way to somewhere -- Outside main
    (*"towards a main"*) and vent, the passage between ruins and dig --
    stay transit."""
    table = _ancient()
    for area in _ANCIENT_PARTS:
        assert table[area].junction, area
    parts = [p for area in _ANCIENT_PARTS for p in table[area].split.parts]
    assert {p.callout for p in parts if not p.junction} == {"outside main", "vent"}
    for part in parts:
        if part.junction:
            assert "a split does not remove its area's junction" in (
                part.junction_source
            ), part.callout


def test_mainhall_is_coarse_under_his_three_names_joined() -> None:
    """Only a coarse area is split, so MainHall became coarse. It has no
    guide claim, so its coarse name, for a rule row on the whole area, is
    its parts joined with '/'."""
    entry = _ancient()["MainHall"]
    assert entry.coarse and entry.junction
    assert entry.callout == "/".join(p.callout for p in entry.split.parts)


@pytest.mark.parametrize("area", sorted(_ANCIENT_PARTS))
def test_ancient_grid_is_grid2s(area: str) -> None:
    """``grid2.py``'s naming, reproduced from game positions. grid2 names a
    position by its pixel ``px = sx * x + bx``, ``py = -sy * y + by``, the
    half-cell ``ci = floor(px / (CW / 2))``, ``ri = floor(py / (CH / 2))``
    with ``CW = 425 * sx``, ``CH = 425 * sy``, and the name
    ``L[ci // 2] + (ri // 2 + 1) + ('ab', 'cd')[ri % 2][ci % 2]``. Here
    that formula is written out on its own, from the table's fit alone; the
    split is only asked, through :meth:`CellSplit.part_at`, which callout
    the same game position takes. Two positions per half-cell, near
    opposite corners, off the box. The answer must be the part that names
    grid2's half-cell in its ``cells`` as written, or else that half-cell's
    inheritance."""
    from math import floor

    split = _ancient()[area].split
    sx, sy, bx, by = split.fit.sx, split.fit.sy, split.fit.bx, split.fit.by
    half_w, half_h = 425 * sx / 2, 425 * sy / 2
    # The table's grid is grid2's, derived from the same fit: a cell rounded
    # apart from the fit, or a fit rounded apart from the cell, fails here.
    assert split.origin == (0.0, 0.0)
    assert split.cell == (2 * half_w, 2 * half_h)
    written = {name: part.callout for part in split.parts for name in part.cells}
    checked = 0
    for ci in range(2 * len(split.columns)):
        for ri in range(2 * split.rows):
            for fx, fy in ((0.1, 0.15), (0.9, 0.85)):
                x = (half_w * (ci + fx) - bx) / sx
                y = (by - half_h * (ri + fy)) / sy
                px, py = sx * x + bx, -sy * y + by
                if _in_a_box(split, px, py):
                    continue
                c, r = floor(px / half_w), floor(py / half_h)
                name = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[c // 2] + (
                    f"{r // 2 + 1}{('ab', 'cd')[r % 2][c % 2]}"
                )
                expected = written.get(name) or split.inherited[name]
                assert split.part_at(x, y, 0.0).callout == expected, name
                checked += 1
    assert checked > 4 * len(split.columns) * split.rows


@pytest.mark.parametrize("area", sorted(_ANCIENT_PARTS))
def test_the_ancient_grid_covers_the_image_exactly(area: str) -> None:
    """The grid's size is derived from the image, not trusted: its pixel
    size is in the file name (``kartta08_sivu8_1350x1350.png``), so the
    columns are ``ceil(width / cell_w)`` and the rows ``ceil(height /
    cell_h)`` -- the fewest whole cells that cover it, as grid2.py's
    pictures do. A row or a column dropped would leave the image's edge to
    the nearest rule instead of the half-cells he could name there.

    **Two limits, accepted.** A typo made identically in a part's ``cells``
    and in the row its source quotes cannot be caught here: the quote is the
    only copy of his table in the repo, and catching it needs an independent
    source (his answer document is outside the repo). And there is no check
    across the splits of one map for a half-cell used twice: each split
    divides its own game area, and one half-cell legitimately holds
    positions of two areas -- D9a and D9b are Outside main's by name in
    ``Outside`` and inherited as hall in ``MainHall``."""
    from math import ceil

    split = _ancient()[area].split
    size = re.search(r"_(\d+)x(\d+)\.png$", split.image)
    assert size, split.image
    width, height = int(size[1]), int(size[2])
    assert len(split.columns) == ceil(width / split.cell[0])
    assert split.rows == ceil(height / split.cell[1])


def test_dig_is_the_point_where_the_top_edges_of_h9b_and_i9a_meet() -> None:
    """His words: *"H9b ja I9a yläreunan risteyspiste"* -- as postimerkki,
    a box centred where the H/I column border meets row 9's top edge, half
    a half-cell to each side and above and below. Inside it: dig; in H9b
    and I9a below it: the nearest, which is dig's H8d and I8c."""
    split = _ancient()["SideEntrance"].split
    (dig,) = [p for p in split.parts if p.callout == "dig"]
    ((x0, y0, x1, y1),) = dig.boxes
    border = split.origin[0] + split.cell[0] * split.columns.index("I")
    top = split.origin[1] + split.cell[1] * 8
    assert (x0 + x1) / 2 == pytest.approx(border)
    assert (y0 + y1) / 2 == pytest.approx(top)
    assert x1 - x0 == pytest.approx(split.cell[0] / 2)
    assert y1 - y0 == pytest.approx(split.cell[1] / 2)
    assert split.part_at(*_at_pixel(split, border, top)).callout == "dig"
    assert split.inherited["H9b"] == split.inherited["I9a"] == "dig"


#: One change per field of :data:`SPLIT_GRID_FIELDS` to the split of
#: :func:`_toml_split`, each leaving a split that loads on its own.
_GRID_VARIANTS = {
    "image": ('image = "a.png"', 'image = "b.png"'),
    "fit": ("sx = 1.0", "sx = 1.5"),
    "origin": ("origin = [0.0, 0.0]", "origin = [1.0, 0.0]"),
    "cell": ("cell = [20.0, 10.0]", "cell = [20.0, 11.0]"),
    "columns": ('columns = "ABCD"', 'columns = "ABCE"'),
    "rows": ("rows = 4", "rows = 5"),
}


def test_every_grid_field_has_a_variant() -> None:
    assert set(_GRID_VARIANTS) == set(SPLIT_GRID_FIELDS)


@pytest.mark.parametrize("field", sorted(_GRID_VARIANTS))
def test_the_splits_of_one_map_are_refused_on_two_grids(
    tmp_path: Path, field: str
) -> None:
    """His half-cell names are read off one picture, so two splits of one
    map on different grids are refused -- here the same split copied onto
    a second area with one grid field changed; unchanged, the two load, and
    a different ``zmin`` loads too, because a floor cut belongs to one
    area."""
    one = _toml_split(_part("a", '"A1a"'))
    two = one.replace("de_nuke.Outside", "de_nuke.Yard")
    path = tmp_path / "callouts.toml"
    path.write_text(one + "\n" + two, encoding="utf-8")
    assert set(load_callouts(["de_nuke"], path)["de_nuke"]) == {"Outside", "Yard"}
    path.write_text(
        one + "\n" + two.replace("zmin = -500.0", "zmin = -400.0"),
        encoding="utf-8",
    )
    assert set(load_callouts(["de_nuke"], path)["de_nuke"]) == {"Outside", "Yard"}
    old, new = _GRID_VARIANTS[field]
    assert two.count(old) == 1
    path.write_text(one + "\n" + two.replace(old, new), encoding="utf-8")
    with pytest.raises(SettingsError, match="not on one grid"):
        load_callouts(["de_nuke"], path)


def test_a_split_with_no_floor_cut_places_a_position_at_any_height(
    tmp_path: Path,
) -> None:
    """Ancient's fit was made on every position, so its splits have no
    ``zmin``: a position is split whatever its height, and one with no
    coordinates still keeps the coarse name."""
    split = _load(
        tmp_path, _toml_split(_part("a", '"A1a"')).replace("zmin = -500.0\n", "")
    )
    assert split.zmin is None
    assert split.part_at(1.0, -1.0, -1e6).callout == "a"
    assert split.part_at(1.0, -1.0, None) is None
