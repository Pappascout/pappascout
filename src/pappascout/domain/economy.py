"""Kierroksen talouspäättely: loss count ja kierrostyyppi (AD-4).

Tämä moduuli on ``classify``-vaiheen aivot. Se ei lue demoa, tiedostoja eikä
asetustiedostoa -- se saa kierrostaulun rivit ja ``[thresholds]``-osion ja
palauttaa jokaiselle kierrokselle tyypin, **ihmisluettavan perustelun** ja
**kaikki vertailuun käytetyt arvot**. Perustelu ja lähtöarvot eivät ole koriste:
ilman niitä kynnysten kalibrointi olisi arvailua, eikä Veeti pystyisi
tarkistamaan työkalun näkemystä demoa vasten.

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

Kalibrointi 2026-08-29 osoitti, että **päätös nojaa jäljelle jääneeseen
saldoon**, ei käytettävissä olleeseen rahaan: se erottaa forcen puoliostosta
(S2 alla). Käytettävissä ollut raha kulkee yhä perustelussa ja
``inputs``-rakenteessa, koska se selittää lukijalle, mistä joukkueen tilanne
syntyi, mutta yksikään sääntö ei enää vertaa siihen.

Ostettu summa on erotus ``equip_freeze_end - equip_round_start``. Se on ainoa
suoraan havaittu mittari sille, ostiko joukkue vai ei, ja juuri se erottaa
ostokierroksen (force tai puoliosto) ecosta.

Oletuspistooli (Glock / USP-S / P2000) on ilmainen mutta lasketaan
varustearvoon **200 $:n** arvoisena, joten jokaisella pelaajalla on aina
vähintään 200 $ varustearvoa ja täysi eco on joukkueena noin 1 000 $, ei 0.
Kynnykset pidetään raa'assa varustearvossa; pistoolin osuutta ei vähennetä.

Kolme kovaa sääntöä (kalibrointi 2026-08-29)
--------------------------------------------
Nämä ovat sääntöjä, eivät kynnyksiä. Niitä ei viilata luvuilla. Ne tulevat
``kalibrointi-kierrostyypit.md``-dokumentista, joka on ihmisen antama totuus:
jos tämä moduuli ja se dokumentti ovat eri mieltä, **tämä moduuli on väärässä**.

* **S1 -- Säästö on aina reaktio häviöön.** Voitetun kierroksen jälkeen
  joukkue tekee normaalin oston. Voiton jälkeen ei siis koskaan ``eco``,
  ``force`` eikä ``half``; ainoa poikkeus on niin matala varustearvo, ettei se
  ole osto lainkaan -- se on ``anomaly``.
* **S2 -- Force ja puoliosto eroavat taskuun jätetystä rahasta, eivät
  varustearvosta.** Force = ostettiin tyhjäksi, eli ``money_freeze_end`` jäi
  ``force_money_left_max``iin tai sen alle. Puoliosto = ostettiin, mutta
  jätettiin varaa seuraavalle kierrokselle.
* **S3 -- Säästetty ase ei ole ostos.** Ratkaisee tällä kierroksella ostettu
  summa (``equip_freeze_end - equip_round_start``), ei varustearvo. Eloon
  jääneiden säästämä kalusto nostaa varustearvoa ilman että mitään ostettiin,
  ja varustearvoon nojaava sääntö kääntäisi sellaisen econ puoliostoksi.

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
4. **Negatiivinen ostos** -- varustearvo laski kierroksen alusta freezetimen
   loppuun. Havainnot ovat ristiriidassa, joten tulos on ``anomaly``; nollaan
   vaimentaminen piilottaisi virheen. Tämä on **ennen** täyttä ostoa: jos
   havainnot ovat keskenään ristiriidassa, niistä ei lueta luokkaa, oli
   varustearvo miten korkea tahansa.
5. **Täysi osto** -- varustearvo/pelaaja ``>= full_equip_min``.
6. **Edellistä kierrosta ei ole** -- eco, force ja puoliosto ovat sääntöjä
   *suhteessa edelliseen kierrokseen*, joten ilman sitä tulos on ``anomaly``.
7. **Voiton jälkeen** (S1) -- ``full``, paitsi jos varustearvo jäi
   ``anomaly_equip_max_after_win``iin tai sen alle: silloin ``anomaly``.
   Voiton jälkeen ei ole ecoa, forcea eikä puoliostoa.
8. **Hävityn jälkeen** -- neljä riviä, kaikki per pelaaja::

       varusteet >= full_equip_min                                   -> full
       ostettu >= force_buy_min ja jäljellä <= force_money_left_max   -> force
       ostettu >= force_buy_min                                      -> half
       muuten                                                        -> eco

   Ensimmäinen rivi on sama sääntö kuin vaihe 5 ja osuu jo siellä; se on
   tässä siksi, että häviön haara olisi luettavissa yksinään.

Miksi täysi osto ratkaistaan ennen edellisen kierroksen tuntemista
-------------------------------------------------------------------
Kalibrointidokumentin johdettu järjestys tarkistaa edellisen kierroksen ennen
täyttä ostoa. Tässä moduulissa vaihe 5 on tarkoituksella ennen vaihetta 6:
5 000 $/pelaaja on täysi osto riippumatta siitä, tunnetaanko edellinen kierros.
Vasta puoliajan ensimmäisellä kierroksella ja kierrosnumeroiden aukossa
edellistä ei ole, ja niissä ``anomaly`` väittäisi ilmiselvästä täydestä ostosta,
ettei sitä voi luokitella. Edellisen kierroksen tuntemista tarvitaan vain
econ, forcen ja puolioston erottamiseen toisistaan -- ei täyden oston
tunnistamiseen. Järjestys on pinnattu testillä, jotta se ei muutu vahingossa.

Miksi ``force_buy_min`` on forcen **ehto**, ei sen kaista
---------------------------------------------------------
Vanha malli vertasi ostettua summaa kaistaan ``force_money_min`` ..
``force_money_max``. Yläraja teki kalibrointidemon kierroksesta 20
poikkeaman: 2 710 $/pelaaja ylitti kaistan mutta jäi täyden oston alle, eikä
mikään sääntö kattanut sitä. Ylhäältä rajaa nyt ``full_equip_min``, joten
kaistaa ei tarvita.

Alaraja sen sijaan tarvitaan, eikä pelkkä "raha loppui" riitä forceksi: köyhä
joukkue, joka ostaa panssarin ja pistoolin viimeisillä rahoillaan, tyhjensi
kassan mutta ei forcannut. Siksi ``force_buy_min`` on molempien ostosääntöjen
(force ja puoliosto) yhteinen edellytys, ja vasta sen jälkeen
``force_money_left_max`` erottaa forcen puoliostosta (S2).

Kahden raharajan todistusvoima on eri
--------------------------------------
``force_buy_min`` **on havaittu**: kalibrointiaineiston forcet ostivat
1 840-2 710 ja ecot 120-950 $/pelaaja, eli valittu 1 500 on tyhjässä välissä
ja marginaalia jää molempiin suuntiin (550 ecoihin, 340 forceihin). Se ei ole
välin keskikohta eikä sen tarvitse olla; olennaista on, että kumpikaan havaittu
joukko ei ole lähellä.

``force_money_left_max`` **on päättely, ei havainto.** Aineiston forceilla jäi
taskuun 270 ja 750, mutta *puoliostoja ei aineistossa ole yhtäkään*. Raja
erottaa forcen puoliostosta, joten sen toinen puoli on kokonaan havaitsematta:
1 000 $ sanoo vain, että sillä rahalla ei enää saa mitään merkittävää.
Marginaali havaittuihin forceihin on 250 $. **Tämä on ainoa kynnys, joka
odottaa ensimmäistä kiistatonta puoliostoa** -- kun sellainen nähdään, raja
säädetään sitä vasten.

Aukkoja ei enää ole
-------------------
Häviön haara on tyhjentävä: neljäs rivi (``eco``) kattaa kaiken, mitä kolme
ensimmäistä eivät kata, joten talouspäättelyyn ei jää poikkeamaksi putoavaa
väliä. ``anomaly`` on nyt varattu tilanteille, joissa **havainto** on
ristiriitainen (negatiivinen ostos, puuttuva tai epäjatkuva edellinen kierros)
tai joissa voiton jälkeen ei ostettu käytännössä mitään.

Tunnetut rajaukset
------------------
* **Jatkoaika litistetään yhdeksi ``ot``-tyypiksi.** Myös jatkoajassa
  säästetään ja forcataan, mutta talousmalli on eri (aloitusraha
  ``league.ot_start_money``, ei eco-sykliä), eikä ``[league]``-osio vaikuta
  tässä storyssa päättelyyn lainkaan -- vain manifestin parametrihashiin ja
  kierroslistan otsikkoon. Jatkoajan oma talouspäättely on v2.
* ``force_money_left_max`` on ainoa kynnys, jonka toista puolta ei ole
  havaittu: kalibrointiaineistossa ei ole yhtäkään kiistatonta puoliostoa.
  Se säädetään uudelleen, kun sellainen kierros nähdään.
* ``loss_count`` ei enää osallistu päätökseen. Se on mukana perustelussa ja
  ``inputs``-rakenteessa, koska se kertoo lukijalle joukkueen taloustilanteen,
  mutta kalibrointiaineisto osoitti, ettei sitä tarvita econ erottamiseen.

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
    "force_buy_min",
    "force_money_left_max",
    "anomaly_equip_max_after_win",
)


def available_money(row: Mapping[str, Any]) -> int | None:
    """Kierroksella käytettävissä ollut raha = jäljelle jäänyt + käytetty.

    ``None``, jos kumpikaan osa ei ole tiedossa.

    **Yksikään sääntö ei vertaa tähän lukuun.** Force ja puoliosto eroavat
    jäljelle jääneestä saldosta (``force_money_left_max``) ja eco erottuu
    ostetusta summasta (``force_buy_min``). Käytettävissä ollut raha on
    perustelussa ja ``inputs``-rakenteessa siksi, että se selittää lukijalle,
    mistä joukkueen tilanne syntyi -- ja siksi, ettei jäljelle jäänyttä saldoa
    luulisi käytettävissä olleeksi rahaksi.
    """
    left = row.get("money_freeze_end")
    spent = row.get("money_spent")
    if left is None and spent is None:
        return None
    return int(left or 0) + int(spent or 0)


def per_player(value: Any, players: int) -> int | None:
    """Dollarimäärä per pelaaja kokonaislukuna.

    **Ainoa** paikka, jossa per pelaaja -arvo pyöristetään. Taulukko ja
    perustelu näyttävät siksi samalla rivillä saman luvun; kaksi eri
    pyöristystä erottaisi ne toisistaan dollarilla.
    """
    if value is None or not players:
        return None
    return round(int(value) / players)


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
    missing = [name for name in LOSS_COUNT_COLUMNS if name not in team_rounds.columns]
    if missing:
        raise SchemaError(
            "loss_counts tarvitsee sarakkeet "
            f"{', '.join(LOSS_COUNT_COLUMNS)}; puuttuu: {', '.join(missing)}."
        )
    if team_rounds.is_empty():
        return []

    rows = team_rounds.select(list(LOSS_COUNT_COLUMNS)).to_dicts()
    numbers = [r["round_no"] for r in rows]
    if any(n is None for n in numbers):
        raise SchemaError(
            "loss_counts: round_no sisältää tyhjiä arvoja. Loss count on "
            "kierrosten järjestykseen sidottu laskuri, joten numeroimaton "
            "kierros ei voi olla mukana."
        )
    if any(r["side"] is None for r in rows):
        raise SchemaError(
            "loss_counts: side sisältää tyhjiä arvoja. Puoliaika tunnistetaan "
            "puolen vaihtumisesta, joten tyhjä puoli nollaisi laskurin "
            "äänettömästi ja vääristäisi kaikki seuraavat kierrokset."
        )
    if any(b <= a for a, b in zip(numbers, numbers[1:])):
        raise SchemaError(
            "loss_counts: rivit eivät ole nousevassa kierrosjärjestyksessä tai "
            "sama kierros esiintyy kahdesti. Anna yhden joukkueen rivit "
            "järjestettynä round_no-sarakkeen mukaan."
        )

    result: list[int] = []
    counter = thresholds.loss_count_half_start
    previous_side: str | None = None
    previous_won: bool | None = None

    for row in rows:
        side = str(row["side"])
        if previous_side is None or side != previous_side:
            counter = thresholds.loss_count_half_start
        elif previous_won is True:
            counter = max(thresholds.loss_count_min, counter - 1)
        elif previous_won is False:
            counter = min(thresholds.loss_count_max, counter + 1)
        result.append(counter)
        previous_side = side
        previous_won = None if row["won"] is None else bool(row["won"])

    return result


def classify_round(
    row: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    thresholds: ThresholdSettings,
    *,
    loss_count: int,
) -> Decision:
    """Luokittele yksi kierros yhden joukkueen näkökulmasta.

    Sääntöjärjestys on moduulin docstringissä. Ensimmäinen osuva sääntö
    voittaa, eikä mitään arvata: tuntematon tilanne on ``anomaly``
    perusteluineen.

    Args:
        row: Kierrosrivi, sarakkeet :data:`CLASSIFY_COLUMNS`.
        previous: Saman joukkueen edellinen kierrosrivi tai ``None``.
            **Jatkuvuus tarkistetaan täällä**: rivi kelpaa edelliseksi vain,
            jos sen ``round_no`` on tasan yksi pienempi ja ``side`` on sama.
            Muuten kierroksella ei ole edellistä, eikä voiton tai häviön
            jälkeisiä sääntöjä sovelleta.
        thresholds: ``[thresholds]``-osio.
        loss_count: Tähän kierrokseen mentäessä voimassa oleva laskuri,
            :func:`loss_counts`-funktiosta.

    Returns:
        :class:`Decision`, joka purkautuu myös muodossa
        ``(round_type, reason, inputs)``.
    """
    players, readable, divisor_ok = _players(row, thresholds)
    previous = _continuous_previous(row, previous)
    inputs = _inputs(row, previous, thresholds, players, readable)

    round_no = row.get("round_no")
    if round_no is None:
        return Decision(
            None, "Kierrosta ei ole numeroitu, joten sitä ei luokitella.", inputs
        )
    round_no = int(round_no)

    status = row.get("status")
    if status is not None and str(status) != "ok":
        return Decision(
            None,
            f"Kierrosta {round_no} ei luokitella: kierroksen tila on "
            f"{status!r}, eli freezetimen lopun havainnot puuttuvat.",
            inputs,
        )

    money = row.get("money_freeze_end")
    equip = row.get("equip_freeze_end")
    equip_start = row.get("equip_round_start")
    missing = [
        name
        for name, value in (
            ("raha", money),
            ("varustearvo", equip),
            ("kierroksen alun varustearvo", equip_start),
        )
        if value is None
    ]
    if missing:
        return Decision(
            None,
            f"Kierrosta {round_no} ei luokitella: freezetimen lopusta puuttuu "
            f"{', '.join(missing)}. Puuttuvaa arvoa ei korvata nollalla, koska "
            "se väittäisi koko varustearvon ostetuksi tällä kierroksella.",
            inputs,
        )

    basis = _basis(row, thresholds, players, readable, loss_count, divisor_ok)

    if round_no in thresholds.pistol_rounds:
        return Decision(
            "pistol",
            f"Kierros {round_no} on pistoolikierros "
            f"({_listing(thresholds.pistol_rounds)}), joten talouspäättelyä ei "
            f"sovelleta. {basis}",
            inputs,
        )

    if round_no > thresholds.regulation_rounds:
        return Decision(
            "ot",
            f"Kierros {round_no} on jatkoaikaa (säännönmukaisia kierroksia "
            f"{thresholds.regulation_rounds}), joten talouspäättelyä ei "
            f"sovelleta. {basis}",
            inputs,
        )

    # Kaikki vertailtavat luvut pyöristetään **kerran** per pelaaja -arvoiksi,
    # ja perustelu tulostaa tasan samat luvut. Jos vertailu tehtäisiin
    # pyöristämättömällä liukuluvulla, perustelu voisi sanoa "jäi 1000 $ eli
    # yli 1000 $" -- teksti ja päätös olisivat keskenään ristiriidassa juuri
    # siinä rajatapauksessa, jonka lukija haluaa tarkistaa.
    # Raha, varustearvo ja kierroksen alun varustearvo on juuri todettu
    # olemassa oleviksi ja pelaajia on aina vähintään yksi, joten nämä eivät
    # voi olla None.
    equip_pp = per_player(equip, players) or 0
    bought = int(equip) - int(equip_start)
    bought_pp = per_player(bought, players) or 0
    money_pp = per_player(money, players) or 0

    # Ristiriitainen havainto ennen täyttä ostoa: jos varustearvo laski
    # ostoaikana, luvuista ei lueta luokkaa, oli varustearvo miten korkea
    # tahansa. Merkki luetaan joukkuesummasta, koska pyöristys per pelaaja
    # voisi vaimentaa pienen laskun nollaan.
    if bought < 0:
        return Decision(
            "anomaly",
            f"Varustearvo laski kierroksen alusta freezetimen loppuun "
            f"({bought} $ joukkueena, {_d(bought_pp)} $/pelaaja), mikä ei ole "
            "ostotapahtuma. Havainnot ovat ristiriidassa, eikä erotusta "
            f"vaimenneta nollaan. {basis}",
            inputs,
        )

    if equip_pp >= thresholds.full_equip_min:
        return Decision(
            "full",
            f"Täysi osto: varustearvo {_d(equip_pp)} $/pelaaja vähintään "
            f"{thresholds.full_equip_min} $. {basis}",
            inputs,
        )

    previous_won = None if previous is None else previous.get("won")
    if previous_won is None:
        return Decision(
            "anomaly",
            "Edellistä kierrosta ei ole tai sen tulosta ei tiedetä, joten "
            "eco-, force- ja puoliostosääntöjä ei voi soveltaa -- ne pätevät "
            f"vain suhteessa edelliseen kierrokseen. {basis}",
            inputs,
        )

    if bool(previous_won):
        # S1: säästö on reaktio häviöön, joten voiton jälkeen ei ole ecoa,
        # forcea eikä puoliostoa -- vain normaali osto tai poikkeama.
        if equip_pp <= thresholds.anomaly_equip_max_after_win:
            return Decision(
                "anomaly",
                f"Matala varustearvo voiton jälkeen: {_d(equip_pp)} "
                f"$/pelaaja enintään {thresholds.anomaly_equip_max_after_win} $. "
                "Ecoa, forcea eikä puoliostoa ei pelata voiton jälkeen, joten "
                f"tämä on poikkeama eikä eco. {basis}",
                inputs,
            )
        return Decision(
            "full",
            f"Normaali osto voitetun kierroksen jälkeen: varustearvo "
            f"{_d(equip_pp)} $/pelaaja ylittää matalan varustearvon rajan "
            f"{thresholds.anomaly_equip_max_after_win} $. Säästö on aina reaktio "
            "häviöön, joten voiton jälkeen ei tehdä ecoa, forcea eikä "
            f"puoliostoa. {basis}",
            inputs,
        )

    # Edellinen kierros hävittiin. Täysi osto on jo ratkaistu vaiheessa 5,
    # joten jäljellä ovat force, puoliosto ja eco. Molempien ostosääntöjen
    # yhteinen edellytys on, että joukkue oikeasti osti (S3): säästetty ase
    # nostaa varustearvoa, mutta ei ole ostos.
    if bought_pp >= thresholds.force_buy_min:
        if money_pp <= thresholds.force_money_left_max:
            return Decision(
                "force",
                f"Force hävityn kierroksen jälkeen: ostettu {_d(bought_pp)} "
                f"$/pelaaja eli vähintään {thresholds.force_buy_min} $, ja "
                f"taskuun jäi vain {_d(money_pp)} $/pelaaja eli enintään "
                f"{thresholds.force_money_left_max} $ -- ostettu tyhjäksi, "
                f"seuraavalle kierrokselle ei jätetty varaa. {basis}",
                inputs,
            )
        return Decision(
            "half",
            f"Puoliosto hävityn kierroksen jälkeen: ostettu {_d(bought_pp)} "
            f"$/pelaaja eli vähintään {thresholds.force_buy_min} $, mutta "
            f"taskuun jäi {_d(money_pp)} $/pelaaja eli yli "
            f"{thresholds.force_money_left_max} $ -- ostettiin, mutta jätettiin "
            f"varaa seuraavalle kierrokselle. {basis}",
            inputs,
        )

    return Decision(
        "eco",
        f"Eco hävityn kierroksen jälkeen: ostettu vain {_d(bought_pp)} "
        f"$/pelaaja eli alle forcen edellytyksen {thresholds.force_buy_min} $. "
        f"Varustearvo {_d(equip_pp)} $/pelaaja ei ratkaise: säästetty "
        f"kalusto ei ole tällä kierroksella tehty ostos. {basis}",
        inputs,
    )


# -- Apurit --------------------------------------------------------------------


def _continuous_previous(
    row: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """Palauta edellinen kierros vain, jos se oikeasti on edellinen.

    Kelpaa vain ``round_no - 1`` samalta puolelta. Aukko kierrosnumeroissa tai
    puolen vaihtuminen tarkoittaa, että "edellinen kierros" on eri puoliajalta
    tai puuttuu kokonaan -- silloin voiton ja häviön jälkeiset säännöt eivät
    päde, eikä niitä sovelleta arvaamalla.
    """
    if previous is None:
        return None
    current = row.get("round_no")
    before = previous.get("round_no")
    if current is None or before is None or int(before) != int(current) - 1:
        return None
    if row.get("side") is None or previous.get("side") is None:
        return None
    if str(previous["side"]) != str(row["side"]):
        return None
    return previous


def _players(
    row: Mapping[str, Any], thresholds: ThresholdSettings
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
    if thresholds.roster_size < 1:
        raise SchemaError(
            f"thresholds.roster_size on {thresholds.roster_size}; per pelaaja "
            "-arvoja ei voi laskea, koska jakaja olisi nolla tai negatiivinen."
        )
    observed = row.get("players_freeze_end")
    readable = None if observed is None else int(observed)
    if readable is not None and 1 <= readable <= thresholds.roster_size:
        return readable, readable, True
    return thresholds.roster_size, readable, False


def _inputs(
    row: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    thresholds: ThresholdSettings,
    players: int,
    readable: int | None,
) -> dict[str, Any]:
    """Kokoa päätöksen lähtöarvot ``CLASSIFIED_INPUTS``-rakenteeseen.

    Ostettu summa ei ole omana kenttänään: se on erotus
    ``equip_freeze_end - equip_round_start``, ja molemmat ovat mukana.
    Käytettävissä ollut raha on vastaavasti ``money_freeze_end + money_spent``.
    Kumpikin on siis jäljitettävissä ilman skeemamuutosta.
    """
    return {
        "money_freeze_end": _i(row.get("money_freeze_end")),
        "money_spent": _i(row.get("money_spent")),
        "equip_freeze_end": _i(row.get("equip_freeze_end")),
        "equip_round_start": _i(row.get("equip_round_start")),
        "survivors_prev": None if previous is None else _i(previous.get("survivors")),
        "survivors_equip_prev": _i(row.get("survivors_equip_prev")),
        "prev_round_won": (
            None
            if previous is None or previous.get("won") is None
            else bool(previous["won"])
        ),
        "players": players,
        "players_readable": readable,
        "full_equip_min": thresholds.full_equip_min,
        "force_buy_min": thresholds.force_buy_min,
        "force_money_left_max": thresholds.force_money_left_max,
        "anomaly_equip_max_after_win": thresholds.anomaly_equip_max_after_win,
    }


def _basis(
    row: Mapping[str, Any],
    thresholds: ThresholdSettings,
    players: int,
    readable: int | None,
    loss_count: int,
    divisor_ok: bool,
) -> str:
    """Jokaisen perustelun yhteinen loppuosa.

    I/O-matriisi vaatii, että perustelu kertoo rahan ja loss countin -- myös
    silloin kun päätös ratkesi varustearvosta. Rahasta näytetään molemmat
    suunnat, jotta lukija ei sekoita jäljelle jäänyttä saldoa käytettävissä
    olleeseen rahaan.
    """
    equip = row.get("equip_freeze_end")
    start = row.get("equip_round_start")
    bought = None if equip is None or start is None else int(equip) - int(start)
    parts = [
        f"Käytettävissä {_pp(available_money(row), players)}"
        f" (jäljellä {_pp(row.get('money_freeze_end'), players)}"
        f", käytetty {_pp(row.get('money_spent'), players)})",
        f"varusteet {_pp(equip, players)}",
        f"ostettu {_pp(bought, players)}",
        f"loss count {loss_count}",
    ]
    if divisor_ok and readable is not None and readable < thresholds.roster_size:
        divisor = (
            f"vain {readable} pelaajan arvot olivat luettavissa "
            f"(kokoonpano {thresholds.roster_size}), ja jakajana on se määrä"
        )
    elif divisor_ok:
        divisor = f"{players} pelaajaa"
    elif readable is None:
        divisor = (
            "pelaajamäärä ei ollut luettavissa, jaettu asetuksen roster_size "
            f"arvolla {thresholds.roster_size}"
        )
    else:
        divisor = (
            f"luettu pelaajamäärä {readable} on sallitun välin "
            f"1-{thresholds.roster_size} ulkopuolella, jaettu asetuksen "
            f"roster_size arvolla {thresholds.roster_size}"
        )
    return f"({'; '.join(parts)}; {divisor}.)"


def _pp(value: Any, players: int) -> str:
    number = per_player(value, players)
    return "ei tiedossa" if number is None else f"{number} $/pelaaja"


def _d(value: float) -> str:
    """Dollarimäärä ilman desimaaleja; sama pyöristys kuin :func:`per_player`."""
    return str(round(value))


def _listing(values: list[int]) -> str:
    return "kierrokset " + ", ".join(str(a) for a in values)


def _i(value: Any) -> int | None:
    return None if value is None else int(value)
