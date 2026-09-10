"""The command-line shell (Typer).

The CLI is thin: it reads the settings, chooses the stages and shows the
result. It does not call the adapters or the archive directly, and it holds no
analysis logic -- the same pipeline will later be run behind a web shell
without changing the domain.

The commands: ``info`` shows the settings, the state of the archive and the
state of the credentials without revealing the credentials' values,
``discover`` fetches the division's matches and writes the match index and the
team index out of them, ``select`` chooses a team's maps with the roster
threshold, ``parse`` runs the pipeline's demo stage for one demo, ``classify``
classifies its rounds from one team's point of view, ``aggregate`` gathers a
team's classified rounds into one ``report.json`` file and ``report`` writes a
readable Markdown report out of it. Three commands bring demos into the
archive, and they are the same outcome of three different unit choices:
``fetch`` downloads one team's sample, ``collect`` the whole division's
finished matches from the match index, and ``import`` takes in a demo
downloaded with a browser -- and **``import`` is the only route when there is
no Downloads permission**. ``scout`` (Story 4.1) runs the whole chain for one
team in one command, over ``stages.pipeline``; the single-stage commands stay
beside it, because they are how a person stops and looks at an intermediate
result. ``next`` comes in a later story.

**The listing does not open with a count.** The line still promised seven
commands when there were nine: a hand-maintained number that no test guards
goes stale at the first addition. The listing itself is guarded
(``test_help_lists_every_pipeline_command``), so the number says nothing that
is not already read from the next sentence.

Every pipeline command is in ``test_help_lists_every_pipeline_command``'s
listing. That is not a formality: in Story 3.2 ``discover`` was added without
it, and the command was in the help but the claim that it exists was missing
from exactly the test whose docstring warns about this.

The archive and the adapters are not touched from here: the paths are asked
of ``stages.archive_paths``, the demo port of
``stages.parse.default_parser`` and the match port of
``stages.discover.default_source``. The dependency arrow is
``cli -> stages -> {domain, adapters, archive}``.

The user does not code, so no error may reach the screen as a raw traceback:
:func:`main` turns them into readable messages and exit codes.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import typer

from pappascout import __version__
from pappascout.constants import (
    ROSTER_CLASSES,
    ROUND_TYPES,
    SAMPLE_BUCKET_FI,
    SAMPLE_BUCKETS,
    UNCLASSIFIED,
)
from pappascout.domain.models import Settings, load_settings, secrets_env_path
from pappascout.errors import PappascoutError
from pappascout.render.view import players_text
from pappascout.stages import StageResult, archive_paths
from pappascout.stages import aggregate as aggregate_stage
from pappascout.stages import classify as classify_stage
from pappascout.stages import discover as discover_stage
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import import_demo as import_stage
from pappascout.stages import parse as parse_stage
from pappascout.stages import pipeline as pipeline_stage
from pappascout.stages import render as render_stage
from pappascout.stages import select as select_stage

__all__ = ["app", "main"]

app = typer.Typer(
    name="pappascout",
    help="Pappaliiga CS2 opponent scouting: round types and a report from demos.",
    no_args_is_help=True,
    add_completion=False,
)

_SECRET_NAMES = ("FACEIT_API_KEY", "FACEIT_DOWNLOADS_TOKEN")

#: The exit codes. 0 = succeeded, 1 = a known error, 2 = an unexpected error.
EXIT_KNOWN_ERROR = 1
EXIT_UNEXPECTED_ERROR = 2

#: How many players are listed by name on one line of a summary.
#: The rest are counted; the whole listing is always in the index file.
MAX_LISTED_PLAYERS = 5


# **There is one byte-count formatter, and it is ``fetch_stage.size_fi``.**
# This module had a ``_human_size`` of its own with a unit table of its own,
# and the tables were of different lengths (``Pt`` only here) -- so the same
# number could print two different ways depending on which command printed it.
# Story 3.6 knew about ``size_fi`` and used it in the stage layer but not here.
# Now ``Pt`` is in ``size_fi``'s table and this layer calls it. The guard:
# ``tests/test_cli.py::test_only_one_byte_formatter_exists``.


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pappascout {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(  # noqa: ARG001 - Typer's callback convention
        False,
        "--version",
        help="Show the version and stop.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Pappascout."""


@app.command("info")
def info(
    size: bool = typer.Option(
        False,
        "--size",
        help=(
            "Compute the archive's total size as well. Off by default, "
            "because it reads through the whole directory tree."
        ),
    ),
) -> None:
    """Show the settings, the state of the archive and the state of the credentials.

    A credential's value is never printed -- only whether it is set.
    """
    settings = load_settings()
    typer.echo(_render_info(settings, show_size=size))


def _pruning_value(value: object) -> str:
    """A pruning setting's value for the ``info`` output.

    The same *shape* as in the report's summary
    (:func:`pappascout.render.view._pruning_summary_text`): an empty list is a
    word of its own and not an empty string, and a boolean is a word and not
    ``True``. Two outputs of the same section in different shapes would read
    as if the values were different.

    **The wording is no longer shared, and that is AD-11's line.** The report
    is read in Finnish and this line is console output, so the report keeps
    its own Finnish word where this one says "yes". What has to stay identical
    is which settings appear and in what form, because that is what the reader
    compares between the two.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return "/".join(f"{item:g}".replace(".", ",") for item in value) or "none"
    return str(value)


def _render_info(settings: Settings, show_size: bool = False) -> str:
    """Assemble the ``info`` command's output.

    Split into a function of its own so that the output can be tested without
    running the command line.

    Args:
        settings: The loaded settings.
        show_size: Whether the archive's total size is computed. Not by
            default: the archive holds hundreds of megabytes of demos, and
            walking the whole tree would make a fast status check slow.
    """
    archive = archive_paths(settings.project)
    lines: list[str] = []

    lines.append(f"Pappascout {__version__}")
    lines.append("")

    lines.append("Settings")
    lines.append(f"  Settings file      {settings.settings_file}")
    lines.append(f"  Own team           {settings.project.own_team_name}")
    lines.append(f"  Language           {settings.project.language}")
    lines.append(f"  Season             {settings.league.season}")
    lines.append(f"  Championships      {', '.join(settings.league.championship_ids)}")
    lines.append(f"  Map pool           {', '.join(settings.league.map_pool)}")
    lines.append(
        "  Own default bans   "
        + (", ".join(settings.league.own_default_bans) or "not set")
    )
    lines.append(
        f"  Format             MR{settings.league.mr}, "
        f"${settings.league.ot_start_money} starting money in overtime"
    )
    lines.append(
        "  Sample points      "
        + ", ".join(f"{s:g} s" for s in settings.parse.snapshot_seconds)
    )
    lines.append(
        f"  Full buy threshold ${settings.thresholds.full_equip_min} / player"
    )
    lines.append(
        "  Pistol rounds      "
        + ", ".join(str(r) for r in settings.thresholds.pistol_rounds)
        + f"; regulation rounds {settings.thresholds.regulation_rounds}"
    )
    # The pruning rules (Story 2.13). The line is a mechanical listing of the
    # section's fields, so a sixth rule shows up as soon as it is in the
    # section -- and it is here for the same reason as in the report's
    # summary: a rule that never hits does not show in the report at all, so
    # without this line the user has nowhere to see which rules are on.
    lines.append(
        "  Pruning            "
        + ", ".join(
            f"{key} {_pruning_value(value)}"
            for key, value in sorted(settings.report.model_dump(mode="json").items())
        )
    )
    lines.append("")

    lines.append("Archive")
    lines.append(f"  Path               {archive.root}")
    # **Where the demos are has to be said when it is not the archive.** A
    # downloaded demo can be on a local disk outside the synchronised folder
    # (Story 3.4), and the user should not have to open the settings file to
    # see where.
    demos_note = " (local)" if archive.demos_root is not None else ""
    lines.append(f"  Demos              {archive.demos_dir()}{demos_note}")
    if not archive.exists():
        lines.append(
            "  Status             missing -- the directory is created on the first run"
        )
    elif show_size:
        lines.append(
            "  Status             found, "
            f"{fetch_stage.size_fi(archive.total_size_bytes())}"
        )
    else:
        lines.append("  Status             found")
        lines.append("  Size               not computed (--size computes it)")
    lines.append("")

    lines.append("Credentials")
    lines.append(f"  File               {settings.secrets_file or secrets_env_path()}")
    width = max(len(name) for name in _SECRET_NAMES)
    for name in _SECRET_NAMES:
        lines.append(f"  {name:<{width}} {settings.secret_status(name)}")

    return "\n".join(lines)


@app.command("discover")
def discover(
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "The team's name, an unambiguous part of it, or the team id. "
            "Letter case does not matter. An ambiguous name lists the "
            "alternatives and chooses nothing. Without this the indexes are "
            "written and no team is looked up."
        ),
    ),
) -> None:
    """Fetch the division's matches and write the match index and the team index.

    One network call per competition is enough: the match row carries both
    teams' starters and substitutes. The standing roster is their union over
    all of the team's matches -- the unplayed ones too, so one match played
    out of eleven does not make the roster incomplete.

    The command fetches the match list **every single time**: it is not cached
    and the run is not skipped, because seeing the new matches is the whole
    point of the command. That is why there is no --force option here.

    The archive's directories are not renamed. The bridge to the archive is
    visible in the lineup_keys field of index/teams.json.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    typer.echo("Fetching the division's matches...", err=True)
    result = discover_stage.run(
        settings.league,
        archive,
        team,
        source=discover_stage.default_source(settings, archive),
        thresholds=settings.thresholds,
    )
    typer.echo(_render_discover(result))


def _render_discover(result: StageResult) -> str:
    """Assemble the ``discover`` command's summary.

    The most important line is the scope of the teams and the rosters: the
    user checks from it whether the whole division shows up. The range of
    roster sizes is there because too small a roster is the only way to notice
    a missing ``substitutes`` list without opening the file.

    Without the ``--team`` option the output **lists the division's teams**.
    Without that, the names could be seen only by deliberately entering an
    unknown name and reading them off the error message -- that is, exactly
    what the user needs after an ambiguous search would not be available
    without causing an error.
    """
    stats = result.stats
    matches_found = int(stats.get("matches", 0) or 0)
    teams_found = int(stats.get("teams", 0) or 0)
    roster_min = int(stats.get("roster_min", 0) or 0)
    roster_max = int(stats.get("roster_max", 0) or 0)
    span = (
        f"{roster_min} players"
        if roster_min == roster_max
        else f"{roster_min}-{roster_max} players"
    )

    lines: list[str] = []
    lines.append(
        f"Division fetched: {teams_found} teams, {matches_found} matches"
    )
    if result.reason:
        lines.append(_line("Note", result.reason))
    lines.append(
        _line(
            "Matches played",
            f"{int(stats.get('matches_played', 0) or 0)} / {matches_found}",
        )
    )
    lines.append(_line("Rosters", span))
    lines.extend(_discover_gaps(stats))

    team = stats.get("team")
    if isinstance(team, dict):
        lines.extend(_discover_team(team))
    else:
        lines.extend(_discover_division(stats))

    lines.append("")
    for path in result.outputs:
        lines.append(_line("Output", str(path)))
    lines.append(_line("Run time", _seconds(result.duration_s)))
    return "\n".join(lines)


def _discover_gaps(stats: dict) -> list[str]:
    """The lines about what was left out -- drops, disputes and transfers.

    Every one of these is a silent drop if it is not said out loud: the roster
    would simply be shorter or longer than it should be, and nothing would say
    why.
    """
    lines: list[str] = []
    without_id = int(stats.get("players_without_steam_id", 0) or 0)
    if without_id:
        dropped = stats.get("dropped_players") or []
        named = ", ".join(
            str(row.get("nickname") or row.get("player_id") or "?")
            for row in dropped[:MAX_LISTED_PLAYERS]
        )
        if len(dropped) > MAX_LISTED_PLAYERS:
            named += f" (+{len(dropped) - MAX_LISTED_PLAYERS} more)"
        lines.append(
            _line(
                "Without a SteamID64",
                f"{without_id} players were left out of the rosters: {named}",
            )
        )
    rows_without_id = int(stats.get("team_rows_without_id", 0) or 0)
    if rows_without_id:
        lines.append(
            _line(
                "Team rows without id",
                f"{rows_without_id} were skipped -- they cannot be attached "
                "to any team",
            )
        )
    transfers = stats.get("transfers") or []
    moved = [t for t in transfers if t.get("kind") == "released"]
    shared = [t for t in transfers if t.get("kind") == "shared"]
    if moved:
        lines.append(
            _line(
                "Transferred players",
                ", ".join(
                    f"{t.get('nickname') or t.get('game_player_id')} "
                    f"({t.get('from_team')})"
                    for t in moved[:MAX_LISTED_PLAYERS]
                ),
            )
        )
    if shared:
        lines.append(
            _line(
                "In two teams",
                ", ".join(
                    f"{t.get('nickname') or t.get('game_player_id')} "
                    f"({t.get('from_team')})"
                    for t in shared[:MAX_LISTED_PLAYERS]
                ),
            )
        )
    contested = stats.get("contested_lineup_keys") or []
    if contested:
        lines.append(
            _line(
                "Contested lineups",
                ", ".join(str(key) for key in contested)
                + " -- more than one team is over the threshold",
            )
        )
    return lines


def _discover_division(stats: dict) -> list[str]:
    """The division's teams as a listing, with their ids."""
    division = stats.get("division") or []
    if not division:
        return []
    lines = ["", "The division's teams:"]
    for team in division:
        name = str(team.get("name") or team.get("team_key") or "")
        lines.append(
            f"  {name} -- {int(team.get('roster_size', 0) or 0)} players, "
            f"id {team.get('team_key')}"
        )
    return lines


def _discover_team(team: dict) -> list[str]:
    """The rows of the team that was looked up."""
    lines = ["", f"Team: {team.get('name') or team.get('team_key') or ''}"]
    lines.append(_line("Id", str(team.get("team_key", ""))))
    faction_ids = [str(key) for key in team.get("faction_ids") or []]
    if len(faction_ids) > 1:
        lines.append(
            _line(
                "Source ids",
                ", ".join(faction_ids) + " -- the same team, different seasons",
            )
        )
    roster = [str(player) for player in team.get("roster") or []]
    lines.append(
        _line(
            "Standing roster",
            f"{len(roster)} players: {', '.join(roster)}"
            if roster
            else "no players at all",
        )
    )
    released = [str(player) for player in team.get("released") or []]
    if released:
        lines.append(
            _line("Transferred away", ", ".join(released))
        )
    shared = [str(player) for player in team.get("shared_players") or []]
    if shared:
        lines.append(
            _line(
                "Also in another team",
                f"{len(shared)} players -- the dispute was not resolved",
            )
        )
    lines.append(
        _line(
            "Matches",
            f"{int(team.get('matches', 0) or 0)} in all, played "
            f"{int(team.get('matches_played', 0) or 0)}",
        )
    )
    lineups = [str(key) for key in team.get("lineup_keys") or []]
    if lineups:
        lines.append(_line("Archive lineups", ", ".join(lineups)))
    alternatives = [str(other) for other in team.get("alternative_names") or []]
    if alternatives:
        lines.append(_line("Other observed names", ", ".join(alternatives)))
    return lines


@app.command("select")
def select(
    team: str = typer.Option(
        ...,
        "--team",
        help=(
            "The team's name, an unambiguous part of it, or the team id. "
            "Letter case does not matter. An ambiguous name lists the "
            "alternatives and chooses nothing."
        ),
    ),
) -> None:
    """Choose a team's maps with the roster threshold.

    The command reads the match index and the team index and writes
    index/selections/<team_key>.json, which holds one row per MapDemo: whether
    it qualifies for the sample, why, which roster class it is and whether it
    is a league match. The threshold is judged per map, because the league
    allows two substitutions between maps.

    A row comes into being only from a played match: an unplayed match has no
    maps, so no MapDemos exist.

    A map's lineup is a prediction from the match roster until the demo has
    been parsed -- after that it is an observation from the demo, and the row
    says which of the two it is. Run discover first if the indexes are not
    there.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    result = select_stage.run(
        settings.league,
        archive,
        team,
        thresholds=settings.thresholds,
    )
    typer.echo(_render_select(result))


def _render_select(result: StageResult) -> str:
    """Assemble the ``select`` command's summary.

    The most important line is the ratio of accepted to rejected: the user
    checks from it whether the sample is what they expected. Right after it
    come **the reasons for the rejections in full**, because a rejection
    without the numbers is a decision they cannot check without opening the
    file -- and they do not code.

    **The keys are read directly and not with a default.** ``stats`` is this
    stage's own contract with this function, not the user's data: a missing
    key means the stage and the output have drifted apart, and
    ``stats.get(name, 0)`` would turn that into a silent zero. A zero looks
    like a measured result. An exception is better -- and
    ``test_the_summary_renders_from_a_real_stage_result`` is the test that
    notices the difference before the user does.
    """
    stats = result.stats
    total = int(stats["map_demos"])
    accepted = int(stats["accepted"])

    lines: list[str] = []
    lines.append(
        f"Selection made: {stats['team_display']} -- "
        f"{accepted} / {total} maps into the sample"
    )
    # Every note on a line of its own: joined into one string, the second one
    # would disappear behind the first.
    for note in stats["notes"]:
        lines.append(_line("Note", str(note)))
    lines.append(_line("Id", str(stats["team_key"])))
    lines.append(
        _line(
            "Standing roster",
            f"{int(stats['roster_players'])} players, threshold "
            f"{stats['roster_threshold']}",
        )
    )
    lines.append(
        _line(
            "Matches",
            f"{int(stats['matches_with_maps'])} played with maps, "
            f"{int(stats['matches_not_played'])} not played, "
            f"{int(stats['matches_without_veto'])} without veto data "
            f"({int(stats['matches_seen'])} in all)",
        )
    )
    lines.extend(_select_classes(stats))
    lines.append(
        _line("League matches", f"{int(stats['league'])} of {total} maps")
    )
    lines.append(
        _line(
            "Lineup source",
            f"{int(stats['observed'])} observations from the demo, "
            f"{int(stats['predicted'])} predictions from the match roster",
        )
    )
    uncertain = int(stats["uncertain"])
    if uncertain:
        lines.append(
            _line(
                "Possibly not played",
                f"{uncertain} maps are in the veto data, but the match length "
                "does not guarantee that they were played",
            )
        )
    drifted = int(stats["drifted"])
    if drifted:
        lines.append(
            _line(
                "Substitution between maps",
                f"{drifted} of the maps had a lineup that differed from the "
                "match roster",
            )
        )
    lines.extend(_select_rejections(stats))

    lines.append("")
    for path in result.outputs:
        lines.append(_line("Output", str(path)))
    lines.append(_line("Run time", _seconds(result.duration_s)))
    return "\n".join(lines)


def _select_classes(stats: dict) -> list[str]:
    """The class breakdown, and **only over the accepted rows**.

    A rejected row has no class: a class would be a claim about rounds that
    are not counted. The sum is therefore always the same as the number of
    accepted rows.
    """
    parts = [f"{label}: {int(stats[f'class_{label}'])}" for label in ROSTER_CLASSES]
    return [_line("Roster classes", ", ".join(parts))]


def _select_rejections(stats: dict) -> list[str]:
    """The rejected maps with their reasons -- in full, not truncated.

    The reason is what the whole row exists for: a truncated reason would look
    like a justification without being one. The *listing*, on the other hand,
    **is** truncated (``MAX_LISTED_REJECTIONS``), and then the output says how
    many were not shown and where they can be found.
    """
    rows = stats["rejections"]
    total = int(stats["rejections_total"])
    if not total:
        return []
    lines = ["", f"Rejected maps ({total}):"]
    for row in rows:
        unit = str(row["map_demo_id"])
        name = row.get("map_name")
        # Without a name the id is already in the heading, and it is not
        # repeated in brackets.
        lines.append(f"  {name} ({unit})" if name else f"  {unit}")
        lines.append(f"    {row['roster_reason']}")
    hidden = total - len(rows)
    if hidden > 0:
        lines.append(
            f"  (+{hidden} more -- the whole listing with reasons is in the "
            "selection file)"
        )
    return lines


#: The message when nothing is downloaded because of the user's answer.
#:
#: One string and not two: answering no and not answering at all are the same
#: outcome, and two different wordings would suggest that they differ.
_CANCELLED = "Cancelled. No demos were downloaded."

#: The answers that are read as consent.
#:
#: ``y`` and ``yes`` are the ones this interface expects, and the finger
#: remembers them from every other tool. The five Finnish forms beside them
#: are what the tool asked for until 2026-09-09, and they stay accepted: a
#: season of muscle memory should not turn into a cancelled download. **The
#: no-answers are not listed**: anything other than consent is a no, because
#: a misread answer must never lead to a download.
_CONSENT_ANSWERS = frozenset({"k", "kylla", "kyllä", "y", "yes", "j", "joo"})


def _confirm(
    question: str,
    cancelled: str = _CANCELLED,
    *,
    before_cancelling: Callable[[], None] | None = None,
) -> None:
    """Ask for confirmation and stop cleanly if the answer is no.

    ``cancelled`` is the sentence printed for a negative answer. A parameter and
    not a constant, because the sentence says **what was left undone**: on a
    download "no demos were downloaded", on an import "the demo was not
    imported". A shared wording would be wrong for one of the two.

    ``before_cancelling`` is printed **before** that sentence and only on the
    way out. It exists for ``scout``, where the question sits in the middle
    of a chain: a caller who answers no has to be told what the run already
    did, and after the cancellation sentence is too late for something the
    reader stops at.

    ``typer.confirm`` will not do, and the reason survived the translation:
    its abort message is ``Aborted.`` and its exit code is not zero, so a user
    who answered the question would be told that something went wrong.

    A negative answer **is not an error**: the user was asked and answered.
    The exit code is therefore 0 and the message states what happened, rather
    than ``Aborted.``, which looks as if something had gone wrong.
    """

    def cancel() -> None:
        if before_cancelling is not None:
            before_cancelling()
        typer.echo(cancelled)

    try:
        answer = typer.prompt(f"{question} [y/n]", default="n", show_default=False)
    except (typer.Abort, EOFError):
        # **The same message as for a negative answer, and for the same
        # reason.** A command run without input (a pipe, a scheduler, Ctrl-C)
        # gets EOF, and then ``typer`` aborts with a message of **its own**,
        # ``Aborted.``, before the code below sees anything. That word claims
        # a failure where there was none -- it just arrives by another route.
        # Not answering is an answer too, and it is "no".
        typer.echo("")
        cancel()
        raise typer.Exit() from None
    if str(answer).strip().lower() not in _CONSENT_ANSWERS:
        cancel()
        raise typer.Exit()


@app.command("fetch")
def fetch(
    team: str = typer.Option(
        ...,
        "--team",
        help=(
            "The team's name, an unambiguous part of it, or the team id. "
            "Letter case does not matter. An ambiguous name lists the "
            "alternatives and chooses nothing."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Do not ask for confirmation. The plan is still shown.",
    ),
) -> None:
    """Download the demos chosen into the team's sample onto the disk.

    The command reads index/selections/<team_key>.json and fetches the maps
    whose roster_ok is true and which are not on the disk yet. Run select
    first if there is no selection file.

    The demos are written into the directory named by the
    PAPPASCOUT_DEMOS_ROOT environment variable, or into the archive's demos
    directory if that variable is not set. A demo that is already in either
    one is skipped -- so setting the variable does not download anything
    again.

    The download is safe to run again, and one demo failing does not stop the
    others. A demo that FACEIT no longer offers is marked with the status
    no_demo and a reason -- that is not an error but a fact, and it is not
    retried.

    Before the download it shows how many demos are fetched, where to and how
    much disk space they take, and asks for confirmation. A download spends
    FACEIT's Downloads quota and hundreds of megabytes of disk space, so the
    question is deliberate; --yes skips it.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)

    team_key = fetch_stage.resolve_team_key(archive, team)
    todo = fetch_stage.plan(archive, team_key)
    free = fetch_stage.free_space(archive)
    typer.echo(_render_fetch_plan(todo, free, str(archive.demos_dir())))

    if not todo.pending:
        return

    _disk_space_gate(str(archive.demos_dir()), free)

    if not yes:
        _confirm("Download these demos?")

    source = fetch_stage.default_source(settings, archive)
    results = fetch_stage.run_many(archive, todo.pending, source=source)
    typer.echo(_render_fetch(results, todo))


def _disk_space_gate(demos_dir: str, free: int | None) -> None:
    """Stop if not one demo would fit on the disk.

    **The gate comes before the question.** Asking for confirmation of a
    download that does not fit on the disk would be a question with no right
    answer. The stage checks the same thing again for every demo -- the space
    can run out part way through the series.

    Shared between ``fetch`` and ``collect`` rather than copied: two wordings
    for the same situation would be two different instructions, and at most
    one of them would stay up to date.

    The parameter is **the target directory's path and not an archive
    object**, and there are two reasons for that. The layering rule
    (``tests/test_layering.py``) forbids the ``archive`` package from the
    command line, so ``ArchivePaths`` cannot even be named here as a type. And
    this function needs only the one path from the archive that it prints --
    the same string the plan already showed, so the gate and the plan cannot
    name different directories.

    Args:
        demos_dir: The directory the demos would be written into.
        free: The free space in bytes, or ``None`` = it could not be found
            out.

    Raises:
        ~pappascout.errors.PappascoutError: If the free space is known and is
            not enough for one demo plus the reservation. Unknown space
            (``None``) does not stop the download.
    """
    need = fetch_stage.DEMO_SIZE_ESTIMATE_BYTES + fetch_stage.DISK_RESERVE_BYTES
    if free is not None and free < need:
        raise PappascoutError(
            "The disk has no room for a single demo: the "
            f"disk holding {demos_dir} has "
            f"{fetch_stage.size_fi(free)} free, and one demo plus the "
            f"reservation needs {fetch_stage.size_fi(need)}.\n"
            "Free some space by deleting demos that have been parsed (the "
            "parsed tables stay) or point the demos at another disk with the "
            "PAPPASCOUT_DEMOS_ROOT environment variable."
        )


def _fetch_failures(heading: str, results) -> list[str]:
    """One block of failures: **every row with its reason and its advice**.

    The advice is printed on a line of its own under the reason rather than in
    the heading, because two units that failed for two different reasons have
    two different pieces of advice -- and a shared heading can be right for at
    most one of them.
    """
    if not results:
        return []
    lines = ["", f"{heading} ({len(results)}):"]
    for result in results:
        lines.append(f"  {result.unit}")
        for row in str(result.reason or "").splitlines():
            lines.append(f"    {row}")
        step = str(result.stats.get("next_step", "")).strip()
        if step:
            lines.append(f"    -> {step}")
    return lines


def _fetch_notes(results) -> list[str]:
    """A successful download's notes -- **onto the screen, not only into the result**.

    ``reason`` was printed only inside :func:`_fetch_failures`' blocks, so a
    note on a ``status="ok"`` result was never seen. There are two of them and
    both are the user's information: ``fetch._unverified_note``, whose own
    documentation says the stage **"has to say it"**, and the outcome of
    removing an orphaned metadata file -- which includes the ``WARNING`` that
    says the removal did not succeed. A note that comes into being in the
    result but not on the screen is the same thing as silence.

    **Only the downloaded ones, not the skipped ones -- and the filter is
    here, not at the call site.** A skipped result's ``reason`` is "the demo
    was already on the disk", and that is already the count on the summary's
    first line -- a listing of the same sentence as long as the sample would
    drown exactly the rows this block exists for. The review of 2026-09-06
    pointed out that the rule was documented here but implemented at the call
    site: the next caller, who passed the whole ``results``, would get exactly
    the listing this line is written against. So the function filters for
    itself.
    """
    rows = [
        result
        for result in results
        if result.status == "ok"
        and not result.skipped
        and str(result.reason or "").strip()
    ]
    if not rows:
        return []
    lines = ["", f"Notes ({len(rows)}):"]
    for result in rows:
        lines.append(f"  {result.unit}")
        for row in str(result.reason).splitlines():
            lines.append(f"    {row}")
    return lines


def _render_fetch_plan(todo, free: int | None, demos_dir: str) -> str:
    """The plan **before** the download: how many, where to, how much space.

    The free space is there because the question cannot be answered without
    it: "12 demos, 2.6 Gt" is a different question on a disk with 100 GB free
    than on one with 3 GB. The target directory is there for the same reason
    -- the demos can go outside the archive, and the user should not have to
    open the settings file to see where.

    **The disk-space warning and the truncation of the listing are the same as
    the sister's** (:func:`_render_collect_plan`). Both were added in Story
    3.5 to ``collect`` only, but neither is about the division: a sample of 12
    demos fits on a 1 GB disk no better than one of 132, and the user
    confirms the same question here. Two different plan outputs for the same
    download were a difference, not a decision.

    The inflection of the noun comes from :func:`_maps` and not from a
    hard-coded "maps": a one-map sample is the normal state of the season's
    first run, and "1 maps" is a mistake on every line it appears on.
    """
    lines = [
        f"Sample: {_maps(todo.selected)}, "
        f"{_of_which(todo.selected)} {len(todo.present)} already on disk"
    ]
    if not todo.pending:
        lines.append(
            "Every demo in the sample is already on disk -- nothing to download."
        )
        return "\n".join(lines)
    lines.append(
        _line(
            "Demos",
            f"{len(todo.pending)} to download, an estimated "
            f"{fetch_stage.size_fi(todo.estimated_bytes)}",
        )
    )
    lines.append(_line("Target", demos_dir))
    if free is not None:
        lines.append(_line("Free disk space", fetch_stage.size_fi(free)))
        lines.extend(_space_warning(todo, free))
    lines.extend(_listed_units(todo.pending))
    return "\n".join(lines)


def _render_fetch(results, todo) -> str:
    """The summary of what was downloaded, skipped and failed.

    **Anything other than ok is listed with its reason and its advice.** The
    bare number "3 failed" would give the count but not what to do about it.

    The heading **states only what happened**, and advises nothing. It used to
    say "Failed (N) -- run the command again", and that was a bucket that
    collected both a passing glitch and a permanent fault: two consecutive
    live runs on 2026-09-05 found the same pattern, first with a 403 and then
    with a 400. **The advice belongs to the fault, not to the bucket** --
    otherwise every new class of fault inherits the wrong advice by default.
    Now every row carries its own ``next_step``, which comes from the error's
    own ``advice`` field.
    """
    downloaded = [r for r in results if r.status == "ok" and not r.skipped]
    skipped = [r for r in results if r.skipped]
    missing = [r for r in results if r.status == "no_demo"]
    failed = [r for r in results if r.status == "download_failed"]
    total_bytes = sum(int(r.stats.get("downloaded_bytes", 0)) for r in results)

    lines = [
        f"Download done: {len(downloaded)} fetched, "
        f"{len(skipped) + len(todo.present)} already on disk, "
        f"{len(missing)} not available, {len(failed)} failed"
    ]
    lines.append(_line("Written", fetch_stage.size_fi(total_bytes)))
    directories = sorted(
        {str(r.stats["demos_dir"]) for r in downloaded if "demos_dir" in r.stats}
    )
    for directory in directories:
        lines.append(_line("Target", directory))
    lines.extend(_fetch_notes(results))
    lines.extend(_fetch_failures("Not available", missing))
    lines.extend(_fetch_failures("Failed", failed))
    lines.append("")
    lines.append(_line("Run time", _seconds(sum(r.duration_s for r in results))))
    return "\n".join(lines)


@app.command("collect")
def collect(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Do not ask for confirmation. The plan is still shown.",
    ),
) -> None:
    """Collect the demos of the whole division's finished matches onto the disk.

    FACEIT removes a demo in about 30 days, and at the end of the season a
    match that has gone is gone for good. That is why this command looks at
    neither the roster threshold nor any selection file: the units come
    straight from the match index, and every played match is taken in -- the
    one that is not yet in anybody's sample too.

    The matches are read from index/matches.json. Run discover first if there
    is no index -- and run it again if the index age the plan reports is older
    than the most recently played matches. The command does not fetch matches
    itself and does not write to the index.

    The download is exactly the same as fetch's: the same write, the same
    metadata file, the same rules. A demo already on the disk is skipped in
    all three locations, so the command can be run again at any time.

    A played match whose map list is not in the index shows up on a row of its
    own with its reason. It is neither zero maps nor an unplayed match.

    Before the download it shows how many demos are fetched, where to and how
    much disk space they take, and asks for confirmation. --yes skips the
    question, not the printing of the plan.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)

    todo = fetch_stage.plan_division(archive, settings.league)
    free = fetch_stage.free_space(archive)
    typer.echo(_render_collect_plan(todo, free, str(archive.demos_dir())))

    if not todo.pending:
        return

    _disk_space_gate(str(archive.demos_dir()), free)

    if not yes:
        _confirm("Download these demos?")

    source = fetch_stage.default_source(settings, archive)
    results = fetch_stage.run_many(archive, todo.pending, source=source)
    typer.echo(_render_fetch(results, todo))


#: How many ids of things to download the plan lists on the screen.
#:
#: **A ceiling and not an arbitrary limit.** With ``fetch`` the listing was as
#: long as the sample (one team, a few maps), but ``collect`` gathers the
#: whole division: 12 demos now and about 132 at the end of the season. Over a
#: hundred rows of ids would scroll off the screen exactly the rows the plan
#: is printed for in the first place -- the target, the free space, the
#: matches without veto data and the question itself. The listing is extra
#: information; the numbers and the gates are not.
#:
#: Twenty is a screenful: it can be taken in at once, and it is enough to tell
#: whether the ids are from the right division's matches.
MAX_LISTED_UNITS = 20


def _render_collect_plan(
    todo: fetch_stage.CollectPlan, free: int | None, demos_dir: str
) -> str:
    """The division's plan **before** the download.

    :func:`_render_fetch_plan`'s sister, and four lines longer -- each of them
    answers a question that the per-team fetch does not have:

    **The index's age.** ``collect``'s whole set of units comes from the match
    index, so an old index means matches this run does not see at all.
    Measured 2026-09-06: the archive's index was from the 4th and the next
    matches started on the 6th at 17:00. The age is therefore said out loud
    and not left to be inferred.

    **An unknown match length.** ``best_of`` was missing from the whole index
    written on the 4th. The maps are then read from the veto data, and the row
    says so -- silently it would look the same as a known length. The row also
    says **how many** matches this concerns, because an observation without
    its scope cannot be checked.

    **Matches without veto data.** A block of their own with their reasons,
    not zero rows. Story 3.3's review found the same fault in the selection,
    where such a match was counted as unplayed.

    **The disk-space warning.** The plan's size and the free space are on the
    same screen, but comparing them is left to the user only if the tool does
    not do it. Measured 2026-09-06: the whole season is about 25 GB and 9.8 GB
    was free.

    Two empty plans are **different things**, and they are said in different
    words. "Everything already on disk" is a result; "not one map of the
    division is known" is a sign that the ``championship_ids`` setting and the
    index do not meet, or that the season has not been played yet. A shared
    message would, in the latter case, claim that something is on the disk
    that is not.
    """
    lines = [
        f"Division: {_matches_played(todo.matches_played)}, "
        f"{_maps(todo.selected)}, {_of_which(todo.selected)} "
        f"{len(todo.present)} already on disk"
    ]
    lines.append(
        _line("Match index", todo.index_generated_at or "time unknown")
    )
    if todo.best_of_unknown:
        lines.append(
            _line(
                "Match length",
                f"unknown in {len(todo.best_of_unknown)} of the matches "
                "(best_of is missing from the index) -- the maps are read "
                "from the veto data",
            )
        )
        # **An observation without advice leaves the user guessing.** The
        # field is not missing from the source but from **the old index**:
        # ``discover`` writes it (``discover._match_row``), so running again
        # fixes the row. Without this sentence the row looks like a fault
        # nothing can be done about.
        lines.append(
            "  The field is missing from the old index, not from the source "
            "-- a new run writes it:\n"
            "  uv run pappascout discover"
        )
    if todo.pending:
        lines.append(
            _line(
                "Demos",
                f"{len(todo.pending)} to download, an estimated "
                f"{fetch_stage.size_fi(todo.estimated_bytes)}",
            )
        )
        lines.append(_line("Target", demos_dir))
        if free is not None:
            lines.append(_line("Free disk space", fetch_stage.size_fi(free)))
            lines.extend(_space_warning(todo, free))
        lines.extend(_listed_units(todo.pending))
    elif todo.selected:
        lines.append(
            "Every demo in the division is already on disk -- nothing to download."
        )
    else:
        lines.append(_empty_division(todo))
    lines.extend(_collect_no_veto(todo.no_veto))
    return "\n".join(lines)


def _matches_played(count: int) -> str:
    """``1 match played`` / ``6 matches played``.

    The noun inflects at 1, and the same pattern is already in
    :func:`_rounds` and :func:`_players`. A one-match division is not a
    rarity: the season's first run lands on exactly that.
    """
    return f"{count} match played" if count == 1 else f"{count} matches played"


def _maps(count: int) -> str:
    """``1 map`` / ``12 maps``."""
    return f"{count} map" if count == 1 else f"{count} maps"


def _of_which(count: int) -> str:
    """``of which`` -- **and in English it does not inflect with the number**.

    In Finnish the relative pronoun took the same number as the noun before
    it, and that is why this helper exists at all rather than two conditions
    at the call sites: two separate conditions would drift apart, and Story
    3.7 is about exactly that drift. The review of 2026-09-06 found that
    adopting ``_maps`` fixed the numeral but left the next word alone --
    "1 kartta, **joista** 0 on jo levylla". A fix that moves the mistake one
    word later is not a fix.

    English has one form, so both branches now return the same word. **The
    branch is kept and the call sites are unchanged**: the sentence they build
    still depends on the count through :func:`_maps`, and this helper is
    where a future language with two forms would put them back.
    """
    return "of which" if count == 1 else "of which"


def _listed_units(units: tuple[str, ...]) -> list[str]:
    """The ids to download, **truncated** to :data:`MAX_LISTED_UNITS`.

    The truncation says how many were not shown, just as with
    :func:`_select_rejections`. A silent shortening would look like a plan
    shorter than its own "Downloading" line promises -- and that is the very
    number the user is confirming.
    """
    lines = [f"  {unit}" for unit in units[:MAX_LISTED_UNITS]]
    hidden = len(units) - MAX_LISTED_UNITS
    if hidden > 0:
        lines.append(
            f"  (+{hidden} more -- they are downloaded just like the ones "
            "listed above)"
        )
    return lines


def _space_warning(
    todo: fetch_stage.CollectPlan | fetch_stage.FetchPlan, free: int
) -> list[str]:
    """Warn if the whole plan does not fit on the disk -- **and do not stop the run**.

    **The same line for both plans.** The function reads only ``pending`` and
    ``estimated_bytes``, which are in both; that is why the name no longer
    says ``collect``. A warning that applied to only one of the two download
    commands would be a difference, not a decision.

    A partial collection is better than no collection: FACEIT removes a demo
    in about 30 days, and the part that is fetched in time is kept for good.
    The stage checks the space separately for every demo, so the run stops of
    its own accord at the right point and does not fill the disk.

    **The gate and the warning are different things.** :func:`_disk_space_gate`
    stops a run that could not fit a single demo -- there is then nothing the
    run could achieve. This line says that not everything fits, and leaves the
    decision to the user, who is about to be asked. Measured 2026-09-06: the
    season is about 132 demos, that is 25 GB, and 9.8 GB was free -- so this
    line is the normal state at the end of a season and not an exception.
    """
    if free >= todo.estimated_bytes:
        return []
    fits = max(0, free - fetch_stage.DISK_RESERVE_BYTES) // max(
        1, fetch_stage.DEMO_SIZE_ESTIMATE_BYTES
    )
    return [
        _line(
            "NOTE",
            f"the whole plan does not fit on the disk: there is room for an "
            f"estimated {min(fits, len(todo.pending))} of the "
            f"{len(todo.pending)} demos. The rest are left unfetched, and "
            "they can be fetched later by running the command again.",
        )
    ]


def _empty_division(todo: fetch_stage.CollectPlan) -> str:
    """Not one map of the division is known -- **not the same as "already on disk"**.

    A wrong claim about the data is worse than a hard-to-read one: "everything
    is already on disk" would say the demos are safe when in fact not one of
    them is known. The situation comes from a wrong or foreign
    ``championship_ids``, from another division's index, and from a season
    that has not been played yet -- and in the first two the user has to see
    exactly the id that was filtered with. That is why the message repeats it
    rather than merely telling them to check.
    """
    ids = ", ".join(todo.league_ids) or "(none at all)"
    return (
        "The match index holds no played match from this division at all "
        "-- nothing to download.\n"
        f"  Filtered with these championship_ids: {ids}\n"
        "  Check [league].championship_ids in the settings and, if need be, "
        "run again: uv run pappascout discover"
    )


def _collect_no_veto(rows: tuple[fetch_stage.NoVetoMatch, ...]) -> list[str]:
    """Played matches with no map list -- **each with its reason and its advice**.

    The block exists so that a played match does not disappear quietly. Zero
    rows would look exactly like a match that was not played.

    **The advice branches on ``finished_at``**, and that is exactly what the
    field is for. On a fresh match the veto is missing from the index because
    the index is older than the match -- ``discover`` fixes that. On a match
    weeks old, ``discover`` has already been run since the match and there is
    still no veto: running it again then produces nothing, and the remaining
    route is a demo fetched with a browser and imported by hand (``import``).
    A shared piece of advice would be right for at most one of the two -- the
    same rule as :func:`_fetch_failures`' (D1).
    """
    if not rows:
        return []
    lines = ["", f"Played match with no veto data ({len(rows)}):"]
    for row in rows:
        when = f" (ended {row.finished_at})" if row.finished_at else ""
        lines.append(f"  {row.match_id}{when}")
        for text in row.reason.splitlines():
            lines.append(f"    {text}")
        lines.append(f"    -> {_no_veto_next_step(row)}")
    return lines


#: How old a match has to be for ``discover`` to stop bringing it a veto.
#:
#: Measured 2026-09-06: the archive's index was two days old and held every
#: match older than itself with its veto. The limit is therefore comfortably
#: above the normal delay: a match past it has been in the index for several
#: runs already without the veto appearing.
NO_VETO_STALE_DAYS = 7


def _no_veto_next_step(row: fetch_stage.NoVetoMatch) -> str:
    """The advice for one match without veto data, according to its age."""
    if _is_older_than(row.finished_at, NO_VETO_STALE_DAYS) is not True:
        # A fresh **or unknown** age. The index may simply be older than the
        # match, and that is the cheapest fix to try first -- and an unknown
        # age must not be given advice that claims the match is old.
        return "Run again: uv run pappascout discover"
    return (
        f"The match is over {NO_VETO_STALE_DAYS} days old and no veto data "
        "has appeared, so discover probably will not bring it. Fetch the "
        "demos with a browser and import them: uv run pappascout import"
    )


def _is_older_than(moment: str | None, days: int) -> bool | None:
    """Is an ISO timestamp over ``days`` days old? ``None`` = it is not known.

    **Three return values and not two.** A missing or unparseable timestamp is
    neither "fresh" nor "old", and presenting it as either would choose the
    advice with information that does not exist. An unknown age gets the same
    advice as a fresh one, but the choice is made visibly at the call site and
    not hidden in here.
    """
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A timestamp without a zone is read as UTC, because the index's other
        # times are. Read as local time it would be off by hours at most, and
        # it cannot turn the seven-day limit the wrong way round.
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed) > timedelta(days=days)


#: The message when nothing is imported because of the user's answer.
#:
#: A sentence of its own rather than :data:`_CANCELLED`, because a cancellation
#: says what was left undone -- and this command downloads nothing, so "no
#: demos were downloaded" would answer a question that was not asked. The
#: latter half is the same promise as the rejections make: the source file is
#: not touched.
_CANCELLED_IMPORT = (
    "Cancelled. The demo was not imported, and the source file was not touched."
)


@app.command("import")
def import_demo(
    match: str = typer.Option(
        ...,
        "--match",
        help=(
            "The match's FACEIT id, for example 1-<uuid>. The ids are in the "
            "archive's index/matches.json file."
        ),
    ),
    map_no: str = typer.Option(
        ...,
        "--map",
        help=(
            "The map's number in the match, 1-based: the first map is 1. In "
            "the archive's id the same map is 0."
        ),
    ),
    file: str | None = typer.Option(
        None,
        "--file",
        help=(
            "The file to import, named explicitly. Without this the file is "
            "looked for in the archive's import folder under FACEIT's own "
            "name. A file outside the import folder is copied, not moved."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help=(
            "Do not ask for confirmation. Does NOT skip the map check's "
            "question: a demo saved under the wrong name would quietly spoil "
            "the report."
        ),
    ),
) -> None:
    """Import a demo downloaded with a browser into the archive.

    The command moves the file to the name demos/<map_demo_id>.dem.zst and
    writes a .meta.json file beside it with the entry source = import. An
    imported demo is indistinguishable from a downloaded one in the pipeline:
    run parse on it directly.

    The file's extension is decided from the content and not from the name
    given, so an uncompressed .dem does not end up in the archive named
    .dem.zst.

    The map's name is read from the demo's own header and compared against the
    FACEIT match's veto data. A mismatch -- and the fact that the comparison
    could not be made at all -- is a confirmation question that --yes does
    NOT skip. It is this tool's only question that the flag does not silence.

    The command does not download demos: the demo is the file you give it. The
    match's veto data, on the other hand, comes from FACEIT -- primarily from
    the response cache (raw/faceit/), and if the match is not there it is
    fetched from the interface and the response is written into the cache. No
    Downloads token is needed.
    """
    # **--map is a string and not an integer, and that is deliberate.**
    # Typer's own int conversion would fail with a message of its own before
    # the stage saw the value at all, and the stage's own check would be code
    # unreachable from the command line. The conversion and its error belong
    # in the same place as the number's other rules.
    settings = load_settings()
    archive = archive_paths(settings.project)

    # A compressed demo is decompressed in full to read the header, and that
    # takes seconds. Without this line the user looks at a blank screen and
    # does not know whether anything started.
    typer.echo("Reading the demo's header to find out the map name...", err=True)

    todo = import_stage.plan(
        archive,
        match,
        map_no,
        source=import_stage.default_source(settings, archive),
        parser=import_stage.default_parser(),
        file=file,
    )
    typer.echo(_render_import_plan(todo))

    for confirmation in import_stage.unanswered(todo.confirmations, yes=yes):
        typer.echo("")
        typer.echo(confirmation.detail)
        _confirm(confirmation.question, _CANCELLED_IMPORT)

    result = import_stage.run(archive, todo)
    typer.echo(_render_import(result))


def _render_import_plan(todo) -> str:
    """What the import intends to do -- **before it does it**.

    Both map observations are on rows of their own even when they agree.
    Showing only the mismatch would mean the user never sees a successful
    check -- and so does not know that it happens.
    """
    lines = [
        _line("Importing", todo.map_demo_id),
        _line("Source", str(todo.source_path)),
        _line("Target", str(todo.target_path)),
        _line("Size", fetch_stage.size_fi(todo.size_bytes)),
        _line("Method", "move" if todo.move else "copy (the source stays put)"),
        _line("Map from the header", todo.header_map_name or "(no name)"),
        _line("Map from the veto", todo.expected_map_name or "(no veto data)"),
        _line(
            "Map check",
            "matches" if todo.map_matches else "DOES NOT MATCH -- asked below",
        ),
        # **Completeness on a row of its own, in both directions.** A row that
        # appears only on a failure says nothing about a successful check --
        # and then the user does not know that it happens.
        _line("Completeness", _integrity_text(todo)),
    ]
    return "\n".join(lines)


def _integrity_text(todo) -> str:
    """Whether the source could be found complete, and against what.

    **Uncertainty is said out loud on the same row as certainty.** Measured
    2026-09-05: a ``.dem.zst`` truncated half way decompresses quietly and the
    right map name is read from its header, so the shortfall is not visible
    anywhere. A compressed file is checked against the size the frame declares;
    an uncompressed ``.dem`` has nothing to check against, and that is the
    user's information and not an implementation detail.
    """
    if todo.declared_bytes is not None:
        return (
            "checked -- decompressed "
            f"{fetch_stage.size_fi(todo.declared_bytes)} "
            "as the frame declares"
        )
    if todo.length_verified:
        return "checked -- the gzip stream's end marker"
    return (
        "COULD NOT BE CHECKED -- an uncompressed demo carries no length, so a "
        "short file would come to light only during parsing"
    )


def _render_import(result: StageResult) -> str:
    """What the import did: the files, the digest and every note on its own row.

    The digest is printed because it is the number an imported demo is
    identified by later -- ``parse`` reads it from the metadata file and does
    not compute it again, so this is the only time it is seen.
    """
    stats = result.stats
    lines = [
        f"Import done: {result.unit}",
        _line("Demo", str(stats.get("demo_path", ""))),
        _line("Metadata", str(stats.get("meta_path", ""))),
        _line("Size", fetch_stage.size_fi(int(stats.get("size", 0)))),
        _line("sha256", str(stats.get("sha256", ""))),
        # **"Source entry" and not "Source".** In the plan "Source" is the
        # file that was imported from; here it is the value of the metadata
        # file's ``source`` field. The same word for two different things in
        # the same output is misreadable -- and it also masked the test that
        # tried to check the two rows separately.
        _line("Source entry", str(stats.get("demo_source", ""))),
    ]
    for note in stats.get("notes", ()):
        for row in str(note).splitlines():
            lines.append(f"  {row}")
    lines.append("")
    lines.append(_line("Run time", _seconds(result.duration_s)))
    return "\n".join(lines)


@app.command("parse")
def parse(
    target: str = typer.Argument(
        ...,
        metavar="FILE|MAP_DEMO_ID",
        help=(
            "The path of a demo file, or a map_demo_id, in which case the "
            "demo is looked for in the archive's demos and import directories."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Parse even if the manifest matches. Use this if you suspect the "
            "archive's result is out of date."
        ),
    ),
) -> None:
    """Parse a demo into seven tables.

    Writes the ``parsed/<map_demo_id>/rounds.parquet``, ``ticks.parquet``,
    ``events.parquet``, ``lineups.parquet``, ``deaths.parquet``,
    ``callouts.parquet`` and ``match.parquet`` tables and their manifest.
    If the manifest matches, the stage is skipped and the demo is not read
    again.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    map_demo_id, demo_path = parse_stage.resolve_demo(archive, target)

    # A 233 MB demo takes seconds, a compressed one more. Without this line
    # the user looks at a blank screen and does not know whether anything
    # started.
    typer.echo(f"Parsing {map_demo_id} ({demo_path.name})...", err=True)

    result = parse_stage.run(
        settings.parse,
        archive,
        map_demo_id,
        parse_stage.default_parser(settings.parse),
        demo_path=demo_path,
        force=force,
    )
    typer.echo(_render_parse(result, regulation_rounds=2 * settings.league.mr))


#: The output's column width, so that the values line up under the labels. The
#: longest label is "Partial sample points" (21 characters), and a value needs
#: at least one space in front of it.
_PARSE_LABEL_WIDTH = 22


def _line(label: str, value: str) -> str:
    """Format one output row with a fixed-width label column."""
    return f"  {label:<{_PARSE_LABEL_WIDTH}}{value}"


def _render_parse(result: StageResult, regulation_rounds: int) -> str:
    """Assemble the ``parse`` command's output.

    Split into a function of its own so that the output can be tested without
    running the command line.

    Args:
        result: The result the stage returned.
        regulation_rounds: The number of regulation rounds (MR12 -> 24). This
            is used **only** in the output's mention of overtime; the stage
            itself sees neither the league nor the threshold settings (AD-3).
    """
    stats = result.stats
    lines: list[str] = []

    lines.append(f"{'Skipped' if result.skipped else 'Parsed'}: {result.unit}")

    # AD-9: the status is always shown when it is not ok, so that a failed
    # unit does not look like a successful one.
    if result.status != "ok":
        lines.append(_line("Status", str(result.status)))
    if result.reason:
        lines.append(_line("Reason", result.reason))

    if "unreadable" in stats:
        lines.append(_line("Rounds", f"no counts obtained ({stats['unreadable']})"))
        lines.append(_line("Run time", _seconds(result.duration_s)))
        return "\n".join(lines)

    rounds = int(stats.get("rounds", 0) or 0)
    lines.append(
        _line("Rounds", f"{rounds} ({int(stats.get('rows', 0) or 0)} rows)")
    )

    max_round = int(stats.get("max_round_no", 0) or 0)
    if max_round > regulation_rounds:
        lines.append(
            _line(
                "Overtime",
                f"yes -- {max_round} rounds played, regulation is "
                f"{regulation_rounds}",
            )
        )
    else:
        lines.append(_line("Overtime", f"no ({max_round}/{regulation_rounds})"))

    # A skipped run has no count: unnumbered rounds are not in the table, so
    # their number cannot be read out of a finished result.
    skipped_rounds = int(stats.get("skipped_rounds", 0) or 0)
    if not result.skipped and skipped_rounds:
        lines.append(
            _line(
                "Skipped rounds",
                f"{skipped_rounds} (warmup and the knife round)",
            )
        )

    lines.extend(_match_restarts(stats))

    no_anchor = int(stats.get("no_freeze_end", 0) or 0)
    if no_anchor:
        lines.append(
            _line(
                "Without an anchor",
                f"{no_anchor} (the freezetime tick is missing, the round is "
                "still included)",
            )
        )

    lines.extend(_buy_window(stats))
    lines.extend(_armed_players(stats))
    lines.extend(_armored_players(stats))
    lines.extend(_sample_points(stats, rounds))
    lines.extend(_utility(stats, rounds))
    lines.extend(_callouts(stats))
    lines.extend(_map_name(stats))
    lines.extend(_deaths(stats, rounds))
    lines.extend(_lineups(stats))

    if "tick_rate" in stats and not stats.get("tick_rate_measured", True):
        lines.append(
            _line(
                "Tickrate",
                f"{stats['tick_rate']:g} (a default -- it could not be "
                "measured from the demo)",
            )
        )

    for path in result.outputs:
        lines.append(_line("Output", str(path)))
    if result.manifest_path is not None:
        lines.append(_line("Manifest", str(result.manifest_path)))
    lines.append(_line("Run time", _seconds(result.duration_s)))

    return "\n".join(lines)


#: How many round numbers are printed when purchases were left behind the cut.
#: The cut hits about half of the rounds, so at worst the list would be as long
#: as the whole match -- and that is exactly when the user should see at a
#: glance that this is a systematic fault and not one round.
_MAX_LOST_PURCHASE_ROUNDS = 10


def _buy_window(stats: dict) -> list[str]:
    """The buy window's rows for the ``parse`` output.

    Three things that have to be said out loud:

    **Which moment the numbers were read from.** The economy is measured at
    the end of the buy time and not at the end of the freezetime, and the
    moment of measurement is a setting. Without the row, two results run with
    two different settings would look the same. The row also gives the real
    spread of the moments of measurement: the setting promises the window's
    length, but a death cutting it short often drags the median below that.

    **Whether the death cut cost anything.** A death cuts the window short in
    about half of the rounds -- that is the normal path and not an exception,
    so the number of cuts alone is not an alarm. The alarm is
    ``buy_window_purchases_after_cut``: if somebody bought after the cut, the
    measurement lost a purchase. It ought to be zero, and **the zero is said
    out loud** -- an unspoken zero would not stand apart from an unspoken
    five. A zero has two different causes, so the cuts that went unchecked are
    said separately.

    **What went wrong at the measurement point.** An empty tick, players lost,
    an entirely empty team row and the stale equipment value a refund leaves
    behind each get a row only when the number is non-zero: they are faults
    and not the normal state, so repeating a zero would drown them.

    On a skipped run the rows are left out: the measurement point is in the
    tables (``buy_end_tick``), but the number of cuts and of lost purchases
    cannot be read out of a finished result.
    """
    if "buy_window_truncated_by_death" not in stats:
        return []

    lines: list[str] = [_line("Measurement point", _measurement_point(stats))]

    truncated = int(stats.get("buy_window_truncated_by_death", 0) or 0)
    missed = int(stats.get("buy_window_purchases_after_cut", 0) or 0)
    unchecked = int(stats.get("buy_window_cuts_unchecked", 0) or 0)

    if truncated:
        cut = (
            f"{_rounds(truncated)} measured earlier, because the round's "
            "first death cut the window short"
        )
    else:
        cut = "none at all -- the window ran to its end in every round"
    if missed:
        cut += f"; {_players(missed)} bought after the cut"
        rounds = tuple(stats.get("buy_window_rounds_with_lost_purchases") or ())
        if rounds:
            shown = rounds[:_MAX_LOST_PURCHASE_ROUNDS]
            listed = ", ".join(str(number) for number in shown)
            if len(rounds) > len(shown):
                listed += f" (+{len(rounds) - len(shown)} more)"
            cut += f" -- rounds (round_raw) {listed}"
        else:
            cut += " -- purchases were left behind the measurement"
    else:
        cut += "; not one purchase was left behind the cut"
    if unchecked:
        cut += (
            f"; {_rounds(unchecked)} could not be checked at all "
            "(no players were obtained from the window's last tick)"
        )
    lines.append(_line("Cut short by a death", cut))

    lines.extend(_buy_window_faults(stats))
    return lines


def _measurement_point(stats: dict) -> str:
    """The ``Measurement point`` row's text: which moment the economy was read from.

    Three states have to be kept apart. ``buy_window_seconds`` is missing or
    ``None`` when the port does not report the window -- the moment of
    measurement is then unknown and must not be claimed. Zero means the
    anchor, and it is said as the end of the freezetime: "the end of the buy
    time, window 0.0 s" would be true but misleading, because a measurement by
    exactly that name was this story's fault. A positive value is the end of
    the buy time.
    """
    window = stats.get("buy_window_seconds")
    if window is None:
        return "not known (the demo port does not report the buy window's length)"

    window = float(window)
    if not window:
        return "economy read at the end of the freezetime (window 0,0 s)"

    text = f"economy read at the end of the buy time (window {_seconds(window)}"
    # The window is computed from the tickrate. If that could not be measured,
    # the seconds rest on a default too -- and a row that prints the seconds
    # with equal confidence in both cases would claim a measurement.
    if "tick_rate" in stats and not stats.get("tick_rate_measured", True):
        text += ", tickrate a default"
    text += ")"

    offsets = stats.get("buy_end_offsets_s")
    if offsets:
        low, middle, high = (float(value) for value in offsets)
        text += (
            f"; measured {_seconds(low)}-{_seconds(high)} from the anchor, "
            f"median {_seconds(middle)}"
        )
    return text


def _buy_window_faults(stats: dict) -> list[str]:
    """The measurement point's faults as rows of their own -- only when non-zero.

    All four of these are **faults and not observations**, unlike the death
    cut. Repeating a zero on every run would teach the reader to skip them.
    """
    lines: list[str] = []
    empty = int(stats.get("buy_window_ticks_without_players", 0) or 0)
    if empty:
        lines.append(
            _line(
                "Buy-end tick empty",
                f"{_rounds(empty)} -- no players were obtained from the "
                "tick, the measurement fell back to the freezetime anchor",
            )
        )

    lost = int(stats.get("buy_window_players_lost", 0) or 0)
    sides = int(stats.get("buy_window_sides_without_rows", 0) or 0)
    if lost:
        text = (
            f"{_players(lost)} could be read at the anchor but no longer "
            "at the measurement point"
        )
        if sides:
            text += (
                f"; {sides} of the team rows ended up entirely empty, and "
                "they are not classified"
            )
        lines.append(_line("Players lost", text))

    stale = int(stats.get("buy_window_stale_equipment", 0) or 0)
    if stale:
        lines.append(
            _line(
                "Stale value",
                f"{_players(stale)}: the value rose without a purchase -- "
                "the trace of a refund, at most $1000 per player",
            )
        )
    return lines


def _rounds(count: int) -> str:
    """``1 round`` / ``13 rounds`` -- the noun inflects at 1."""
    return f"{count} round" if count == 1 else f"{count} rounds"


def _players(count: int) -> str:
    """``1 player`` / ``2 players``.

    One lost purchase is exactly the case the row is reporting, so "1 players"
    would be wrong at the very moment the row matters most.
    """
    return f"{count} player" if count == 1 else f"{count} players"


#: The rule by which ``players_armed_buy_end`` is counted. Always printed with
#: the distribution: without it the numbers cannot be interpreted, and the
#: whole point of the row is to be a self-check during a run.
_ARMED_RULE = "armour and a weapon held at the end of the buy time"

#: The rule by which ``players_armored_buy_end`` is counted. The same
#: justification as above, and one more: two rows with nearly the same name in
#: succession are read wrongly without the rule after each of them. **Held and
#: not bought**, as in the one above -- armour survives the round for a player
#: who lived through it.
_ARMORED_RULE = "armour held at the end of the buy time, regardless of weapon"

#: How many unknown names are printed at most. If demoparser2 changes the way
#: it names things, **every** name is unknown: without the truncation the row
#: would be hundreds of names long at exactly the moment the user should see
#: at a glance that something is badly wrong.
_MAX_UNKNOWN_ITEMS = 20


def _player_counter_line(
    label: str, rule: str, distribution: dict | None, missing: int
) -> list[str]:
    """One player counter's value distribution as a single output row.

    Shared between the armed and the armoured counters for the same reason as
    ``stages.parse``'s ``_column_distribution``: the **difference** between the
    rows is what the reader reads out of the output, and two copies would
    before long lay them out differently -- one reporting missing observations
    and the other keeping quiet.

    **A distribution and not the extremes**: 41 rows of zero and a single five
    would give ``0-5``, which looks healthy, but ``0 -> 41, 5 -> 1`` does not.

    Args:
        label: The row's label.
        rule: The rule by which the number was counted. Always included: two
            rows with nearly the same name in succession are read wrongly
            without it.
        distribution: Value -> number of rows, or ``None`` if there is no
            count (a skipped run with an old port). The row is then left out.
        missing: The rows that have no observation.

    Returns:
        Zero or one rows.
    """
    if distribution is None:
        return []
    prefix = f"{rule}; "
    if not distribution:
        return [_line(label, f"{prefix}no observations at all ({missing} rows)")]
    spread = ", ".join(
        f"{value} -> {rows} rows" for value, rows in sorted(distribution.items())
    )
    text = f"{prefix}{spread}"
    if missing:
        text += f"; the observation is missing from {missing} rows"
    return [_line(label, text)]


def _armed_players(stats: dict) -> list[str]:
    """The equipment counter's value distribution for the ``parse`` output.

    The counter is an observation that can be checked only by looking: a wrong
    rule would produce a table that passes every schema check.

    Unknown inventory names get a row of their own. They arm nobody (the
    classification is a list of allowed weapons), so without the row a new
    weapon and a new knife skin would look exactly alike: the distribution
    would simply drift quietly downwards.
    """
    return _player_counter_line(
        "Armed",
        _ARMED_RULE,
        stats.get("armed_distribution"),
        int(stats.get("armed_missing", 0) or 0),
    ) + _armed_unknown_items(stats)


def _armored_players(stats: dict) -> list[str]:
    """The armour counter's value distribution for the ``parse`` output.

    A row of its own beneath the armed row, not a continuation of it. They are
    different observations and differ most on a pistol round, where the armed
    count is in practice 0: it is exactly that difference the target analysis'
    *"5 kevlars"* is read from. A combined row would hide the difference the
    two counters exist for.

    The row also works as a self-check during a run: if the distribution is
    identical to the armed distribution, the armour counter is reading the
    wrong condition -- and that shows here rather than only in the report.

    Unknown items are not printed here: the armour counter does not read the
    inventory, so item names cannot affect it.
    """
    return _player_counter_line(
        "Armoured",
        _ARMORED_RULE,
        stats.get("armored_distribution"),
        int(stats.get("armored_missing", 0) or 0),
    )


def _match_restarts(stats: dict) -> list[str]:
    """The match restarts for the ``parse`` output.

    A restart is not a round: it does not reach the table at all, so it **is
    not included** in the count of skipped rounds. A row of its own, then --
    otherwise two rows would count the same thing or the drop would stay
    silent.

    Three states are kept apart, as in :func:`_armed_unknown_items`:

    ``match_restarts`` is missing
        A skipped run. A restart is not in the tables, so its number cannot be
        read out of a finished result. The row is left out entirely.
    the value is ``None``
        A fresh run with a port that does not report restarts. "None at all"
        would be a claim nothing supports.
    the value is a number
        A fresh run where the port gave the number. A zero is said out loud
        too.
    """
    if "match_restarts" not in stats:
        return []
    value = stats.get("match_restarts")
    if value is None:
        return [
            _line(
                "Match restarts",
                "not known (the demo port does not report restarts)",
            )
        ]
    count = int(value)
    if not count:
        return [_line("Match restarts", "none at all")]
    # The noun inflects: "1 round boundary", "2 round boundaries".
    boundaries = "round boundary" if count == 1 else "round boundaries"
    return [
        _line(
            "Match restarts",
            f"{count} {boundaries} with no number of the demo's own "
            "-- not a round, not a row in the table",
        )
    ]


def _armed_unknown_items(stats: dict) -> list[str]:
    """The inventory names the weapon classification does not recognise.

    Three different states that have to be kept apart:

    ``armed_unknown_items`` is missing
        A skipped run. The names are not in the table -- they arm nobody --
        so they cannot be read back. The row is left out entirely.
    the value is ``None``
        A fresh run with a port that does not report unknowns. The row says so
        out loud: "none at all" would be a claim nothing supports.
    the value is empty
        A fresh run in which every name was recognised. That is a healthy
        result, and it is said out loud.

    The number of occurrences is printed after the name (``New Weapon x3``):
    it tells one exotic knife apart from a demoparser2 naming change, which
    hits every row.
    """
    if "armed_unknown_items" not in stats:
        return []
    items = stats.get("armed_unknown_items")
    if items is None:
        return [
            _line(
                "Unknown items",
                "not known (the demo port does not report unknown names)",
            )
        ]
    items = tuple(items)
    if not items:
        return [_line("Unknown items", "none at all")]

    shown = items[:_MAX_UNKNOWN_ITEMS]
    listed = ", ".join(_unknown_item(item) for item in shown)
    hidden = len(items) - len(shown)
    if hidden:
        listed += f" (+{hidden} more)"
    count = (
        "1 item name" if len(items) == 1 else f"{len(items)} different item names"
    )
    return [
        _line(
            "Unknown items",
            f"{count}: {listed} "
            "(not counted as a weapon -- add a recognised weapon to "
            "constants.py)",
        )
    ]


def _unknown_item(item: object) -> str:
    """Format one unknown name with its number of occurrences."""
    if isinstance(item, (tuple, list)) and len(item) == 2:
        name, seen = item
        return f"{name} x{int(seen)}"
    return str(item)


def _sample_points(stats: dict, rounds: int) -> list[str]:
    """The sample point and first contact rows for the ``parse`` output.

    Without these the user cannot see whether any positional data came into
    being at all: the round count would look the same when ``ticks.parquet``
    is empty. A zero is therefore as important to report as a large number,
    and it is said out loud.
    """
    if "ticks_unreadable" in stats:
        return [
            _line(
                "Sample points",
                f"no counts obtained ({stats['ticks_unreadable']})",
            )
        ]
    if "tick_rows" not in stats:
        return []

    lines: list[str] = []
    points = int(stats.get("sample_points", 0) or 0)
    tick_rows = int(stats.get("tick_rows", 0) or 0)
    sampled_rounds = int(stats.get("sample_rounds", 0) or 0)

    if points:
        lines.append(
            _line(
                "Sample points",
                f"{points} (in {sampled_rounds}/{rounds} rounds, "
                f"{tick_rows} rows)",
            )
        )
    else:
        lines.append(
            _line(
                "Sample points",
                "0 -- no positional data came into being",
            )
        )

    # A round with no sample point at all can have four causes: the anchor is
    # missing, the round was decided before the first sample point, the sample
    # point times are wrong, or every player row was without a pawn (Story
    # 2.10) -- the last one has a row of its own further down. The difference
    # is reported and the cause is not guessed; the fourth is mentioned only
    # when it has been measured, so that the explanation does not list a cause
    # this run did not have.
    without_samples = rounds - sampled_rounds
    if without_samples > 0:
        reason = (
            "the anchor is missing or the round was decided before the first "
            "sample point"
        )
        if int(stats.get("sample_points_without_pawn") or 0):
            reason += "; see also Player without a pawn"
        lines.append(
            _line("No sample point", f"{without_samples} rounds ({reason})")
        )

    contacts = int(stats.get("first_contact_rounds", 0) or 0)
    if contacts:
        lines.append(_line("First contacts", f"{contacts}/{rounds} rounds"))
    else:
        lines.append(
            _line(
                "First contacts",
                "0 -- not one round yielded a cross-side hit",
            )
        )

    # The adapter's own observations: these cannot be computed from a finished
    # table.
    #
    # Two rows that report partly the same event: a player without a pawn is
    # **one cause** of a partial sample point. They still do not merge into
    # one number -- there are partial points for other reasons too, and there
    # are pawnless rows on throw ticks as well, which are not sample points at
    # all. The connection is therefore said out loud rather than leaving the
    # reader to count the same event twice.
    without_pawn = int(stats.get("sample_rows_without_pawn") or 0)
    partial = int(stats.get("partial_samples", 0) or 0)
    if partial:
        cause = " -- the pawnless rows below are one cause" if without_pawn else ""
        lines.append(
            _line(
                "Partial sample points",
                f"{partial} (fewer players than at a full point{cause})",
            )
        )
    lines.extend(_pawnless(stats))
    unknown = int(stats.get("unknown_side_events", 0) or 0)
    if unknown:
        lines.append(
            _line(
                "Side unknown",
                f"{unknown} damage events were skipped while looking for the "
                "first contact",
            )
        )
    return lines


def _pawnless(stats: dict) -> list[str]:
    """The pawnless rows for the ``parse`` output (Story 2.10).

    A player without a pawn is **an observation and not a fault**: their
    controller is there but their character is not on the map, so the row is
    skipped like a spectator's. It still shrinks that round's setup, and
    without a row of its own the round would look as if the team had simply
    played a man down.

    Three states are kept apart, as in :func:`_match_restarts`, and here the
    difference is the whole reason the row exists:

    the key is missing
        A skipped run. The row is not in the table and the point is not among
        its sample points, so the number cannot be read out of a finished
        result. The row is left out entirely.
    the value is ``None``
        A fresh run with a port that does not report pawnless rows. The row
        says so out loud -- "none at all" would be a claim nothing supports.
    the value is zero
        A fresh run in which every player had a character. A healthy result,
        and the row is left out as with the other anomaly counters.

    **A sample point missed entirely** is appended to the same row rather than
    getting one of its own: it is a graver form of the same phenomenon, and
    separate rows would invite reading them as two different events.
    """
    if "sample_rows_without_pawn" not in stats:
        return []
    rows = stats.get("sample_rows_without_pawn")
    if rows is None:
        return [
            _line(
                "Player without a pawn",
                "not known (the demo port does not report pawnless rows)",
            )
        ]
    count = int(rows)
    if not count:
        return []
    value = (
        f"{count} rows were skipped (the controller is there, the character "
        "is not on the map)"
    )
    dropped = int(stats.get("sample_points_without_pawn") or 0)
    if dropped:
        value += f"; {dropped} of the sample points went missing entirely"
    return [_line("Player without a pawn", value)]


def _deaths(stats: dict, rounds: int) -> list[str]:
    """The deaths table's rows for the ``parse`` output (Story 2.7).

    Three questions the output answers:

    **Whether any data came into being.** The row count and how many rounds
    had a death in them. The latter is there because the row count alone would
    not reveal it if every death piled up in a single round.

    **Whether anything went missing.** Unnumbered rounds (the knife round), a
    death with no tick, deaths outside the bounds, an event with no victim and
    a victim with no side are different causes and must not be lumped into
    one. The first is **expected** in a league demo -- players die on the
    knife round there -- and the rest are zeros in the target state.

    **Whether the observation is intact.** A death with no attacker is an
    observation (a fall, the bomb), a missing area is not. They are therefore
    on different rows: a shared number would look like an area fault that does
    not exist.
    """
    if "deaths_unreadable" in stats:
        return [
            _line("Deaths", f"no counts obtained ({stats['deaths_unreadable']})")
        ]
    if "death_rows" not in stats:
        return []

    rows = int(stats["death_rows"])
    death_rounds = int(stats.get("death_rounds", 0) or 0)
    lines = [_line("Deaths", f"{rows} (in {death_rounds}/{rounds} rounds)")]

    # The knife round's deaths: an expected number, not a fault. Without it
    # the drop would be silent -- and that drop is this table's only
    # knife-round rule.
    unnumbered = int(stats.get("deaths_unnumbered_rounds", 0) or 0)
    if unnumbered:
        lines.append(
            _line(
                "From unnumbered",
                f"{unnumbered} deaths (warmup and the knife round)",
            )
        )

    without_attacker = int(stats.get("deaths_without_attacker", 0) or 0)
    if without_attacker:
        lines.append(
            _line(
                "No attacker",
                f"{without_attacker} (a fall or the bomb; an observation and "
                "not a fault)",
            )
        )

    for key, label in (
        ("deaths_without_victim_area", "Victim without area"),
        ("deaths_without_attacker_area", "Attacker without area"),
    ):
        count = int(stats.get(key, 0) or 0)
        if count:
            lines.append(
                _line(label, f"{count} rows (the coordinates are still there)")
            )

    for key, label, detail in (
        (
            "deaths_without_tick",
            "Death without a tick",
            "the row was dropped: without a tick there is no round and no t_s",
        ),
        (
            "deaths_outside_rounds",
            "Between rounds",
            "does not belong to any round, so there is no t_s",
        ),
        (
            "deaths_without_victim",
            "Death without victim",
            "the row was dropped: the event had no user_steamid",
        ),
        (
            "deaths_without_victim_side",
            "Victim without side",
            "the row was dropped: the death belongs to neither team",
        ),
        (
            "deaths_attacker_without_side",
            "Attacker without side",
            "the row survived, the attacker's lineup was left empty",
        ),
    ):
        count = int(stats.get(key, 0) or 0)
        if count:
            lines.append(_line(label, f"{count} ({detail})"))

    return lines


def _lineups(stats: dict) -> list[str]:
    """The lineup table's rows for the ``parse`` output (Story 2.6).

    A row **per lineup**, not joint counts: the demo holds both teams'
    players, so a shared clan listing is non-empty as soon as the opponent has
    a name and therefore says nothing about the subject team. The same goes
    for players without a name.

    Every row carries the ``lineup_key``, because the user's next command is
    ``classify --team <lineup_key>``: the name says who is meant, the id says
    what to type on the command line.
    """
    if "lineups_unreadable" in stats:
        return [
            _line("Lineups", f"no counts obtained ({stats['lineups_unreadable']})")
        ]
    if "lineup_rows" not in stats:
        return []

    # The key is checked above, so no default is needed: it would mask a drift
    # between the producer and the consumer as a zero.
    rows = int(stats["lineup_rows"])
    lineups = tuple(stats.get("lineups") or ())
    lines = [_line("Lineups", f"{rows} player rows")]
    for key, clan, players, without_name in lineups:
        name = str(clan) if clan else "no clan name was observed"
        detail = f"{name} ({key}) -- {int(players)} players"
        if int(without_name):
            detail += (
                f", {int(without_name)} without a name (the report shows them "
                "their SteamID64)"
            )
        lines.append(_line("Lineup", detail))

    lines.extend(_lineup_conflicts(stats))
    return lines


def _lineup_conflicts(stats: dict) -> list[str]:
    """The players observed with more than one name or clan on the same map.

    Zero is the whole lineup table's base assumption: the name is a property
    of the map and not of the round, and the clan follows the player and not
    the side. The table writes the mode, so a broken assumption would look
    intact there -- this number is the only place it shows. A zero is not
    printed, because it is the expected value.
    """
    lines: list[str] = []
    for key, label in (
        ("lineup_clan_conflicts", "Clan changed mid-map"),
        ("lineup_name_conflicts", "Name changed mid-map"),
    ):
        count = int(stats.get(key, 0) or 0)
        if count:
            lines.append(
                _line(
                    label,
                    f"{count} of the players had more than one observation on "
                    "the same map -- the most frequently observed one was "
                    "written into the table",
                )
            )
    return lines


def _utility(stats: dict, rounds: int) -> list[str]:
    """The utility events' rows for the ``parse`` output.

    Four questions, four numbers: **did** utility data come into being
    (throws), **did** the trajectory end (detonations), **did** the area
    reasoning land, and **did** anything go missing on the way. Zero throws is
    a valid result -- a demo may have had no utility thrown in it -- but it is
    said out loud, because the round count would otherwise look the same with
    a broken read.
    """
    if "events_unreadable" in stats:
        return [
            _line("Utility", f"no counts obtained ({stats['events_unreadable']})")
        ]
    if "event_rows" not in stats:
        return []

    lines: list[str] = []
    throws = int(stats.get("utility_throws", 0) or 0)
    detonations = int(stats.get("utility_detonations", 0) or 0)
    utility_rounds = int(stats.get("utility_rounds", 0) or 0)

    if throws:
        lines.append(
            _line(
                "Utility",
                f"{throws} throws, {detonations} detonations "
                f"(in {utility_rounds}/{rounds} rounds)",
            )
        )
    else:
        lines.append(_line("Utility", "0 throws -- no utility data came into being"))

    # A grenade that does not detonate is normal (the player dies with the
    # throw in hand), but a large difference would mean the end of the
    # trajectory is not recognised. The other way round it is impossible: a
    # detonation comes into being only as a throw's pair, so a negative
    # difference is a fault and not an observation -- and it is not printed as
    # a negative count of "missing" ones.
    if detonations > throws:
        lines.append(
            _line(
                "Too many detonations",
                f"{detonations - throws} more than there were throws -- "
                "the utility table is inconsistent",
            )
        )
    elif throws > detonations:
        lines.append(
            _line("Without a detonation", f"{throws - detonations} grenades")
        )

    if throws:
        lines.extend(_utility_areas(stats))

    # The adapter's and the stage's own observations: a dropped grenade cannot
    # be seen in a finished table. The labels fit within _PARSE_LABEL_WIDTH so
    # that the value column stays straight.
    for key, label, description in (
        (
            "grenades_without_thrower",
            "Without a thrower",
            "trajectories were skipped",
        ),
        (
            "grenades_outside_rounds",
            "Without a round",
            "grenades (warmup or after the round was decided)",
        ),
        (
            "utility_unnumbered_rounds",
            "No round number",
            "throws from unnumbered rounds (warmup, the knife round)",
        ),
        (
            "grenades_unknown_side",
            "Without a side",
            "grenades were skipped (the thrower's team was not resolved)",
        ),
        (
            "grenades_unknown_type",
            "Unknown type",
            "grenades -- demoparser2's class name is not on the list",
        ),
        (
            "grenades_fire_type_unresolved",
            "Fire type unresolved",
            "grenades were left as molotovs (the incendiary distinction was "
            "not resolved)",
        ),
        (
            "grenades_detonating_after_round",
            "Late detonation",
            "after the round ended (an observation -- the area comes from the "
            "point cloud as it does for the others)",
        ),
        (
            "grenade_ticks_without_players",
            "No rows on the tick",
            "throws with no player rows -- the thrower's area could not even "
            "be attempted",
        ),
        (
            "grenades_sharing_an_entity_id",
            "Shared id",
            "grenades share the game's id within a round (an observation)",
        ),
    ):
        count = int(stats.get(key, 0) or 0)
        if count:
            lines.append(_line(label, f"{count} {description}"))
    return lines


def _utility_areas(stats: dict) -> list[str]:
    """The area's sources separately: observation, estimate and missing.

    Three numbers and not one, because they are information of different
    quality. A throw's area is the thrower's own ``m_szLastPlaceName``, that
    is, an observation; a detonation's area is an estimate derived from the
    nearest cell of the point cloud. Lumped together, the report's reader
    would take both to be equally certain.

    The three extra rows are Story 2.9's measures, and they are reported **on
    every run** and not once during calibration:

    ``Detonation area``
        The coverage ``n/m`` and the share. It is the only number that answers
        the question "how many utility rows are left without an area".
    ``Distance to a cell``
        The median, p90 and largest. The setting states the threshold; this
        states where the measurement actually landed -- and the largest number
        is the one that shows why the threshold exists.
    ``Beyond the threshold``
        The detonations for which a nearest cell was found but fell beyond the
        threshold. That is the threshold's **price**, and it is a different
        thing from an empty point cloud: in both the area is missing, but only
        here is the distance known. The row appears only when the number is
        non-zero.

    **The ``Thrower without a row`` row is left over from Story 2.10.** A
    throw's area is the thrower's own ``m_szLastPlaceName`` from the same
    tick, so without their row the area is left empty and cannot be replaced:
    the point cloud names detonations, not throws. Before pawnless rows were
    skipped this case crashed the run; now the throw would drift quietly,
    without a row of its own, into the ``without an area`` count above. The
    row appears only when the number is non-zero, and the expected value is
    zero.

    **The ``Unnamed area`` row went away in Story 2.9.** It reported the case
    "the nearest player was found, but the game has no name for their area",
    and it can no longer come into being: an unnamed cell does not get into
    the point cloud, so a detonation landing inside the threshold always gets
    a name. In its place is ``Beyond the threshold``, which answers the same
    question -- "the area was missing, but the measurement succeeded" -- with
    the right cause. The note is here so that anyone comparing two runs across
    versions sees the row has gone and finds out why.
    """
    observed = int(stats.get("utility_area_observed", 0) or 0)
    from_cloud = int(stats.get("utility_area_point_cloud", 0) or 0)
    beyond = int(stats.get("utility_area_beyond_threshold", 0) or 0)
    without_area = int(stats.get("utility_without_area", 0) or 0)
    lines = [
        _line(
            "Utility area",
            f"{observed} observed, {from_cloud} from the point cloud, "
            f"{without_area} without an area",
        )
    ]

    coverage = stats.get("utility_detonation_area_coverage")
    if coverage:
        # The denominator cannot be zero: the stage leaves the number out
        # entirely if there were no detonations. The same rule as with the
        # other missing keys -- "0/0" would be a claim nothing supports.
        named, total = coverage
        lines.append(
            _line("Detonation area", f"{named}/{total} named ({named / total:.0%})")
        )
    spread = stats.get("utility_snap_distance")
    if spread:
        median, p90, largest = spread
        lines.append(
            _line(
                "Distance to a cell",
                f"median {median:.0f}, p90 {p90:.0f}, largest {largest:.0f} "
                "units",
            )
        )
    orphans = int(stats.get("grenade_throwers_without_row") or 0)
    if orphans:
        lines.append(
            _line(
                "Thrower without a row",
                f"{orphans} throws were left without an area (the thrower was "
                "not among the rows of the throw's tick)",
            )
        )
    if beyond:
        lines.append(
            _line(
                "Beyond the threshold",
                f"{beyond} detonations (a nearest cell was found, but it is "
                "further away than area_snap_units)",
            )
        )
    return lines


def _callouts(stats: dict) -> list[str]:
    """The point cloud's rows for the ``parse`` output.

    **The number of areas matters more than the number of cells.** The number
    of cells says only how big a cell is; the number of areas says whether the
    cloud recognised the map. Measured 2026-08-30: Ancient 18, Nuke 29, Anubis
    28, Inferno 24. A single-digit number would mean ``last_place_name``
    arrives mostly empty -- and then every detonation area would be a guess.

    Of the observations a **ratio and not a bare sum** is reported. The
    numerator is the table's own ``callout_observations`` (the usable rows)
    and the denominator is the diagnostics' ``callout_cloud_rows_read`` (the
    whole tick count); only the latter cannot be read from a finished table,
    so the whole row appears only from a fresh run. The usable share is 71-78
    % in the measured data, and a collapse would mean a broken filter.
    """
    if "callouts_unreadable" in stats:
        return [
            _line("Point cloud", f"no counts obtained ({stats['callouts_unreadable']})")
        ]
    if "callout_cells" not in stats:
        return []

    cells = int(stats.get("callout_cells", 0) or 0)
    areas = int(stats.get("callout_areas", 0) or 0)
    lines = [
        _line("Point cloud", f"{cells} cells, {areas} areas")
        if cells
        else _line("Point cloud", "empty -- not one detonation area is named")
    ]

    read = int(stats.get("callout_cloud_rows_read", 0) or 0)
    # The usable rows come from the table and not from a second counter: they
    # are the sum of the cells' observations, and two sources for the same
    # number could drift apart.
    usable = int(stats.get("callout_observations", 0) or 0)
    if read:
        lines.append(
            _line(
                "Cloud observations",
                f"{usable}/{read} tick rows qualified ({usable / read:.0%} "
                "alive and with a known area)",
            )
        )
    reason = stats.get("callout_cloud_empty_reason")
    if reason:
        lines.append(_line("Cloud empty because", str(reason)))
    return lines


def _map_name(stats: dict) -> list[str]:
    """The map's name for the ``parse`` output -- also when it was not obtained.

    The row is always there, and that is the whole point. The name is
    ``aggregate``'s only means of joining two demos into the same map, and it
    cannot be inferred from the FACEIT id. If demoparser2 renamed the
    ``map_name`` field, every demo would go back to being a map branch of its
    own -- and without this row that would happen with no sign at all. The
    same class of fault as Story 2.10's player without a pawn: a silent
    return to a worse result.

    The name comes from **the finished table**, so it appears from a skipped
    run too. The reason for its absence comes from the diagnostics and appears
    only from a fresh run, because the finished table does not know it.
    """
    if "match_unreadable" in stats:
        return [_line("Map", f"no counts obtained ({stats['match_unreadable']})")]
    if "map_name" not in stats:
        return []

    name = stats.get("map_name")
    if name:
        return [_line("Map", f"{name} (observed from the demo's header)")]

    lines = [
        _line(
            "Map",
            "the header held no map name -- aggregate infers it from the id",
        )
    ]
    reason = stats.get("header_map_name_missing_reason")
    if reason:
        lines.append(_line("Map missing because", str(reason)))
    return lines


@app.command("classify")
def classify(
    target: str = typer.Argument(
        ...,
        metavar="MAP_DEMO_ID",
        help="The parsed demo's id, the same one parse was run with.",
    ),
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "The subject team's lineup id (lineup_key) or an unambiguous "
            "beginning of it. Without this the command lists the demo's "
            "lineups."
        ),
    ),
    all_teams: bool = typer.Option(
        False,
        "--all-teams",
        help=(
            "Classify the demo from both teams' points of view. Each gets a "
            "result of its own; --team is ignored."
        ),
    ),
    show: bool = typer.Option(
        False,
        "--show",
        help=(
            "Print the round list: round, side, money and equipment value per "
            "player, loss count, type and reasoning."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Classify even if the manifest matches.",
    ),
) -> None:
    """Classify a parsed demo's rounds from one team's point of view.

    Writes the ``classified/<team_key>/<map_demo_id>.parquet`` table, the same
    content as a round list in Markdown, and the manifest. The demo is not
    read, so adjusting the thresholds and running again finish in seconds.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)

    teams: list[str | None]
    if all_teams:
        teams = list(classify_stage.team_keys(archive, target))
    else:
        teams = [team]

    for index, choice in enumerate(teams):
        if index:
            typer.echo("")
        result = classify_stage.run(
            settings.thresholds,
            settings.league,
            archive,
            target,
            choice,
            economy=settings.economy,
            force=force,
        )
        typer.echo(_render_classify(result))
        if show:
            lines = result.stats.get("rows")
            typer.echo("")
            if lines:
                typer.echo(_render_round_list(lines))
            else:
                typer.echo(
                    "The round list could not be read out of the result. Run "
                    "the command again with the --force flag."
                )


def _render_classify(result: StageResult) -> str:
    """Assemble the ``classify`` command's summary.

    Split into a function of its own so that the output can be tested without
    running the command line.
    """
    stats = result.stats
    lines: list[str] = []
    lines.append(f"{'Skipped' if result.skipped else 'Classified'}: {result.unit}")

    if result.status != "ok":
        lines.append(_line("Status", str(result.status)))
    if result.reason:
        lines.append(_line("Reason", result.reason))

    team_key = stats.get("team_key")
    if team_key:
        lines.append(_line("Team", str(team_key)))

    if "unreadable" in stats:
        lines.append(_line("Rounds", f"no counts obtained ({stats['unreadable']})"))
        lines.append(_line("Run time", _seconds(result.duration_s)))
        return "\n".join(lines)

    lines.append(_line("Rounds", str(int(stats.get("rounds", 0) or 0))))

    distribution = stats.get("by_type") or {}
    if distribution:
        # A fixed order for the round types, so that the output is comparable
        # from one run to the next; the unknown ones last.
        order = [t for t in ROUND_TYPES if t in distribution]
        order += [t for t in sorted(distribution) if t not in ROUND_TYPES]
        lines.append(_line("Types", ", ".join(f"{t} {distribution[t]}" for t in order)))

    # AD-9: an unclassified round must not be lost in the type breakdown, so
    # it is on a row of its own -- and only there.
    #
    # **The label is Finnish and stays Finnish.** ``UNCLASSIFIED`` is the only
    # visible name of a round that could not be classified, in the output and
    # in the report's sections alike (``constants.UNCLASSIFIED``), so the
    # console and the report have to call it the same thing.
    unclassified = int(stats.get("unclassified", 0) or 0)
    if unclassified:
        lines.append(
            _line(
                UNCLASSIFIED.capitalize(),
                f"{unclassified} (the observation is missing, the reason is "
                "in the round list)",
            )
        )

    unnumbered = int(stats.get("unnumbered", 0) or 0)
    if unnumbered:
        lines.append(
            _line(
                "Unnumbered",
                f"{unnumbered} (no round number, left out of the "
                "classification)",
            )
        )

    for path in result.outputs:
        lines.append(_line("Output", str(path)))
    if result.manifest_path is not None:
        lines.append(_line("Manifest", str(result.manifest_path)))
    lines.append(_line("Run time", _seconds(result.duration_s)))
    return "\n".join(lines)


#: The reasoning does not fit in a column, so it is printed on an indented row
#: of its own -- a truncated reasoning will not do, because the classification
#: is checked against the demo by exactly that text.
_REASON_COLUMN = "reason"


def _render_round_list(rows: list[dict]) -> str:
    """The round list for the console.

    This is the output the user checks the classification against the demo
    with: every round shows both the decision and the values it rested on. The
    columns come from the stage's own ``ROUND_LIST_COLUMNS`` definition, so the
    console and the Markdown cannot present different things.
    """
    if not rows:
        return "There are no rounds."

    narrow = [
        (index, label)
        for index, (label, key) in enumerate(classify_stage.ROUND_LIST_COLUMNS)
        if key != _REASON_COLUMN
    ]
    reason_index = next(
        index
        for index, (_, key) in enumerate(classify_stage.ROUND_LIST_COLUMNS)
        if key == _REASON_COLUMN
    )

    cells = [classify_stage.round_list_cells(row) for row in rows]
    headers = [label for _, label in narrow]
    widths = [
        max(len(label), *(len(r[index]) for r in cells))
        for index, label in narrow
    ]

    result: list[str] = []
    result.append("  ".join(o.ljust(w) for o, w in zip(headers, widths)).rstrip())
    result.append("  ".join("-" * w for w in widths))
    for row_cells in cells:
        narrow_cells = [row_cells[index] for index, _ in narrow]
        result.append(
            "  ".join(s.ljust(w) for s, w in zip(narrow_cells, widths)).rstrip()
        )
        reason = row_cells[reason_index].strip()
        if reason:
            result.append(f"    {reason}")
    result.append("")
    result.append("The money figures are $/player at the end of the buy time.")
    result.append("Available = left + spent; left is the balance after the")
    result.append("purchases, so on a saving round it is large.")
    return "\n".join(result)


@app.command("aggregate")
def aggregate(
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "The team's id (the name of the classified/ directory) or an "
            "unambiguous beginning of it. Without this the run ends in an "
            "error that lists the archive's teams."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Aggregate even if the manifest matches.",
    ),
) -> None:
    """Gather a team's classified rounds into one report.json file.

    The stage computes everything: the player counts by area at every sample
    point, utility's throw and detonation areas with their time windows, and
    the first contact's areas -- one map, side and round type at a time, every
    claim with its sample. It does not choose what the report says; render
    does that.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    result = aggregate_stage.run(
        settings.thresholds,
        settings.league,
        archive,
        team,
        aggregate_settings=settings.aggregate,
        force=force,
    )
    typer.echo(_render_aggregate(result))


def _render_aggregate(result: StageResult) -> str:
    """Assemble the ``aggregate`` command's summary.

    Split into a function of its own so that the output can be tested without
    running the command line. The samples are the output's most important
    part: the user checks from them whether the data they expected came in.
    """
    stats = result.stats
    lines: list[str] = []
    lines.append(f"{'Skipped' if result.skipped else 'Aggregated'}: {result.unit}")

    if result.reason:
        lines.append(_line("Reason", result.reason))

    # The name before the lineups: it is the first thing the user checks, and
    # the source says whether it is an observation or an id standing in for
    # one.
    if stats.get("display_name_source") == "clan_name":
        lines.append(
            _line("Name", f"{stats.get('display_name')} (observed from the demos)")
        )
    elif "display_name_source" in stats:
        lines.append(
            _line(
                "Name",
                "not observed -- the report speaks of the id "
                f"{stats.get('display_name')}",
            )
        )
    alternatives = stats.get("display_name_alternatives") or []
    if alternatives:
        lines.append(
            _line(
                "Other observed names",
                f"{', '.join(str(n) for n in alternatives)} (the demos give "
                "the team more than one name)",
            )
        )

    lineups = stats.get("lineup_keys") or []
    if len(lineups) > 1:
        lines.append(
            _line(
                "Lineups",
                f"{', '.join(str(k) for k in lineups)} (joined into the same "
                "team on the strength of their shared players)",
            )
        )
    roster = stats.get("roster") or []
    if roster:
        # Names into the output, ids into the report: the command line's
        # output is a glance, and six SteamID64s would fill it without saying
        # more. A player without a name is said out loud and not dropped.
        named = ", ".join(
            str(entry.get("display_name") or entry.get("player_id"))
            for entry in roster
        )
        without = sum(1 for entry in roster if not entry.get("display_name"))
        lines.append(
            _line(
                "Roster",
                f"{len(roster)} players observed: {named}"
                + (
                    f" ({without} without a name, the id stands in for it)"
                    if without
                    else ""
                ),
            )
        )

    lines.append(
        _line(
            "Sample",
            f"{int(stats.get('demos', 0) or 0)} demos, "
            f"{int(stats.get('rounds', 0) or 0)} rounds",
        )
    )
    sample = stats.get("sample") or {}
    if sample:
        # **The bucket names stay Finnish, and they are the one exception in
        # this module.** The keys are English because they are part of
        # ``report.json``'s contract; the names are ``SAMPLE_BUCKET_FI``, that
        # is, report vocabulary that passes through the console (AD-11), so
        # the reader sees the same word here and in the report. The rest of
        # the line is console text and is English.
        lines.append(
            _line(
                "Buckets",
                ", ".join(
                    f"{SAMPLE_BUCKET_FI[name]} {sample[name]['demos']} demos / "
                    f"{sample[name]['rounds']} rounds"
                    for name in SAMPLE_BUCKETS
                    if name in sample
                ),
            )
        )

    unclassified = int(stats.get("unclassified", 0) or 0)
    if unclassified:
        lines.append(
            _line(
                "Unclassified",
                f"{unclassified} rounds (the observation is missing, not part "
                "of the structure)",
            )
        )

    unpaired = int(stats.get("unpaired_detonations", 0) or 0)
    if unpaired:
        lines.append(
            _line(
                "Unpaired detonations",
                f"{unpaired} (no throw row was found; not part of utility's "
                "numbers)",
            )
        )

    # The anomalies on a row of their own right after the sample: they are the
    # epic's most valuable output, and it is from them that the user checks
    # whether adjusting the threshold showed. Zero anomalies is written **with
    # the coverage**, because a zero is an observation only about what was
    # examined.
    scan = stats.get("anomaly_scan") or {}
    anomalies = stats.get("anomalies") or []
    if scan:
        rules = ", ".join(str(name) for name in scan.get("rules") or [])
        scanned = int(scan.get("rounds_scanned", 0) or 0)
        crunch_rounds = int(scan.get("crunch_rounds", 0) or 0)
        advance_rounds = int(scan.get("advance_rounds", 0) or 0)
        stack_rounds = int(scan.get("stack_rounds", 0) or 0)
        blind = scan.get("demos_without_orientation") or []
        no_groups = scan.get("demos_without_site_groups") or []
        deferred = scan.get("rules_deferred") or []
        detail = (
            f"{len(anomalies)} in all; the rules {rules} were run over "
            f"{scanned} rounds -- crunch can hit {crunch_rounds}, "
            f"the advance {advance_rounds} and stack {stack_rounds}"
        )
        if deferred:
            detail += f"; not run: {', '.join(str(n) for n in deferred)}"
        if blind:
            detail += (
                f"; without area orientation, {len(blind)} of the demos: "
                f"{', '.join(str(d) for d in blind)}"
            )
        # The silenced demos as a count of their own rather than after the
        # ones without orientation: they are a different blind spot (the sites
        # do not stand apart as levels) and concern a different rule. Lumped
        # into one listing the user would read them as the same shortcoming
        # and look for the fix in the wrong place.
        if no_groups:
            detail += (
                f"; without site groups, {len(no_groups)} of the demos: "
                f"{', '.join(str(d) for d in no_groups)}"
            )
        lines.append(_line("Anomalies", detail))
        for entry in anomalies:
            types = ", ".join(str(t) for t in entry.get("round_types") or [])
            # The stack's player count is a fraction in the output too: four
            # out of five is the defence's choice, four out of four is what
            # was left, and the number alone does not tell them apart. The
            # other rules have no denominator, because they were not measured.
            #
            # ``players_text`` is ``render``'s, and it says "1 pelaaja" /
            # "5 pelaajaa" in Finnish. It is report vocabulary that reaches
            # this console line through T14's package; it is not this
            # tranche's to translate. See the report of tranche T15.
            alive = entry.get("alive_at_max")
            players = (
                players_text(int(entry["players_max"]))
                if alive is None
                else f"{int(entry['players_max'])}/{int(alive)} players"
            )
            lines.append(
                f"  {entry['rule']} {entry['map_name']} {entry['side']} "
                f"{types}: {entry['area']} {players} "
                f"({entry['n']}/{entry['m']})"
            )

    for entry in stats.get("maps") or []:
        lines.append("")
        # The condition is on **the unknown one**, not on the known ones:
        # there are three sources (``demo_header``, ``map_demo_id``,
        # ``unknown``), and listing the known ones would quietly make every
        # new source an "unknown" one.
        source = (
            " (name unknown)" if entry["map_name_source"] == "unknown" else ""
        )
        lines.append(
            f"{entry['map_name']}{source}: {entry['demos']} demos, "
            f"{entry['rounds']} rounds"
        )
        for side in entry["sides"]:
            types = ", ".join(
                f"{name} {count}" for name, count in side["round_types"].items()
            )
            small = side["small_samples"]
            note = f"  [small sample: {', '.join(small)}]" if small else ""
            lines.append(f"  {side['side']}: {types}{note}")

    for missing in stats.get("missing_demos") or []:
        lines.append("")
        lines.append(_line("Missing demo", f"{missing['match']}: {missing['reason']}"))

    lines.append("")
    for path in result.outputs:
        lines.append(_line("Output", str(path)))
    if result.manifest_path is not None:
        lines.append(_line("Manifest", str(result.manifest_path)))
    lines.append(_line("Run time", _seconds(result.duration_s)))
    return "\n".join(lines)


@app.command("report")
def report(
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "The team's id (the name of the aggregates/ directory) or an "
            "unambiguous beginning of it. Without this the run ends in an "
            "error that lists the aggregated teams."
        ),
    ),
) -> None:
    """Write a readable Markdown report out of the team's report.json.

    The stage computes nothing: every number comes from the aggregation as it
    is. The report gets a timestamped name, so a new run never overwrites an
    earlier one -- and that is why the command has no --force option.

    The pruning rules (``[report]``, Story 2.13) decide which rows are written
    into the report. They do not change report.json: turning a rule off and
    running the command again brings the row back without aggregating.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    result = render_stage.run(settings.report, archive, team)
    typer.echo(_render_report(result))


def _render_report(result: StageResult) -> str:
    """Assemble the ``report`` command's output.

    The most important line is the result's path: the user opens the file
    next. The numbers say what went into it, so that a missing map or a
    missing demo is noticed before the report is pasted into Discord.
    """
    stats = result.stats
    # The first line is the file's path, because it is the only thing the user
    # needs next. The team id will not do: it is a 16-character digest that
    # the user just typed on the command line themselves.
    written = str(result.outputs[0]) if result.outputs else "(no file)"
    lines = [f"Report written: {written}"]

    team = str(stats.get("team_key", result.unit))
    if not stats.get("team_name_known", False):
        team += " (the team's name is not known)"
    lines.append(_line("Team", team))

    lines.append(
        _line(
            "Sample",
            f"{int(stats.get('demos', 0) or 0)} demos, "
            f"{int(stats.get('rounds', 0) or 0)} rounds",
        )
    )
    maps = stats.get("maps") or []
    lines.append(
        _line(
            "Maps",
            ", ".join(str(name) for name in maps) if maps else "no maps at all",
        )
    )
    missing = int(stats.get("missing_demos", 0) or 0)
    if missing:
        lines.append(
            _line("Missing demos", f"{missing} in all -- listed in the report")
        )
    unclassified = int(stats.get("unclassified", 0) or 0)
    if unclassified:
        lines.append(
            _line(
                "Unclassified",
                f"{unclassified} rounds -- mentioned in the report's summary",
            )
        )
    lines.append(
        _line(
            "Extent",
            f"{int(stats.get('lines', 0) or 0)} lines, "
            f"{int(stats.get('characters', 0) or 0)} characters",
        )
    )

    lines.append("")
    if result.manifest_path is not None:
        lines.append(_line("Manifest", str(result.manifest_path)))
    lines.append(_line("Run time", _seconds(result.duration_s)))
    return "\n".join(lines)


@app.command("scout")
def scout(
    team: str = typer.Option(
        ...,
        "--team",
        help=(
            "The team's name, an unambiguous part of it, or the team id. "
            "Letter case does not matter. An ambiguous name lists the "
            "alternatives and chooses nothing."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Do not ask for confirmation. The plan is still shown.",
    ),
) -> None:
    """Run the whole chain for one team and write the report.

    discover, select, fetch, parse, classify, aggregate and report, in that
    order, in one command. The stages are the same ones the single-stage
    commands run, and each decides for itself whether its result is already
    current -- the chain does not decide it for them.

    A demo that cannot be fetched or cannot be parsed gets a status and the
    chain carries on with the rest, so one bad recording does not cost the
    others their place in the report. The run stops only when nothing can
    proceed at all, and even then the summary of what did happen is printed
    first.

    Before the first download it shows how many demos are fetched, where to
    and how much disk space they take, and asks for confirmation -- once for
    the whole plan, not once per demo. --yes skips the question; nothing
    else in the run asks anything.

    **Answering no ends the whole run**, not only the download: nothing is
    parsed, classified or aggregated after it, and no report is written.
    The exit code is 0, because the caller was asked and answered, and the
    last line says so in those words rather than leaving the missing report
    to be discovered. Not answering at all -- a pipe, a scheduler, Ctrl-C --
    reads as no.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)

    def before_download(
        todo: fetch_stage.FetchPlan, so_far: pipeline_stage.PipelineRun
    ) -> None:
        # The same three steps as the ``fetch`` command's, in the same order
        # and through the same functions: show the plan, refuse a download
        # that does not fit, then ask. The disk gate is **not** behind
        # ``--yes`` there either -- a plan that does not fit on the disk is a
        # plan with no right answer, whether or not anybody is asked.
        free = fetch_stage.free_space(archive)
        typer.echo(_render_fetch_plan(todo, free, str(archive.demos_dir())))
        _disk_space_gate(str(archive.demos_dir()), free)
        if not yes:
            # **The wording is this command's own, and the summary goes out
            # with it.** A "no" here ends the whole chain at exit 0 with no
            # report, and ``fetch``'s sentence -- "No demos were
            # downloaded" -- is exactly what a normal run with no download
            # authorisation says, except that one writes a report. An
            # unattended caller told that would go looking for a file that
            # is not there. It is also told what did happen before the
            # question, which is otherwise lost entirely.
            _confirm(
                "Download these demos?",
                _CANCELLED_SCOUT,
                before_cancelling=lambda: typer.echo(_render_scout(so_far)),
            )

    typer.echo(f"Scouting {team}...", err=True)
    try:
        result = pipeline_stage.run(
            settings,
            archive,
            team,
            match_source=discover_stage.default_source(settings, archive),
            demo_source=lambda: fetch_stage.default_source(settings, archive),
            parser=lambda: parse_stage.default_parser(settings.parse),
            before_download=before_download,
        )
    except pipeline_stage.RunStopped as stopped:
        # The summary first, the error after it. The run may have parsed and
        # classified a dozen demos before the step that could not go on, and
        # the caller acts on the summary -- losing it here would leave them
        # with the last line of a run they cannot see.
        typer.echo(_render_scout(stopped.run))
        raise stopped.cause from stopped
    typer.echo(_render_scout(result))


#: What ``scout`` says when the download question is answered no.
#:
#: **Not** :data:`_CANCELLED`. That sentence -- "No demos were downloaded" --
#: is true of a normal ``scout`` run with no download authorisation as well,
#: and that run *writes a report*. A caller told only that, at exit 0, has no
#: way to tell the two apart and will go looking for a file that was never
#: written. So this one names what was actually cancelled, which is the whole
#: chain, and says the report is missing rather than leaving it to be
#: discovered.
#:
#: The same reason ``import`` has a sentence of its own: the wording says
#: **what was left undone**, and here that is everything after the download.
_CANCELLED_SCOUT = (
    "Cancelled. Nothing was downloaded, and the rest of the chain -- parse, "
    "classify, aggregate, report -- was not run either, so no report was "
    "written. Run scout again and answer yes, or run it with --yes."
)

#: The outcome column's width. The longest word is ``no-units`` (8), and a
#: value needs a space after it.
_OUTCOME_WIDTH = 10

#: The stage column's width. The longest stage name is ``aggregate`` (9).
_STAGE_WIDTH = 11

#: How wide the unit column may grow before it stops padding. A
#: ``map_demo_id`` built from a FACEIT match id is around 40 characters, and
#: one of those must not push the detail off the screen for every other row.
_MAX_UNIT_WIDTH = 42

#: The order the totals are counted in -- the same words the rows use, so a
#: total can be matched to the lines it counts.
OUTCOME_ORDER: tuple[str, ...] = pipeline_stage.OUTCOMES

#: What each outcome word means, one entry per word in
#: :data:`~pappascout.stages.pipeline.OUTCOMES`.
#:
#: A mapping and not a paragraph, because the legend is **built by walking
#: ``OUTCOMES``**: a fifth outcome with no entry here raises a ``KeyError``
#: from every render, which is every test that prints a summary. Written out
#: as prose it drifted silently -- the words could be added to the pipeline
#: and never explained, or explained here and never set.
_OUTCOME_LEGEND: dict[str, str] = {
    "ran": "the stage did the work",
    "skipped": "the work was not needed and was not done",
    "failed": "the unit did not come through and the chain went on",
    "no-units": "the stage had nothing to do",
}

#: How wide the legend's paragraphs are wrapped. The label column is
#: :data:`_OUTCOME_WIDTH` wide, and the whole line stays inside 79 columns.
_LEGEND_WIDTH = 78

#: Below this many seconds a step's own time is not printed.
#:
#: ``_seconds`` shows one decimal, so anything under this rounds to
#: ``0,0 s`` -- a number that measures nothing and would sit on almost every
#: row of a run whose real cost is in two of them.
_STEP_TIME_FLOOR = 0.05


def _render_scout(run: pipeline_stage.PipelineRun) -> str:
    """Assemble the ``scout`` command's summary.

    **This output is the whole interface.** A person running the nine
    commands sees each result and adapts; a model running this one sees only
    this, so it has to distinguish what ran, what was skipped and what failed
    without the reader inferring anything from a number or an absence. That
    is why every step gets a line even when it did nothing, why the outcome
    is a literal word in the first column rather than a symbol, and why a
    failed step's next command is printed under it instead of being left to
    be worked out.

    The stages with no skip are named in the legend rather than silently
    reported as ``ran`` every time: ``discover`` fetches the match list on
    every run because seeing the new matches is the point of it, ``select``
    rewrites the selection file, and ``render`` writes a report -- a skipped
    report would leave the caller without the file they asked for. Each says
    so in its own module documentation, and a reader who did not know it
    would read "ran" as a change. **The names come from
    ``pipeline.NO_SKIP_STAGES``**, which is also what the test reads the
    three modules' source against, so the sentence and the behaviour cannot
    part company.

    Two of the trailing rows report a **check** rather than a result, and
    each has three states, not two: found something, found nothing, and did
    not run. A run cancelled at the download question never reaches the
    conflict scan, and "none" there would be a clean result nobody measured.
    """
    lines = [_line("Team", f"{run.team} ({run.team_key})")]
    if run.lineup_key is not None:
        lines.append(_line("Lineup", run.lineup_key))
    lines.append("")
    lines.extend(_scout_legend())
    lines.append("")

    width = min(
        _MAX_UNIT_WIDTH, max((len(s.unit) for s in run.steps), default=1) + 1
    )
    for step in run.steps:
        lines.extend(_scout_step(step, width))

    lines.append("")
    lines.append(
        _line(
            "Totals",
            ", ".join(
                f"{outcome} {run.count(outcome)}" for outcome in OUTCOME_ORDER
            ),
        )
    )
    written = run.report
    lines.append(_line("Report", str(written) if written else "(none written)"))
    lines.extend(_scout_left_out(run.lineups_left_out))
    lines.append(
        _line("Conflict copies", _found_or_not(run.conflicts, str))
    )
    lines.append(_line("Run time", _seconds(run.duration_s)))
    return "\n".join(lines)


def _found_or_not(found: Sequence[object] | None, show) -> str:
    """A check's result in three states, because it has three.

    ``None`` is "the check did not run". A check whose clean answer is
    silence cannot be told from one that never happened -- and neither can a
    check whose clean answer is the same word as its unmeasured one.
    """
    if found is None:
        return "(not checked -- the run did not get that far)"
    if not found:
        return "none"
    return f"{len(found)}: " + ", ".join(show(item) for item in found)


def _scout_legend() -> list[str]:
    """The legend, built from the pipeline's own lists.

    Neither the words nor the skipless stages are written out here. The
    words come from ``pipeline.OUTCOMES`` through :data:`_OUTCOME_LEGEND`,
    which raises if one of them has no explanation, and the stage names come
    from ``pipeline.NO_SKIP_STAGES``.

    **No legend line opens with an outcome word.** A line beginning
    ``ran = ...`` reads as a row of the table below it, both to a person
    scanning the left column and to anything matching on it -- which is
    exactly what the first draft did, and what ``outcome_rows`` in the tests
    counted as an eighth step.
    """
    words = "; ".join(
        f"{word} = {_OUTCOME_LEGEND[word]}" for word in pipeline_stage.OUTCOMES
    )
    skipless = _and_list(pipeline_stage.NO_SKIP_STAGES)
    note = (
        f"{skipless} have no skip -- they do their work on every run."
        if len(pipeline_stage.NO_SKIP_STAGES) != 1
        else f"{skipless} has no skip -- it does its work on every run."
    )
    return [
        *_wrapped("Outcomes:", f"{words}.", _OUTCOME_WIDTH),
        *_wrapped("Note:", note, _OUTCOME_WIDTH),
    ]


def _and_list(names: Sequence[str]) -> str:
    """``a, b and c``. Built from the list, so a fourth name reads correctly."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _wrapped(label: str, text: str, indent_width: int) -> list[str]:
    """One labelled paragraph, wrapped, with the continuation lines indented."""
    return textwrap.wrap(
        text,
        width=_LEGEND_WIDTH,
        initial_indent=f"{label:<{indent_width}}",
        subsequent_indent=" " * indent_width,
    )


def _scout_left_out(left_out: tuple[str, ...] | None) -> list[str]:
    """The lineups that were classified as this team but are not in the report.

    See :func:`~pappascout.stages.pipeline.lineups_left_out` for why the two
    can differ. The line is printed on every run, including the ordinary one
    where it says ``none``: a silent clean result is a result nobody can
    tell from a check that was dropped -- and the whole reason this row
    exists is that these demos leave no other trace, not even in the
    report's own missing-demos list.
    """
    lines = [_line("Lineups left out", _found_or_not(left_out, str))]
    if not left_out:
        return lines
    lines.extend(
        _wrapped(
            "",
            "These lineups share enough players with the team's standing "
            "roster to be classified as this team, but not enough with the "
            "report's own lineup to be joined to it, so their demos are in "
            "neither the report nor its missing-demos list. Report each on "
            "its own: uv run pappascout aggregate --team <key> and then "
            "uv run pappascout report --team <key>.",
            _PARSE_LABEL_WIDTH + 2,
        )
    )
    return lines


def _scout_step(step: pipeline_stage.Step, width: int) -> list[str]:
    """One step's lines: the row itself, then its reason and its next step.

    The reason goes on a line of its own rather than into the row, because a
    stage's reason can be a paragraph -- ``fetch``'s disk errors and
    ``classify``'s selection notes both are -- and a paragraph inside a
    column destroys the columns for every other row.

    **The step's own time is on the row when there is one.** Twelve demos
    through the chain is minutes, and which of them cost them is a question
    the summary is the only place to answer; SM-1 is measured against this
    command. A time below the threshold is left off rather than printed as
    ``0,0 s``, which is a measurement of nothing.
    """
    head = (
        f"{step.outcome:<{_OUTCOME_WIDTH}}"
        f"{step.stage:<{_STAGE_WIDTH}}"
        f"{step.unit:<{width}}"
    )
    tail = step.detail or ""
    if step.duration_s >= _STEP_TIME_FLOOR:
        tail = f"{tail}  [{_seconds(step.duration_s)}]" if tail else (
            f"[{_seconds(step.duration_s)}]"
        )
    # The padding is kept when something follows it and dropped when nothing
    # does: a row of trailing spaces is invisible on the screen and awkward
    # in anything that reads the output back.
    head = f"{head}  {tail}" if tail else head.rstrip()
    rows = [head]
    trailer = (step.reason, f"-> {step.next_step}" if step.next_step else None)
    for text in trailer:
        if not text:
            continue
        for row in str(text).splitlines():
            rows.append(f"{'':<{_OUTCOME_WIDTH}}{row}")
    return rows


def _seconds(value: float) -> str:
    """Seconds with a Finnish decimal comma.

    The comma is number formatting for the same reader as
    ``fetch.size_fi``'s ``kt/Mt/Gt`` and its comma, and the two appear on the
    same screen. AD-11 moved the console's words into English; it did not
    change how a number is written, and one line reading "2,6 Gt" beside
    another reading "0.3 s" would be worse than either alone.
    """
    return f"{value:.1f} s".replace(".", ",")


def main() -> None:
    """The program's entry point.

    Turns exceptions into something the user can understand:

    * :class:`~pappascout.errors.PappascoutError` -- an expected situation
      whose message says what to do next. Exit code 1.
    * any other exception -- a program fault, shown as a short line and not as
      a traceback. Exit code 2.

    The user does not code, so a traceback is of no use to them.
    """
    try:
        app()
    except PappascoutError as exc:
        # The advice on a line of its own, if the error carries one. The same
        # rule as with the per-unit failures: the advice comes from the fault,
        # not from where the fault happened to be sorted.
        advice = getattr(exc, "advice", None)
        tail = f"\n-> {advice}" if isinstance(advice, str) and advice.strip() else ""
        typer.secho(f"Error: {exc}{tail}", fg=typer.colors.RED, err=True)
        sys.exit(EXIT_KNOWN_ERROR)
    except Exception as exc:  # noqa: BLE001 - the last guard in front of the user
        typer.secho(
            f"Unexpected error: {exc}\n"
            "This is a program fault. Try again or make a note of the case.",
            fg=typer.colors.RED,
            err=True,
        )
        sys.exit(EXIT_UNEXPECTED_ERROR)


if __name__ == "__main__":  # pragma: no cover
    main()
