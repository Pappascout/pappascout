"""Kierroksen talouspäättely: loss count ja kierrostyyppi (AD-4).

Tämä moduuli on ``classify``-vaiheen aivot. Se ei lue demoa, tiedostoja eikä
asetustiedostoa -- se saa kierrostaulun rivit ja ``[thresholds]``-osion ja
palauttaa jokaiselle kierrokselle tyypin, **ihmisluettavan perustelun** ja
**kaikki vertailuun käytetyt arvot**. Perustelu ja lähtöarvot eivät ole koriste:
ilman niitä kynnysten kalibrointi olisi arvailua, eikä Veeti pystyisi
tarkistamaan työkalun näkemystä demoa vasten.

Mitä havaitaan ja mitä johdetaan
--------------------------------
``parse`` havaitsee: raha ja käytetty raha ostoajan lopussa, varustearvo
ostoajan lopussa, kierroksen alun varustearvo, luettavissa olleiden
pelaajien määrä, eloonjääneet ja voittaja. Tämä moduuli johtaa niistä loss
countin ja kierrostyypin. Mitään johdettua ei kirjoiteta takaisin
``parsed/``-hakemistoon.

Rahan kaksi suuntaa -- lue tämä ennen kuin muutat kynnyksiä
-----------------------------------------------------------
``money_buy_end`` on **jäljelle jäänyt saldo ostoajan jälkeen**, ei
käytettävissä ollut raha. Säästökierroksella se on siksi *suuri* ja täydellä
ostolla *pieni*. Käytettävissä ollut raha saadaan summana::

    käytettävissä = money_buy_end + money_spent

Kalibrointi 2026-08-29 osoitti, että **päätös nojaa jäljelle jääneeseen
saldoon**, ei käytettävissä olleeseen rahaan: se erottaa forcen puoliostosta
(S2 alla). Käytettävissä ollut raha kulkee yhä perustelussa ja
``inputs``-rakenteessa, koska se selittää lukijalle, mistä joukkueen tilanne
syntyi, mutta yksikään sääntö ei enää vertaa siihen.

Ostettu summa on erotus ``equip_buy_end - equip_round_start``. Se on ainoa
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
  varustearvosta.** Force = ostettiin tyhjäksi. Puoliosto = ostettiin, mutta
  jätettiin varaa seuraavalle kierrokselle. **Ehto lasketaan pelaajista, ei
  keskiarvosta** (ks. "Puolioston kaksi ehtoa" alla).
* **S3 -- Säästetty ase ei ole ostos.** Ratkaisee tällä kierroksella ostettu
  summa (``equip_buy_end - equip_round_start``), ei varustearvo. Eloon
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
8. **Hävityn jälkeen** -- osto on kaikkien yhteinen edellytys, ja sen
   jälkeen kaksi ehtoa ratkaisevat luokan::

       varusteet >= full_equip_min                       -> full  (vaihe 5)
       ostettu < force_buy_min                           -> eco   (S3)
       ostettu >= force_buy_min:
           havainto puuttuu tai on ristiriitainen        -> ei luokitella
           ehto A ei täyty (liian harva aseistettu)      -> eco
           raha ei siirry seuraavalle kierrokselle       -> force
           ehto A täyttyy, ehto B ei                     -> force
           molemmat täyttyvät                            -> half

   Ensimmäinen rivi on sama sääntö kuin vaihe 5 ja osuu jo siellä; se on
   tässä siksi, että häviön haara olisi luettavissa yksinään.

Puolioston kaksi ehtoa (Story 1.10)
-----------------------------------
Veetin määritelmä on **kaksisuuntainen**:

    "Puoliosto ei ole force silloin kun seuraavalla kierroksella
    mahdollistetaan normaali osto, ja ei ole eco kun käytössä on tarpeeksi
    arvoa."

**Ehto A -- kalusto.** Vähintään ``armed_players_min`` pelaajalla oli panssari
ja jokin parannettu ase ostoajan lopussa (havainto ``players_armed_buy_end``,
Story 1.6). Tämä erottaa puolioston **ecosta**: alle sen kierrosta ei oikeasti
pelata.

**Ehto B -- ensi kierroksen varallisuus.** Vähintään
``normal_buy_players_min`` pelaajaa pystyy normaaliin ostoon seuraavalla
kierroksella: oma saldo ostoajan lopussa plus häviöbonus yltää arvoon
``normal_buy_money_min``. Tämä erottaa puolioston **forcesta**.

**Molempien on täytyttävä, eikä kumpikaan riitä yksin.** Ehdot mittaavat eri
asiaa: A katsoo tälle kierrokselle ostettua kalustoa, B seuraavan kierroksen
ostovoimaa. ``inferno_vs_ryhmarama`` kierroksilla 6 ja 10 on **molemmissa
viisi aseistettua pelaajaa**, joten ehto A ei erota niitä lainkaan; erottelun
tekee ehto B -- kierroksella 6 kukaan viidestä ei pysty ostamaan (Veeti:
force), kierroksella 10 kaikki viisi (Veeti: puoliosto). Kierros 11 vahvisti
ennusteen **normaalilla ostolla** -- viisi AK:ta, 4 940 $/pelaaja -- ja se on
pinnattu omana rivinään ``test_calibration.py``:n ``INFERNO_TRUTH``iin,
jottei väite eläisi vain kommenteissa.

Mikä tässä on mitattu ja mikä ei
--------------------------------
**Yksikään mitattu kierros ei erota tätä sääntöä poistuneesta
keskiarvosäännöstä.** Kuudessa demossa on 23 hävityn kierroksen jälkeistä
ostokierrosta, ja vanha ``force_money_left_max`` antaisi niistä jokaiselle
saman luokan kuin ehdot A ja B. Myös aineiston epätasaisin jakauma (Anubis
kierros 6 CT: 5 050, 4 500, 2 700, 2 250, 2 150) menee samoin.

Kierrokset 6 ja 10 eivät ole vastaesimerkki vanhalle säännölle. Vanha sääntö
luokitteli kierroksen 6 väärin vain **ennen Story 1.9:ää**, kun raha luettiin
freezetimen lopusta eikä ostoajan lopusta; korjauksen teki mittaus, ei sääntö.

Säännön peruste on siis kaksiosainen, ja kumpikaan osa ei ole "mittaus kumosi
edellisen säännön":

1. **Se toteuttaa käyttäjän oman määritelmän**, joka on pelaajakohtainen:
   *"paljonko rahaa on jätetty taskuun ja mitä se tarkoittaa seuraavalle
   ostolle"* -- kysymys yksittäisistä pelaajista, ei joukkueen keskiarvosta.
2. **Se kestää epätasaisen jakauman.** Käsin rakennetut rivit
   (``test_economy.py``) osoittavat sen suoraan: sama joukkuesumma, eri
   jakauma, eri tuomio. Keskiarvo ei voi erottaa niitä millään kynnyksellä.

Aineisto ei siis vielä koettele sääntöä. Ensimmäinen kierros, jolla raha on
kasautunut harvoille, on myös ensimmäinen, joka voi kumota sen.

Miksi jakauma eikä keskiarvo
----------------------------
Ehto B lasketaan **pelaajakohtaisesta rahajakaumasta**
(``money_players_buy_end``), ei joukkuesummasta. Keskiarvo peittää juuri sen,
mistä on kyse: joukkue jolla yhdellä on 5 000 ja neljällä nolla saa saman
keskiarvon kuin joukkue jolla kaikilla on 1 000, mutta edellisessä neljä
viidestä ei voi ostaa mitään. Keskiarvo antaa myös mahdottomia lukuja:
kalibroinnin kierros 19 CT näytti "30 $/pelaaja", kun todelliset saldot olivat
0, 0, 50, 50, 50 -- kaikki hinnat ovat viidenkymmenen monikertoja, joten 30 ei
voi olla kenenkään saldo.

Sama vika **voi olla** säännössä eikä vain esitystavassa: poistunut
``force_money_left_max`` oli kiinteä raja joukkuesummalle viidellä jaettuna,
joten sen läpäisisi myös joukkue, jonka neljä viidestä ei voi ostaa mitään.
Aineistossa sellaista kierrosta ei toistaiseksi ole -- väite on siis säännön
rakenteesta, ei havainnosta.

Miksi bonus lasketaan häviön oletuksella
----------------------------------------
Puoliosto on päätös, joka tehdään varautuen siihen ettei tätä kierrosta
voiteta. Jos joukkue voittaa, rahaa tulee enemmän eikä kysymystä ole. Sääntö
kysyy siis: *jos tämä menee pieleen, onko meillä silti varaa?* Siksi bonus
luetaan loss countista, joka **on jo** se porras, joka maksetaan tämän
kierroksen häviöstä (:func:`loss_bonus_if_lost`).

Miksi ``loss_count`` palaa päätöksentekoon
-----------------------------------------
Se poistui päätöksestä Story 1.4:ssä, koska yksikään sääntö ei enää
verrannut siihen. Nyt sillä on tehtävä: häviöbonus on suoraan sen funktio
(``[economy].loss_bonus_steps``, portaat 1 400-3 400 $), ja juuri bonus
ratkaisee erottelun. Kierroksilla 6 ja 10 taskuun jäänyt raha on samaa
suuruusluokkaa, mutta bonus on 1 900 vastaan 3 400 -- ja se siirtää rajaa.

Bonusta ei kovakoodata: portaat luetaan asetuksista, ja siksi tämä moduuli
saa myös ``[economy]``-osion. ``stages.classify`` ottaa sen mukaan
parametrihashiinsa, joten portaan muuttaminen invalidoi luokittelun tuloksen.

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
kassan mutta ei forcannut. Siksi ``force_buy_min`` on kaikkien häviön
jälkeisten ostosääntöjen yhteinen edellytys, ja vasta sen jälkeen ehdot A ja B
erottavat econ, forcen ja puolioston toisistaan.

``force_buy_min`` **on havaittu**: kalibrointiaineiston forcet ostivat
1 840-2 710 ja ecot 120-950 $/pelaaja, eli valittu 1 500 on tyhjässä välissä
ja marginaalia jää molempiin suuntiin (550 ecoihin, 340 forceihin). Se ei ole
välin keskikohta eikä sen tarvitse olla; olennaista on, että kumpikaan havaittu
joukko ei ole lähellä.

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
* **Häviön jälkeinen ostokierros vaatii molemmat pelaajakohtaiset havainnot.**
  Jos ``players_armed_buy_end`` tai ``money_players_buy_end`` puuttuu -- tai
  jos ne ovat keskenään ristiriidassa ``players_buy_end``in kanssa --
  kierrosta ei luokitella lainkaan. Ehtoja A ja B ei voi arvata
  joukkuesummasta. Muut haarat (pistooli, jatkoaika, täysi osto, voiton
  jälkeen, eco ilman ostoa) eivät lue niitä eivätkä siis kaadu niiden
  puutteeseen.
* **Puoliajan viimeisellä kierroksella ehtoa B ei lasketa.** Raha ei siirry
  pistoolikierrokselle eikä jatkoajalle, joten taskuun jätettyä rahaa ei ole
  jätetty varaa varten -- tulos on ``force`` (ks.
  :func:`_money_carries_over`). Sääntö ei siis voi tuottaa puoliostoa
  kierrokselle, jolla säästäminen on mahdotonta.
* **Vajaalla joukkueella molemmat pelaajalaskurit skaalataan luettavien
  määrään** (``min(kynnys, luettavat)``). Kolmea aseistettua ei voi havaita
  kahdesta pelaajasta, ja ilman skaalausta puoliosto olisi tavoittamaton aina
  kun luettavia on kynnystä vähemmän. Skaalaus kerrotaan perustelussa. Se on
  myönnytys, ei tarkennus: kahdesta luettavasta pelaajasta ei voi päätellä,
  mitä kolme muuta tekivät.
* ``normal_buy_money_min`` on **yhden pelaajan oma saldo**, ei joukkueen
  keskiarvo -- toisin kuin kaikki muut tämän moduulin raharajat, jotka ovat
  per pelaaja -arvoja. Ero on koko säännön syy.
* ``armed_players_min`` **on lausuttu sääntö, ei havainto.** Käyttäjä sanoi
  rajan ("vähintään kolmella kevlar ja jokin parannettu ase"), mutta yksikään
  kalibroitu kierros ei koettele sitä: aineiston ainoa vähän aseistettu
  kierros (Ancient 21 T, 2/5) ratkeaa jo ostorajalla ``force_buy_min`` eikä
  koskaan saavuta ehtoa A. Sama koskee ``normal_buy_players_min``ia:
  havainnot ovat 0/5 ja 5/5, joten mikä tahansa arvo väliltä 1..5 tuottaisi
  samat tuomiot.

Moduuli on puhdas ja testataan käsin rakennetuilla tauluilla ilman demoja.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

import polars as pl

from pappascout.domain.models import EconomySettings, ThresholdSettings
from pappascout.domain.schemas import ARMED_COLUMN, MONEY_DISTRIBUTION_COLUMN
from pappascout.errors import SchemaError

__all__ = [
    "Decision",
    "INPUT_FIELDS",
    "LOSS_COUNT_COLUMNS",
    "CLASSIFY_COLUMNS",
    "available_money",
    "per_player",
    "loss_bonus_if_lost",
    "players_who_can_buy",
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
#:
#: **Tämä ei ole dokumentaatiota vaan valinta.**
#: :func:`~pappascout.stages.classify._classify_team` poimii kierrostaulusta
#: tasan nämä sarakkeet ennen kuin antaa rivit tänne, joten sarakkeen
#: pudottaminen listalta pudottaa sen myös päätöksestä. Ilman sitä lista
#: olisi kommentti, joka voi vanhentua hiljaa.
#:
#: ``won`` ja ``survivors`` ovat mukana, koska ne luetaan **edelliseltä**
#: riviltä (S1 ja ``inputs.survivors_prev``) -- sama rivijoukko kiertää
#: molemmissa rooleissa.
CLASSIFY_COLUMNS: tuple[str, ...] = (
    "round_no",
    "side",
    "status",
    "won",
    "survivors",
    "money_buy_end",
    "money_spent",
    "equip_buy_end",
    "equip_round_start",
    "players_buy_end",
    # Puolioston kaksi ehtoa. Kumpaakaan ei voi laskea joukkuesummasta, ja
    # juuri siksi ne ovat omina havaintoinaan kierrostaulussa.
    ARMED_COLUMN,
    MONEY_DISTRIBUTION_COLUMN,
    "survivors_equip_prev",
)

#: ``CLASSIFIED_INPUTS``-rakenteen kentät siinä järjestyksessä, jossa ne
#: kirjoitetaan. Nimet on lukittu ``domain/schemas.py``:ssä.
INPUT_FIELDS: tuple[str, ...] = (
    "money_buy_end",
    "money_spent",
    "money_players",
    "equip_buy_end",
    "equip_round_start",
    "survivors_prev",
    "survivors_equip_prev",
    "prev_round_won",
    "players",
    "players_readable",
    "players_armed",
    "loss_bonus_if_lost",
    "players_can_buy",
    "full_equip_min",
    "force_buy_min",
    "armed_players_min",
    "normal_buy_money_min",
    "normal_buy_players_min",
    "anomaly_equip_max_after_win",
)


def available_money(row: Mapping[str, Any]) -> int | None:
    """Kierroksella käytettävissä ollut raha = jäljelle jäänyt + käytetty.

    ``None``, jos kumpikaan osa ei ole tiedossa.

    **Yksikään sääntö ei vertaa tähän lukuun.** Eco erottuu ostetusta summasta
    (``force_buy_min``), ja force erottuu puoliostosta pelaajakohtaisesta
    rahajakaumasta (ehto B). Käytettävissä ollut raha on perustelussa ja
    ``inputs``-rakenteessa siksi, että se selittää lukijalle, mistä joukkueen
    tilanne syntyi -- ja siksi, ettei jäljelle jäänyttä saldoa luulisi
    käytettävissä olleeksi rahaksi.
    """
    left = row.get("money_buy_end")
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


def loss_bonus_if_lost(
    loss_count: int,
    thresholds: ThresholdSettings,
    economy: EconomySettings,
) -> int:
    """Häviöbonus, jonka joukkue saa **jos tämä kierros hävitään**.

    Bonus luetaan portaista ``[economy].loss_bonus_steps`` (1 400-3 400 $) --
    sitä ei kovakoodata. **Indeksi on loss count sellaisenaan**, ei
    ``loss_count + 1``: laskuri kuvaa tilaa kierrokseen mentäessä, ja juuri
    se porras maksetaan, jos kierros hävitään. ``settings.toml`` sanoo saman
    suoraan -- puoliajan alku (``loss_count_half_start = 1``) antaa
    pistoolihäviöstä 1 900 $, ja se on portaan 1 arvo.

    Puoliosto on päätös, joka tehdään varautuen häviöön, joten tämä on se
    luku, jolla ehto B lasketaan. Voitolla kysymystä ei ole: silloin rahaa
    tulee enemmän kuin bonuksesta.

    Mitattu: ``inferno_vs_ryhmarama`` kierros 6 menee laskurilla 1 arvoon
    1 900 $ ja kierros 10 laskurilla 4 arvoon 3 400 $ (katto). Ero on
    1 500 $, ja se siirtää rajaa -- taskuun jäänyt raha on kierroksilla samaa
    suuruusluokkaa.

    Args:
        loss_count: Tähän kierrokseen mentäessä voimassa oleva laskuri.
        thresholds: ``[thresholds]``-osio (laskurin katto).
        economy: ``[economy]``-osio (portaat).

    Returns:
        Bonus dollareina **yhdelle pelaajalle**.
    """
    steps = economy.loss_bonus_steps
    index = min(int(loss_count), thresholds.loss_count_max)
    # Asetusten lataus vaatii tasan loss_count_max + 1 porrasta, joten
    # katkaisu on turva eikä sääntö: ilman sitä käsin rakennettu
    # EconomySettings tai negatiivinen laskuri kaataisi luokittelun
    # IndexErroriin sen sijaan että antaisi reunimmaisen portaan.
    return int(steps[max(0, min(index, len(steps) - 1))])


def players_who_can_buy(
    money_players: list[int] | tuple[int, ...],
    loss_bonus: int,
    thresholds: ThresholdSettings,
    economy: EconomySettings,
) -> int:
    """Montako pelaajaa pystyy normaaliin ostoon seuraavalla kierroksella.

    Ehto B. Pelaaja pystyy, jos hänen **oma** saldonsa ostoajan lopussa plus
    häviöbonus yltää arvoon ``normal_buy_money_min``.

    ``normal_buy_money_min`` on yhden pelaajan oma saldo, ei joukkueen
    keskiarvo. Keskiarvo peittää juuri sen, mistä tässä on kyse: joukkue jolla
    yhdellä on 5 000 ja neljällä nolla saa saman keskiarvon kuin joukkue jolla
    kaikilla on 1 000, mutta edellisessä neljä viidestä ei voi ostaa mitään.

    **Summa katkaistaan rahakattoon** (``[economy].max_money``). Peli ei anna
    pelaajalle sen enempää, joten katkaisematta laskuri lupaisi ostovoimaa
    rahalla, jonka peli leikkaisi pois. Nykyisillä arvoilla katto on 16 000 $
    eikä pure, mutta se on osa mallia eikä sattumaa.

    Args:
        money_players: Rahajakauma, yksi alkio per luettavissa ollut pelaaja
            (``ROUNDS.money_players_buy_end``). Ei saa sisältää tyhjiä
            arvoja: lukuvirhe nollana väittäisi pelaajaa rahattomaksi.
        loss_bonus: :func:`loss_bonus_if_lost`-funktion tulos.
        thresholds: ``[thresholds]``-osio.
        economy: ``[economy]``-osio (rahakatto).

    Returns:
        Laskuri välillä ``0..len(money_players)``.

    Raises:
        SchemaError: Jos jakaumassa on tyhjä arvo. Funktio on julkinen, joten
            sopimus ei voi elää vain kutsujissa -- hiljainen nolla näyttäisi
            forcelta.
    """
    if any(money is None for money in money_players):
        raise SchemaError(
            "players_who_can_buy: rahajakaumassa on tyhjä arvo. Puuttuvaa "
            "saldoa ei korvata nollalla, koska se väittäisi pelaajaa "
            "rahattomaksi ja kääntäisi puolioston forceksi. Anna jakauma, "
            "jossa jokainen alkio on havaittu, tai jätä kierros "
            "luokittelematta."
        )
    return sum(
        1
        for money in money_players
        if min(int(money) + int(loss_bonus), economy.max_money)
        >= thresholds.normal_buy_money_min
    )


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
    economy: EconomySettings,
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
        economy: ``[economy]``-osio. Tästä luetaan ``loss_bonus_steps``
            (puolioston ehto B) ja ``max_money`` (sen rahakatto). Osio on
            kokonaisena parametrina, koska ``stages.classify`` ottaa sen
            parametrihashiinsa sellaisenaan. **Avainsanaparametri**:
            positionaalisesti annettuna se sitoutuisi hiljaa
            ``thresholds``in paikalle, ja kaksi pydantic-osiota menisi
            vaihtaen läpi tyyppitarkistuksesta.
        loss_count: Tähän kierrokseen mentäessä voimassa oleva laskuri,
            :func:`loss_counts`-funktiosta. Se palasi päätöksentekoon Story
            1.10:ssä: häviöbonus on suoraan sen funktio.

    Returns:
        :class:`Decision`, joka purkautuu myös muodossa
        ``(round_type, reason, inputs)``.
    """
    players, readable, divisor_ok = _players(row, thresholds)
    previous = _continuous_previous(row, previous)
    inputs = _inputs(
        row, previous, thresholds, economy, players, readable, loss_count
    )

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
            f"{status!r}, eli ostoajan lopun havainnot puuttuvat.",
            inputs,
        )

    money = row.get("money_buy_end")
    equip = row.get("equip_buy_end")
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
            f"Kierrosta {round_no} ei luokitella: ostoajan lopusta puuttuu "
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

    # Kaikki vertailtavat per pelaaja -luvut pyöristetään **kerran**, ja
    # perustelu tulostaa tasan samat luvut. Jos vertailu tehtäisiin
    # pyöristämättömällä liukuluvulla, perustelu voisi sanoa "ostettu 1500 $
    # eli alle 1500 $" -- teksti ja päätös olisivat keskenään ristiriidassa
    # juuri siinä rajatapauksessa, jonka lukija haluaa tarkistaa.
    #
    # Puolioston ehdoissa A ja B ongelmaa ei ole lainkaan: ne lasketaan
    # pelaajakohtaisista havainnoista eikä joukkuesummasta, joten mitään ei
    # jaeta eikä pyöristetä.
    #
    # Varustearvo ja kierroksen alun varustearvo on juuri todettu olemassa
    # oleviksi ja pelaajia on aina vähintään yksi, joten nämä eivät voi olla
    # None.
    equip_pp = per_player(equip, players) or 0
    bought = int(equip) - int(equip_start)
    bought_pp = per_player(bought, players) or 0

    # Ristiriitainen havainto ennen täyttä ostoa: jos varustearvo laski
    # ostoaikana, luvuista ei lueta luokkaa, oli varustearvo miten korkea
    # tahansa. Merkki luetaan joukkuesummasta, koska pyöristys per pelaaja
    # voisi vaimentaa pienen laskun nollaan.
    if bought < 0:
        return Decision(
            "anomaly",
            f"Varustearvo laski kierroksen alusta ostoajan loppuun "
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
    # joten jäljellä ovat force, puoliosto ja eco. Kaikkien yhteinen edellytys
    # on, että joukkue oikeasti osti (S3): säästetty ase nostaa varustearvoa,
    # mutta ei ole ostos.
    if bought_pp >= thresholds.force_buy_min:
        return _after_loss_purchase(
            row,
            thresholds,
            economy,
            bought_pp=bought_pp,
            loss_count=loss_count,
            round_no=round_no,
            readable=readable,
            inputs=inputs,
            basis=basis,
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


def _money_carries_over(round_no: int, thresholds: ThresholdSettings) -> bool:
    """Siirtyykö taskuun jäänyt raha tältä kierrokselta seuraavalle?

    Ei siirry kahdessa tilanteessa, ja molemmissa saldo nollataan:

    * **Seuraava kierros on pistoolikierros** (puoliajan ensimmäinen). Peli
      antaa silloin kaikille ``[economy].start_money``n.
    * **Seuraava kierros on jatkoaikaa.** Jatkoajalla on oma aloitusraha
      (``league.ot_start_money``, Pappaliigassa 12 500 $).

    Ehto B kysyy "onko ensi kierroksella varaa normaaliin ostoon", ja näissä
    kahdessa tapauksessa kysymys on merkityksetön: taskuun jätetty raha
    haihtuu. Ks. :func:`_after_loss_purchase` siitä, mitä sääntö silloin
    tekee.
    """
    following = round_no + 1
    return not (
        following in thresholds.pistol_rounds
        or following > thresholds.regulation_rounds
    )


def _after_loss_purchase(
    row: Mapping[str, Any],
    thresholds: ThresholdSettings,
    economy: EconomySettings,
    *,
    bought_pp: int,
    loss_count: int,
    round_no: int,
    readable: int | None,
    inputs: dict[str, Any],
    basis: str,
) -> Decision:
    """Eco, force vai puoliosto -- kun hävityn jälkeen oikeasti ostettiin.

    Kaksi ehtoa, ja **molempien on täytyttävä** jotta kierros on puoliosto:

    * **Ehto A (kalusto)** erottaa puolioston **ecosta**: vähintään
      ``armed_players_min`` pelaajalla oli panssari ja ase ostoajan lopussa.
      Alle sen kierrosta ei oikeasti pelata.
    * **Ehto B (ensi kierroksen varallisuus)** erottaa sen **forcesta**:
      vähintään ``normal_buy_players_min`` pelaajaa pystyy normaaliin ostoon
      seuraavalla kierroksella.

    Ehdot mittaavat eri asioita eikä kumpikaan korvaa toista, mutta **yksikään
    mitattu kierros ei vielä erota niitä poistuneesta keskiarvosäännöstä**:
    kuudessa demossa on 23 häviön jälkeistä ostokierrosta, ja vanha sääntö
    antaisi niistä jokaiselle saman luokan. Ero näkyy vasta epätasaisella
    jakaumalla, jonka ``test_economy.py`` rakentaa käsin: sama joukkuesumma,
    eri jakauma, eri tuomio. Säännön peruste on siis käyttäjän oma
    määritelmä, joka on pelaajakohtainen -- ei mittaus, joka olisi kumonnut
    edellisen säännön.

    **Kun raha ei siirry seuraavalle kierrokselle** (ks.
    :func:`_money_carries_over`), ehto B jätetään laskematta ja tulos on
    ``force``. Taskuun jätetty raha haihtuu puoliajan vaihtuessa, joten sitä
    ei ole jätetty *varaa varten* -- eikä kierros voi olla puoliosto S2:n
    merkityksessä. Tämä on johdos pelin talousmallista, ei kynnys: uutta
    asetusta ei tarvita, eikä sääntö voi tuottaa puoliostoa kierrokselle,
    jolla säästäminen on mahdotonta.

    Molemmat laskurit ovat perustelussa myös silloin, kun toinen jo ratkaisi
    asian: lukija ei muuten näe, kumpi ehto hylkäsi kierroksen ja kuinka
    läheltä.
    """
    armed = row.get(ARMED_COLUMN)
    money_players = row.get(MONEY_DISTRIBUTION_COLUMN)

    missing: list[str] = []
    if armed is None:
        missing.append("aseistettujen laskuri")
    # Tyhjä lista on sama asia kuin puuttuva: se ei ole havainto siitä, ettei
    # ketään ollut, vaan siitä ettei ketään saatu luettua. Yksittäinen tyhjä
    # alkio tyhjentää saman tien koko jakauman: null tulkittuna nollaksi
    # väittäisi pelaajaa rahattomaksi, ja lukuvirhe näyttäisi forcelta.
    if not money_players:
        missing.append("pelaajakohtainen rahajakauma")
    elif any(money is None for money in money_players):
        missing.append("yhden pelaajan saldo rahajakaumasta")

    if missing:
        return Decision(
            None,
            f"Kierrosta {round_no} ei luokitella: hävityn kierroksen jälkeen "
            f"ostettiin {_d(bought_pp)} $/pelaaja, mutta puoliosto erotetaan "
            f"forcesta ja ecosta pelaajakohtaisista havainnoista, ja niistä "
            f"puuttuu {_names(missing)}. Joukkuesummasta niitä ei voi "
            f"päätellä, eikä luokkaa arvata. {basis}",
            inputs,
        )

    armed = int(armed)
    players_read = len(money_players)

    # Rivin sisäinen ristiriita: kaikkien pelaajakohtaisten lukujen on
    # tultava **samasta joukosta**. Jos jakauman pituus ja havaittu
    # pelaajamäärä eroavat, samalla rivillä olisi kaksi eri jakajaa -- ja
    # laskuri "3/5" tarkoittaisi eri asiaa kuin varustearvo per pelaaja.
    # Eroa ei paikata kumpaankaan suuntaan.
    conflict: str | None = None
    if readable is None or readable != players_read:
        conflict = (
            f"rahajakaumassa on {players_read} pelaajaa, mutta "
            f"players_buy_end sanoo {readable}"
        )
    elif armed > players_read:
        conflict = (
            f"aseistettuja on {armed}, mutta luettavissa oli vain "
            f"{players_read} pelaajaa"
        )
    if conflict is not None:
        return Decision(
            None,
            f"Kierrosta {round_no} ei luokitella: pelaajakohtaiset havainnot "
            f"ovat keskenään ristiriidassa -- {conflict}. Laskurit on "
            f"laskettava samasta joukosta kuin summat, eikä eroa paikata "
            f"arvaamalla. {basis}",
            inputs,
        )

    # Vajaa joukkue: kolmea aseistettua ei voi havaita kahdesta pelaajasta,
    # joten kynnys skaalataan luettavien määrään. Ilman tätä puoliosto olisi
    # tavoittamaton aina kun luettavia on kynnystä vähemmän, ja jokainen
    # ostos putoaisi ecoksi -- hiljaa ja uskottavan näköisesti.
    armed_min = min(thresholds.armed_players_min, players_read)
    buyers_min = min(thresholds.normal_buy_players_min, players_read)
    needed = max(thresholds.armed_players_min, thresholds.normal_buy_players_min)
    scaled = (
        ""
        if players_read >= needed
        else (
            f" Vaatimukset on skaalattu luettavien pelaajien määrään "
            f"({players_read}), koska sitä suurempaa laskuria ei voi havaita."
        )
    )

    carries = _money_carries_over(round_no, thresholds)
    bonus = loss_bonus_if_lost(loss_count, thresholds, economy) if carries else None
    can_buy = (
        players_who_can_buy(money_players, bonus, thresholds, economy)
        if carries
        else None
    )

    armed_part = f"{armed}/{players_read} aseistettua"
    buy_part = (
        f"{can_buy}/{players_read} pystyy ostamaan ensi kierroksella"
        if carries
        else "ehtoa B ei lasketa, koska raha ei siirry seuraavalle kierrokselle"
    )
    counters = f"{armed_part}, {buy_part}"
    bonus_note = (
        (
            f"ehto B laskettiin häviön oletuksella: oma saldo + häviöbonus "
            f"{bonus} $ vähintään {thresholds.normal_buy_money_min} $, saldot "
            f"{_listing_money(money_players)}"
        )
        if carries
        else (
            f"saldot {_listing_money(money_players)}, mutta ne nollautuvat "
            f"ennen kierrosta {round_no + 1}"
        )
    )

    if armed < armed_min:
        # Ehto A ensin: jos kierrosta ei oikeasti pelata, se ei ole force
        # eikä puoliosto vaikka rahaa olisi liikkunut paljonkin.
        return Decision(
            "eco",
            f"Eco hävityn kierroksen jälkeen: ostettiin {_d(bought_pp)} "
            f"$/pelaaja eli vähintään {thresholds.force_buy_min} $, mutta "
            f"{counters} -- aseistettuja on alle {armed_min}, eli kierrosta ei "
            f"oikeasti pelata. ({bonus_note}.){scaled} {basis}",
            inputs,
        )

    if not carries:
        return Decision(
            "force",
            f"Force hävityn kierroksen jälkeen: ostettu {_d(bought_pp)} "
            f"$/pelaaja eli vähintään {thresholds.force_buy_min} $, "
            f"{armed_part}. Taskuun jäänyt raha ei siirry kierrokselle "
            f"{round_no + 1}, joten sitä ei ole jätetty varaa varten eikä "
            f"kierros voi olla puoliosto. ({bonus_note}.){scaled} {basis}",
            inputs,
        )

    if can_buy >= buyers_min:
        return Decision(
            "half",
            f"Puoliosto hävityn kierroksen jälkeen: ostettu {_d(bought_pp)} "
            f"$/pelaaja eli vähintään {thresholds.force_buy_min} $, "
            f"{counters} -- aseistettuja vähintään {armed_min} ja "
            f"ostokykyisiä vähintään {buyers_min}, eli ostettiin, mutta "
            f"jätettiin varaa seuraavalle kierrokselle. ({bonus_note}.)"
            f"{scaled} {basis}",
            inputs,
        )

    return Decision(
        "force",
        f"Force hävityn kierroksen jälkeen: ostettu {_d(bought_pp)} "
        f"$/pelaaja eli vähintään {thresholds.force_buy_min} $, {counters} "
        f"-- ostokykyisiä on alle {buyers_min}, eli ostettu tyhjäksi: "
        f"seuraavalle kierrokselle ei jätetty varaa. ({bonus_note}.)"
        f"{scaled} {basis}",
        inputs,
    )


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
    observed = row.get("players_buy_end")
    readable = None if observed is None else int(observed)
    if readable is not None and 1 <= readable <= thresholds.roster_size:
        return readable, readable, True
    return thresholds.roster_size, readable, False


def _inputs(
    row: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    thresholds: ThresholdSettings,
    economy: EconomySettings,
    players: int,
    readable: int | None,
    loss_count: int,
) -> dict[str, Any]:
    """Kokoa päätöksen lähtöarvot ``CLASSIFIED_INPUTS``-rakenteeseen.

    Ostettu summa ei ole omana kenttänään: se on erotus
    ``equip_buy_end - equip_round_start``, ja molemmat ovat mukana.
    Käytettävissä ollut raha on vastaavasti ``money_buy_end + money_spent``.
    Kumpikin on siis jäljitettävissä ilman skeemamuutosta.

    ``loss_bonus_if_lost`` ja ``players_can_buy`` lasketaan **joka
    kierrokselle**, ei vain sille haaralle joka niitä lukee. Kierroslista on
    silloin luettavissa yhtenä tauluna: lukija voi verrata forcen ja
    puolioston laskureita myös niihin kierroksiin, joilla luokka ratkesi
    muualla. ``players_can_buy`` on ``None`` vain, jos jakaumaa ei saatu.
    """
    money_players = row.get(MONEY_DISTRIBUTION_COLUMN)
    if money_players is not None:
        money_players = [_i(money) for money in money_players]

    # Häviöbonus ja ostokykyisten laskuri lasketaan vain silloin, kun ne
    # tarkoittavat jotain:
    #
    #   * Jatkoajassa (round_no > regulation_rounds) tämän moduulin talousmalli
    #     ei päde lainkaan -- aloitusraha on eri eikä eco-sykliä ole (ks.
    #     "Tunnetut rajaukset"). Bonusluku siellä olisi tästä mallista lainattu
    #     ja lukisi kuin havainto.
    #   * Puoliajan viimeisellä kierroksella raha ei siirry seuraavalle
    #     kierrokselle (ks. :func:`_money_carries_over`), joten kysymys "onko
    #     ensi kierroksella varaa" on merkityksetön.
    #
    # Molemmissa kenttä jää tyhjäksi. Tyhjä on tässä väite: lukua ei ole,
    # eikä sitä pidä lukea kierroslistalta ikään kuin se olisi.
    round_no = row.get("round_no")
    applies = round_no is not None and int(round_no) <= thresholds.regulation_rounds
    if applies:
        applies = _money_carries_over(int(round_no), thresholds)

    bonus = loss_bonus_if_lost(loss_count, thresholds, economy) if applies else None
    can_buy = (
        players_who_can_buy(money_players, bonus, thresholds, economy)
        if applies
        and money_players
        and not any(money is None for money in money_players)
        else None
    )
    return {
        "money_buy_end": _i(row.get("money_buy_end")),
        "money_spent": _i(row.get("money_spent")),
        "money_players": money_players,
        "equip_buy_end": _i(row.get("equip_buy_end")),
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
        "players_armed": _i(row.get(ARMED_COLUMN)),
        "loss_bonus_if_lost": bonus,
        "players_can_buy": can_buy,
        "full_equip_min": thresholds.full_equip_min,
        "force_buy_min": thresholds.force_buy_min,
        "armed_players_min": thresholds.armed_players_min,
        "normal_buy_money_min": thresholds.normal_buy_money_min,
        "normal_buy_players_min": thresholds.normal_buy_players_min,
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
    equip = row.get("equip_buy_end")
    start = row.get("equip_round_start")
    bought = None if equip is None or start is None else int(equip) - int(start)
    parts = [
        f"Käytettävissä {_pp(available_money(row), players)}"
        f" (jäljellä {_pp(row.get('money_buy_end'), players)}"
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


def _listing_money(values: list[int] | tuple[int, ...]) -> str:
    """Rahajakauma sellaisenaan, jotta laskuri on tarkistettavissa.

    Pelkkä "0/5 pystyy ostamaan" ei kerro, kuinka läheltä viisi muuta
    jäivät -- eikä sitä voi tarkistaa demoa vasten ilman lukuja.

    Yksikkö toistetaan **jokaisessa** luvussa. Pelkkä lopun dollarimerkki
    ("1750, 500, 150, 0, 0 $") lukisi kuin se koskisi vain viimeistä.
    """
    return ", ".join(f"{int(v)} $" for v in values)


def _names(values: list[str]) -> str:
    """Puuttuvien havaintojen nimet luettavana listana."""
    return ", ".join(values)


def _i(value: Any) -> int | None:
    return None if value is None else int(value)
