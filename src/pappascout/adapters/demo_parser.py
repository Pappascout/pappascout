"""demoparser2-toteutus kaikille kuudelle parse-taululle (AD-8).

**Tämä on ainoa moduuli, jossa pelin propinimet esiintyvät.** Vaihe näkee vain
:class:`~pappascout.adapters.protocols.DemoParser`-portin, joten demoparser2:n
vaihtaminen tai päivittäminen ei kosketa putkea.

Kaikki alla käytetyt kentät on **todettu oikeasta demosta** (ks.
``_bmad-output/implementation-artifacts/demoparser2-kentat.md``), ei arvattu.

Kierrosrajat
------------
Kierros rajautuu kahden tapahtuman väliin:

``round_freeze_end``
    Freezetimen loppu, kierroksen **ankkuri**. Siitä lasketaan kaikki ajat
    (``t_s``) ja siitä alkaa ostoaika. Puoli luetaan tästä hetkestä.
``round_end``
    Kierroksen ratkeaminen. Tässä hetkessä luetaan voittaja, voiton syy,
    eloonjääneet ja heidän varusteensa. Pelaajat eivät ole vielä syntyneet
    uudelleen -- ``round_officially_ended`` olisi liian myöhään, siinä
    kaikki kymmenen ovat jo elossa.

Ostoaika
--------
**Talousarvoja ei lueta ankkurista.** CS2:n ostoaika jatkuu freezetimen
päättymisen jälkeen, ja **noin puolella kierroksista ostetaan vielä silloin**:
mitattuna viidestä liigademosta 52 kierroksella 106:sta. Osuus vaihtelee
ottelusta toiseen rajusti -- Anubiksessa se oli 20 kierrosta 22:sta eli 91 %,
Nukella 7/23 eli 30 % -- joten "noin puolet" on aineiston keskiarvo eikä luku,
johon yksittäisen demon voi olettaa asettuvan. Ankkurista luettu varustearvo
aliarvioi kaluston, yliarvioi taskuun jääneen rahan ja antaa liian pienen
luvun aseistetuille.

Talousarvot luetaan siksi **ostoajan lopusta**::

    buy_end_tick = min(freeze_end_tick + buy_window_seconds,
                       ensimmäistä kuolemaa edeltävä tick,
                       kierroksen loppu)

Kolme asiaa, jotka tässä on todettu demosta eikä oletettu:

* **``m_unFreezetimeEndEquipmentValue`` ei päivity freezetimen jälkeen.** Se on
  pelin oma tilannekuva ankkurihetkestä, joten myöhemmältä tickiltä luettuna se
  antaa täsmälleen saman luvun. Ostoajan lopun kalusto on siksi luettava
  propista ``m_unCurrentEquipmentValue``. Ankkurihetkellä nämä kaksi ovat sama
  luku, joten ikkuna 0 s antaa saman tuloksen kuin ennen tätä muutosta.
  (Mitattu: ``inferno_vs_ryhmarama`` kierros 6, ankkuri 11 550 $ molemmilla
  propeilla; +2 s freezetimen prop yhä 11 550, current 15 350 -- ja 15 350 on
  se luku, jonka käyttäjä luki demosta.)
* **Kuolema tyhjentää tavaraluettelon ja panssarin.** Kuolleen pelaajan
  ``inventory`` on ``[]`` ja ``m_ArmorValue`` 0 heti kuolintickillä, joten
  häntä ei saa lukea: kalustolaskuri putoaisi. Siksi ikkuna katkeaa
  ensimmäistä kuolemaa **edeltävään** tickiin eikä kuolintickiin.
  (Varustearvo sen sijaan ei nollaudu kuolemasta, mutta se ei muuta sääntöä.)
* **Yksi tick koko kierrokselle.** Kun mittaushetkellä kukaan ei ole vielä
  kuollut, kukaan ei ole myöskään ehtinyt pudottaa asetta kuollessaan --
  kaksoislaskenta (joukkuekaveri poimii vainajan kiväärin) on siis
  rakenteellisesti poissuljettu, eikä pelaajakohtaista "viimeinen elossa"
  -pistettä tarvita. Mittaushetkellä joukkue on koskematon.

Katkaisu **laukeaa noin puolella kierroksista** eikä ole reunatapaus:
kuudessa demossa (134 pelattua kierrosta) se osuu 69 kierrokseen eli 51 %:iin. Aineistossa
yksikään kuolema ei silti edellä viimeistä ostoa. Koska päällekkäisyys on
mahdollinen -- ostaminen on valmis 8 s mennessä 92 %:ssa niistä kierroksista
joilla ostettiin ankkurin jälkeen, ja aikaisin kuolema on 9,8 s -- katkaisun
hinta mitataan joka ajolla: ``buy_window_purchases_after_cut`` kertoo, montako
pelaajaa osti vielä katkaisupisteen jälkeen, ja ``buy_window_cuts_unchecked``
sen, montaako katkaisua ei voitu tarkistaa lainkaan. Molempien kuuluu olla
nolla.

``round_end`` **on olemassa** demoparser2 0.42.0:ssa, vaikka se ei näy
``list_game_events()``-listalla. Se palauttaa sarakkeet ``round``, ``tick``,
``winner`` ja ``reason``, ja ensimmäinen rivi on tyhjä alkuarvo (tick 1).
``round`` on demon oma kierroslaskuri (Ancient 1..22, puukkokierros mukaan
lukien), ja se päätyy sellaisenaan ``round_raw``-sarakkeeseen.

Kierrosnumeroa **ei päätetä täällä**: adapteri palauttaa ``round_no``-sarakkeen
tyhjänä, ja ``stages.parse`` kutsuu ``domain.rounds.mark_played_rounds``ia.

Yksi kierrosraja ratkeaa silti jo täällä: **ottelun uudelleenaloitus**.
Liigademoissa puukkokierroksen jälkeen tulee oma ``round_freeze_end`` ilman
yhtään ``round_end``iä, ja peli jatkuu sen jälkeen normaalisti. Se ei ole
kierros, ja koska sillä ei ole demon omaa numeroa, se ei voi saada
``round_raw``:takaan -- ilman sitä ``stages.parse`` ei voisi tunnistaa sen
rivejä. Siksi se jää tässä numeroimattomaksi eikä tuota riviä yhteenkään
tauluun; lukumäärä kulkee diagnostiikassa ajon yhteenvetoon.

Tunnistus on **havaintoihin eikä sijaintiin** perustuva: uudelleenaloituksella
ei ole ``round_end``iä *ja* demon oma numerointi jatkuu sen yli yhdellä. Kumpi
tahansa ehto rikki keskeyttää parsinnan sen sijaan että kierros pudotettaisiin
hiljaa. Ks. :meth:`Demoparser2Adapter._assign_round_raw` ja
:meth:`Demoparser2Adapter._match_restarts`.

Uudelleenaloituksen aikana heitetty utility ei kuulu millekään kierrokselle:
sillä ei ole kierrosikkunaa, joten heitto päätyy lukuun
``grenades_outside_rounds`` -- samaan, jossa lämmittelyheitot ovat. Luku on
siis liigademossa normaalisti hieman suurempi kuin vanhassa demossa, eikä se
ole vika.

Pistemäärän mittauspisteet
--------------------------
``score_start`` luetaan kierroksen omasta freezetime-ankkurista ja ``score_end``
**seuraavan kierroksen** ankkurista. Syy on puukkokierros: sen tuottaman
pisteen näkee vielä sen omassa ``round_end``-tickissä, mutta ``mp_restartgame``
nollaa sen heti perään -- oman lopputickin lukema väittäisi puukkokierrosta
pelatuksi. Seuraavan ankkurin lukema on nollauksen jälkeinen ja siksi oikea.

Viimeisellä kierroksella seuraavaa ankkuria ei ole, joten sen ``score_end``
luetaan omasta ``round_end``-tickistä. Se on turvallista: pistemäärä on siinä
hetkessä jo kasvanut (todennettu molemmista testidemoista), eikä nollausta
enää tule. Sama varalähde on käytössä myös silloin, kun seuraavalta
kierrokselta puuttuu ankkuri.

Kenen arvot summataan
---------------------
Ostoajan lopun summat (raha, käytetty raha, varustearvo, kierroksen alun
varustearvo) lasketaan vain niistä pelaajista, joiden **kaikki** nämä propit
ovat luettavissa, ja ``players_buy_end`` on saman joukon koko. Jakaja on
siis aina sama joukko kuin osoittaja: kolmen pelaajan summa viidellä jaettuna
näyttäisi ecolta, vaikka joukkue olisi ostanut täyden.

``players_armed_buy_end`` lasketaan **samasta joukosta**: montako pelaajaa
oli aseistettu ostoajan lopussa. Summa ei kerro sitä -- kaksi AK:ta ja kolme
tyhjää antaa saman summan kuin viisi puolinaista.

``money_players_buy_end`` on samasta joukosta myös: **rahasaldot yksi pelaaja
kerrallaan**, laskevasti lajiteltuna. Arvot ovat samat, jotka
``money_buy_end`` summaa; tässä ne vain säilytetään. Syy on sama kuin
kalustolaskurilla: joukkue jolla yhdellä on 5 000 ja neljällä nolla saa saman
summan kuin joukkue jolla kaikilla on 1 000, mutta jälkimmäisessä kaikki
viisi pystyvät ostamaan seuraavalla kierroksella ja edellisessä yksi. Juuri
se erottaa puolioston forcesta (``domain.economy``, ehto B).

Järjestys on lajiteltu eikä pelaajajärjestys: rivillä ei ole pelaajien
tunnisteita, joten alkion paikka ei kerro kenestä on kyse. Lajittelu tekee
lukemasta toistettavan riippumatta siitä, missä järjestyksessä tickin rivit
sattuvat tulemaan.

Aseistettu = **panssari ja vähintään yksi ase hallussa**. Ase luetaan pelaajan
tavaraluettelosta (``inventory``) ja panssari propista ``m_ArmorValue``, eikä
varustearvosta: varustearvo on ase + panssari + kranaatit yhtenä lukuna, joten
Glock + kevlar + kaksi valoa (1250 $, mitattu Ancientista) näyttäisi
aseistetulta ilman yhtään asetta.

**Hallussapito, ei ostos.** Tavaraluettelo luetaan ostoajan lopusta, joten
edelliseltä kierrokselta säästetty tai vainajalta poimittu kivääri laskeutuu
samoin kuin juuri ostettu. Kierroksen kannalta ratkaisee mitä kädessä on, ei
mistä se tuli. Oletuspistoolit rajataan silti ulos: ne saa joka kierros
ilmaiseksi, joten niiden hallussapito ei kerro mitään.

**Lukukelvoton havainto tyhjentää koko rivin.** Jos yhdenkin luettavan pelaajan
panssari tai tavaraluettelo puuttuu, laskuri on ``null`` -- ei se luku, joka
saataisiin lopuista. Pelaaja pysyy ``players_buy_end``in jakajassa, joten
hiljainen pudotus näyttäisi säästökierrokselta eikä lukuvirheeltä.

``players_armored_buy_end`` on **sama lukema eri ehdolla**: montako samasta
joukosta kantoi panssaria (``m_ArmorValue > 0``) samalla tickillä. Se ei ole
aseistettujen laskurin yleistys vaan oma havaintonsa, koska ne vastaavat eri
kysymyksiin: aseistettu on puolioston kalibroitu ehto A, panssaroitu vastaa
kysymykseen "monellako oli panssari".

**Hallussapito, ei ostos** -- sama sääntö kuin aseistettujen laskurilla.
Panssari säilyy kierroksen yli hengissä selvinneellä, myös vaurioituneena
(37/100 on yhä panssari), joten laskuri kertoo mitä pelaajilla oli, ei mitä he
ostivat. **Poikkeus on pistoolikierros** (1 ja 13): puoliaika alkaa puhtaalta
pöydältä eikä perintää ole, joten siellä -- ja vain siellä -- luku on
ostohavainto. Juuri siksi Veetin *"5 kevlaria"* on oikea luenta Nuken
T-pistoolista.

Pistoolikierroksella laskurit myös eroavat eniten: 800 dollarin aloitusrahalla
kevlar (650) ja parannettu ase eivät mahdu samaan ostokseen, joten aseistettuja
on käytännössä 0, vaikka kaikilla viidellä olisi kevlar. Sääntö se ei ole:
poimittu ase riittää aseistamaan, ja mitattu vastaesimerkki on
``Anubis_vs_ryhmarama`` kierros 13, jolla CT-puolen laskurit ovat 3 ja 1.

Panssarilaskurin luettavuusehto on **kapeampi**: vain ``m_ArmorValue``
(:data:`_ARMORED_PROPS`). Tavaraluettelo ei kuulu siihen, koska laskuri ei lue
sitä; lukukelvoton tavaraluettelo tyhjentää siis aseistettujen laskurin muttei
panssarilaskuria. Kumpikin tyhjentyminen on omassa diagnostiikkaluvussaan,
jotta ero näkyy ajossa eikä vasta raportissa. Kypärää ei eroteta: analyysi
puhuu kevlarista, ja kypärä on eri havainto.

Luokittelu on **sallittujen aseiden luettelo** (:mod:`pappascout.constants`),
ei kiellettyjen: tuntematon nimi ei ole ase. Veitset ovat avoin joukko, jota
Valve kasvattaa, aseet suljettu. Tuntemattomat nimet kulkevat diagnostiikkaan
ja sieltä ajon yhteenvetoon -- hiljainen pudotus olisi yhtä paha kuin
hiljainen hyväksyntä.

Näytepisteet
------------
Sama lukukerta tuottaa myös ``ticks``-taulun: rivi per (pelaaja, kierros,
näytepiste). Näytepisteet valitsee :mod:`pappascout.domain.sampling`, joka on
puhdas funktio -- adapterin osuus on lukea propit valituilta tickeiltä ja
kertoa domainille, kummalla puolella kukin pelaaja on.

Kierrosrajat, kokoonpanot ja tickrate lasketaan **kerran** ja käytetään
molempiin tauluihin. Siksi portti palauttaa ne yhdessä
(:class:`~pappascout.adapters.protocols.DemoTables`): kaksi erillistä kutsua
tekisi kokoonpanojen tunnistuksen kahdesti, ja jos tulokset joskus eroaisivat,
``lineup_key`` olisi tauluissa eri eikä liitos enää osuisi.

Joukkueen ja pelaajien nimet
----------------------------
Sama lukukerta tuottaa myös ``lineups``-taulun: rivi per (kokoonpano, pelaaja),
jossa pelaajan nimi ja hänen klaaninimensä. Molemmat luetaan **samoilta
ankkuritickeiltä**, joilta kokoonpanot jo tunnistetaan -- demoa ei lueta
uudelleen, ja propit ovat samassa ``parse_ticks``-kutsussa.

**Klaani luetaan pelaajakohtaisesti, ei puolen kautta.** Mitattu 2026-08-30
viidellä demolla: ``team_clan_name`` antaa jokaiselle SteamID:lle täsmälleen
yhden klaanin kaikilla ankkureilla, myös puoliajan vaihdon yli. Puolen
(``m_iTeamNum``) kautta luettuna sama arvo vaihtaa joukkuetta puoliajalla --
``team_num=2`` on 1. puoliajalla ``KALJUKOSTAJA`` ja 2. puoliajalla
``MatureMayhem``. Se on ansa, jonka pelaajakohtainen luku välttää.

``lineup_key`` **ei muutu**: se lasketaan edelleen pelkistä SteamID:istä
(:meth:`_Lineup.key`), joten nimien lisääminen ei siirrä yhtäkään arkiston
hakemistoa.

Tapot ja kuolemat
-----------------
Sama lukukerta tuottaa myös ``deaths``-taulun: rivi per kuolema, uhri ja
ampuja molemmat alueineen ja koordinaatteineen. ``player_death`` luetaan
**kerran** kutsulla, joka pyytää pelaajakohtaiset kentät::

    parse_event("player_death", player=["last_place_name", "X", "Y", "Z",
                                        "team_num"])

Kirjasto palauttaa ne etuliitteillä ``user_*`` (uhri), ``attacker_*`` ja
``assister_*``. Sama tulos kelpaa myös ostoikkunan rajaamiseen ja
ensikontaktin varalähteeksi, joten tapahtumaa ei lueta kahdesti.

**Mitattu 2026-08-30, ``Ancient_vs_kaljukostaja``.** Rivejä on 151.
Kattavuus: ``user_last_place_name`` 151/151, ``attacker_last_place_name``
149/151, ``assister_last_place_name`` 58/151. Avustaja jätetään pois: se on
puolet tyhjää eikä yksikään tavoiteanalyysin rivi nojaa siihen. Ne kaksi
riviä, joilta ampujan alue puuttuu, ovat **samat kaksi**, joilta ampuja
puuttuu kokonaan (``planted_c4``) -- alue ei siis kadonnut, ampujaa ei ollut.

Alue on **havainto molemmilta**: se tulee samalta tapahtumalta kuin kuolema
itse, joten sitä ei johdeta mistään eikä taulussa ole ``area_source``ia.

Puoli ja kokoonpano luetaan **kierroksen omasta puolikuvauksesta**
(:meth:`Demoparser2Adapter._assign_sides`) samalla :func:`_side_lookup`illa
kuin utilityssä, ei tapahtuman ``team_num``-kentästä. Syy on yhdenmukaisuus:
tapahtumasta luettuna yksi poikkeava lukema panisi kuoleman eri joukkueelle
kuin mitä ``ticks`` ja ``events`` sanovat samasta pelaajasta samalla
kierroksella. Tapahtuman oma ``team_num`` on **varalähde** niille pelaajille,
joita ei ole kummassakaan kokoonpanossa eikä kierroksen ankkuritickillä --
kesken karttaa tullut tai uudelleenyhdistänyt pelaaja.

Kierros ratkeaa samasta jaksotuksesta kuin utilityssä
(:func:`_round_windows`, ankkuri = viimeinen ``round_freeze_end``), ja
``round_no`` jää tyhjäksi: numeroinnin omistaa ``stages.parse``.
Puukkokierroksella kuollaan oikeasti, ja juuri siksi sen rivit putoavat
samassa liitoksessa kuin näytepisteet ja kranaatit -- erillistä
puukkokierrossääntöä ei ole eikä saa olla.

Utility
-------
``grenade_thrown``-tapahtumaa **ei ole olemassa**, joten utility luetaan
``parse_grenades()``-lentoradoista: radan ensimmäinen piste on heitto ja
viimeinen räjähdys. Taulu on demon suurin yksittäinen erä -- Ancientissa
1 553 329 riviä -- ja se pelkistetään kahteen riviin per kranaatti heti
:func:`~pappascout.domain.utility.grenade_endpoints`illa, jolloin eteenpäin
kulkee noin 750 riviä.

Kaksi asiaa raakadatassa yllättää, ja molemmat on todettu Ancient-demolla:

* **Suurin osa riveistä ei ole lentorataa.** Kranaatti saa rivin myös pelaajan
  repussa ollessaan, ja silloin ``x, y, z`` ovat tyhjiä; 1,34 miljoonaa riviä
  1,55:stä on tällaisia. Lennossa tyyppi on ``...Projectile``, repussa ei.
* **``grenade_entity_id`` kierrätetään.** 374 lentorataa mahtuu 187
  tunnisteeseen. Kierrätys ei rajoitu kierrosten väliin: liigademossa
  ``inferno_vs_ryhmarama`` kierroksella 11 tunniste 564 kantaa kolme eri
  lentorataa (molotov 9,2 s, flashbang 18,0 s ja incendiary 64,2 s).
  Jaksotus on siksi ``grenade_endpoints``in vastuulla, ei ryhmittelyn
  tunnisteen mukaan, ja tauluun kulkee sen antama ``grenade_no``.

Lennossa molotov ja incendiary ovat molemmat ``CMolotovProjectile``. Erottelu
tehdään heittäjän repussa olevasta tyypistä heittoa edeltävällä tickillä
(``CMolotovGrenade`` / ``CIncendiaryGrenade``); jos se ei ratkea yksiselitteisesti,
tyypiksi jää ``molotov``.

Räjähdyksen paikka on ristiintarkistettu demon omiin tapahtumiin
(``smokegrenade_detonate``, ``hegrenade_detonate``, ``flashbang_detonate``):
radan viimeinen piste osuu niihin 0,024 pelin yksikön tarkkuudella kaikissa
281 tapauksessa. Tapahtumia ei silti lueta ajossa -- rata riittää, ja kolme
ylimääräistä tapahtumalukua maksaisi ilman lisätietoa.

Pistepilvi ja räjähdyksen alue
------------------------------
Sama lukukerta tuottaa myös ``callouts``-taulun: ruudukon siitä, missä
pelaajat ovat kartalla oikeasti seisoneet ja mikä alue kussakin kohdassa on.
Se on **räjähdysalueiden lähde**, ja se on tallessa juuri siksi, että johdettu
alue olisi tarkistettavissa demoa vasten.

**Mikä poistui ja miksi.** Story 2.2 johti räjähdysalueen lähimmästä elossa
olevasta pelaajasta. Se ei ollut epätarkka vaan rakenteellisesti väärä: savu
heitetään sinne, missä ketään ei ole -- juuri siksi, että se estää näkyvyyden
ja pakottaa rotaatioita. Proxy mittasi päinvastaista kuin piti, ja **42 %
räjähdyksistä jäi kokonaan ilman aluetta** (mitattu neljästä liigademosta,
1 716 räjähdystä); pistepilvellä osuus on 6,4 %. Menetelmää ei jätetty
rinnalle varalähteeksi: kaksi menetelmää tekisi rivistä tulkitsemattoman.

**Mitä se maksaa, mitattuna.** Pilvi vaatii ainoan koko demon kattavan
tickiluvun tässä moduulissa::

    parse_ticks([m_szLastPlaceName, X, Y, Z, m_lifeState])

``Ancient_vs_kaljukostaja`` 2026-08-30: **2,1 s, 1 529 910 riviä**, joista
elossa ja alue tiedossa 1 092 083. Aineisto pudotetaan ruudukoksi heti -- 32
yksikön ruutuun mahtuu 7 703 ruutua ja 18 aluetta.

**Muistihuippu on 1,0 GB, ja se on kirjaston eikä tämän moduulin.** Mitattu
``Nuke_vs_imuaijat`` (1 914 720 riviä) prosessin ``PeakWorkingSetSize``illa:
lähtötaso 48 MB, ``parse_ticks``in jälkeen 705 MB ja huippu **1 043 MB** jo
kutsun sisällä; oma Polars-muunnoksemme lisää siihen 44 MB (705 -> 749) ja
ruudukon rakentaminen 134 MB. Huippu syntyy siis demoparser2:n omasta
kehyksestä, jossa on 1,9 miljoonaa riviä ja kahdeksan saraketta -- pyydetyt
viisi propia sekä kirjaston aina lisäämät ``tick``, ``steamid`` ja ``name``.

``del`` pudottaa vain nimen eikä palauta muistia käyttöjärjestelmälle:
mitattu työjoukko ei pienene ``del frame``in jälkeen lainkaan. Lupaus on siis
täsmälleen se, mitä se on -- **aineisto ei elä pidempään kuin rakentaminen
vaatii**, jolloin varaaja voi käyttää alueen uudelleen -- eikä "muisti
vapautuu".

**Luku on ehdoton, ja se on vaihtokauppa.** Pilvi rakennetaan myös silloin,
kun demossa ei ole yhtään kranaattia: taulu on oma tuotoksensa, jonka
``parse`` lupaa kirjoittaa, eikä sen olemassaolo saa riippua siitä sattuiko
joku heittämään savun. Hinta on noin 2 s ja noin 1 GB huippu per demo.
Kytkintä ei ole: ehdollinen pilvi tekisi ``callouts.parquet``ista joskus
olemassa olevan ja joskus puuttuvan, ja ``parse``in ohitussääntö (jokainen
odotettu tulos paikallaan) muuttuisi arvattavasta arvaamattomaksi.

**Kynnys ei poistu.** "Lähin ruutu löytyy aina" ei ole kattavuutta: mitattu
maksimietäisyys on 1 074 yksikköä, ja ilman kynnystä raportti väittäisi
aluetta räjähdykselle, joka tapahtui kaukana kaikesta, missä yksikään pelaaja
on koskaan seissyt. ``[parse].area_snap_units`` on siksi tallella,
kalibroituna pistepilveä varten uudelleen.

Räjähdyksen tickeiltä **ei enää lueta pelaajia**: alue tulee pilvestä, ei
hetkestä. Heiton tickit luetaan yhä, koska heittäjän oma alue on havainto.

Kontrolleri ja pawn ovat eri entiteettejä
-----------------------------------------
CS2:ssa pelaajalla on kaksi entiteettiä. **Kontrolleri**
(``CCSPlayerController``) edustaa pelaajaa -- nimi, joukkue, raha, pisteet --
ja säilyy koko ottelun. **Pawn** (``CCSPlayerPawn``) on hänen fyysinen
hahmonsa kartalla -- elossaolo, alue, koordinaatit, varustearvo, panssari --
ja se katoaa, kun pelaaja ei ole pelissä. Propin etuliite kertoo kummasta on
kyse, ja se on luettava jokaisesta tarkistuksesta: kontrollerin kentän
löytyminen **ei** todista, että pelaaja on kartalla.

**Mitattu 2026-08-31, ``anubis_vs_RCAVE_VETERANS``** (kierros 19, pelaaja
``egerrrrr`` / 76561199635619622): kontrollerin ``m_iTeamNum`` on 3, mutta
jokainen pawn-kenttä on tyhjä samoilla tickeillä -- ``m_lifeState``,
``m_szLastPlaceName`` ja ``X``/``Y``/``Z``. Pawnittomia rivejä on **15**
(viisi näytepisteiden tickeiltä, kymmenen heittojen tickeiltä); arkiston
seitsemässä muussa demossa niitä on nolla. Näiden rivien ohittaminen on
:meth:`Demoparser2Adapter._read_sample_ticks`in työtä, ja ohitus vaatii
**kaikkien** pawn-kenttien puuttumisen -- yksi puuttuva kenttä on kirjaston
muutos eikä pelaajan tila.

Muistinkäyttö
-------------
Demoa ei ladata muistiin kokonaan. ``parse_ticks`` kutsutaan **vain
kierrosrajojen, ostoaikojen loppujen, näytepisteiden ja kranaattien heittojen
tickeille** (Ancient: 44 + 21 + noin 100 + noin 375 tickiä), ei koko
tickisarjalle. Näitä kohdennettuja kutsuja on neljä eikä yksi -- pistepilven
koko demon luku on viides ja oma tapauksensa, ks. alla -- koska sekä ostoajan
loppu että
näytepisteiden tickit riippuvat tickratesta, joka mitataan vasta kierrosrajojen
lukemisesta, ja kranaattien tickit selviävät vasta lentoradoista. Pakattu demo
puretaan virtaavasti temp-tiedostoon.

**Yksi poikkeus, ja se on tarkoitus.** Pistepilvi luetaan koko demon
tickisarjasta (:data:`CLOUD_TICK_PROPS`), koska kysymys on "missä kartalla on
seisottu ja mikä alue se on" eikä "missä joukkue oli tällä hetkellä". Se on
yksi kutsu, viisi kevyttä proppia ja 2,1 sekuntia, ja tulos pelkistetään
muutamaan tuhanteen ruutuun ennen kuin mitään muuta tehdään. Pilven laajuus
on koko demo myös tarkoituksella: lämmittelyn ja puukkokierroksen rivit
kertovat kartasta yhtä paljon kuin pelattujen kierrosten.

Ostoikkuna maksaa yhden ylimääräisen ``parse_ticks``-kutsun (Ancient: 21
mittauspistettä) ja yhden ``parse_event("player_death")``-kutsun. Jälkimmäinen
tehtiin ennen vain silloin, kun ensikontaktin varasääntö oli päällä; nyt se
tehdään aina, koska ikkunan katkaisu ei saa riippua ensikontaktin asetuksesta.
Tapahtumaluku on kertaluokkia halvempi kuin tickiluku, ja se tehdään kerran ja
jaetaan molemmille käyttäjille.
"""

from __future__ import annotations

import hashlib
import statistics
import warnings
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from pappascout.adapters.decompress import readable_demo
from pappascout.adapters.protocols import (
    CALLOUTS_ADAPTER_COLUMNS,
    DEATHS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    MATCH_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
    ParseDiagnostics,
)
from pappascout.constants import ARMING_WEAPONS, KNOWN_INVENTORY_ITEMS
from pappascout.domain.sampling import (
    FIRST_CONTACT_SAMPLE,
    DamageEvent,
    RoundBounds,
    SamplePoint,
    first_contact_tick,
    sample_ticks,
    seconds_since_freeze_end,
)
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    CALLOUT_CLOUD,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    MONEY_DISTRIBUTION_COLUMN,
    ROUNDS,
    TICKS,
)
from pappascout.domain.utility import (
    DETONATE,
    THROWN,
    build_point_cloud,
    empty_point_cloud,
    flight_point,
    grenade_endpoints,
    nearest_cells,
    trajectory_gap_ticks,
)
from pappascout.errors import ParseError

__all__ = [
    "Demoparser2Adapter",
    "TEAM_SIDES",
    "TICK_PROPS",
    "SAMPLE_TICK_PROPS",
    "SAMPLE_PAWN_PROPS",
    "CLOUD_TICK_PROPS",
    "DAMAGE_COLUMNS",
    "DEATH_PLAYER_PROPS",
    "DEATH_COLUMNS",
    "GRENADE_COLUMNS",
    "GRENADE_TYPES",
    "FIRE_ITEM_TYPES",
    "MOLOTOV_PROJECTILE",
    "DEFAULT_TICK_RATE",
    "TICK_RATE_MIN",
    "TICK_RATE_MAX",
    "MAX_MATCH_RESTARTS",
]

# -- Pelin kentät -------------------------------------------------------------

_TEAM_NUM = "CCSPlayerController.m_iTeamNum"
_ACCOUNT = "CCSPlayerController.CCSPlayerController_InGameMoneyServices.m_iAccount"
_CASH_SPENT = (
    "CCSPlayerController.CCSPlayerController_InGameMoneyServices"
    ".m_iCashSpentThisRound"
)
_EQUIP_ROUND_START = "CCSPlayerPawn.m_unRoundStartEquipmentValue"

#: Pelaajan kaluston arvo **juuri nyt**. Tämä on ostoajan lopun varustearvon
#: lähde, ei ``m_unFreezetimeEndEquipmentValue``: jälkimmäinen on pelin
#: tilannekuva ankkurihetkestä eikä päivity freezetimen jälkeen, joten
#: myöhemmältä tickiltä luettuna se antaisi yhä ankkurin luvun ja koko korjaus
#: jäisi näkymättömäksi. Ankkurilla nämä kaksi ovat sama luku.
_EQUIP_CURRENT = "CCSPlayerPawn.m_unCurrentEquipmentValue"
_ARMOR_VALUE = "CCSPlayerPawn.m_ArmorValue"

#: Pelaajan tavaraluettelo: lista esineiden näyttönimiä (``AK-47``,
#: ``Smoke Grenade``, ``knife_t``, veitsiskinien omat nimet). Ei propinimi vaan
#: demoparser2:n oma johdettu sarake, ja ainoa lähde, josta näkee **mikä** ase
#: pelaajalla on -- varustearvo kertoo vain paljonko kalusto maksoi.
_INVENTORY = "inventory"

_LIFE_STATE = "CCSPlayerPawn.m_lifeState"
_TEAM_SCORE = "CCSTeam.m_iScore"
_ROUND_START_TIME = "CCSGameRulesProxy.CCSGameRules.m_fRoundStartTime"

#: Pelin oma aluenimi (``env_cs_place``). Noin kaksi kertaa karkeampi kuin
#: Total CS -callout; tyhjä merkkijono tarkoittaa aluetta, jolle peli ei anna
#: nimeä, ja se säilyy taulussa ``null``:na.
_PLACE_NAME = "CCSPlayerPawn.m_szLastPlaceName"

#: Pelaajan klaaninimi eli joukkueen nimi demossa. demoparser2:n oma johdettu
#: sarake (ei propinimi), ja ainoa lähde joukkueen nimelle -- tiedostonimestä
#: tai FACEIT-tunnisteesta sitä ei saa arvata.
#:
#: **Luetaan pelaajakohtaisesti.** Puolen kautta luettuna arvo vaihtaa
#: joukkuetta puoliajalla; SteamID:n kautta se on vakio koko kartan ajan
#: (mitattu 2026-08-30, viisi demoa, nolla poikkeusta).
_CLAN_NAME = "team_clan_name"

#: Pelaajan nimi. demoparser2 lisää sen jokaiseen ``parse_ticks``-tulokseen
#: automaattisesti ``steamid``in ja ``tick``in rinnalle, joten sitä ei pyydetä
#: propina -- mutta se **tarkistetaan** palautuneista sarakkeista, jottei
#: kirjaston muutos jättäisi nimiä hiljaa tyhjiksi.
_PLAYER_NAME = "name"

#: Pelaajan koordinaatit. demoparser2 palauttaa nämä valmiiksi float32:na.
_X = "X"
_Y = "Y"
_Z = "Z"

#: Propit, jotka luetaan kierrosrajojen tickeistä.
TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _CLAN_NAME,
    _ACCOUNT,
    _CASH_SPENT,
    _EQUIP_ROUND_START,
    _EQUIP_CURRENT,
    _ARMOR_VALUE,
    _INVENTORY,
    _LIFE_STATE,
    _TEAM_SCORE,
    _ROUND_START_TIME,
)

#: Propit, jotka luetaan näytepisteiden tickeistä. Lyhyempi lista kuin
#: kierrosrajoilla: asetelmasta tarvitaan vain paikka, puoli ja elossaolo --
#: talousarvot ovat kierroksen ominaisuus, eivät hetken.
#:
#: **Yksi näistä on kontrollerin kenttä ja neljä pawnin.** ``m_iTeamNum``
#: tulee ``CCSPlayerController``ilta ja on tallella myös pelaajalla, jolla ei
#: ole hahmoa kartalla; loput neljä ovat ``CCSPlayerPawn``in kenttiä ja
#: katoavat hänen mukanaan. Ero on tämän moduulin dokumentaatiossa mitattuna,
#: ja se on syy siihen, miksi
#: :meth:`Demoparser2Adapter._read_sample_ticks` katsoo neljää kenttää eikä
#: yhtä.
SAMPLE_TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _LIFE_STATE,
    _PLACE_NAME,
    _X,
    _Y,
    _Z,
)

#: :data:`SAMPLE_TICK_PROPS`in **pawn-kentät**, eli ne, jotka katoavat kun
#: pelaajalla ei ole hahmoa kartalla. Luettelo on oma vakionsa, koska
#: pawnittoman rivin ohitus vaatii, että jokainen näistä on tyhjä -- uusi
#: pawn-prop on lisättävä tähän, tai ohitus löysenisi hiljaa yhden kentän
#: verran.
SAMPLE_PAWN_PROPS: tuple[str, ...] = (
    _LIFE_STATE,
    _PLACE_NAME,
    _X,
    _Y,
    _Z,
)

#: Propit, jotka luetaan **koko demon** tickisarjasta pistepilveä varten.
#:
#: Lyhyempi lista kuin näytepisteillä: ``m_iTeamNum`` ei ole mukana, koska
#: pilvi on kartan ominaisuus eikä joukkueen -- kysymys on "missä tässä
#: kohdassa on seisottu ja mikä alue se on", eikä siihen vastaa se, kumpi puoli
#: siellä seisoi. Katsojarivit eivät pilaa pilveä: katsojalla ei ole
#: ``last_place_name``ia eikä hän ole elossa, joten suodatin pudottaa hänet
#: samalla ehdolla kuin kuolleen.
#:
#: **Tämä on moduulin ainoa koko tickisarjan luku.** Perustelu ja mitattu
#: hinta ovat moduulin dokumentaatiossa.
CLOUD_TICK_PROPS: tuple[str, ...] = (
    _PLACE_NAME,
    _X,
    _Y,
    _Z,
    _LIFE_STATE,
)

#: Sarakkeet, jotka ``parse_grenades()``-taulussa on oltava. ``name`` on
#: mukana kirjastossa mutta jätetään lukematta: pelaajan nimi voi vaihtua
#: kesken ottelun, ja tunniste on ``steamid``.
GRENADE_COLUMNS: tuple[str, ...] = (
    "grenade_type",
    "grenade_entity_id",
    "x",
    "y",
    "z",
    "tick",
    "steamid",
)

#: Pelin luokkanimi lennossa -> kanoninen kranaattityyppi.
#:
#: Nämä ovat ``parse_grenades()``in ``grenade_type``-arvot niillä riveillä,
#: joilla on koordinaatit. Tuntematon nimi säilyy sellaisenaan: se on
#: harvinainen mutta luettava tulos, kun taas tyhjäksi muuttaminen hukkaisi
#: havainnon.
GRENADE_TYPES: dict[str, str] = {
    "CSmokeGrenadeProjectile": "smoke",
    "CFlashbangProjectile": "flashbang",
    "CHEGrenadeProjectile": "he",
    "CMolotovProjectile": "molotov",
    "CDecoyProjectile": "decoy",
}

#: Lennossa molotov ja incendiary ovat **sama** luokka.
MOLOTOV_PROJECTILE = "CMolotovProjectile"

#: Repussa ne erottuvat. Tästä kranaatin oikea tyyppi saadaan takaisin.
FIRE_ITEM_TYPES: dict[str, str] = {
    "CMolotovGrenade": "molotov",
    "CIncendiaryGrenade": "incendiary",
}

#: ``m_iTeamNum`` -> puoli. 0 ja 1 ovat katsoja ja liittymätön, eivät joukkueita.
TEAM_SIDES: dict[int, str] = {2: "T", 3: "CT"}

#: Sarakkeet, jotka ``player_hurt``- ja ``player_death``-tapahtumissa on oltava.
#: Molemmat tarjoavat kaikki neljä demoparser2 0.42.0:ssa.
DAMAGE_COLUMNS: tuple[str, ...] = (
    "tick",
    "attacker_steamid",
    "user_steamid",
    "weapon",
)

#: Pelaajakohtaiset kentät, jotka pyydetään ``player_death``-tapahtumalta.
#:
#: Kirjasto palauttaa jokaisen näistä **kolmella etuliitteellä**: ``user_*``
#: (uhri), ``attacker_*`` ja ``assister_*``. Avustajaa ei lueta: se on puolet
#: tyhjää (58/151 mitattuna 2026-08-30) eikä yksikään tavoiteanalyysin rivi
#: nojaa siihen.
DEATH_PLAYER_PROPS: tuple[str, ...] = (
    "last_place_name",
    "X",
    "Y",
    "Z",
    "team_num",
)

#: ``player_death``in **pelaajakohtaiset** sarakkeet, etuliitteineen.
#:
#: Nämä tulevat :data:`DAMAGE_COLUMNS`-kenttien **lisäksi**, eivät niiden
#: tilalle: luettelot ovat erillisiä, koska ne korjataan eri paikoista, ja
#: :meth:`Demoparser2Adapter._damage_rows` nimeää puuttuvan sarakkeen sen
#: oman luettelon kanssa.
#:
#: Puuttuva sarake on virhe eikä tyhjä arvo: ilman tarkistusta kuolemataulu
#: olisi rakenteellisesti kelvollinen mutta alueeton, ja raportin
#: "ensimmäinen kuolema, useimmin Cave" -rivi katoaisi kertomatta miksi.
#: ``*_team_num`` on mukana varalähteenä puolelle, jota kokoonpanoista ei
#: löydy -- sekin on pakollinen, koska sen katoaminen näkyisi vain
#: pudotettuina riveinä.
DEATH_COLUMNS: tuple[str, ...] = tuple(
    f"{prefix}_{prop}"
    for prefix in ("user", "attacker")
    for prop in DEATH_PLAYER_PROPS
)

#: Elossa olevan pelaajan ``m_lifeState``. Muut arvot ovat kuollut tai kuolemassa.
_ALIVE = 0

#: CS2:n oletustickrate. Käytetään vain jos demosta ei saa mitattua arvoa.
DEFAULT_TICK_RATE = 64.0

#: Järkevyysrajat mitatulle tickratelle. CS2:n palvelimet ajavat 64 tai 128
#: tickiä; näiden ulkopuolinen arvo on mittausvirhe (esimerkiksi kellon nollaus
#: kesken ottelun), ei totuus.
TICK_RATE_MIN = 16.0
TICK_RATE_MAX = 256.0

#: Montako ottelun uudelleenaloitusta yhdessä demossa hyväksytään.
#:
#: Uudelleenaloitus on kierrosraja, jolla on freezetime-ankkuri mutta ei
#: ``round_end``iä, ja jonka **yli demon oma kierrosnumerointi jatkuu
#: yhdellä**. Liigaotteluissa niitä on tasan yksi, heti puukkokierroksen
#: jälkeen. Useampi tarkoittaisi ilmiötä, jota ei ole vielä nähty; silloin
#: parsinta pysähtyy eikä arvaa.
#:
#: Kaikki tästä johdetut viestit lukevat luvun täältä, jotta rajan nostaminen
#: ei jätä tekstejä valehtelemaan (ks.
#: :meth:`Demoparser2Adapter._match_restarts`).
MAX_MATCH_RESTARTS = 1


@dataclass
class _Lineup:
    """Yhden joukkueen kokoonpano yhdellä kartalla.

    ``members`` kasvaa kartan aikana, jos joukkue vaihtaa pelaajaa. Tunniste
    lasketaan kaikista kartalla pelanneista, jotta sama kokoonpano tuottaa
    saman avaimen ajosta toiseen.

    ``names`` ja ``clans`` ovat **pelaajakohtaisia** havaintolaskureita, eivät
    joukkuekohtaisia: klaaninimi luetaan SteamID:n kautta, koska puolen kautta
    luettuna se vaihtaisi joukkuetta puoliajalla (ks. moduulin
    dokumentaatio). Laskuri eikä yksi arvo siksi, että ristiriita ratkeaa
    havaintojen määrällä eikä lukujärjestyksellä -- ja tasatilanne
    aakkosjärjestyksessä, jotta ajo on toistettava.

    **Tunniste lasketaan yhä pelkistä SteamID:istä.** Nimien lisääminen ei saa
    muuttaa ``lineup_key``tä: se on arkiston hakemistorakenne.
    """

    members: set[str] = field(default_factory=set)
    names: dict[str, Counter[str]] = field(default_factory=dict)
    clans: dict[str, Counter[str]] = field(default_factory=dict)

    def observe(self, rows: Sequence[dict[str, Any]], side: str) -> None:
        """Kirjaa yhden tickin rivit tälle kokoonpanolle kuuluviksi.

        Ottaa mukaan vain annetun puolen rivit, eli täsmälleen saman joukon,
        joka ennen liitettiin ``members``iin joukko-operaatiolla.

        Tyhjä merkkijono ei ole nimi: ``_read_ticks`` on jo muuttanut sen
        ``None``:ksi, ja ``None`` jätetään kirjaamatta. Nimen puuttuminen on
        havainto, ja se näkyy taulussa ``null``:na eikä keksittynä arvona.
        """
        for row in rows:
            if row["side"] != side:
                continue
            steamid = row["steamid"]
            self.members.add(steamid)
            name = row.get("player_name")
            if name is not None:
                self.names.setdefault(steamid, Counter())[name] += 1
            clan = row.get("clan_name")
            if clan is not None:
                self.clans.setdefault(steamid, Counter())[clan] += 1

    def key(self) -> str:
        """Kokoonpanon tiiviste.

        Raises:
            ParseError: Jos kokoonpano on tyhjä. Tyhjän merkkijonon tiiviste
                olisi molemmilla joukkueilla sama, jolloin ``lineup_key`` ei
                erottaisi joukkueita lainkaan ja kaikki myöhempi ryhmittely
                menisi hiljaa väärin.
        """
        if not self.members:
            raise ParseError(
                "Demosta ei saatu tunnistettua kummankin joukkueen kokoonpanoa: "
                "toinen jäi tyhjäksi.\n"
                "Kierrosrajojen tickeistä ei löytynyt pelaajia molemmilta "
                "puolilta. Demo on todennäköisesti vioittunut tai katkennut."
            )
        raw = ",".join(sorted(self.members))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Segment:
    """Yksi kierrosraja demossa: pelattu kierros, ratkeamaton tai uudelleenaloitus.

    Attributes:
        demo_round: Demon oma kierrosnumero ``round_end``-tapahtuman
            ``round``-kentästä. ``None`` = segmentti ei ratkennut, tai
            demoparser2 ei antanut numeroa.
        freeze_end_tick: Kierroksen ankkuri, viimeinen ``round_freeze_end``
            ennen päättymistä. ``None`` = ankkuria ei ole, jolloin
            freezetimen lopun havaintoja ei voi lukea (``status`` kertoo sen).
        end_tick: Kierroksen ratkeamishetki ``round_end``-tapahtumasta.
            ``None`` = kierros ei ratkennut: demo katkesi kesken, tai
            segmentti ei ole kierros lainkaan.
        winner_side: Voittanut puoli (``"T"``/``"CT"``), tai ``None`` jos
            kierros ei ratkennut.
        win_reason: Voiton syy demon omalla nimellä, tai ``None`` samasta
            syystä kuin ``winner_side``.
        round_raw: Segmentille annettu demon oma kierrosnumero. ``None``
            tarkoittaa **ottelun uudelleenaloitusta**: se pelattiin, mutta se
            ei ole kierros eikä se tuota riviä yhteenkään tauluun -- samoin
            kuin puukkokierros. Ks.
            :meth:`Demoparser2Adapter._assign_round_raw`.
    """

    demo_round: int | None
    freeze_end_tick: int | None
    end_tick: int | None
    winner_side: str | None
    win_reason: str | None
    round_raw: int | None = None


@dataclass(frozen=True)
class _SampleTickCounts:
    """Yhden näytepistetickin rivilaskurit.

    Kaksi lukua eikä yksi, koska ohitus ei saa perustua pelkkään
    pawnittomien määrään. Tick, jolta ei saatu yhtään käyttökelpoista riviä,
    on **havainto** vain silloin kun pawnittomuus selittää sen kokonaan --
    jos rivejä katosi myös muusta syystä (katsoja, tuntematon puoli),
    kyseessä on vika, ja se on nostettava. Ilman ``seen``iä yksikin pawniton
    rivi vaimentaisi kovan virheen sattuman perusteella.

    Attributes:
        seen: Rivit, jotka demoparser2 palautti tältä tickiltä. Kaikki
            rivit, myös ohitetut.
        without_pawn: Näistä ne, joilla kontrolleri oli tallella mutta
            jokainen pawn-kenttä tyhjä.
    """

    seen: int = 0
    without_pawn: int = 0


@dataclass(frozen=True)
class _UtilityCounts:
    """Kranaatit, jotka eivät päätyneet tauluun sellaisenaan -- ja syy.

    Nämä eivät ole tauluun sopivia sarakkeita: pudotettu kranaatti ei voi olla
    rivi, eikä ratkeamaton tyyppi erotu ratkaistusta muuten kuin lukuna. Kaikki
    kulkevat siksi diagnostiikkaan ja sieltä ajon yhteenvetoon.

    Nolla on tavoitetila, mutta ei jokaiselle: ``outside_rounds`` on
    normaalisti 1-2, koska kierroksen ratkeamisen jälkeen heitetään yhä
    kranaatteja eikä niille ole ``t_s``:ää.

    Attributes:
        without_thrower: Rata ilman heittäjää.
        outside_rounds: Heitto, joka ei osu yhdenkään kierroksen rajoihin.
        unknown_side: Heittäjä, jonka puolta ei saatu selville.
        unknown_type: Kranaatti, jonka luokkanimeä ei tunneta. Nimi säilyy
            taulussa sellaisenaan, mutta luku paljastaa demoparser2:n
            uudelleennimeämisen ennen kuin se näkyy raportissa.
        fire_type_unresolved: Tulikranaatti, jonka molotov/incendiary-erottelu
            ei ratkennut. Tyypiksi jää ``molotov``, joten ilman lukua
            reppuhaun täydellinen rikkoutuminen näyttäisi täsmälleen samalta
            kuin demo, jossa heitettiin pelkkiä molotoveja.
        detonating_after_round: Räjähdys, joka osuu kierroksen päättymisen
            jälkeen. **Havainto eikä pudotus**: rivi saa alueensa
            pistepilvestä kuten muutkin. Story 2.2:ssa nämä jätettiin
            aluettomiksi, koska silloinen menetelmä olisi lukenut alueen
            seuraavan kierroksen spawnista; syy katosi menetelmän mukana.
        ticks_without_players: **Heiton** tick, jolta ei saatu yhtään
            pelaajariviä. Toisin kuin muut tämän luokan luvut, tämä on **vika**
            eikä havainto -- se tarkoittaa, ettei heittäjän omaa aluetta voitu
            edes yrittää lukea. Räjähdyksen tickejä ei lueta lainkaan.
        sharing_an_entity_id: Lentoradat, jotka jakavat pelin oman
            ``grenade_entity_id``:n toisen radan kanssa **samalla
            ``round_raw``:lla** -- demon omalla kierroslaskurilla, joka
            sisältää myös lämmittelyn ja puukkokierroksen. Luku on
            lentoratoja eikä pareja: kolme rataa yhdellä tunnisteella on 3.
            Havainto eikä vika, koska taulun avain on ``grenade_no``.
        throwers_without_row: Heitot, joiden **heittäjää ei ollut heiton
            tickin riveissä**. Alue jää silloin tyhjäksi, koska havaintoa ei
            korvata arviolla -- eikä pistepilvi auta: heiton alue on
            heittäjän oma ``m_szLastPlaceName``.

            **Vika eikä havainto**, ja uusi Story 2.10:n jäljiltä: ennen
            pawnittoman rivin ohitusta tämä tapaus kaatoi ajon
            elossaolovartijaan. Nyt rivi ohitetaan hiljaa, joten heitto voi
            valua ``utility_without_area``-lukuun ilman syytä. Tämä luku on
            se syy. Odotusarvo on nolla: heittäjällä on määritelmän mukaan
            pawn sillä hetkellä kun hän heittää.
    """

    without_thrower: int = 0
    outside_rounds: int = 0
    unknown_side: int = 0
    unknown_type: int = 0
    fire_type_unresolved: int = 0
    detonating_after_round: int = 0
    ticks_without_players: int = 0
    sharing_an_entity_id: int = 0
    throwers_without_row: int = 0


@dataclass(frozen=True)
class _CloudCounts:
    """Pistepilven havainnot, jotka eivät mahdu ``CALLOUT_CLOUD``-sopimukseen.

    Ruutujen ja alueiden määrä **ei ole täällä**, eikä myöskään pilveen
    kelvanneiden rivien määrä: kaikki kolme ovat luettavissa valmiista
    taulusta. ``observations``-sarakkeen summa **on** kelvanneiden rivien
    määrä, koska jokainen kelvollinen rivi päätyy täsmälleen yhteen ruutuun --
    sen laskeminen myös täällä olisi sama luku kahdesta lähteestä. Täällä on
    vain se, mikä näkyy **vain** lukuhetkellä.

    Attributes:
        rows_read: Rivit, jotka koko demon tickiluku palautti (rivi per
            pelaaja per tick). Moduulin suurin yksittäinen erä, ja tämä on
            ainoa paikka, jossa sen koko näkyy -- valmiissa taulussa on
            jäljellä vain se osa, joka kelpasi.
        empty_reason: Miksi pilvi jäi tyhjäksi, tai ``None``. Tyhjä pilvi ei
            kaada ajoa, mutta ilman syytä se näyttäisi demolta, jossa ei
            heitetty utilityä.
    """

    rows_read: int = 0
    empty_reason: str | None = None


@dataclass(frozen=True)
class _DeathCounts:
    """Kuolemat, jotka eivät päätyneet tauluun sellaisenaan -- ja syy.

    Nolla on tavoitetila kaikille kolmelle, mutta ``outside_rounds`` voi olla
    pieni luku aidosti: kierroksen ratkeamisen jälkeen kuollaan yhä, eikä
    sellaiselle kuolemalle ole ``t_s``:ää.

    **Puukkokierroksen kuolemat eivät ole näissä luvuissa.** Ne ovat
    kierroksen rajojen sisällä ja saavat ``round_raw``:nsa; ne putoavat vasta
    ``stages.parse``in numeroinnissa, samalla mekanismilla kuin näytepisteet
    ja kranaatit, ja niiden määrän kertoo vaihe.

    Attributes:
        without_tick: Kuolema, jonka tickiä ei saatu luettua. Ilman tickiä
            kuolemaa ei voi kohdistaa kierrokseen eikä laskea ``t_s``:ää.
        outside_rounds: Kuolema, joka ei osu yhdenkään kierroksen rajoihin.
        without_victim: Kuolema **ilman uhria**. Eri asia kuin puuttuva puoli:
            tässä tapahtumalta puuttuu ``user_steamid`` kokonaan, eikä kyse
            ole puolen päättelyn epäonnistumisesta. Syyt pidetään erillään
            samasta syystä kuin ``without_attacker`` ja
            ``without_attacker_area`` ``stages.parse``in luvuissa: yhdistetty
            luku näyttäisi päättelyvialta, jota ei ole.
        without_victim_side: Kuolema, jonka uhri **tunnetaan** mutta jonka
            puolta ei saatu selville sen paremmin kokoonpanosta, kierroksen
            ankkuritickistä kuin tapahtuman omasta ``user_team_num``-kentästä.
            Rivi pudotetaan: ``victim_lineup_key`` on koko taulun
            liitosavain.
        attacker_without_side: Kuolema, jonka **ampujan** puoli jäi
            tuntemattomaksi vaikka ampuja tunnetaan. Rivi säilyy ja ampujan
            havainnot sen mukana; vain ``attacker_side`` ja
            ``attacker_lineup_key`` jäävät tyhjiksi.
    """

    without_tick: int = 0
    outside_rounds: int = 0
    without_victim: int = 0
    without_victim_side: int = 0
    attacker_without_side: int = 0


@dataclass
class _BuyWindowCounters:
    """Ostoikkunan havainnot, jotka eivät mahdu ``ROUNDS``-sopimukseen.

    Attributes:
        cuts: Kuoleman katkaisemat kierrokset pareina ``(round_raw, montako
            ostosta jäi katkaisun taakse)``. **Pareina eikä lukuna**, koska
            adapteri ei tiedä mitkä kierrokset päätyvät tauluun: puukkokierros
            saa oman ``round_raw``:nsa mutta ``stages.parse`` pudottaa sen,
            ja pelkkä yhteisluku sisältäisi sen ilman että sitä voisi enää
            vähentää pois. Vaihe suodattaa nämä pelattuja kierroksia vasten.

            Katkaisu itsessään on **havainto eikä vika**: se on sääntö, koska
            kuolleen tavaraluettelo tyhjenee, ja se osuu noin puoleen
            kierroksista. Menetettyjen ostojen **kuuluu olla nolla**.
        unchecked_cuts: Katkaistut kierrokset (``round_raw``), joilla
            menetettyjä ostoja **ei voitu tarkistaa**: ikkunan lopun tickiltä
            ei saatu yhdeltäkään pelaajalta luettavaa ``cash_spent``-arvoa.
            Ilman tätä menetettyjen ostojen nolla tarkoittaisi kahta eri asiaa
            -- "mitään ei menetetty" ja "ei tiedetä".
        ticks_without_players: Kierrokset, joilla ostoajan lopun tickiltä ei
            saatu yhtään pelaajariviä ja mittaus palautui ankkuriin. **Vika
            eikä havainto**: käytännössä demo on katkennut kesken kierroksen.
            Ilman varasääntöä koko kierroksen talous olisi tyhjä. Tällaisella
            kierroksella katkaisua **ei** kirjata: mitään ei mitattu ikkunan
            lopusta, joten menetetyt ostot eivät ole kuoleman katkaisun syytä.
        players_lost: Joukkuerivit kertaa pelaajat, jotka olivat luettavissa
            ankkurilla mutta eivät enää mittauspisteessä. Summat ja jakaja
            kutistuvat yhdessä, joten per pelaaja -arvot pysyvät oikeina --
            mutta joukkue näyttää pelaavan vajaalla, ja se on eri väite kuin
            "yhteys katkesi kesken kierroksen".
        sides_without_rows: Joukkuerivit, joilta mittauspisteessä ei saatu
            yhtään luettavaa pelaajaa, vaikka ankkurilla saatiin. Rivi menee
            tauluun tyhjänä mutta tilalla ``ok``, ja ``classify`` jättää sen
            luokittelematta puuttuvan havainnon takia -- oikea lopputulos,
            mutta ilman tätä lukua kukaan ei saisi tietää miksi.
        refunds: Pelaajarivit, joilla ``cash_spent`` pieneni ankkurin ja
            mittauspisteen välillä eli ostos palautettiin. Prop kasvaa vain
            ostoista, joten lasku on yksikäsitteinen merkki palautuksesta.
        stale_equipment: Pelaajarivit, joilla varustearvo nousi ilman että
            pelaaja osti, sai panssaria tai muutti tavaraluetteloaan. Se on
            palautuksen jättämä vanhentunut lukema (ks.
            :func:`_refunds_and_stale_equipment`). Mitattu: 1 rivi 134
            kierroksesta, enintään 1 000 $ per pelaaja.
    """

    cuts: list[tuple[int, int]] = field(default_factory=list)
    unchecked_cuts: list[int] = field(default_factory=list)
    ticks_without_players: int = 0
    players_lost: int = 0
    sides_without_rows: int = 0
    refunds: int = 0
    stale_equipment: int = 0


@dataclass
class _ArmedCounters:
    """Kalustolaskurin havainnot, jotka eivät mahdu ``ROUNDS``-sopimukseen.

    Attributes:
        unknown_items: Tavaraluettelon nimi -> montako kertaa se nähtiin.
            **Määrä eikä pelkkä joukko**: yksi eksoottinen veitsi ja
            demoparser2:n nimeämismuutos, joka osuu joka riviin, näyttäisivät
            pelkkänä nimenä täsmälleen samalta.
        unreadable_rows: Joukkuerivit, joilla **kalustolaskuri** jäi tyhjäksi
            siksi, että jonkun luettavan pelaajan panssari tai tavaraluettelo
            puuttui. Ankkurittomat kierrokset **eivät** ole tässä: niillä ei
            ole havaintoa lainkaan, mikä on eri asia kuin epäonnistunut luku.
        armored_unreadable_rows: Sama panssarilaskurille. Oma lukunsa, koska
            ehdot eroavat: tämä kasvaa vain panssarin jäädessä lukematta, kun
            taas edellinen kasvaa myös pelkän tavaraluettelon pettäessä.
            Erotus on siis "rivit, joilla vain tavaraluettelo petti" -- juuri
            se ero, jonka takia laskureiden luettavuusehdot ovat erilaiset.
            Tämä luku on aina pienempi tai yhtä suuri kuin edellinen.
    """

    unknown_items: Counter[str] = field(default_factory=Counter)
    unreadable_rows: int = 0
    armored_unreadable_rows: int = 0


class Demoparser2Adapter:
    """Lukee kierros- ja näytepistetaulun demoparser2:lla.

    Toteuttaa :class:`~pappascout.adapters.protocols.DemoParser`-portin.

    Args:
        exclude_weapons: Aseet, jotka eivät kelpaa ensikontaktiksi
            (``[parse].first_contact_exclude_weapons``). Oletus on tarkoituksella
            tyhjä: adapteri ei lue asetuksia, vaan vaihe antaa listan.
        fallback_death: Saako ensikontakti tulla ``player_death``-tapahtumasta,
            jos kelvollista ``player_hurt``ia ei ole
            (``[parse].first_contact_fallback_death``).
        area_snap_units: Enimmäisetäisyys, jolta räjähdyksen alue saa tulla
            lähimmästä pistepilviruudusta (``[parse].area_snap_units``).
            ``None`` = ei kynnystä käytössä, jolloin ``area`` jää tyhjäksi
            mutta koordinaatit ja etäisyys tallentuvat. Se on
            kalibroimattoman asetuksen rehellinen arvo eikä vika: lähin ruutu
            löytyy aina, joten kynnyksetön nimeäminen olisi väite eikä mittaus.
        callout_grid_units: Pistepilven ruudun särmä pelin yksiköissä
            (``[parse].callout_grid_units``).
        callout_z_weight: Pystyeron painokerroin, kun räjähdykselle etsitään
            lähintä ruutua (``[parse].callout_z_weight``).
        callout_z_tolerance_units: Pystyero, joka on painotuksessa ilmaista
            (``[parse].callout_z_tolerance_units``). Pelaajan korkeus:
            kranaatti räjähtää mistä tahansa lattian ja pään väliltä, joten
            ilman toleranssia pystyrangaistus osuisi normaaliin tapaukseen.

            Kolmen viimeisen oletukset ovat **mitattuja arvoja** eivätkä
            neutraaleja nollia -- neutraalia ruudun kokoa ei ole olemassa, ja
            nolla olisi kelvoton. Tuotannossa ne tulevat silti aina vaiheelta:
            adapteri ei lue asetuksia.
        buy_window_seconds: Ostoajan pituus sekunteina freezetimen lopusta
            (``[parse].buy_window_seconds``). Oletus on tarkoituksella
            **0.0** eikä pelin 20 s: adapteri ei lue asetuksia, ja neutraali
            oletus tarkoittaa "mittaa ankkurista", eli täsmälleen sitä mitä
            tämä luokka teki ennen ostoikkunaa. Vaihe antaa oikean arvon.
    Aseistettujen laskurilla ei ole asetuksia: sääntö on "panssari ja
    vähintään yksi ase hallussa", ja aseluettelo on :mod:`pappascout.constants`.
    Luettelon muutos mitätöi arkiston ``stages.parse``in parametrihashin
    kautta, ei tämän luokan kautta.

    Attributes:
        diagnostics: Viimeisimmän parsinnan havainnot, jotka eivät mahdu
            taulusopimuksiin. ``None`` ennen ensimmäistä kutsua.
    """

    def __init__(
        self,
        *,
        exclude_weapons: Sequence[str] = (),
        fallback_death: bool = True,
        area_snap_units: float | None = None,
        buy_window_seconds: float = 0.0,
        callout_grid_units: int = 32,
        callout_z_weight: float = 1.0,
        callout_z_tolerance_units: float = 72.0,
    ) -> None:
        self.exclude_weapons = tuple(exclude_weapons)
        self.fallback_death = fallback_death
        self.area_snap_units = area_snap_units
        self.buy_window_seconds = float(buy_window_seconds)
        self.callout_grid_units = int(callout_grid_units)
        self.callout_z_weight = float(callout_z_weight)
        self.callout_z_tolerance_units = float(callout_z_tolerance_units)
        self.diagnostics: ParseDiagnostics | None = None
        #: Miksi otsikosta ei saatu kartan nimeä; asetetaan lukuhetkellä ja
        #: siirretään diagnostiikkaan. Nollataan joka luvun alussa, jottei
        #: edellisen demon syy kanna seuraavaan.
        self._header_missing_reason: str | None = None

    def parse_demo(
        self, path: Path, sample_seconds: Sequence[float]
    ) -> DemoTables:
        """Ks. portin dokumentaatio."""
        path = Path(path)
        with readable_demo(path) as demo_path:
            return self._parse(demo_path, path, tuple(sample_seconds))

    # -- Sisäinen ------------------------------------------------------------

    def _parse(
        self,
        demo_path: Path,
        original_path: Path,
        sample_seconds: tuple[float, ...],
    ) -> DemoTables:
        parser = self._open(demo_path, original_path)
        # Otsikko luetaan **samasta parser-oliosta** kuin kaikki muukin: demoa
        # ei avata toista kertaa kartan nimen takia.
        self._header_missing_reason = None
        map_name = self._header_map_name(parser, original_path)
        freeze_ticks = self._freeze_end_ticks(parser, original_path)
        round_ends = self._round_ends(parser, original_path)
        segments = self._segments(freeze_ticks, round_ends)

        if not segments:
            raise ParseError(
                f"Demosta {original_path.name} ei löytynyt yhtään kierrosta.\n"
                "Tiedosto on todennäköisesti katkennut kesken latauksen. "
                "Lataa demo uudelleen."
            )

        wanted = sorted(
            {s.freeze_end_tick for s in segments if s.freeze_end_tick is not None}
            | {s.end_tick for s in segments if s.end_tick is not None}
        )
        by_tick = self._read_ticks(parser, wanted, original_path)
        tick_rate, measured = self._tick_rate(by_tick, freeze_ticks)

        # Kuolemat luetaan **aina**, myös kun first_contact_fallback_death on
        # epätosi: ne rajaavat ostoikkunan, koska kuolleen tavaraluettelo
        # tyhjenee, eikä se saa riippua ensikontaktin asetuksesta. Asetus
        # ratkaisee vain sen, saako ensikontakti tulla kuolemasta. Sama luku
        # annetaan näytepisteille, jottei tapahtumaa parsita kahdesti.
        death_rows, deaths_without_tick = self._death_events(
            parser, original_path
        )
        deaths = [
            (r["tick"], r["attacker_id"], r["victim_id"], r["weapon"])
            for r in death_rows
        ]
        death_ticks = sorted(tick for tick, *_ in deaths)
        buy_ticks, window_ticks = _buy_end_ticks(
            segments, death_ticks, tick_rate, self.buy_window_seconds
        )
        extra = sorted(
            {
                tick
                for tick in (*buy_ticks, *window_ticks)
                if tick is not None and tick not in by_tick
            }
        )
        if extra:
            by_tick.update(self._read_ticks(parser, extra, original_path))

        lineups = [_Lineup(), _Lineup()]
        sides = self._assign_sides(segments, by_tick, lineups)
        lineup_keys = self._lineup_keys(lineups)
        # Kalustolaskurin ja ostoikkunan omat havainnot palautuvat taulun
        # mukana eivätkä kerry kutsujan antamaan olioon: muuttuva
        # ulosparametri lakkaisi hiljaa toimimasta, jos joku unohtaisi
        # välittää sen eteenpäin.
        rounds, armed, buy = self._build_frame(
            segments, by_tick, tick_rate, sides, lineup_keys, buy_ticks, window_ticks
        )

        points, unknown_sides = self._sample_points(
            parser,
            original_path,
            segments,
            sides,
            lineups,
            by_tick,
            tick_rate,
            sample_seconds,
            deaths,
        )
        ticks, partial, sample_tick_counts, points_without_pawn = (
            self._build_ticks_frame(
                points, parser, original_path, segments, sides, lineup_keys
            )
        )
        # Pistepilvi ennen tapahtumataulua: se on räjähdysalueiden lähde,
        # joten se on oltava kädessä ennen kuin yhtäkään aluetta nimetään.
        callouts, cloud_counts = self._build_callout_cloud(parser, original_path)
        events, utility, throw_tick_counts = self._build_events_frame(
            parser,
            original_path,
            segments,
            sides,
            lineup_keys,
            lineups,
            by_tick,
            tick_rate,
            callouts,
        )
        death_frame, death_counts = self._build_deaths_frame(
            death_rows,
            segments,
            sides,
            lineup_keys,
            lineups,
            by_tick,
            tick_rate,
            without_tick=deaths_without_tick,
        )
        # Kokoonpanotaulu rakennetaan vasta tässä, jotta se kantaa kaikki
        # kartan aikana havaitut jäsenet ja nimet -- myös vaihtopelaajan, joka
        # tuli mukaan vasta myöhemmällä kierroksella.
        lineups_frame = self._build_lineups_frame(lineups, lineup_keys)

        self.diagnostics = ParseDiagnostics(
            tick_rate=tick_rate,
            tick_rate_measured=measured,
            rounds_seen=len(segments),
            match_restarts=sum(1 for s in segments if s.round_raw is None),
            partial_samples=partial,
            sample_rows_without_pawn=_pawnless_rows(
                sample_tick_counts, throw_tick_counts
            ),
            sample_points_without_pawn=points_without_pawn,
            grenade_throwers_without_row=utility.throwers_without_row,
            unknown_side_events=unknown_sides,
            grenades_without_thrower=utility.without_thrower,
            grenades_outside_rounds=utility.outside_rounds,
            grenades_unknown_side=utility.unknown_side,
            grenades_unknown_type=utility.unknown_type,
            grenades_fire_type_unresolved=utility.fire_type_unresolved,
            grenades_detonating_after_round=utility.detonating_after_round,
            grenade_ticks_without_players=utility.ticks_without_players,
            grenades_sharing_an_entity_id=utility.sharing_an_entity_id,
            callout_cloud_rows_read=cloud_counts.rows_read,
            callout_cloud_empty_reason=cloud_counts.empty_reason,
            header_map_name_missing_reason=self._header_missing_reason,
            unknown_inventory_items=tuple(sorted(armed.unknown_items.items())),
            lineup_name_conflicts=sum(
                1
                for lineup in lineups
                for votes in lineup.names.values()
                if len(votes) > 1
            ),
            lineup_clan_conflicts=sum(
                1
                for lineup in lineups
                for votes in lineup.clans.values()
                if len(votes) > 1
            ),
            deaths_without_tick=death_counts.without_tick,
            deaths_outside_rounds=death_counts.outside_rounds,
            deaths_without_victim=death_counts.without_victim,
            deaths_without_victim_side=death_counts.without_victim_side,
            deaths_attacker_without_side=death_counts.attacker_without_side,
            armed_unreadable_rows=armed.unreadable_rows,
            armored_unreadable_rows=armed.armored_unreadable_rows,
            buy_window_seconds=self.buy_window_seconds,
            buy_window_cuts=tuple(sorted(buy.cuts)),
            buy_window_unchecked_cuts=tuple(sorted(buy.unchecked_cuts)),
            buy_window_ticks_without_players=buy.ticks_without_players,
            buy_window_players_lost=buy.players_lost,
            buy_window_sides_without_rows=buy.sides_without_rows,
            buy_window_refunds=buy.refunds,
            buy_window_stale_equipment=buy.stale_equipment,
        )
        return DemoTables(
            rounds=rounds,
            ticks=ticks,
            events=events,
            lineups=lineups_frame,
            deaths=death_frame,
            callouts=callouts,
            match=self._build_match_frame(map_name),
        )

    def _header_map_name(self, parser: Any, original_path: Path) -> str | None:
        """Kartan nimi demon otsikosta, tai ``None`` jos sitä ei ole.

        Otsikko on **havainto**: nimi palautetaan sellaisenaan eikä sitä
        verrata karttapooliin. Poolin ulkopuolinen kartta -- workshop-versio
        tai ``de_train`` -- on aito havainto eikä tuntematon kartta, ja
        hiljainen korjaus poolin nimeksi tekisi siitä valheen.

        Tyhjä tai pelkkiä välilyöntejä sisältävä nimi on ``None`` eikä
        korvike: vasta silloin ``aggregate`` palaa päättelemään nimen
        ``map_demo_id``:stä. Otsikon muut kentät (esimerkiksi
        ``server_name``) eivät kuulu tähän tauluun.

        Poikkeus kääritään :class:`~pappascout.errors.ParseError`iksi samalla
        säännöllä kuin :meth:`_open`issa ja :meth:`_event`issä: kirjaston oma
        virhetyyppi ei ole tämän kerroksen sopimusta.

        Viesti nimeää **kaksi** mahdollista syytä eikä vain vioittunutta
        tiedostoa. Kirjaston uudelleennimeämä metodi nostaa ``AttributeError``in
        täysin ehjästä demosta, ja pelkkä "lataa uudelleen" lähettäisi
        käyttäjän hakemaan 230 MB:n tiedoston, joka on jo kunnossa.
        """
        try:
            header = parser.parse_header()
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} otsikkoa ei voitu lukea: "
                f"{type(exc).__name__}: {exc}\n"
                "Syy on jokin näistä kahdesta: tiedosto on vioittunut, tai "
                "demoparser2:n rajapinta on muuttunut eikä otsikkoa enää lueta "
                "näin. Tarkista ensin, aukeaako sama demo toisella "
                "demoparser2-versiolla; jos aukeaa, korjaus kuuluu adapteriin. "
                "Muuten lataa demo uudelleen."
            ) from exc
        get = getattr(header, "get", None)
        if not callable(get):
            self._header_missing_reason = (
                "parse_header() ei palauttanut sanakirjaa "
                f"(tyyppi {type(header).__name__})"
            )
            return None
        value = get("map_name")
        if value is None:
            self._header_missing_reason = (
                "otsikossa ei ole map_name-kenttää lainkaan -- demoparser2 on "
                "todennäköisesti nimennyt sen uudelleen"
            )
            return None
        # ``isinstance``, ei ``str()``: tavujono kääntyisi nimeksi
        # ``b'de_ancient'``, joka näyttäisi taulussa havainnolta ja pirstoisi
        # kartan omaksi haarakseen. Havainto on nimi tai sen puuttuminen, ei
        # korvike -- eikä kirjaston tyyppimuutos saa mennä läpi hiljaa.
        if not isinstance(value, str):
            self._header_missing_reason = (
                f"map_name ei ole merkkijono vaan {type(value).__name__}"
            )
            return None
        text = value.strip()
        if not text:
            self._header_missing_reason = "map_name on tyhjä otsikossa"
            return None
        return text

    @staticmethod
    def _build_match_frame(map_name: str | None) -> pl.DataFrame:
        """Rakenna ottelutaulu: **yksi rivi**, tunnettu tai tuntematon nimi.

        Rivi kirjoitetaan myös silloin, kun nimeä ei ollut. Tyhjä taulu
        tarkoittaisi demoa ilman ottelua, ja se olisi eri väite kuin
        "ottelu on, mutta kartan nimeä ei saatu" -- vain jälkimmäinen on tosi.
        """
        schema: dict[str, Any] = {
            name: MATCH[name] for name in MATCH_ADAPTER_COLUMNS
        }
        # Ei ``orient``ia: sanakirjarivi kertoo sarakkeensa nimellä, joten
        # rivi- ja sarakesuunnan erottelu ei koske tätä kutsua.
        return pl.DataFrame([{"map_name": map_name}], schema=schema)

    def _open(self, demo_path: Path, original_path: Path) -> Any:
        from demoparser2 import DemoParser as _Demoparser2

        try:
            return _Demoparser2(str(demo_path))
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demoa {original_path.name} ei voitu avata: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut. Lataa demo uudelleen."
            ) from exc

    def _freeze_end_ticks(self, parser: Any, original_path: Path) -> list[int]:
        frame = self._event(parser, "round_freeze_end", original_path)
        if frame is None or "tick" not in frame.columns:
            return []
        return sorted({int(t) for t in frame["tick"].tolist()})

    def _round_ends(self, parser: Any, original_path: Path) -> list[dict[str, Any]]:
        """Kierrosten päättymiset aikajärjestyksessä.

        Ensimmäinen rivi (tick 1, ``round`` 0, tyhjä voittaja) on demoparser2:n
        alkuarvo eikä kierros, joten se pudotetaan.
        """
        frame = self._event(parser, "round_end", original_path)
        if frame is None or "tick" not in frame.columns:
            return []
        ends: list[dict[str, Any]] = []
        for row in frame.to_dict("records"):
            tick = _as_int(row.get("tick"))
            if tick is None or tick <= 1:
                continue
            ends.append(
                {
                    "tick": tick,
                    "round": _as_int(row.get("round")),
                    "winner": _as_side(row.get("winner")),
                    "reason": _as_str(row.get("reason")),
                }
            )
        ends.sort(key=lambda r: r["tick"])
        return ends

    def _event(
        self,
        parser: Any,
        name: str,
        original_path: Path,
        *,
        player: Sequence[str] | None = None,
    ) -> Any:
        """Lue yksi tapahtuma; ``player`` pyytää pelaajakohtaiset kentät.

        Kirjasto lisää pyydetyt kentät kolmella etuliitteellä (``user_*``,
        ``attacker_*``, ``assister_*``). Parametri on **avainsanallinen ja
        oletukseltaan tyhjä**, koska useimmat tapahtumat luetaan ilman niitä
        eikä ylimääräisiä sarakkeita haluta maksaa.
        """
        try:
            frame = (
                parser.parse_event(name)
                if player is None
                else parser.parse_event(name, player=list(player))
            )
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} tapahtumaa {name!r} ei voitu lukea: "
                f"{exc}\n"
                "Tiedosto on todennäköisesti katkennut. Lataa demo uudelleen."
            ) from exc
        if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
            return None
        return frame

    @staticmethod
    def _segments(
        freeze_ticks: list[int], round_ends: list[dict[str, Any]]
    ) -> list[_Segment]:
        """Paritä freezetime-ankkurit ja kierrosten päättymiset.

        Kierroksen ankkuri on viimeinen ``round_freeze_end`` ennen sen
        päättymistä. Jos ankkuria ei ole, kierros on silti mukana --
        ``freeze_end_tick`` jää tyhjäksi ja ``status`` kertoo syyn (AD-9).
        Jos ankkurin jälkeen ei tule päättymistä, demo on katkennut kesken
        kierroksen; kierros pysyy mukana, mutta ilman tulosta.
        """
        segments: list[_Segment] = []
        pending: list[int] = []
        i = 0
        for end in round_ends:
            while i < len(freeze_ticks) and freeze_ticks[i] < end["tick"]:
                pending.append(freeze_ticks[i])
                i += 1
            # Kaikki paitsi viimeinen ankkuri jäivät ilman päättymistä.
            for orphan in pending[:-1]:
                segments.append(_Segment(None, orphan, None, None, None))
            segments.append(
                _Segment(
                    demo_round=end["round"],
                    freeze_end_tick=pending[-1] if pending else None,
                    end_tick=end["tick"],
                    winner_side=end["winner"],
                    win_reason=end["reason"],
                )
            )
            pending = []
        for orphan in freeze_ticks[i:]:
            segments.append(_Segment(None, orphan, None, None, None))

        Demoparser2Adapter._assign_round_raw(segments)
        return segments

    @staticmethod
    def _assign_round_raw(segments: list[_Segment]) -> None:
        """Anna jokaiselle kierrokselle demon oma juokseva numero.

        Arvo tulee ``round_end``-tapahtuman ``round``-kentästä. Segmentti, joka
        jää ilman omaa arvoa, käsitellään sen mukaan **mistä kohtaa listaa se
        löytyy ja mitä siitä on havaittu**:

        * **Listan hännässä** se on ratkeamaton kierros: demo katkesi kesken,
          eikä ``round_end``iä enää tule. Sille johdetaan naapureista arvo,
          joka säilyttää järjestyksen.
        * **Listan alussa**, ennen demon ensimmäistä omaa numeroa, se saa
          arvonsa taaksepäin laskettuna. Numerointi voi alkaa mistä tahansa,
          eikä sitä ennen ole arvoa, johon törmätä.
        * **Keskellä** se on ottelun uudelleenaloitus -- mutta vain jos
          havainnot sanovat niin. Sen ratkaisee
          :meth:`_match_restarts`, joka keskeyttää parsinnan jos ehdot eivät
          täyty. Uudelleenaloitus jää **numeroimattomaksi**
          (``round_raw = None``) samalla mekanismilla kuin puukkokierros:
          naapurista täyttäminen antaisi sille numeron, jonka demo käyttää
          heti perään uudelleen.

        Raises:
            ParseError: Jos keskellä oleva numeroimaton kierrosraja ei täytä
                uudelleenaloituksen ehtoja, jos niitä on enemmän kuin
                :data:`MAX_MATCH_RESTARTS`, tai jos numerointi ei kasva
                tasaisesti.
        """
        if not segments:
            return
        raws: list[int | None] = [s.demo_round for s in segments]
        own = [index for index, value in enumerate(raws) if value is not None]

        if not own:
            # Yhdelläkään segmentillä ei ole demon omaa numeroa. Järjestys on
            # silti tiedossa, joten varasääntö on juokseva numerointi.
            raws = list(range(1, len(raws) + 1))
        else:
            first_own, last_own = own[0], own[-1]

            # Häntä: näiden jälkeen ei tule enää yhtään demon omaa numeroa,
            # joten ne ovat ratkeamattomia kierroksia.
            value = raws[last_own]
            assert value is not None
            for index in range(last_own + 1, len(raws)):
                value += 1
                raws[index] = value

            # Alku: ennen ensimmäistä omaa numeroa ei ole arvoa, johon törmätä.
            value = raws[first_own]
            assert value is not None
            for index in range(first_own - 1, -1, -1):
                value -= 1
                raws[index] = value

            # Keskelle jääneet tarkistetaan havaintoja vasten; hyväksytyt
            # jäävät None:ksi eli numeroimattomiksi.
            Demoparser2Adapter._match_restarts(segments, raws, own)

        known = [value for value in raws if value is not None]
        for first, second in zip(known, known[1:]):
            if second <= first:
                raise ParseError(
                    "Demon oma kierrosnumerointi ei kasva tasaisesti "
                    f"({first} -> {second}).\n"
                    "Kierrosrajat eivät vastaa demoparser2:n round_end-numeroita, "
                    "joten kierroksia ei voi tunnistaa luotettavasti."
                )

        for segment, number in zip(segments, raws):
            segment.round_raw = number

    @staticmethod
    def _match_restarts(
        segments: list[_Segment], raws: list[int | None], own: list[int]
    ) -> list[int]:
        """Keskellä olevat kierrosrajat, jotka ovat ottelun uudelleenaloituksia.

        Tunnistus perustuu **havaintoihin eikä sijaintiin**. Pelkkä "keskellä
        ja ilman numeroa" ei riitä: samalta näyttäisi myös kierros, jonka
        rajojen tunnistus hukkasi, ja sen pudottaminen veisi kierroksen pois
        jokaisesta taulusta ja nimeäisi sen vielä uudelleenaloitukseksi.

        Uudelleenaloitus täyttää molemmat ehdot:

        * **Ei ``round_end``iä.** Segmentti, joka ratkesi mutta jolta puuttuu
          demon oma numero, on kierros ilman numeroa -- ei uudelleenaloitus.
          Se numeroidaan naapurista kuten ennenkin: kierros on olemassa, joten
          sitä ei pudoteta. Jos johdettu numero törmää demon omaan,
          monotonisuustarkistus hoitaa sen.
        * **Demon oma numerointi jatkuu sen yli yhdellä.** Uudelleenaloitus ei
          kuluta kierrosnumeroa, joten sen molemmin puolin numerot ovat
          peräkkäiset. Hyppy tarkoittaa, että väliin on jäänyt kierros; se
          keskeyttää parsinnan, koska pudotus siirtäisi kaiken jälkeen tulevan.

        Args:
            segments: Kierrosrajat aikajärjestyksessä.
            raws: Kullekin segmentille päätetty numero; ``None`` niillä, joita
                ei ole numeroitu. **Muutetaan paikallaan**: aukot, jotka eivät
                ole uudelleenaloituksia, täytetään tässä.
            own: Niiden segmenttien indeksit, joilla on demon oma numero.

        Returns:
            Uudelleenaloitusten indeksit ``segments``-listassa. Ne jäävät
            ``raws``issa ``None``:ksi.

        Raises:
            ParseError: Jos demon numerointi hyppää numeroimattoman
                kierrosrajan yli, tai jos uudelleenaloituksia on enemmän kuin
                :data:`MAX_MATCH_RESTARTS`.
        """
        restarts: list[int] = []
        for previous, following in zip(own, own[1:]):
            gap = list(range(previous + 1, following))
            if not gap:
                continue

            if any(segments[i].end_tick is not None for i in gap):
                # Väliin jäi kierros, joka ratkesi mutta jolta puuttuu demon
                # oma numero. Se on kierros eikä uudelleenaloitus, joten se
                # numeroidaan naapurista -- pudottaminen veisi sen pois
                # jokaisesta taulusta ja nimeäisi sen vielä väärin.
                value = raws[previous]
                assert value is not None
                for index in gap:
                    value += 1
                    raws[index] = value
                continue

            before, after = raws[previous], raws[following]
            assert before is not None and after is not None
            if after != before + 1:
                ticks = ", ".join(str(segments[i].freeze_end_tick) for i in gap)
                raise ParseError(
                    "Demon oma kierrosnumerointi hyppää numeroimattoman "
                    f"kierrosrajan yli ({before} -> {after}, freezetime-tickit "
                    f"{ticks}).\n"
                    "Uudelleenaloituksen yli numerointi jatkuisi yhdellä, joten "
                    "väliin on jäänyt kierros, jota kierrosrajojen tunnistus ei "
                    "löytänyt. Sitä ei pudoteta arvaamalla: katso demoa "
                    "listatuista tickeistä ja kerro havainto kehittäjälle."
                )

            restarts.extend(gap)

        if len(restarts) > MAX_MATCH_RESTARTS:
            ticks = ", ".join(str(segments[i].freeze_end_tick) for i in restarts)
            raise ParseError(
                f"Demossa on {len(restarts)} ottelun uudelleenaloitukselta "
                f"näyttävää kierrosrajaa (freezetime-tickit {ticks}), mutta "
                f"enintään {MAX_MATCH_RESTARTS} hyväksytään.\n"
                "Useampi tarkoittaa ilmiötä, jota ei ole vielä nähty, eikä sitä "
                "arvata. Avaa demo listatuista tickeistä ja kerro havainto "
                "kehittäjälle ennen kuin tulosta käytetään."
            )
        return restarts

    def _read_ticks(
        self, parser: Any, ticks: list[int], original_path: Path
    ) -> dict[int, list[dict[str, Any]]]:
        """Lue propit annetuista tickeistä ja ryhmittele tickin mukaan."""
        if not ticks:
            return {}
        try:
            frame = parser.parse_ticks(list(TICK_PROPS), ticks=ticks)
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} tick-arvoja ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne näitä kenttiä. Aja: uv sync"
            ) from exc

        received = set(getattr(frame, "columns", ()))
        # ``name`` on mukana vaatimuksissa, vaikka sitä ei pyydetä propina:
        # demoparser2 lisää sen itse, ja ilman tarkistusta kirjaston muutos
        # jättäisi rosterin nimet hiljaa tyhjiksi.
        missing = [
            name
            for name in (*TICK_PROPS, "tick", "steamid", _PLAYER_NAME)
            if name not in received
        ]
        if missing:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia pyydettyjä kenttiä demosta "
                f"{original_path.name}. Puuttuu: {', '.join(missing)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta taulu näyttäisi kelvolliselta "
                "mutta olisi tyhjä. Päivitä adapters/demo_parser.py:n propinimet."
            )

        by_tick: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in frame.to_dict("records"):
            steamid = _as_str(row.get("steamid"))
            side = TEAM_SIDES.get(_as_int(row.get(_TEAM_NUM)) or -1)
            tick = _as_int(row.get("tick"))
            if steamid is None or side is None or tick is None:
                # Katsojat ja liittymättömät eivät ole kierroksen osapuolia.
                continue
            by_tick[tick].append(
                {
                    "steamid": steamid,
                    "side": side,
                    # Identiteetti: nimi ja klaani samalta tickiltä kuin
                    # kokoonpano. Tyhjä merkkijono muuttuu _as_str:ssä
                    # None:ksi -- tyhjä ei ole nimi.
                    "player_name": _as_str(row.get(_PLAYER_NAME)),
                    "clan_name": _as_str(row.get(_CLAN_NAME)),
                    "account": _as_int(row.get(_ACCOUNT)),
                    "cash_spent": _as_int(row.get(_CASH_SPENT)),
                    "equip_round_start": _as_int(row.get(_EQUIP_ROUND_START)),
                    "equip_current": _as_int(row.get(_EQUIP_CURRENT)),
                    "armor_value": _as_int(row.get(_ARMOR_VALUE)),
                    "inventory": _as_inventory(row.get(_INVENTORY)),
                    # Puuttuva elossaolo muuttuu tässä arvoksi False, ja se
                    # on **tarkoituksellista** -- toisin kuin näytepisteillä,
                    # joilla sama muunnos on kielletty. Kaksi syytä, ja
                    # molemmat on mitattu:
                    #
                    # 1. Pawniton pelaaja EI OLE elossa. Hän ei ole kartalla,
                    #    joten False on oikea vastaus molempiin lukuihin,
                    #    jotka tätä käyttävät (``survivors`` ja
                    #    ``survivors_equip_prev``). Näytepisteillä sama arvo
                    #    olisi väärä, koska siellä rivin olemassaolo on itse
                    #    väite pelaajan olemisesta asetelmassa.
                    # 2. Rivi ei silti valu talouslukuihin: _BUY_END_PROPS
                    #    sisältää kaksi PAWNIN kenttää
                    #    (``m_unCurrentEquipmentValue``,
                    #    ``m_unRoundStartEquipmentValue``), joten _readable
                    #    pudottaa pawnittoman rivin sekä summista että niiden
                    #    jakajasta ennen kuin elossaololla on väliä. Mitattu
                    #    2026-08-31 anubis_vs_RCAVE_VETERANS kierros 19:
                    #    players_buy_end on 4 kun naapurikierroksilla 5.
                    #
                    # Kirjaston nimenmuutos ei pääse tästä läpi hiljaa:
                    # _read_sample_ticks lukee saman propin ja kaataa koko
                    # ajon ennen kuin yhtäkään taulua kirjoitetaan.
                    "alive": _as_int(row.get(_LIFE_STATE)) == _ALIVE,
                    "team_score": _as_int(row.get(_TEAM_SCORE)),
                    "round_start_time": _as_float(row.get(_ROUND_START_TIME)),
                }
            )
        return dict(by_tick)

    @staticmethod
    def _tick_rate(
        by_tick: dict[int, list[dict[str, Any]]], freeze_ticks: list[int]
    ) -> tuple[float, bool]:
        """Laske tickrate kierrosten alkuaikojen ja tickien suhteesta.

        ``m_fRoundStartTime`` on pelin sekuntikello, tick demon oma laskuri.
        Kahden kierroksen välinen suhde antaa tickraten suoraan; mediaani
        suojaa yksittäiseltä uudelleenkäynnistykseltä. Demon otsikossa
        tickratea ei ole.

        Returns:
            ``(tickrate, mitattiinko)``. Jos mittausta ei saatu tai tulos on
            järkevyysrajojen ulkopuolella, palautetaan
            :data:`DEFAULT_TICK_RATE` ja ``False`` -- vaihe kertoo sen
            käyttäjälle, jottei oletus mene läpi mittauksena.
        """
        observations: list[float] = []
        previous: tuple[int, float] | None = None
        for tick in freeze_ticks:
            rows = by_tick.get(tick) or []
            time_s = next(
                (
                    r["round_start_time"]
                    for r in rows
                    if r["round_start_time"] is not None
                ),
                None,
            )
            if time_s is None:
                continue
            if previous is not None:
                d_tick = tick - previous[0]
                d_time = time_s - previous[1]
                if d_tick > 0 and d_time > 0:
                    observations.append(d_tick / d_time)
            previous = (tick, time_s)
        if not observations:
            return DEFAULT_TICK_RATE, False
        rate = statistics.median(observations)
        if not TICK_RATE_MIN <= rate <= TICK_RATE_MAX:
            return DEFAULT_TICK_RATE, False
        rounded = round(rate)
        clean = float(rounded) if abs(rate - rounded) < 0.05 else float(rate)
        return clean, True

    @staticmethod
    def _lineup_keys(lineups: list[_Lineup]) -> list[str]:
        """Kokoonpanojen tunnisteet; ne eivät saa olla samat.

        Sama tunniste tarkoittaisi, ettei joukkueita voi erottaa toisistaan --
        ja silloin jokainen joukkuekohtainen luku olisi molempien summa.
        """
        lineup_keys = [lineup.key() for lineup in lineups]
        if lineup_keys[0] == lineup_keys[1]:
            raise ParseError(
                "Molemmille joukkueille tuli sama kokoonpanotunniste, joten "
                "niitä ei voi erottaa toisistaan.\n"
                "Kierrosrajojen tickeissä näkyy sama pelaajajoukko molemmilla "
                "puolilla. Demo on todennäköisesti vioittunut."
            )
        return lineup_keys

    @staticmethod
    def _build_lineups_frame(
        lineups: list[_Lineup], lineup_keys: list[str]
    ) -> pl.DataFrame:
        """Rakenna kokoonpanotaulu: rivi per (kokoonpano, pelaaja).

        Lähde on sama ankkuritick-joukko, jolta kokoonpanot jo tunnistettiin
        (:meth:`_assign_sides`), joten demoa ei lueta uudelleen. Rivejä syntyy
        täsmälleen niistä pelaajista, joista ``lineup_key`` on laskettu -- eli
        taulun pelaajajoukko ja tunniste eivät voi olla eri mieltä.

        Nimi ja klaani ovat **useimmin havaitut**; tasatilanne ratkeaa
        aakkosjärjestyksessä, jotta sama demo antaa saman tuloksen ajosta
        toiseen. Havainnon puuttuminen on ``null`` eikä korvike.

        Moodin valinta **hukkaa ristiriidan**, joten se lasketaan erikseen
        diagnostiikkaan (``lineup_name_conflicts``, ``lineup_clan_conflicts``).
        Ilman sitä oletus "yksi nimi ja yksi klaani per pelaaja" olisi
        ajonaikaisesti tarkistamaton: rikkoutuneena se näyttäisi taulussa
        täsmälleen samalta kuin ehjänä.
        """
        rows: list[dict[str, Any]] = []
        # strict: pituusero pudottaisi kokoonpanon hiljaa, ja juuri se
        # invariantti -- taulun pelaajajoukko on se, josta lineup_key on
        # laskettu -- on tämän taulun koko lupaus.
        for lineup, key in zip(lineups, lineup_keys, strict=True):
            for player_id in sorted(lineup.members):
                rows.append(
                    {
                        "lineup_key": key,
                        "player_id": player_id,
                        "player_name": _most_observed(lineup.names.get(player_id)),
                        "clan_name": _most_observed(lineup.clans.get(player_id)),
                    }
                )
        schema: dict[str, Any] = {
            name: LINEUPS[name] for name in LINEUPS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row")

    def _build_frame(
        self,
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
        buy_ticks: list[int | None],
        window_ticks: list[int | None],
    ) -> tuple[pl.DataFrame, _ArmedCounters, _BuyWindowCounters]:
        """Rakenna kierrostaulu.

        Talousarvot luetaan ``buy_ticks[index]``-tickiltä (ostoajan loppu),
        voittaja ja eloonjääneet ``segment.end_tick``iltä. Ankkuri
        ``freeze_end_tick`` on yhä rivillä, mutta siitä ei enää lueta lukuja --
        se on kierroksen aikanollakohta.

        ``window_ticks[index]`` on ei-``None`` vain silloin, kun kuolema
        katkaisi ikkunan: se on se tick, jolta olisi mitattu ilman katkaisua,
        ja sitä käytetään pelkästään sen laskemiseen, jäikö ostoja katkaisun
        taakse (``cash_spent`` kasvaa vain ostoista, ei kuolemista).
        """
        armed = _ArmedCounters()
        buy = _BuyWindowCounters()
        anchor_score = [
            _total_score(by_tick.get(s.freeze_end_tick or -1) or []) for s in segments
        ]
        end_score = [_total_score(by_tick.get(s.end_tick or -1) or []) for s in segments]

        # Edellisen kierroksen eloonjääneiden varustearvo, joukkueittain.
        previous_saved: list[int | None] = [None, None]
        rows: list[dict[str, Any]] = []

        for index, segment in enumerate(segments):
            freeze_rows = by_tick.get(segment.freeze_end_tick or -1) or []
            end_rows = by_tick.get(segment.end_tick or -1) or []

            # Ostoajan lopun tick. Varasääntö on tarkoituksella ankkuri eikä
            # tyhjä joukko: jos tick jää demon lopun taakse, koko kierroksen
            # talous olisi muuten null. Palautus lasketaan, koska se on vika.
            buy_tick = buy_ticks[index]
            buy_rows = by_tick.get(buy_tick if buy_tick is not None else -1) or []
            fell_back = False
            if buy_tick is not None and not buy_rows and freeze_rows:
                buy.ticks_without_players += 1
                fell_back = True
                buy_tick = segment.freeze_end_tick
                buy_rows = freeze_rows

            # Tuntemattomat nimet skannataan **molemmilta tickeiltä** ja
            # kaikilta riveiltä, ei vain laskuriin kelpaavilta. Kaksi syytä:
            # uusi asenimi voi esiintyä ensimmäisen kerran pelaajalla, jonka
            # talousarvot eivät ole luettavissa (_readable pudottaa hänet), ja
            # ase voi olla hallussa vain toisella tickillä -- pelaaja, joka
            # pudottaa tai vaihtaa aseen ostoajan aikana, näyttäisi vain
            # toisesta hetkestä katsottuna siltä ettei nimeä koskaan ollut.
            # Sama nimi samalla pelaajalla lasketaan silti kerran per kierros,
            # jottei kahden tickin luku kaksinkertaistaisi esiintymämääriä.
            seen_unknown: set[tuple[str, str]] = set()
            for row in (*freeze_rows, *buy_rows):
                for name in row.get("inventory") or ():
                    key = (row["steamid"], name)
                    if name not in KNOWN_INVENTORY_ITEMS and key not in seen_unknown:
                        seen_unknown.add(key)
                        armed.unknown_items[name] += 1

            # Numeroimaton segmentti (ottelun uudelleenaloitus) ei ole
            # kierros: se ei tuota riviä. Ankkurin tavaraluettelot luetaan
            # silti yllä, koska uusi asenimi voi esiintyä ensimmäisen kerran
            # juuri siinä. Segmentti pysyy listassa, jotta edellisen
            # kierroksen ``score_end`` luetaan yhä **sen** ankkurista --
            # juuri siitä lukemasta puukkokierroksen nollaus näkyy.
            if segment.round_raw is None:
                # Uudelleenaloitus nollaa kaluston, joten seuraava kierros ei
                # peri eloonjääneiden varusteita sitä edeltäneeltä
                # kierrokselta. Sama tulos kuin ennenkin: haamulla ei ole
                # päättymistickiä, joten sen oma summa olisi tyhjä.
                previous_saved = [None, None]
                continue

            score_start = anchor_score[index]
            if score_start is None:
                score_start = _score_before(index, segments, anchor_score, end_score)
            if score_start is None:
                score_start = end_score[index]

            score_end = anchor_score[index + 1] if index + 1 < len(segments) else None
            if score_end is None:
                score_end = end_score[index]

            # Kuoleman katkaisema ikkuna: kerrotaan aina, ja lisäksi katsotaan
            # **maksoiko se mitään**. cash_spent kasvaa vain ostoista eikä
            # reagoi kuolemiin, joten sen kasvu katkaisun ja ikkunan lopun
            # välillä on suora mittari sille, montako ostosta jäi mittauksen
            # taakse.
            #
            # Varasääntöön pudonnutta kierrosta ei kirjata katkaisuksi.
            # Mittauspiste on silloin ankkuri eikä katkaisukohta, joten
            # ikkunan loppuun verrattu ero olisi tyhjän tickin syytä eikä
            # kuoleman -- ja se on jo laskettu omaan lukuunsa.
            window_tick = window_ticks[index]
            if window_tick is not None and not fell_back:
                missed, compared = _purchases_between(
                    buy_rows, by_tick.get(window_tick) or []
                )
                buy.cuts.append((segment.round_raw, missed))
                if not compared:
                    buy.unchecked_cuts.append(segment.round_raw)

            # Palautettu ostos ja sen jättämä vanhentunut varustearvo. Vain
            # silloin kun tickit ovat eri: samalta tickiltä verrattuna jokainen
            # arvo on triviaalisti sama.
            if buy_tick is not None and buy_tick != segment.freeze_end_tick:
                refunds, stale = _refunds_and_stale_equipment(freeze_rows, buy_rows)
                buy.refunds += refunds
                buy.stale_equipment += stale

            saved_now: list[int | None] = [None, None]
            for team_index, side in enumerate(sides[index]):
                own_buy = _readable([r for r in buy_rows if r["side"] == side])
                own_end = [r for r in end_rows if r["side"] == side]
                alive = [r for r in own_end if r["alive"]]
                armed_count = _armed_count(own_buy)
                armored_count = _armored_count(own_buy)
                # Tyhjä joukko on ankkuriton kierros, ei lukuvirhe -- vain
                # jälkimmäinen lasketaan, jotta luku kertoo propivikaa eikä
                # normaalia puutetta.
                if armed_count is None and own_buy:
                    armed.unreadable_rows += 1
                if armored_count is None and own_buy:
                    armed.armored_unreadable_rows += 1

                # Ankkurilla luettavissa olleet pelaajat, jotka eivät ole enää
                # mittauspisteessä. Summa ja jakaja kutistuvat yhdessä, joten
                # per pelaaja -arvot pysyvät oikeina -- mutta joukkue näyttää
                # pelaavan vajaalla, ja se on eri väite kuin "yhteys katkesi".
                if not fell_back:
                    at_anchor = _readable([r for r in freeze_rows if r["side"] == side])
                    if len(own_buy) < len(at_anchor):
                        buy.players_lost += len(at_anchor) - len(own_buy)
                        if not own_buy:
                            buy.sides_without_rows += 1
                saved_now[team_index] = (
                    _sum_or_zero([r["equip_current"] for r in alive])
                    if own_end
                    else None
                )
                rows.append(
                    {
                        "round_raw": segment.round_raw,
                        "round_no": None,
                        "lineup_key": lineup_keys[team_index],
                        "side": side,
                        "won": (
                            None
                            if segment.winner_side is None
                            else segment.winner_side == side
                        ),
                        "win_reason": segment.win_reason,
                        "money_buy_end": _sum_or_none(
                            [r["account"] for r in own_buy]
                        ),
                        "money_spent": _sum_or_none(
                            [r["cash_spent"] for r in own_buy]
                        ),
                        "equip_buy_end": _sum_or_none(
                            [r["equip_current"] for r in own_buy]
                        ),
                        "equip_round_start": _sum_or_none(
                            [r["equip_round_start"] for r in own_buy]
                        ),
                        # Kynnykset ovat per pelaaja, joten jakaja on
                        # havaittava eikä oletettava: vajaalla pelaava
                        # joukkue näyttäisi viidellä jaettuna ecolta.
                        # Jakaja on sama joukko kuin summissa (ks. _readable).
                        "players_buy_end": len(own_buy) or None,
                        # Sama joukko ja sama järjestys joka ajolla:
                        # rahasaldot yksi pelaaja kerrallaan, laskevasti
                        # lajiteltuna. Arvot ovat jo käsillä -- tähän asti ne
                        # vain summattiin, ja summa peittää juuri sen mistä
                        # puolioston säännössä on kyse.
                        MONEY_DISTRIBUTION_COLUMN: (
                            sorted(
                                (int(r["account"]) for r in own_buy),
                                reverse=True,
                            )
                            or None
                        ),
                        # Sama joukko kuin summissa ja jakajassa. Kaksi eri
                        # jakajaa samalla rivillä olisi vika, joka näkyisi
                        # vasta raportissa.
                        ARMED_COLUMN: armed_count,
                        # Sama joukko, sama tick ja sama panssarilukema kuin
                        # yllä -- eri ehto. Kaksi laskuria eikä yksi, koska
                        # ne vastaavat eri kysymyksiin: ylempi on puolioston
                        # kalibroitu ehto A, tämä on "monellako oli panssari".
                        ARMORED_COLUMN: armored_count,
                        "survivors": len(alive) if own_end else None,
                        "survivors_equip_prev": previous_saved[team_index],
                        "freeze_end_tick": segment.freeze_end_tick,
                        "buy_end_tick": buy_tick,
                        "tick_rate": tick_rate,
                        "status": (
                            "ok"
                            if segment.freeze_end_tick is not None
                            else "no_freeze_end"
                        ),
                        "score_start": score_start,
                        "score_end": score_end,
                    }
                )
            previous_saved = saved_now

        return self._typed_frame(rows), armed, buy

    @staticmethod
    def _assign_sides(
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        lineups: list[_Lineup],
    ) -> list[tuple[str, str]]:
        """Päätä kummalla puolella kumpikin kokoonpano on kullakin kierroksella.

        Joukkueet vaihtavat puolta puoliajalla ja jatkoajassa, joten puoli ei
        kelpaa joukkueen tunnisteeksi. Kokoonpanot tunnistetaan pelaajajoukkojen
        päällekkäisyydestä: se kestää sekä puolenvaihdon että yksittäisen
        pelaajavaihdon.

        Tasapeliä **ei ratkaista arvaamalla**. Jos kumpikaan kuvaus ei voita,
        käytetään edellisen kierroksen kuvausta; jos edellistäkään ei ole,
        parsinta keskeytetään. Hiljainen oletus kohdistaisi voitot väärälle
        joukkueelle.

        **Ottelun uudelleenaloitus ohitetaan kokonaan.** Se on juuri se hetki,
        jolloin joukkue- ja puolitila on epävakain: pelaajia siirretään,
        yhdistetään uudelleen ja puolet asetetaan uusiksi. Yksikin väärä lukema
        siellä jäisi pysyvästi ``lineups``iin ja voisi kääntää puolet kaikille
        sen jälkeisille kierroksille. Segmentti saa silti oman alkionsa, jotta
        lista pysyy segmenttien mittaisena; sitä ei käytetä mihinkään, koska
        uudelleenaloitus ei tuota riviä yhteenkään tauluun.

        Returns:
            Kierroksittain pari ``(kokoonpanon 0 puoli, kokoonpanon 1 puoli)``.
        """
        result: list[tuple[str, str]] = []
        previous: tuple[str, str] | None = None

        for segment in segments:
            if segment.round_raw is None:
                result.append(
                    _require_previous(previous, segment, "ottelun uudelleenaloitus")
                )
                continue
            rows = (
                by_tick.get(segment.freeze_end_tick or -1)
                or by_tick.get(segment.end_tick or -1)
                or []
            )
            sets_by_side = {
                side: {r["steamid"] for r in rows if r["side"] == side}
                for side in ("T", "CT")
            }
            if not sets_by_side["T"] and not sets_by_side["CT"]:
                result.append(_require_previous(previous, segment, "ei pelaajia"))
                continue

            if not lineups[0].members and not lineups[1].members:
                if not sets_by_side["T"] or not sets_by_side["CT"]:
                    raise ParseError(
                        "Ensimmäiseltä tunnistetulta kierrokselta löytyi "
                        "pelaajia vain toiselta puolelta, joten kokoonpanoja ei "
                        "voi erottaa.\n"
                        "Demo on todennäköisesti katkennut alusta."
                    )
                lineups[0].observe(rows, "T")
                lineups[1].observe(rows, "CT")
                previous = ("T", "CT")
                result.append(previous)
                continue

            direct = sum(
                len(sets_by_side[side] & lineups[i].members)
                for i, side in enumerate(("T", "CT"))
            )
            swapped = sum(
                len(sets_by_side[side] & lineups[i].members)
                for i, side in enumerate(("CT", "T"))
            )
            if direct == swapped:
                sides = _require_previous(
                    previous, segment, "kokoonpanot eivät erotu toisistaan"
                )
            else:
                sides = ("T", "CT") if direct > swapped else ("CT", "T")
            for i, side in enumerate(sides):
                lineups[i].observe(rows, side)
            previous = sides
            result.append(sides)
        return result

    @staticmethod
    def _typed_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna taulu sopimuksen tyypeillä.

        Tyypit annetaan eksplisiittisesti, koska pelkistä null-arvoista Polars
        päättelisi ``Null``-tyypin ja ``schemas.validate`` hylkäisi taulun.
        ``score_start`` ja ``score_end`` ovat osa porttisopimusta
        (``ROUNDS_ADAPTER_COLUMNS``); ``stages.parse`` pudottaa ne ennen
        kirjoitusta.
        """
        schema: dict[str, Any] = {
            name: ROUNDS.get(name, pl.Int32) for name in ROUNDS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row")

    # -- Näytepisteet --------------------------------------------------------

    def _sample_points(
        self,
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        sample_seconds: tuple[float, ...],
        all_deaths: list[tuple[int, str | None, str | None, str | None]],
    ) -> tuple[list[SamplePoint], int]:
        """Valitse hetket, joilta pelaajien sijainnit luetaan.

        Aikapisteet tulevat suoraan :func:`~pappascout.domain.sampling.sample_ticks`
        -funktiolta. Ensikontakti ratkaistaan kierros kerrallaan, koska sen
        sääntö vaatii tiedon siitä, kummalla puolella kumpikin pelaaja oli
        **tällä** kierroksella -- puolet vaihtuvat puoliajalla.

        Args:
            all_deaths: ``player_death``-tapahtumat, jotka ``_parse`` on jo
                lukenut ostoikkunaa varten. Ne annetaan tänne eikä parsita
                uudelleen; ``fallback_death`` ratkaisee vain sen, saako
                ensikontakti tulla niistä.

        Returns:
            ``(näytepisteet, tuntemattoman puolen takia ohitetut tapahtumat)``.
        """
        # Ottelun uudelleenaloitusta ei näytteistetä: sillä ei ole
        # kierrosnumeroa, johon rivit kiinnittyisivät. Alkuperäinen indeksi
        # kulkee mukana, koska ``sides`` ja ``segments`` ovat segmenttien
        # järjestyksessä -- ilman sitä kaikki uudelleenaloituksen jälkeiset
        # kierrokset lukisivat edellisen segmentin puolet.
        sampled: list[tuple[int, _Segment]] = []
        bounds: list[RoundBounds] = []
        for index, segment in enumerate(segments):
            raw = segment.round_raw
            if raw is None:
                continue
            sampled.append((index, segment))
            bounds.append(
                RoundBounds(
                    round_raw=raw,
                    freeze_end_tick=segment.freeze_end_tick,
                    end_tick=segment.end_tick,
                )
            )
        points = sample_ticks(bounds, tick_rate, sample_seconds)

        hurt = self._damage_events(parser, "player_hurt", original_path)
        deaths = all_deaths if self.fallback_death else []
        if not hurt and not deaths:
            return _sorted_points(points), 0

        lineup_of = _lineup_index_by_player(lineups)
        unknown_sides = 0
        for position, round_bounds in enumerate(bounds):
            if not round_bounds.is_samplable:
                continue
            index = sampled[position][0]
            player_sides = _side_lookup(lineup_of, sides[index], segments[index], by_tick)
            own_hurt, a = _with_sides(hurt, round_bounds, player_sides)
            own_deaths, b = _with_sides(deaths, round_bounds, player_sides)
            unknown_sides += a + b
            tick = first_contact_tick(
                own_hurt,
                round_bounds,
                exclude_weapons=self.exclude_weapons,
                death_events=own_deaths,
                fallback_death=self.fallback_death,
            )
            if tick is None:
                continue
            assert round_bounds.freeze_end_tick is not None  # is_samplable
            t_s = seconds_since_freeze_end(tick, round_bounds.freeze_end_tick, tick_rate)
            points.append(
                SamplePoint(
                    round_raw=round_bounds.round_raw,
                    tick=tick,
                    sample_kind=FIRST_CONTACT_SAMPLE,
                    sample_t_s=t_s,
                    t_s=t_s,
                )
            )
        return _sorted_points(points), unknown_sides

    def _damage_events(
        self, parser: Any, name: str, original_path: Path
    ) -> list[tuple[int, str | None, str | None, str | None]]:
        """``player_hurt`` neljänä kenttänä: ``(tick, tekijä, uhri, ase)``.

        Ensikontaktin sääntö tarvitsee vain nämä, joten pelaajakohtaisia
        kenttiä ei pyydetä -- ne olisivat 30 saraketta, joita mikään ei lue.

        Puolia ei liitetä tässä: sama pelaaja on eri puolella ennen ja jälkeen
        puoliajan, joten kuvaus on kierroskohtainen.
        """
        rows, _ = self._damage_rows(parser, name, original_path)
        return [
            (r["tick"], r["attacker_id"], r["victim_id"], r["weapon"])
            for r in rows
        ]

    def _death_events(
        self, parser: Any, original_path: Path
    ) -> tuple[list[dict[str, Any]], int]:
        """``player_death`` uhrin ja ampujan kentät mukaan lukien.

        Yksi kutsu, kolme käyttäjää: kuolemataulu, ostoikkunan rajaus ja
        ensikontaktin varalähde. Pelaajakohtaiset kentät maksavat saman
        tapahtumaluvun kuin ilman niitä, joten erillistä kevyttä kutsua ei ole
        -- kaksi kutsua voisi lisäksi antaa eri rivijoukon, jos kirjasto
        joskus muuttuu.

        Returns:
            ``(rivit, tickittömät)``. Jälkimmäinen on niiden tapahtumien
            määrä, joilta tick ei ollut luettavissa; ilman tickiä kuolemaa ei
            voi kohdistaa kierrokseen eikä laskea ``t_s``:ää.
        """
        return self._damage_rows(
            parser, "player_death", original_path, player=DEATH_PLAYER_PROPS
        )

    def _damage_rows(
        self,
        parser: Any,
        name: str,
        original_path: Path,
        *,
        player: Sequence[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Lue vahinkotapahtuma ja tarkista, että sen sarakkeet ovat tallella.

        Yksi lukija molemmille tapahtumille. Kaksi lähes samanlaista kopiota
        antaisi **ristiriitaiset korjausohjeet samasta uudelleennimeämisestä**:
        kadonnut ``user_steamid`` kehottaisi päivittämään toisella polulla
        ``DAMAGE_COLUMNS``in ja toisella ``DEATH_COLUMNS``in. Tässä jokainen
        puuttuva sarake nimetään **sen oman luettelon kanssa**, joten ohje on
        aina se, joka korjaa vian.

        Puuttuva **sarake** on virhe. Ilman tarkistusta ensikontakti häviäisi
        äänettömästi, ja kuolemataulusta tulisi alueeton mutta
        rakenteellisesti kelvollinen. Puuttuva **tapahtuma** ei ole virhe --
        kierros voi ratketa ilman yhtään vahinkoa, ja kuolemataulun tyhjyyden
        tarkistaa ``stages.parse``, joka näkee myös kierrosten määrän.

        Args:
            player: Pelaajakohtaiset kentät, jotka kirjasto palauttaa
                etuliitteillä ``user_*`` ja ``attacker_*``. ``None`` lukee vain
                :data:`DAMAGE_COLUMNS`-kentät.

        Returns:
            ``(rivit, tickittömät)``. Rivi on sanakirja, jossa ``tick``,
            ``attacker_id``, ``victim_id`` ja ``weapon`` ovat aina; muut
            kentät vain jos ``player`` annettiin. Tickitön tapahtuma
            **pudotetaan ja lasketaan** -- jokainen muu pudotussyy
            raportoidaan, eikä tämä saa olla poikkeus.
        """
        frame = self._event(parser, name, original_path, player=player)
        if frame is None:
            # Tapahtumaa ei ole demossa lainkaan. Se on mahdollista (kierros
            # voi ratketa ilman vahinkoa), joten se ei ole virhe.
            return [], 0

        # Sarake -> se luettelo, jota kehittäjän on korjattava. Pari eikä
        # pelkkä nimi: ohje ilman oikeaa luetteloa lähettäisi etsimään väärää
        # vakiota.
        required: dict[str, str] = {c: "DAMAGE_COLUMNS" for c in DAMAGE_COLUMNS}
        if player is not None:
            required.update({c: "DEATH_COLUMNS" for c in DEATH_COLUMNS})
        missing = [
            f"{column} ({owner})"
            for column, owner in required.items()
            if column not in frame.columns
        ]
        if missing:
            raise ParseError(
                f"Demon {original_path.name} tapahtumasta {name!r} puuttuu "
                f"sarake: {', '.join(missing)}.\n"
                "Ilman sitä jokainen tapahtuma hylättäisiin äänettömästi ja "
                "tulos väittäisi, ettei yhdelläkään kierroksella ollut "
                "ensikontaktia -- tai kuolemataulusta tulisi alueeton mutta "
                "kelvollisen näköinen. Kenttä on todennäköisesti nimetty "
                "uudelleen demoparser2:n päivityksessä; päivitä suluissa "
                "nimetty luettelo tiedostossa adapters/demo_parser.py."
            )

        rows: list[dict[str, Any]] = []
        without_tick = 0
        for row in frame.to_dict("records"):
            tick = _as_int(row.get("tick"))
            if tick is None:
                without_tick += 1
                continue
            entry: dict[str, Any] = {
                "tick": tick,
                "attacker_id": _as_str(row.get("attacker_steamid")),
                "victim_id": _as_str(row.get("user_steamid")),
                "weapon": _as_str(row.get("weapon")),
            }
            if player is not None:
                entry.update(
                    {
                        "victim_area": _as_str(row.get("user_last_place_name")),
                        "victim_x": _as_float(row.get("user_X")),
                        "victim_y": _as_float(row.get("user_Y")),
                        "victim_z": _as_float(row.get("user_Z")),
                        "victim_team": _as_int(row.get("user_team_num")),
                        "attacker_area": _as_str(
                            row.get("attacker_last_place_name")
                        ),
                        "attacker_x": _as_float(row.get("attacker_X")),
                        "attacker_y": _as_float(row.get("attacker_Y")),
                        "attacker_z": _as_float(row.get("attacker_Z")),
                        "attacker_team": _as_int(row.get("attacker_team_num")),
                    }
                )
            rows.append(entry)
        return rows, without_tick

    def _build_deaths_frame(
        self,
        death_rows: list[dict[str, Any]],
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        *,
        without_tick: int = 0,
    ) -> tuple[pl.DataFrame, _DeathCounts]:
        """Rakenna ``DEATHS``-muotoinen taulu luetuista kuolemista.

        Kierros ratkeaa **kuolintickistä**: sama jaksotus kuin utilityssä
        (:func:`_round_windows`), joten kuolema kuuluu sille kierrokselle,
        jonka rajojen sisään se osuu. Kierroksen ulkopuolinen kuolema ei saa
        ``t_s``:ää eikä siis riviä; puukkokierroksen kuolema saa molemmat ja
        putoaa vasta ``stages.parse``in numeroinnissa.

        Puoli ja kokoonpano tulevat kierroksen omasta puolikuvauksesta, ja
        tapahtuman ``team_num`` on varalähde pelaajalle, jota kierros ei
        tunne. Uhrin puolen puuttuminen pudottaa rivin -- kuolema, joka ei
        kuulu kummallekaan joukkueelle, ei kelpaa liitoksen kohteeksi.
        Ampujan puolen puuttuminen ei pudota mitään: ampujan omat havainnot
        ovat luettavissa, ja tyhjentäminen hukkaisi ne.

        Returns:
            ``(taulu, luvut)``. Taulu on tyhjä mutta sopimuksen mukainen, jos
            yksikään kuolema ei osu kierroksen sisään.
        """
        if not death_rows:
            return (
                self._typed_deaths_frame([]),
                _DeathCounts(without_tick=without_tick),
            )

        windows = _round_windows(segments)
        starts = [window[0] for window in windows]
        lineup_of = _lineup_index_by_player(lineups)
        sides_by_round: dict[int, dict[str, str]] = {}
        keys_by_round: dict[int, dict[str, str]] = {}

        rows: list[dict[str, Any]] = []
        outside = 0
        without_victim = 0
        without_victim_side = 0
        attacker_without_side = 0

        for death in death_rows:
            index = _round_of_tick(starts, windows, death["tick"])
            if index is None:
                outside += 1
                continue
            if index not in sides_by_round:
                sides_by_round[index] = _side_lookup(
                    lineup_of, sides[index], segments[index], by_tick
                )
                keys_by_round[index] = _keys_by_side(
                    sides[index], lineup_keys, segments[index]
                )
            player_sides = sides_by_round[index]
            keys = keys_by_round[index]

            # Kaksi eri syytä, kaksi eri laskuria. Uhriton tapahtuma ei
            # ole puolen päättelyn epäonnistuminen, ja yhdistettynä se
            # näyttäisi vialta, jota ei ole.
            if death["victim_id"] is None:
                without_victim += 1
                continue
            victim_side = _resolve_side(
                death["victim_id"], death["victim_team"], player_sides
            )
            if victim_side is None:
                without_victim_side += 1
                continue

            attacker_side: str | None = None
            if death["attacker_id"] is not None:
                attacker_side = _resolve_side(
                    death["attacker_id"], death["attacker_team"], player_sides
                )
                if attacker_side is None:
                    attacker_without_side += 1

            segment = segments[index]
            freeze_end = segment.freeze_end_tick
            if freeze_end is None:  # pragma: no cover - _round_windows takaa
                raise ParseError(
                    "Kuolema kohdistui kierrokselle "
                    f"(round_raw={segment.round_raw}), jolta puuttuu "
                    "ankkuri.\n"
                    "Ilman sitä t_s:ää ei voi laskea. Demo on "
                    "todennäköisesti vioittunut."
                )
            has_attacker = death["attacker_id"] is not None
            rows.append(
                {
                    "round_raw": segment.round_raw,
                    "round_no": None,
                    "t_s": seconds_since_freeze_end(
                        death["tick"], freeze_end, tick_rate
                    ),
                    "victim_id": death["victim_id"],
                    "victim_lineup_key": keys[victim_side],
                    "victim_side": victim_side,
                    "victim_x": death["victim_x"],
                    "victim_y": death["victim_y"],
                    "victim_z": death["victim_z"],
                    "victim_area": death["victim_area"],
                    "attacker_id": death["attacker_id"],
                    "attacker_lineup_key": (
                        None if attacker_side is None else keys[attacker_side]
                    ),
                    "attacker_side": attacker_side,
                    # Ampujattoman kuoleman jokainen ampujakenttä on tyhjä.
                    # Koordinaatit ja alue luetaan vain, jos ampuja on:
                    # maailman aiheuttamalla kuolemalla ei ole paikkaa, ja
                    # kirjaston jättämä irtoarvo näyttäisi ampujalta.
                    "attacker_x": death["attacker_x"] if has_attacker else None,
                    "attacker_y": death["attacker_y"] if has_attacker else None,
                    "attacker_z": death["attacker_z"] if has_attacker else None,
                    "attacker_area": (
                        death["attacker_area"] if has_attacker else None
                    ),
                    "weapon": death["weapon"],
                }
            )

        counts = _DeathCounts(
            without_tick=without_tick,
            outside_rounds=outside,
            without_victim=without_victim,
            without_victim_side=without_victim_side,
            attacker_without_side=attacker_without_side,
        )
        return self._typed_deaths_frame(rows), counts

    @staticmethod
    def _typed_deaths_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna kuolemataulu sopimuksen tyypeillä ja vakaassa järjestyksessä.

        Sarakkeet poimitaan **nimellä** eikä rividictin järjestyksessä, samasta
        syystä kuin tapahtumataulussa: taulussa on kolme peräkkäistä
        Float32-saraketta uhrille ja kolme ampujalle, ja ``orient="row"``
        vaihtaisi ne hiljaa keskenään, jos ``DEATHS``in avainjärjestys joskus
        muuttuu.

        Lajitteluavain on ``(round_raw, t_s, victim_id)``. ``victim_id`` on
        mukana, koska kaksi joukkuekaveria voi kuolla samalla tickillä --
        ilman sitä rivijärjestys riippuisi siitä, missä järjestyksessä
        kirjasto sattui palauttamaan tapahtumat, ja sama demo tuottaisi eri
        tavut eri ajoilla.
        """
        schema: dict[str, Any] = {
            name: DEATHS[name] for name in DEATHS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        columns = {name: [row[name] for row in rows] for name in schema}
        return pl.DataFrame(columns, schema=schema).sort(
            "round_raw", "t_s", "victim_id"
        )

    def _build_ticks_frame(
        self,
        points: list[SamplePoint],
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
    ) -> tuple[pl.DataFrame, int, dict[int, _SampleTickCounts], int]:
        """Lue pelaajien sijainnit näytepisteiden tickeiltä ja rakenna taulu.

        Rivi syntyy **jokaisesta** pelaajasta, myös kuolleesta: kuolleiden
        suodatus on aggregoinnin työ (AD-10), ei parsinnan. Tuntematon alue
        jää ``null``:ksi, mutta koordinaatit tallentuvat silti -- riviä ei
        pudoteta hiljaa.

        Returns:
            ``(taulu, vajaiden näytepisteiden määrä, tickikohtaiset
            rivilaskurit, kokonaan väliin jääneiden näytepisteiden määrä)``.

            **Vajaa näytepiste** on sellainen, jolta saatiin vähemmän pelaajia
            kuin demon parhaalta pisteeltä. Luku raportoidaan, koska
            systemaattinen propivika näkyisi muuten vasta vinoutuneina
            aggregaatteina. Pawniton rivi on **yksi syy siihen**, että
            näytepiste jää vajaaksi, ja juuri siksi molemmat luvut kerrotaan.

            **Kokonaan väliin jäänyt** näytepiste on eri asia eikä sisälly
            vajaisiin: siltä ei tullut riviäkään, koska jokainen rivi oli
            pawniton. Se on vakavampi kuin vajaa piste, joten se ei saa
            kadota vajaiden joukkoon -- eikä myöskään jäädä laskematta.
        """
        if not points:
            return self._typed_ticks_frame([]), 0, {}, 0

        wanted = sorted({p.tick for p in points})
        by_tick, tick_counts = self._read_sample_ticks(
            parser, wanted, original_path
        )
        points_without_pawn = 0
        # sides on segmenttien järjestyksessä, mutta näytepiste tuntee vain
        # round_raw-arvon, joten kuvaus tarvitaan takaisin segmentti-indeksiin.
        index_by_raw = {
            s.round_raw: index
            for index, s in enumerate(segments)
            if s.round_raw is not None
        }
        # Laiskasti eikä ahnaasti: _keys_by_side nostaa ParseErrorin, jos
        # molemmille kokoonpanoille tuli sama puoli. Ahne rakennus antaisi
        # uudelleenaloitukselle vallan kaataa koko ajon, vaikka se ei tuota
        # riviä yhteenkään tauluun -- ja virheviesti kertoisi sen round_raw:ksi
        # ``None``, joka ei auta lukijaa mihinkään.
        keys_per_round: dict[int, dict[str, str]] = {}

        rows: list[dict[str, Any]] = []
        players_per_point: list[int] = []
        for point in points:
            segment_index = index_by_raw.get(point.round_raw)
            if segment_index is None:  # pragma: no cover - sample_ticks takaa
                continue
            side_keys = keys_per_round.get(segment_index)
            if side_keys is None:
                side_keys = _keys_by_side(
                    sides[segment_index], lineup_keys, segments[segment_index]
                )
                keys_per_round[segment_index] = side_keys
            tick_rows = by_tick.get(point.tick, ())
            counts = tick_counts.get(point.tick, _SampleTickCounts())
            if (
                not tick_rows
                and counts.without_pawn
                and counts.without_pawn == counts.seen
            ):
                # Koko näytepiste pawniton: jokainen rivi oli olemassa mutta
                # kenelläkään ei ollut hahmoa kartalla. Se ei ole vika vaan
                # havainto -- demo ei palauttanut tyhjää, vaan pelaajia ei
                # ollut. Ajoa ei kaadeta; piste jää väliin ja sekä rivit että
                # piste itse näkyvät omissa laskureissaan.
                #
                # Ehto vaatii että pawnittomuus selittää tickin **kokonaan**.
                # Pelkkä "yksikin pawniton rivi" vaimentaisi kovan virheen
                # sattuman perusteella: tick, jolta yhdeksän riviä katosi
                # katsojina ja yksi pawnittomana, on yhä vika.
                points_without_pawn += 1
                continue
            if not tick_rows:
                raise ParseError(
                    f"Demon {original_path.name} naytepisteeltä "
                    f"(round_raw={point.round_raw}, {point.sample_kind}, "
                    f"t={point.sample_t_s:g} s, tick={point.tick}) ei saatu "
                    "yhtään pelaajariviä.\n"
                    "Tick on kierroksen rajojen sisällä, joten tyhjä tulos "
                    "tarkoittaa että demo on vioittunut tai demoparser2 ei "
                    "palauta tältä tickiltä mitään. Näytepiste laskettaisiin "
                    "mukaan lukuihin mutta puuttuisi taulusta."
                )
            players_per_point.append(len(tick_rows))
            for row in tick_rows:
                side = row["side"]
                rows.append(
                    {
                        "round_raw": point.round_raw,
                        "round_no": None,
                        "player_id": row["steamid"],
                        "lineup_key": side_keys[side],
                        "side": side,
                        "sample_kind": point.sample_kind,
                        "sample_t_s": point.sample_t_s,
                        "t_s": point.t_s,
                        "x": row["x"],
                        "y": row["y"],
                        "z": row["z"],
                        "area": row["area"],
                        "is_alive": row["alive"],
                    }
                )

        # Odotettu pelaajamäärä luetaan demosta itsestään: [thresholds] ei näy
        # tähän vaiheeseen (AD-3), joten roster_size'a ei voi käyttää.
        full_count = max(players_per_point, default=0)
        partial = sum(1 for count in players_per_point if count < full_count)
        return (
            self._typed_ticks_frame(rows),
            partial,
            tick_counts,
            points_without_pawn,
        )

    def _read_sample_ticks(
        self, parser: Any, ticks: list[int], original_path: Path
    ) -> tuple[dict[int, list[dict[str, Any]]], dict[int, _SampleTickCounts]]:
        """Lue sijaintipropit annetuilta tickeiltä ja ryhmittele tickin mukaan.

        **Pawniton pelaaja ei ole kierroksen osapuoli.** Kontrollerin ja
        pawnin ero on moduulin dokumentaatiossa mitattuna. Rivi, jolla
        kontrolleri on tallella mutta **jokainen** :data:`SAMPLE_PAWN_PROPS`in
        kenttä on tyhjä, kertoo pelaajasta jota ei ole kartalla -- hänen
        rivinsä ohitetaan kuten katsojan, omaan laskuriinsa merkittynä.

        Ohitus vaatii **kaikkien** pawn-kenttien puuttumisen eikä pelkän
        elossaolon: jos se laukeaisi pelkästä tyhjästä ``m_lifeState``ista,
        se söisi juuri sen vian, jota vastaan alla oleva vartija on olemassa.
        demoparser2:n päivitys, joka nimeäisi kentän uudelleen, tuottaisi
        tyhjän arvon jokaiselle riville, jokainen rivi ohitettaisiin, ja
        asetelma tyhjenisi äänettömästi. Kaikkien kenttien vaatiminen erottaa
        "pelaajaa ei ole" tilanteesta "kentän nimi vaihtui".

        **Vartija itse pysyy paikallaan pawnilliselle riville**, ja
        alkuperäisin sanoin: ``is_alive`` ei ole nullable, joten puuttuva
        arvo muuttuisi hiljaa arvoksi ``False`` ja elossa oleva pelaaja
        katoaisi aggregoinnista. Tuntematon alue saa jäädä nulliksi, mutta
        tämä ei voi.

        **Ohitus lepää oletuksella, että puuttuva pawn-kenttä tulee
        tyhjänä.** demoparser2 antaa puuttuvan arvon ``None``:na tai
        ``NaN``:ina, ja molemmat päätyvät ``None``:ksi ``_as_*``-muuntimissa;
        tyhjä ``m_szLastPlaceName`` on jo valmiiksi ``None``. Jos kirjasto
        joskus palauttaa pawnittomalle pelaajalle nollakoordinaatit tai
        nollan elossaolona, ohitus **ei laukea** ja alla oleva vartija kaataa
        ajon kuten ennen Story 2.10:tä. Se on tarkoituksellinen suunta: luku
        nolla on havainto siinä missä mikä tahansa muukin, eikä sitä saa
        tulkita puuttumiseksi.

        Returns:
            ``(tickeittäin ryhmitellyt rivit, :class:`_SampleTickCounts`
            tickeittäin)``.

            Jälkimmäinen on **tickeittäin eikä yhtenä summana**, koska kutsuja
            tarvitsee sen kolmeen eri asiaan: kokonaisluku menee
            diagnostiikkaan, tickikohtainen erottaa tyhjän tickin kahdesta eri
            syystä ("demo ei palauttanut mitään" vs. "jokainen rivi oli
            pawniton"), ja sama tick voi tulla luetuksi kahdesti eri
            kutsupolulta -- summattuna sama rivi laskettaisiin kahdesti.
        """
        if not ticks:
            return {}, {}
        try:
            frame = parser.parse_ticks(list(SAMPLE_TICK_PROPS), ticks=ticks)
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} näytepisteitä ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne näitä kenttiä. Aja: uv sync"
            ) from exc

        received = set(getattr(frame, "columns", ()))
        missing = [
            name
            for name in (*SAMPLE_TICK_PROPS, "tick", "steamid")
            if name not in received
        ]
        if missing:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia näytepisteen kenttiä "
                f"demosta {original_path.name}. Puuttuu: {', '.join(missing)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta asetelmataulu näyttäisi "
                "kelvolliselta mutta olisi tyhjä tai paikaton. Päivitä "
                "adapters/demo_parser.py:n propinimet."
            )

        by_tick: dict[int, list[dict[str, Any]]] = defaultdict(list)
        seen: Counter[int] = Counter()
        without_pawn: Counter[int] = Counter()
        for row in frame.to_dict("records"):
            steamid = _as_str(row.get("steamid"))
            side = TEAM_SIDES.get(_as_int(row.get(_TEAM_NUM)) or -1)
            tick = _as_int(row.get("tick"))
            if tick is not None:
                # Nähdyt rivit lasketaan **ennen** yhtäkään ohitusta: vain
                # niitä vasten voi sanoa, selittikö pawnittomuus tyhjän
                # tickin kokonaan.
                seen[tick] += 1
            if steamid is None or side is None or tick is None:
                # Katsojat ja liittymättömät eivät ole kierroksen osapuolia.
                continue
            life_state = _as_int(row.get(_LIFE_STATE))
            # Tyhjä aluenimi on pelin tapa sanoa "ei nimettyä aluetta".
            # Se säilyy null:na; koordinaatit kertovat silti paikan.
            area = _as_str(row.get(_PLACE_NAME))
            x = _as_float(row.get(_X))
            y = _as_float(row.get(_Y))
            z = _as_float(row.get(_Z))
            # "Kaikki pawn-kentät tyhjiä" luettuna :data:`SAMPLE_PAWN_PROPS`in
            # kautta eikä käsin kirjoitettuna ketjuna. Uusi pawn-prop ilman
            # arvoa tässä sanakirjassa on ``KeyError`` eikä hiljaa löysempi
            # ohitus -- ja ``KeyError`` on oikea reaktio, koska ohituksen
            # kattavuus on koko korjauksen ehto.
            pawn_fields = {
                _LIFE_STATE: life_state,
                _PLACE_NAME: area,
                _X: x,
                _Y: y,
                _Z: z,
            }
            if all(pawn_fields[name] is None for name in SAMPLE_PAWN_PROPS):
                # Pawniton pelaaja: kontrolleri on tallella, mutta hahmoa ei
                # ole kartalla. Hän ei ole tämän tickin osapuoli, joten rivi
                # ohitetaan kuten katsojan -- ei arvata elossaoloa eikä
                # sijaintia. Laskuri pitää pudotuksen näkyvissä: puuttuva
                # pelaaja pienentää asetelmaa, ja lukijan on nähtävä se.
                without_pawn[tick] += 1
                continue
            if life_state is None:
                # is_alive ei ole nullable, joten puuttuva arvo muuttuisi
                # hiljaa arvoksi False ja elossa oleva pelaaja katoaisi
                # aggregoinnista. Tuntematon alue saa jäädä nulliksi, mutta
                # tämä ei voi.
                raise ParseError(
                    f"Demon {original_path.name} tickistä {tick} puuttuu "
                    f"pelaajan {steamid} {_LIFE_STATE}.\n"
                    "Elossaolo on pakollinen havainto: puuttuvasta arvosta "
                    "tulisi 'kuollut', ja pelaaja katoaisi asetelmasta "
                    "äänettömästi. Tarkista demoparser2:n versio."
                )
            by_tick[tick].append(
                {
                    "steamid": steamid,
                    "side": side,
                    "area": area,
                    "x": x,
                    "y": y,
                    "z": z,
                    "alive": life_state == _ALIVE,
                }
            )
        return dict(by_tick), {
            tick: _SampleTickCounts(seen=count, without_pawn=without_pawn[tick])
            for tick, count in seen.items()
        }

    @staticmethod
    def _typed_ticks_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna näytepistetaulu sopimuksen tyypeillä.

        Tyypit annetaan eksplisiittisesti samasta syystä kuin kierrostaulussa:
        pelkistä null-arvoista Polars päättelisi ``Null``-tyypin.
        """
        schema: dict[str, Any] = {name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS}
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row")

    # -- Utility -------------------------------------------------------------

    def _build_events_frame(
        self,
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        cloud: pl.DataFrame,
    ) -> tuple[pl.DataFrame, _UtilityCounts, dict[int, _SampleTickCounts]]:
        """Lue lentoradat ja rakenna niistä ``EVENTS``-muotoinen taulu.

        Järjestys on tarkoituksellinen: rata pelkistetään päätepisteiksi
        **ennen** kuin mitään muuta tehdään, jolloin 1,55 miljoonaa riviä
        kutistuu noin 750:een eikä kulje vaiheiden läpi kokonaisena.

        Kierros ratkeaa **heitosta**: kierroksen lopussa heitetty savu kuuluu
        sille kierrokselle, jolta se lähti, vaikka se palaisi vasta seuraavan
        puolella. Molemmat rivit saavat siis saman ``round_raw``:n, ja
        räjähdyksen ``t_s`` voi ylittää kierroksen keston -- se on oikea
        havainto eikä virhe.

        Alue on kahdenlaista tietoa. Heittäjällä on oma ``m_szLastPlaceName``
        samalta tickiltä, joten heiton alue on **havainto**
        (``area_source = "observed"``). Kranaatilla ei ole aluenimeä, joten
        räjähdyksen alue luetaan **pistepilvestä**: lähimmän ruudun alue
        (``"point_cloud"``), ja etäisyys tallentuu, jotta kuluttaja voi
        erottaa varman osuman kaukaisesta arviosta.

        Räjähdysalueet ratkaistaan **yhtenä eränä** eikä rivi kerrallaan:
        vertailu on jokainen räjähdys jokaista ruutua vasten, ja erä antaa
        Polarsin tehdä sen kerran satojen pienten kutsujen sijaan.

        Tickejä luetaan vain **heittojen** kohdalta. Räjähdyksen tickillä ei
        ole enää mitään luettavaa: sen alue tulee pilvestä eikä siitä, ketkä
        sattuivat olemaan lähellä.

        Returns:
            ``(taulu, luvut)``. Taulu on tyhjä mutta sopimuksen mukainen, jos
            demossa ei ollut yhtään heitettyä kranaattia.
        """
        raw = self._read_grenades(parser, original_path)
        if raw.is_empty():
            return self._typed_events_frame([]), _UtilityCounts(), {}

        endpoints, without_thrower = self._endpoints(raw, tick_rate, original_path)
        if endpoints.is_empty():
            return (
                self._typed_events_frame([]),
                _UtilityCounts(without_thrower=without_thrower),
            )
        unknown_type = _unknown_type_count(endpoints)
        endpoints, fire_unresolved = _name_fire_grenades(
            endpoints, raw, trajectory_gap_ticks(tick_rate)
        )

        windows = _round_windows(segments)
        starts = [window[0] for window in windows]

        round_of_throw: dict[int, int] = {}
        outside = 0
        throws = endpoints.filter(pl.col("event_kind") == THROWN)
        for row in throws.iter_rows(named=True):
            index = _round_of_tick(starts, windows, row["tick"])
            if index is None:
                outside += 1
                continue
            round_of_throw[row["grenade_no"]] = index

        lineup_of = _lineup_index_by_player(lineups)
        sides_by_round: dict[int, dict[str, str]] = {}
        keys_by_round: dict[int, dict[str, str]] = {}

        selected: list[dict[str, Any]] = []
        unknown_side_count = 0
        for row in endpoints.iter_rows(named=True):
            index = round_of_throw.get(row["grenade_no"])
            if index is None:
                continue
            if index not in sides_by_round:
                sides_by_round[index] = _side_lookup(
                    lineup_of, sides[index], segments[index], by_tick
                )
                keys_by_round[index] = _keys_by_side(
                    sides[index], lineup_keys, segments[index]
                )
            side = sides_by_round[index].get(row["thrower_id"])
            if side is None:
                # Kranaatti pudotetaan kokonaan, mutta lasketaan kerran --
                # heitosta, jotta luku on kranaatteja eikä rivejä.
                if row["event_kind"] == THROWN:
                    unknown_side_count += 1
                continue
            selected.append(
                {
                    **row,
                    "_segment": index,
                    "_side": side,
                    "_lineup": keys_by_round[index][side],
                }
            )

        wanted = sorted({r["tick"] for r in selected if r["event_kind"] == THROWN})
        # Tyhjä lista **ei** saa mennä parse_ticksille: se tarkoittaa
        # demoparser2:lle "kaikki tickit". Pistepilvi lukee koko tickisarjan
        # tarkoituksella ja kerran; tämä kutsu ei saa tehdä sitä vahingossa
        # toista kertaa. Tilanne syntyy, jos jokainen kranaatti putoaa
        # kierrosten ulkopuolisena tai tuntemattoman puolen takia.
        positions, throw_tick_counts = (
            self._read_sample_ticks(parser, wanted, original_path)
            if wanted
            else ({}, {})
        )
        # Tyhjä tick on **vika** vain silloin, kun pawnittomuus ei selitä
        # sitä: silloin heittäjän omaa aluetta ei voitu edes yrittää lukea.
        # Kokonaan pawniton tick on havainto samalla säännöllä kuin
        # näytepisteillä, ja se on jo laskettu pawnittomien riveihin -- sama
        # ilmiö ei saa olla toisella polulla vika ja toisella havainto.
        empty_ticks = sum(
            1
            for tick in wanted
            if not positions.get(tick)
            and not _is_wholly_pawnless(throw_tick_counts.get(tick))
        )
        # Räjähdysalueet kerralla: pilvi ei muutu rivien välillä, joten
        # jokaisen rivin oma haku tekisi saman työn uudelleen.
        detonation_areas = self._detonation_areas(selected, cloud)

        rows: list[dict[str, Any]] = []
        late_detonations = 0
        throwers_without_row = 0
        for r in selected:
            segment = segments[r["_segment"]]
            freeze_end = segment.freeze_end_tick
            end_tick = segment.end_tick
            if freeze_end is None or end_tick is None:
                # _round_windows rakennetaan vain ankkurillisista kierroksista,
                # joten tämä ei voi tapahtua. Tarkistus on silti oikea eikä
                # assert: assert katoaa python -O:lla, ja seurauksena olisi
                # TypeError kesken 233 MB:n demon parsinnan.
                raise ParseError(
                    f"Demon {original_path.name} kranaatti kohdistui kierrokselle "
                    f"(round_raw={segment.round_raw}), jolta puuttuu ankkuri tai "
                    "päättymistick.\n"
                    "Ilman niitä t_s:ää ei voi laskea. Demo on todennäköisesti "
                    "vioittunut."
                )
            if r["event_kind"] == DETONATE and r["tick"] > end_tick:
                late_detonations += 1
            if r["event_kind"] == THROWN:
                area, source, distance, thrower_found = self._throw_area(
                    r, positions.get(r["tick"], ())
                )
                if not thrower_found:
                    # Heittäjää ei ollut riveissä: alue jää tyhjäksi eikä
                    # kukaan muu voi antaa sitä. Story 2.10:n jälkeen yksi
                    # syy tähän on pawniton heittäjä, jonka rivi ohitetaan
                    # -- ennen sitä tapaus kaatoi ajon.
                    throwers_without_row += 1
            else:
                area, distance = detonation_areas.get(r["grenade_no"], (None, None))
                source = "point_cloud" if area is not None else None
            rows.append(
                {
                    "round_raw": segment.round_raw,
                    "round_no": None,
                    "event_kind": r["event_kind"],
                    "grenade_no": r["grenade_no"],
                    "grenade_entity_id": r["grenade_entity_id"],
                    "grenade_type": r["grenade_type"],
                    "thrower_id": r["thrower_id"],
                    "lineup_key": r["_lineup"],
                    "side": r["_side"],
                    "t_s": seconds_since_freeze_end(r["tick"], freeze_end, tick_rate),
                    "x": r["x"],
                    "y": r["y"],
                    "z": r["z"],
                    "area": area,
                    "area_source": source,
                    "snap_distance": distance,
                }
            )

        frame = self._typed_events_frame(rows)
        counts = _UtilityCounts(
            without_thrower=without_thrower,
            outside_rounds=outside,
            unknown_side=unknown_side_count,
            unknown_type=unknown_type,
            fire_type_unresolved=fire_unresolved,
            detonating_after_round=late_detonations,
            ticks_without_players=empty_ticks,
            sharing_an_entity_id=_shared_entity_id_count(frame),
            throwers_without_row=throwers_without_row,
        )
        return frame, counts, throw_tick_counts

    @staticmethod
    def _throw_area(
        row: dict[str, Any],
        tick_players: Sequence[dict[str, Any]],
    ) -> tuple[str | None, str | None, float | None, bool]:
        """Heiton alue: heittäjän oma ``m_szLastPlaceName`` samalta tickiltä.

        Se on **havainto** eikä johdos, ja siksi tämä polku ei koske
        pistepilveen lainkaan: pilvi antaisi lähimmän ruudun alueen, vaikka
        oikea vastaus on luettavissa heittäjältä itseltään. Myös kuollut
        pelaaja kelpaa -- hän heitti kranaatin ollessaan elossa, ja rivi
        kertoo hänen oman alueensa.

        ``snap_distance`` on aina ``None``: havainnolla ei ole etäisyyttä.

        Returns:
            ``(alue, lähde, etäisyys, löytyikö heittäjä)``. Kolme ensimmäistä
            ovat tyhjiä, jos heittäjää ei ole tickin riveissä -- havaintoa ei
            korvata arviolla.

            **Neljäs erottaa kaksi tyhjää.** "Heittäjä löytyi, mutta pelillä
            ei ole nimeä hänen alueelleen" on havainto; "heittäjää ei ollut
            riveissä" on vika, ja sillä on oma laskurinsa
            (:attr:`_UtilityCounts.throwers_without_row`). Ilman tätä lippua
            ne näyttäisivät kutsujalle täsmälleen samalta.
        """
        for player in tick_players:
            if player["steamid"] == row["thrower_id"]:
                area = player["area"]
                return area, ("observed" if area is not None else None), None, True
        return None, None, None, False

    def _detonation_areas(
        self, selected: Sequence[dict[str, Any]], cloud: pl.DataFrame
    ) -> dict[int, tuple[str | None, float | None]]:
        """Nimeä kaikki räjähdykset pistepilvestä yhdellä kertaa.

        Avain on ``grenade_no``, joka on yksikäsitteinen koko demossa ja jolla
        radalla on **enintään yksi** räjähdysrivi -- pelin oma
        ``grenade_entity_id`` ei kelpaisi, koska se kierrätetään.

        **Myöhäinen räjähdys ei ole poikkeus.** Story 2.2:ssa kierroksen
        päättymisen jälkeen räjähtänyt kranaatti jätettiin aluetta vaille,
        koska silloinen menetelmä olisi lukenut alueen seuraavan kierroksen
        spawnissa seisovista pelaajista. Pistepilvi ei riipu hetkestä, joten
        syy katosi menetelmän mukana ja rivi saa alueensa kuten muutkin.

        Returns:
            ``grenade_no -> (alue, etäisyys)``. Etäisyys on tallessa myös
            silloin, kun alue jäi kynnyksen taakse; molemmat ovat ``None``
            vain, jos pilvi on tyhjä tai koordinaatteja ei ole.
        """
        rows = [r for r in selected if r["event_kind"] == DETONATE]
        if not rows:
            return {}
        points = pl.DataFrame(
            {
                "point_id": [int(r["grenade_no"]) for r in rows],
                "x": [r["x"] for r in rows],
                "y": [r["y"] for r in rows],
                "z": [r["z"] for r in rows],
            },
            schema={
                "point_id": pl.Int64,
                "x": pl.Float64,
                "y": pl.Float64,
                "z": pl.Float64,
            },
        )
        named = nearest_cells(
            points,
            cloud,
            grid_units=self.callout_grid_units,
            z_weight=self.callout_z_weight,
            z_tolerance_units=self.callout_z_tolerance_units,
            max_units=self.area_snap_units,
        )
        return {
            int(row["point_id"]): (row["area"], row["distance"])
            for row in named.iter_rows(named=True)
        }

    # -- Pistepilvi ----------------------------------------------------------

    def _build_callout_cloud(
        self, parser: Any, original_path: Path
    ) -> tuple[pl.DataFrame, _CloudCounts]:
        """Lue koko demon tickisarja ja pelkistä se ruudukoksi.

        Tämä on moduulin **ainoa** koko demon tickiluku, ja se on tarkoitus:
        kysymys on "missä kartalla on seisottu ja mikä alue kussakin kohdassa
        on", eikä siihen vastaa muutaman ankkurin otos. Aineisto pudotetaan
        ruudukoksi heti, joten miljoona riviä ei kulje eteenpäin.

        Tyhjä pilvi **ei ole virhe**: se on demo, josta ei saatu yhtään
        elossa-riviä nimetyllä alueella. Silloin jokainen räjähdysalue jää
        tyhjäksi, ajo jatkuu, ja syy kulkee diagnostiikassa ajon
        yhteenvetoon.

        Returns:
            ``(pistepilvi, luvut)``.
        """
        frame = self._read_cloud_ticks(parser, original_path)
        if frame is None:
            return self._typed_callouts_frame(empty_point_cloud()), _CloudCounts(
                empty_reason=(
                    "demoparser2 ei palauttanut yhtään tickiriviä koko demosta"
                )
            )
        # Muunnos ja pelkistys ovat saman virhekäärön sisällä: molemmat
        # nostavat kirjaston tai domainin oman virhetyypin, ja portin sopimus
        # lupaa suomenkielisen ParseErrorin. Ilman kääröä demoparser2:n
        # tyyppimuutos näkyisi paljaana PolarsErrorina keskellä 400 MB:n
        # parsintaa.
        try:
            observations = _cloud_observations(frame)
            # Pandas-kehys ei ole enää tarpeen. Se ei palauta muistia
            # käyttöjärjestelmälle -- mitattu työjoukko ei pienene -- mutta se
            # päästää varaajan käyttämään alueen uudelleen.
            del frame
            rows_read = observations.height
            cloud = build_point_cloud(
                observations, grid_units=self.callout_grid_units
            )
            del observations
        except (ValueError, pl.exceptions.PolarsError) as exc:
            raise ParseError(
                f"Demon {original_path.name} pistepilveä ei voitu rakentaa: "
                f"{exc}\n"
                "Joko demoparser2 palautti kentän odottamattomassa tyypissä "
                "tai [parse].callout_grid_units on kelvoton. Tarkista "
                "asetukset ja aja: uv sync"
            ) from exc

        reason = None
        if cloud.is_empty():
            reason = (
                f"{rows_read} tickiriviä luettiin, mutta yhdelläkään ei ollut "
                "elossa olevaa pelaajaa nimetyllä alueella"
                if rows_read
                else "demoparser2 ei palauttanut yhtään tickiriviä koko demosta"
            )
        return self._typed_callouts_frame(cloud), _CloudCounts(
            rows_read=rows_read, empty_reason=reason
        )

    @staticmethod
    def _typed_callouts_frame(cloud: pl.DataFrame) -> pl.DataFrame:
        """Aseta pistepilvelle portin sopimuksen sarakkeet ja tyypit.

        Domain rakentaa pilven omilla tyypeillään; tämä sitoo sen
        ``CALLOUT_CLOUD``-sopimukseen. Ilman sidosta skeeman tyypin muutos
        näkyisi vasta vaiheen ``validate``ssa, ja virheilmoitus syyttäisi
        vaihetta työstä, jonka adapteri jätti tekemättä.
        """
        schema: dict[str, Any] = {
            name: CALLOUT_CLOUD[name] for name in CALLOUTS_ADAPTER_COLUMNS
        }
        return cloud.select(CALLOUTS_ADAPTER_COLUMNS).cast(schema)

    def _read_cloud_ticks(self, parser: Any, original_path: Path) -> Any:
        """Lue :data:`CLOUD_TICK_PROPS` koko demosta ja tarkista sarakkeet.

        Tyhjä tulos ei ole virhe -- pistepilvi jää silloin tyhjäksi ja syy
        kerrotaan. Puuttuva **sarake** on virhe: ilman tarkistusta pilvi olisi
        tyhjä eikä sitä voisi erottaa demosta, jossa kukaan ei liikkunut, ja
        jokainen räjähdys jäisi aluetta vaille kertomatta miksi.
        """
        try:
            frame = parser.parse_ticks(list(CLOUD_TICK_PROPS))
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} pistepilveä ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne näitä kenttiä. Aja: uv sync"
            ) from exc

        if frame is None or not hasattr(frame, "columns"):
            return None

        # SARAKKEET ENNEN TYHJYYTTÄ. Uudelleennimetty kenttä voi tuottaa
        # kehyksen, jossa on sarakkeet mutta nolla riviä, ja tyhjyystarkistus
        # ensin muuttaisi sopimusrikon havainnoksi "demossa ei ollut tickejä".
        # Se on juuri se hiljainen tulkinta, jonka tämä vartija estää.
        missing = [name for name in CLOUD_TICK_PROPS if name not in frame.columns]
        if missing:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia pistepilven kenttiä "
                f"demosta {original_path.name}. Puuttuu: {', '.join(missing)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta pistepilvi olisi tyhjä ja "
                "jokainen räjähdysalue null -- eikä mikään kertoisi miksi. "
                "Päivitä adapters/demo_parser.py:n CLOUD_TICK_PROPS."
            )
        if len(frame) == 0:
            return None
        return frame

    def _endpoints(
        self, raw: pl.DataFrame, tick_rate: float, original_path: Path
    ) -> tuple[pl.DataFrame, int]:
        """Kutsu domainin pelkistystä ja käännä sen virheet suomeksi.

        Puuttuva sarake voi paljastua kahdessa kohdassa: Polars nostaa
        ``ColumnNotFoundError``in jo muunnoksessa, ja ``grenade_endpoints``
        nostaa ``ValueError``in omassa tarkistuksessaan. Kumpi tahansa on sama
        vika kuin :meth:`_read_grenades`in oma tarkistus havaitsee, joten
        kaikkien kolmen on näytettävä käyttäjälle samalta -- eikä paljaalta
        pinojäljeltä.
        """
        try:
            return grenade_endpoints(
                _trajectory_frame(raw),
                max_gap_ticks=trajectory_gap_ticks(tick_rate),
            )
        except (ValueError, pl.exceptions.PolarsError) as exc:
            raise ParseError(
                f"Demon {original_path.name} lentoratoja ei voitu pelkistää: "
                f"{exc}\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Päivitä adapters/demo_parser.py:n "
                "GRENADE_COLUMNS."
            ) from exc

    def _read_grenades(self, parser: Any, original_path: Path) -> pl.DataFrame:
        """Lue ``parse_grenades()`` ja tarkista, että sarakkeet ovat tallella.

        Tyhjä tulos ei ole virhe: demossa ei välttämättä heitetty yhtään
        kranaattia. Puuttuva **sarake** on virhe, koska silloin tulos olisi
        tyhjä eikä sitä voisi erottaa utilityttömästä demosta.
        """
        try:
            frame = parser.parse_grenades()
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} lentoratoja ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne parse_grenades-metodia. Aja: uv sync"
            ) from exc

        if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
            return pl.DataFrame()

        missing = [name for name in GRENADE_COLUMNS if name not in frame.columns]
        if missing:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia lentoradan kenttiä demosta "
                f"{original_path.name}. Puuttuu: {', '.join(missing)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta utility-taulu olisi tyhjä ja "
                "näyttäisi demolta, jossa ei heitetty yhtään kranaattia. "
                "Päivitä adapters/demo_parser.py:n GRENADE_COLUMNS."
            )
        return _as_polars(frame, GRENADE_COLUMNS)

    @staticmethod
    def _typed_events_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna tapahtumataulu sopimuksen tyypeillä ja vakaassa järjestyksessä.

        Lajittelu on eksplisiittinen: matkan varrella tehdyt liitokset eivät
        säilytä rivijärjestystä, ja sama demo tuottaisi muuten eri tavut eri
        ajoilla. ``event_kind`` on Enum, joten sen järjestys on luettelon
        järjestys -- heitto ennen räjähdystä.

        Toisena avaimena on ``grenade_no`` eikä pelin oma tunniste, ja siihen
        on kaksi syytä. Se on **yksikäsitteinen**, joten avain määrää
        järjestyksen täysin eikä jää riippumaan lajittelun vakaudesta. Ja se
        pitää radan kaksi riviä **vierekkäin**: pelin tunnisteella
        lajiteltuna kierrätetyn tunnisteen kaikki heitot tulisivat ennen sen
        kaikkia räjähdyksiä, ja pari hajoaisi taulun eri kohtiin.
        """
        schema: dict[str, Any] = {
            name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        # Sarakkeet poimitaan **nimella**, ei rividictin jarjestyksessa.
        # ``orient="row"`` lukisi arvot jarjestyksessa, ja kaksi vierekkaista
        # Int32-saraketta (grenade_no, grenade_entity_id) vaihtaisi silloin
        # hiljaa paikkaa, jos EVENTSin avainjarjestys joskus muuttuu -- ilman
        # tyyppivirhetta, joka paljastaisi sen.
        columns = {name: [row[name] for row in rows] for name in schema}
        return pl.DataFrame(columns, schema=schema).sort(
            "round_raw", "grenade_no", "event_kind", "t_s"
        )


def _as_polars(frame: Any, columns: Sequence[str]) -> pl.DataFrame:
    """Muunna demoparser2:n taulu Polarsiksi, vain pyydetyt sarakkeet.

    Sarakevalinta tehdään **ennen** muunnosta: ``name`` on 1,55 miljoonan rivin
    merkkijonosarake, jota ei tarvita mihinkään -- tunniste on ``steamid``.
    """
    if isinstance(frame, pl.DataFrame):
        return frame.select(columns)
    return pl.from_pandas(frame[list(columns)])


def _thrower_id() -> pl.Expr:
    """``steamid`` merkkijonoksi niin, ettei tunniste mene liukuluvuksi.

    Pandas nostaa kokonaislukusarakkeen ``float64``:ksi heti kun siinä on yksi
    tyhjä arvo. Suora ``cast(Utf8)`` tekisi silloin jokaisesta tunnisteesta
    muotoa ``"7.6561e+16"``, puolihaku ei osuisi yhteenkään pelaajaan ja
    **kaikki kranaatit putoaisivat tuntemattomana puolena** -- taulu olisi
    tyhjä eikä mikään kertoisi miksi. Kierto kokonaisluvun kautta antaa saman
    desimaalimuodon kuin tickien ``steamid``.
    """
    return pl.coalesce(
        pl.col("steamid").cast(pl.Int64, strict=False).cast(pl.Utf8),
        pl.col("steamid").cast(pl.Utf8, strict=False),
    )


def _cloud_observations(frame: Any) -> pl.DataFrame:
    """Pistepilven havainnot domainin sarakenimillä ja tyypeillä.

    Kääntää demoparser2:n propinimet (:data:`CLOUD_TICK_PROPS`) domainin
    nimiksi (``x``, ``y``, ``z``, ``area``, ``is_alive``), jotta
    :func:`~pappascout.domain.utility.build_point_cloud` pysyy puhtaana eikä
    tunne pelin kenttiä. Sama käännös kuin :func:`_trajectory_frame`illa
    tekee lentoradoille.

    **Tyhjä aluenimi ei ole alue.** Peli antaa nimettömälle alueelle tyhjän
    merkkijonon, ja se muutetaan tässä ``null``:iksi -- samoin kuin
    näytepisteillä. Ilman muunnosta pilveen syntyisi ruutuja, joiden alue on
    ``""``: räjähdys saisi niistä tyhjän nimen ja näyttäisi silti osumalta.

    ``is_alive`` on ``null``, jos ``m_lifeState`` puuttuu. Se **ei** muutu
    tässä epätodeksi: pilven suodatin hylkää null:in joka tapauksessa, mutta
    väärä paikka päättää siitä olisi tämä.
    """
    return pl.from_pandas(frame[list(CLOUD_TICK_PROPS)]).select(
        pl.col(_X).cast(pl.Float64).alias("x"),
        pl.col(_Y).cast(pl.Float64).alias("y"),
        pl.col(_Z).cast(pl.Float64).alias("z"),
        pl.when(pl.col(_PLACE_NAME).cast(pl.Utf8).str.len_chars() > 0)
        .then(pl.col(_PLACE_NAME).cast(pl.Utf8))
        .otherwise(None)
        .alias("area"),
        (pl.col(_LIFE_STATE).cast(pl.Int64) == _ALIVE).alias("is_alive"),
    )


def _trajectory_frame(raw: pl.DataFrame) -> pl.DataFrame:
    """Lentorata domainin sarakenimillä ja tyypeillä."""
    return raw.select(
        pl.col("grenade_entity_id").cast(pl.Int32),
        pl.col("grenade_type").cast(pl.Utf8),
        _thrower_id().alias("thrower_id"),
        pl.col("tick").cast(pl.Int32),
        pl.col("x").cast(pl.Float32),
        pl.col("y").cast(pl.Float32),
        pl.col("z").cast(pl.Float32),
    )


def _unknown_type_count(endpoints: pl.DataFrame) -> int:
    """Kranaatit, joiden luokkanimeä ei tunneta.

    Tuntematon nimi säilyy taulussa sellaisenaan -- se on luettava havainto --
    mutta demoparser2:n uudelleennimeäminen vuotaisi muuten tauluun ilman
    varoitusta, ja raportti näyttäisi utilityä, jonka tyyppi on pelin
    C++-luokan nimi.
    """
    return int(
        endpoints.filter(
            (pl.col("event_kind") == THROWN)
            & ~pl.col("grenade_type").is_in(list(GRENADE_TYPES))
        ).height
    )


def _name_fire_grenades(
    endpoints: pl.DataFrame, raw: pl.DataFrame, tolerance: int
) -> tuple[pl.DataFrame, int]:
    """Käännä luokkanimet kanonisiksi ja erota molotov incendiarystä.

    Lennossa molemmat ovat ``CMolotovProjectile``, joten erottelu on haettava
    heittäjän repusta heittoa edeltävältä hetkeltä: siellä kranaatti on yhä
    ``CMolotovGrenade`` tai ``CIncendiaryGrenade``. Haku on ``join_asof`` eikä
    tarkka tick: lentoradalle sallitaan pieni aukko, ja repulle on sallittava
    sama -- yksi hukkuva tick ei saa muuttaa incendiarya molotoviksi.

    Molemmat tulikranaatit repussa (poimittu vastustajan pudottama) jättää
    tyypin ratkaisematta; arvaus antaisi puolet ajasta väärän vastauksen ja
    näyttäisi silti havainnolta.

    Returns:
        ``(taulu, ratkeamattomat)``. Jälkimmäinen kattaa sekä osumattomat että
        epäselvät. Ilman lukua reppuhaun **täydellinen** epäonnistuminen --
        luokkanimen muutos, liian tiukka toleranssi -- näyttäisi täsmälleen
        samalta kuin demo, jossa heitettiin pelkkiä molotoveja.
    """
    canonical = endpoints.with_columns(
        pl.col("grenade_type").replace(GRENADE_TYPES)
    )
    fire_throws = (
        endpoints.filter(
            (pl.col("event_kind") == THROWN)
            & (pl.col("grenade_type") == MOLOTOV_PROJECTILE)
        )
        .select("grenade_no", "thrower_id", pl.col("tick").alias("throw_tick"))
        .sort("throw_tick")
    )
    if fire_throws.is_empty():
        return canonical, 0

    in_inventory = raw.filter(~flight_point()).select(
        _thrower_id().alias("thrower_id"),
        pl.col("tick").cast(pl.Int32),
        pl.col("grenade_type").cast(pl.Utf8),
    )

    # Yksi asof-liitos per tyyppi: se kertoo, kumpia tulikranaatteja heittäjällä
    # oli repussa juuri ennen heittoa. Kaksi osumaa on epäselvä tapaus, yksi
    # ratkaisee tyypin, nolla jättää sen auki.
    names = list(FIRE_ITEM_TYPES.values())
    matches = fire_throws.select("grenade_no")
    for class_name, name in FIRE_ITEM_TYPES.items():
        own = (
            in_inventory.filter(pl.col("grenade_type") == class_name)
            .select("thrower_id", "tick")
            .unique()
            .sort("tick")
        )
        if own.is_empty():
            matches = matches.with_columns(pl.lit(False).alias(name))
            continue
        with warnings.catch_warnings():
            # Polars ei voi tarkistaa lajittelua, kun ryhmittely on annettu, ja
            # varoittaa siitä joka kutsulla. Molemmat kehykset on lajiteltu
            # tickin mukaan tässä funktiossa, joten varoitus olisi pelkkää
            # kohinaa käyttäjän ruudulla kesken parsinnan.
            warnings.simplefilter("ignore", UserWarning)
            joined = fire_throws.join_asof(
                own,
                left_on="throw_tick",
                right_on="tick",
                by="thrower_id",
                strategy="backward",
                tolerance=tolerance,
            ).select("grenade_no", pl.col("tick").is_not_null().alias(name))
        matches = matches.join(joined, on="grenade_no", how="left")

    resolved = matches.with_columns(
        pl.sum_horizontal(
            [pl.col(name).fill_null(False).cast(pl.Int8) for name in names]
        ).alias("_osumia")
    )
    unresolved = int(resolved.filter(pl.col("_osumia") != 1).height)

    unambiguous = resolved.filter(pl.col("_osumia") == 1).select(
        "grenade_no",
        pl.coalesce(
            [
                pl.when(pl.col(name).fill_null(False)).then(
                    pl.lit(name, dtype=pl.Utf8)
                )
                for name in names
            ]
        ).alias("fire_type"),
    )
    if unambiguous.is_empty():
        return canonical, unresolved

    renamed = (
        canonical.join(unambiguous, on="grenade_no", how="left")
        .with_columns(
            pl.when(pl.col("fire_type").is_not_null())
            .then(pl.col("fire_type"))
            .otherwise(pl.col("grenade_type"))
            .alias("grenade_type")
        )
        .drop("fire_type")
        .sort("grenade_no", "tick")
    )
    return renamed, unresolved


def _shared_entity_id_count(frame: pl.DataFrame) -> int:
    """Lentoradat, jotka jakavat pelin tunnisteen toisen radan kanssa.

    Tämä luku oli aikanaan hälytys: ``(round_no, grenade_entity_id)`` oli
    luvattu parin avaimeksi, ja nollasta poikkeava arvo tarkoitti, ettei avain
    yksilöi paria. Liigademot nostivat luvun nollasta ylös
    (``inferno_vs_ryhmarama``: tunniste 564 kierroksella 11 kantaa kolme
    rataa), ja vastaus oli vaihtaa avain: taulussa on nyt ``grenade_no``, joka
    on yksikäsitteinen koko demossa. Luku jää paikalleen **havaintona**, ja se
    on ainoa mittari, joka varoittaisi jos joku palaisi käyttämään
    entiteettitunnistetta avaimena.

    Laskettava yksikkö on **lentorata eikä pari**: kolme rataa yhdellä
    tunnisteella on 3, ei 2. Aiempi versio ryhmitteli
    ``(round_raw, grenade_entity_id, event_kind)`` ja laski ryhmiä, jolloin
    sama tilanne antoi luvun 2 -- kaksi ryhmää, heitot ja räjähdykset -- eli
    luku ei kertonut ratojen eikä parien määrää vaan tapahtumalajien määrän.
    Nyt lasketaan eri ``grenade_no``-arvot per tunniste.

    Kierros on demon oma ``round_raw``, ei ``round_no``: adapterin taulussa
    ``round_no`` on aina tyhjä, koska numeroinnin omistaa ``stages.parse``.
    Luku sisältää siis myös lämmittelyn ja puukkokierroksen.
    """
    if frame.is_empty():
        return 0
    per_id = (
        frame.group_by("round_raw", "grenade_entity_id")
        .agg(pl.col("grenade_no").n_unique().alias("trajectories"))
        .filter(pl.col("trajectories") > 1)
    )
    return int(per_id["trajectories"].sum())


def _round_windows(segments: list[_Segment]) -> list[tuple[int, int, int]]:
    """Kierrosten ``[ankkuri, loppu]``-ikkunat aikajärjestyksessä.

    Raises:
        ParseError: Jos ikkunat menevät päällekkäin. Silloin
            :func:`_round_of_tick`in binäärihaku voisi kohdistaa kranaatin
            väärälle kierrokselle -- ja kierroksen jokainen utility-havainto
            olisi väärän joukkueen suunnitelmaa.
    """
    windows = sorted(
        (s.freeze_end_tick, s.end_tick, index)
        for index, s in enumerate(segments)
        if s.freeze_end_tick is not None and s.end_tick is not None
    )
    for first, second in zip(windows, windows[1:]):
        if second[0] <= first[1]:
            raise ParseError(
                "Demon kierrosrajat menevät päällekkäin: kierros alkaa tickistä "
                f"{second[0]} vaikka edellinen päättyy vasta tickissä {first[1]}.\n"
                "Kranaattia ei voi silloin kohdistaa yksikäsitteisesti "
                "kierrokselle. Demo on todennäköisesti vioittunut."
            )
    return windows


def _round_of_tick(
    starts: list[int], windows: list[tuple[int, int, int]], tick: int
) -> int | None:
    """Kierros, jonka rajojen sisään tick osuu, tai ``None``.

    Ikkunat eivät mene päällekkäin (:func:`_round_windows` varmistaa sen),
    joten viimeinen ankkuri ennen tickiä on ainoa ehdokas. ``None`` tarkoittaa
    lämmittelyä ennen ensimmäistä ankkuria tai heittoa kierroksen ratkeamisen
    ja seuraavan ostoajan välissä; kummallakaan ``t_s`` ei ole määritelty.
    """
    position = bisect_right(starts, tick) - 1
    if position < 0:
        return None
    _, end, index = windows[position]
    return index if tick <= end else None


def _keys_by_side(
    sides: tuple[str, str], lineup_keys: list[str], segment: _Segment
) -> dict[str, str]:
    """Puoli -> kokoonpanotunniste yhdellä kierroksella.

    Sanakirja eikä ``sides.index(side)``: jos puolikuvaus olisi jostain syystä
    ``("T", "T")``, ``.index`` palauttaisi molemmille nollan ja **molemmat
    joukkueet saisivat saman lineup_keyn**. Taulu näyttäisi kelvolliselta,
    mutta jokainen joukkuekohtainen luku olisi molempien summa -- täsmälleen se
    ristiinkytkentä, jonka :meth:`Demoparser2Adapter._lineup_keys` estää
    kierrostaulussa.
    """
    if sides[0] == sides[1]:
        raise ParseError(
            f"Kierroksella (round_raw={segment.round_raw}, "
            f"freeze_end_tick={segment.freeze_end_tick}) molemmille "
            f"kokoonpanoille tuli sama puoli {sides[0]!r}.\n"
            "Puolet eivät erotu, joten näytepisteiden rivit kohdistuisivat "
            "samalle joukkueelle. Demo on todennäköisesti vioittunut."
        )
    return {sides[0]: lineup_keys[0], sides[1]: lineup_keys[1]}


def _lineup_index_by_player(lineups: list[_Lineup]) -> dict[str, int]:
    """Pelaaja -> kokoonpanon indeksi.

    Pelaaja, joka on ehtinyt näkyä molemmissa kokoonpanoissa, jätetään pois:
    hänen puoltaan ei voi päätellä, ja arvaus kohdistaisi kontaktin väärin
    päin. Sellaista ei normaalissa demossa esiinny.
    """
    result: dict[str, int] = {}
    in_both = lineups[0].members & lineups[1].members
    for index, lineup in enumerate(lineups):
        for steamid in lineup.members - in_both:
            result[steamid] = index
    return result


def _side_lookup(
    lineup_of: dict[str, int],
    sides: tuple[str, str],
    segment: _Segment,
    by_tick: dict[int, list[dict[str, Any]]],
) -> dict[str, str]:
    """Pelaaja -> puoli **tällä kierroksella**.

    Ensisijainen lähde on kokoonpano: puoli tulee kierroksen omasta
    kuvauksesta, ei pelaajasta, koska joukkueet vaihtavat puolta puoliajalla ja
    jatkoajassa.

    Varalähteenä on kierroksen oman tickin ``m_iTeamNum``. Sitä tarvitaan
    pelaajalle, joka ei ole kummassakaan kokoonpanossa -- kesken karttaa tullut
    tai uudelleenyhdistänyt pelaaja. Ilman varalähdetta hanen vahinkonsa
    hylättäisiin äänettömästi ja kierros voisi menettaa ensikontaktinsa.
    """
    player_sides = {steamid: sides[index] for steamid, index in lineup_of.items()}
    for tick in (segment.freeze_end_tick, segment.end_tick):
        for row in by_tick.get(tick or -1) or ():
            player_sides.setdefault(row["steamid"], row["side"])
    return player_sides


def _resolve_side(
    player_id: str | None,
    team_num: int | None,
    player_sides: dict[str, str],
) -> str | None:
    """Pelaajan puoli: ensin kierroksen kuvaus, sitten tapahtuman oma lukema.

    Ensisijainen lähde on :func:`_side_lookup`in kartta, joka on **saman
    kierroksen** puolikuvaus -- se, jonka mukaan näytepiste- ja
    tapahtumataulun rivit on kirjattu. Yhdenmukaisuus on tässä tärkeämpää kuin
    tuoreus: tapahtumasta luettu poikkeava puoli panisi kuoleman eri
    joukkueelle kuin mitä muut taulut sanovat samasta pelaajasta samalla
    kierroksella.

    Varalähde on tapahtuman oma ``team_num``. Se kattaa pelaajan, jota ei ole
    kummassakaan kokoonpanossa eikä kierroksen ankkuritickillä -- kesken
    karttaa tullut tai uudelleenyhdistänyt. Ilman sitä hänen kuolemansa
    putoaisi taulusta.

    **Miksi kuolemataulussa on kolmas taso ja muissa kaksi.** Ketju on
    lineup -> kierroksen ankkuritick -> tapahtuman oma ``team_num``. Kaksi
    ensimmäistä ovat :func:`_side_lookup`issa ja jaetut utilityn kanssa;
    kolmas on vain täällä, ja syy on kenttien saatavuus eikä eri sääntö:
    ``player_death`` **kantaa puolen mukanaan**, kranaatin lentorata ei. Siksi
    puoleton kranaatti päätyy lukuun ``grenades_unknown_side`` ja puoleton
    kuolema ei -- kummallakin luetaan kaikki mitä lähteessä on.

    **Se ei ole väite rosterista.** ``victim_lineup_key`` kertoo, *minkä
    joukkueen puoli* menetti pelaajan sillä kierroksella; kokoonpanon
    jäsenluettelo on ``lineups``-taulussa, joka lasketaan ankkuritickeistä
    eikä tästä. Kesken karttaa liittynyt pelaaja pelaa silti sen joukkueen
    puolella, ja hänen kuolemansa kuuluu sille -- vaikka rosteritiiviste ei
    häntä tunne. Näytepistetaulussa sama pelaaja vääristäisi *pelaajamäärän*
    alueella, mikä on eri väite; siksi siellä ei ole vastaavaa polkua.

    Returns:
        ``"T"``, ``"CT"`` tai ``None``. ``None`` tarkoittaa, ettei kumpikaan
        lähde tiennyt: pelaaja on katsoja, liittymätön tai tuntematon.
    """
    if player_id is None:
        return None
    side = player_sides.get(player_id)
    if side is not None:
        return side
    return TEAM_SIDES.get(team_num if team_num is not None else -1)


def _with_sides(
    events: list[tuple[int, str | None, str | None, str | None]],
    bounds: RoundBounds,
    player_sides: dict[str, str],
) -> tuple[list[DamageEvent], int]:
    """Rajaa tapahtumat kierrokseen ja liitä niihin pelaajien puolet.

    Returns:
        ``(tapahtumat, montako jäi ilman puolta)``. Jälkimmäinen luku päätyy
        diagnostiikkaan: äänettömästi hylätty vahinko voisi viedä kierrokselta
        ensikontaktin, eikä mikään kertoisi siitä.
    """
    if bounds.freeze_end_tick is None or bounds.end_tick is None:
        return [], 0
    start, end = bounds.freeze_end_tick, bounds.end_tick

    result: list[DamageEvent] = []
    unknown_sides = 0
    for tick, attacker, victim, weapon in events:
        if not start <= tick <= end:
            continue
        attacker_side = player_sides.get(attacker) if attacker else None
        victim_side = player_sides.get(victim) if victim else None
        # Maailman aiheuttama vahinko (attacker None) on tunnettu tapaus eikä
        # puuttuva havainto, joten sitä ei lasketa tuntemattomaksi.
        if (attacker and attacker_side is None) or (victim and victim_side is None):
            unknown_sides += 1
        result.append(
            DamageEvent(
                tick=tick,
                attacker_id=attacker,
                victim_id=victim,
                weapon=weapon,
                attacker_side=attacker_side,
                victim_side=victim_side,
            )
        )
    return result, unknown_sides


def _sorted_points(points: list[SamplePoint]) -> list[SamplePoint]:
    """Näytepisteet vakaassa järjestyksessä.

    ``sample_kind`` on avaimessa, koska ensikontakti voi osua tasan
    konfiguroidulle sekunnille. Ilman sitä kahden rivin järjestys riippuisi
    syötejärjestyksestä, ja sama demo tuottaisi eri tavut eri ajoilla.
    """
    return sorted(points, key=lambda p: (p.round_raw, p.sample_t_s, p.sample_kind))


def _require_previous(
    previous: tuple[str, str] | None, segment: _Segment, reason: str
) -> tuple[str, str]:
    """Palauta edellisen kierroksen puolikuvaus tai keskeytä.

    Oletus ``("T", "CT")`` olisi arvaus, joka näyttäisi toimivan mutta
    kohdistaisi kierroksen havainnot väärälle joukkueelle.
    """
    if previous is not None:
        return previous
    raise ParseError(
        f"Kierroksen (freeze_end_tick={segment.freeze_end_tick}, "
        f"round_end_tick={segment.end_tick}) puolia ei voitu määrittää: {reason}, "
        "eikä edellistä kierrosta ole, josta kuvauksen voisi periä.\n"
        "Puolen arvaaminen kohdistaisi kierroksen havainnot väärälle "
        "joukkueelle, joten parsinta keskeytetään. Demo on todennäköisesti "
        "vioittunut."
    )


# -- Pieniä muuntimia ---------------------------------------------------------


def _is_wholly_pawnless(counts: "_SampleTickCounts | None") -> bool:
    """Selittikö pawnittomuus sen, ettei tickiltä jäänyt yhtään riviä.

    ``True`` vain kun rivejä oli ja **jokainen** niistä oli pawniton. Tyhjä
    tulos ei kelpaa: silloin demo ei palauttanut mitään, ja se on vika.
    """
    return bool(counts and counts.without_pawn and counts.without_pawn == counts.seen)


def _pawnless_rows(*by_call: dict[int, _SampleTickCounts]) -> int:
    """Pawnittomat rivit yhteensä, sama tick laskettuna kerran.

    Näytepisteiden ja heittojen tickit luetaan omilla kutsuillaan, ja ne
    voivat osua **samaan tickiin**: kierroksen alussa heitetty savu lähtee
    samalta tickiltä kuin 6 sekunnin näytepiste. Suora summa laskisi silloin
    yhden fyysisen rivin kahdesti, eikä luku olisi enää "rivejä" vaan
    "rivilukemia". Tickikohtaiset laskurit yhdistetään siksi unionina; sama
    tick antaa molemmilla kutsuilla saman luvun, joten maksimi on oikea
    valinta eikä varmuuden vuoksi otettu.
    """
    merged: dict[int, int] = {}
    for counts in by_call:
        for tick, count in counts.items():
            merged[tick] = max(merged.get(tick, 0), count.without_pawn)
    return sum(merged.values())


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:  # pragma: no cover - vertailukelvoton tyyppi
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _most_observed(counts: "Counter[str] | None") -> str | None:
    """Useimmin havaittu arvo, tasatilanne aakkosjärjestyksessä.

    Aakkosjärjestys ei ole makuasia vaan toistettavuus: ``Counter.most_common``
    palauttaa tasatilanteessa lisäysjärjestyksen, joka riippuu siitä missä
    järjestyksessä demoparser2 sattui palauttamaan rivit.

    Returns:
        Arvo, tai ``None`` jos havaintoja ei ole. ``None`` on rehellinen
        tulos: nimen puuttuminen on havainto eikä syy keksiä korviketta.
    """
    if not counts:
        return None
    return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:  # pragma: no cover
        return None
    text = str(value).strip()
    return text or None


def _as_inventory(value: Any) -> tuple[str, ...] | None:
    """Tavaraluettelo yhdeltä tickiltä.

    Returns:
        Nimet järjestyksessä, tai ``None`` jos propia ei saatu luettua. Tyhjä
        monikko ja ``None`` ovat **eri asioita**: edellinen sanoo "luettiin,
        eikä mitään ollut", jälkimmäinen "ei luettu". Vain jälkimmäinen saa
        jättää kalustolaskurin tyhjäksi.
    """
    if value is None:
        return None
    if isinstance(value, float):  # pandas nostaa puuttuvan arvon NaN:ksi
        return None
    if isinstance(value, str):  # yksittäinen nimi ilman listaa
        text = _as_str(value)
        return () if text is None else (text,)
    try:
        items = list(value)
    except TypeError:
        return None
    names = [_as_str(item) for item in items]
    return tuple(name for name in names if name is not None)


def _as_side(value: Any) -> str | None:
    text = _as_str(value)
    if text is None:
        return None
    text = text.upper()
    return text if text in ("T", "CT") else None


# -- Ostoaika -----------------------------------------------------------------


def _buy_end_ticks(
    segments: list[_Segment],
    death_ticks: list[int],
    tick_rate: float,
    window_seconds: float,
) -> tuple[list[int | None], list[int | None]]:
    """Valitse jokaiselle kierrokselle tick, jolta talousarvot luetaan.

    Mittauspiste on::

        max(freeze_end_tick,
            min(freeze_end_tick + window_seconds * tick_rate,
                kierroksen ensimmäistä kuolemaa EDELTÄVÄ tick,
                kierroksen yläraja))

    Uloin ``max`` ei ole koriste: ilman sitä kuolema tasan ankkuria seuraavalla
    tickillä työntäisi mittauspisteen ankkuria aiemmaksi eli freezetimen
    sisään.

    Kierroksen yläraja on ``end_tick``. Jos kierros ei ratkennut (demo katkesi
    kesken), ylärajaksi otetaan **seuraavan kierrosrajan ankkuria edeltävä
    tick**: ilman sitä ikkuna valuisi seuraavan kierroksen puolelle ja lukisi
    sen talousarvot tämän kierroksen riville.

    **Kuolemaa edeltävä tick, ei kuoleman tick.** Kuolintickillä uhrin
    ``inventory`` on jo tyhjä ja ``m_ArmorValue`` 0 (mitattu:
    ``inferno_vs_ryhmarama`` kierros 6, tick 42236). Tasan kuolintickiltä
    luettuna joukkueesta katoaisi yhden pelaajan koko kalusto -- eri vika kuin
    liian aikainen mittaus, mutta yhtä hiljainen. Haku on siksi
    :func:`~bisect.bisect_left`, joka ottaa mukaan myös **tasan ankkurilla**
    olevan kuoleman; ``bisect_right`` ohittaisi sen ja lukisi ruumiin.

    **Yksi tick koko kierrokselle.** Kun mittaushetkellä kukaan ei ole vielä
    kuollut, kukaan ei ole myöskään ehtinyt pudottaa asetta kuollessaan, joten
    kaksoislaskennan lähde (joukkuekaveri poimii vainajan kiväärin) on
    rakenteellisesti poissuljettu. Pelaajakohtaista "viimeinen elossa"
    -pistettä ei siis tarvita: mittaushetkellä joukkue on koskematon.

    **Katkaisu on normaali polku, ei reunatapaus.** Mitattuna kuudesta demosta
    (134 pelattua kierrosta) kuolema katkaisee ikkunan 69 kierroksella eli
    **51 %:lla**. Kierroksen ensimmäinen kuolema osuu aikaisintaan 9,80 s
    kohdalle ja mediaanina 19,7 s kohdalle; 8 sekunnin sisään ei kuolla
    yhdelläkään kierroksella. Efektiivinen mittaushetki on siis usein
    10-20 s eikä 20 s.

    Päällekkäisyys ostamisen kanssa on kapea mutta todellinen: niistä
    kierroksista, joilla ostettiin vielä freezetimen jälkeen, ostaminen oli
    valmis 8 s mennessä 92 %:ssa, ja aikaisin kuolema on 9,8 s. Samassa
    aineistossa yksikään kuolema ei edellä viimeistä ostoa, mutta neljä
    kierrosta ostaa vielä 11,0 / 11,3 / 13,5 / 19,4 s kohdalla. Siksi
    katkaisun hinta **mitataan joka ajolla** (:func:`_purchases_between`) eikä
    oleteta nollaksi.

    Args:
        segments: Kierrosrajat.
        death_ticks: Kaikkien ``player_death``-tapahtumien tickit nousevassa
            järjestyksessä.
        tick_rate: Käytetty tickrate. Voi olla mittaamaton oletus, jolloin myös
            ikkunan pituus tickeinä on oletus -- ``stages.parse`` kertoo sen
            käyttäjälle, tämä funktio ei voi tietää eroa.
        window_seconds: ``[parse].buy_window_seconds``.

    Returns:
        ``(mittauspisteet, katkaisemattomat ikkunan loput)``, molemmat
        segmenttien järjestyksessä.

        Mittauspiste on ``None``, jos kierroksella ei ole ankkuria tai jos se
        ei ole kierros lainkaan (ottelun uudelleenaloitus).

        Jälkimmäinen lista on ``None`` kaikkialla muualla paitsi niillä
        kierroksilla, joilla kuolema katkaisi ikkunan: siellä se on se tick,
        jolta olisi mitattu ilman katkaisua. Sitä ei käytetä mittaukseen vaan
        vain sen laskemiseen, jäikö ostoja katkaisun taakse.
    """
    # Ikkunan pituus tickeinä. Nimi ei ole ``window_ticks``, koska kutsujalla
    # se tarkoittaa listaa tickejä; sama nimi kahdelle eri asialle on juuri se
    # sekaannus, jota tämä moduuli muuten välttää.
    window_length = max(0, round(window_seconds * tick_rate))
    measured: list[int | None] = []
    uncut: list[int | None] = []

    for index, segment in enumerate(segments):
        anchor = segment.freeze_end_tick
        if anchor is None or segment.round_raw is None:
            measured.append(None)
            uncut.append(None)
            continue

        limit = anchor + window_length
        bound = _round_upper_bound(segments, index, anchor)
        if bound is not None:
            limit = min(limit, bound)
        limit = max(limit, anchor)

        # Ensimmäinen kuolema ankkurilla tai sen jälkeen. bisect, koska tickit
        # ovat järjestyksessä ja niitä on demossa satoja.
        position = bisect_left(death_ticks, anchor)
        first_death = death_ticks[position] if position < len(death_ticks) else None

        cut = None if first_death is None else max(anchor, first_death - 1)
        if cut is not None and cut < limit:
            measured.append(cut)
            uncut.append(limit)
        else:
            # Kuolema ikkunan jälkeen, tai ikkuna on jo nollan mittainen:
            # ikkuna ei lyhentynyt, joten katkaisua ei myöskään raportoida.
            # Nolla-arvon kirjaaminen katkaisuksi tekisi laskurista kohinaa.
            measured.append(limit)
            uncut.append(None)
    return measured, uncut


def _round_upper_bound(
    segments: list[_Segment], index: int, anchor: int
) -> int | None:
    """Viimeinen tick, joka vielä kuuluu kierrokselle ``index``.

    Ratkennut kierros päättyy omaan ``end_tick``iinsä. Ratkeamattomalla (demo
    katkesi kesken) sitä ei ole, ja silloin raja otetaan **seuraavasta
    kierrosrajasta**: ostoikkuna ei saa yltää seuraavan kierroksen ankkuriin,
    koska siellä luetut talousarvot olisivat jo seuraavan kierroksen.

    Ottelun uudelleenaloitus kelpaa rajaksi siinä missä kierroskin: se ei ole
    kierros, mutta se on hetki, jonka jälkeen tämän kierroksen arvot eivät enää
    ole voimassa.

    Returns:
        Yläraja, tai ``None`` jos kierros on demon viimeinen eikä sillä ole
        päättymistä -- silloin rajaa ei ole olemassa eikä sitä keksitä.
    """
    if segments[index].end_tick is not None:
        return segments[index].end_tick
    for later in segments[index + 1 :]:
        if later.freeze_end_tick is not None and later.freeze_end_tick > anchor:
            return later.freeze_end_tick - 1
    return None


def _purchases_between(
    at_measurement: list[dict[str, Any]],
    at_window_end: list[dict[str, Any]],
) -> tuple[int, int]:
    """Menetetyt ostot ikkunan katkaisun takana -- ja montako voitiin tarkistaa.

    ``m_iCashSpentThisRound`` kasvaa **vain ostoista** eikä reagoi kuolemiin
    tai pudotettuihin aseisiin, ja se on pelaajan controllerissa eikä
    pawnissa, joten se säilyy myös kuoleman yli. Se on siksi ainoa turvallinen
    mittari sille, maksoiko ikkunan katkaisu jotain.

    Menetettyjen ostojen **kuuluu olla nolla**. Nollasta poikkeava arvo
    tarkoittaa, että joku osti sen jälkeen kun ikkuna katkaistiin, eli mittaus
    menetti ostoksen -- ja se on sanottava ajon tulosteessa ääneen eikä
    vaiettava.

    Vertailtujen määrä palautuu mukana, koska **nollalla on kaksi eri syytä**:
    mitään ei menetetty, tai vertailua ei voitu tehdä lainkaan (ikkunan lopun
    tickiltä ei saatu rivejä). Ilman erottelua tarinan tärkein luku voisi lukea
    tyhjää nollaa ilman että mikään kertoisi siitä.

    Args:
        at_measurement: Pelaajarivit mittauspisteen tickiltä.
        at_window_end: Pelaajarivit siltä tickiltä, jolle ikkuna olisi
            yltänyt ilman katkaisua.

    Returns:
        ``(menetettyjä ostoja, vertailtuja pelaajia)``. Ensimmäinen on niiden
        pelaajien määrä, joiden ``cash_spent`` on jälkimmäisellä tickillä
        suurempi kuin ensimmäisellä. Pelaaja, joka puuttuu jommaltakummalta
        tickiltä tai jolta luku ei ole luettavissa, ei kelpaa havainnoksi eikä
        kasvata kumpaakaan lukua.
    """
    before = {
        r["steamid"]: r["cash_spent"]
        for r in at_measurement
        if r["cash_spent"] is not None
    }
    missed = 0
    compared = 0
    for row in at_window_end:
        spent = row["cash_spent"]
        earlier = before.get(row["steamid"])
        if spent is None or earlier is None:
            continue
        compared += 1
        if spent > earlier:
            missed += 1
    return missed, compared


def _refunds_and_stale_equipment(
    at_anchor: list[dict[str, Any]],
    at_measurement: list[dict[str, Any]],
) -> tuple[int, int]:
    """Palautetut ostokset ikkunan aikana -- ja niiden jättämä vanhentunut arvo.

    CS2:ssa juuri ostetun tavaran voi palauttaa muutaman sekunnin ajan. Raha ja
    panssari palautuvat oikein, mutta **varustearvo ei aina laske mukana**:
    mitattuna ``Anubis_vs_ryhmarama`` kierros 3 CT, jossa pelaaja osti kevlarin
    ja kypärän 0,4 s kohdalla ja palautti ne 1,9 s kohdalla -- ``m_iAccount``
    ja ``m_ArmorValue`` palasivat lähtöarvoihinsa (450 -> 1 450 ja 100 -> 0),
    mutta ``m_unCurrentEquipmentValue`` jäi 1 200:aan eikä palannut 200:aan.

    Kaksi lukua, koska ne ovat eri havaintoja:

    ``palautuksia``
        ``cash_spent`` **pieneni** ankkurin ja mittauspisteen välillä. Prop
        kasvaa vain ostoista, joten lasku voi tarkoittaa vain palautusta --
        yksikäsitteinen merkki, joka ei sekoitu kuolemaan. Mitattu: 8
        pelaajariviä 7 kierroksella kuudesta demosta, ja näissä varustearvo
        seurasi palautusta oikein.
    ``vanhentunutta arvoa``
        Varustearvo **nousi**, vaikka pelaaja ei ostanut (``cash_spent``
        ennallaan), ei saanut panssaria (``m_ArmorValue`` ennallaan) eikä hänen
        tavaraluettelonsa muuttunut. Mitään ei tullut, joten arvon on oltava
        vanhentunut. Tämä on se jälki, jonka **kokonaan kahden luetun tickin
        välissä** tapahtunut palautus jättää: molemmilla tickeillä
        ``cash_spent`` on sama, eikä palautus näy mitenkään muuten. Mitattu:
        1 pelaajarivi 134 kierroksesta, vaikutus 1 000 $ eli joukkuetasolla
        200 $/pelaaja.

    **Ei tunnisteta jäljestä "panssari katosi eikä arvo laskenut."** Kuolema
    tuottaa täsmälleen saman jäljen ja on kymmenkertaisesti yleisempi, joten
    sellainen laskuri mittaisi kuolemia eikä palautuksia. Molemmat ehdot yllä
    vaativat päinvastoin, ettei panssari muuttunut.

    **Ei koske aseistettujen laskuria.** Se lukee tavaraluettelon ja
    ``m_ArmorValue``n, jotka molemmat palautuvat oikein; vanhentuminen koskee
    vain varustearvoa.

    Returns:
        ``(palautuksia, vanhentunutta arvoa)`` pelaajariveinä.
    """
    anchor_by_id = {r["steamid"]: r for r in at_anchor}
    refunds = 0
    stale = 0
    for row in at_measurement:
        earlier = anchor_by_id.get(row["steamid"])
        if earlier is None:
            continue
        spent_before, spent_now = earlier["cash_spent"], row["cash_spent"]
        if spent_before is None or spent_now is None:
            continue
        if spent_now < spent_before:
            refunds += 1
            continue
        equip_before, equip_now = earlier["equip_current"], row["equip_current"]
        armor_before, armor_now = earlier["armor_value"], row["armor_value"]
        if None in (equip_before, equip_now, armor_before, armor_now):
            continue
        if (
            equip_now > equip_before
            and spent_now == spent_before
            and armor_now == armor_before
            and (earlier.get("inventory") or ()) == (row.get("inventory") or ())
        ):
            stale += 1
    return refunds, stale


#: Propit, joiden on oltava luettavissa, jotta pelaaja lasketaan mukaan
#: ostoajan lopun summiin ja niiden jakajaan.
_BUY_END_PROPS: tuple[str, ...] = (
    "account",
    "cash_spent",
    "equip_current",
    "equip_round_start",
)


def _readable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pelaajat, joiden ostoajan lopun arvot ovat kaikki luettavissa.

    Sekä summa että sen jakaja lasketaan **tästä samasta joukosta**. Jos
    summattaisiin vain luettavat mutta jaettaisiin kaikilla riveillä, kolmen
    pelaajan varustearvo jaettuna viidellä aliarvioisi tuloksen 40 prosenttia
    ja työntäisi kierroksen ecoksi -- hiljaa ja uskottavan näköisesti.
    """
    return [
        r for r in rows if all(r.get(name) is not None for name in _BUY_END_PROPS)
    ]


#: Panssarilukeman propin nimi. Vakiona, koska sitä lukee kolme paikkaa --
#: kummankin laskurin luettavuusehto ja jaettu :func:`_has_armor` -- ja
#: kovakoodattuna nimi erkanisi niistä huomaamatta.
_ARMOR_PROP = "armor_value"

#: Propit, joiden on oltava luettavissa, jotta kalustolaskurin voi laskea.
#: Nämä **eivät** ole :data:`_BUY_END_PROPS`issa: pelaaja pysyy summissa ja
#: niiden jakajassa, vaikka nämä puuttuisivat, koska jakajan on oltava sama
#: joukko kaikille rivin luvuille.
_ARMED_PROPS: tuple[str, ...] = (_ARMOR_PROP, "inventory")

#: Propit, joiden on oltava luettavissa, jotta panssarilaskurin voi laskea.
#: **Aito osajoukko** :data:`_ARMED_PROPS`ista, ja se on koko ero kirjoitettuna
#: koodiin eikä kommenttiin: panssarilaskuri ei lue tavaraluetteloa, joten
#: lukukelvoton tavaraluettelo tyhjentää vain aseistettujen laskurin.
_ARMORED_PROPS: tuple[str, ...] = (_ARMOR_PROP,)


def _has_armor(row: dict[str, Any]) -> bool:
    """Onko pelaajalla panssaria ostoajan lopussa.

    **Molempien laskureiden yhteinen ehto**, ja siksi yhdessä paikassa. Sekä
    :func:`_is_armed` että :func:`_armored_count` lukevat saman
    ``m_ArmorValue``-lukeman samalta tickiltä; jos ehto olisi kirjoitettu
    kahdesti, kynnyksen, kypärän erottelun tai vaurioituneen panssarin rajaus
    muuttaisi vain toista laskuria -- ja juuri sen hiljaisen erkaantumisen
    estäminen on koko kahden sarakkeen perustelu.

    Kutsuja vastaa siitä, että arvo on luettavissa; tässä ``None``
    tulkittaisiin "ei panssaria", eli lukuvirhe näyttäisi säästöltä.

    Kypärää ei eroteta: ``m_bHasHelmet`` on oma havaintonsa, eikä analyysi
    puhu siitä. Vaurioitunutta panssaria ei myöskään eroteta ehjästä: 37/100
    on yhä panssari, ja pelaaja kantaa sitä.
    """
    return (row.get(_ARMOR_PROP) or 0) > 0


def _armed_readable(row: dict[str, Any]) -> bool:
    """Ovatko pelaajan panssari ja tavaraluettelo luettavissa.

    Tyhjä tavaraluettelo (``()``) on **havainto**: pelaajalla ei ollut mitään.
    Puuttuva (``None``) ei ole. Sama koskee panssaria: ``0`` on havainto,
    ``None`` ei.
    """
    return all(row.get(name) is not None for name in _ARMED_PROPS)


def _armored_readable(row: dict[str, Any]) -> bool:
    """Onko pelaajan panssari luettavissa.

    **Kapeampi ehto** kuin :func:`_armed_readable`: tavaraluettelo ei kuulu
    siihen, koska panssarilaskuri ei lue sitä. ``0`` on havainto (pelaajalla
    ei ollut panssaria), ``None`` ei.
    """
    return all(row.get(name) is not None for name in _ARMORED_PROPS)


def _is_armed(row: dict[str, Any]) -> bool:
    """Onko pelaajalla panssari ja vähintään yksi ase hallussa.

    Käyttäjän määritelmä on "kevlar **ja** jokin parannettu ase". Kevlar ilman
    asetta ei riitä eikä ase ilman kevlaria. ``armor_value > 0`` riittää;
    kypärää ei vaadita, koska CT ostaa usein pelkän kevlarin AK:n
    kertaosuman takia.

    Ratkaisee **hallussapito, ei ostos**: säästetty tai poimittu kivääri
    laskeutuu samoin kuin ostettu. Oletuspistoolit ovat silti ulkona, koska ne
    saa joka kierros ilmaiseksi.

    Rivi on ostoajan lopun tickiltä, joka on valittu ennen kierroksen
    ensimmäistä kuolemaa (ks. :func:`_buy_end_ticks`). Kuolleen pelaajan
    ``inventory`` on tyhjä ja panssari 0, joten kuolintickiltä luettuna tämä
    palauttaisi ``False`` riippumatta siitä, mitä pelaaja osti.

    Kutsuja on jo varmistanut :func:`_armed_readable`illa, että arvot ovat
    luettavissa -- tässä ``None`` tulkittaisiin "ei panssaria" ja "ei
    tavaroita", eli lukuvirhe näyttäisi säästöltä.

    Tuntematon nimi ei ole ase (ks. :data:`~pappascout.constants.ARMING_WEAPONS`).
    """
    if not _has_armor(row):
        return False
    return any(name in ARMING_WEAPONS for name in row.get("inventory") or ())


def _armed_count(own_buy: list[dict[str, Any]]) -> int | None:
    """Montako pelaajaa oli aseistettu ostoajan lopussa.

    Aseistettu = **panssari ja vähintään yksi ase hallussa**. Joukkuesumma ei
    kerro tätä: kaksi AK:ta ja kolme tyhjää antaa saman summan kuin viisi
    puolinaista, eikä varustearvo ylipäätään erota asetta panssarista ja
    kranaateista. Laskuri lasketaan **samasta joukosta** kuin summat ja
    ``players_buy_end`` (ks. :func:`_readable`), joten rivillä on vain yksi
    jakaja.

    Args:
        own_buy: :func:`_readable`-suodatettu joukkueen pelaajajoukko.

    Returns:
        Aseistettujen määrä, tai ``None`` jos lukua ei voi antaa.

        **Nolla ei ole puuttuva havainto**: se on tieto siitä, ettei kukaan
        ollut aseistettu -- täysi eco tuottaa nollan, ja se on aineistoa.

        ``None`` on kaksi eri asiaa, ja molemmat ovat "ei tiedetä":

        * joukko on tyhjä (kierros ilman freezetime-ankkuria), tai
        * **yhdenkin** pelaajan panssari tai tavaraluettelo on lukukelvoton.

        Jälkimmäinen tyhjentää koko rivin eikä vain pudota yhtä pelaajaa,
        koska pelaaja pysyy silti ``players_buy_end``in jakajassa: "3/5"
        väittäisi, että kaksi oli aseetonta, vaikka totuus on ettei heitä
        saatu luettua. Vaiettu lukuvirhe näyttäisi säästökierrokselta.
    """
    if not own_buy:
        return None
    if not all(_armed_readable(row) for row in own_buy):
        return None
    return sum(1 for row in own_buy if _is_armed(row))


def _armored_count(own_buy: list[dict[str, Any]]) -> int | None:
    """Montako pelaajaa kantoi panssaria ostoajan lopussa.

    **Eri luku kuin** :func:`_armed_count`, ei sen yleistys. Ehto on tässä
    pelkkä :func:`_has_armor`; aseesta ei välitetä. Tästä luetaan
    tavoiteanalyysin rivit *"5 kevlaria"* ja *"ei kevuja"*, joita
    aseistettujen laskurista ei saa: pistoolikierroksella se on käytännössä 0,
    koska 800 dollarilla ei osta sekä kevlaria että parannettua asetta.

    **Hallussapito, ei ostos.** Panssari säilyy kierroksen yli hengissä
    selvinneellä, joten muilla kierrostyypeillä luku kertoo mitä pelaajilla
    oli eikä mitä he ostivat. Pistoolikierroksella (1 ja 13) perintää ei ole
    -- puoliaika alkaa puhtaalta pöydältä -- joten siellä se on ostohavainto.

    Sama joukko, sama tick ja sama lukema kuin :func:`_armed_count`illa, joten
    aseistetut ovat aina panssaroitujen osajoukko eikä panssaroituja voi olla
    enempää kuin luettavissa olleita.

    Args:
        own_buy: :func:`_readable`-suodatettu joukkueen pelaajajoukko.

    Returns:
        Panssaroitujen määrä, tai ``None`` jos lukua ei voi antaa.

        **Nolla ei ole puuttuva havainto**: kierros, jolla kukaan ei kantanut
        panssaria, tuottaa nollan, ja juuri se on Ancientin CT-pistoolin
        *"ei kevuja"*.

        ``None`` on kaksi eri asiaa, ja molemmat ovat "ei tiedetä": joukko on
        tyhjä (ankkuriton kierros), tai **yhdenkin** pelaajan panssari on
        lukukelvoton. Jälkimmäinen tyhjentää koko rivin samasta syystä kuin
        aseistettujen laskurissa: pelaaja pysyy ``players_buy_end``in
        jakajassa, joten osittainen luku näyttäisi säästöltä eikä
        lukuvirheeltä.

        Luettavuusehto on **kapeampi** kuin aseistettujen laskurilla:
        tavaraluettelo ei kuulu siihen, koska tämä laskuri ei lue sitä.
        Lukukelvoton tavaraluettelo tyhjentää siis vain ylemmän laskurin.
    """
    if not own_buy:
        return None
    if not all(_armored_readable(row) for row in own_buy):
        return None
    return sum(1 for row in own_buy if _has_armor(row))


def _sum_or_none(values: list[int | None]) -> int | None:
    """Summaa arvot; ``None`` jos yhtään havaintoa ei ole."""
    valid = [v for v in values if v is not None]
    return sum(valid) if valid else None


def _sum_or_zero(values: list[int | None]) -> int:
    """Summaa arvot; tyhjä joukko on nolla (kukaan ei jäänyt henkiin)."""
    return sum(v for v in values if v is not None)


def _score_before(
    index: int,
    segments: list[_Segment],
    anchor_score: list[int | None],
    end_score: list[int | None],
) -> int | None:
    """Yhteispistemäärä juuri ennen kierrosta ``index``, kun ankkuri puuttuu.

    Varasääntöä kysytään vain kierrokselta, jolla ei ole omaa
    freezetime-ankkuria. Lähin aiempi lukema kelpaa, mutta **ottelun
    uudelleenaloituksen kohdalla luetaan sen ankkuri eikä lopputickiä**:
    lopputickiä sillä ei ole lainkaan, ja sitä edeltävän kierroksen lukema on
    *nollausta edeltävältä* hetkeltä. Puukkokierroksen jälkeen se olisi 1
    vaikka pistemäärä on juuri nollattu -- silloin uudelleenaloitusta seuraava
    kierros saisi ``score_start == score_end`` ja putoaisi pelattujen joukosta.

    Returns:
        Lukema, tai ``None`` jos yhtään ei löytynyt.
    """
    for back in range(index - 1, -1, -1):
        value = (
            anchor_score[back]
            if segments[back].round_raw is None
            else end_score[back]
        )
        if value is not None:
            return value
    return None


def _total_score(rows: list[dict[str, Any]]) -> int | None:
    """Joukkueiden yhteispistemäärä yhdessä tickissä.

    Summa kestää puoliajan vaihdon: joukkuekohtaiset pisteet vaihtavat paikkaa,
    mutta summa säilyy ja kasvaa vain pelatusta kierroksesta.

    Vaatii **molempien** puolten lukeman. Yksipuolinen summa näyttäisi
    kelvolliselta luvulta mutta olisi liian pieni, jolloin kierros voisi pudota
    pelattujen joukosta -- tai pysyä mukana väärällä numerolla.
    """
    per_side: dict[str, int] = {}
    for row in rows:
        if row["team_score"] is not None:
            per_side.setdefault(row["side"], row["team_score"])
    if len(per_side) != len(TEAM_SIDES):
        return None
    return sum(per_side.values())

