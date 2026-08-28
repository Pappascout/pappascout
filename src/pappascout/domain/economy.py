"""Kierroksen talouspäättely: loss count ja kierrostyyppi (AD-4).

Tämä moduuli on ``classify``-vaiheen aivot. Se ei lue demoa, tiedostoja eikä
asetustiedostoa -- se saa kierrostaulun rivit ja ``[thresholds]``-osion ja
palauttaa jokaiselle kierrokselle tyypin, **ihmisluettavan perustelun** ja
**kaikki vertailuun käytetyt arvot**. Perustelu ja lähtöarvot eivät ole koriste:
ilman niitä kynnysten kalibrointi Story 1.4:ssä olisi arvailua, eikä Veeti
pystyisi tarkistamaan työkalun näkemystä demoa vasten.

Mitä havaitaan ja mitä johdetaan
--------------------------------
``parse`` havaitsee: raha ja käytetty raha freezetimen lopussa, varustearvo
freezetimen lopussa, kierroksen alun varustearvo, luettavissa olleiden
pelaajien määrä, eloonjääneet ja voittaja. Tämä moduuli johtaa niistä loss
countin ja kierrostyypin. Mitään johdettua ei kirjoiteta takaisin
``parsed/``-hakemistoon.

Rahan kaksi suuntaa -- lue tämä ennen kuin muutat kynnyksiä
-----------------------------------------------------------
``money_freeze_end`` on **jäljelle jäänyt saldo ostoajan jälkeen**, ei
käytettävissä ollut raha. Säästökierroksella se on siksi *suuri* ja täydellä
ostolla *pieni*. Käytettävissä ollut raha saadaan summana::

    käytettävissä = money_freeze_end + money_spent

Se on se luku, jota käyttäjän alkuperäinen sääntö ("hävityn jälkeen ~2 000
$/pelaaja ja loss count >= 2 -> eco") tarkoittaa. **Story 1.3 vain tallentaa
sen** perusteluun ja ``inputs``-rakenteeseen; sääntöjen kytkeminen siihen on
Story 1.4:n kalibrointityötä, eikä sitä tehdä täällä etukäteen.

Ostettu summa on erotus ``equip_freeze_end - equip_round_start``. Se on ainoa
suoraan havaittu mittari sille, ostiko joukkue vai ei, ja juuri se erottaa
forcen ecosta (I/O-matriisin sanoin: *"ostettu lähes tyhjäksi"*).

Oletuspistooli (Glock / USP-S / P2000) on ilmainen mutta lasketaan
varustearvoon **200 $:n** arvoisena, joten jokaisella pelaajalla on aina
vähintään 200 $ varustearvoa ja täysi eco on joukkueena noin 1 000 $, ei 0.
Kynnykset pidetään raa'assa varustearvossa; pistoolin osuutta ei vähennetä.

Sääntöjärjestys
---------------
Järjestys on tarkoituksella jyrkkä, ja ensimmäinen osuma voittaa:

1. **Puuttuva havainto** -- ``status != "ok"`` tai raha, varustearvo tai
   kierroksen alun varustearvo tyhjä. Kierrosta ei luokitella: ``round_type``
   on ``None`` ja syy kertoo miksi. Ajo ei kaadu. Puuttuvaa lähtöarvoa ei
   korvata nollalla: nolla väittäisi, että koko varustearvo ostettiin tällä
   kierroksella, ja kääntäisi aidon säästön forceksi.
2. **Pistooli** -- kierrosnumerosta (``pistol_rounds``), ei rahasta.
3. **Jatkoaika** -- ``round_no > regulation_rounds``.
4. **Täysi osto** -- varustearvo/pelaaja ``>= full_equip_min``.
5. **Negatiivinen ostos** -- varustearvo laski kierroksen alusta freezetimen
   loppuun. Havainnot ovat ristiriidassa, joten tulos on ``anomaly``; nollaan
   vaimentaminen piilottaisi virheen.
6. **Edellistä kierrosta ei ole** -- eco, force ja puoliosto ovat sääntöjä
   *suhteessa edelliseen kierrokseen*, joten ilman sitä tulos on ``anomaly``.
7. **Voiton jälkeen** -- matala varustearvo on ``anomaly``, ei ``eco``;
   puoliosto kelpaa myös voiton jälkeen (I/O-matriisi ei rajaa sitä hävittyyn
   kierrokseen).
8. **Hävityn jälkeen** -- puoliosto, sitten force ja lopuksi eco.

Aukot ovat poikkeamia, eivät arvauksia
--------------------------------------
Kummallakin puolella (voitto ja häviö) sääntöjen väliin jäävä tilanne on
``anomaly`` perusteluineen, ei "luultavasti eco". Tunnetut aukot ovat
``anomaly_equip_max_after_win``in ja ``half_equip_min``in väli sekä
``force_money_max``in ylittävä ostos, joka silti jää puoliostorajan alle. Ne
eivät ole vika vaan tarkalleen se joukko kierroksia, joille Story 1.4:n on
löydettävä raja -- kalibroimaton eco olisi pahempi, koska se näyttäisi
oikealta.

Miksi force tarkistetaan ennen econ raharajaa
---------------------------------------------
I/O-matriisin eco-rivi ja force-rivi menevät päällekkäin: molemmat kuvaavat
tilannetta, jossa rahaa on vähän. Ainoa havainto, joka erottaa ne, on se
ostiko joukkue vai ei. Siksi ``force_money_min`` ja ``force_money_max``
verrataan **ostettuun summaan**: joukkue, joka pani 1 500-2 500 $/pelaaja
kiinni halpaan kalustoon, on forcannut. Kaistan ylittävä ostos, joka silti
jäi puoliostorajan alle, on ``anomaly`` -- se tarkoittaa, että
``force_money_max`` ja ``half_equip_min`` eivät kohtaa.

Tunnetut rajaukset
------------------
* **Jatkoaika litistetään yhdeksi ``ot``-tyypiksi.** Myös jatkoajassa
  säästetään ja forcataan, mutta talousmalli on eri (aloitusraha
  ``league.ot_start_money``, ei eco-sykliä), eikä ``[league]``-osio vaikuta
  tässä storyssa päättelyyn lainkaan -- vain manifestin parametrihashiin ja
  kierroslistan otsikkoon. Jatkoajan oma talouspäättely on v2.
* ``anomaly_equip_max_after_win`` toimii tässä myös hävityn kierroksen
  puolella econ ylärajana. Nimi kertoo sen alkuperäisen käyttötarkoituksen;
  jos Story 1.4 tarvitsee kaksi eri rajaa, ne erotetaan silloin.

Moduuli on puhdas ja testataan käsin rakennetuilla tauluilla ilman demoja.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

import polars as pl

from pappascout.domain.models import ThresholdSettings
from pappascout.errors import SchemaError

__all__ = [
    "Decision",
    "INPUT_FIELDS",
    "LOSS_COUNT_COLUMNS",
    "CLASSIFY_COLUMNS",
    "available_money",
    "per_player",
    "loss_counts",
    "classify_round",
]


class Decision(NamedTuple):
    """Yhden kierroksen luokittelupäätös.

    NamedTuple, joten se puretaan ``round_type, reason, inputs = ...``
    -muodossa mutta kentät ovat myös nimillä luettavissa.

    Attributes:
        round_type: Kierrostyyppi tai ``None``, jos kierrosta ei voitu
            luokitella (havainto puuttuu).
        reason: Suomenkielinen perustelu, joka nimeää päätöksen ratkaisseet
            arvot. Sisältää aina rahan ja loss countin.
        inputs: Kaikki vertailuun käytetyt arvot ja kynnykset
            (``schemas.CLASSIFIED_INPUTS``-rakenne).
    """

    round_type: str | None
    reason: str
    inputs: dict[str, Any]


#: Sarakkeet, jotka :func:`loss_counts` tarvitsee.
LOSS_COUNT_COLUMNS: tuple[str, ...] = ("round_no", "side", "won")

#: Sarakkeet, jotka :func:`classify_round` lukee kierrosriviltä.
CLASSIFY_COLUMNS: tuple[str, ...] = (
    "round_no",
    "side",
    "status",
    "money_freeze_end",
    "money_spent",
    "equip_freeze_end",
    "equip_round_start",
    "players_freeze_end",
    "survivors_equip_prev",
)

#: ``CLASSIFIED_INPUTS``-rakenteen kentät siinä järjestyksessä, jossa ne
#: kirjoitetaan. Nimet on lukittu ``domain/schemas.py``:ssä.
INPUT_FIELDS: tuple[str, ...] = (
    "money_freeze_end",
    "money_spent",
    "equip_freeze_end",
    "equip_round_start",
    "survivors_prev",
    "survivors_equip_prev",
    "prev_round_won",
    "players",
    "players_readable",
    "full_equip_min",
    "half_equip_min",
    "eco_money_max",
    "eco_money_max_low_loss",
    "eco_loss_count_min",
    "eco_money_max_applied",
    "force_money_min",
    "force_money_max",
    "anomaly_equip_max_after_win",
)


def available_money(rivi: Mapping[str, Any]) -> int | None:
    """Kierroksella käytettävissä ollut raha = jäljelle jäänyt + käytetty.

    ``None``, jos kumpikaan osa ei ole tiedossa. Tämä on se luku, jota
    ``[thresholds]``-osion eco- ja force-raharajat alun perin tarkoittavat;
    Story 1.3 näyttää sen mutta ei vielä luokittele sen perusteella.
    """
    jaljella = rivi.get("money_freeze_end")
    kaytetty = rivi.get("money_spent")
    if jaljella is None and kaytetty is None:
        return None
    return int(jaljella or 0) + int(kaytetty or 0)


def per_player(arvo: Any, pelaajat: int) -> int | None:
    """Dollarimäärä per pelaaja kokonaislukuna.

    **Ainoa** paikka, jossa per pelaaja -arvo pyöristetään. Taulukko ja
    perustelu näyttävät siksi samalla rivillä saman luvun; kaksi eri
    pyöristystä erottaisi ne toisistaan dollarilla.
    """
    if arvo is None or not pelaajat:
        return None
    return round(int(arvo) / pelaajat)


def loss_counts(team_rounds: pl.DataFrame, thresholds: ThresholdSettings) -> list[int]:
    """Laske yhden joukkueen loss count jokaiselle kierrokselle.

    Laskuri kuvaa tilaa **kierrokseen mentäessä**: puoliajan ensimmäisellä
    kierroksella se on ``loss_count_half_start``, ja sen jälkeen edellisen
    kierroksen tulos siirtää sitä yhdellä (häviö ylös, voitto alas) rajojen
    ``loss_count_min``..``loss_count_max`` sisällä.

    Puoliaika tunnistetaan **``side``-sarakkeen vaihtumisesta**, ei
    kierrosnumerosta. Kierrosnumero pettäisi jatkoajassa ja demossa, jossa
    puoliaika ei ala kierroksesta 13.

    Kierros, jonka tulos on tuntematon (``won`` on tyhjä), ei siirrä laskuria
    kumpaankaan suuntaan -- arvaus vääristäisi kaikki seuraavat kierrokset.

    Args:
        team_rounds: Yhden joukkueen rivit, **yksi rivi per kierros** ja
            järjestettynä ``round_no``-sarakkeen mukaan nousevasti. Vaaditut
            sarakkeet ovat :data:`LOSS_COUNT_COLUMNS`.
        thresholds: ``[thresholds]``-osio.

    Returns:
        Lista, jonka alkiot vastaavat syötteen rivejä samassa järjestyksessä.

    Raises:
        SchemaError: Jos sarake puuttuu, ``round_no`` tai ``side`` on tyhjä,
            tai rivit eivät ole nousevassa kierrosjärjestyksessä ilman
            toistoja.
    """
    puuttuvat = [name for name in LOSS_COUNT_COLUMNS if name not in team_rounds.columns]
    if puuttuvat:
        raise SchemaError(
            "loss_counts tarvitsee sarakkeet "
            f"{', '.join(LOSS_COUNT_COLUMNS)}; puuttuu: {', '.join(puuttuvat)}."
        )
    if team_rounds.is_empty():
        return []

    rivit = team_rounds.select(list(LOSS_COUNT_COLUMNS)).to_dicts()
    numerot = [r["round_no"] for r in rivit]
    if any(n is None for n in numerot):
        raise SchemaError(
            "loss_counts: round_no sisältää tyhjiä arvoja. Loss count on "
            "kierrosten järjestykseen sidottu laskuri, joten numeroimaton "
            "kierros ei voi olla mukana."
        )
    if any(r["side"] is None for r in rivit):
        raise SchemaError(
            "loss_counts: side sisältää tyhjiä arvoja. Puoliaika tunnistetaan "
            "puolen vaihtumisesta, joten tyhjä puoli nollaisi laskurin "
            "äänettömästi ja vääristäisi kaikki seuraavat kierrokset."
        )
    if any(b <= a for a, b in zip(numerot, numerot[1:])):
        raise SchemaError(
            "loss_counts: rivit eivät ole nousevassa kierrosjärjestyksessä tai "
            "sama kierros esiintyy kahdesti. Anna yhden joukkueen rivit "
            "järjestettynä round_no-sarakkeen mukaan."
        )

    tulos: list[int] = []
    laskuri = thresholds.loss_count_half_start
    edellinen_side: str | None = None
    edellinen_won: bool | None = None

    for rivi in rivit:
        side = str(rivi["side"])
        if edellinen_side is None or side != edellinen_side:
            laskuri = thresholds.loss_count_half_start
        elif edellinen_won is True:
            laskuri = max(thresholds.loss_count_min, laskuri - 1)
        elif edellinen_won is False:
            laskuri = min(thresholds.loss_count_max, laskuri + 1)
        tulos.append(laskuri)
        edellinen_side = side
        edellinen_won = None if rivi["won"] is None else bool(rivi["won"])

    return tulos


def classify_round(
    rivi: Mapping[str, Any],
    edellinen: Mapping[str, Any] | None,
    kynnykset: ThresholdSettings,
    *,
    loss_count: int,
) -> Decision:
    """Luokittele yksi kierros yhden joukkueen näkökulmasta.

    Sääntöjärjestys on moduulin docstringissä. Ensimmäinen osuva sääntö
    voittaa, eikä mitään arvata: tuntematon tilanne on ``anomaly``
    perusteluineen.

    Args:
        rivi: Kierrosrivi, sarakkeet :data:`CLASSIFY_COLUMNS`.
        edellinen: Saman joukkueen edellinen kierrosrivi tai ``None``.
            **Jatkuvuus tarkistetaan täällä**: rivi kelpaa edelliseksi vain,
            jos sen ``round_no`` on tasan yksi pienempi ja ``side`` on sama.
            Muuten kierroksella ei ole edellistä, eikä voiton tai häviön
            jälkeisiä sääntöjä sovelleta.
        kynnykset: ``[thresholds]``-osio.
        loss_count: Tähän kierrokseen mentäessä voimassa oleva laskuri,
            :func:`loss_counts`-funktiosta.

    Returns:
        :class:`Decision`, joka purkautuu myös muodossa
        ``(round_type, reason, inputs)``.
    """
    pelaajat, luettavat, jakaja_ok = _players(rivi, kynnykset)
    edellinen = _continuous_previous(rivi, edellinen)
    inputs = _inputs(rivi, edellinen, kynnykset, pelaajat, luettavat, loss_count)

    round_no = rivi.get("round_no")
    if round_no is None:
        return Decision(
            None, "Kierrosta ei ole numeroitu, joten sitä ei luokitella.", inputs
        )
    round_no = int(round_no)

    status = rivi.get("status")
    if status is not None and str(status) != "ok":
        return Decision(
            None,
            f"Kierrosta {round_no} ei luokitella: kierroksen tila on "
            f"{status!r}, eli freezetimen lopun havainnot puuttuvat.",
            inputs,
        )

    raha = rivi.get("money_freeze_end")
    varusteet = rivi.get("equip_freeze_end")
    alkuvarusteet = rivi.get("equip_round_start")
    puuttuu = [
        nimi
        for nimi, arvo in (
            ("raha", raha),
            ("varustearvo", varusteet),
            ("kierroksen alun varustearvo", alkuvarusteet),
        )
        if arvo is None
    ]
    if puuttuu:
        return Decision(
            None,
            f"Kierrosta {round_no} ei luokitella: freezetimen lopusta puuttuu "
            f"{', '.join(puuttuu)}. Puuttuvaa arvoa ei korvata nollalla, koska "
            "se väittäisi koko varustearvon ostetuksi tällä kierroksella.",
            inputs,
        )

    perusta = _perusta(rivi, kynnykset, pelaajat, luettavat, loss_count, jakaja_ok)

    if round_no in kynnykset.pistol_rounds:
        return Decision(
            "pistol",
            f"Kierros {round_no} on pistoolikierros "
            f"({_lista(kynnykset.pistol_rounds)}), joten talouspäättelyä ei "
            f"sovelleta. {perusta}",
            inputs,
        )

    if round_no > kynnykset.regulation_rounds:
        return Decision(
            "ot",
            f"Kierros {round_no} on jatkoaikaa (säännönmukaisia kierroksia "
            f"{kynnykset.regulation_rounds}), joten talouspäättelyä ei "
            f"sovelleta. {perusta}",
            inputs,
        )

    varusteet_pp = int(varusteet) / pelaajat
    ostettu = int(varusteet) - int(alkuvarusteet)
    ostettu_pp = ostettu / pelaajat

    if varusteet_pp >= kynnykset.full_equip_min:
        return Decision(
            "full",
            f"Täysi osto: varustearvo {_d(varusteet_pp)} $/pelaaja vähintään "
            f"{kynnykset.full_equip_min} $. {perusta}",
            inputs,
        )

    if ostettu < 0:
        return Decision(
            "anomaly",
            f"Varustearvo laski kierroksen alusta freezetimen loppuun "
            f"({_d(ostettu_pp)} $/pelaaja), mikä ei ole ostotapahtuma. "
            "Havainnot ovat ristiriidassa, eikä erotusta vaimenneta nollaan. "
            f"{perusta}",
            inputs,
        )

    edellinen_won = None if edellinen is None else edellinen.get("won")
    if edellinen_won is None:
        return Decision(
            "anomaly",
            "Edellistä kierrosta ei ole tai sen tulosta ei tiedetä, joten "
            "eco-, force- ja puoliostosääntöjä ei voi soveltaa -- ne pätevät "
            f"vain suhteessa edelliseen kierrokseen. {perusta}",
            inputs,
        )

    if bool(edellinen_won):
        if varusteet_pp <= kynnykset.anomaly_equip_max_after_win:
            return Decision(
                "anomaly",
                f"Matala varustearvo voiton jälkeen: {_d(varusteet_pp)} "
                f"$/pelaaja enintään {kynnykset.anomaly_equip_max_after_win} $. "
                "Ecoa, forcea eikä puoliostoa ei pelata voiton jälkeen, joten "
                f"tämä on poikkeama eikä eco. {perusta}",
                inputs,
            )
        if varusteet_pp >= kynnykset.half_equip_min:
            return Decision(
                "half",
                f"Puoliosto voiton jälkeen: varustearvo {_d(varusteet_pp)} "
                f"$/pelaaja välillä {kynnykset.half_equip_min}-"
                f"{kynnykset.full_equip_min} $. {perusta}",
                inputs,
            )
        return Decision(
            "anomaly",
            f"Varustearvo {_d(varusteet_pp)} $/pelaaja jää voiton jälkeen "
            f"poikkeamarajan ({kynnykset.anomaly_equip_max_after_win} $) ja "
            f"puoliostorajan ({kynnykset.half_equip_min} $) väliin, eikä mikään "
            f"sääntö kata sitä. {perusta}",
            inputs,
        )

    # Edellinen kierros hävittiin: puoliosto, force, eco.
    if varusteet_pp >= kynnykset.half_equip_min:
        return Decision(
            "half",
            f"Puoliosto hävityn kierroksen jälkeen: varustearvo "
            f"{_d(varusteet_pp)} $/pelaaja välillä {kynnykset.half_equip_min}-"
            f"{kynnykset.full_equip_min} $. {perusta}",
            inputs,
        )

    if ostettu_pp > kynnykset.force_money_max:
        return Decision(
            "anomaly",
            f"Ostettu {_d(ostettu_pp)} $/pelaaja ylittää forcen kaistan "
            f"({kynnykset.force_money_min}-{kynnykset.force_money_max} $), "
            f"mutta varustearvo jäi {_d(varusteet_pp)} $/pelaaja alle "
            f"puoliostorajan {kynnykset.half_equip_min} $. Mikään sääntö ei "
            "kata tätä: force_money_max ja half_equip_min eivät kohtaa. "
            f"{perusta}",
            inputs,
        )

    if ostettu_pp >= kynnykset.force_money_min:
        return Decision(
            "force",
            f"Force hävityn kierroksen jälkeen: ostettu {_d(ostettu_pp)} "
            f"$/pelaaja eli forcen kaistalla {kynnykset.force_money_min}-"
            f"{kynnykset.force_money_max} $, mutta varustearvo jäi "
            f"{_d(varusteet_pp)} $/pelaaja alle puoliostorajan "
            f"{kynnykset.half_equip_min} $ -- ostettu lähes tyhjäksi. "
            f"{perusta}",
            inputs,
        )

    if varusteet_pp > kynnykset.anomaly_equip_max_after_win:
        return Decision(
            "anomaly",
            f"Joukkue ei ostanut (ostettu {_d(ostettu_pp)} $/pelaaja alle "
            f"forcen rajan {kynnykset.force_money_min} $), mutta varustearvo "
            f"{_d(varusteet_pp)} $/pelaaja on silti matalan varustearvon rajan "
            f"({kynnykset.anomaly_equip_max_after_win} $) yläpuolella. Ei eco "
            f"eikä force -- poikkeama, jolle Story 1.4 hakee rajan. {perusta}",
            inputs,
        )

    raja = _eco_bar(kynnykset, loss_count)
    kaytettavissa = available_money(rivi)
    kaytettavissa_pp = None if kaytettavissa is None else kaytettavissa / pelaajat
    if kaytettavissa_pp is None:
        tuki = "käytettävissä ollutta rahaa ei tiedetä"
    elif kaytettavissa_pp <= raja:
        tuki = (
            f"käytettävissä oli vain {_d(kaytettavissa_pp)} $/pelaaja eli "
            f"eco-rajan {raja} $ verran tai alle"
        )
    else:
        tuki = (
            f"käytettävissä oli {_d(kaytettavissa_pp)} $/pelaaja eli yli "
            f"eco-rajan {raja} $, joten raja kaipaa kalibrointia"
        )
    return Decision(
        "eco",
        f"Eco hävityn kierroksen jälkeen: ostettu vain {_d(ostettu_pp)} "
        f"$/pelaaja eli alle forcen rajan {kynnykset.force_money_min} $, ja "
        f"varustearvo {_d(varusteet_pp)} $/pelaaja jäi matalan varustearvon "
        f"rajaan {kynnykset.anomaly_equip_max_after_win} $; {tuki}. {perusta}",
        inputs,
    )


# -- Apurit --------------------------------------------------------------------


def _eco_bar(kynnykset: ThresholdSettings, loss_count: int) -> int:
    """Voimassa oleva eco-raharaja: matalalla loss countilla oma rajansa."""
    if loss_count >= kynnykset.eco_loss_count_min:
        return kynnykset.eco_money_max
    return kynnykset.eco_money_max_low_loss


def _continuous_previous(
    rivi: Mapping[str, Any], edellinen: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """Palauta edellinen kierros vain, jos se oikeasti on edellinen.

    Kelpaa vain ``round_no - 1`` samalta puolelta. Aukko kierrosnumeroissa tai
    puolen vaihtuminen tarkoittaa, että "edellinen kierros" on eri puoliajalta
    tai puuttuu kokonaan -- silloin voiton ja häviön jälkeiset säännöt eivät
    päde, eikä niitä sovelleta arvaamalla.
    """
    if edellinen is None:
        return None
    nyt = rivi.get("round_no")
    ennen = edellinen.get("round_no")
    if nyt is None or ennen is None or int(ennen) != int(nyt) - 1:
        return None
    if rivi.get("side") is None or edellinen.get("side") is None:
        return None
    if str(edellinen["side"]) != str(rivi["side"]):
        return None
    return edellinen


def _players(
    rivi: Mapping[str, Any], kynnykset: ThresholdSettings
) -> tuple[int, int | None, bool]:
    """Jakaja per pelaaja -arvoille.

    Pappaliigassa vajaalla pelaaminen on käytännössä mahdotonta, mutta otanta
    sisältää myös liigan ulkopuolisia jonopelejä, joissa se on tavallista.
    Siksi jakaja luetaan havainnosta eikä oleteta viideksi.

    Havainto hyväksytään vain välillä ``1..roster_size``. Sen ulkopuolinen arvo
    -- nolla, negatiivinen tai kokoonpanoa suurempi (vanhentuneet tai
    ylimääräiset rivit tickissä) -- aliarvioisi tai räjäyttäisi per pelaaja
    -arvot, joten silloin käytetään ``roster_size``ia ja se kerrotaan
    perustelussa.

    Returns:
        ``(jakaja, luettavien määrä havaintona, kelpasiko havainto)``.
    """
    if kynnykset.roster_size < 1:
        raise SchemaError(
            f"thresholds.roster_size on {kynnykset.roster_size}; per pelaaja "
            "-arvoja ei voi laskea, koska jakaja olisi nolla tai negatiivinen."
        )
    havaittu = rivi.get("players_freeze_end")
    luettavat = None if havaittu is None else int(havaittu)
    if luettavat is not None and 1 <= luettavat <= kynnykset.roster_size:
        return luettavat, luettavat, True
    return kynnykset.roster_size, luettavat, False


def _inputs(
    rivi: Mapping[str, Any],
    edellinen: Mapping[str, Any] | None,
    kynnykset: ThresholdSettings,
    pelaajat: int,
    luettavat: int | None,
    loss_count: int,
) -> dict[str, Any]:
    """Kokoa päätöksen lähtöarvot ``CLASSIFIED_INPUTS``-rakenteeseen.

    Ostettu summa ei ole omana kenttänään: se on erotus
    ``equip_freeze_end - equip_round_start``, ja molemmat ovat mukana.
    Käytettävissä ollut raha on vastaavasti ``money_freeze_end + money_spent``.
    Kumpikin on siis jäljitettävissä ilman skeemamuutosta.
    """
    return {
        "money_freeze_end": _i(rivi.get("money_freeze_end")),
        "money_spent": _i(rivi.get("money_spent")),
        "equip_freeze_end": _i(rivi.get("equip_freeze_end")),
        "equip_round_start": _i(rivi.get("equip_round_start")),
        "survivors_prev": None if edellinen is None else _i(edellinen.get("survivors")),
        "survivors_equip_prev": _i(rivi.get("survivors_equip_prev")),
        "prev_round_won": (
            None
            if edellinen is None or edellinen.get("won") is None
            else bool(edellinen["won"])
        ),
        "players": pelaajat,
        "players_readable": luettavat,
        "full_equip_min": kynnykset.full_equip_min,
        "half_equip_min": kynnykset.half_equip_min,
        "eco_money_max": kynnykset.eco_money_max,
        "eco_money_max_low_loss": kynnykset.eco_money_max_low_loss,
        "eco_loss_count_min": kynnykset.eco_loss_count_min,
        "eco_money_max_applied": _eco_bar(kynnykset, loss_count),
        "force_money_min": kynnykset.force_money_min,
        "force_money_max": kynnykset.force_money_max,
        "anomaly_equip_max_after_win": kynnykset.anomaly_equip_max_after_win,
    }


def _perusta(
    rivi: Mapping[str, Any],
    kynnykset: ThresholdSettings,
    pelaajat: int,
    luettavat: int | None,
    loss_count: int,
    jakaja_ok: bool,
) -> str:
    """Jokaisen perustelun yhteinen loppuosa.

    I/O-matriisi vaatii, että perustelu kertoo rahan ja loss countin -- myös
    silloin kun päätös ratkesi varustearvosta. Rahasta näytetään molemmat
    suunnat, jotta lukija ei sekoita jäljelle jäänyttä saldoa käytettävissä
    olleeseen rahaan.
    """
    varusteet = rivi.get("equip_freeze_end")
    alku = rivi.get("equip_round_start")
    ostettu = None if varusteet is None or alku is None else int(varusteet) - int(alku)
    osat = [
        f"Käytettävissä {_pp(available_money(rivi), pelaajat)}"
        f" (jäljellä {_pp(rivi.get('money_freeze_end'), pelaajat)}"
        f", käytetty {_pp(rivi.get('money_spent'), pelaajat)})",
        f"varusteet {_pp(varusteet, pelaajat)}",
        f"ostettu {_pp(ostettu, pelaajat)}",
        f"loss count {loss_count}",
    ]
    if jakaja_ok and luettavat is not None and luettavat < kynnykset.roster_size:
        jakaja = (
            f"vain {luettavat} pelaajan arvot olivat luettavissa "
            f"(kokoonpano {kynnykset.roster_size}), ja jakajana on se määrä"
        )
    elif jakaja_ok:
        jakaja = f"{pelaajat} pelaajaa"
    elif luettavat is None:
        jakaja = (
            "pelaajamäärä ei ollut luettavissa, jaettu asetuksen roster_size "
            f"arvolla {kynnykset.roster_size}"
        )
    else:
        jakaja = (
            f"luettu pelaajamäärä {luettavat} on sallitun välin "
            f"1-{kynnykset.roster_size} ulkopuolella, jaettu asetuksen "
            f"roster_size arvolla {kynnykset.roster_size}"
        )
    return f"({'; '.join(osat)}; {jakaja}.)"


def _pp(arvo: Any, pelaajat: int) -> str:
    luku = per_player(arvo, pelaajat)
    return "ei tiedossa" if luku is None else f"{luku} $/pelaaja"


def _d(arvo: float) -> str:
    """Dollarimäärä ilman desimaaleja; sama pyöristys kuin :func:`per_player`."""
    return str(round(arvo))


def _lista(arvot: list[int]) -> str:
    return "kierrokset " + ", ".join(str(a) for a in arvot)


def _i(arvo: Any) -> int | None:
    return None if arvo is None else int(arvo)
