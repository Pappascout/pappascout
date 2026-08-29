"""Kierrosnumerointi ja kierroksen sisäiset invariantit.

Kierrosnumerointi on Story 1.2:n ainoa aito päättelykohta. Demossa on ennen
ensimmäistä pelattua kierrosta warmup-kierroksia ja puukkokierros, ja niiden
jälkeen ``mp_restartgame`` nollaa pisteet. Kierroksia ei siksi voi laskea
tapahtumien lukumäärästä: Ancient-demossa ``round_officially_ended`` esiintyy
40 kertaa, vaikka pelattuja kierroksia on 21.

Luotettava tunnusmerkki on **pistemäärän kehitys**: pelattu kierros on
sellainen, jonka jälkeen joukkueiden yhteispistemäärä on suurempi kuin ennen
sitä. Puukkokierros ei täytä ehtoa, koska sen tuottama piste nollataan
uudelleenkäynnistyksessä, eikä warmup-kierros koska se ei tuota pistettä
lainkaan.

Toinen tehtävä on **voiton syyn invariantti** (:func:`check_win_reasons`). CS2:n
sääntöjen mukaan T voi voittaa kierroksen vain eliminoimalla CT:t tai
räjäyttämällä pommin; CT voittaa eliminoinnilla, purkamisella tai ajan
loppuessa -- jos molemmat joukkueet vain istuvat aloituspaikallaan, CT voittaa.
Sääntö on riippumaton tarkistus sille, että puolet ovat oikein päin: jos
kokoonpanojen tunnistus menisi väärin, voitot kohdistuisivat väärälle
joukkueelle ja tämä tarkistus jää siitä kiinni.

Moduuli on puhdas: ei tiedostoja, ei demoparser2:ta, ei asetuksia. Sen voi
siksi testata käsin rakennetulla taululla, ja se on ainoa paikka, joka päättää
``round_no``-arvon (spinen sääntö: yksi funktio, jota vain ``parse`` kutsuu).
"""

from __future__ import annotations

import polars as pl

from pappascout.errors import ParseError, SchemaError

__all__ = [
    "REQUIRED_COLUMNS",
    "T_WIN_REASONS",
    "CT_WIN_REASONS",
    "WIN_REASONS",
    "mark_played_rounds",
    "check_win_reasons",
]

#: Sarakkeet, jotka :func:`mark_played_rounds` tarvitsee.
#:
#: ``score_start`` ja ``score_end`` ovat **molempien joukkueiden yhteispisteet**
#: kierroksen alussa ja lopussa. Yhteispiste kestää puoliajan vaihdon, jossa
#: joukkuekohtaiset pisteet vaihtavat paikkaa mutta summa säilyy.
REQUIRED_COLUMNS: tuple[str, ...] = ("round_raw", "score_start", "score_end")

#: Syyt, joilla **T** voi voittaa kierroksen.
#:
#: Vain kaksi tapaa: kaikki CT:t kuolleina tai pommi räjähtänyt.
#: ``ct_surrender`` on mukana, koska CS2 tuntee sen; Pappaliigan demoissa sitä
#: ei esiinny. Nimet ovat demoparser2 0.42.0:n ``round_end``-tapahtuman
#: ``reason``-merkkijonoja, luettu kirjaston omasta merkkijonotaulusta.
T_WIN_REASONS: frozenset[str] = frozenset(
    {
        "ct_killed",  # kaikki CT:t eliminoitu
        "bomb_exploded",  # pommi räjähti
        "ct_surrender",  # CT luovutti
    }
)

#: Syyt, joilla **CT** voi voittaa kierroksen.
#:
#: Eliminointi, purku tai aika. Aikavoitto on mukana, vaikka sitä ei esiinny
#: kummassakaan testidemossa: se on CS2:n normaali lopputulos, kun kumpikaan
#: joukkue ei tee mitään. demoparser2:n merkkijonotaulussa on aikavoitolle
#: kaksi nimeä, joten molemmat kelpaavat.
CT_WIN_REASONS: frozenset[str] = frozenset(
    {
        "t_killed",  # kaikki T:t eliminoitu
        "bomb_defused",  # pommi purettu
        "t_saved",  # aika loppui, kohde säästyi
        "time_ran_out",  # aika loppui
        "t_surrender",  # T luovutti
    }
)

#: Puoli -> sallitut voiton syyt.
WIN_REASONS: dict[str, frozenset[str]] = {"T": T_WIN_REASONS, "CT": CT_WIN_REASONS}


def mark_played_rounds(df: pl.DataFrame) -> pl.DataFrame:
    """Lisää tauluun ``round_no``-sarake.

    Taulu on pitkä: sama ``round_raw`` esiintyy kerran kummallekin joukkueelle.
    Molemmat rivit saavat saman numeron.

    Args:
        df: Taulu, jossa on vähintään :data:`REQUIRED_COLUMNS`. Rivien
            järjestys säilyy.

    Returns:
        Sama taulu ``round_no``-sarakkeella täydennettynä. Pelatut kierrokset
        saavat juoksevan numeron 1..N ``round_raw``-järjestyksessä; warmup,
        puukkokierros ja uudelleenkäynnistykset saavat ``null``.

    Raises:
        SchemaError: Jos vaadittu sarake puuttuu, ``round_raw`` sisältää
            nulleja tai samalla ``round_raw``-arvolla on ristiriitaiset
            pistelukemat.
        ParseError: Jos yhteispistemäärä kasvaa yhdellä kierroksella enemmän
            kuin yhdellä. Silloin kierrosrajojen tunnistus on pudottanut
            kierroksen välistä, eikä numerointi enää vastaisi demoa.
    """
    missing = [name for name in REQUIRED_COLUMNS if name not in df.columns]
    if missing:
        raise SchemaError(
            "mark_played_rounds tarvitsee sarakkeet "
            f"{', '.join(REQUIRED_COLUMNS)}; puuttuu: {', '.join(missing)}."
        )

    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Int32).alias("round_no"))

    if df["round_raw"].null_count():
        raise SchemaError(
            "mark_played_rounds: round_raw sisältää null-arvoja. Jokaisella "
            "kierroksella on oltava demon oma kierrostunniste, jotta "
            "numerointi on toistettavissa."
        )

    per_round = (
        df.select(list(REQUIRED_COLUMNS)).unique(maintain_order=True).sort("round_raw")
    )
    if per_round.height != df["round_raw"].n_unique():
        raise SchemaError(
            "mark_played_rounds: samalla round_raw-arvolla on ristiriitaiset "
            "score_start- tai score_end-lukemat. Kierroksen pistelukemat ovat "
            "kierroskohtaisia, joten molemmilla joukkuerivillä on oltava samat "
            "arvot."
        )

    _check_score_steps(per_round)

    played = (
        pl.col("score_end").is_not_null()
        & pl.col("score_start").is_not_null()
        & (pl.col("score_end") > pl.col("score_start"))
    )
    numbered = per_round.with_columns(
        pl.when(played)
        .then(played.cast(pl.Int32).cum_sum())
        .otherwise(pl.lit(None, dtype=pl.Int32))
        .alias("round_no")
    )

    return df.with_columns(
        pl.col("round_raw")
        .replace_strict(
            old=numbered["round_raw"],
            new=numbered["round_no"],
            return_dtype=pl.Int32,
        )
        .alias("round_no")
    )


def _check_score_steps(per_round: pl.DataFrame) -> None:
    """Yksi kierros saa tuottaa enintään yhden pisteen.

    Pistemäärän lasku on sallittu -- se on ``mp_restartgame``. Yli yhden hyppy
    sen sijaan tarkoittaa, että kahden mittauspisteen väliin on jäänyt kierros,
    jota ei tunnistettu. Hiljainen hyväksyntä siirtäisi kaikkien seuraavien
    kierrosten numeroinnin yhdellä.
    """
    too_many = per_round.filter(
        pl.col("score_start").is_not_null()
        & pl.col("score_end").is_not_null()
        & ((pl.col("score_end") - pl.col("score_start")) > 1)
    )
    if too_many.is_empty():
        return
    row = too_many.row(0, named=True)
    raise ParseError(
        f"Kierroksella round_raw={row['round_raw']} yhteispistemäärä kasvoi "
        f"{row['score_start']} -> {row['score_end']} eli enemmän kuin "
        "yhdellä.\n"
        "Kierrosrajojen tunnistuksesta on jäänyt kierros välistä, joten "
        "numerointi ei vastaisi demoa. Tarkista demo ja demoparser2:n versio."
    )


def check_win_reasons(df: pl.DataFrame) -> pl.DataFrame:
    """Tarkista, että voiton syy sopii voittaneeseen puoleen.

    CS2:n säännöt sallivat T:lle vain kaksi voittotapaa (eliminointi tai
    räjähtänyt pommi) ja CT:lle kolme (eliminointi, purku tai aika). Jos
    tarkistus pettää, kyse ei ole yksittäisestä väärästä kentästä vaan
    merkistä siitä, että **puolet ovat menneet väärin päin** -- silloin
    jokainen kierroksen havainto olisi kohdistettu väärälle joukkueelle.

    Rivit, joilla ``won`` tai ``win_reason`` on tyhjä, ohitetaan: ne ovat
    kierroksia, jotka eivät ehtineet ratketa.

    Args:
        df: Taulu, jossa on sarakkeet ``side``, ``won`` ja ``win_reason``.

    Returns:
        Sama taulu muuttumattomana.

    Raises:
        SchemaError: Jos vaadittu sarake puuttuu.
        ParseError: Jos jokin kierros rikkoo säännön.
    """
    required = ("side", "won", "win_reason")
    missing = [name for name in required if name not in df.columns]
    if missing:
        raise SchemaError(
            "check_win_reasons tarvitsee sarakkeet "
            f"{', '.join(required)}; puuttuu: {', '.join(missing)}."
        )
    if df.is_empty():
        return df

    winners = df.filter(
        pl.col("won").fill_null(False) & pl.col("win_reason").is_not_null()
    )
    for row in winners.iter_rows(named=True):
        side = str(row["side"])
        reason = str(row["win_reason"])
        allowed = WIN_REASONS.get(side)
        if allowed is None:
            raise ParseError(
                f"Tuntematon puoli {side!r} kierrostaulussa. Sallitut ovat "
                f"{', '.join(sorted(WIN_REASONS))}."
            )
        if reason in allowed:
            continue
        round_id = row.get("round_no") or row.get("round_raw")
        other_side = "CT" if side == "T" else "T"
        hint = (
            f" Syy {reason!r} kuuluu puolelle {other_side}, joten puolet ovat "
            "todennäköisesti väärin päin."
            if reason in WIN_REASONS[other_side]
            else " Syy ei ole tunnettu CS2:n kierroksen lopetustapa."
        )
        raise ParseError(
            f"Kierroksella {round_id} puoli {side} voitti syyllä {reason!r}, "
            "mikä on CS2:n sääntöjen vastaista.\n"
            f"{side} voi voittaa vain näin: {', '.join(sorted(allowed))}."
            f"{hint}\n"
            "Parsinta on mennyt pieleen -- tulosta ei kirjoiteta."
        )
    return df
