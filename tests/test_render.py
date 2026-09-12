"""``render`` -- the tests of the report's selection and formatting.

The stage reads neither a demo nor the archive, so the whole report is
tested from hand-built :class:`~pappascout.domain.report.Report` objects.
The tests match the rows of spec-2-4's I/O matrix and its acceptance
criteria.

The builder functions are in this file, because ``test_stage_render`` and
``test_cli_report`` use the same ones: three copies would drift, and the
stage's test could then pass on a report the rendering's test never sees.

**The fixture covers both variants of every branch in which the report
chooses.** A first-contact sample point is a different kind from a time
sample point, and an observed detonation area is a different thing from one
derived from the point cloud; both pairs have to be in the fixture, or only
one direction is guarded and a wrong default goes through every claim
unnoticed.
"""

from __future__ import annotations


import hashlib
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from pappascout.constants import (
    ANOMALY_RULE_FI,
    ANOMALY_RULES,
    ROUND_TYPE_FI,
    ROUND_TYPES,
    UTILITY_BUCKET_ALL,
    UTILITY_BUCKET_UNKNOWN,
    seconds_label,
)
from pappascout.domain.report import (
    Anomaly,
    AnomalyPoint,
    AnomalyRound,
    AnomalyScan,
    AreaDistribution,
    AreaOrientation,
    ArmedCount,
    ArmedPlayers,
    ArmoredCount,
    ArmoredPlayers,
    DeathReport,
    FirstContactArea,
    FirstDeathArea,
    GrenadeCount,
    KillArea,
    MapReport,
    MissingDemo,
    PlayersCount,
    Position,
    Report,
    RosterEntry,
    RosterSample,
    RoundTypeReport,
    Sample,
    SampleBucket,
    SideReport,
    TeamReport,
    UtilityCounts,
    UtilityUse,
    slugify,
)
from pappascout.domain.models import PLAYERS_ON_SERVER, ReportSettings
from pappascout.errors import PappascoutError
from pappascout.render import view as view_module
from pappascout.render import (
    build_view,
    render_report,
    round_list_demo_ids,
    template_digest,
    template_text,
)
from pappascout.render.view import (
    ANOMALY_HEADING,
    GRENADE_ORDER,
    GRENADE_TYPE_FI,
    MAX_ANOMALY_LINES,
    MAX_DEATH_LINES,
    MERGED_EQUIPMENT_LABEL,
    PATTERN_ROUND_TYPES,
    PROTECTED_ROUND_TYPES,
    ROUND_TYPE_ORDER,
    TRACEABILITY_HEADING,
    UNKNOWN_AREA,
    UNKNOWN_MAP_LABEL,
    UNNAMED_PLAYER,
    Claim,
    block_min_rounds,
    pattern_min_rounds,
)

# Private, but imported on purpose: the traceability chapter's explanation
# is a constant, and copying a fragment of it into the test would make two
# truths of it -- the same rationale as with TEAM_SLUG and
# TRACEABILITY_HEADING.
from pappascout.render.view import _PRUNING_KEPT_THE_BLOCK, _TRACEABILITY_NOTE

TEAM_KEY = "aaaaaaaaaaaaaaaa"
TEAM_NAME = "MatureMayhem"

#: The slug the default report gets: it is derived from the NAME and not from
#: the id, because the name is an observation. The file-name tests read it
#: from here, so that the rule is not in two places in two different forms.
TEAM_SLUG = "maturemayhem"

#: A roster in which everyone has a name and a SteamID64. The body speaks in
#: names and the traceability chapter carries the ids (Story 2.12), so the
#: fixture has to hold both -- with names alone the chapter's claims could not
#: be written.
DEFAULT_ROSTER = [
    RosterEntry(player_id=str(n), display_name=f"pelaaja{n}")
    for n in range(1, 6)
]

#: A roster whose ids are **in the shape of a SteamID64 but invented**.
#:
#: The default roster's ``1``..``5`` are not fit for the claim that no id
#: appears in the body: they do not match :data:`IDENTIFIER_SHAPE`, so the
#: claim would pass even when the numbers are still in the body. The shape is
#: therefore what this fixture brings -- not anybody's real id.
#:
#: **Real ids do not belong in this file.** The archive is not in git but the
#: tests are, and the pair of a genuine player name and a SteamID64 is
#: personal data whose committing adds not one claim: the measurement
#: concerns the shape (``7656119`` + 10 digits), and the shape needs nobody
#: real.
STEAM_ROSTER = [
    RosterEntry(
        player_id=f"765611900000000{number:02d}", display_name=f"pelaaja{number}"
    )
    for number in range(1, 8)
]

#: An id's **shape**: a 16-character digest or a 17-digit SteamID64.
#:
#: A shape and not a list, so that the claim "there are no ids in the body"
#: does not lean on which strings the test happens to know. Three extensions
#: compared with spec-2-12's manual ``grep``, each for its own reason: upper
#: case included (a digest can come from a source using either case), the
#: ``7656119`` prefix dropped (a SteamID64's shape is 17 digits, and a player
#: is no less a player because his account is newer) and the anchoring
#: removed from the shape's edges (a longer hex run is still an id).
#:
#: **The shape does not recognise demo ids and cannot.**
#: ``ANCIENT_vs_RCAVE_VETERANS`` does not resemble a digest, and a FACEIT
#: id's longest hex runs are 12 characters. They are verified
#: **literally**: the test knows its fixture's demo ids and claims about them
#: by name.
IDENTIFIER_SHAPE = re.compile(r"[0-9a-fA-F]{16}|[0-9]{17}")

#: A Markdown code span. It is needed because the round appendix's exception
#: is **narrower than the chapter**: an id may be in a path, and a path is a
#: code span.
CODE_SPAN = re.compile(r"`[^`]*`")

#: A FACEIT id that does not hold the map's name (Story 2.11). When the map's
#: name is not recognised, ``map_name`` **is** this string -- that is, the map
#: chapter's heading is an id, and that is the body's third exception.
FACEIT_DEMO_ID = "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1"

#: A demo that did not make it into the sample. The id is in the body on
#: purpose: the reason contains a command the reader copies.
MISSING_DEMO_ID = "ANCIENT_vs_RCAVE_VETERANS"

#: The backtick as a constant: test text that contains code spans is easier
#: to read by name than by character, and Markdown's nested backticks are
#: precisely the place where the quoting gets muddled.
BACKTICK = "`"

#: Seven areas each of which has exactly one round: the pruning threshold (3)
#: takes them all, and the median alone is left. The same setup as in the
#: RCAVE de_anubis default block the retro measured.
FIRST_CONTACT_SPREAD = (
    "Alley",
    "BombsiteB",
    "Bricks",
    "Bridge",
    "Connector",
    "Middle",
    "Palace",
)

DEMO_ID = "Ancient_vs_kaljukostaja"

#: The threshold the report carries with it. The same number as in
#: ``settings.toml``; the tests read it from the report and not from the
#: settings -- exactly as ``render`` itself does.
SMALL_SAMPLE = 3

#: The threshold for joining lineups
#: (``[thresholds].team_identity_min_common``, AD-6). In the same default
#: fixture as :data:`SMALL_SAMPLE`, because the lineup row writes it out in
#: the same way as the small-sample row writes its own -- and without it the
#: fixture would not look like a real report.
MIN_COMMON = 3

#: The round lists' paths, as the stage would hand them in. Absolute, because
#: the report is pasted into Discord and the reader does not know where the
#: archive's root is.
ROUND_LISTS = (rf"C:\arkisto\classified\{TEAM_KEY}\{DEMO_ID}.md",)


# --- The builder functions ------------------------------------------------------


def sample(rounds: int, demos: int = 1, bucket: str = "unknown") -> Sample:
    """The sample in one bucket; the others stay zero."""
    zero = SampleBucket(demos=0, rounds=0)
    buckets = {"league": zero, "other": zero, "unknown": zero}
    buckets[bucket] = SampleBucket(demos=demos, rounds=rounds)
    return Sample(
        demos=sum(b.demos for b in buckets.values()),
        rounds=sum(b.rounds for b in buckets.values()),
        **buckets,
    )


def roster_sample(
    rounds: int, demos: int = 1, bucket: str = "unknown"
) -> RosterSample:
    """Roster breakdown in one bucket; the others stay zero.

    Default bucket is ``unknown``, which is the state of every demo in the
    archive: the fixture therefore renders the sentence the reader actually
    sees today, and a test that wants the split asks for it.
    """
    zero = SampleBucket(demos=0, rounds=0)
    buckets = {"full": zero, "partial": zero, "unknown": zero}
    buckets[bucket] = SampleBucket(demos=demos, rounds=rounds)
    return RosterSample(
        demos=sum(b.demos for b in buckets.values()),
        rounds=sum(b.rounds for b in buckets.values()),
        **buckets,
    )


def area(name: str | None, m: int, bars: dict[int, int]) -> AreaDistribution:
    """An area's distribution. ``bars`` is ``player count -> rounds``."""
    return AreaDistribution(
        area=name,
        m=m,
        players_dist=[
            PlayersCount(players=players, n=n) for players, n in bars.items() if n
        ],
    )


def position(
    seconds: float | None,
    areas: list[AreaDistribution],
    m: int,
    *,
    kind: str = "time",
    median: float | None = None,
    missing: int = 0,
) -> Position:
    return Position(
        sample_kind=kind,
        seconds=seconds,
        seconds_median=median,
        m=m,
        rounds_missing=missing,
        areas=areas,
    )


def first_contact_position(
    areas: list[AreaDistribution], m: int, *, median: float | None = 9.05
) -> Position:
    """A first-contact sample point: no nominal seconds, a median instead."""
    return position(None, areas, m, kind="first_contact", median=median)


def armed(m: int, bars: dict[int, int], unknown: int = 0) -> ArmedPlayers:
    return ArmedPlayers(
        m=m,
        rounds_unknown=unknown,
        counts=[ArmedCount(armed=count, n=n) for count, n in bars.items() if n],
    )


def armored(m: int, bars: dict[int, int], unknown: int = 0) -> ArmoredPlayers:
    return ArmoredPlayers(
        m=m,
        rounds_unknown=unknown,
        counts=[ArmoredCount(armored=count, n=n) for count, n in bars.items() if n],
    )


def counts(grenade_type: str, m: int, bars: dict[int, int]) -> UtilityCounts:
    return UtilityCounts(
        grenade_type=grenade_type,
        m=m,
        counts=[GrenadeCount(thrown=thrown, n=n) for thrown, n in bars.items() if n],
    )


def use(
    grenade_type: str,
    throw: str | None,
    detonate: str | None,
    *,
    n: int,
    m: int,
    throws: int | None = None,
    bucket: str = "0-5",
    source: str | None = "point_cloud",
) -> UtilityUse:
    return UtilityUse(
        grenade_type=grenade_type,
        throw_area=throw,
        detonate_area=detonate,
        area_source=source if detonate is not None else None,
        seconds_bucket=bucket,
        n=n,
        throws=throws if throws is not None else n,
        m=m,
    )


def deaths(
    *,
    first: dict[str | None, int] | None = None,
    rounds_missing: int = 0,
    median: float | None = None,
    kills: dict[str | None, int] | None = None,
) -> DeathReport:
    """The deaths part of the report.

    ``first`` is area -> rounds, ``kills`` area -> kills. The denominators
    are computed from the sums, because that is precisely the model's
    contract: the first-death distribution's ``m`` is rounds and the kill
    distribution's ``m`` is kills.

    ``rounds_missing`` has to be given so that ``m + rounds_missing`` is the
    round type's sample -- the model checks that. :func:`round_type` fills it
    in for you when no deaths part is given.
    """
    first = first or {}
    kills = kills or {}
    m = sum(first.values())
    total = sum(kills.values())
    return DeathReport(
        m=m,
        rounds_missing=rounds_missing,
        first_death_seconds_median=median,
        first_death_areas=[
            FirstDeathArea(area=area, n=n, m=m) for area, n in first.items() if n
        ],
        kills_total=total,
        kills=[KillArea(area=area, n=n, m=total) for area, n in kills.items() if n],
    )


def round_type(
    name: str,
    rounds: int,
    *,
    positions: list[Position] | None = None,
    utility: list[UtilityUse] | None = None,
    utility_counts: list[UtilityCounts] | None = None,
    players_armed: ArmedPlayers | None = None,
    players_armored: ArmoredPlayers | None = None,
    first_contact: list[FirstContactArea] | None = None,
    death_report: DeathReport | None = None,
    small_sample: bool | None = None,
) -> RoundTypeReport:
    return RoundTypeReport(
        round_type=name,
        sample=sample(rounds),
        small_sample=rounds < SMALL_SAMPLE if small_sample is None else small_sample,
        positions=positions or [],
        utility=utility or [],
        utility_counts=utility_counts or [],
        players_armed=players_armed or armed(0, {}),
        players_armored=players_armored or armored(0, {}),
        first_contact=first_contact or [],
        # Without deaths every round is "ei omia kuolemia" -- and the model's
        # cross-check demands that the deaths cover the whole sample.
        deaths=(
            death_report
            if death_report is not None
            else deaths(rounds_missing=rounds)
        ),
    )


def side(name: str, round_types: list[RoundTypeReport]) -> SideReport:
    return SideReport(
        side=name,
        sample=sample(sum(rt.sample.rounds for rt in round_types)),
        round_types=round_types,
    )


def map_report(
    name: str,
    sides: list[SideReport],
    *,
    demo_ids: list[str] | None = None,
    source: str = "map_demo_id",
) -> MapReport:
    ids = demo_ids or [DEMO_ID]
    return MapReport(
        map_name=name,
        map_name_source=source,
        map_demo_ids=ids,
        sample=sample(sum(s.sample.rounds for s in sides), demos=len(ids)),
        sides=sides,
    )


def scan(**overrides) -> AnomalyScan:
    """The anomaly rules' coverage for the fixture.

    The default is **full coverage without blind spots**, because that is the
    state in which an empty anomaly chapter is a measured negative. Blind
    spots are built separately in the tests that concern them -- otherwise
    every other test would measure the warning's text by accident.
    """
    values: dict[str, object] = {
        "rules": ["ct_advance", "crunch", "stack"],
        "rules_deferred": [],
        "rounds_scanned": 2,
        "crunch_rounds": 1,
        "advance_rounds": 0,
        "stack_rounds": 1,
    }
    values.update(overrides)
    return AnomalyScan(**values)


def report(
    maps: list[MapReport] | None = None,
    *,
    anomalies: list[Anomaly] | None = None,
    scan_: AnomalyScan | None = None,
    missing_demos: list[MissingDemo] | None = None,
    unclassified: int = 0,
    unpaired: int = 0,
    thresholds_used: dict | None = None,
    classify_thresholds: dict | None = None,
    display_name: str = TEAM_NAME,
    display_name_source: str | None = None,
    name_alternatives: list[str] | None = None,
    roster: list[RosterEntry] | None = None,
    lineup_keys: list[str] | None = None,
    generated_at: datetime | None = None,
    roster_sample_: RosterSample | None = None,
) -> Report:
    entries = maps or []
    rounds = sum(m.sample.rounds for m in entries)
    demos = sum(m.sample.demos for m in entries)
    # The source is the fixture's OWN parameter and not derived from the
    # name. Were it derived by the rule "name == id", the case ``_has_name``
    # is written for -- an observed name that happens to look like the id --
    # would never be exercised at all.
    source = display_name_source or (
        "team_key" if display_name == TEAM_KEY else "clan_name"
    )
    return Report(
        generated_at=generated_at or datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        tool_versions={"pappascout": "0.1.0"},
        team=TeamReport(
            key=TEAM_KEY,
            # The slug is derived from the display name, as ``aggregate``
            # derives it; the model guards this pair.
            slug=slugify(display_name) or slugify(TEAM_KEY),
            display_name=display_name,
            display_name_source=source,
            display_name_alternatives=list(name_alternatives or []),
            lineup_keys=list(lineup_keys or [TEAM_KEY]),
            roster=list(roster if roster is not None else DEFAULT_ROSTER),
            roster_source="lineups",
        ),
        sample=sample(rounds, demos=demos),
        # The roster breakdown defaults to **the same sample, wholly
        # unknown**, which is the archive's state for as long as ``select``
        # has not been run over it. The league and roster totals are equal,
        # which the model requires; a test that measures the split hands in
        # its own value.
        roster_sample=(
            roster_sample(rounds, demos=demos)
            if roster_sample_ is None
            else roster_sample_
        ),
        thresholds_used=(
            {
                "thresholds": {
                    "small_sample_rounds": SMALL_SAMPLE,
                    "team_identity_min_common": MIN_COMMON,
                    # The anomaly thresholds into the fixture, because the
                    # reading guide reads them from the report and does not
                    # invent them. The same numbers as in settings.toml.
                    "advance_t_share": 0.80,
                    "advance_area_min_observations": 20,
                    "advance_max_sample_s": 30.0,
                    "advance_min_players": 1,
                    "crunch_min_players": 2,
                    "crunch_min_sources": 2,
                    "stack_min_players": 4,
                    "stack_group_margin": 1.25,
                    "stack_site_separation_min": 2.0,
                }
            }
            if thresholds_used is None
            else thresholds_used
        ),
        classify_thresholds=(
            {"full_equip_min": 4000}
            if classify_thresholds is None
            else classify_thresholds
        ),
        unpaired_detonations=unpaired,
        missing_demos=missing_demos or [],
        unclassified_rounds=unclassified,
        # The default is empty: the anomaly chapter exists even when there
        # are no anomalies, and that is exactly the background state of every
        # other test.
        anomalies=anomalies or [],
        anomaly_scan=scan_ if scan_ is not None else scan(),
        maps=entries,
    )


def pistol_map() -> MapReport:
    """One map with a pistol round on both sides.

    The T-side block covers **every** kind of row the report can write: a
    time sample point, a first-contact sample point, grenade counts, the
    grenades' targets (both an observed and a derived area) and the armed
    count.
    """
    return map_report(
        "de_ancient",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[
                            position(
                                15.0,
                                [
                                    area("Middle", 1, {3: 1}),
                                    area("MainHall", 1, {2: 1}),
                                ],
                                1,
                            ),
                            first_contact_position([area("Middle", 1, {2: 1})], 1),
                        ],
                        utility_counts=[
                            counts("smoke", 1, {2: 1}),
                            counts("flashbang", 1, {2: 1}),
                        ],
                        utility=[
                            use("smoke", "TSpawn", "Middle", n=1, m=1),
                            use(
                                "flashbang",
                                "TSpawn",
                                "MainHall",
                                n=1,
                                m=1,
                                source="observed",
                            ),
                        ],
                        players_armed=armed(1, {0: 1}),
                        # The pistol round's whole plot on one row: 0 armed,
                        # 5 kevlars.
                        players_armored=armored(1, {5: 1}),
                    )
                ],
            ),
            side("CT", [round_type("pistol", 1)]),
        ],
    )


def default_map(rounds: int = 8) -> MapReport:
    """A map whose default block holds both a pattern and a single
    observation.
    """
    return map_report(
        "de_inferno",
        [
            side(
                "T",
                [
                    round_type(
                        "full",
                        rounds,
                        positions=[
                            position(
                                15.0,
                                [
                                    # A pattern: it repeats on 4 rounds.
                                    area("Apartments", rounds, {3: 4, 0: rounds - 4}),
                                    # A single one: 1 round, not a pattern.
                                    area("Banana", rounds, {2: 1, 0: rounds - 1}),
                                ],
                                rounds,
                            )
                        ],
                        utility_counts=[counts("smoke", rounds, {2: 5, 0: rounds - 5})],
                        utility=[
                            use("smoke", "TSpawn", "BombsiteB", n=4, m=rounds),
                            use("flashbang", "TSpawn", "Banana", n=1, m=rounds),
                        ],
                    )
                ],
            )
        ],
        demo_ids=["inferno_vs_ryhmarama"],
    )


def demo_map(
    demo_ids: list[str],
    *,
    name: str = "de_nuke",
    source: str = "map_demo_id",
) -> MapReport:
    """A map whose **demo ids** are the test's subject.

    The content is deliberately the smallest possible: these tests care about
    the map's heading and the traceability chapter's map row, not about the
    observation rows.
    """
    return map_report(
        name,
        [side("T", [round_type("eco", 2)])],
        demo_ids=demo_ids,
        source=source,
    )


def unknown_map(demo_id: str = FACEIT_DEMO_ID) -> MapReport:
    """A map whose name was not recognised: **the name is the demo id**.

    This is how ``aggregate`` builds it (see
    :class:`~pappascout.domain.report.MapReport`): without an observation and
    without an inference from the pool, the name is the ``map_demo_id``
    itself and the source is ``unknown``. The fixture therefore does not
    imagine the situation but reproduces it.
    """
    return demo_map([demo_id], name=demo_id, source="unknown")


def missing_demo(demo_id: str = MISSING_DEMO_ID) -> MissingDemo:
    """A missing demo whose reason contains a command to run.

    The wording is ``aggregate``'s own (``stages/aggregate.py``): that
    command is precisely the reason the id stays on the body's row.
    """
    return MissingDemo(
        match=demo_id,
        reason=(
            "Kokoonpanotaulua (lineups.parquet) ei saatu luettua, joten ei "
            "tiedetä kuuluuko demo tälle joukkueelle. Aja parsinta uudelleen: "
            f"uv run pappascout parse {demo_id}"
        ),
    )


#: The pruning rules **as they are in ``settings.toml``** (Story 2.13). The
#: tests' default is production's default: if the fixture ran with pruning
#: switched off, the whole test set would describe a report nobody gets --
#: and every pruning fault would pass unnoticed.
#:
#: ``ReportSettings()`` and not hand-written values: that the defaults agree
#: with the settings file is ``test_settings``'s claim, and it is not written
#: out here a second time.
DEFAULT_PRUNING = ReportSettings()

#: Pruning entirely off. This story's most important test runs the report
#: with this and demands that the result is character for character the same
#: as before Story 2.13.
NO_PRUNING = ReportSettings(
    drop_saturated_equipment_lines=False,
    merge_equal_equipment_lines=False,
    skip_sample_seconds=[],
    max_utility_targets=0,
    max_kill_areas=0,
)


def render(entry: Report, settings: ReportSettings = DEFAULT_PRUNING) -> str:
    """The report with the round lists' paths -- as the stage writes it."""
    return render_report(entry, settings=settings, round_list_paths=ROUND_LISTS)


def view_of(
    entry: Report,
    settings: ReportSettings = DEFAULT_PRUNING,
    *,
    round_list_paths: tuple[str, ...] = (),
):
    """The view with the same pruning default as :func:`render`.

    A helper and not a direct ``build_view`` call, so that the default is in
    **one place** in the tests: written twice, one half of the test set could
    run with pruning on and the other with it off, and nothing would say
    which was which.
    """
    return build_view(
        entry, settings=settings, round_list_paths=round_list_paths
    )


def report_sections(text: str) -> list[tuple[str, str]]:
    """The report chapter by chapter: ``(heading, content without the heading
    line)``.

    The heading and the content separately, because the id exceptions fall in
    different places: a map chapter's **heading** can be a demo id (when the
    map's name was not recognised), but its content still has to be clean. As
    one string neither claim could be made.

    The first item is the document's start before any ``## `` heading, and
    its heading is the empty string: it holds the report's ``# `` title,
    which is just as much the body.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:], []))
        else:
            sections[-1][1].append(line)
    return [(heading, "\n".join(lines)) for heading, lines in sections]


def section_text(text: str, heading: str) -> str:
    """One chapter's content. A missing chapter is an error, not an empty
    string.
    """
    for name, content in report_sections(text):
        if name == heading:
            return content
    raise AssertionError(f"the report has no chapter {heading!r}")


def summary_text(text: str) -> str:
    """The summary's content -- the part the reader reads first."""
    return section_text(text, "Yhteenveto")


def traceability_text(text: str) -> str:
    """The traceability chapter's content."""
    return section_text(text, TRACEABILITY_HEADING)


# --- The basic shape ------------------------------------------------------------


def test_report_has_the_structure_the_spec_asks_for() -> None:
    """Title, summary, map, side, round type, the last three chapters."""
    text = render(report([pistol_map()]))
    for expected in (
        f"# {TEAM_NAME} -- scouting-raportti",
        "## Yhteenveto",
        "## `de_ancient` -- 2 kierrosta, 1 demo",
        "### T-puoli -- 1 kierros",
        "### CT-puoli -- 1 kierros",
        "**Pistooli** (1 kierros)",
        "## Kierrosliite",
        "## Lukuohje",
        f"## {TRACEABILITY_HEADING}",
    ):
        assert expected in text, expected


def test_report_ends_with_exactly_one_newline() -> None:
    text = render(report([pistol_map()]))
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_positions_utility_and_first_contact_are_bullets_not_paragraphs() -> None:
    """The product owner's analysis is bullets; the report is of the same
    shape.
    """
    text = render(report([pistol_map()]))
    body = text.split("**Pistooli** (1 kierros)")[1].split("\n\n")[0]
    # The first line is the end of the heading ("-- pieni otanta"), not an
    # observation.
    rows = [row for row in body.splitlines()[1:] if row.strip()]
    assert rows, body
    assert all(row.startswith("- ") for row in rows), rows


def test_areas_stay_in_english_and_text_is_finnish() -> None:
    """Callouts in English, the text in Finnish -- there is no translation
    layer.
    """
    text = render(report([pistol_map()]))
    assert "Middle 3" in text
    assert "MainHall 2" in text
    assert "kierroksesta" in text
    assert "utility:" in text


# --- The first-contact sample point ---------------------------------------------


def test_first_contact_position_is_labelled_by_its_median_not_by_zero_seconds() -> None:
    """First contact is not a time sample point and has no seconds.

    Without this test, removing the ``sample_kind`` guard would label every
    first-contact row as ``0 s`` and the median would vanish -- in every real
    report, without a single other claim noticing anything.
    """
    text = render(report([pistol_map()]))
    assert "- ensikontakti (mediaani 9,1 s): Middle 2 (1/1 kierroksesta)" in text
    assert "- 0 s:" not in text


def test_first_contact_without_a_median_is_still_labelled_first_contact() -> None:
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[
                            first_contact_position(
                                [area("Ramp", 1, {2: 1})], 1, median=None
                            )
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([entry]))
    assert "- ensikontakti: Ramp 2 (1/1 kierroksesta)" in text


def test_time_and_first_contact_positions_are_told_apart() -> None:
    """Two kinds of sample point in the same block get different labels."""
    view = view_of(report([pistol_map()]), round_list_paths=ROUND_LISTS)
    labels = [
        line.label
        for line in view.maps[0].sides[0].round_types[0].lines
        if line.label and ("s" in line.label or "ensikontakti" in line.label)
    ]
    assert "15 s" in labels
    assert any(label.startswith("ensikontakti") for label in labels)


# --- Every claim carries its sample ---------------------------------------------

#: One claim's sample as the template sets it.
_SAMPLE = re.compile(r" \(\d+/\d+ kierroksesta(?:, [^)]*)?\)")


#: The start of a map chapter's heading. **The name is a code span** (Story
#: 2.15, B1): it is free text the demo gave, and bare, a workshop map's name
#: broke the heading in the middle. A constant, because four test helpers
#: split the report at this point -- as four literals they would drift.
MAP_HEADING_START = "## `de_"


def observation_rows(text: str) -> list[str]:
    """The observation rows: the bullets that are not italic notes."""
    body = text.split(MAP_HEADING_START)[1].split("## Kierrosliite")[0]
    return [
        row
        for row in body.splitlines()
        if row.startswith("- ") and not row.startswith("- *")
    ]


def claim_segments(row: str) -> list[str]:
    """Split a row into claims by removing the samples.

    Returns the claims' texts without their samples. The note at the end of
    the row (``" -- ..."``) and the label at the start (``"- 15 s: "``) are
    cut off, so what is left is only the part every piece of which **has to**
    carry its sample.
    """
    part = row[2:]
    if " -- " in part:
        part = part.split(" -- ")[0]
    if ": " in part:
        part = part.split(": ", 1)[1]
    return _SAMPLE.sub("\x00", part).split("\x00")


def test_every_claim_on_every_line_carries_its_own_sample() -> None:
    """Spec-2-4's acceptance criterion word for word.

    The check is two-directional, because a bare "the row has a sample" would
    pass on a row that has three claims and a sample on only one:

    1. Every row **ends** in a sample, that is, the last claim has not been
       left without one.
    2. The pieces between the samples are whole claims and do not contain the
       separator ``", "`` -- if a claim were left without a sample, it would
       merge into its neighbour and the separator would stay inside the
       piece.
    """
    text = render(report([pistol_map(), default_map()]))
    rows = observation_rows(text)
    assert rows
    for row in rows:
        segments = claim_segments(row)
        if len(segments) == 1:
            # A row without claims is a bare note (a missing sample, say).
            assert "(" not in segments[0], row
            continue
        assert segments[-1] == "", row
        for index, segment in enumerate(segments[:-1]):
            text_only = segment[2:] if index else segment
            assert text_only, row
            assert ", " not in text_only, row


def test_the_number_of_samples_matches_the_number_of_claims() -> None:
    """The other direction of the same rule, counted from the view model.

    If one claim were left without a sample, the number of samples would be
    smaller than the number of claims -- regardless of where on the row it
    is.
    """
    entry = report([pistol_map(), default_map()])
    text = render(entry)
    view = view_of(entry, round_list_paths=ROUND_LISTS)
    claims = sum(
        len(line.claims)
        for map_view in view.maps
        for side_view in map_view.sides
        for round_view in side_view.round_types
        for line in round_view.lines
    )
    assert claims > 0
    assert len(re.findall(r"\(\d+/\d+ kierroksesta", text)) == claims


def test_sample_is_written_as_n_of_m_rounds() -> None:
    text = render(report([pistol_map()]))
    assert "Middle 3 (1/1 kierroksesta)" in text


# --- Filtered round types vs. protected ones ------------------------------------


def test_default_shows_only_repeating_patterns() -> None:
    """On full buys the broad lines are told, not the round-by-round."""
    text = render(report([default_map()]))
    assert "Apartments 3 (4/8 kierroksesta)" in text
    assert "Banana 2" not in text
    assert "vain toistuvat kuviot" in text


def test_default_says_how_many_observations_it_left_out() -> None:
    """The filtering is not silent: the number left out is written out."""
    text = render(report([default_map()]))
    assert re.search(
        r"toistuvat vähintään 3 kierroksella; \d+ harvinaisempaa havaintoa jäi pois",
        text,
    ), text


def one_block_report(round_type_name: str, rounds: int, **kwargs) -> Report:
    """A report of one map, one side and one round-type block.

    The block's round count is the second argument, because the pattern
    threshold is capped at it: a fixture that does not say the round count
    out loud says nothing about which threshold was used.
    """
    entry = round_type(round_type_name, rounds, **kwargs)
    return report([map_report("de_nuke", [side("T", [entry])])])


def test_a_saving_round_is_filtered_like_the_default() -> None:
    """``eco``, ``force`` and ``half`` are pattern types too (2026-09-11).

    They used to be written round by round, which in a block of a handful of
    rounds meant writing every single observation: in the real three-map
    report ``eco`` took 27 % of the body for 7 % of the rounds, and almost
    every row of a two-round block read ``(1/2 kierroksesta)`` -- one
    observation out of two, which is not a pattern.

    The fixture has four rounds, so the threshold is the report's own 3 and
    the cap does not come into it: the bar seen once goes and the bar seen
    three times stays.
    """
    for name in ("eco", "force", "half"):
        text = render(
            one_block_report(
                name,
                4,
                positions=[position(6.0, [area("Ramp", 4, {2: 3, 1: 1})], 4)],
            )
        )
        assert "Ramp 1 (1/4 kierroksesta)" not in text, name
        assert "Ramp 2 (3/4 kierroksesta)" in text, name
        assert "vain toistuvat kuviot" in text, name
        assert (
            "Vain kuviot, jotka toistuvat vähintään 3 kierroksella; "
            "1 harvinaisempaa havaintoa jäi pois."
        ) in text, name


def test_a_protected_round_type_shows_every_observation() -> None:
    """Pistol and anomaly are the round-by-round ones, and only they.

    The same fixture as above: on a protected type the bar seen once stays
    and no block claims to have filtered anything.
    """
    for name in ("pistol", "anomaly"):
        text = render(
            one_block_report(
                name,
                4,
                positions=[position(6.0, [area("Ramp", 4, {2: 3, 1: 1})], 4)],
            )
        )
        assert "Ramp 1 (1/4 kierroksesta)" in text, name
        assert "vain toistuvat kuviot" not in text, name
        assert "jäi pois" not in text, name


def test_every_round_type_is_either_filtered_or_protected() -> None:
    """There is no third state, and the gap is what this change closed.

    Until 2026-09-11 ``eco``, ``force`` and ``half`` were on neither list:
    not filtered, and not protected for any stated reason. A round type that
    falls between the two is written round by round by accident rather than
    by decision, and nothing in the code says so.
    """
    assert PATTERN_ROUND_TYPES | PROTECTED_ROUND_TYPES == set(ROUND_TYPES)
    assert not PATTERN_ROUND_TYPES & PROTECTED_ROUND_TYPES


def test_protected_round_types_never_claim_that_a_threshold_dropped_anything() -> None:
    """On a protected round type the threshold is 1, and no bar can fall
    below it.
    """
    text = render(report([pistol_map()]))
    assert "jäi pois" not in text
    assert "kynnyksen alta" not in text


# --- The threshold is capped at the block's round count (2026-09-11) ------------


@pytest.mark.parametrize(
    ("threshold", "rounds", "expected"),
    [
        (3, 2, 2),  # two rounds: only what happened in both
        (3, 3, 3),
        (3, 4, 3),  # the cap is inert as soon as the block is big enough
        (3, 6, 3),
        (3, 62, 3),  # the block that holds the data is untouched
        (3, 1, 1),  # nothing can repeat; the filter demands nothing
        (3, 0, 1),  # a floor, so that no block claims to filter at 0
        (None, 4, None),  # no threshold in the report -> no filtering
    ],
)
def test_the_threshold_is_capped_at_the_blocks_round_count(
    threshold: int | None, rounds: int, expected: int | None
) -> None:
    """A cap and not a proportion.

    "At least half the rounds" was the first proposal: at 62 rounds it would
    demand 31 and gut the one block that actually holds the data. The 62-row
    is in the table for precisely that reason -- it is the row a proportional
    rule would fail.
    """
    assert block_min_rounds(threshold, rounds) == expected


def test_a_two_round_block_keeps_only_what_happened_in_both_rounds() -> None:
    """The cap is what makes filtering a two-round block possible at all.

    At the report's uncapped threshold of 3 the block would empty; at the
    cap of 2 the observation made on both rounds stays and the one made on
    one round goes.
    """
    text = render(
        one_block_report(
            "eco",
            2,
            positions=[position(6.0, [area("Ramp", 2, {2: 1, 0: 1})], 2)],
            players_armed=armed(2, {1: 2}),
        )
    )
    assert "aseistettuja ostoajan lopussa: 1 (2/2 kierroksesta)" in text
    assert "Ramp 2 (1/2 kierroksesta)" not in text
    assert "Ramp 0 (1/2 kierroksesta)" not in text


def test_the_block_states_the_threshold_it_really_used() -> None:
    """A block that said "3" while filtering at 2 would be a false claim.

    The sentence states the number, so with the cap it has to be the capped
    number -- and the two wordings say which of the two rules produced it.
    """
    two = render(one_block_report("eco", 2, players_armed=armed(2, {1: 2})))
    assert "toistuvat kaikilla 2 kierroksella" in two
    assert "vähintään 3" not in two

    four = render(one_block_report("eco", 4, players_armed=armed(4, {1: 4})))
    assert "toistuvat vähintään 3 kierroksella" in four
    assert "kaikilla 4" not in four


def test_a_one_round_block_is_not_marked_as_filtered() -> None:
    """At a capped threshold of 1 the filter demands nothing.

    Every observation in a one-round block was made on every round of it, so
    marking the block "vain toistuvat kuviot" would claim a selection that
    was not made. The round count in the heading already says why.
    """
    text = render(
        one_block_report(
            "eco",
            1,
            positions=[position(6.0, [area("Ramp", 1, {2: 1})], 1)],
        )
    )
    assert "Ramp 2 (1/1 kierroksesta)" in text
    assert "vain toistuvat kuviot" not in text
    assert "toistuvat" not in text


def test_a_block_the_cap_empties_says_the_sample_was_too_small() -> None:
    """A bare heading is not an answer, and neither is a bare threshold.

    When the cap takes every row, the reason is not that the team did
    nothing repeatedly -- it is that two rounds cannot hold a repetition.
    The sentence says that, and it points at ``report.json`` so that the
    reader knows the observations still exist.
    """
    text = render(
        one_block_report(
            "eco",
            2,
            death_report=deaths(first={"Ramp": 1, "Palace": 1}),
        )
    )
    assert (
        "Yksikään havainto ei toistunut kaikilla 2 kierroksella: otanta on "
        "liian pieni, jotta mikään ehtisi toistua. Havainnot ovat "
        "report.jsonissa."
    ) in text


def test_a_block_with_no_observations_does_not_blame_the_threshold() -> None:
    """Nothing was offered to the threshold, so it took nothing.

    The branch used to ask whether the round type was filtered at all, which
    answers a different question: a ``full`` block with nothing in it was
    made to say that nothing passed the threshold.
    """
    text = render(one_block_report("full", 0))
    assert "Ei havaintoja tältä kierrostyypiltä." in text
    assert "Ei kuvioita, jotka ylittäisivät kynnyksen." not in text


def test_the_pattern_threshold_also_applies_to_counts_and_armed_players() -> None:
    """The threshold concerns every kind of row, not only the positions.

    When no bar repeats, the whole row is left out even from a large sample
    -- and that is intended: a scattered distribution is not a pattern. The
    ones left out are counted into the number the block gives.

    **The armour row is included**, because the threshold really bites on the
    ``full`` branches in particular and those are the report's most common.
    Without it, the armour row's filtering would be wholly untested: in every
    other test the round type is ``pistol`` or ``eco``, where ``min_n`` is 1.
    """
    scattered = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "full",
                        8,
                        # Eight rounds, eight different readings: nothing
                        # repeats three times.
                        players_armed=armed(8, dict.fromkeys(range(8), 1)),
                        players_armored=armored(8, dict.fromkeys(range(8), 1)),
                        utility_counts=[
                            counts("smoke", 8, dict.fromkeys(range(8), 1))
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([scattered]), NO_PRUNING)
    assert "aseistettuja ostoajan lopussa" not in text
    assert "panssaroituja ostoajan lopussa" not in text
    assert "utility:" not in text
    # 8 armed bars + 8 armour bars + 7 grenade bars (zero is not an
    # observation and therefore not something to drop).
    assert "23 harvinaisempaa havaintoa jäi pois" in text

    # **The same number with pruning** (Story 2.13, review round 1, item A):
    # the threshold's bookkeeping is a claim about the data and not a
    # presentation choice, so pruning does not touch it. The counters are
    # identical here, that is, rule 2 would merge the rows -- and if the
    # merging happened before the row builder, the number would be 15 and the
    # block would claim something different about the data from an unpruned
    # report.
    assert "23 harvinaisempaa havaintoa jäi pois" in render(report([scattered]))


def test_the_pattern_threshold_keeps_a_repeating_armored_bar() -> None:
    """The threshold must not eat a pattern that really repeats.

    A pair with the previous one: a bare "the row vanished" claim would pass
    on an implementation that always drops the armour row from a ``full``
    branch.
    """
    repeating = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "full",
                        8,
                        players_armored=armored(8, {5: 6, 4: 1, 3: 1}),
                    )
                ],
            )
        ],
    )
    text = render(report([repeating]))
    assert "panssaroituja ostoajan lopussa: 5 (6/8 kierroksesta)" in text
    assert "4 (1/8" not in text
    assert "2 harvinaisempaa havaintoa jäi pois" in text


def test_pattern_threshold_comes_from_the_report_not_from_code() -> None:
    """The threshold is read from ``thresholds_used``; without it nothing is
    filtered.
    """
    without = report([default_map()], thresholds_used={})
    assert pattern_min_rounds(without) is None
    text = render(without)
    assert "Banana 2 (1/8 kierroksesta)" in text
    assert "Toistumisen kynnystä ei ollut raportissa" in text


@pytest.mark.parametrize("value", [0, -1, "3", True, None])
def test_unusable_threshold_is_treated_as_missing(value: object) -> None:
    """An unusable threshold must not silently turn into a filter."""
    entry = report(
        [default_map()], thresholds_used={"thresholds": {"small_sample_rounds": value}}
    )
    assert pattern_min_rounds(entry) is None


# --- The I/O matrix -------------------------------------------------------------


def test_small_sample_is_marked_not_hidden() -> None:
    """A sample below the threshold is marked, but the observation still
    shows.
    """
    text = render(report([pistol_map()]))
    assert "**Pistooli** (1 kierros) -- pieni otanta" in text
    assert "Middle 3" in text


def test_unknown_league_status_is_said_out_loud() -> None:
    """When no demo's kind is known, the summary says so."""
    text = render(report([pistol_map()]))
    assert "**Liigatieto:**" in text
    assert "tuntematon" in text


def in_league(entry: Report) -> Report:
    """Move the report's whole sample from the ``unknown`` bucket into the
    ``league`` bucket.

    The bucket is in the structure at every level, and the model checks the
    sums **one bucket at a time**, so changing the top level alone will not
    do. The round trip through the model (``model_dump`` ->
    ``model_validate``) also makes sure that the report survives
    serialisation -- which is exactly what ``render`` does when it reads
    ``report.json``.
    """
    data = entry.model_dump(mode="json")

    def swap(node: object) -> None:
        if isinstance(node, dict):
            if {"league", "other", "unknown"} <= set(node):
                node["league"] = node["unknown"]
                node["unknown"] = {"demos": 0, "rounds": 0}
            for value in node.values():
                swap(value)
        elif isinstance(node, list):
            for value in node:
                swap(value)

    swap(data)
    return Report.model_validate(data)


def test_confirmed_league_demos_do_not_trigger_the_warning() -> None:
    entry = in_league(report([pistol_map()]))
    assert entry.sample.league.demos == 1
    assert "**Liigatieto:**" not in render(entry)


# --- Roster class (Story 3.9) ---------------------------------------------------


def three_demo_report(rounds: int = 6) -> Report:
    """A report of one map played across three demos.

    Three, because the roster split needs at least two known demos plus an
    unknown one before the line can be read wrong: with one demo every bucket
    count is either 0 or the total.
    """
    return report(
        [
            map_report(
                "de_nuke",
                [side("T", [round_type("full", rounds)])],
                demo_ids=["Nuke_vs_a", "Nuke_vs_b", "Nuke_vs_c"],
            )
        ]
    )


def with_roster(entry: Report, buckets: dict[str, tuple[int, int]]) -> Report:
    """Replace the report's roster breakdown. ``bucket -> (demos, rounds)``.

    Missing buckets are zero. The model requires the roster totals to equal
    the league totals, so a test that gets the arithmetic wrong fails at
    construction instead of in an assertion about text. The round trip through
    ``model_dump`` -> ``model_validate`` is the one ``render`` does when it
    reads ``report.json``.
    """
    filled = {
        name: {
            "demos": buckets.get(name, (0, 0))[0],
            "rounds": buckets.get(name, (0, 0))[1],
        }
        for name in ("full", "partial", "unknown")
    }
    data = entry.model_dump(mode="json")
    data["roster_sample"] = {
        "demos": sum(b["demos"] for b in filled.values()),
        "rounds": sum(b["rounds"] for b in filled.values()),
        **filled,
    }
    return Report.model_validate(data)


#: The whole sentence for the unknown case, asserted as one string.
#:
#: A prefix assertion would let the half that carries the information -- which
#: bucket the demos went into, and that the split is therefore absent -- be
#: deleted while both this test and ``GOLDEN`` were updated to match.
UNKNOWN_ROSTER_ROW = (
    "- **Rosteriluokka:** yhdenkään demon rosteriluokkaa ei ole vahvistettu: "
    "kaikki ovat lokerossa tuntematon, eikä otanta erottele 5/5- ja "
    "4/5-karttoja"
)


def test_an_unknown_roster_class_is_said_once_without_a_zero_split() -> None:
    """While ``select`` has not been run over the archive: zeros are not news."""
    text = render(three_demo_report())
    assert UNKNOWN_ROSTER_ROW in text
    assert "5/5: 0 demoa" not in text
    assert text.count("**Rosteriluokka:**") == 1


def test_both_roster_classes_are_reported_with_demo_and_round_counts() -> None:
    """AC: with demos classified ``5/5`` and ``4/5``, the summary says both."""
    text = render(
        with_roster(
            three_demo_report(),
            {"full": (1, 3), "partial": (2, 3)},
        )
    )
    assert (
        "**Rosteriluokka:** 5/5: 1 demo / 3 kierrosta, "
        "4/5: 2 demoa / 3 kierrosta, tuntematon: 0 demoa / 0 kierrosta"
    ) in text
    assert "yhdenkään demon rosteriluokkaa" not in text


def test_a_partly_known_roster_class_keeps_the_unknown_bucket() -> None:
    """I/O matrix: one ``5/5`` and two unknown -- the split is still shown."""
    text = render(
        with_roster(three_demo_report(), {"full": (1, 2), "unknown": (2, 4)})
    )
    assert (
        "**Rosteriluokka:** 5/5: 1 demo / 2 kierrosta, "
        "4/5: 0 demoa / 0 kierrosta, tuntematon: 2 demoa / 4 kierrosta"
    ) in text


def test_the_roster_line_scales_with_the_data() -> None:
    """The counts are read from the report; a hardcoded line would not move."""
    few = render(with_roster(three_demo_report(), {"full": (1, 1), "partial": (2, 5)}))
    many = render(
        with_roster(three_demo_report(12), {"full": (2, 9), "partial": (1, 3)})
    )
    assert "5/5: 1 demo / 1 kierros, 4/5: 2 demoa / 5 kierrosta" in few
    assert "5/5: 2 demoa / 9 kierrosta, 4/5: 1 demo / 3 kierrosta" in many


def test_a_wholly_partial_roster_is_reported_and_not_called_unknown() -> None:
    """Every classified map played with a stand-in -- the weakest evidence.

    This is the case the story exists for: the observation is weaker and
    therefore has to be *said*. A branch that asked only about ``full``
    would print "yhdenkään demon rosteriluokkaa ei ole vahvistettu" over
    measured ``4/5`` numbers.
    """
    text = render(with_roster(three_demo_report(), {"partial": (3, 6)}))
    assert (
        "**Rosteriluokka:** 5/5: 0 demoa / 0 kierrosta, "
        "4/5: 3 demoa / 6 kierrosta"
    ) in text
    assert "yhdenkään demon rosteriluokkaa" not in text


def test_the_partial_gloss_is_absent_when_no_demo_is_partial() -> None:
    """A notation the row's own numbers do not use sends the reader looking."""
    text = render(with_roster(three_demo_report(), {"full": (3, 6)}))
    assert "5/5: 3 demoa / 6 kierrosta" in text
    assert "vakirosterin ulkopuolelta" not in text


def test_neither_breakdown_note_speaks_of_demos_that_are_not_there() -> None:
    """Item 7: an empty sample has no demos to make a claim about.

    Both notes are claims about the demos in the sample. With none in it, the
    ``Otanta`` row already says ``0 demoa`` and the empty-data note says the
    rest, so the two rows stay silent -- and they stay silent *together*, or
    the reader learns to trust one and not the other.
    """
    text = render(report([]))
    assert "Aineistoa ei ole" in text
    assert "**Rosteriluokka:**" not in text
    assert "**Liigatieto:**" not in text


def test_the_roster_line_says_what_a_partial_class_means() -> None:
    """``4/5`` is a notation the reader cannot interpret without a gloss."""
    text = render(with_roster(three_demo_report(), {"full": (2, 4), "partial": (1, 2)}))
    assert "yksi pelaaja oli vakirosterin ulkopuolelta" in text


def test_missing_demos_get_their_own_section_with_reasons() -> None:
    text = render(
        report(
            [pistol_map()],
            missing_demos=[MissingDemo(match="Nuke_vs_imuaijat", reason="ei parsittu")],
        )
    )
    assert "## Puuttuvat demot" in text
    assert "Nuke_vs_imuaijat" in text
    assert "ei parsittu" in text


def test_unclassified_rounds_are_mentioned_in_the_summary() -> None:
    text = render(report([pistol_map()], unclassified=4))
    assert "**Luokittelemattomat:**" in text
    assert "4 kierrosta" in text


def test_unpaired_detonations_are_mentioned_in_the_summary() -> None:
    text = render(report([pistol_map()], unpaired=7))
    assert "**Parittomat räjähdykset:**" in text
    assert "7 kpl" in text


def test_a_round_type_that_does_not_exist_gets_no_empty_heading() -> None:
    """A map without a force: the heading is not written empty."""
    text = render(report([pistol_map()]))
    assert "**Force**" not in text
    assert "**Eco**" not in text


def test_a_round_type_without_observations_says_so_rather_than_going_silent() -> None:
    """A round type without observations and a block the threshold ate are
    different things.

    The number of rounds is itself an observation: if there are rounds, the
    team either lost players or did not, and both are told. "No observations"
    is therefore reserved for a block that has no rounds at all -- otherwise
    it would claim ignorance in a situation about which something is known.
    """
    nothing = map_report(
        "de_nuke",
        [side("T", [round_type("eco", 0)])],
    )
    assert "Ei havaintoja tältä kierrostyypiltä." in render(report([nothing]))

    # The same block with its rounds says that nobody died -- and does not
    # claim to be without observations.
    played = map_report("de_nuke", [side("T", [round_type("eco", 4)])])
    text = render(report([played]))
    assert "ei omia kuolemia 4 kierroksella" in text
    assert "Ei havaintoja tältä kierrostyypiltä." not in text

    # On full buys the filtering rule is still told, even when the block has
    # a row.
    default = map_report("de_nuke", [side("T", [round_type("full", 8)])])
    assert "Vain kuviot, jotka toistuvat vähintään 3 kierroksella" in render(
        report([default])
    )


def test_a_side_without_round_types_is_not_a_bare_heading() -> None:
    entry = map_report("de_nuke", [side("CT", [])])
    text = render(report([entry]))
    assert "### CT-puoli" in text
    assert "Ei yhtään luokiteltua kierrostyyppiä" in text


def test_a_map_without_sides_is_not_a_bare_heading() -> None:
    entry = map_report("de_nuke", [])
    text = render(report([entry]))
    assert "## `de_nuke`" in text
    assert "Ei havaintoja kummaltakaan puolelta" in text


def test_area_without_a_name_is_named_and_explained() -> None:
    """``area = null`` is an unknown area, and the absence of coordinates is
    stated.
    """
    unknown = map_report(
        "de_anubis",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[position(6.0, [area(None, 1, {4: 1})], 1)],
                    )
                ],
            )
        ],
    )
    text = render(report([unknown]))
    assert f"{UNKNOWN_AREA} 4 (1/1 kierroksesta)" in text
    assert "Koordinaatteja ei ole report.jsonissa" in text


def test_the_unknown_area_note_is_absent_when_every_area_is_known() -> None:
    assert "Koordinaatteja ei ole" not in render(report([pistol_map()]))


def test_empty_report_still_writes_a_summary_and_says_there_is_no_data() -> None:
    """An empty report: the summary is written, and the absence of data is
    stated.
    """
    text = render(report([]))
    assert "## Yhteenveto" in text
    assert "Aineistoa ei ole" in text
    # Two claims and not one: the protected heading (``## `de_``) is the one
    # this version sets, but the unprotected one (``## de_``) is absent too
    # -- otherwise the test would pass on a report in which the protection
    # has been forgotten.
    assert MAP_HEADING_START not in text
    assert "## de_" not in text


def test_unknown_map_name_is_flagged_in_the_heading() -> None:
    entry = map_report(
        "1-uuid-1-1",
        [side("T", [round_type("pistol", 1)])],
        demo_ids=["1-uuid-1-1"],
        source="unknown",
    )
    assert "kartan nimeä ei tunnistettu" in render(report([entry]))


@pytest.mark.parametrize("source", ["demo_header", "map_demo_id"])
def test_a_known_map_name_is_not_flagged(source: str) -> None:
    """The mark belongs only to the source ``unknown`` (Story 2.11).

    ``demo_header`` is an observation from the demo's header and
    ``map_demo_id`` an inference from the id; both are recognised names. A
    condition that lists the known sources would silently make every new
    source "unknown".
    """
    entry = map_report(
        "de_ancient",
        [side("T", [round_type("pistol", 1)])],
        source=source,
    )
    text = render(report([entry]))
    assert "kartan nimeä ei tunnistettu" not in text
    assert "de_ancient" in text


def test_missing_sample_point_is_reported_not_dropped() -> None:
    """45 s is missing from a round that was decided earlier -- the
    difference is written out.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "eco",
                        4,
                        positions=[
                            position(45.0, [area("Ramp", 3, {1: 3})], 3, missing=1)
                        ],
                    )
                ],
            )
        ],
    )
    assert "näyte puuttuu 1 kierrokselta" in render(report([entry]))


# --- Utility --------------------------------------------------------------------


def test_grenade_counts_answer_how_many_were_thrown() -> None:
    """The target analysis's line *"2 savua 2 valoo"*."""
    text = render(report([pistol_map()]))
    assert "savu 2 kpl (1/1 kierroksesta)" in text
    assert "valo 2 kpl (1/1 kierroksesta)" in text


def test_grenade_uses_answer_where_it_went() -> None:
    """The target analysis's line *"T-spawnista CT-savu B sitelle"*."""
    text = render(report([pistol_map()]))
    assert "savu: TSpawn -> Middle (arvio) 0-5 s (1/1 kierroksesta)" in text


def test_only_a_derived_area_is_marked_as_an_estimate() -> None:
    """An observed area is an observation and must not be marked an estimate.

    The fixture has both directions: the smoke's detonation area is derived
    (``point_cloud``) and the flash's observed (``observed``). Without the
    observed case, removing the condition would mark **every** area an
    estimate, and the reading guide would claim the observations to be
    estimates -- without a single claim failing.
    """
    text = render(report([pistol_map()]))
    assert "savu: TSpawn -> Middle (arvio)" in text
    assert "valo: TSpawn -> MainHall 0-5 s" in text
    assert "MainHall (arvio)" not in text


def test_the_estimate_note_appears_only_when_something_was_estimated() -> None:
    observed_only = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        utility=[
                            use("smoke", "TSpawn", "Ramp", n=1, m=1, source="observed")
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([observed_only]))
    assert "(arvio)" not in text
    # The bare word "pistepilvestä" can no longer be searched for: since
    # Story 2.14 the stack's site groups are read from it too, and that
    # explanation is always written. The claim concerns the detonation area's
    # estimate in particular, so it is searched for in that sentence of its
    # own.
    assert "alue on luettu demon pistepilvestä" not in text


def test_estimated_detonation_area_is_marked_and_explained() -> None:
    text = render(report([pistol_map()]))
    assert "(arvio)" in text
    assert "alue on luettu demon pistepilvestä" in text


def test_throws_are_reported_when_they_outnumber_the_rounds() -> None:
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        utility=[use("flashbang", "TSpawn", None, n=1, m=1, throws=2)],
                    )
                ],
            )
        ],
    )
    assert "2 heittoa" in render(report([entry]))


def test_one_line_per_grenade_type_not_per_throw() -> None:
    """Five smoke rows would take five rows to say one thing."""
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        utility=[
                            use("smoke", "TSpawn", "BombsiteA", n=1, m=1),
                            use("smoke", "TSpawn", "BombsiteB", n=1, m=1),
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([entry]))
    assert len([row for row in text.splitlines() if row.startswith("- savu:")]) == 1
    assert "BombsiteA" in text
    assert "BombsiteB" in text


@pytest.mark.parametrize(
    "bucket,expected,unexpected",
    [
        ("10-20", "10-20 s", None),
        (UTILITY_BUCKET_ALL, None, "kaikki s"),
        (UTILITY_BUCKET_UNKNOWN, "(heittoaika tuntematon)", "tuntematon s"),
    ],
)
def test_special_bucket_names_are_not_glued_to_the_word_seconds(
    bucket: str, expected: str | None, unexpected: str | None
) -> None:
    """``kaikki`` ("all") and ``tuntematon`` ("unknown") are not time
    intervals.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        utility=[
                            use("smoke", "TSpawn", "Ramp", n=1, m=1, bucket=bucket)
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([entry]))
    if expected is not None:
        assert expected in text
    if unexpected is not None:
        assert unexpected not in text


def test_finnish_names_cover_every_grenade_type_the_parser_knows() -> None:
    """A new grenade type must be left neither without a name nor without a
    place in the order.
    """
    from pappascout.adapters.demo_parser import FIRE_ITEM_TYPES, GRENADE_TYPES

    known = set(GRENADE_TYPES.values()) | set(FIRE_ITEM_TYPES.values())
    assert known <= set(GRENADE_TYPE_FI)
    assert known <= set(GRENADE_ORDER)


# --- The armed count ------------------------------------------------------------


def test_armed_players_are_shown_with_the_caveat_that_they_are_not_kevlar() -> None:
    """The counter is "armour AND an upgraded weapon" -- it is not the number
    of kevlars.
    """
    text = render(report([pistol_map()]))
    assert "aseistettuja ostoajan lopussa: 0 (1/1 kierroksesta)" in text
    assert "kevlarien määrän" in text


def test_the_kevlar_caveat_is_absent_when_no_armed_line_was_written() -> None:
    entry = map_report("de_nuke", [side("T", [round_type("pistol", 1)])])
    assert "kevlarien määrä" not in render(report([entry]))


def test_an_armed_line_alone_still_gets_its_own_definition() -> None:
    """The armed row alone: the explanation is its own and not the pair's.

    The third branch of :func:`_player_counter_legend`. Without this test a
    lone armed row could be left without a definition or get a sentence that
    speaks of an armour row that is not in the report.
    """
    entry = map_report(
        "de_nuke",
        [side("T", [round_type("pistol", 1, players_armed=armed(1, {0: 1}))])],
    )
    text = render(report([entry]))
    assert "Aseistettu = panssari JA parannettu ase" in text
    assert "panssaroitu = panssari, aseesta riippumatta" not in text


def test_rounds_without_an_inventory_reading_are_reported() -> None:
    entry = map_report(
        "de_nuke",
        [side("T", [round_type("eco", 4, players_armed=armed(3, {0: 3}, unknown=1))])],
    )
    assert "havainto puuttuu 1 kierrokselta" in render(report([entry]))


# --- The armour count (Story 2.8) -----------------------------------------------


def test_the_armored_line_reads_the_product_owners_five_kevlars() -> None:
    """*"5 kevlaria"*: the armour row is in the report as a row of its own
    with its samples.
    """
    text = render(report([pistol_map()]))
    assert "panssaroituja ostoajan lopussa: 5 (1/1 kierroksesta)" in text


def test_the_two_counters_stand_side_by_side_and_differ() -> None:
    """Both rows in the same block with different numbers -- that difference
    is the observation.

    Without this test an implementation that renders the same distribution
    twice would pass every other claim.
    """
    view = view_of(report([pistol_map()]))
    lines = {
        line.label: tuple(claim.text for claim in line.claims)
        for line in view.maps[0].sides[0].round_types[0].lines
    }
    assert lines["aseistettuja ostoajan lopussa"] == ("0",)
    assert lines["panssaroituja ostoajan lopussa"] == ("5",)


def test_the_armored_line_follows_the_armed_line() -> None:
    """The order is part of the observation: the rows are read as a pair."""
    view = view_of(report([pistol_map()]))
    labels = [line.label for line in view.maps[0].sides[0].round_types[0].lines]
    assert (
        labels.index("panssaroituja ostoajan lopussa")
        == labels.index("aseistettuja ostoajan lopussa") + 1
    )


def test_the_legend_explains_the_two_counters_as_one_nested_pair() -> None:
    """The explanation is **one paragraph**, because the numbers are nested.

    Two separate sentences would leave "aseistettuja 0" and "panssaroituja 5"
    as two loose numbers. The reading guide has to state the subset relation,
    the shared tick and the shared divisor, because the rows' difference
    comes about from them.
    """
    text = render(report([pistol_map()]))
    assert "aseistetut ovat panssaroitujen osajoukko" in text
    assert "samalta tickiltä samasta pelaajajoukosta" in text
    assert "jakaja on sama" in text


def test_the_legend_says_the_counters_are_holdings_not_purchases() -> None:
    """Armour survives the round, so the number is not a buy observation.

    The exception is the pistol round, and that is exactly what saves the row
    *"5 kevlaria"*. Both halves belong in the reading guide: without the
    first the reader reads every eco as a purchase, without the second he
    doubts the pistol round too.
    """
    text = render(report([pistol_map()]))
    assert "hallussapitoa eivätkä ostoja" in text
    assert "Poikkeus on pistoolikierros" in text


def test_the_armored_line_alone_still_gets_its_own_definition() -> None:
    """The armour row alone: the explanation must not speak of the armed."""
    entry = map_report(
        "de_nuke",
        [side("T", [round_type("pistol", 1, players_armored=armored(1, {5: 1}))])],
    )
    text = render(report([entry]))
    assert "Panssaroitu = panssari ostoajan lopussa, aseesta riippumatta" in text
    assert "Aseistettu = panssari JA parannettu ase" not in text


def test_the_armored_legend_is_absent_when_no_armored_line_was_written() -> None:
    entry = map_report("de_nuke", [side("T", [round_type("pistol", 1)])])
    assert "aseesta riippumatta" not in render(report([entry]))


def test_the_ancient_ct_row_reads_no_kevlars() -> None:
    """*"Kitit ja duelit takaboksille piiloon (ei kevuja)"*: 1/5 kevlars."""
    entry = map_report(
        "de_ancient",
        [side("CT", [round_type("pistol", 1, players_armored=armored(1, {1: 1}))])],
    )
    assert "panssaroituja ostoajan lopussa: 1 (1/1 kierroksesta)" in render(
        report([entry])
    )


def test_a_wholly_unreadable_armor_observation_still_gets_a_line() -> None:
    """``m=0, rounds_unknown=n``: the row is written although there are no
    claims.

    Without the row the reader could not tell the branch "nobody had armour"
    (which would show as a zero) from the branch "the armour could not be
    read" (which would simply be missing) -- and preserving that difference
    is exactly why this column exists. The row is a bare label and a note,
    the same shape as on a round type without deaths.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [round_type("eco", 4, players_armored=armored(0, {}, unknown=4))],
            )
        ],
    )
    view = view_of(report([entry]))
    lines = [
        line
        for line in view.maps[0].sides[0].round_types[0].lines
        if line.label == "panssaroituja ostoajan lopussa"
    ]

    assert len(lines) == 1
    assert lines[0].claims == ()
    assert lines[0].note == "havainto puuttuu 4 kierrokselta"


def test_a_wholly_unreadable_armed_observation_still_gets_a_line() -> None:
    """The same branch on the armed row -- it was unverified even before
    this.
    """
    entry = map_report(
        "de_nuke",
        [side("T", [round_type("eco", 4, players_armed=armed(0, {}, unknown=4))])],
    )
    view = view_of(report([entry]))
    lines = [
        line
        for line in view.maps[0].sides[0].round_types[0].lines
        if line.label == "aseistettuja ostoajan lopussa"
    ]

    assert len(lines) == 1
    assert lines[0].claims == ()
    assert lines[0].note == "havainto puuttuu 4 kierrokselta"


def test_a_note_only_counter_line_still_gets_its_legend() -> None:
    """A label without a definition would be worse than a missing row.

    The flag is raised by the row being written and not by the existence of
    claims: the row "panssaroituja ostoajan lopussa: havainto puuttuu 4
    kierrokselta" is as new a concept to the reader as a row with claims.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [round_type("eco", 4, players_armored=armored(0, {}, unknown=4))],
            )
        ],
    )
    text = render(report([entry]))
    assert "panssaroituja ostoajan lopussa" in text
    assert "Panssaroitu = panssari ostoajan lopussa, aseesta riippumatta" in text


def test_rounds_without_an_armor_reading_are_reported() -> None:
    """A missing observation is said out loud -- it is not zero kevlars."""
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [round_type("eco", 4, players_armored=armored(3, {0: 3}, unknown=1))],
            )
        ],
    )
    assert "havainto puuttuu 1 kierrokselta" in render(report([entry]))


# --- First contact's presence list ----------------------------------------------


def test_presence_only_first_contact_areas_are_not_lost() -> None:
    """A presence without a corresponding distribution row is written on a
    row of its own.

    The report shows first contact from the sample point, because that is a
    superset. The assumption must not be left an assumption: if the
    aggregation produced an area only into the presence list, the observation
    would otherwise vanish without a trace.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[first_contact_position([area("Ramp", 1, {2: 1})], 1)],
                        first_contact=[
                            FirstContactArea(area="Ramp", n=1, m=1),
                            FirstContactArea(area="Heaven", n=1, m=1),
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([entry]))
    assert "ensikontakti, vain läsnäolo: Heaven (1/1 kierroksesta)" in text
    # Ramp is in the distribution already, so it is not repeated.
    assert text.count("Ramp") == 1


def test_no_presence_line_when_the_distribution_covers_everything() -> None:
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[first_contact_position([area("Ramp", 1, {2: 1})], 1)],
                        first_contact=[FirstContactArea(area="Ramp", n=1, m=1)],
                    )
                ],
            )
        ],
    )
    assert "vain läsnäolo" not in render(report([entry]))


# --- The summary's readability --------------------------------------------------


def test_a_team_without_a_name_says_so_instead_of_repeating_the_hash() -> None:
    """The source ``team_key`` means that no name was observed.

    No digest is written into the title in the name's place: it would read as
    if the team were named that. Story 2.12: neither is the digest written on
    the summary's row -- the body states the absence **with its reason** and
    says where the id is to be found, and the id itself is in the
    traceability chapter.
    """
    entry = report(
        [pistol_map()], display_name=TEAM_KEY, display_name_source="team_key"
    )
    text = render(entry)
    summary = summary_text(text)

    assert text.startswith("# Scouting-raportti -- joukkueen nimi ei tiedossa")
    assert "nimi ei ole tiedossa." in summary
    assert "team_clan_name" in summary
    assert TEAM_KEY not in summary
    # The row also says where the id is to be found: for a nameless team it
    # is all the reader has.
    assert TRACEABILITY_HEADING in summary
    assert f"**Joukkueen tunniste:** `{TEAM_KEY}`" in traceability_text(text)


def test_an_observed_name_that_looks_like_the_key_is_still_a_name() -> None:
    """The source settles it, not a comparison with the id (``_has_name``).

    This is the one case ``_has_name`` exists for: a team's clan name can
    look exactly like its own id, and a rule based on comparison would then
    claim the observation was missing -- that is, it would hide a name read
    from the demo.
    """
    entry = report(
        [pistol_map()], display_name=TEAM_KEY, display_name_source="clan_name"
    )
    text = render(entry)
    assert text.startswith(f"# {TEAM_KEY} -- scouting-raportti")
    assert "nimi ei ole tiedossa" not in text
    assert f"- **Joukkue:** {TEAM_KEY}" in text


def test_a_name_with_markdown_characters_cannot_break_the_report() -> None:
    """A string the demo gave must not turn into structure (P1).

    Every one of Markdown's structural characters occurs in CS2 names. The
    escaping is done at presentation time, not in the data: ``report.json``
    keeps the observation as it is.
    """
    entry = report(
        [pistol_map()],
        display_name="*|LOL|*",
        display_name_source="clan_name",
        name_alternatives=["<b>hax</b>"],
        roster=[RosterEntry(player_id="1", display_name="a_b  c" + chr(10) + "d")],
    )
    text = render(entry)

    assert text.startswith("# " + chr(92) + "*" + chr(92) + "|LOL" + chr(92) + "|" + chr(92) + "*")
    assert chr(92) + "<b" + chr(92) + ">hax" in text
    # A newline and runs of spaces are cleaned up visibly. The name is in the
    # summary on its own and in the traceability chapter as its id's label
    # after the ordinal, so the same escaping has to be verified in both.
    assert "a" + chr(92) + "_b c d" in summary_text(text)
    assert "**1. a" + chr(92) + "_b c d:** `1`" in traceability_text(text)
    # The model itself keeps the observation as it is.
    assert entry.team.display_name == "*|LOL|*"


def test_a_known_team_name_is_used_in_the_title() -> None:
    """The name into the title and the summary **without the id** (Story
    2.12).
    """
    text = render(report([pistol_map()]))

    assert text.startswith(f"# {TEAM_NAME} -- scouting-raportti")
    assert f"- **Joukkue:** {TEAM_NAME}" in summary_text(text)
    assert TEAM_KEY not in summary_text(text)
    assert f"**Joukkueen tunniste:** `{TEAM_KEY}`" in traceability_text(text)


def test_the_roster_speaks_names_and_the_chapter_carries_the_ids() -> None:
    """The summary in names, the traceability chapter in name -> SteamID64
    pairs.

    Story 2.6 wrote both on the same row. The rationale did not turn false --
    the id is still the only traceable value -- but the place changed: seven
    17-digit numbers beside the names make the body's row a list that is not
    read in the rush before a match.
    """
    text = render(report([pistol_map()], roster=STEAM_ROSTER))
    summary = summary_text(text)
    traceability = traceability_text(text)

    expected = f"- **Rosteri:** {len(STEAM_ROSTER)} pelaajaa "
    assert expected + "(havaittu demoista): " in summary
    for index, player in enumerate(STEAM_ROSTER, start=1):
        assert player.display_name in summary
        assert player.player_id not in summary
        assert (
            f"**{index}. {player.display_name}:** `{player.player_id}`"
            in traceability
        )


def test_a_player_without_a_name_keeps_the_row_and_says_the_name_is_missing() -> None:
    """A nameless player does not vanish: the count matches and the id is in
    its own chapter.

    Three claims together, because they are the same rule: a silently dropped
    player would shrink the roster, a nameless place on the row would make
    him nameless by accident, and without the traceability chapter's row his
    id would vanish from the report entirely.
    """
    named, nameless = STEAM_ROSTER[0], STEAM_ROSTER[1]
    entry = report(
        [pistol_map()],
        roster=[named, RosterEntry(player_id=nameless.player_id)],
    )
    text = render(entry)
    summary = summary_text(text)

    assert "- **Rosteri:** 2 pelaajaa (havaittu demoista): " in summary
    assert f"{named.display_name}, {UNNAMED_PLAYER}" in summary
    assert nameless.player_id not in summary
    assert (
        f"**2. {UNNAMED_PLAYER}:** `{nameless.player_id}`"
        in traceability_text(text)
    )


def test_conflicting_team_names_are_listed_instead_of_disappearing() -> None:
    """The most often observed into the title, the others listed -- the
    conflict does not vanish.
    """
    entry = report([pistol_map()], name_alternatives=["MM Academy"])
    text = render(entry)
    assert text.startswith(f"# {TEAM_NAME} -- scouting-raportti")
    assert "Muut havaitut nimet" in text
    assert "MM Academy" in text


def test_thresholds_are_listed_once_and_without_raw_dictionaries() -> None:
    """The thresholds are provenance; braces and repetition take up space for
    nothing.
    """
    entry = report(
        [pistol_map()],
        classify_thresholds={"full_equip_min": 4000, "armed_players_min": 3},
        thresholds_used={
            "thresholds": {"full_equip_min": 4000, "small_sample_rounds": 3},
            "aggregate": {"utility_seconds_buckets": [5.0, 10.0]},
        },
    )
    text = render(entry)
    summary = text.split("## Yhteenveto")[1].split("##")[0]
    assert "{" not in summary
    assert "}" not in summary
    assert summary.count("full_equip_min") == 1
    assert "small_sample_rounds 3" in summary
    assert "utility_seconds_buckets 5/10" in summary


def test_a_threshold_that_differs_between_the_two_records_is_shown_twice() -> None:
    """An identical pair is dropped, a differing one is not: the difference
    is precisely what says something.
    """
    entry = report(
        [pistol_map()],
        classify_thresholds={"full_equip_min": 4000},
        thresholds_used={"thresholds": {"full_equip_min": 3000}},
    )
    summary = render(entry).split("## Yhteenveto")[1].split("##")[0]
    assert "full_equip_min 4000" in summary
    assert "full_equip_min 3000" in summary


def test_a_naive_timestamp_is_not_labelled_utc() -> None:
    entry = report([pistol_map()], generated_at=datetime(2026, 8, 30, 12, 0))
    text = render(entry)
    assert "2026-08-30 12:00 (aikavyöhyke tuntematon)" in text
    assert "12:00 UTC" not in text


def test_an_offset_timestamp_is_converted_to_utc_not_relabelled() -> None:
    helsinki = timezone(timedelta(hours=3))
    entry = report(
        [pistol_map()], generated_at=datetime(2026, 8, 30, 12, 0, tzinfo=helsinki)
    )
    assert "2026-08-30 09:00 UTC" in render(entry)


# --- The bounds -----------------------------------------------------------------


def test_report_contains_no_interpretation() -> None:
    """No "fake", no "rush", no counter-strategy -- only the observations."""
    text = render(report([pistol_map(), default_map()])).lower()
    for word in ("fake", "rush", "antisträt", "kannattaa", "suositel"):
        assert word not in text, word


def test_normal_buying_is_never_explained_by_the_previous_round() -> None:
    """R-1: a normal buy is not explained by the previous round's win."""
    text = render(report([default_map()])).lower()
    for word in ("voitti", "edellisen kierroksen", "hävisi"):
        assert word not in text, word


def test_no_html_and_no_images() -> None:
    text = render(report([pistol_map()]))
    assert "<" not in text
    assert "![" not in text


# --- The round appendix ---------------------------------------------------------


def test_round_appendix_lists_the_paths_the_stage_resolved() -> None:
    """There are no per-round rows in report.json; render does not compute
    them.
    """
    text = render(report([pistol_map()]))
    assert "## Kierrosliite" in text
    assert "eivät ole report.jsonissa" in text
    assert ROUND_LISTS[0] in text


def test_round_appendix_without_paths_does_not_dangle_a_colon() -> None:
    """An empty list after a colon would read as if the list had been lost."""
    text = render_report(report([pistol_map()]), settings=DEFAULT_PRUNING)
    appendix = text.split("## Kierrosliite")[1].split("## Lukuohje")[0].strip()
    assert not appendix.endswith(":")
    assert "polkuja ei annettu" in appendix


def test_the_view_names_the_demos_but_not_the_paths() -> None:
    """Building a path belongs to the stage, which sees the archive."""
    assert round_list_demo_ids(report([pistol_map(), default_map()])) == [
        DEMO_ID,
        "inferno_vs_ryhmarama",
    ]


# --- Technical traceability (Story 2.12) ----------------------------------------

#: Four lineup ids that were joined into the same team.
#:
#: The shape is measured from the RCAVE report of 2026-08-31 (a 16-character
#: digest, four lineups on one team); the values are invented for the same
#: reason as :data:`STEAM_ROSTER`'s. **The last one is deliberately**
#: :data:`TEAM_KEY`: ``lineups_of_same_team`` always returns the target
#: included, so in a real report the team's own lineup is on the list -- and
#: that is exactly what makes the body's count easily one too large.
LINEUP_KEYS = ["0f1e2d3c4b5a6978", "1a2b3c4d5e6f7081", "2b3c4d5e6f708192", TEAM_KEY]


def crowded_report() -> Report:
    """A report that holds **every** source of an id and every exception.

    Four joined lineups, seven players with ids in SteamID64 shape, two named
    maps, one map whose name was not recognised, and one missing demo.
    Without every one of these the cleanliness claim would be true only of
    whatever the fixture happens to contain.
    """
    return report(
        [pistol_map(), default_map(), unknown_map()],
        roster=STEAM_ROSTER,
        lineup_keys=LINEUP_KEYS,
        missing_demos=[missing_demo()],
    )


def literal_identifiers(entry: Report) -> list[str]:
    """The fixture's ids **by name**, as they would appear in the report.

    The shape (:data:`IDENTIFIER_SHAPE`) does not recognise demo ids and
    cannot: ``ANCIENT_vs_RCAVE_VETERANS`` does not resemble a digest. They
    therefore have to be listed, and the list is derived from the report and
    not hand-written -- a hand-written one would fall behind as soon as the
    fixture gets a new demo.
    """
    names = [entry.team.key, *entry.team.lineup_keys]
    names += [row.player_id for row in entry.team.roster]
    for map_report in entry.maps:
        names += map_report.map_demo_ids
    names += [missing.match for missing in entry.missing_demos]
    return sorted(set(names))


def test_no_identifier_appears_in_the_body_outside_the_three_exceptions() -> None:
    """The acceptance criterion whole, exceptions included.

    The claim is made **chapter by chapter** and not as one string, and every
    chapter is either checked or released by a named rule. An earlier version
    of this test cut the round appendix out before the check, that is, it
    could not see a leak from precisely the place where ids still are -- and
    its rule was therefore invisible.

    Three exceptions, and which rule releases which:

    1. ``Kierrosliite`` -- the id is **in a path**, and a path is a code
       span. The exception is therefore narrower than the chapter: outside
       code spans the round appendix is checked like any other body.
    2. ``Puuttuvat demot`` -- the id is part of a command the reader copies,
       and the command does not work without it. The whole chapter is
       released.
    3. An unrecognised map's **heading** -- then ``map_name`` *is* the
       ``map_demo_id``, that is, the id is the map's only name. Only the
       heading is released; the chapter's content has to be clean.
    """
    entry = crowded_report()
    text = render(entry)
    view = view_of(entry, round_list_paths=ROUND_LISTS)
    unnamed_headings = {m.heading for m in view.maps if m.name_unknown}
    literals = literal_identifiers(entry)

    def assert_clean(what: str, where: str) -> None:
        assert IDENTIFIER_SHAPE.search(what) is None, (
            where,
            IDENTIFIER_SHAPE.findall(what),
        )
        for literal in literals:
            assert literal not in what, (where, literal)

    checked_headings = 0
    for heading, content in report_sections(text):
        if heading not in unnamed_headings:  # exception 3 is only the heading
            assert_clean(heading, f"heading {heading!r}")
            checked_headings += 1
        if heading in ("Puuttuvat demot", TRACEABILITY_HEADING):
            continue  # exception 2, and the chapter that is for ids
        if heading == "Kierrosliite":
            content = CODE_SPAN.sub("", content)  # exception 1 is narrow
        assert_clean(content, f"chapter {heading!r}")

    # The fixture covers exception 3 -- without this the loop could have
    # passed without meeting it once.
    assert len(unnamed_headings) == 1
    # An exact number and not a lower bound: a loop that checks nothing would
    # pass a lower bound. Ten chapters, of which the unrecognised map's
    # heading is released.
    assert checked_headings == 9


def test_every_identifier_the_body_dropped_is_in_the_chapter() -> None:
    """Removing is a **move**: nothing falls out on the way.

    The missing demo's id is not in the chapter and does not belong there: it
    is not any map branch's demo but a demo that stayed outside the sample,
    and it is in the body with its command. The claim therefore lists exactly
    the sources that left the body.
    """
    entry = crowded_report()
    traceability = traceability_text(render(entry))

    moved = [entry.team.key, *entry.team.lineup_keys]
    moved += [row.player_id for row in entry.team.roster]
    for map_report in entry.maps:
        moved += map_report.map_demo_ids
    for identifier in moved:
        assert identifier in traceability, identifier
    assert missing_demo().match not in traceability


def test_the_exceptions_are_not_vacuous() -> None:
    """The three exceptions are real, not written just in case.

    Without this the cleanliness test could pass because the released
    chapters hold no id at all -- and the reading guide's sentence about the
    exceptions would be false in the other direction.
    """
    entry = crowded_report()
    text = render(entry)

    paths = section_text(text, "Kierrosliite")
    assert TEAM_KEY in paths
    assert IDENTIFIER_SHAPE.search(CODE_SPAN.sub("", paths)) is None

    missing = section_text(text, "Puuttuvat demot")
    assert missing_demo().match in missing
    assert f"uv run pappascout parse {missing_demo().match}" in missing

    headings = [heading for heading, _ in report_sections(text)]
    assert FACEIT_DEMO_ID in " ".join(headings)


def test_the_legend_names_all_three_exceptions() -> None:
    """The report must not claim more about itself than is true.

    The reading guide is the place where the report states its own rules. If
    it says "the body speaks in names only", three chapters above it make the
    sentence a lie.
    """
    legend = section_text(render(crowded_report()), "Lukuohje")

    assert TRACEABILITY_HEADING in legend
    assert "kierrosliitteen polut" in legend
    assert "puuttuvan demon rivi" in legend
    assert "nimeä ei tunnistettu" in legend
    assert "vain nimillä" not in legend


def test_the_chapter_note_separates_identifiers_from_thresholds() -> None:
    """A threshold is not an id, and the chapter's explanation says why.

    The thresholds meet literally the same criterion as the ids (the machine
    needs them, the human does not), but the difference is genuine: a
    threshold says **how the number was computed**, so the claim cannot be
    judged without it. Without this sentence the next reader would move them
    too.
    """
    traceability = traceability_text(render(report([pistol_map()])))

    assert _TRACEABILITY_NOTE in traceability
    assert "Kynnykset" in _TRACEABILITY_NOTE
    assert "miten luku laskettiin" in _TRACEABILITY_NOTE


def test_the_traceability_chapter_is_the_last_one() -> None:
    """The ids stop being the first thing the reader sees.

    The chapter is last and not anywhere: the reading guide says where the
    ids are to be found, and the chapter cannot come before it -- the ids
    would then be back in the middle of the body.
    """
    headings = [heading for heading, _ in report_sections(render(crowded_report()))]

    assert headings[-3:] == ["Kierrosliite", "Lukuohje", TRACEABILITY_HEADING]


def test_the_chapter_name_is_the_same_in_the_template_and_in_the_code() -> None:
    """The heading belongs to the template, but the report's own text refers
    to it.

    Without this claim, renaming the chapter would leave in the summary and
    the reading guide two references to a chapter that does not exist -- and
    no test would notice, because both references read the same constant.
    """
    assert "## " + TRACEABILITY_HEADING in template_text()


def test_the_roster_rows_are_in_the_same_order_as_the_names_in_the_body() -> None:
    """The promise about the order that two docstrings give is checkable.

    The row's label is ``<ordinal>. <the same string as in the body>``, so
    the reader finds his player by counting. Without this test the promise
    would be a mere sentence: changing the loops' order would break nothing.
    """
    roster = [
        RosterEntry(player_id="76561190000000101", display_name="cee"),
        RosterEntry(player_id="76561190000000102"),
        RosterEntry(player_id="76561190000000103", display_name="aaa"),
    ]
    text = render(report([pistol_map()], roster=roster))

    roster_row = next(
        line
        for line in summary_text(text).splitlines()
        if line.startswith("- **Rosteri:**")
    )
    listed = roster_row.split("(havaittu demoista): ")[1].split(", ")
    labels = [
        line.split("**")[1].rstrip(":")
        for line in traceability_text(text).splitlines()
        if line.startswith("- **") and line[4].isdigit()
    ]

    assert listed == ["cee", UNNAMED_PLAYER, "aaa"]
    assert labels == [f"{n}. {name}" for n, name in enumerate(listed, start=1)]


def test_two_players_without_a_name_get_two_distinguishable_rows() -> None:
    """A name is not a unique key, so the label cannot be the name alone.

    Two nameless players would without the ordinal produce two identical
    rows, and the reader could not say which SteamID64 belongs to which. The
    same goes for two with the same name, which is ordinary in CS2.
    """
    roster = [
        RosterEntry(player_id="76561190000000201"),
        RosterEntry(player_id="76561190000000202"),
        RosterEntry(player_id="76561190000000203", display_name="kaksoset"),
        RosterEntry(player_id="76561190000000204", display_name="kaksoset"),
    ]
    traceability = traceability_text(render(report([pistol_map()], roster=roster)))

    for index, player in enumerate(roster, start=1):
        name = player.display_name or UNNAMED_PLAYER
        assert f"- **{index}. {name}:** `{player.player_id}`" in traceability
    rows = [line for line in traceability.splitlines() if line.startswith("- **")]
    assert len(rows) == len(set(rows))


def test_several_lineups_are_a_checkable_count_with_its_threshold() -> None:
    """I/O matrix: four ``lineup_keys``.

    The row's number is **checkable**: ``lineup_keys`` contains the target's
    own lineup, so a bare count would read as if one more had been joined
    than was. The row gives the number joined and the total, and the
    threshold the decision was made on -- as the neighbouring row gives its
    own.
    """
    entry = report([pistol_map()], lineup_keys=LINEUP_KEYS)
    text = render(entry)
    summary = summary_text(text)
    traceability = traceability_text(text)

    assert (
        "- **Kokoonpanot:** 3 muuta kokoonpanoa liitetty samaksi joukkueeksi "
        f"vähintään {MIN_COMMON} yhteisen pelaajan perusteella; yhteensä 4 "
        f"kokoonpanoa, tunnisteet luvussa {TRACEABILITY_HEADING}" in summary
    )
    for key in LINEUP_KEYS:
        assert key not in summary
        assert "`" + key + "`" in traceability
    assert "**Kokoonpanotunnisteet:**" in traceability


def test_the_lineup_row_says_the_rule_in_words_when_the_threshold_is_absent(
) -> None:
    """A missing threshold must not make the rendering invent a number.

    The same rule as with the pattern limit: the value is read from the
    report, and if it is not there, the row gives the rationale in words. A
    hardcoded threshold would be computation in the wrong layer.
    """
    entry = report(
        [pistol_map()], lineup_keys=LINEUP_KEYS, thresholds_used={"thresholds": {}}
    )
    summary = summary_text(render(entry))

    assert (
        "- **Kokoonpanot:** 3 muuta kokoonpanoa liitetty samaksi joukkueeksi "
        "yhteisten pelaajien perusteella; yhteensä 4 kokoonpanoa, tunnisteet "
        f"luvussa {TRACEABILITY_HEADING}" in summary
    )


def test_a_single_lineup_is_the_team_key_and_is_not_printed_twice() -> None:
    """I/O matrix: one ``lineup_key``.

    There is no row in the body at all -- "joining" one lineup into the same
    team is not an observation. Neither is there a row of its own in the
    traceability chapter, but for a different reason from the one previously
    supposed: ``team.key`` **is** that lineup id, so a row of its own would
    repeat the team row word for word. The id therefore does not vanish, and
    the team row says it is both.
    """
    text = render(report([pistol_map()]))
    summary = summary_text(text)
    traceability = traceability_text(text)

    assert "- **Kokoonpanot:**" not in summary
    # The row's PREFIX and not the bare word: the team row's own text
    # mentions the lineup id in lower case, and a bare word search would pass
    # by accident -- or fail if the sentence were written with a capital.
    assert "- **Kokoonpanotunniste" not in traceability
    assert (
        f"- **Joukkueen tunniste:** `{TEAM_KEY}` -- sama arvo kuin joukkueen "
        "ainoa kokoonpanotunniste" in traceability
    )
    assert traceability.count(TEAM_KEY) == 1


def test_an_empty_roster_says_the_source_was_empty_and_lists_nobody() -> None:
    """I/O matrix: an empty ``roster``.

    The body says the source is empty as before, and the traceability chapter
    has no roster row at all -- an empty name -> id pair would be an invented
    row.
    """
    entry = report([pistol_map()], roster=[])
    text = render(entry)

    assert (
        "- **Rosteri:** ei pelaajia (havaittu demoista -- lähde tyhjä)"
        in summary_text(text)
    )
    traceability = traceability_text(text)
    assert f"`{TEAM_KEY}`" in traceability
    assert UNNAMED_PLAYER not in traceability


def test_a_map_shows_only_the_demo_count_and_the_chapter_names_the_demos() -> None:
    """I/O matrix: ``map_demo_ids`` per map.

    The map's heading gives the **number** of demos; which demos summed into
    one branch is a traceability question and therefore in a chapter of its
    own.
    """
    demos = [MISSING_DEMO_ID, FACEIT_DEMO_ID]
    text = render(report([demo_map(demos, name="de_ancient")]))
    traceability = traceability_text(text)

    assert "`de_ancient` -- 2 kierrosta, 2 demoa" in text
    for demo_id in demos:
        assert demo_id not in summary_text(text)
        assert "`" + demo_id + "`" in traceability
    assert "- **`de_ancient`:** " in traceability


def test_a_map_whose_name_was_not_recognised_gets_a_label_that_says_so() -> None:
    """I/O matrix: ``map_name_source`` is ``unknown``.

    Then ``map_name`` is the ``map_demo_id`` itself, so the name is not fit
    to be the label: the row would be ``- **<id>:** `<the same id>``` and
    would say nothing. The label says instead what it is about, and the
    ordinal says which map chapter the row concerns.
    """
    entry = report([pistol_map(), unknown_map()])
    traceability = traceability_text(render(entry))

    assert f"- **kartta 2, nimeä ei tunnistettu:** `{FACEIT_DEMO_ID}`" in traceability
    assert f"**`{FACEIT_DEMO_ID}`:**" not in traceability


def test_two_unrecognised_maps_do_not_get_the_same_label() -> None:
    """A label is a label only if it identifies its row.

    Without the ordinal two unrecognised maps would produce two rows with the
    same label, that is, the same fault as with two nameless players.
    """
    other = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1"
    entry = report([unknown_map(), unknown_map(other)])
    traceability = traceability_text(render(entry))

    assert f"- **kartta 1, nimeä ei tunnistettu:** `{FACEIT_DEMO_ID}`" in traceability
    assert f"- **kartta 2, nimeä ei tunnistettu:** `{other}`" in traceability


def test_a_map_name_with_markdown_characters_keeps_the_row_a_single_pair() -> None:
    """A genuine fault case: a workshop map broke the row.

    Story 2.11 decided that a name read from the header is not validated
    against a map pool, so ``*|Aim|* Botz [beta]`` is a legal observation.
    Bare, the label's bold was left unclosed and the row stopped reading as a
    label/value pair -- and it is that row that carries the demo ids. A code
    span keeps the row one pair with **any** string, without the map getting
    a different spelling from the map chapter's heading.
    """
    name = "*|Aim|* Botz [beta]"
    entry = report([demo_map([MISSING_DEMO_ID], name=name)])
    traceability = traceability_text(render(entry))

    assert f"- **`{name}`:** `{MISSING_DEMO_ID}`" in traceability
    # The same spelling as in the map chapter's heading: escaping would
    # produce another, and the report is read raw as well.
    assert f"## `{name}` -- " in render(entry)
    row = next(
        line for line in traceability.splitlines() if line.startswith("- **")
    )
    assert row.count("**") == 2


def test_a_demo_id_is_a_code_span_so_it_stays_usable() -> None:
    """An id's worth is that it can be copied out of the report as it is.

    :func:`markdown_text` would protect the underscores but would make the
    value a different string, which no longer matches any directory in the
    archive.
    """
    traceability = traceability_text(render(report([demo_map([MISSING_DEMO_ID])])))

    assert "`" + MISSING_DEMO_ID + "`" in traceability
    assert chr(92) + "_" not in traceability


def test_a_backtick_in_an_identifier_falls_back_to_escaping() -> None:
    """The backtick is the only character a code span cannot contain.

    A broken code span would set the rest of the report wrongly, which is
    worse than losing copyability on one row. On Windows the backtick is
    legal in a file name, that is possible in a demo id.
    """
    demo_id = "demo" + chr(96) + "vs" + chr(96) + "toinen"
    traceability = traceability_text(render(report([demo_map([demo_id])])))

    assert (
        "demo" + chr(92) + chr(96) + "vs" + chr(92) + chr(96) + "toinen"
        in traceability
    )
    assert chr(96) + "demo" not in traceability


def test_an_empty_report_still_gets_the_traceability_chapter() -> None:
    """A team has an id even when there are no maps.

    An empty report is precisely the case in which the reader asks "which
    team was this about" -- and the id is the only answer there is to that.
    The template sets the chapter unconditionally, so an empty sequence would
    produce a bare heading; that is not guarded, because :func:`build_view`
    cannot produce it.
    """
    view = view_of(report([]))

    assert view.traceability
    assert view.traceability[0].label == "Joukkueen tunniste"
    assert f"`{TEAM_KEY}`" in traceability_text(render(report([])))


# --- The structure's coverage ---------------------------------------------------


def test_every_round_type_has_a_place_in_the_report() -> None:
    """A new round type cannot vanish from the report in silence."""
    assert set(ROUND_TYPE_ORDER) == set(ROUND_TYPES)
    assert PATTERN_ROUND_TYPES <= set(ROUND_TYPES)


def test_pistol_and_saving_rounds_come_before_default() -> None:
    """The order is spec-2-4's structure: pistol, the savings, the default."""
    order = list(ROUND_TYPE_ORDER)
    assert order.index("pistol") == 0
    for saving in ("eco", "force", "half"):
        assert order.index(saving) < order.index("full")


@pytest.mark.parametrize(
    "raw,expected",
    [("eco", "Eco"), ("OT", "OT"), ("HE", "HE"), ("puoliosto", "Puoliosto"), ("", "")],
)
def test_capitalising_a_heading_leaves_the_other_letters_alone(
    raw: str, expected: str
) -> None:
    """``str.capitalize`` would turn the abbreviation "OT" into "Ot".

    The round types' Finnish names are ordinary words today, but the list is
    in ``constants`` and not here -- the rule must not lean on whatever
    happens to be there now.
    """
    from pappascout.render.view import _capitalise

    assert _capitalise(raw) == expected


def test_a_round_type_heading_uses_the_finnish_name_capitalised() -> None:
    entry = map_report("de_nuke", [side("T", [round_type("ot", 4)])])
    text = render(report([entry]))
    assert "**Jatkoaika** (4 kierrosta)" in text


def test_view_is_built_without_touching_the_report() -> None:
    """``Report`` is a frozen contract; the view must not touch it up."""
    entry = report([pistol_map()])
    before = entry.model_dump_json()
    view_of(entry, round_list_paths=ROUND_LISTS)
    assert entry.model_dump_json() == before


# --- The template ---------------------------------------------------------------


def test_template_digest_is_the_sha256_of_the_template_itself() -> None:
    """The digest has to be bound to the template's content, not to any old
    value.

    Without this claim the digest could be replaced by a constant and both
    tests about it would pass -- whereupon editing the template would produce
    a different report under an unchanged ``params_hash``, that is, exactly
    the state the digest was added to prevent.
    """
    expected = hashlib.sha256(template_text().encode("utf-8")).hexdigest()
    assert template_digest() == expected
    assert re.fullmatch(r"[0-9a-f]{64}", template_digest())


def test_editing_the_template_changes_both_the_digest_and_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The digest and the rendering read the same file -- not two different
    caches.
    """
    import pappascout.render as render_pkg

    original = template_text()
    edited = tmp_path / "edited.md.j2"
    edited.write_text(original + "\nEXTRA-ROW\n", encoding="utf-8")

    before_digest = template_digest()
    before_text = render(report([pistol_map()]))

    monkeypatch.setattr(render_pkg, "template_path", lambda: edited)
    assert template_digest() != before_digest
    assert render(report([pistol_map()])) != before_text
    assert "EXTRA-ROW" in render(report([pistol_map()]))


def test_a_broken_template_is_a_pappascout_error_not_a_jinja_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Jinja's ``TemplateError`` does not inherit from ``PappascoutError``."""
    from pappascout.errors import PappascoutError

    import pappascout.render as render_pkg

    broken = tmp_path / "broken.md.j2"
    broken.write_text("{% for x in %}", encoding="utf-8")
    monkeypatch.setattr(render_pkg, "template_path", lambda: broken)

    with pytest.raises(PappascoutError, match="Rendering the report template"):
        render(report([pistol_map()]))


# --- The whole document's shape -------------------------------------------------

#: The golden output of a small report.
#:
#: Substring claims do not see the document's shape: blank lines, the gaps
#: between headings, nor what ``render._tidy`` does to the template's output.
#: The shape comes about in two places (the template and the post-processing),
#: so it is locked whole. This test failing means that the report's layout
#: changed -- which is sometimes right, and then this text is updated.
GOLDEN = """\
# MatureMayhem -- scouting-raportti

## Yhteenveto

- **Joukkue:** MatureMayhem
- **Rosteri:** 5 pelaajaa (havaittu demoista): pelaaja1, pelaaja2, pelaaja3, pelaaja4, pelaaja5
- **Otanta:** 1 demo, 4 kierrosta (demoa/kierrosta: liiga 0 / 0, muut 0 / 0, tuntematon 1 / 4)
- **Liigatieto:** yhdenkään demon lajia ei ole vahvistettu: kaikki ovat lokerossa tuntematon, eikä otannassa ole yhtään varmistettua liigaottelua
- **Rosteriluokka:** yhdenkään demon rosteriluokkaa ei ole vahvistettu: kaikki ovat lokerossa tuntematon, eikä otanta erottele 5/5- ja 4/5-karttoja
- **Pieni otanta:** alle 3 kierrosta merkitään (pieni otanta); havaintoa ei silti piiloteta
- **Luokittelun kynnykset:** full_equip_min 4000
- **Aggregoinnin kynnykset:** advance_area_min_observations 20, advance_max_sample_s 30, advance_min_players 1, advance_t_share 0,8, crunch_min_players 2, crunch_min_sources 2, small_sample_rounds 3, stack_group_margin 1,25, stack_min_players 4, stack_site_separation_min 2, team_identity_min_common 3
- **Karsinnan säännöt:** drop_saturated_equipment_lines kyllä, max_kill_areas 3, max_utility_targets 2, merge_equal_equipment_lines kyllä, skip_sample_seconds ei yhtään
- **Aineisto koottu:** 2026-08-30 12:00 UTC (pappascout 0.1.0)

## Poikkeamat

- CT-eteneminen (`de_nuke`, CT-puoli, eco): Lobby (1/4 kierroksesta, T-osuus 0,89 alueen 64 havainnosta)
  - kierros 23: 2 pelaajaa 30 s kohdalla

## `de_nuke` -- 4 kierrosta, 1 demo

### T-puoli -- 4 kierrosta

**Eco** (4 kierrosta) -- vain toistuvat kuviot
- 6 s: Ramp 2 (3/4 kierroksesta)
- ensikontakti (mediaani 9,1 s): Ramp 1 (3/4 kierroksesta)
- utility: savu 1 kpl (3/4 kierroksesta)
- aseistettuja ostoajan lopussa: 0 (4/4 kierroksesta)
- panssaroituja ostoajan lopussa: 3 (4/4 kierroksesta)
- ensimmäinen kuolema (mediaani 24,0 s): Cave (3/3 kierroksesta) -- ei omia kuolemia 1 kierroksella
- tapot alueittain: Middle (4/7 taposta), BombsiteB (3/7 taposta)
- *Vain kuviot, jotka toistuvat vähintään 3 kierroksella; jokainen havainto ylitti kynnyksen.*

## Kierrosliite

Kierros, tyyppi ja perustelu eivät ole report.jsonissa: se sisältää reunajakaumia, ei kierroskohtaisia rivejä. Liite on classify-vaiheen kierroslistassa, jossa jokaisella kierroksella on päätös ja sen lähtöarvot:

- `C:\\arkisto\\classified\\aaaaaaaaaaaaaaaa\\Ancient_vs_kaljukostaja.md`

## Lukuohje

- Jokainen väite kantaa otantansa muodossa (n/m kierroksesta): n on kierrokset, joissa havainto tehtiin, m kyseisen kierrostyypin kaikki kierrokset. Mediaanin otanta rivin otsikossa (esimerkiksi "mediaani 14,2 s, 7/9 kierroksesta") noudattaa tätä sääntöä: se kertoo, monellako kierroksella ajoitus mitattiin. Saman rivin aluevaateet laskevat sen sijaan vain niitä kierroksia, joilla havainto oli olemassa, joten niiden nimittäjä on pienempi.
- Ensikontaktin rivi kertoo elossa olevat pelaajat alueittain sillä hetkellä, kun kierroksen ensimmäinen ristiinpuolinen osuma tapahtui.
- Luvun Poikkeamat T-osuus on **demon oma havainto** siitä, kumman puolen aluetta alue on: se on alueen elossa-havainnoista aikanäytepisteillä laskettu T-puolen osuus, **molempien joukkueiden** riveistä. Ei karttatietokantaa eikä käsin annettua aluejakoa -- ja eri demo voi antaa samalle alueelle eri osuuden, joten havaintomäärä on osuuden vieressä. Alue on T:n aluetta, kun osuus on vähintään 0,80 ja alueella on vähintään 20 havaintoa; sitä vähemmällä alue ei ole kummankaan puolen aluetta eikä tuota poikkeamaa.
- **CT-eteneminen**: subjektin CT-pelaaja alueella, joka on siinä demossa T:n hallussa, **säästökierroksella** (eco, force tai puoliosto). Vähintään 1 pelaaja alueella ja havainto enintään 30 sekunnin kohdalla kierroksen alusta.
- **Crunch**: sama T:n alue, mutta pelaajien on **saavuttava** sinne yhtä aikaa eri suunnista -- lähtösuunta on pelaajan oma alue edellisellä näytepisteellä. Vähintään 2 pelaajaa ja 2 eri suuntaa. **Crunchia ei ole rajattu kierrostyyppiin**, toisin kuin etenemistä, joten sen otanta on puolen kaikki kierrokset ja nimiö kertoo millä kierrostyypeillä se havaittiin. Sama kierros voi siis tuottaa molemmat rivit, ja täysi osto vain crunchin.
- **Stack**: subjektin puolustus kasautuneena yhden siten ympärille. Alueryhmä on **johdettu tästä demosta**: jokaisen alueen keskipiste lasketaan demon omasta pistepilvestä, ja alue kuuluu lähemmän siten ryhmään, jos toinen site on vähintään 1,25 kertaa kauempana. Ei karttatietokantaa eikä käsin annettua aluejakoa. Osuma vaatii vähintään 4 pelaajaa saman siten ryhmässä ja vähintään yhden heistä sitellä itsellään; spawnissa seisova ei laske. Rivin luku on muotoa 4/5 -- ryhmässä olleet kaikista elossa olleista. **Stackia ei ole rajattu kierrostyyppiin** eikä se lue alueen T-osuutta, joten se ei ole kummankaan toisen säännön tiukempi eikä löysempi muoto.
- Stackin kattavuus on 1/1 CT-kierroksesta. Jokaiselta demolta saatiin siteryhmät.
- Aseistettu = panssari JA parannettu ase ostoajan lopussa; panssaroitu = panssari, aseesta riippumatta. Luvut ovat **sisäkkäisiä**: aseistetut ovat panssaroitujen osajoukko, molemmat on luettu samalta tickiltä samasta pelaajajoukosta, ja jakaja on sama. Rivien ero on siis se havainto -- pistoolikierroksella aseistettuja on tyypillisesti 0 (800 $ ei riitä sekä kevlariin että parannettuun aseeseen), joten panssaririvi on se, joka kertoo kevlarien määrän.
- Molemmat luvut ovat **hallussapitoa eivätkä ostoja**: panssari ja ase säilyvät kierroksen yli hengissä selvinneellä, eikä vaurioitunutta panssaria eroteta ehjästä. Poikkeus on pistoolikierros -- puoliaika alkaa puhtaalta pöydältä, joten siellä luvut kertovat mitä ostettiin.
- Tapot alueittain: alue on **ampujan** oma alue tappohetkellä, ja otanta (n/m taposta) laskee tappoja eikä kierroksia -- kierrostyypillä on yleensä enemmän tappoja kuin kierroksia.
- Runko puhuu nimillä: joukkueen ja kokoonpanojen tiivisteet, pelaajien SteamID64 ja karttojen demotunnisteet ovat raportin viimeisessä luvussa Tekninen jäljitettävyys. Kolme poikkeusta, joissa tunniste on rungossa siksi että se on siellä ainoa käyttökelpoinen muoto: kierrosliitteen polut, puuttuvan demon rivi (tunniste on osa komentoa, jonka voi kopioida) ja kartta, jonka nimeä ei tunnistettu (tunniste on kartan ainoa nimi).
- Raportti kuvaa vain havainnot. Tulkinta ja vastastrategia ovat lukijan.

## Tekninen jäljitettävyys

Tunnisteet, jotka eivät ole rungossa: joukkueen ja kokoonpanojen tiivisteet, pelaajien SteamID64 ja karttojen demotunnisteet. Mitään ei ole poistettu -- ne ovat täällä, koska ne palvelevat vain jäljittämistä. Kynnykset, työkaluversiot ja aikaleima jäivät yhteenvetoon, koska ne kertovat miten luku laskettiin, eikä väitettä voi arvioida ilman niitä; tunniste ei muuta yhtäkään raportin lukua. Rungossa tunniste on vain siellä, missä se on ainoa käyttökelpoinen muoto: kierrosliitteen polussa, puuttuvan demon komennossa ja kartassa, jonka nimeä ei tunnistettu.

- **Joukkueen tunniste:** `aaaaaaaaaaaaaaaa` -- sama arvo kuin joukkueen ainoa kokoonpanotunniste
- **1. pelaaja1:** `1`
- **2. pelaaja2:** `2`
- **3. pelaaja3:** `3`
- **4. pelaaja4:** `4`
- **5. pelaaja5:** `5`
- **`de_nuke`:** `Ancient_vs_kaljukostaja`
"""


def golden_report() -> Report:
    """A small report whose whole output is locked into :data:`GOLDEN`.

    **Every row repeats at least three times** (2026-09-11). ``eco`` is a
    pattern round type now, so a bar seen once or twice is not written at
    all -- and a golden whose rows the threshold removes locks the shape of
    an empty block rather than of a report. The numbers were raised for
    that and for nothing else; the rows, their order and their labels are
    the same ones.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "eco",
                        4,
                        positions=[
                            position(6.0, [area("Ramp", 4, {2: 3, 0: 1})], 4),
                            first_contact_position([area("Ramp", 4, {1: 3, 0: 1})], 4),
                        ],
                        utility_counts=[counts("smoke", 4, {1: 3, 0: 1})],
                        # Both player counters are included for the same
                        # reason as the deaths: only the golden locks that
                        # they are consecutive and that the reading guide
                        # gets two different explanations and not one. The
                        # values differ (0 and 3) so that rule 2 does not
                        # merge them, and neither is 5, so that rule 1 does
                        # not drop one.
                        players_armed=armed(4, {0: 4}),
                        players_armored=armored(4, {3: 4}),
                        # The deaths are included because it is this fixture
                        # that locks the document's shape: without them the
                        # rows' place, the note and the new reading-guide
                        # paragraph would be locked nowhere.
                        death_report=deaths(
                            first={"Cave": 3},
                            rounds_missing=1,
                            median=24.0,
                            kills={"Middle": 4, "BombsiteB": 3},
                        ),
                    )
                ],
            )
        ],
    )
    # One anomaly, so that the golden locks the anomaly chapter's shape and
    # place as well. The empty chapter is locked in a test of its own -- both
    # variants cannot be in the same output, and this is the one in which the
    # row's shape can be seen.
    return report(
        [entry],
        anomalies=[
            anomaly(
                map_name="de_nuke",
                area="Lobby",
                rounds=[anomaly_round(round_no=23, seconds=[30.0])],
                orientation=[(DEMO_ID, 0.89, 64)],
                m=4,
            )
        ],
    )


def test_the_whole_document_matches_the_golden_output() -> None:
    assert render(golden_report()) == GOLDEN


# --- Deaths and kills (Story 2.7) -----------------------------------------------


#: The number of rounds in :func:`death_report`'s block.
DEATH_ROUNDS = 4


def death_report(**kwargs) -> Report:
    """A report of one pistol-round block with the given deaths part.

    ``rounds_missing`` is filled in so that the deaths cover the whole sample
    -- otherwise the model's cross-check would reject every call in which the
    deathless rounds have not been counted by hand.
    """
    kwargs.setdefault(
        "rounds_missing", DEATH_ROUNDS - sum((kwargs.get("first") or {}).values())
    )
    entry = round_type(
        "pistol", DEATH_ROUNDS, death_report=deaths(**kwargs)
    )
    return report([map_report("de_ancient", [side("CT", [entry])])])


def test_a_round_type_gets_at_most_two_death_lines() -> None:
    """The bound: at most two rows per round type.

    The report already runs to hundreds of rows, and the deaths were added to
    explain the other rows and not to be a chapter of their own. The rows are
    counted from the **list rows**, not by a string search: the view is what
    the template sets.
    """
    view = view_of(
        death_report(
            first={"Cave": 3, "Long": 1},
            median=24.0,
            kills={"Middle": 4, "BombsiteB": 2},
        )
    )
    lines = view.maps[0].sides[0].round_types[0].lines
    labels = [line.label for line in lines]
    # Two as a literal and not as the constant: the constant compared with
    # itself is a tautology that would pass even when the limit is raised by
    # accident. The same rule as with pinning the schema version.
    assert len(lines) == 2
    assert MAX_DEATH_LINES == 2
    assert labels == ["ensimmäinen kuolema (mediaani 24,0 s)", "tapot alueittain"]


def test_the_first_death_line_reads_like_the_target_analysis() -> None:
    """*"Ensimmäinen kuolema mediaani 24 s, useimmin Cave (3/4
    kierroksesta)"*.
    """
    text = render(death_report(first={"Cave": 3, "Long": 1}, median=24.0))
    assert "- ensimmäinen kuolema (mediaani 24,0 s): Cave (3/4 kierroksesta)" in text
    assert "Long (1/4 kierroksesta)" in text


def test_the_kill_line_reads_like_the_target_analysis() -> None:
    """*"Tapot: Middle 4, BombsiteB 2"* -- the area and the count, largest
    first.
    """
    text = render(death_report(kills={"BombsiteB": 2, "Middle": 4}))
    assert (
        "- tapot alueittain: Middle (4/6 taposta), BombsiteB (2/6 taposta)"
        in text
    )


def test_the_kill_sample_is_kills_not_rounds() -> None:
    """The denominator is kills, and the row says so itself.

    The row is read on its own, far from the reading guide. "4/6
    kierroksesta" would in a four-round block be an outright impossible
    sentence.
    """
    view = view_of(death_report(kills={"Middle": 4, "BombsiteB": 2}))
    line = next(
        line
        for line in view.maps[0].sides[0].round_types[0].lines
        if line.label == "tapot alueittain"
    )
    assert [c.unit for c in line.claims] == ["taposta", "taposta"]
    assert [c.sample_text for c in line.claims] == [
        "4/6 taposta",
        "2/6 taposta",
    ]


def test_every_other_claim_still_counts_rounds() -> None:
    """The unit is an exception and not a new default."""
    assert Claim(text="Cave", n=1, m=2).sample_text == "1/2 kierroksesta"


def test_rounds_without_an_own_death_are_said_out_loud() -> None:
    """A round on which the team lost nobody does not vanish in silence."""
    text = render(death_report(first={"Cave": 2}, median=20.0, rounds_missing=2))
    assert "ei omia kuolemia 2 kierroksella" in text


def test_a_first_death_line_without_a_median_is_still_labelled() -> None:
    """A missing timing must not take the area away."""
    view = view_of(death_report(first={"Cave": 1}))
    assert (
        view.maps[0].sides[0].round_types[0].lines[0].label
        == "ensimmäinen kuolema"
    )


def test_a_round_type_where_nobody_died_still_says_so() -> None:
    """"Ei omia kuolemia 4 kierroksella" is an **observation**, not
    emptiness.

    It says that the team lost nobody -- a different thing from nothing being
    known about the round type. The row is therefore a bare label and a note
    without a single claim, and that is precisely the rule: a row is written
    when it has a claim **or** an observation.
    """
    view = view_of(death_report())
    lines = view.maps[0].sides[0].round_types[0].lines

    assert [line.label for line in lines] == ["ensimmäinen kuolema"]
    assert lines[0].claims == ()
    assert lines[0].note == "ei omia kuolemia 4 kierroksella"


def test_a_round_type_without_rounds_gets_no_death_line_at_all() -> None:
    """The guard's other branch: a bare label with neither will not do.

    Without this, the previous test would read as if the row were always
    written.
    """
    entry = round_type("pistol", 0, death_report=deaths())
    view = view_of(report([map_report("de_ancient", [side("CT", [entry])])]))

    assert view.maps[0].sides[0].round_types[0].lines == ()


def test_the_kill_line_stands_on_its_own_without_any_deaths() -> None:
    """The kill row does not need a death row for company.

    A round type on which the team killed but lost nobody is ordinary -- and
    the kill row must not vanish because the first-death row has no claims.
    """
    view = view_of(death_report(kills={"Middle": 2}))
    labels = [line.label for line in view.maps[0].sides[0].round_types[0].lines]

    assert labels == ["ensimmäinen kuolema", "tapot alueittain"]


def test_an_unknown_first_death_area_is_named_and_explained() -> None:
    """An unknown position is a different thing from an empty area -- and it
    is explained.

    A bare ``UNKNOWN_AREA in text`` would not prove the explanation:
    ``_area(None)`` returns exactly that string **as the claim's text**, so
    the claim would pass even if the flag were left unraised. The reading
    guide's sentence is a different string, and only it tells the reader what
    the name means.
    """
    text = render(death_report(first={None: 1}, median=9.0))
    assert f"- ensimmäinen kuolema (mediaani 9,0 s): {UNKNOWN_AREA} " in text
    assert "pelin aluenimeä ei saatu" in text


def test_an_unknown_kill_area_is_named_and_explained() -> None:
    """The same on the kills side: a kill without an area neither drops out
    nor goes unexplained.

    A test of its own, because the flag is raised by a different row from the
    first-death case -- a shared test would leave one of them unexercised.
    """
    text = render(death_report(kills={None: 2}))
    assert f"- tapot alueittain: {UNKNOWN_AREA} (2/2 taposta)" in text
    assert "pelin aluenimeä ei saatu" in text


def test_the_unknown_area_note_stays_away_when_every_death_area_is_known() -> None:
    """An explanation without a case would be a reading guide to something
    that is not in the report.
    """
    text = render(death_report(first={"Cave": 1}, median=9.0, kills={"Middle": 1}))
    assert "pelin aluenimeä ei saatu" not in text


def test_the_kill_note_explains_the_denominator() -> None:
    """The reading guide says once where the kills' denominator comes
    from.
    """
    text = render(death_report(kills={"Middle": 2}))
    assert "laskee tappoja eikä kierroksia" in text
    assert "ampujan" in text


def test_the_kill_note_is_absent_when_no_kill_line_was_written() -> None:
    """An explanation without a row would be a reading guide to something
    that is not in the report.
    """
    text = render(death_report(first={"Cave": 1}, median=9.0))
    assert "laskee tappoja eikä kierroksia" not in text


def test_the_pattern_threshold_also_applies_to_deaths() -> None:
    """On full buys only the repeating patterns are told -- of the deaths
    too.
    """
    entry = round_type(
        "full",
        10,
        death_report=deaths(
            first={"Cave": 4, "Long": 1},
            rounds_missing=5,
            median=20.0,
            kills={"Middle": 5, "Pit": 1},
        ),
    )
    text = render(report([map_report("de_ancient", [side("CT", [entry])])]))
    assert "Cave (4/5 kierroksesta)" in text
    assert "Long" not in text
    assert "Middle (5/6 taposta)" in text
    assert "Pit" not in text
    assert "harvinaisempaa havaintoa jäi pois" in text


def test_saving_rounds_keep_every_death_observation() -> None:
    """On saving rounds every observation is written."""
    text = render(death_report(first={"Cave": 3, "Long": 1}, median=20.0))
    assert "Cave" in text and "Long" in text


def test_exceeding_the_death_line_limit_is_an_error_not_a_quiet_growth(
    monkeypatch,
) -> None:
    """The guard exists and it bites.

    Exceeding the limit cannot come about with the present code, so it is
    constructed by lowering the limit. Without this test the guard would be a
    claim nothing verifies -- and such a guard disappears at the next edit.
    """
    monkeypatch.setattr(view_module, "MAX_DEATH_LINES", 1)
    with pytest.raises(PappascoutError, match="There were 2 death rows"):
        view_of(death_report(first={"Cave": 1}, median=9.0, kills={"Middle": 1}))


# --- The anomaly chapter (Story 2.5) --------------------------------------------


def anomaly_text(text: str) -> str:
    """The anomaly chapter's content. A missing chapter is an error, not an
    empty string.
    """
    return section_text(text, ANOMALY_HEADING)


def anomaly_round(
    *,
    round_no: int = 18,
    demo: str = DEMO_ID,
    round_type: str = "eco",
    seconds: list[float] | None = None,
    players: int = 2,
    sources: list[str] | None = None,
    alive: int | None = None,
    points: list[AnomalyPoint] | None = None,
) -> AnomalyRound:
    """One round row under an anomaly.

    ``seconds``, ``players`` and ``alive`` are a shortcut to one pair of
    numbers at every sample point; ``points`` is given when the points have
    **different** numbers -- and that case is exactly why a round does not
    carry one maximum.

    ``alive`` is **only on a stack**, and ``None`` is its right value
    elsewhere: the two other rules do not count the living, and an invented
    denominator does not stand out on the row from a measured one.
    """
    return AnomalyRound(
        map_demo_id=demo,
        round_no=round_no,
        round_type=round_type,
        points=points
        if points is not None
        else [
            AnomalyPoint(sample_t_s=value, players=players, alive=alive)
            for value in (seconds if seconds is not None else [30.0])
        ],
        sources=sources or [],
    )


def anomaly(
    *,
    rule: str = "ct_advance",
    map_name: str = "de_ancient",
    map_name_source: str = "map_demo_id",
    side: str = "CT",
    area: str = "TSideLower",
    rounds: list[AnomalyRound] | None = None,
    orientation: list[tuple[str, float, int]] | None = None,
    site: str | None = None,
    m: int = 3,
    small_sample: bool = False,
) -> Anomaly:
    """One anomaly row. The defaults are the calibration's Ancient round 18.

    ``round_types``, ``n`` and ``players_max`` are derived from the rounds,
    because the model watches that they match them -- the fixture must not be
    able to build a row that disagrees with itself.
    """
    entries = rounds if rounds is not None else [anomaly_round()]
    types = {entry.round_type for entry in entries}
    return Anomaly(
        rule=rule,
        map_name=map_name,
        map_name_source=map_name_source,
        side=side,
        area=area,
        site=site,
        round_types=[name for name in ROUND_TYPES if name in types],
        rounds=entries,
        orientation=[
            AreaOrientation(map_demo_id=demo, t_share=share, observations=count)
            for demo, share, count in (
                orientation
                if orientation is not None
                else [(entry.map_demo_id, 0.88, 24) for entry in entries[:1]]
            )
        ],
        players_max=max(entry.players_max for entry in entries),
        n=len(entries),
        m=m,
        small_sample=small_sample,
    )


def crunch_anomaly(**overrides) -> Anomaly:
    """A crunch row: the source areas are mandatory on every round."""
    overrides.setdefault("rule", "crunch")
    overrides.setdefault(
        "rounds", [anomaly_round(sources=["Arch", "TopofMid"])]
    )
    return anomaly(**overrides)


def stack_anomaly(**overrides) -> Anomaly:
    """A stack row: the site group and the living are mandatory, the
    orientation forbidden.

    Three defaults in one place, because the model watches them together: the
    area is the site's own area, ``site`` says the same as a group, and every
    round says how many players were alive. The orientation is empty -- the
    rule does not read it.
    """
    overrides.setdefault("rule", "stack")
    overrides.setdefault("area", "BombsiteB")
    overrides.setdefault("site", "B")
    overrides.setdefault("orientation", [])
    overrides.setdefault(
        "rounds", [anomaly_round(round_no=13, seconds=[15.0], players=4, alive=5)]
    )
    return anomaly(**overrides)


def test_the_anomaly_chapter_exists_even_without_anomalies() -> None:
    """An empty anomaly chapter is an observation: the rules were run, they
    stayed silent.
    """
    text = render(report([pistol_map()]))
    assert f"## {ANOMALY_HEADING}" in text
    assert "Ei poikkeamia" in anomaly_text(text)


def test_the_empty_chapter_says_what_was_run_and_on_what() -> None:
    """**"No anomalies" is an observation only about what was examined.**

    Four things without which the empty chapter would claim a measured
    negative about a blind spot as well: how many rounds the rules saw, how
    many rounds **each** of them can hit on, whether some demo's orientation
    was left empty and whether the site groups went unobtained from some
    demo.
    """
    text = anomaly_text(render(report([pistol_map()])))
    assert "CT-eteneminen, Crunch ja Stack" in text
    assert "2 kierrokselle" in text
    # The stack's own coverage number: it is not the crunch's number, because
    # a silenced demo is in the crunch's denominator but not in the stack's.
    assert "stack 1 kierroksella" in text
    assert "sokeita pisteitä ei ole" in text
    # The stack's blind spot is **in the reading guide** and not here: it is
    # written also when there are anomalies, and from two places the empty
    # chapter would set the same sentence twice.
    assert "siteryhmiä" not in text


def test_a_deferred_rule_is_still_named_when_there_is_one() -> None:
    """The setting must not go untested because the list is empty.

    ``ANOMALY_RULES_DEFERRED`` emptied in Story 2.14, and with that the whole
    coverage sentence would without this test be unexercised code -- it would
    come back to life only with the next deferred rule, by which time nobody
    remembers it exists. The number is the coverage's denominator, so it has
    to be kept exercised.
    """
    text = anomaly_text(
        render(report([pistol_map()], scan_=scan(rules_deferred=["rotate"])))
    )
    assert "Arkkitehtuuri nimeää 4 poikkeamasääntöä" in text
    assert "1 on toteuttamatta (rotate)" in text


def test_the_empty_chapter_names_the_blind_spots() -> None:
    """An empty orientation is a blind spot and not a measured negative."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                scan_=scan(demos_without_orientation=[DEMO_ID]),
            )
        )
    )
    assert "1 demo ei antanut" in text
    assert "sokea piste eikä havainto" in text


def test_the_empty_chapter_mentions_unclassified_rounds() -> None:
    """Unclassified rounds are outside the rules."""
    text = anomaly_text(render(report([pistol_map()], unclassified=4)))
    assert "4 kierrosta jäi kokonaan tutkimatta" in text


def test_the_empty_chapter_exists_in_an_empty_report() -> None:
    """A report without maps gets the anomaly chapter too."""
    assert "Ei poikkeamia" in anomaly_text(render(report()))


def test_an_advance_line_carries_area_sample_and_orientation() -> None:
    """The summary row: what, where, how often and on what grounds."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "CT-eteneminen (`de_ancient`, CT-puoli, eco): TSideLower" in text
    assert "1/3 kierroksesta" in text
    assert "T-osuus 0,88 alueen 24 havainnosta" in text


def test_the_round_line_carries_the_round_number() -> None:
    """The scout's next act is to open that round in the demo."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "  - kierros 18: 2 pelaajaa 30 s kohdalla" in text


def test_a_crunch_line_names_its_source_areas_per_round() -> None:
    """The matrix's row 2: in a crunch the source areas as well -- within the
    round.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    crunch_anomaly(
                        area="Middle",
                        rounds=[
                            anomaly_round(
                                round_no=2,
                                seconds=[15.0],
                                players=5,
                                sources=["Arch", "TopofMid"],
                            )
                        ],
                        orientation=[(DEMO_ID, 0.83, 60)],
                    )
                ],
            )
        )
    )
    assert "Crunch (`de_ancient`, CT-puoli, havaittu: eco): Middle" in text
    assert (
        "  - kierros 2 (eco): 5 pelaajaa 15 s kohdalla, yhtä aikaa "
        "suunnista Arch ja TopofMid"
    ) in text


def test_two_crunch_rounds_never_merge_their_directions() -> None:
    """**Simultaneity does not cross the round boundary.**

    The union ("suunnista A, B, C ja D") would read as four simultaneous
    directions, which is the opposite of the definition. The round rows exist
    precisely to prevent this.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    crunch_anomaly(
                        m=4,
                        rounds=[
                            anomaly_round(
                                round_no=3, seconds=[15.0], players=2,
                                sources=["Alley", "BombsiteB"],
                            ),
                            anomaly_round(
                                round_no=10, round_type="full", seconds=[15.0],
                                players=3,
                                sources=["Arch", "LowerTunnel", "TopofMid"],
                            ),
                        ],
                        orientation=[(DEMO_ID, 0.81, 54)],
                    )
                ],
            )
        )
    )
    assert "2/4 kierroksesta" in text
    assert "kierros 3 (eco): 2 pelaajaa 15 s kohdalla, yhtä aikaa suunnista Alley ja BombsiteB" in text
    assert "kierros 10 (default): 3 pelaajaa 15 s kohdalla, yhtä aikaa suunnista Arch, LowerTunnel ja TopofMid" in text
    # The union of four directions is nowhere.
    assert "Alley, BombsiteB, Arch" not in text


def test_a_stack_line_names_the_site_its_group_and_the_survivors() -> None:
    """A stack row says what the rule measured -- and nothing else.

    On the summary row the area is **the site's own area** and the extra is
    the group, not the T share: the rule does not read the orientation, so a
    share would concern another question. On the round row the player count
    is a fraction, because four out of five and four out of four are
    different observations.
    """
    text = anomaly_text(
        render(report([pistol_map()], anomalies=[stack_anomaly(m=9)]))
    )
    assert "Stack (`de_ancient`, CT-puoli, havaittu: eco): BombsiteB" in text
    assert "1/9 kierroksesta" in text
    assert "B-siten ryhmässä" in text
    assert "  - kierros 13 (eco): 4/5 pelaajaa 15 s kohdalla" in text
    # There is no T share, because it was not measured.
    assert "T-osuus" not in text


def test_a_stack_line_never_claims_directions() -> None:
    """The directions are the crunch's observation; a stack does not count
    them.
    """
    text = anomaly_text(
        render(report([pistol_map()], anomalies=[stack_anomaly()]))
    )
    assert "suunnista" not in text


def test_all_five_alive_reads_as_five_of_five() -> None:
    """The calibration's two extreme hits: 5/5 is a different observation
    from 4/5.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    stack_anomaly(
                        area="BombsiteA",
                        site="A",
                        m=12,
                        rounds=[
                            anomaly_round(
                                round_no=2, seconds=[30.0], players=5, alive=5
                            ),
                            anomaly_round(
                                round_no=7, seconds=[6.0], players=4, alive=5
                            ),
                        ],
                    )
                ],
            )
        )
    )
    assert "  - kierros 2 (eco): 5/5 pelaajaa 30 s kohdalla" in text
    assert "  - kierros 7 (eco): 4/5 pelaajaa 6 s kohdalla" in text


def test_the_stack_rows_sit_in_the_same_chapter_as_the_other_rules() -> None:
    """Three rules, one chapter -- and the order comes from
    ``ANOMALY_RULES``.

    The anomaly chapter is the epic's most valuable output, and stack must
    not be left as a block of its own: the reader compares the rows with each
    other.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[stack_anomaly(), crunch_anomaly(), anomaly()],
            )
        )
    )
    order = [
        text.index("CT-eteneminen ("),
        text.index("Crunch ("),
        text.index("Stack ("),
    ]
    assert order == sorted(order)


def test_the_stack_legend_names_the_silenced_demos() -> None:
    """The silence has to be recorded also when there ARE anomalies.

    The empty chapter's text is then not set at all, so the coverage would go
    untold in precisely the report in which the reader sees rows from the
    other maps but not from the silenced one.

    **The reason clause is asserted and not only the tail.** Until Story 4.3
    this paragraph told the reader that on a map where A and B are stacked on
    different floors the distance to the site cannot say which one it is, and
    named Nuke as the case. That change is the proof it can -- on the other
    axis -- so the sentence is now about failing on *both* axes. Nothing
    pinned the old wording: the tail sentence was asserted and the clause
    carrying the claim was not, so it could have been replaced by anything
    and stayed green. That is the guard that stops guarding in silence, and
    this assertion is what closes it.
    """
    text = render(
        report(
            [pistol_map()],
            anomalies=[stack_anomaly()],
            scan_=scan(
                rounds_scanned=9,
                crunch_rounds=9,
                stack_rounds=4,
                demos_without_site_groups=["Nuke_vs_imuaijat"],
            ),
        )
    )
    assert "Stackin kattavuus on 4/9 CT-kierroksesta" in text
    assert "1 demo ilman siteryhmiä" in text
    assert "kummallakaan akselilla" in text
    assert "ei vaakatasossa eikä korkeudella" in text
    assert "muttei havainto siitä, ettei stackeja ollut" in text
    # The superseded reason must not come back: it was a claim about one
    # axis written as a claim about the map.
    assert "mikä tahansa" not in text
    assert "Alley, Arch" not in text


def test_a_crunch_label_says_the_types_are_observations_not_a_limit() -> None:
    """Crunch is not restricted to a round type, and the label says so."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    crunch_anomaly(
                        m=4,
                        rounds=[
                            anomaly_round(round_no=1, sources=["A", "B"]),
                            anomaly_round(
                                round_no=2, round_type="full", sources=["A", "B"]
                            ),
                        ],
                    )
                ],
            )
        )
    )
    assert "havaittu: eco, default" in text


def test_an_advance_line_never_claims_source_areas() -> None:
    """For an advance an empty list means "not asked" and not "no
    directions".
    """
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "suunnista" not in text


def test_the_advance_round_line_omits_the_round_type() -> None:
    """It is in the label already; the same word is not written twice on the
    row.
    """
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "kierros 18: " in text
    assert "kierros 18 (eco)" not in text


def test_several_sample_points_are_listed_as_a_finnish_list() -> None:
    """The row is read as a sentence, so the last separator is "ja"."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(rounds=[anomaly_round(seconds=[15.0, 30.0])])],
            )
        )
    )
    assert "15 ja 30 s kohdalla" in text


def test_two_demos_give_the_same_area_two_shares() -> None:
    """An average would be a number that has not been observed."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    anomaly(
                        rounds=[
                            anomaly_round(round_no=1, demo="demo-a"),
                            anomaly_round(round_no=2, demo="demo-b"),
                        ],
                        orientation=[("demo-a", 0.88, 24), ("demo-b", 0.84, 37)],
                    )
                ],
            )
        )
    )
    assert (
        "T-osuus 0,88 alueen 24 havainnosta; "
        "T-osuus 0,84 alueen 37 havainnosta"
    ) in text


def test_two_demos_make_the_round_line_name_its_demo() -> None:
    """The round number does not identify when the map has two demos."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    anomaly(
                        rounds=[
                            anomaly_round(round_no=1, demo="demo-a"),
                            anomaly_round(round_no=2, demo="demo-b"),
                        ],
                        orientation=[("demo-a", 0.88, 24), ("demo-b", 0.84, 37)],
                    )
                ],
            )
        )
    )
    assert "kierros 1: 2 pelaajaa 30 s kohdalla -- `demo-a`" in text


def test_one_demo_leaves_the_identifier_out_of_the_round_line() -> None:
    """The guard's other branch: with one demo the id does not belong in the
    body.
    """
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert DEMO_ID not in text


def test_each_sample_point_carries_its_own_player_count() -> None:
    """**The maximum must not come back to the row.**

    Measured, MatureMayhem Anubis round 4: at 15 s five players out of five,
    at 30 s four. As one maximum the row read "5/5 pelaajaa 15 ja 30 s
    kohdalla" -- a claim about data that does not exist.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    stack_anomaly(
                        m=12,
                        rounds=[
                            anomaly_round(
                                round_no=4,
                                points=[
                                    AnomalyPoint(
                                        sample_t_s=15.0, players=5, alive=5
                                    ),
                                    AnomalyPoint(
                                        sample_t_s=30.0, players=4, alive=5
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        )
    )
    assert (
        "  - kierros 4 (eco): 5/5 pelaajaa 15 s ja 4/5 pelaajaa 30 s kohdalla"
    ) in text
    assert "5/5 pelaajaa 15 ja 30 s kohdalla" not in text


def test_an_advance_over_two_sample_points_keeps_both_counts() -> None:
    """The same fault concerned all three rules, not only the stack.

    Measured, MatureMayhem Inferno round 2: at 15 s five players in Middle,
    at 30 s **one**. The row read "5 pelaajaa 15 ja 30 s kohdalla".
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    anomaly(
                        area="Middle",
                        rounds=[
                            anomaly_round(
                                round_no=2,
                                points=[
                                    AnomalyPoint(sample_t_s=15.0, players=5),
                                    AnomalyPoint(sample_t_s=30.0, players=1),
                                ],
                            )
                        ],
                    )
                ],
            )
        )
    )
    assert "  - kierros 2: 5 pelaajaa 15 s ja 1 pelaaja 30 s kohdalla" in text


def test_equal_counts_still_collapse_into_one_phrase() -> None:
    """The collapsing stays when the numbers really are the same.

    The condition compares **all** the numbers, so the collapsing can no
    longer hide a difference; without it every row would list the same number
    twice.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    stack_anomaly(
                        m=12,
                        rounds=[
                            anomaly_round(
                                round_no=4,
                                seconds=[15.0, 30.0],
                                players=4,
                                alive=5,
                            )
                        ],
                    )
                ],
            )
        )
    )
    assert "  - kierros 4 (eco): 4/5 pelaajaa 15 ja 30 s kohdalla" in text


def test_a_stack_over_two_demos_also_names_them() -> None:
    """The same guard on a stack, although it has no orientation.

    The count is read **from the rounds** and not from the orientation for
    exactly this reason: read from the orientation, a stack's id would always
    be left out, and Ancient's two demos are in the same map chapter (Story
    2.11).
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    stack_anomaly(
                        m=18,
                        rounds=[
                            anomaly_round(
                                round_no=13,
                                demo="demo-a",
                                seconds=[15.0],
                                players=4,
                                alive=5,
                            ),
                            anomaly_round(
                                round_no=16,
                                demo="demo-b",
                                seconds=[30.0],
                                players=4,
                                alive=5,
                            ),
                        ],
                    )
                ],
            )
        )
    )
    assert "kierros 13 (eco): 4/5 pelaajaa 15 s kohdalla -- `demo-a`" in text
    assert "kierros 16 (eco): 4/5 pelaajaa 30 s kohdalla -- `demo-b`" in text


def test_a_small_sample_anomaly_is_marked_not_hidden() -> None:
    """One round is a valid sample and is marked as small."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(m=1, small_sample=True)],
            )
        )
    )
    assert "1/1 kierroksesta" in text
    assert "pieni otanta" in text


def test_the_small_sample_mark_is_not_confused_with_the_label() -> None:
    """``--`` means only one thing on the row: a note.

    The label used to have the same separator, so the same mark meant two
    different things on the same row. The label's parts are in parentheses
    now.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(m=1, small_sample=True)],
            )
        )
    )
    row = next(r for r in text.splitlines() if r.startswith("- "))
    assert row.count(" -- ") == 1
    assert row.endswith("pieni otanta")


def test_an_unrecognised_map_never_puts_its_identifier_in_the_body() -> None:
    """The body speaks in names; an unrecognised map is named in place.

    The label is **the same string** as on the traceability chapter's map
    row, so the reader can connect the row to the right map chapter.
    """
    entry = map_report(FACEIT_DEMO_ID, [side("CT", [round_type("eco", 1)])],
                       demo_ids=[FACEIT_DEMO_ID], source="unknown")
    text = render(
        report(
            [entry],
            anomalies=[
                anomaly(
                    map_name=FACEIT_DEMO_ID,
                    map_name_source="unknown",
                    rounds=[anomaly_round(demo=FACEIT_DEMO_ID)],
                    m=1,
                )
            ],
        )
    )
    chapter = anomaly_text(text)
    assert FACEIT_DEMO_ID not in chapter
    assert "kartta 1, nimeä ei tunnistettu" in chapter
    assert "kartta 1, nimeä ei tunnistettu" in traceability_text(text)


def test_the_anomaly_chapter_comes_before_the_map_chapters() -> None:
    """The anomalies are the epic's most valuable output and do not belong at
    the end.
    """
    text = render(report([pistol_map()], anomalies=[anomaly()]))
    headings = [heading for heading, _ in report_sections(text) if heading]
    assert headings.index(ANOMALY_HEADING) < headings.index(
        "`de_ancient` -- 2 kierrosta, 1 demo"
    )
    assert headings.index("Yhteenveto") < headings.index(ANOMALY_HEADING)


def test_the_anomaly_chapter_follows_the_missing_demos_chapter() -> None:
    """The order is summary, missing demos, anomalies, maps."""
    text = render(
        report(
            [pistol_map()],
            anomalies=[anomaly()],
            missing_demos=[MissingDemo(match=MISSING_DEMO_ID, reason="ei demoa")],
        )
    )
    headings = [heading for heading, _ in report_sections(text) if heading]
    assert headings.index("Puuttuvat demot") < headings.index(ANOMALY_HEADING)


def test_the_anomaly_lines_are_bullets_not_paragraphs() -> None:
    text = anomaly_text(
        render(report([pistol_map()], anomalies=[anomaly(), crunch_anomaly()]))
    )
    rows = [row for row in text.splitlines() if row.strip()]
    assert len(rows) == 4  # two summary rows and two round rows
    assert all(row.lstrip().startswith("- ") for row in rows), rows


def test_the_most_repeated_anomaly_comes_first() -> None:
    """The chapter raises what repeats."""
    once = anomaly(area="Ramp", m=4)
    twice = anomaly(
        area="TSideLower",
        m=4,
        rounds=[anomaly_round(round_no=1), anomaly_round(round_no=2)],
    )
    text = anomaly_text(render(report([pistol_map()], anomalies=[once, twice])))
    assert text.index("TSideLower") < text.index("Ramp")


def test_the_chapter_has_a_line_cap_and_says_what_it_dropped() -> None:
    """The chapter is the report's first content chapter and must not grow
    without bound.

    The cap on death rows is structural, so exceeding it is an error. The
    number of anomalies is a property of the data, so an error would abort
    the run over data that cannot be chosen -- the bound is applied and the
    number left out is written out, as with the pattern threshold.
    """
    many = [
        anomaly(area=f"Alue{i:02d}", m=MAX_ANOMALY_LINES + 5)
        for i in range(MAX_ANOMALY_LINES + 3)
    ]
    text = anomaly_text(render(report([pistol_map()], anomalies=many)))
    rows = [r for r in text.splitlines() if r.startswith("- ")]
    # 20 summary rows + one note row.
    assert len(rows) == MAX_ANOMALY_LINES + 1
    assert "3 poikkeamaa jäi pois" in text
    assert "report.jsonissa" in text


def test_the_cap_note_is_absent_when_nothing_was_dropped() -> None:
    """The guard's other branch: a silent bound is not the only danger."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "jäi pois" not in text


def test_the_chapter_heading_is_the_same_in_the_code_and_the_template() -> None:
    """The same guard as with the traceability chapter: two copies would
    drift.
    """
    assert f"## {ANOMALY_HEADING}" in template_text()


def test_the_legend_explains_what_the_t_share_means() -> None:
    """The reading guide says that the orientation is the demo's own
    observation.
    """
    legend = section_text(render(report([pistol_map()])), "Lukuohje")
    assert "demon oma havainto" in legend
    assert "molempien joukkueiden" in legend


def test_the_legend_names_the_thresholds_from_the_report() -> None:
    """The thresholds are read from the report and not invented in the
    rendering.

    An adjusted ``settings.toml`` shows in the report's text only if the text
    comes from the report -- the same rule as with the pattern threshold.
    """
    legend = section_text(render(report([pistol_map()])), "Lukuohje")
    assert "vähintään 0,80" in legend
    assert "vähintään 20 havaintoa" in legend
    assert "enintään 30 sekunnin kohdalla" in legend


def test_the_legend_follows_a_changed_threshold() -> None:
    """A guard that the number is not hardcoded."""
    legend = section_text(
        render(
            report(
                [pistol_map()],
                thresholds_used={
                    "thresholds": {
                        "small_sample_rounds": SMALL_SAMPLE,
                        "team_identity_min_common": MIN_COMMON,
                        "advance_t_share": 0.9,
                        "advance_area_min_observations": 40,
                        "advance_max_sample_s": 15.0,
                        "advance_min_players": 2,
                        "crunch_min_players": 3,
                        "crunch_min_sources": 3,
                    }
                },
            )
        ),
        "Lukuohje",
    )
    assert "vähintään 0,90" in legend
    assert "vähintään 40 havaintoa" in legend
    assert "enintään 15 sekunnin kohdalla" in legend
    assert "Vähintään 2 pelaajaa alueella" in legend
    assert "Vähintään 3 pelaajaa ja 3 eri suuntaa" in legend


def test_the_legend_defines_both_rules() -> None:
    """Without the definitions the rules' asymmetry is invisible.

    The reader cannot otherwise know why an area appears on an ``eco`` row
    but not on a ``default`` row.
    """
    legend = section_text(render(report([pistol_map()])), "Lukuohje")
    assert "**CT-eteneminen**" in legend
    assert "**Crunch**" in legend
    assert "säästökierroksella" in legend
    assert "ei ole rajattu kierrostyyppiin" in legend


def test_the_legend_is_written_even_for_an_empty_chapter() -> None:
    """The reader of a clean report needs the method more than anyone.

    The rationale differs from the reading guide's other paragraphs: these do
    not explain a row that is in the report but what was measured.
    """
    legend = section_text(render(report()), "Lukuohje")
    assert "demon oma havainto" in legend
    assert "**Crunch**" in legend


def test_the_areas_stay_in_english_in_the_anomaly_chapter() -> None:
    """Callouts in English, the text in Finnish -- the same rule as
    elsewhere.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    crunch_anomaly(
                        rounds=[
                            anomaly_round(
                                sources=["SideEntrance", "TSideUpper"]
                            )
                        ]
                    )
                ],
            )
        )
    )
    assert "SideEntrance" in text
    assert "TSideUpper" in text


def test_the_anomaly_chapter_carries_no_interpretation() -> None:
    """No interpretation and no counter-strategy -- only the observation."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    for word in ("fake", "rush", "kannattaa", "suositus", "vastaus"):
        assert word not in text.lower()


def test_a_single_player_is_not_written_in_the_plural() -> None:
    """Four of the six calibrated hits are one player's observation."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(rounds=[anomaly_round(players=1)])],
            )
        )
    )
    assert "kierros 18: 1 pelaaja 30 s kohdalla" in text


def test_every_anomaly_rule_has_a_finnish_name_in_the_view() -> None:
    """The view indexes the map directly, so a missing name breaks it."""
    for rule in ANOMALY_RULES:
        assert ANOMALY_RULE_FI[rule]


# --- Pruning (Story 2.13) -------------------------------------------------------

#: Utility's targets in :func:`pruning_map`'s eco block: nine targets on one
#: row. Measured, ten rows had between five and nine targets, and such a row
#: is a list and not a pattern.
#:
#: **Five of the nine repeat at least three times** (2026-09-11). ``eco`` is
#: a filtered round type now, so the pattern threshold reaches this row
#: before rule 4 does: with only two targets above the threshold the row
#: would already be at the limit and rule 4 would prune nothing, and the
#: tests that watch rule 4 would pass without the rule existing.
PRUNE_UTILITY = {
    "BombsiteA": 4,
    "Palace": 4,
    "Connector": 3,
    "Ramp": 3,
    "Apartments": 3,
    "Catwalk": 1,
    "Jungle": 1,
    "Underpass": 1,
    "Window": 1,
}

#: The kill areas in the same block: nine areas on one row. Five of them
#: repeat at least three times, for the same reason as in
#: :data:`PRUNE_UTILITY` -- rule 5's limit is three, so fewer than four
#: surviving areas would leave the rule nothing to do.
PRUNE_KILLS = {
    "BombsiteA": 5,
    "Palace": 4,
    "Connector": 4,
    "Ramp": 3,
    "Apartments": 3,
    "Catwalk": 1,
    "Jungle": 1,
    "Underpass": 1,
    "Window": 1,
}

#: Only the named sample point off (rule 3). The default is to keep it, so
#: this is the only rule whose switching **on** is a separate choice.
SKIP_45 = ReportSettings(skip_sample_seconds=[45.0])


def pruning_map() -> MapReport:
    """A map every block of which touches a different pruning rule.

    One fixture and not five, because the rules affect each other: rule 1
    drops a row rule 2 would otherwise merge, pattern filtering can drop a
    row before pruning gets there, and the unpruned block is the point of
    comparison for all of them. Five separate fixtures would never show the
    order.

    * **eco** -- a saturated armour row (rule 1), an armed row that is
      **not** saturated, four time sample points (rule 3), nine utility
      targets (rule 4) and nine kill areas (rule 5).
    * **force** -- identical equipment rows (rule 2).
    * **half-buy** -- differing equipment rows (1 armed, 3 armoured): the I/O
      matrix's row on which both rows stay.
    * **default** -- a block in which **pattern filtering bites**: the
      armour row and the 45 s sample point fall below the threshold, so
      neither is ever built and rule 3 has nothing to skip. It was precisely
      that interaction that broke the story's core promise on review round
      1.
    * **pistol** -- saturated and identical equipment rows, four targets,
      five kill areas and a 45 s sample point. None of them is pruned.

    **Every block but the pistol one is pattern-filtered since 2026-09-11**,
    and the fixture had to be re-measured for it. Two things changed, and
    both are the same requirement -- *a rule can only be watched where it
    has something left to do*:

    1. **What each row observes now repeats often enough to survive the
       threshold.** Before, the eco, force and half-buy blocks were written
       round by round, so a bar of ``1`` reached the pruning rules; now it
       does not reach them, and a fixture left as it was would have tested
       the rules by deleting their input.
    2. **The default block has four rounds and not two.** The threshold is
       capped at the block's round count, so a two-round block filters at 2
       -- and at 2 the saturated row (``5`` on two rounds out of two) passes
       the threshold and rule 1 drops it, which is the exact opposite of
       what this block is here to show. Four rounds keeps the block at the
       report's own threshold of 3, where the row falls below it. The cost
       is that the block is no longer a small sample; the small-sample mark
       is watched elsewhere.
    """
    return map_report(
        "de_mirage",
        [
            side(
                "T",
                [
                    round_type(
                        "eco",
                        4,
                        positions=[
                            position(6.0, [area("Middle", 4, {2: 4})], 4),
                            position(15.0, [area("Middle", 4, {2: 4})], 4),
                            position(30.0, [area("Middle", 4, {1: 4})], 4),
                            position(45.0, [area("Middle", 4, {1: 3, 0: 1})], 4),
                        ],
                        utility=[
                            use("smoke", "TSpawn", target, n=n, m=4)
                            for target, n in PRUNE_UTILITY.items()
                        ],
                        players_armed=armed(4, {0: 4}),
                        players_armored=armored(4, {5: 4}),
                        death_report=deaths(
                            first={"Palace": 3},
                            rounds_missing=1,
                            kills=PRUNE_KILLS,
                        ),
                    ),
                    round_type(
                        "force",
                        3,
                        players_armed=armed(3, {3: 3}),
                        players_armored=armored(3, {3: 3}),
                    ),
                    round_type(
                        "half",
                        2,
                        players_armed=armed(2, {1: 2}),
                        players_armored=armored(2, {3: 2}),
                    ),
                    round_type(
                        "full",
                        SMALL_SAMPLE + 1,
                        positions=[
                            position(
                                6.0,
                                [area("Middle", SMALL_SAMPLE + 1, {2: 4})],
                                SMALL_SAMPLE + 1,
                            ),
                            position(
                                45.0,
                                [area("Middle", SMALL_SAMPLE + 1, {1: 2, 0: 2})],
                                SMALL_SAMPLE + 1,
                            ),
                        ],
                        players_armored=armored(SMALL_SAMPLE + 1, {5: 2, 4: 2}),
                        death_report=deaths(
                            first={"Palace": 1, "Ramp": 1},
                            rounds_missing=2,
                            median=20.0,
                        ),
                    ),
                ],
            ),
            side(
                "CT",
                [
                    round_type(
                        "pistol",
                        2,
                        positions=[
                            position(30.0, [area("Middle", 2, {2: 2})], 2),
                            position(45.0, [area("Middle", 2, {1: 2})], 2),
                        ],
                        utility=[
                            use("flashbang", "CTSpawn", target, n=n, m=2)
                            for target, n in {
                                "BombsiteA": 2,
                                "Connector": 1,
                                "Jungle": 1,
                                "Palace": 1,
                            }.items()
                        ],
                        players_armed=armed(2, {5: 2}),
                        players_armored=armored(2, {5: 2}),
                        death_report=deaths(
                            first={"Connector": 2},
                            kills={
                                "BombsiteA": 3,
                                "Palace": 2,
                                "Connector": 1,
                                "Jungle": 1,
                                "Window": 1,
                            },
                        ),
                    )
                ],
            ),
        ],
        demo_ids=["Mirage_vs_karsinta"],
    )


def pruning_report() -> Report:
    """A report that holds :func:`pruning_map` and nothing else."""
    return report([pruning_map()])


def content_rows(text: str) -> list[str]:
    """The map chapters' rows -- the number the epic's metric concerns.

    The same bound as :func:`observation_rows`'s, but the italic notes are
    included: pruning's price is a row even when that row is a note, and
    without them the measurement would make pruning look more effective than
    it is.
    """
    body = text.split(MAP_HEADING_START)[1].split("## Kierrosliite")[0]
    return [row for row in body.splitlines() if row.startswith("- ")]


def map_chapter(text: str) -> str:
    """The map chapter whole -- the part pruning can touch.

    The golden locks this and not the whole document: the summary, the
    anomaly chapter, the round appendix, the reading guide's standing
    paragraphs and the traceability chapter are already locked in
    :data:`GOLDEN`, and as two copies any other report change would break two
    goldens over one thing.
    """
    return MAP_HEADING_START + text.split(MAP_HEADING_START)[1].split(
        "## Kierrosliite"
    )[0].rstrip(
        "\n"
    )


def block(text: str, heading: str) -> str:
    """One round-type block's rows.

    A block and not the whole report, because every pruning rule is
    per round type: the claim "the row vanished" has to be made about the
    block it was supposed to vanish from, or the same claim would pass even
    when the row vanished from the wrong block.
    """
    part = text.split(f"**{heading}**", 1)[1]
    rows: list[str] = []
    for row in part.splitlines()[1:]:
        if not row.strip():
            if rows:
                break
            continue
        rows.append(row)
    return "\n".join(rows)


def threshold_note(text: str, heading: str = "Default") -> str:
    """The block's pattern-filtering note -- **a claim about the data**, not
    presentation.

    A helper of its own, because it is this row that is item A: pruning must
    not change it, and making the claim about the whole block would pass even
    when the note changed and some other row vanished at the same time.
    """
    rows = [
        row
        for row in block(text, heading).splitlines()
        if "kuviot, jotka toistuvat" in row or "kynnystä ei ollut" in row
    ]
    return rows[0] if rows else ""


# --- Every rule off: the report is character for character the same ------------

#: The golden of :func:`pruning_map`'s map chapter with **pruning entirely
#: off**.
#:
#: The claim is a regression guard: pruning does not change the map chapter
#: in any way when every rule is switched off. The fixture triggers all five
#: rules, so the test also fails if some rule prunes while its setting is
#: off -- and that is the fault it is primarily looking for.
#:
#: **The provenance was reproducible against the code before Story 2.13**
#: (baseline ``e9a8c88``), by copying this file's ``pruning_map()`` into a
#: worktree at that commit and rendering it there without the settings
#: parameter. **That reproduction no longer holds, and it must not be
#: attempted** (2026-09-11): the pattern threshold now reaches ``eco``,
#: ``force`` and ``half`` and is capped at the block's round count, so the
#: unpruned chapter at ``e9a8c88`` is a different text on purpose. The
#: fixture's numbers were raised in the same change, which would make the
#: comparison meaningless even if the filter had not moved.
#:
#: The claim the repository can still check by itself on every run is the
#: one that matters here: **this text does not change** when pruning is off.
#: Story 2.13's own evidence -- the empty ``diff`` against the archive's
#: real reports -- stands as a record of that story and is in the spec's
#: Manual checks section; it is not re-runnable against today's renderer,
#: and the measurement that replaces it is the re-rendered report recorded
#: with the 2026-09-11 change.
GOLDEN_PRUNING_OFF_CHAPTER = """\
## `de_mirage` -- 15 kierrosta, 1 demo

### T-puoli -- 13 kierrosta

**Eco** (4 kierrosta) -- vain toistuvat kuviot
- 6 s: Middle 2 (4/4 kierroksesta)
- 15 s: Middle 2 (4/4 kierroksesta)
- 30 s: Middle 1 (4/4 kierroksesta)
- 45 s: Middle 1 (3/4 kierroksesta)
- savu: TSpawn -> BombsiteA (arvio) 0-5 s (4/4 kierroksesta), TSpawn -> Palace (arvio) 0-5 s (4/4 kierroksesta), TSpawn -> Apartments (arvio) 0-5 s (3/4 kierroksesta), TSpawn -> Connector (arvio) 0-5 s (3/4 kierroksesta), TSpawn -> Ramp (arvio) 0-5 s (3/4 kierroksesta)
- aseistettuja ostoajan lopussa: 0 (4/4 kierroksesta)
- panssaroituja ostoajan lopussa: 5 (4/4 kierroksesta)
- ensimmäinen kuolema: Palace (3/3 kierroksesta) -- ei omia kuolemia 1 kierroksella
- tapot alueittain: BombsiteA (5/23 taposta), Connector (4/23 taposta), Palace (4/23 taposta), Apartments (3/23 taposta), Ramp (3/23 taposta)
- *Vain kuviot, jotka toistuvat vähintään 3 kierroksella; 8 harvinaisempaa havaintoa jäi pois.*

**Force** (3 kierrosta) -- vain toistuvat kuviot
- aseistettuja ostoajan lopussa: 3 (3/3 kierroksesta)
- panssaroituja ostoajan lopussa: 3 (3/3 kierroksesta)
- ensimmäinen kuolema: ei omia kuolemia 3 kierroksella
- *Vain kuviot, jotka toistuvat kaikilla 3 kierroksella; jokainen havainto ylitti kynnyksen.*

**Puoliosto** (2 kierrosta) -- pieni otanta -- vain toistuvat kuviot
- aseistettuja ostoajan lopussa: 1 (2/2 kierroksesta)
- panssaroituja ostoajan lopussa: 3 (2/2 kierroksesta)
- ensimmäinen kuolema: ei omia kuolemia 2 kierroksella
- *Vain kuviot, jotka toistuvat kaikilla 2 kierroksella; jokainen havainto ylitti kynnyksen.*

**Default** (4 kierrosta) -- vain toistuvat kuviot
- 6 s: Middle 2 (4/4 kierroksesta)
- ensimmäinen kuolema (mediaani 20,0 s, 2/4 kierroksesta): ei omia kuolemia 2 kierroksella
- *Vain kuviot, jotka toistuvat vähintään 3 kierroksella; 5 harvinaisempaa havaintoa jäi pois.*

### CT-puoli -- 2 kierrosta

**Pistooli** (2 kierrosta) -- pieni otanta
- 30 s: Middle 2 (2/2 kierroksesta)
- 45 s: Middle 1 (2/2 kierroksesta)
- valo: CTSpawn -> BombsiteA (arvio) 0-5 s (2/2 kierroksesta), CTSpawn -> Connector (arvio) 0-5 s (1/2 kierroksesta), CTSpawn -> Jungle (arvio) 0-5 s (1/2 kierroksesta), CTSpawn -> Palace (arvio) 0-5 s (1/2 kierroksesta)
- aseistettuja ostoajan lopussa: 5 (2/2 kierroksesta)
- panssaroituja ostoajan lopussa: 5 (2/2 kierroksesta)
- ensimmäinen kuolema: Connector (2/2 kierroksesta)
- tapot alueittain: BombsiteA (3/8 taposta), Palace (2/8 taposta), Connector (1/8 taposta), Jungle (1/8 taposta), Window (1/8 taposta)"""


def test_with_every_rule_off_the_map_chapter_is_what_it_was() -> None:
    """The story's most important test: pruning changes nothing else.

    Substring claims are not enough for this: they do not see the rows'
    order, the claims' order within a row, nor the blocks' notes, and those
    are exactly what "character for character the same" means.
    """
    text = render(pruning_report(), NO_PRUNING)
    assert map_chapter(text) == GOLDEN_PRUNING_OFF_CHAPTER
    # And the rest of the document carries no trace of pruning either: the
    # reading guide has no paragraph about a rule that is off, and the
    # summary has no row about rules that are not there. The map chapter is
    # locked into the golden, and these two cover what pruning could change
    # elsewhere -- the other chapters are locked in :data:`GOLDEN`.
    assert "[report]." not in section_text(text, "Lukuohje")
    assert "Karsinnan säännöt" not in summary_text(text)


def test_the_summary_row_appears_only_when_a_rule_is_on() -> None:
    """Every rule off -> no row: the report is as it was.

    The row says with which rules the report was written. When none is on,
    there is nothing to report -- and writing the row would break the story's
    most important promise, which is a report character for character the
    same as before Story 2.13. One rule is enough to bring the row, and the
    row then lists **all** the values, the switched-off ones included.
    """
    assert "Karsinnan säännöt" not in summary_text(
        render(pruning_report(), NO_PRUNING)
    )
    one_rule = ReportSettings(
        drop_saturated_equipment_lines=False,
        merge_equal_equipment_lines=False,
        max_utility_targets=0,
        max_kill_areas=1,
    )
    summary = summary_text(render(pruning_report(), one_rule))
    assert "Karsinnan säännöt" in summary
    assert "max_kill_areas 1" in summary
    assert "drop_saturated_equipment_lines ei" in summary


def test_pruning_never_touches_the_report_model() -> None:
    """``report.json`` is the source of traceability: pruning does not write
    into it.

    The model is serialised before and after the rendering and compared as
    bytes. A pruned value is therefore still on the machine, and switching
    the setting back needs only a new rendering -- not a re-aggregation.
    """
    entry = pruning_report()
    before = entry.model_dump_json()
    render(entry)
    render(entry, NO_PRUNING)
    assert entry.model_dump_json() == before


def test_every_rule_is_its_own_setting() -> None:
    """The product owner can switch any rule off without a code change.

    One at a time: switching each rule off brings back its own row, and one
    setting does not drive two rules.
    """
    entry = pruning_report()
    with_defaults = render(entry)

    without_saturated = render(
        entry, ReportSettings(drop_saturated_equipment_lines=False)
    )
    assert "panssaroituja ostoajan lopussa: 5 (4/4" in block(
        without_saturated, "Eco"
    )
    assert "- panssaroituja ostoajan lopussa" not in block(with_defaults, "Eco")

    without_merge = render(entry, ReportSettings(merge_equal_equipment_lines=False))
    assert MERGED_EQUIPMENT_LABEL not in without_merge
    assert MERGED_EQUIPMENT_LABEL in with_defaults

    assert "45 s:" in block(with_defaults, "Eco")
    assert "45 s:" not in block(render(entry, SKIP_45), "Eco")

    # The row each limit brings back has to be one the **limit** removed:
    # the pattern threshold reaches this block too now, and a target seen on
    # one round out of four is gone before the limit is consulted -- such a
    # row would show the rule working while the rule did nothing.
    without_target_limit = render(entry, ReportSettings(max_utility_targets=0))
    assert "Apartments" in block(without_target_limit, "Eco")
    assert "Apartments" not in block(with_defaults, "Eco")

    without_kill_limit = render(entry, ReportSettings(max_kill_areas=0))
    assert "Apartments (3/23 taposta)" in block(without_kill_limit, "Eco")
    assert "Apartments (3/23 taposta)" not in block(with_defaults, "Eco")


# --- Item A: pruning does not change a claim about the data ------------------


PRUNING_VARIANTS = {
    "all defaults": DEFAULT_PRUNING,
    "rule 3 on": SKIP_45,
    "saturated only": ReportSettings(
        merge_equal_equipment_lines=False,
        max_utility_targets=0,
        max_kill_areas=0,
    ),
    "sample point only": ReportSettings(
        drop_saturated_equipment_lines=False,
        merge_equal_equipment_lines=False,
        skip_sample_seconds=[45.0],
        max_utility_targets=0,
        max_kill_areas=0,
    ),
}


@pytest.mark.parametrize("name", sorted(PRUNING_VARIANTS))
def test_the_pattern_threshold_note_is_the_same_with_and_without_pruning(
    name: str,
) -> None:
    """Item A, review round 1: the block's threshold note is **a claim about
    the data**.

    The number of observations the threshold dropped is not a presentation
    choice but an observation of what did not repeat enough. If pruning short
    circuited the row builder, the counter would shrink along with the
    pruning and the block would say *"jokainen havainto ylitti kynnyksen"* in
    a report in which one did not.

    In the fixture's ``default`` block both the saturated equipment row and
    the 45 s sample point fall below the threshold, so both pruning rules hit
    a row that would not be written anyway.
    """
    entry = pruning_report()
    plain = threshold_note(render(entry, NO_PRUNING))
    assert "harvinaisempaa havaintoa jäi pois" in plain
    assert threshold_note(render(entry, PRUNING_VARIANTS[name])) == plain


def test_a_row_the_threshold_already_dropped_is_not_claimed_as_pruned() -> None:
    """Item A2: the reading guide does not claim to have pruned a row that
    was not written.

    Rule 3 leaves the 45 s sample point unwritten, but here the threshold
    got there first: the point's only visible bar was seen on two rounds out
    of four, so the row is never built. Rule 3 therefore removed nothing,
    and it must not say that it did -- the missing row's reason is the
    threshold, and the block's own note says so.

    **The rule this is asked of changed on 2026-09-11**, and the reason is
    the cap. It used to be rule 1, on a two-round block whose saturated
    armour row the threshold dropped before rule 1 could see it. Saturation
    means the one bar carries every round (``n = m``) and the threshold is
    now capped at the block's round count, so a saturated row always clears
    it: that combination no longer exists, and a test built on it would
    watch an impossible state. Rule 3's row has no such tie between ``n``
    and ``m``, so the order of the two mechanisms is still observable
    there.
    """
    entry = report(
        [
            map_report(
                "de_nuke",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "full",
                                SMALL_SAMPLE + 1,
                                positions=[
                                    position(
                                        6.0,
                                        [area("Middle", SMALL_SAMPLE + 1, {2: 4})],
                                        SMALL_SAMPLE + 1,
                                    ),
                                    position(
                                        45.0,
                                        [
                                            area(
                                                "Middle",
                                                SMALL_SAMPLE + 1,
                                                {1: 2, 0: 2},
                                            )
                                        ],
                                        SMALL_SAMPLE + 1,
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )
    text = render(entry, SKIP_45)
    assert "6 s: Middle 2 (4/4 kierroksesta)" in text
    assert "45 s:" not in text
    legend = section_text(text, "Lukuohje")
    assert "Näytepistettä 45 s ei kirjoiteta" not in legend
    assert "[report]." not in legend


def test_a_pruned_row_does_not_leave_its_explanation_behind() -> None:
    """A dropped row's explanation does not stay in the reading guide.

    The only unknown area is on the 45 s sample point, which is left
    unwritten. The reading guide would otherwise explain a mark that is not
    in the report -- the same fault as a pruning paragraph about a rule that
    did not hit.
    """
    entry = report(
        [
            map_report(
                "de_nuke",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                2,
                                positions=[
                                    position(6.0, [area("Ramp", 2, {2: 2})], 2),
                                    position(45.0, [area(None, 2, {1: 2})], 2),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )
    assert UNKNOWN_AREA in render(entry, NO_PRUNING)
    assert UNKNOWN_AREA not in render(entry, SKIP_45)


# --- Rule 1: the saturated equipment row --------------------------------------


def test_a_saturated_equipment_line_is_left_out() -> None:
    """I/O matrix: the default block, 5 armoured on every round."""
    eco = block(render(pruning_report()), "Eco")
    assert "- panssaroituja ostoajan lopussa" not in eco
    # The armed row is not saturated (0), so it stays: the rule concerns a
    # saturated reading and not equipment rows in general.
    assert "aseistettuja ostoajan lopussa: 0 (4/4 kierroksesta)" in eco


def test_the_legend_says_what_a_missing_equipment_line_means() -> None:
    """Nothing vanishes in silence: the reading guide says what the absence
    means.
    """
    legend = section_text(render(pruning_report()), "Lukuohje")
    assert "Kylläinen kalustorivi on jätetty pois" in legend
    assert f"vain arvo {PLAYERS_ON_SERVER}" in legend
    # Saturation is not the only reason: the threshold can drop the same row,
    # and the reading guide must not claim otherwise (review round 1, item
    # D4).
    assert "Kylläisyys ei ole ainoa syy" in legend
    assert "[report].drop_saturated_equipment_lines" in legend


def test_a_zero_line_is_not_saturated_even_though_it_never_varies() -> None:
    """*"Ei kevuja"* is an observation, not an expectation.

    One bar with the value 0 is just as monotonous a row as one with the
    value 5, but it says the opposite thing -- and it is that row that is in
    the target analysis (Ancient, CT). The rule is written for the value, not
    for the absence of variation.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco", 3, players_armored=armored(3, {0: 3})
                            )
                        ],
                    )
                ],
            )
        ]
    )
    assert "panssaroituja ostoajan lopussa: 0 (3/3 kierroksesta)" in render(entry)


def test_a_saturated_line_with_unreadable_rounds_stays() -> None:
    """A note about unreadable rounds is an observation and does not
    vanish.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                4,
                                players_armored=armored(3, {5: 3}, unknown=1),
                            )
                        ],
                    )
                ],
            )
        ]
    )
    text = render(entry)
    assert "panssaroituja ostoajan lopussa: 5 (3/3 kierroksesta)" in text
    assert "havainto puuttuu 1 kierrokselta" in text


def test_a_varying_equipment_line_is_not_saturated() -> None:
    """Two bars mean that the reading varied -- variation is an
    observation.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco", 4, players_armored=armored(4, {5: 3, 4: 1})
                            )
                        ],
                    )
                ],
            )
        ]
    )
    assert "panssaroituja ostoajan lopussa: 5 (3/4" in render(entry)


def test_both_counters_saturated_and_identical_drop_before_they_merge() -> None:
    """The rules' order is load-bearing: the saturated row is dropped
    **before** the merging.

    If the merging happened first, one row would be written into the block,
    and that row is precisely the expectation ("all five had both a weapon
    and armour on every round") rule 1 leaves unsaid. Without this test the
    order would be documented but not enforced.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                3,
                                players_armed=armed(3, {5: 3}),
                                players_armored=armored(3, {5: 3}),
                                death_report=deaths(
                                    first={"Middle": 3}, median=20.0
                                ),
                            )
                        ],
                    )
                ],
            )
        ]
    )
    text = render(entry)
    assert MERGED_EQUIPMENT_LABEL not in text
    assert "ostoajan lopussa" not in text
    legend = section_text(text, "Lukuohje")
    assert "Kylläinen kalustorivi on jätetty pois" in legend
    assert "kirjoitettu yhtenä" not in legend


# --- Rule 2: identical equipment rows into one --------------------------------


def test_identical_equipment_lines_become_one_line_that_says_both() -> None:
    """I/O matrix: armed and armoured with the same distribution.

    In the fixture the number is 3 on every round -- not 5, so that the test
    measures the merging and not rule 1.
    """
    force = block(render(pruning_report()), "Force")
    assert MERGED_EQUIPMENT_LABEL in force
    assert "3 (3/3 kierroksesta)" in force
    assert "- aseistettuja ostoajan lopussa" not in force
    assert "- panssaroituja ostoajan lopussa" not in force


def test_the_merged_line_keeps_both_definitions_in_the_legend() -> None:
    """The merged row carries both numbers, so both are defined.

    Without both flags the reader would see the label "aseistettuja ja
    panssaroituja" without a definition of either word -- and the difference
    between the definitions is exactly what makes identical numbers an
    observation.
    """
    legend = section_text(render(pruning_report()), "Lukuohje")
    assert "Aseistettu = panssari JA parannettu ase" in legend
    assert "panssaroitu = panssari, aseesta riippumatta" in legend
    assert "Aseistettujen ja panssaroitujen rivi on kirjoitettu yhtenä" in legend
    assert "[report].merge_equal_equipment_lines" in legend


def test_differing_equipment_lines_stay_two_lines() -> None:
    """I/O matrix: 1 armed, 3 armoured -> two rows as before."""
    half = block(render(pruning_report()), "Puoliosto")
    assert "aseistettuja ostoajan lopussa: 1 (2/2 kierroksesta)" in half
    assert "panssaroituja ostoajan lopussa: 3 (2/2 kierroksesta)" in half
    assert MERGED_EQUIPMENT_LABEL not in half


def test_the_same_bars_with_a_different_note_are_not_the_same_line() -> None:
    """The bars identical, ``rounds_unknown`` different: the rows' notes
    differ.

    The note is an observation ("havainto puuttuu 1 kierrokselta"), so a
    merged row would give it for only one of the counters.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                4,
                                players_armed=armed(3, {3: 3}, unknown=1),
                                players_armored=armored(3, {3: 3}),
                            )
                        ],
                    )
                ],
            )
        ]
    )
    text = render(entry)
    assert MERGED_EQUIPMENT_LABEL not in text
    assert "aseistettuja ostoajan lopussa: 3 (3/3 kierroksesta)" in text
    assert "panssaroituja ostoajan lopussa: 3 (3/3 kierroksesta)" in text


# --- Rule 3: the sample point off ---------------------------------------------


def test_the_late_sample_point_stays_by_default() -> None:
    """The default is to keep it: 45 s is not repetition but a thin
    observation.
    """
    assert ReportSettings().skip_sample_seconds == []
    assert "45 s:" in block(render(pruning_report()), "Eco")


def test_a_named_sample_point_can_be_left_out() -> None:
    """The setting on -> the row is left out, and the other sample points
    stay.
    """
    eco = block(render(pruning_report(), SKIP_45), "Eco")
    assert "45 s:" not in eco
    assert "6 s:" in eco
    assert "15 s:" in eco
    assert "30 s:" in eco


def test_the_legend_says_which_sample_point_is_missing_and_why() -> None:
    legend = section_text(render(pruning_report(), SKIP_45), "Lukuohje")
    assert "Näytepistettä 45 s ei kirjoiteta tähän raporttiin" in legend
    assert "[parse].snapshot_seconds ole muuttunut" in legend
    assert "[report].skip_sample_seconds" in legend


def test_the_legend_does_not_quote_one_sample_points_numbers_for_another() -> None:
    """The paragraph does not claim measured numbers about a sample point
    that was not measured.

    45 s's coverage numbers (53 %, 81 %) are in ``settings.toml``, where they
    justify the default. In the reading guide they would be wrong as soon as
    the setting names some other sample point -- and that is a valid choice.
    """
    legend = section_text(
        render(pruning_report(), ReportSettings(skip_sample_seconds=[30.0])),
        "Lukuohje",
    )
    assert "Näytepistettä 30 s ei kirjoiteta" in legend
    assert "53 %" not in legend
    assert "81 %" not in legend


def test_the_sample_point_setting_matches_the_label_not_the_float() -> None:
    """``45`` and ``45.0`` mean the same row.

    The setting's value is a TOML number a human wrote and the sample point
    is the report's float; if the match were a float comparison, the spelling
    would settle whether the row is removed.
    """
    text = render(pruning_report(), ReportSettings(skip_sample_seconds=[45]))
    assert "45 s:" not in block(text, "Eco")


def test_the_setting_and_the_row_label_share_one_formatter() -> None:
    """Two layers, one formatter (:func:`seconds_label`).

    The load-time check "two values would look the same on the row" and the
    row's label are the same function. Without a shared source they would
    agree only today: adding one decimal to the row would make two settings
    values the same row without the validation noticing.
    """
    assert seconds_label(45.0) == "45"
    assert seconds_label(9.5) == "9,5"
    assert view_module._seconds(9.5) == seconds_label(9.5)
    with pytest.raises(ValidationError, match="twice"):
        ReportSettings(skip_sample_seconds=[45.0, 45.0000001])


def test_first_contact_is_not_a_sample_point_that_can_be_named() -> None:
    """First contact is the round's own moment, not a chosen sample point.

    It has no nominal number of seconds, so no value of the setting can hit
    it -- not the median's number of seconds either.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                2,
                                positions=[
                                    first_contact_position(
                                        [area("Middle", 2, {2: 2})], 2, median=9.0
                                    )
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )
    text = render(entry, ReportSettings(skip_sample_seconds=[9.0]))
    assert "ensikontakti (mediaani 9,0 s): Middle 2 (2/2 kierroksesta)" in text


# --- Rules 4 and 5: the most common, and the number dropped on the row --------


def test_a_utility_line_keeps_the_two_most_common_targets() -> None:
    """I/O matrix: 9 targets on the row -> the two most common + a
    mention.
    """
    eco = block(render(pruning_report()), "Eco")
    smoke = [row for row in eco.splitlines() if row.startswith("- savu:")][0]
    assert "TSpawn -> BombsiteA (arvio) 0-5 s (4/4 kierroksesta)" in smoke
    assert "TSpawn -> Palace (arvio) 0-5 s (4/4 kierroksesta)" in smoke
    assert "Connector" not in smoke
    # Three and not seven: the four targets seen on a single round fell
    # below the pattern threshold, and this note counts only what the
    # **limit** dropped.
    assert "3 harvinaisempaa kohdetta jäi pois" in smoke


def test_the_target_limit_counts_targets_not_claims() -> None:
    """Item C: the same target in two time buckets is **one** target.

    The time buckets (``[aggregate].utility_seconds_buckets``) produce two
    claims about the same target. By bounding claims, the two retained places
    could be the same target twice, and the row would lose every other target
    while the note calls them targets.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                6,
                                utility=[
                                    use(
                                        "smoke", "TSpawn", "BombsiteA",
                                        n=5, m=6, bucket="0-5",
                                    ),
                                    use(
                                        "smoke", "TSpawn", "BombsiteA",
                                        n=4, m=6, bucket="5-10",
                                    ),
                                    use(
                                        "smoke", "TSpawn", "Ramp",
                                        n=4, m=6, bucket="0-5",
                                    ),
                                    use(
                                        "smoke", "TSpawn", "Palace",
                                        n=3, m=6, bucket="0-5",
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )
    smoke = [
        row
        for row in block(render(entry), "Eco").splitlines()
        if row.startswith("- savu:")
    ][0]
    # Two targets: BombsiteA (both buckets) and Ramp.
    assert "BombsiteA (arvio) 0-5 s" in smoke
    assert "BombsiteA (arvio) 5-10 s" in smoke
    assert "Ramp" in smoke
    assert "Palace" not in smoke
    assert "1 harvinaisempaa kohdetta jäi pois" in smoke


def test_a_kept_claim_does_not_borrow_the_other_explanation() -> None:
    """An estimate and an unknown area do not light each other up.

    The row has two targets: one has an **observed** detonation area but an
    unknown throwing area, and the other (derived, "(arvio)") is dropped over
    the limit. The reading guide then holds the unknown area's explanation
    but **not** the estimate's -- and the other way round if it is the
    unknown one that goes over the limit.

    Found in our own code on review round 1: two flags had been bundled into
    one boolean, so one explanation appeared because of the other. The fault
    was latent, because in no fixture was there an unknown throwing area with
    an observed detonation area.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                4,
                                utility=[
                                    use(
                                        "smoke", None, "Middle",
                                        n=3, m=4, source="observed",
                                    ),
                                    use(
                                        "smoke", "TSpawn", "Ramp",
                                        n=2, m=4, source="point_cloud",
                                    ),
                                    use(
                                        "smoke", "TSpawn", "Palace",
                                        n=1, m=4, source="observed",
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )
    legend = section_text(
        render(entry, ReportSettings(max_utility_targets=1)), "Lukuohje"
    )
    assert UNKNOWN_AREA in legend
    assert "(arvio) räjähdysalueen perässä" not in legend

    # And to the other side of the limit: when the estimate stays on the row,
    # its explanation comes -- and the unknown area's explanation does not
    # come with it.
    entry_two = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                4,
                                utility=[
                                    use(
                                        "smoke", "TSpawn", "Middle",
                                        n=3, m=4, source="point_cloud",
                                    ),
                                    use(
                                        "smoke", None, "Ramp",
                                        n=2, m=4, source="observed",
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )
    legend_two = section_text(
        render(entry_two, ReportSettings(max_utility_targets=1)), "Lukuohje"
    )
    assert "(arvio) räjähdysalueen perässä" in legend_two
    assert UNKNOWN_AREA not in legend_two


def test_a_kill_line_keeps_the_three_most_common_areas() -> None:
    """I/O matrix: 9 areas on the row -> the three most common + a
    mention.
    """
    eco = block(render(pruning_report()), "Eco")
    kills = [row for row in eco.splitlines() if "tapot alueittain" in row][0]
    assert "BombsiteA (5/23 taposta)" in kills
    assert "Connector (4/23 taposta)" in kills
    assert "Palace (4/23 taposta)" in kills
    assert "Ramp" not in kills
    # Two and not six: the four areas with a single kill fell below the
    # pattern threshold, and this note counts only what the limit dropped.
    assert "2 harvinaisempaa aluetta jäi pois" in kills


def test_the_limit_continues_through_a_tie() -> None:
    """Item E1: an equally common observation is not *rarer*.

    Four equally common areas at a limit of three: cutting the limit in the
    middle would drop one of two observations with identical samples and the
    note would call it rarer. The limit is "the most common", not "at most
    three".
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                4,
                                death_report=deaths(
                                    first={"Middle": 4},
                                    median=20.0,
                                    kills={
                                        "BombsiteA": 3,
                                        "Palace": 3,
                                        "Ramp": 3,
                                        "Jungle": 3,
                                    },
                                ),
                            )
                        ],
                    )
                ],
            )
        ]
    )
    kills = [
        row
        for row in block(render(entry), "Eco").splitlines()
        if "tapot alueittain" in row
    ][0]
    for name in ("BombsiteA", "Palace", "Ramp", "Jungle"):
        assert name in kills
    assert "jäi pois" not in kills


def test_the_dropped_count_follows_the_pattern_filter_wording() -> None:
    """The same sentence as with pattern filtering: "N harvinaisempaa X jäi
    pois".

    The wording is a precedent and not a matter of taste: the reader sees the
    same sentence about observations left out for two different reasons, and
    it is the same thing -- the row saying what is missing from it.
    """
    text = render(pruning_report())
    assert "harvinaisempaa kohdetta jäi pois" in text
    assert "harvinaisempaa aluetta jäi pois" in text
    assert "harvinaisempaa havaintoa jäi pois" in render(report([default_map()]))


def test_a_line_at_the_limit_gets_no_note() -> None:
    """An empty note would set a dash on the row with nothing after it."""
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                2,
                                utility=[
                                    use("smoke", "TSpawn", "Middle", n=2, m=2),
                                    use("smoke", "TSpawn", "Ramp", n=2, m=2),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )
    text = render(entry)
    assert "Ramp" in text
    assert "jäi pois" not in text


def test_the_legend_names_the_limits_and_their_settings() -> None:
    legend = section_text(render(pruning_report()), "Lukuohje")
    assert "kirjoitetaan 2 yleisintä **kohdetta**" in legend
    assert "[report].max_utility_targets" in legend
    assert "kirjoitetaan 3 yleisintä aluetta" in legend
    assert "[report].max_kill_areas" in legend


# --- The protected round types ------------------------------------------------


def test_the_pistol_block_is_not_pruned_by_any_rule() -> None:
    """The spec's Always rule: on a pistol round the number is a buy
    observation.

    One test for all five rules, because the claim is one: the same block, in
    which every rule would hit, is identical both with the default settings
    (and rule 3 on) and with pruning entirely off.
    """
    pruned = block(render(pruning_report(), SKIP_45), "Pistooli")
    plain = block(render(pruning_report(), NO_PRUNING), "Pistooli")
    assert pruned == plain
    # And by name, so that the rows cannot vanish from both at once.
    assert "aseistettuja ostoajan lopussa: 5 (2/2 kierroksesta)" in pruned
    assert "panssaroituja ostoajan lopussa: 5 (2/2 kierroksesta)" in pruned
    assert "45 s:" in pruned
    assert "CTSpawn -> Palace" in pruned
    assert "Window (1/8 taposta)" in pruned


def test_an_anomaly_block_is_protected_because_the_line_is_the_anomaly() -> None:
    """``anomaly`` is an economy that looked impossible.

    The round type's **whole rationale** is the equipment row: ``classify``
    marked the round an anomaly because the buy did not match the previous
    round's outcome. Dropping the saturated row would remove the block's only
    reason to exist.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "anomaly",
                                2,
                                players_armed=armed(2, {5: 2}),
                                players_armored=armored(2, {5: 2}),
                            )
                        ],
                    )
                ],
            )
        ]
    )
    text = render(entry)
    assert "aseistettuja ostoajan lopussa: 5 (2/2 kierroksesta)" in text
    assert "panssaroituja ostoajan lopussa: 5 (2/2 kierroksesta)" in text
    assert MERGED_EQUIPMENT_LABEL not in text


def test_overtime_is_not_protected_and_the_reason_is_measured() -> None:
    """``ot`` is **not** protected: overtime's starting money is a full buy.

    Overtime's first round looks like a pistol round, but
    ``[league].ot_start_money`` is $12,500 in this league, so everyone buys
    full equipment and ``5/5`` is the expectation as on a full buy. The
    dependency is written into ``PROTECTED_ROUND_TYPES``'s docstring: if the
    starting money falls to pistol level, ``ot`` has to be protected.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "ot",
                                3,
                                players_armed=armed(3, {5: 3}),
                                players_armored=armored(3, {5: 3}),
                            )
                        ],
                    )
                ],
            )
        ]
    )
    assert "ostoajan lopussa" not in render(entry)
    assert "ot" not in PROTECTED_ROUND_TYPES


def test_the_protected_round_types_are_named_and_bounded() -> None:
    """The bound is named, so that widening it is a contract change."""
    assert PROTECTED_ROUND_TYPES == frozenset({"pistol", "anomaly"})
    assert PROTECTED_ROUND_TYPES <= set(ROUND_TYPES)
    # The pattern bound and the pruning bound are different sets for
    # different reasons; if they intersected, the same block would be both
    # "patterns only" and unpruned.
    assert not PROTECTED_ROUND_TYPES & PATTERN_ROUND_TYPES


def test_every_pruning_paragraph_says_the_exception_out_loud() -> None:
    """Item D1: an unqualified sentence and an unpruned block in the same
    report.

    A paragraph that says "the two most common targets are written" is a
    false sentence in a report whose pistol block prints four -- unless it
    says the exception out loud. The claim is driven from
    ``PROTECTED_ROUND_TYPES``, so a sixth protected type or a sixth rule
    inherits the check.
    """
    legend = section_text(render(pruning_report(), SKIP_45), "Lukuohje")
    paragraphs = [row for row in legend.splitlines() if "[report]." in row]
    assert len(paragraphs) == 5
    for paragraph in paragraphs:
        assert "Karsinta ei koske näitä kierrostyyppejä" in paragraph
        for round_type_name in PROTECTED_ROUND_TYPES:
            assert ROUND_TYPE_FI[round_type_name] in paragraph


# --- The block that would empty -----------------------------------------------


def emptied_block_report() -> Report:
    """A block whose **only** row is a saturated equipment row.

    The state is more particular than it looks, which is why it is in a
    function of its own. A round type that has rounds almost always has a
    death row: if the team lost a player, the row has an area, and if it did
    not, the row has a note ("ei omia kuolemia 3 kierroksella"). Neither is
    pruned, so the block does not empty.

    The only route to an empty block runs through the **pattern threshold**:
    on a full buy every scattered first-death area falls below the threshold,
    and if the team lost a player on every round, there is no note either.
    One row is then left, and its saturation would trigger rule 1.
    """
    return report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "full",
                                8,
                                players_armored=armored(8, {5: 8}),
                                death_report=deaths(
                                    first={f"Alue{n}": 1 for n in range(8)}
                                ),
                            )
                        ],
                    )
                ],
            )
        ]
    )


def test_a_rule_that_would_empty_a_block_keeps_the_lines_and_says_so() -> None:
    """I/O matrix: a rule would remove every row of the block -> the rows
    stay.

    An empty block would stop saying anything, and that is **a suppression
    decision and not pruning** -- suppression is scoped out of this story.
    """
    text = render(emptied_block_report())
    assert "panssaroituja ostoajan lopussa: 5 (8/8 kierroksesta)" in text
    assert "Karsinta olisi poistanut tästä lohkosta jokaisen rivin" in text
    # The reading guide does not claim to have pruned anything, because
    # nothing was pruned.
    assert "Kylläinen kalustorivi on jätetty pois" not in text


def test_the_kept_block_is_the_same_block_as_without_pruning() -> None:
    """The return to the unpruned form is complete and not partial.

    The block is row for row the same as without pruning -- the only
    difference is the note that says why. Without this claim an
    implementation could return one row and prune the rest.
    """
    entry = emptied_block_report()
    kept = block(render(entry), "Default")
    plain = block(render(entry, NO_PRUNING), "Default")
    assert kept == plain + f"\n- *{_PRUNING_KEPT_THE_BLOCK}*"


def test_the_kept_block_does_not_undo_a_shortened_row() -> None:
    """Truncating a row (rules 4 and 5) is no part of the return.

    Truncation cannot empty a block, so undoing it would only bring back the
    5-9 item list the whole story is written against. The block has a
    saturated equipment row (rule 1 would drop it) **and** nine kill areas,
    because of which the block does not empty -- so the truncation stands and
    rule 1 is undone.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "eco",
                                4,
                                players_armored=armored(4, {5: 4}),
                                death_report=deaths(
                                    first={"Middle": 4},
                                    median=20.0,
                                    kills=PRUNE_KILLS,
                                ),
                            )
                        ],
                    )
                ],
            )
        ]
    )
    eco = block(render(entry), "Eco")
    assert "- panssaroituja ostoajan lopussa" not in eco
    assert "harvinaisempaa aluetta jäi pois" in eco
    assert "Karsinta olisi poistanut" not in eco


def test_a_block_that_was_empty_anyway_is_not_blamed_on_pruning() -> None:
    """The threshold ate everything, and pruning had no part in it.

    The same fixture as above but without the saturated row: the block
    empties from the threshold, and that is said with the sentence it has
    always been said with.
    """
    entry = report(
        [
            map_report(
                "de_ancient",
                [
                    side(
                        "T",
                        [
                            round_type(
                                "full",
                                8,
                                death_report=deaths(
                                    first={f"Alue{n}": 1 for n in range(8)}
                                ),
                            )
                        ],
                    )
                ],
            )
        ]
    )
    text = render(entry)
    assert "Ei kuvioita, jotka ylittäisivät kynnyksen." in text
    assert "Karsinta olisi poistanut" not in text


# --- The reading guide and the summary tell of the rules ---------------------


def test_the_legend_explains_only_the_rules_that_pruned_something() -> None:
    """An explanation of a rule that did not hit would be a claim about the
    report that does not hold.

    On the pistol map every rule is on but none prunes, so not one pruning
    paragraph is written -- nor the settings' names.
    """
    legend = section_text(render(report([pistol_map()])), "Lukuohje")
    assert "Kylläinen kalustorivi on jätetty pois" not in legend
    assert "yleisintä **kohdetta**" not in legend
    assert "[report]." not in legend


def test_the_summary_names_the_rules_even_when_nothing_was_pruned() -> None:
    """Item D5: the reader of a clean report sees the adjusted value.

    The pruning paragraphs are written only about the rules that hit, so
    their absence does not say whether a rule was on. The summary lists the
    ``[report]`` section on the same grounds as the thresholds: the reader
    judges a claim by how it was computed -- and pruning decides which claims
    he sees.
    """
    summary = summary_text(render(report([pistol_map()]), SKIP_45))
    assert "Karsinnan säännöt" in summary
    for key in ReportSettings.model_fields:
        assert key in summary
    assert "skip_sample_seconds 45" in summary
    assert "max_kill_areas 3" in summary


def test_the_summary_row_is_mechanical_so_a_sixth_rule_joins_it() -> None:
    """The row comes about from the section's fields, not from a
    hand-written sentence.

    A hand-written sentence would fall behind precisely when a rule is added
    -- and that is this story's own failure mode (a setting that shows
    nowhere).
    """
    summary = summary_text(render(report([pistol_map()])))
    row = [line for line in summary.splitlines() if "Karsinnan säännöt" in line][0]
    assert row.count(",") == len(ReportSettings.model_fields) - 1
    assert "ei yhtään" in row


# --- The metric: how much shorter ---------------------------------------------


def test_the_delivered_defaults_shorten_the_report() -> None:
    """**The delivered configuration** shortens the report, not some other
    one.

    An earlier version of this test measured with rule 3, which is off by
    default -- that is, the configuration nobody runs. The number is the
    fixture's and not the RCAVE report's, so it is not a measured percentage;
    the claim is the direction, and that every row left out is explained.
    """
    plain = content_rows(render(pruning_report(), NO_PRUNING))
    delivered = content_rows(render(pruning_report(), DEFAULT_PRUNING))
    assert len(delivered) < len(plain)
    # Two rows: the eco block's saturated armour row and the other half of
    # the merged pair. The default block's saturated row is not among them,
    # because the threshold dropped it already -- and the 45 s row stays,
    # because rule 3 is off by default.
    assert len(plain) - len(delivered) == 2
    # With rule 3 on the sample point goes too: one row from the eco block,
    # the pistol block is protected and the default block's row was below the
    # threshold already.
    with_rule_three = content_rows(render(pruning_report(), SKIP_45))
    assert len(delivered) - len(with_rule_three) == 1


# --- Story 2.15: the retro's consistency fixes -----------------------------------


#: The case the retro measured (A2): RCAVE ``de_anubis`` default. Nine
#: rounds, an own death on seven and **every one in a different area**, so
#: every area row's repetition is 1 and the pruning threshold (3) takes them
#: all.
PRUNED_AWAY_DEATHS = {
    "Alley": 1,
    "BombsiteB": 1,
    "Bricks": 1,
    "Bridge": 1,
    "Connector": 1,
    "Middle": 1,
    "Palace": 1,
}


def pruned_death_report(**kwargs) -> Report:
    """A default block that pruning concerns (``full`` is a pattern type).

    :func:`death_report` builds a pistol block, and pistol is **protected**
    from pruning (``PROTECTED_ROUND_TYPES``) -- A2 cannot recur there.
    """
    rounds = kwargs.pop("rounds", 9)
    kwargs.setdefault(
        "rounds_missing", rounds - sum((kwargs.get("first") or {}).values())
    )
    entry = round_type("full", rounds, death_report=deaths(**kwargs))
    return report([map_report("de_ancient", [side("CT", [entry])])])


def test_a_median_left_alone_by_pruning_carries_its_own_sample() -> None:
    """A2: pruning can strip the sample from a claim that is left behind.

    Measured from the RCAVE report: when each of the seven deaths was in a
    different area, every area row fell below the threshold and the row was
    left as
    ``ensimmäinen kuolema (mediaani 14,2 s): ei omia kuolemia 2 kierroksella``
    -- a timing without a single number saying what it was computed from. The
    epic's second criterion is *"and not one claim is presented without a
    sample"*, and a median is a claim.
    """
    text = render(pruned_death_report(first=PRUNED_AWAY_DEATHS, median=14.2))
    assert (
        "- ensimmäinen kuolema (mediaani 14,2 s, 7/9 kierroksesta): "
        "ei omia kuolemia 2 kierroksella"
    ) in text


def test_a_median_beside_its_areas_is_not_given_a_second_sample() -> None:
    """A2's other direction: a row that already has a sample does not
    change.

    **This is the fix's bound.** The area rows carry the sample themselves,
    and a number added to the label would be the same sample twice on the
    same row -- and would change every working row in the report.
    """
    text = render(
        pruned_death_report(
            first={"Outside": 4, "Ramp": 3}, rounds=8, median=19.1
        )
    )
    row = next(
        line for line in text.splitlines() if "ensimmäinen kuolema" in line
    )
    # The claim concerns **this row**, not the report: "kierroksesta)" hits
    # anywhere in the report, so it would prove nothing about the label.
    assert row == (
        "- ensimmäinen kuolema (mediaani 19,1 s): Outside (4/7 kierroksesta), "
        "Ramp (3/7 kierroksesta) -- ei omia kuolemia 1 kierroksella"
    )


def test_a_first_death_line_without_a_median_stays_a_bare_label() -> None:
    """Without a median the row has no claim, so it has no sample either.

    ``ei omia kuolemia 9 kierroksella`` is a coverage note: it says that the
    team lost nobody. A sample would give a sample to a claim that is not
    there.
    """
    text = render(pruned_death_report(first={}, median=None))
    assert "- ensimmäinen kuolema: ei omia kuolemia 9 kierroksella" in text
    assert "kierroksesta)" not in text.split("**Default**")[1].split("##")[0]


def test_a_workshop_map_name_is_protected_in_the_chapter_heading() -> None:
    """B1: the map's name was the body's only unprotected string the demo
    gave.

    Story 2.11 made ``map_name`` free demo text: ``*|Aim|* Botz [beta]`` is a
    legal observation. Bare, the heading broke in the middle and the bold was
    left unclosed for the whole rest of the report.

    The protection is **a code span and not escaping**, and the mechanism is
    borrowed from ``_map_label``: the same map in two spellings would read as
    two maps, and the report is read raw as well.
    """
    name = "*|Aim|* Botz [beta]"
    text = render(report([demo_map([MISSING_DEMO_ID], name=name)]))
    assert f"## {BACKTICK}{name}{BACKTICK} -- " in text
    # The heading line contains no unpaired emphasis character outside the
    # code span: that is exactly what broke the row before the fix.
    heading = next(
        line
        for line in text.splitlines()
        if line.startswith("## ") and "Botz" in line
    )
    assert heading.count(BACKTICK) == 2
    assert "*" not in heading.replace(name, "")


def test_a_workshop_map_name_is_protected_on_the_anomaly_line() -> None:
    """B1: the same rule on the anomaly row.

    The row carries the anomaly's sample, so breaking it would take with it
    precisely the number the row exists for.
    """
    name = "*|Aim|* Botz [beta]"
    text = anomaly_text(
        render(
            report(
                [map_report(name, [side("CT", [round_type("eco", 3)])])],
                anomalies=[anomaly(map_name=name)],
            )
        )
    )
    assert f"CT-eteneminen ({BACKTICK}{name}{BACKTICK}, CT-puoli, eco)" in text


def test_an_unrecognised_map_label_is_our_own_text_and_stays_bare() -> None:
    """B1's bound: an unrecognised map's label holds no character the demo
    gave.

    The label is ``UNKNOWN_MAP_LABEL``, that is a sentence we wrote, and a
    code span would make it look like an id.
    """
    text = anomaly_text(
        render(
            report(
                [
                    map_report(
                        MISSING_DEMO_ID,
                        [side("CT", [round_type("eco", 3)])],
                        source="unknown",
                    )
                ],
                anomalies=[
                    anomaly(map_name=MISSING_DEMO_ID, map_name_source="unknown")
                ],
            )
        )
    )
    label = UNKNOWN_MAP_LABEL.format(index=1)
    assert f"CT-eteneminen ({label}, CT-puoli, eco)" in text


def test_both_threshold_readers_agree_on_a_value_below_one() -> None:
    """B4: two readers, one lookup -- and the copies had already drifted.

    ``_threshold_int`` demanded a positive value, ``_threshold_float``
    accepted zero and negatives, although the former's own rationale is
    *"as two copies one would drift"*. ``ThresholdSettings`` demands a
    positive value from every threshold, so zero is a threshold for neither.
    """
    entry = report([pistol_map()], thresholds_used={"thresholds": {
        "small_sample_rounds": 0,
        "advance_t_share": 0.0,
        "advance_max_sample_s": -1.0,
    }})
    assert view_module._threshold_int(entry, "small_sample_rounds") is None
    assert view_module._threshold_float(entry, "small_sample_rounds") is None
    assert view_module._threshold_float(entry, "advance_t_share") is None
    assert view_module._threshold_float(entry, "advance_max_sample_s") is None


def test_only_the_type_and_its_own_floor_differ_between_the_readers() -> None:
    """B4: two conditions are left to the callers, and both are dictated by
    the type.

    The permitted type (``int`` vs. any number) and the floor in the form the
    type requires (``>= 1`` for a count, ``> 0`` for a share). The floors are
    not the same condition twice but the same rule -- a threshold is positive
    -- in two units: ``advance_t_share = 0.80`` is the smallest threshold in
    use, and an integer floor would reject it.

    Everything else -- the section, the key, ``bool``, finiteness -- is the
    same code, so it cannot drift.
    """
    entry = report([pistol_map()], thresholds_used={"thresholds": {
        "advance_t_share": 0.80,
        "small_sample_rounds": 3,
        "a-switch": True,
        "a-string": "three",
        "an-infinity": float("inf"),
    }})
    assert view_module._threshold_float(entry, "advance_t_share") == 0.80
    assert view_module._threshold_int(entry, "advance_t_share") is None
    assert view_module._threshold_int(entry, "small_sample_rounds") == 3
    assert view_module._threshold_float(entry, "small_sample_rounds") == 3.0
    for name in ("a-switch", "a-string", "an-infinity", "no-such-threshold"):
        assert view_module._threshold_int(entry, name) is None, name
        assert view_module._threshold_float(entry, name) is None, name


def test_a_report_without_a_threshold_section_reads_as_no_threshold() -> None:
    """B4: a shared condition, not two copies -- a missing section is the
    same for both.
    """
    entry = report([pistol_map()], thresholds_used={})
    assert view_module._threshold_int(entry, "small_sample_rounds") is None
    assert view_module._threshold_float(entry, "advance_t_share") is None


def test_the_anomaly_legend_has_as_many_paragraphs_as_it_claims() -> None:
    """B5: the docstring is the rule's only statement, so it has to be
    guarded.

    There were three paragraphs in Story 2.5 and five after Story 2.14, but
    the docstring still said three. The number is in this test, so that the
    next rule cannot add a paragraph while the docstring goes stale in
    silence.
    """
    entry = report([pistol_map()])
    assert len(view_module._anomaly_legend(entry)) == 5


def test_the_module_docstring_names_only_functions_that_exist() -> None:
    """B5: the module docstring's list of functions must not name one that
    does not exist.

    **This is a weaker claim than the previous version promised, and that is
    deliberate.** The earlier test repeated the docstring's six names by
    hand, whereupon a seventh function would have passed in silence -- it
    promised a guard that was not there. The set cannot be derived from the
    code syntactically: ``_identifier`` has nine callers, but some of them
    (``_map_label``, ``_team_key_text``) **are** the traceability chapter and
    not the body, and the difference is in the meaning and not in the form.

    What can be proved is proved: every named function exists. The list's
    completeness is left to review, and this docstring says so instead of the
    test's name claiming otherwise.
    """
    doc = view_module.__doc__ or ""
    named = set(re.findall(r":func:`(_[a-z_]+)`", doc))
    assert named, "the module docstring names no function at all"
    missing = sorted(name for name in named if not hasattr(view_module, name))
    assert not missing, missing
    # Story 2.15's own finding: ``_anomaly_map_label`` was missing from the
    # list.
    assert "_anomaly_map_label" in named


def test_a_first_contact_median_left_alone_by_pruning_carries_its_sample() -> None:
    """The same fault as in A2, one function further away in the same
    report.

    ``_position_label`` set ``ensikontakti (mediaani 14,2 s)`` without a
    sample, and ``_position_line`` writes the row also when pruning has taken
    every area claim. What was left was the **character for character same
    shape** A2 fixed on the first-death row:
    ``ensikontakti (mediaani 14,2 s): näyte puuttuu 2 kierrokselta``.
    """
    entry = round_type(
        "full",
        9,
        positions=[
            position(
                None,
                [
                    area(name, 7, {1: 1, 0: 6})
                    for name in FIRST_CONTACT_SPREAD
                ],
                7,
                kind="first_contact",
                median=14.2,
                missing=2,
            )
        ],
    )
    text = render(report([map_report("de_ancient", [side("CT", [entry])])]))
    row = next(line for line in text.splitlines() if "ensikontakti" in line)
    assert row == (
        "- ensikontakti (mediaani 14,2 s, 7/9 kierroksesta): "
        "näyte puuttuu 2 kierrokselta"
    )


def test_a_first_contact_median_beside_its_areas_is_left_alone() -> None:
    """The fix's bound: a row that already has a sample does not change."""
    entry = round_type(
        "full",
        9,
        positions=[
            position(
                None,
                [
                    area("Middle", 7, {1: 4, 0: 3}),
                    area("Ramp", 7, {1: 3, 0: 4}),
                ],
                7,
                kind="first_contact",
                median=14.2,
                missing=2,
            )
        ],
    )
    text = render(report([map_report("de_ancient", [side("CT", [entry])])]))
    row = next(line for line in text.splitlines() if "ensikontakti" in line)
    assert row.startswith("- ensikontakti (mediaani 14,2 s): Middle 1 (4/7 ")
    assert "mediaani 14,2 s," not in row


def test_a_time_sample_left_alone_by_pruning_gets_no_invented_sample() -> None:
    """A time sample point's label is the name of a moment and not a claim.

    ``15 s`` claims nothing, so the row has no sample it could carry -- and
    an invented number would look on the row exactly like a measured one.
    """
    entry = round_type(
        "full",
        9,
        positions=[
            position(
                15.0,
                [
                    area(name, 7, {1: 1, 0: 6})
                    for name in FIRST_CONTACT_SPREAD
                ],
                7,
                missing=2,
            )
        ],
    )
    text = render(report([map_report("de_ancient", [side("CT", [entry])])]))
    row = next(line for line in text.splitlines() if line.startswith("- 15 s"))
    assert row == "- 15 s: näyte puuttuu 2 kierrokselta"


def test_a_hostile_area_name_does_not_break_the_position_line() -> None:
    """The area is text the demo gave just as the team's and the map's names
    are.

    The game gives ``m_szLastPlaceName``, and it is not validated against any
    list: a workshop map's area ``*|Aim|* Botz [beta]`` is a legal
    observation. Bare it broke the observation row in the middle -- and the
    row carries the claim's sample.
    """
    hostile = "*|Aim|* Botz [beta]"
    entry = round_type(
        "eco", 3, positions=[position(15.0, [area(hostile, 3, {2: 3})], 3)]
    )
    text = render(report([map_report("de_ancient", [side("CT", [entry])])]))
    row = next(line for line in text.splitlines() if line.startswith("- 15 s"))
    assert hostile not in row
    assert "\\*\\|Aim\\|\\* Botz \\[beta\\] 2 (3/3 kierroksesta)" in row


def test_a_hostile_area_name_does_not_break_the_anomaly_line() -> None:
    """**Both** halves of an anomaly row are text the demo gave.

    The map's name was protected as a code span in Story 2.15, but the same
    row's area was left bare -- that is, the row still broke, now from its
    right half. Either alone is not enough.
    """
    hostile = "*|Aim|* Botz [beta]"
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(area=hostile)],
            )
        )
    )
    assert hostile not in text
    assert "\\*\\|Aim\\|\\* Botz \\[beta\\] (1/3 kierroksesta" in text


def test_a_real_area_name_is_untouched_by_the_protection() -> None:
    """The protection's price is zero on real areas.

    Not one of CS2's ``env_cs_place`` names holds a Markdown structural
    character, so the report does not change -- which is exactly why the
    protection could be added without changing a single number.
    """
    text = render(report([pistol_map()]))
    rows = observation_rows(text)
    assert rows
    # The observation rows only: the round appendix's paths are Windows
    # paths, in which the backslash is content and not an escape.
    assert not [row for row in rows if "\\" in row]
