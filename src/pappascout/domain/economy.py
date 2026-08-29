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


def available_money(rivi: Mapping[str, Any]) -> int | None:
    """Kierroksella käytettävissä ollut raha = jäljelle jäänyt + käytetty.

    ``None``, jos kumpikaan osa ei ole tiedossa.

    **Yksikään sääntö ei vertaa tähän lukuun.** Force ja puoliosto eroavat
    jäljelle jääneestä saldosta (``force_money_left_max``) ja eco erottuu
    ostetusta summasta (``force_buy_min``). Käytettävissä ollut raha on
    perustelussa ja ``inputs``-rakenteessa siksi, että se selittää lukijalle,
    mistä joukkueen tilanne syntyi -- ja siksi, ettei jäljelle jäänyttä saldoa
    luulisi käytettävissä olleeksi rahaksi.
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
    inputs = _inputs(rivi, edellinen, kynnykset, pelaajat, luettavat)

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

    # Kaikki vertailtavat luvut pyöristetään **kerran** per pelaaja -arvoiksi,
    # ja perustelu tulostaa tasan samat luvut. Jos vertailu tehtäisiin
    # pyöristämättömällä liukuluvulla, perustelu voisi sanoa "jäi 1000 $ eli
    # yli 1000 $" -- teksti ja päätös olisivat keskenään ristiriidassa juuri
    # siinä rajatapauksessa, jonka lukija haluaa tarkistaa.
    # Raha, varustearvo ja kierroksen alun varustearvo on juuri todettu
    # olemassa oleviksi ja pelaajia on aina vähintään yksi, joten nämä eivät
    # voi olla None.
    varusteet_pp = per_player(varusteet, pelaajat) or 0
    ostettu = int(varusteet) - int(alkuvarusteet)
    ostettu_pp = per_player(ostettu, pelaajat) or 0
    raha_pp = per_player(raha, pelaajat) or 0

    # Ristiriitainen havainto ennen täyttä ostoa: jos varustearvo laski
    # ostoaikana, luvuista ei lueta luokkaa, oli varustearvo miten korkea
    # tahansa. Merkki luetaan joukkuesummasta, koska pyöristys per pelaaja
    # voisi vaimentaa pienen laskun nollaan.
    if ostettu < 0:
        return Decision(
            "anomaly",
            f"Varustearvo laski kierroksen alusta freezetimen loppuun "
            f"({ostettu} $ joukkueena, {_d(ostettu_pp)} $/pelaaja), mikä ei ole "
            "ostotapahtuma. Havainnot ovat ristiriidassa, eikä erotusta "
            f"vaimenneta nollaan. {perusta}",
            inputs,
        )

    if varusteet_pp >= kynnykset.full_equip_min:
        return Decision(
            "full",
            f"Täysi osto: varustearvo {_d(varusteet_pp)} $/pelaaja vähintään "
            f"{kynnykset.full_equip_min} $. {perusta}",
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
        # S1: säästö on reaktio häviöön, joten voiton jälkeen ei ole ecoa,
        # forcea eikä puoliostoa -- vain normaali osto tai poikkeama.
        if varusteet_pp <= kynnykset.anomaly_equip_max_after_win:
            return Decision(
                "anomaly",
                f"Matala varustearvo voiton jälkeen: {_d(varusteet_pp)} "
                f"$/pelaaja enintään {kynnykset.anomaly_equip_max_after_win} $. "
                "Ecoa, forcea eikä puoliostoa ei pelata voiton jälkeen, joten "
                f"tämä on poikkeama eikä eco. {perusta}",
                inputs,
            )
        return Decision(
            "full",
            f"Normaali osto voitetun kierroksen jälkeen: varustearvo "
            f"{_d(varusteet_pp)} $/pelaaja ylittää matalan varustearvon rajan "
            f"{kynnykset.anomaly_equip_max_after_win} $. Säästö on aina reaktio "
            "häviöön, joten voiton jälkeen ei tehdä ecoa, forcea eikä "
            f"puoliostoa. {perusta}",
            inputs,
        )

    # Edellinen kierros hävittiin. Täysi osto on jo ratkaistu vaiheessa 5,
    # joten jäljellä ovat force, puoliosto ja eco. Molempien ostosääntöjen
    # yhteinen edellytys on, että joukkue oikeasti osti (S3): säästetty ase
    # nostaa varustearvoa, mutta ei ole ostos.
    if ostettu_pp >= kynnykset.force_buy_min:
        if raha_pp <= kynnykset.force_money_left_max:
            return Decision(
                "force",
                f"Force hävityn kierroksen jälkeen: ostettu {_d(ostettu_pp)} "
                f"$/pelaaja eli vähintään {kynnykset.force_buy_min} $, ja "
                f"taskuun jäi vain {_d(raha_pp)} $/pelaaja eli enintään "
                f"{kynnykset.force_money_left_max} $ -- ostettu tyhjäksi, "
                f"seuraavalle kierrokselle ei jätetty varaa. {perusta}",
                inputs,
            )
        return Decision(
            "half",
            f"Puoliosto hävityn kierroksen jälkeen: ostettu {_d(ostettu_pp)} "
            f"$/pelaaja eli vähintään {kynnykset.force_buy_min} $, mutta "
            f"taskuun jäi {_d(raha_pp)} $/pelaaja eli yli "
            f"{kynnykset.force_money_left_max} $ -- ostettiin, mutta jätettiin "
            f"varaa seuraavalle kierrokselle. {perusta}",
            inputs,
        )

    return Decision(
        "eco",
        f"Eco hävityn kierroksen jälkeen: ostettu vain {_d(ostettu_pp)} "
        f"$/pelaaja eli alle forcen edellytyksen {kynnykset.force_buy_min} $. "
        f"Varustearvo {_d(varusteet_pp)} $/pelaaja ei ratkaise: säästetty "
        f"kalusto ei ole tällä kierroksella tehty ostos. {perusta}",
        inputs,
    )


# -- Apurit --------------------------------------------------------------------


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
        "force_buy_min": kynnykset.force_buy_min,
        "force_money_left_max": kynnykset.force_money_left_max,
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
