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
    CellPart,
    CellSplit,
    load_callouts,
    load_settings,
)
from pappascout.errors import SettingsError
from pappascout.render import view as view_module
from pappascout.stages.aggregate import (
    HASHED_PART_FIELDS,
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


def _at_pixel(split: CellSplit, px: float, py: float) -> tuple[float, float, float]:
    """A game position whose projection is ``(px, py)``, on the fitted floor."""
    return (
        (px - split.fit.bx) / split.fit.sx,
        (split.fit.by - py) / split.fit.sy,
        split.zmin + 100.0,
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


def test_every_named_half_cell_places_its_centre_under_its_callout() -> None:
    """Each half-cell of the FINAL table, at its centre, is counted under the
    row that names it -- unless a box overrides that spot. postimerkki's box
    straddles the corner of four half-cells and reaches exactly one of their
    centres (K12d's, its top-left corner: a box is half-open like a
    half-cell), so one centre is the box's and every other is checked."""
    split = _split()
    checked = 0
    boxed = []
    for part in split.parts:
        for name in part.cells:
            px, py = _centre(split, name)
            if _in_a_box(split, px, py):
                boxed.append(name)
                continue
            assert split.part_at(*_at_pixel(split, px, py)).callout == (
                part.callout
            ), name
            checked += 1
    assert boxed == ["K12d"]
    assert checked == sum(len(part.cells) for part in split.parts) - 1


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


def test_the_shipped_inheritance_covers_the_grid_without_a_named_half_cell() -> None:
    """*"Liitä loput lähimpään"*, written out: every half-cell of the grid is
    named by the table or inherited, never both."""
    split = _split()
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


def test_a_statistic_and_a_route_step_name_one_position_alike() -> None:
    """The one lookup: a tick renamed for the statistics and the same
    position as a route step carry the same callout, for every part the
    table names by half-cell."""
    table = _shipped()
    split = _split()
    for part in split.parts:
        for name in part.cells:
            px, py = _centre(split, name)
            if _in_a_box(split, px, py):
                continue
            row = _row_at(split, name)
            (named,) = named_rows([row], TICK_AREA_COLUMNS, table)
            position = (row["x"], row["y"], row["z"])
            tokens, _ = _junction_path(
                [6.0], {6.0: ("Outside", position)}, None, table
            )
            assert named["area"] == tokens[0].label == part.callout, name
            assert row["area"] == "Outside"  # a copy: the rules' row unmoved


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


#: A half-cell as his FINAL table writes it, with his range form ``K11a–d``.
_WRITTEN = re.compile(r"\b([A-P])([1-9][0-9]*)([a-d])(?:–([a-d]))?\b")
_ROW = re.compile(r"'FINAL table \(source for Story 4\.17\)', row '([^']*)'")


def _quoted_row(source: str) -> str:
    """The FINAL-table row a part's source quotes: ``row '<row>'``."""
    match = _ROW.search(source)
    assert match, source
    return match[1]


def _written_cells(text: str) -> list[str]:
    cells = []
    for letter, row, first, last in _WRITTEN.findall(text):
        span = "abcd"["abcd".index(first) : "abcd".index(last or first) + 1]
        cells.extend(f"{letter}{row}{quarter}" for quarter in span)
    return cells


def test_every_parts_cells_are_the_half_cells_its_quoted_row_names() -> None:
    """Each part's source quotes its FINAL-table row verbatim, and the
    half-cells that row names are exactly the part's ``cells`` -- so a typo
    in a half-cell has to be made twice, in the words and in the list. A
    part held by a box names the half-cells the box meets instead: every one
    of them must share area with the box. The row's callout is the part's."""
    split = _split()
    for part in split.parts:
        row = _quoted_row(part.source)
        callout, _, written = row.partition(" | ")
        assert " ".join(callout.split()).lower() == part.callout, row
        named = _written_cells(written)
        if part.cells:
            assert named == part.cells, part.callout
            continue
        assert named, part.callout
        (box,) = part.boxes
        for name in named:
            column, index = split.index_of(name)
            x0 = split.origin[0] + split.cell[0] / 2 * column
            y0 = split.origin[1] + split.cell[1] / 2 * index
            assert box[0] < x0 + split.cell[0] / 2 and x0 < box[2], name
            assert box[1] < y0 + split.cell[1] / 2 and y0 < box[3], name


def test_the_row_reader_reads_his_range_form() -> None:
    """``K11a–d`` is four half-cells; the reader is what the test above
    trusts, so it is checked on its own."""
    assert _written_cells("K11a–d, L10c") == [
        "K11a", "K11b", "K11c", "K11d", "L10c"
    ]
    assert _written_cells("K13b (except its top edge)") == ["K13b"]


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


def test_a_tie_on_the_shipped_grid_goes_to_the_table_order() -> None:
    """Every inherited half-cell of the shipped grid takes, among the named
    half-cells at the least distance, the first in the table -- the
    distance computed here in exact fractions of the table's cell size, so
    a tie is a tie. Found in the Story 4.17 review: G12b lies 21.1 px from
    both *tladder* (G11d) and *toutside* (G12d), and subtracting float
    centres let rounding noise pick *toutside*; the table lists *tladder*
    first."""
    split = _split()
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
    assert split.inherited["G12b"] == "tladder"
