# Pappascout

Hobby scouting tool for **Pappaliiga**, a Finnish amateur CS2 league organized on the FACEIT platform.

## What it does

Pappascout generates a short written tactical summary of our team's **upcoming league opponents**:

1. Finds the opponent team's recent FACEIT matches where (nearly) the full roster played
2. Parses the match demos (Python: demoparser2 / awpy)
3. Classifies rounds (pistol / eco / force buy / full buy) per side and per map
4. Produces a concise Markdown report the team reads before the match

## Status

Early planning / development. Built as a non-commercial free-time project for a single team.

- No demo files are redistributed
- Low volume (roughly 10–30 demos per month during the season)

---

# Kehittäjän ohje

## Asennus

```powershell
uv sync
```

Avaimet eivät ole tässä repossa eivätkä OneDrivessa. Luo koneen oma tiedosto
`%USERPROFILE%\.pappascout\.env`:

```
FACEIT_API_KEY=<Data API -avain>
FACEIT_DOWNLOADS_TOKEN=<Downloads API -token>
```

## Käyttö

```powershell
uv run pappascout info          # asetukset, arkiston tila ja avainten tila
uv run pappascout info --koko   # sama, mutta laskee myös arkiston yhteiskoon
uv run pappascout discover                       # divisioonan ottelu- ja joukkueindeksi
uv run pappascout discover --team "Rcave"        # sama + joukkueen vakirosteri
uv run pappascout select --team "Rcave"          # kartat rosterikynnyksella
uv run pappascout fetch --team "Rcave"           # lataa otannan demot FACEITista
uv run pappascout import --match <match_id> --map 1   # kasin ladattu demo arkistoon
uv run pappascout parse <tiedosto|map_demo_id>   # demosta kierrokset ja asetelmat
uv run pappascout classify <map_demo_id> --team <tunniste> --show  # kierrostyypit
uv run pappascout classify <map_demo_id> --kaikki-joukkueet        # molemmat joukkueet
uv run pappascout aggregate --team <tunniste>                      # report.json
uv run pappascout report --team <tunniste>                         # Markdown-raportti
uv run pappascout --version
uv run pytest                   # testit
uv run pytest -m "not demo"     # vain demoista riippumattomat testit
```

### Demot arkistoon: `fetch` ja `import`

Molemmat kirjoittavat saman lopputuloksen -- `demos/<map_demo_id>.dem.zst` ja
sen viereen `.meta.json` -- ja **tuotu demo on putkessa erottamaton
ladatusta**: mikään myöhempi vaihe ei haaraudu sen mukaan, kummasta lähteestä
tiedosto tuli. Aja kummalle tahansa suoraan `parse`.

`fetch` hakee otannan demot FACEITin Downloads API:sta. Se vaatii erillisen
Downloads-käyttöoikeuden, jota **tällä avaimella ei tällä hetkellä ole**
(hakemus jonossa, mitattu 2026-09-05) -- signauskutsu vastaa 403.

`import` ottaa vastaan selaimella kirjautuneena ladatun demon, ja se on siihen
asti **ainoa toimiva polku demon saamiseksi arkistoon**:

```powershell
# Kopioi demo alkuperäisellä nimellään arkiston import-kansioon, sitten:
uv run pappascout import --match 1-79f71e00-1396-4f53-a0b4-782ee9742023 --map 1
```

* `--map` on **1-pohjainen** (ottelun ensimmäinen kartta on 1); arkiston
  tunnisteessa sama kartta on 0.
* Tiedoston pääte päätetään **sisällöstä** eikä annetusta nimestä, joten
  pakkaamaton `.dem` ei päädy arkistoon nimellä `.dem.zst`.
* Kartan nimi luetaan demon omasta otsikosta ja verrataan FACEIT-ottelun
  vetotietoon. **Poikkeama -- ja myös se, ettei vertailua voitu tehdä -- on
  vahvistuskysymys, jota `--kylla` EI ohita.** Se on työkalun ainoa kysymys,
  jota lippu ei hiljennä: väärin nimetty demo ei kaada mitään, se pilaisi
  raportin hiljaa.
* Vajaa pakattu tiedosto torjutaan ennen siirtoa, eikä lähdetiedostoon
  kosketa: kesken kopioituva `.dem.zst` purkautuisi muuten hiljaa vajaana.
* `--file <polku>` nimeää tiedoston suoraan. Import-kansion **ulkopuolinen**
  tiedosto kopioidaan eikä siirretä.

`discover` hakee divisioonan ottelut FACEITista **yhdellä kutsulla** ja
kirjoittaa niistä kaksi indeksiä:

* `index/matches.json` -- kilpailun ottelut: tila, aikataulu, osapuolet ja
  karttavalinnat.
* `index/teams.json` -- joukkueet vakirostereineen. Vakirosteri on aloittajien
  ja vaihtopelaajien **yhdiste** joukkueen kaikista otteluista, myös
  pelaamattomista, ja sen tunnisteet ovat SteamID64-muotoisia -- juuri niillä
  rosteri liittyy demoihin.

Joukkuehaku on kirjainkoosta riippumaton ja hyväksyy nimen osan. **Monitulkintaista
nimeä ei ratkaista hiljaa**: `--team "T"` listaa kaikki kolme T-alkuista
joukkuetta ja pyytää tarkentamaan. Ottelulistaa ei välimuistiteta eikä ajoa
ohiteta, joten uudet ottelut näkyvät joka ajolla -- siksi komennossa ei ole
`--pakota`-valintaa.

`discover` **ei nimeä arkiston hakemistoja uudelleen**. Yhteys
`aggregates/<team_key>`-hakemistoihin näkyy `index/teams.json`:in
`lineup_keys`-kentässä.

`select` lukee molemmat indeksit ja kirjoittaa
`index/selections/<team_key>.json`, jossa on **rivi jokaisesta MapDemosta**:
kelpaako se otantaan, miksi, mikä rosteriluokka ja onko kyseessä liigaottelu.
Verkkoon se ei koske.

Kynnys arvioidaan **karttakohtaisesti**, koska Pappaliiga sallii kaksi vaihtoa
karttojen välissä: sama ottelu voi olla kartalla 1 täysi vakikokoonpano ja
kartalla 2 neljä vakipelaajaa ja yksi ulkopuolinen. Kelpuutus on `5/5`
vakirosterista tai vähintään `4/5` + ulkopuolinen, ja **ulkopuolisen kierrokset
lasketaan mukaan** -- ero näkyy luokassa, ei siinä kuka on otannassa.

Neljä sääntöä, jotka näkyvät suoraan tiedostossa:

* **Rivi syntyy vain pelatuista otteluista.** Pelaamattomalla ottelulla ei ole
  karttoja, joten MapDemoja ei ole olemassa. Pelattu ottelu, jonka vetotieto
  puuttuu, on **eri asia** ja se kerrotaan omana huomionaan -- kartat
  pelattiin, mutta emme tiedä mitkä.
* **Luokka on ennuste ennen parsintaa ja havainto sen jälkeen.** FACEITin
  rosteri on ottelukohtainen, ei karttakohtainen, joten kartan todellisen
  kokoonpanon näkee vasta demosta. `roster_source` sanoo kummasta on kyse, ja
  kun molemmat tiedetään, **havainto voittaa ja ero kerrotaan**.
* **Vetotiedon kartta ei ole todiste pelatusta kartasta.** BO3 päättyy usein
  kahteen karttaan, mutta vedossa on kolme nimeä. Kolmas rivi syntyy mutta jää
  otannan ulkopuolelle, kunnes demo todistaa kartan pelatuksi. BO2:ssa
  (runkosarja) yksikään rivi ei jää epävarmaksi.
* **Hylkäyksellä on aina luettava syy.** `roster_reason` kertoo montako
  vakipelaajaa löytyi, mikä kynnys on, ketkä olivat ulkopuolisia (nimimerkillä)
  ja mistä tieto on peräisin. Rivi ilman syytä on rakenteellisesti mahdoton.

`is_league` päätellään ottelun `competition_id`:stä `[league].championship_ids`
-listaa vasten, **ei nimestä**. Tiedosto kantaa myös käytetyt kynnykset ja sen
vakirosterin, jota vasten päätökset tehtiin -- päätöstä ei voi tarkistaa
jälkikäteen, jos sen peruste on muualla ja ehtinyt muuttua.

`parse` lukee `.dem`- tai `.dem.zst`-tiedoston ja kirjoittaa arkistoon
seitsemän taulua yhdellä lukukerralla:

* `parsed/<map_demo_id>/rounds.parquet` -- kaksi riviä jokaista pelattua
  kierrosta kohden, yksi kummallekin joukkueelle.
* `parsed/<map_demo_id>/ticks.parquet` -- rivi per (pelaaja, kierros,
  näytepiste): alue, koordinaatit ja elossaolo. Näytepisteet ovat
  `[parse].snapshot_seconds` (oletus 6/15/30/45 s freezetimen lopusta) sekä
  **ensikontakti**, eli ensimmäinen ristiinpuolinen osuma muulla kuin
  utilityaseella.
* `parsed/<map_demo_id>/events.parquet` -- rivi per utility-tapahtuma. Heitto
  ja räjähdys ovat kaksi riviä, jotka yhdistää `grenade_no`: lentoradan oma
  tunniste, joka on yksikäsitteinen koko demossa (demojen kesken pari
  `(map_demo_id, grenade_no)`). Pelin oma `grenade_entity_id` on tallessa
  omana sarakkeenaan, mutta se **ei yksilöi kranaattia** -- peli kierrättää
  tunnisteet myös saman kierroksen sisällä, ja se on mukana vain siksi, että
  sillä löytää kranaatin demosta uudelleen. Utility mitataan **heitoista, ei
  ostoista**, ja se luetaan lentoradoista -- `grenade_thrown`-tapahtumaa ei
  ole olemassa.
  Heiton alue on **havainto** -- heittäjän oma `m_szLastPlaceName` samalta
  tickiltä. Räjähdyksellä ei ole omaa aluenimeä, joten sen alue luetaan
  **pistepilvestä** (`callouts.parquet`): lähimmän ruudun alue, jos se on
  enintään `[parse].area_snap_units`in päässä; muuten `area` jää tyhjäksi
  mutta koordinaatit ja `snap_distance` tallentuvat. `area_source`
  (`observed` / `point_cloud`) erottaa nämä kaksi ja `snap_distance` kertoo
  arvion etäisyyden, jotta raportti voi myöhemmin sanoa "3 savua Rampille
  (2 varmaa)". Tyhjä taulu on kelvollinen tulos: utility voi aidosti puuttua.
* `parsed/<map_demo_id>/lineups.parquet` -- rivi per (kokoonpano, pelaaja):
  pelaajan nimi ja hänen klaaninimensä. **Identiteettitaulu, ei kierrostaulu**:
  nimi on sama koko kartan ajan, joten sillä ei ole `round_no`:ta eivätkä sen
  rivit katoa puukkokierroksen mukana. Klaani luetaan **pelaajakohtaisesti**
  (`team_clan_name` SteamID:n kautta) eikä puolen kautta -- puolen kautta
  luettuna sama arvo vaihtaisi joukkuetta puoliajalla. `lineup_key` lasketaan
  edelleen pelkistä SteamID:istä, joten nimet eivät siirrä yhtäkään arkiston
  hakemistoa. Nimi on **havainto**: puuttuva klaani on `null` eikä tunniste,
  ja tyhjä merkkijono ei ole nimi.
* `parsed/<map_demo_id>/deaths.parquet` -- rivi per kuolema: uhri ja ampuja
  **molemmat** alueineen ja koordinaatteineen. Oma taulu eikä
  `events.parquet`iin lisätty tapahtumatyyppi, koska kuolemalla on kaksi
  toimijaa ja molempien paikka on merkityksellinen -- "Luola kuolee" on uhrin
  alue, "Vihu meni secret pihalta" on ampujan. Molemmat alueet ovat
  **havaintoja** samalta `player_death`-tapahtumalta, joten taulussa ei ole
  `area_source`ia eikä `snap_distance`ia; ne ovat olemassa vain kranaatin
  approksimaatiota varten. Ampujaton kuolema (putoaminen, pommi) on aito
  tapaus: jokainen `attacker_*` on silloin `null` eikä riviä pudoteta.
  Puukkokierroksella kuollaan oikeasti, ja ne rivit putoavat samassa
  liitoksessa kuin näytepisteet ja kranaatit -- ajon yhteenveto kertoo
  montako. Johdettuja käsitteitä (trade, entry, duel-voitto) taulussa ei ole:
  ne ovat tulkintaa, ja työnjako on havainto koneelta, tulkinta ihmiseltä.
* `parsed/<map_demo_id>/callouts.parquet` -- **pistepilvi**: rivi per ruudukon
  ruutu, eli missä pelaajat ovat kartalla oikeasti seisoneet ja mikä alue
  (`env_cs_place`) kussakin kohdassa on. Se on räjähdysalueiden **lähde**, ja
  se kirjoitetaan juuri siksi: johdettu alue on tarkistettavissa demoa vasten
  vain, jos se mistä se johdettiin on tallessa -- sama periaate kuin
  `rounds.parquet`in `buy_end_tick`-sarakkeella.

  Pilvi rakennetaan **demon omista tickeistä**, ei arkistoon karttuvasta
  karttakohtaisesta taulusta: karttuva taulu antaisi samalle demolle eri
  tuloksen sen mukaan, mitä muita demoja arkistossa sattuu olemaan, eikä
  `params_hash` voisi kattaa sitä. Ruudun särmä, pystyeron paino ja
  pystytoleranssi ovat `[parse]`-asetuksia (`callout_grid_units` 32,
  `callout_z_weight` 1.0, `callout_z_tolerance_units` 72) -- jokainen
  mitattu, ja perustelut ovat `settings.toml`issa taulukoina.

  Se maksaa **yhden koko demon tickiluvun**: noin 2 s ja 1,0 GB muistihuippu
  per demo, ja huippu syntyy demoparser2:n omasta kehyksestä (1,9 M riviä x 8
  saraketta), ei pelkistyksestä. Luku tehdään myös silloin, kun demossa ei
  ole yhtään kranaattia: taulu on oma tuotoksensa, jonka olemassaolo ei saa
  riippua siitä sattuiko joku heittämään savun.

  **Kynnys ei poistu**, vaikka lähin ruutu löytyy aina: mitattu
  maksimietäisyys on 1 074 yksikköä, ja ilman kynnystä raportti väittäisi
  aluetta räjähdykselle, joka tapahtui kaukana kaikesta missä yksikään pelaaja
  on seissyt. Tyhjä pilvi on kelvollinen tulos -- silloin jokainen
  räjähdysalue on `null`, ajo ei kaadu ja syy kerrotaan ajon yhteenvedossa.
* `parsed/<map_demo_id>/match.parquet` -- **yksi rivi per demo**: ottelun omat
  havainnot, tällä hetkellä kartan nimi demon otsikosta (`parse_header()`).
  Kartta on ottelun ominaisuus eikä kierroksen, joten se ei ole kierros-,
  näytepiste-, tapahtuma- eikä kuolemataulun sarake -- sarakkeena sama arvo
  toistuisi kymmeniä tuhansia kertoja ja joutuisi lisäksi kulkemaan
  `classify`n läpi päätyäkseen `aggregate`en. Sama peruste kuin
  `lineups.parquet`illa.

  Nimi on **havainto ja käytetään sellaisenaan**: sitä ei validoida
  karttapoolia vasten, koska poolin ulkopuolinen kartta (workshop-versio,
  `de_train`) on aito havainto eikä tuntematon kartta. Puuttuva nimi on `null`
  eikä korvike, eikä tyhjä merkkijono ole nimi -- vasta silloin `aggregate`
  päättelee nimen `map_demo_id`:stä karttapoolia vasten. Mitattu 2026-08-31
  kaikilla kahdeksalla arkiston demolla: otsikon nimi on **täsmälleen**
  karttapoolin kirjoitusasu (`de_ancient`), joten haaroja ei tarvitse
  normalisoida. Väite on regressiotesti eikä muistiinpano: taulukko
  `DEMO_HEADER_MAP_NAMES` (`tests/test_demo_parser.py`) naulaa sen jokaiselle
  demolle, jonka repo tuntee, ja oma vartija estää uuden demon jäämisen sen
  ulkopuolelle.

Ilman polkua annettu tunniste etsitään arkiston `demos/`- ja
`import/`-hakemistoista. Toisella ajolla vaihe ohitetaan, jos manifesti täsmää.
Ohituksen ehto lasketaan **koko `[parse]`-osiosta**, joten esimerkiksi
`snapshot_seconds`- tai `area_snap_units`-muutos aiheuttaa uudelleenparsinnan
-- jälkimmäinen muuttaa jokaisen räjähdysrivin `area`-arvon, joten vanha tulos
ei olisi enää ajan tasalla. `[thresholds]`-arvon muuttaminen **ei** aiheuta
uudelleenparsintaa: se on eri osio eikä tämä vaihe edes näe sitä.

Warmup ja puukkokierros eivät ole pelattuja kierroksia eivätkä päädy
yhteenkään tauluun. `round_raw` on demoparser2:n `round_end`-tapahtuman oma
kierrosnumero, joten ne näkyvät siinä aukkona: Ancientilla `round_no` 1..21
vastaa `round_raw`-arvoja 2..22. Ajon yhteenveto kertoo niiden määrän rivillä
`Ohitetut kierrokset`.

**Ottelun uudelleenaloitus** on oma tapauksensa, eikä se jätä aukkoa. Sillä on
freezetime-ankkuri mutta ei `round_end`-tapahtumaa, ja demon oma
kierrosnumerointi jatkuu sen yli yhdellä -- se ei siis kuluta kierrosnumeroa
eikä ole kierros lainkaan. Liigademoissa niitä on tasan yksi, heti
puukkokierroksen jälkeen. Uudelleenaloitus ei saa `round_raw`-arvoa eikä tuota
riviä yhteenkään tauluun, ja sen määrä kerrotaan omalla rivillään
`Uudelleenaloitukset` -- ohitettujen kierrosten luku **ei** sisällä sitä.

Tunnistus nojaa havaintoihin eikä sijaintiin, ja epävarmuus pysäyttää ajon sen
sijaan että kierros pudotettaisiin hiljaa. `parse` keskeytyy suomenkieliseen
virheeseen, jos numeroimattoman kierrosrajan yli demon numerointi **hyppää**
(väliin on jäänyt kierros, jota ei tunnistettu) tai jos uudelleenaloituksia on
useampi kuin yksi (tuntematon ilmiö). Kummassakin viesti kertoo
freezetime-tickit, joista demon voi avata. Kierrosraja, jolla on `round_end`
mutta ei demon omaa numeroa, on kierros eikä uudelleenaloitus: se numeroidaan
naapurista kuten ennenkin.

Näytepisteitä on eri määrä eri kierroksilla: piste, joka osuisi kierroksen
päättymisen jälkeen, jätetään pois. Jos kierros ratkeaa 28 sekunnissa, 30 ja 45
sekunnin pisteitä ei ole olemassa.

Ancient-demo havainnollistaa: 21 pelattua kierrosta ja neljä näytepistettä
antaisi 84 aikapistettä, mutta rajauksen jälkeen niitä on **73**. Ensikontakti
löytyy joka kierrokselta, eli 21 lisää -- yhteensä **94 näytepistettä** ja 940
riviä. Luvut on lukittu testiin `test_ancient_sample_point_count_is_exact`.

Kaikki kymmenen pelaajaa tallentuvat joka näytepisteessä `is_alive`-lipulla;
kuolleiden suodatus on aggregoinnin työ.

`sample_t_s` tarkoittaa aikapisteellä asetuksen lukua ja ensikontaktilla
mitattua hetkeä, joten ryhmittely on aina tehtävä parilla
`(sample_kind, sample_t_s)`.

`--pakota` ohittaa manifestin ja parsii joka tapauksessa.

`classify` lukee `rounds.parquet`:n ja kirjoittaa
`classified/<team_key>/<map_demo_id>.parquet`-taulun, saman sisällön luettavana
kierroslistana `<map_demo_id>.md` ja manifestin. Demoa ei lueta, joten ajo
valmistuu sekunneissa. Jokaisella kierroksella on tyyppi (`pistol`, `eco`,
`half`, `force`, `full`, `ot`, `anomaly`), vastustajan tyyppi, loss count,
ihmisluettava perustelu ja kaikki vertailuun käytetyt arvot -- juuri ne, joilla
luokittelun voi tarkistaa demoa vasten.

`--team` on subjektijoukkueen kokoonpanotunniste (`lineup_key`) tai sen
yksikäsitteinen alkuosa; ilman sitä komento listaa demon molemmat kokoonpanot
aloituspuolineen ja voittoineen. Oikea `team_key` tulee joukkueindeksistä vasta
Epicissä 3, joten siihen asti hakemistonimi on kokoonpanotunniste. `--show`
tulostaa kierroslistan konsoliin, `--kaikki-joukkueet` luokittelee molemmat
joukkueet omiksi tuloksikseen ja `--pakota` ohittaa manifestin.

Kynnykset ovat `[thresholds]`-osiossa, eivät koodissa. Niiden muuttaminen ajaa
`classify`-vaiheen uudelleen mutta **ei** parsintaa: luokittelun parametrihash
lasketaan vain `[thresholds]`- ja `[league]`-osioista.

**Talous mitataan ostoajan lopusta, ei freezetimen lopusta.** CS2:n ostoaika
jatkuu kierroksen alettua, ja noin puolella kierroksista ostetaan vielä silloin
(52 kierroksella 106:sta viidessä liigademossa) -- ankkurista luettuna ne ostot
jäisivät näkymättömiin. Mittauspiste on

```
max(freezetimen loppu,
    min(freezetimen loppu + [parse].buy_window_seconds,
        kierroksen ensimmäistä kuolemaa EDELTÄVÄ tick,
        kierroksen loppu))
```

se on **yksi tick koko kierrokselle**, ja se tallentuu sarakkeeseen
`buy_end_tick`. Kuolemaa *edeltävä* tick eikä kuoleman tick, koska kuolleen
tavaraluettelo on jo tyhjä ja panssari nolla; uloin `max` estää mittauspistettä
valumasta freezetimen sisään, jos kuolema osuu heti ankkurin jälkeen. Ikkunan
pituus on asetus, oletus 20 s -- linjaus, jonka mittaus tukee, ei kalibroitu
kynnys.

Kuolema katkaisee ikkunan, koska kuolleen tavaraluettelo tyhjenee ja panssari
nollautuu. Se laukeaa **noin puolella kierroksista** -- kuudessa demossa
(134 pelattua kierrosta) 69 kierroksella eli 51 %:lla -- joten katkaisujen
määrä ei ole hälytys. Hälytys on se, **maksoiko katkaisu jotain**: ajon tuloste kertoo
montako pelaajaa osti vielä katkaisun jälkeen ja montaako katkaisua ei voitu
tarkistaa lainkaan. Samassa aineistossa molemmat ovat nolla.

Raha on kierrostaulussa kahtena lukuna. `money_buy_end` on **jäljelle jäänyt**
saldo ostoajan jälkeen -- säästökierroksella se on suuri ja täydellä ostolla
pieni -- ja `money_spent` on kierroksella käytetty raha. Näiden summa on se
raha, joka joukkueella oli ostoaikana käytettävissä. Ostettu varustemäärä on
vastaavasti erotus `equip_buy_end - equip_round_start`.

Kaluston **jakauma** on omana havaintonaan: `players_armed_buy_end` kertoo,
monellako pelaajalla oli ostoajan lopussa **panssari ja vähintään yksi ase
hallussa**. Se on käyttäjän oma määritelmä ("kevlar ja jokin parannettu ase --
parempi pistooli, SMG tai halpa kivääri") suoraan mitattuna: ase luetaan
pelaajan tavaraluettelosta ja panssari `m_ArmorValue`-propista. Joukkuesumma ei
kerro tätä -- kaksi AK:ta ja kolme tyhjää antaa saman summan kuin viisi
puolinaista. Luku on aina väliltä `0`-`players_buy_end`; `0` on havainto ja
tarkoittaa **"kukaan ei ollut aseistettu"**, ja `null` tarkoittaa, ettei
havaintoa saatu.

Kevlar ilman asetta ei riitä eikä ase ilman kevlaria. Kypärää ei vaadita: CT
ostaa usein pelkän kevlarin, koska AK tappaa päähän kypärästä huolimatta.
Kranaatit, C4 ja Zeus eivät ole aseita. Laskuri on puolioston **ehto A**
(Story 1.10): se erottaa puolioston ecosta.

> **Hallussapito, ei ostos.** Tavaraluettelo luetaan ostoajan lopusta, joten
> edelliseltä kierrokselta säästetty tai vainajalta poimittu kivääri lasketaan
> samoin kuin juuri ostettu. Se on tarkoitus: kierroksen kannalta ratkaisee
> mitä kädessä on, ei mistä se tuli, ja säästetty AK on yhtä vaarallinen kuin
> ostettu. Ainoa poikkeus ovat oletuspistoolit (Glock-18, USP-S, P2000), jotka
> rajataan ulos siksi, että ne saa joka kierros ilmaiseksi -- niiden
> hallussapito ei kerro mitään.

> **Miksi tavaraluettelo eikä varustearvo.** Story 1.5:n laskuri vertasi
> pelaajan varustearvoa kynnykseen 950 $. Varustearvo on ase + panssari +
> kranaatit yhtenä lukuna, eikä sitä voi purkaa osiin: Ancientista mitattuna
> Glock + kevlar + kaksi valoa on 1250 $ ja laskeutui siis aseistetuksi ilman
> yhtäkään asetta. Story 1.6 vaihtoi arvon havaintoon, ja asetus
> `[parse].armed_player_equip_min` poistui.

**Lukukelvoton havainto tyhjentää koko rivin.** Jos yhdenkin luettavan pelaajan
panssari tai tavaraluettelo puuttuu, laskuri on `null` -- ei se luku, joka
saataisiin lopuista. Pelaaja pysyy `players_buy_end`in jakajassa, joten
`4/5` väittäisi, että yksi oli aseeton, vaikka totuus on ettei häntä saatu
luettua: vaiettu lukuvirhe näyttäisi säästökierrokselta. Tyhjä tavaraluettelo
ja `0` panssaria ovat sen sijaan **havaintoja** eivätkä puutteita.

### Panssarilaskuri on eri luku

`players_armored_buy_end` kertoo, moniko samasta pelaajajoukosta **kantoi
panssaria** (`m_ArmorValue > 0`) samalla ostoajan lopun tickillä. Se ei ole
edellisen yleistys vaan oma havaintonsa, ja kysymykset ovat eri:

| Sarake | Kysymys | Käyttö |
| --- | --- | --- |
| `players_armed_buy_end` | oliko pelaaja taisteluvalmis (kevlar **ja** parannettu ase) | puolioston ehto A, `classify` lukee |
| `players_armored_buy_end` | monellako oli panssari | raportin havainto, `classify` **ei** lue |

Luvut ovat **sisäkkäisiä eivätkä rinnakkaisia**: aseistetun ehto sisältää
panssarin, joten `players_armed_buy_end <= players_armored_buy_end` aina.
Molemmat luetaan samalta tickiltä samasta pelaajajoukosta, joten jakajakin on
sama. `parse` tarkistaa molemmat invariantit kirjoittaessaan taulun.

**Hallussapito, ei ostos.** Panssari säilyy kierroksen yli hengissä
selvinneellä, myös vaurioituneena -- 37/100 on yhä panssari, ja laskuri laskee
sen. Luku kertoo siis mitä pelaajilla *oli* ostoajan lopussa, ei mitä he
*ostivat*. Sama sääntö kuin aseistettujen laskurilla, ja samasta syystä:
kierroksen kannalta ratkaisee mitä kädessä on.

> **Pistoolikierros on poikkeus, ja se pelastaa tärkeimmän rivin.**
> Kierroksilla 1 ja 13 puoliaika alkaa puhtaalta pöydältä eikä perintää ole,
> joten siellä -- ja vain siellä -- luku on **ostohavainto**. Juuri siksi
> Veetin *"5 kevlaria"* on oikea luenta Nuken T-pistoolista ja *"ei kevuja"*
> Ancientin CT-pistoolista. Muilla kierrostyypeillä sama luku on
> hallussapitoa, ja raportin lukuohje sanoo eron ääneen.

**Pistoolikierroksella laskurit myös eroavat eniten.** 800 dollarin
aloitusrahalla kevlar (650) ja parannettu ase eivät mahdu samaan ostokseen,
joten aseistettuja on tyypillisesti 0, vaikka kaikilla viidellä olisi kevlar.
Se on rahan seuraus eikä sääntö: **poimittu ase riittää aseistamaan**, ja
mitattu vastaesimerkki löytyy samasta aineistosta (`Anubis_vs_ryhmarama`,
kierros 13, CT-puolen luvut 3 ja 1). Mitattu neljästä MatureMayhem-demosta
2026-08-30: sen kaikilla kahdeksalla pistoolikierroksella aseistettuja `0`,
panssaroituja `1`-`5`. Kumpaakaan Veetin riviä ei siis voinut lukea
aseistettujen laskurista.

Ecoilla ja forceilla luvut ovat lähellä toisiaan, mutta **ei siksi että siellä
ostettaisiin**: ecolla ei osteta juuri mitään, vaan edelliseltä kierrokselta
selvinneiden panssari näkyy laskurissa sellaisenaan. Kypärää ei eroteta --
analyysi puhuu kevlarista, ja kypärä olisi eri havainto.

Luettavuusehto on **kapeampi** kuin aseistettujen laskurilla: vain
`m_ArmorValue`. Tavaraluettelo ei kuulu siihen, koska tämä laskuri ei lue sitä,
joten lukukelvoton tavaraluettelo tyhjentää vain aseistettujen laskurin.
Kumpikin tyhjentyminen on omassa diagnostiikkaluvussaan
(`armed_unreadable_rows`, `armored_unreadable_rows`), ja niiden erotus on
"rivit, joilla vain tavaraluettelo petti".

`parse` tulostaa myös panssarilaskurin jakauman omalla rivillään
`Panssaroituja`, heti `Aseistettuja`-rivin alla. Kaksi riviä eikä yksi: niiden
**ero** on havainto, ja identtiset jakaumat olisivat merkki siitä, että
panssarilaskuri lukee väärää ehtoa. Kumpikin rivi kantaa sääntönsä mukanaan,
koska kaksi lähes samannimistä riviä peräkkäin luetaan muuten väärin.

**Rahan jakauma on omana havaintonaan.** `money_players_buy_end` säilyttää ne
samat saldot, jotka `money_buy_end` summaa -- yksi luku per luettavissa ollut
pelaaja, laskevasti lajiteltuna. Uutta demokenttää ei tarvita: arvot olivat jo
käsillä, ja tähän asti ne vain summattiin. Summa ei kerro, moniko yksittäinen
pelaaja pystyy ostamaan seuraavalla kierroksella, ja juuri se on puolioston
ehto B. Keskiarvo antaa myös mahdottomia lukuja: mitattu kierros näytti
"30 $/pelaaja", kun todelliset saldot olivat 0, 0, 50, 50, 50 -- kaikki
hinnat ovat viidenkymmenen monikertoja, joten 30 ei voi olla kenenkään saldo.
Lista on aina täsmälleen `players_buy_end` pitkä, ja `null` silloin kun
`players_buy_end` on `null`.

**Tuntematon nimi ei ole ase.** Aseluokittelu (`src/pappascout/constants.py`)
on sallittujen aseiden luettelo, ei kiellettyjen. Se tuntee **57 esinenimeä**,
joista **31 aseistaa**. Veitset ovat avoin joukko -- kuudessa demossa on jo 15
eri skininimeä, ja Valve lisää niitä -- kun taas aseita tulee peliin harvoin.
Kiellettyjen luettelo vanhenisi jokaisen kauppapäivityksen myötä ja tekisi sen
hiljaa; sallittujen luettelo vanhenee näkyvästi ja väärään suuntaan (uusi ase
jää laskematta, mikä on turvallisempi virhe kuin veitsi joka aseistaa).

Luettelo ei ole asetus, koska käyttäjä ei säädä esinenimiä -- mutta sen muutos
**pakottaa uudelleenparsinnan** siinä missä `[parse]`-asetuksetkin: `parse`
laskee luokittelun sisällöstä tiivisteen ja ottaa sen parametrihashiin. Käsin
nostettava versionumero toimisi vain, jos kukaan ei unohda.

`parse` tulostaa laskurin jakauman ajon yhteydessä rivillä `Aseistettuja`, esim.
`panssari ja ase hallussa; 0 -> 5 riviä, 1 -> 2 riviä, ..., 5 -> 30 riviä`. Se
on itsetarkistus: väärä sääntö tuottaisi taulun, joka läpäisee jokaisen
skeematarkistuksen, mutta jakaumasta sen näkee heti. Pelkät ääripäät eivät
riittäisi -- 41 riviä nollaa ja yksi viitonen näyttäisi samalta kuin terve
jakauma. Jos havaintoa ei saatu yhdeltäkään riviltä, rivi sanoo sen ääneen
(`ei yhtään havaintoa`), ja puuttuvien rivien määrä kerrotaan erikseen.

Rivi `Tuntemattomat esineet` nimeää ne tavaraluettelon nimet, joita luokittelu
ei tunne, esiintymämäärineen (`Uusi Ase x12`). Ne eivät aseista ketään, joten
ilman tätä riviä uusi ase ja uusi veitsiskini näyttäisivät täsmälleen samalta:
jakauma vain valuisi hiljaa alaspäin. Määrä erottaa nekin toisistaan -- yksi
eksoottinen veitsi näkyy kerran, demoparser2:n nimeämismuutos joka rivillä.
Rivillä on **kolme tilaa, jotka on syytä osata lukea**:

| Rivi | Mitä se tarkoittaa |
|---|---|
| `ei yhtään` | Tuore ajo, jokainen nimi tunnistettiin. Terve tulos. |
| `N eri esinenimeä: …` | Tuore ajo, luettelo on jäänyt jälkeen. Yli 20 nimeä katkaistaan (`+N muuta`). |
| **rivi puuttuu kokonaan** | **Ohitettu ajo.** Nimet eivät ole taulussa -- ne eivät aseista ketään -- joten niitä ei voi lukea takaisin ilman `--pakota`a. Ei siis vika. |

### `aggregate` -- luokitelluista kierroksista `report.json`

`aggregate` lukee joukkueen luokitellut kierrokset ja **kaikki viisi parsittua
taulua** (`rounds`, `ticks`, `events`, `lineups`, `deaths`) ja kirjoittaa
**yhden tiedoston**:
`aggregates/<team_key>/report.json` sekä sen manifestin. Demoa ei lueta, joten
ajo valmistuu sekunneissa. Tiedosto on pydantic-malli
`domain.report.Report`, ja se on `aggregate`-vaiheen ja tulevan
`render`-vaiheen **jaettu sopimus**: `render` ei laske mitään, vaan kaikki
luvut ovat valmiina.

> **`rounds.parquet` on syöte `classify`n ohi.** Kaikki muut raportin luvut
> tulevat joko luokitellusta taulusta tai näytepiste-, tapahtuma- ja
> kuolematauluista. Panssarilaskuri on ainoa, joka luetaan **parsitusta
> kierrostaulusta suoraan**: se on havainto eikä luokittelun päätöksen syöte,
> joten sitä ei lisätä `economy.CLASSIFY_COLUMNS`iin eikä `classify` kanna
> sitä eteenpäin. Liitos on kolmiosaisella avaimella
> `(map_demo_id, round_no, side)`, koska kierrostaulussa on kaksi riviä per
> kierros -- yksi kummallekin joukkueelle. Kaksi eri lukua samalle avaimelle
> on virhe eikä hiljainen ylikirjoitus, ja tyhjäksi suodattunut kierrostaulu
> keskeyttää ajon samoin kuin tyhjä kuolemataulu.

Demo, jonka **jokin** näistä viidestä taulusta puuttuu, menee osioon
"Puuttuvat demot" syyn kanssa eikä katoa hiljaa; yksittäinen puute ei vie
muita demoja mukanaan.

Rakenne on `maps[] -> sides[] -> round_types[]`, ja jokaisella tasolla on
otanta. Kierrostyypin alla on seitsemän havaintoa:

| Kenttä | Mihin se vastaa |
| --- | --- |
| `positions[].areas[].players_dist[]` | *"3A ja 2B"*, *"2-ramp"* -- pelaajamäärä alueittain näytepisteessä |
| `utility[]` | *"T-spawnista CT-savu B sitelle"*, *"insta mid talo savu"* |
| `utility_counts[]` | *"2 savua 2 valoo"* -- montako heitettiin kierroksella |
| `players_armed` | monellako oli panssari ja ase ostoajan lopussa (puolioston ehto A) |
| `players_armored` | *"5 kevlaria"*, *"ei kevuja"* -- monellako oli panssari, aseesta riippumatta |
| `first_contact[]` | *"otti kontaktin partsi käytävällä"* |
| `deaths` | *"Luola kuolee nii pelaa siteltä"* ja *"Vihu meni secret pihalta"* -- ensimmäisen oman kuoleman ajoitus ja alue, sekä tapot ampujan alueen mukaan |

**Jokainen väite kantaa otantansa.** `n` = kierrokset, joissa havainto tehtiin;
`m` = kyseisen puolen ja kierrostyypin kierrokset, joilta havainto oli
luettavissa. `players_dist` sisältää myös arvon `0` (alue oli tyhjä), joten
`n`-arvojen summa yhden alueen yli on aina `m`. Se on tarkistus eikä koriste:
malli itse nostaa `AggregateError`in, jos summa ei täsmää -- silloin kierros on
kadonnut liitoksessa, ja raportti näyttäisi oikealta mutta väittäisi väärää
otantaa. Sama tarkistus tehdään myös **tasojen välillä**: kierrostyyppien summa
on puolen otanta, puolien summa kartan ja karttojen summa koko raportin.

**Kaksi kenttää on tarkoituksella poikkeus tähän sääntöön.**

`first_contact[]` laskee läsnäoloa eikä pelaajamäärää: sama kierros tuottaa
havainnon jokaiselle alueelle, jolla joukkueella oli elossa oleva pelaaja
sillä hetkellä, joten `n`-arvojen summa on suurempi kuin `m`. Täysi jakauma
samalta hetkeltä on `positions`-listan `first_contact`-näytepisteessä, ja
sitä `Σ n = m` koskee normaalisti.

`deaths.kills[]` on poikkeus toisella tavalla: siellä **`m` ei ole
kierroksia vaan tappoja**. `Σ n = m` pätee, mutta nimittäjä on eri, ja
kierrostyypillä on yleensä enemmän tappoja kuin kierroksia -- `4/6` tarkoittaa
siis neljää tappoa kuudesta eikä neljää kierrosta kuudesta. Siksi raportti
kirjoittaa juuri tälle riville yksikön näkyviin (`Middle (4/6 taposta)`).
`deaths.first_death_areas[]` sen sijaan laskee kierroksia normaalisti:
jokaisella kierroksella on täsmälleen yksi ensimmäinen kuolema, ja
kierrokset joilla joukkue ei menettänyt ketään ovat `deaths.rounds_missing`
eivätkä nollarivi.

`m` on aina **näytepisteen oma**. 45 sekunnin näyte puuttuu kierrokselta, joka
ratkesi 30 sekunnissa, ja `rounds_missing` kertoo erotuksen. Jos `m`:ksi
otettaisiin kierrostyypin kokonaismäärä, ratkennut kierros näkyisi jokaisella
alueella arvona "0 pelaajaa" eli väitteenä, että alue oli tyhjä.

**Kolme lokeroa, ei kahta.** `is_league` syntyy vasta `select`-vaiheessa
(Epic 3), joten käsin tuoduilla demoilla se on `null`. Otanta on siksi
`{league, other, unknown}` jokaisella tasolla. Kahden lokeron jako pakottaisi
merkitsemään käsin tuodut demot joko liigaotteluiksi tai muiksi, ja kumpikin
olisi väärin.

**Aggregointi ei valitse mitä raportoidaan.** Se laskee jokaisen
kierrostyypin, myös täydet ostot ja jatkoajan. Säästökierrosten ja defaultin
eri käsittely on esitysvalinta ja kuuluu raporttiin: jos aggregointi
suodattaisi, valinnan muuttaminen vaatisi uudelleenlaskennan. Se ei myöskään
tulkitse -- sanoja "fake" tai "rush" ei ole missään kentässä.

**Kaksi eri kynnysjoukkoa, eikä niitä saa sekoittaa.** `thresholds_used` on
**tämän aggregointiajon** `[thresholds]`- ja `[aggregate]`-osiot;
`classify_thresholds` on ne arvot, joilla kierrokset *oikeasti luokiteltiin*,
luettuna `CLASSIFIED.inputs`-sarakkeesta. Ajo **keskeytyy** kahdessa
tapauksessa: jos kierrokset on luokiteltu eri kynnyksillä keskenään
(sekoitus tuottaisi luvun, joka ei tarkoita yhtä asiaa), tai jos ne eroavat
nykyisistä asetuksista (kynnystä on muutettu eikä `classify`a ole ajettu
uudelleen, jolloin raportti nimeäisi kynnykset joilla yhtäkään kierrosta ei
luokiteltu). Kummassakin viesti kertoo, mitä ajaa seuraavaksi.

**Kokoonpanot liitetään joukkueeksi.** Kokoonpanotunniste on tiiviste kartalla
pelanneista pelaajista, joten yksi vaihto tuottaa uuden tunnisteen -- ja
neljästä liigademosta yksi on toisen tunnisteen alla. Vaihe liittää
kokoonpanot, joilla on vähintään `[thresholds].team_identity_min_common`
yhteistä pelaajaa, ja kirjaa liitetyt tunnisteet kenttään
`team.lineup_keys`. Ilman liittämistä raportti näkisi kolme demoa neljästä
eikä kertoisi menettäneensä yhtä.

**Kartan nimi on havainto, päättely on varalähde.** Nimi luetaan
`parsed/<map_demo_id>/match.parquet`-taulusta, johon `parse` kirjoittaa sen
demon otsikosta. Sitä ei validoida karttapoolia vasten: poolin ulkopuolinen
kartta on aito havainto. Vasta kun otsikossa ei ollut nimeä, se päätellään
`map_demo_id`:stä karttapoolia vasten (`Ancient_vs_kaljukostaja` ->
`de_ancient`). `map_name_source` kertoo mistä nimi tuli: `demo_header` ->
`map_demo_id` -> `unknown`. Tuntematon kartta jää omaksi haarakseen
tunnisteensa nimellä eikä sulaudu toiseen -- arvausta ei tehdä.

Kaksi demoa samalta kartalta on **yksi haara**: kierrokset summautuvat ja
`map_demo_ids` luettelee demot. Juuri se ei toteutunut ilman otsikkoa, koska
FACEIT-tunnisteessa ei ole kartan nimeä.

**Puuttuva demo ei katoa.** Demo, jonka luokittelu on arkistossa mutta
parsinta puuttuu, päätyy `missing_demos[]`-listaan syyn kanssa eikä kaada ajoa.
Sama koskee kokoonpanoa, jonka näytepistetaulua ei saatu luettua lainkaan:
sitä ei voi liittää joukkueeseen, mutta sen demot kirjataan silti. Kierros
ilman kierrostyyppiä ei mahdu rakenteeseen, mutta sen lukumäärä on kentässä
`unclassified_rounds`, ja räjähdys ilman heittoriviä kentässä
`unpaired_detonations`.

Utilityn aikaikkunat ovat asetus (`[aggregate].utility_seconds_buckets`,
oletus `[5, 10, 20]` eli lokerot `0-5`, `5-10`, `10-20`, `20+`; raja kuuluu
ylempään lokeroon). **`[aggregate]` on oma osionsa juuri tätä varten:**
`classify` laskee parametrihashinsa koko `[thresholds]`-osiosta, joten
aikaikkunoiden säätäminen sieltä käsin mitätöisi jokaisen luokitellun demon
turhaan.

> **Story 2.15: mediaani on väite, ja väite kantaa otantansa.** Karsinta voi
> viedä rivin kaikki aluerivit ja jättää jäljelle pelkän ajoituksen:
> `- ensimmäinen kuolema (mediaani 14,2 s): ei omia kuolemia 2 kierroksella`.
> Rivi luki mitatun luvun ilman yhtäkään otantaa -- epicin toinen kriteeri on
> *"eikä yhtäkään väitettä esitetä ilman otantaa"*. Sekä ensimmäisen kuoleman
> että **ensikontaktin** mediaani kantaa nyt oman otantansa, mutta **vain kun
> rivillä ei ole muuta otantaa**: aluerivit kantavat sen jo itse, ja
> otsikkoon lisätty luku olisi sama otanta kahdesti.
>
> Mediaanin nimittäjä on kierrostyypin **kaikki** kierrokset (`7/9`), kun
> aluerivien nimittäjä on niiden kierrosten määrä, joilla havainto oli
> olemassa (`4/7`). Ero on tarkoituksellinen: mediaani on koko lohkon
> ajoitusväite eikä yhden alueen osuus. Luvut eivät koskaan ole samalla
> rivillä, ja lukuohje sanoo eron ääneen.
>
> **Kynnysten lukijoita on yksi haku, ei kaksi.** `_threshold_int` ja
> `_threshold_float` olivat kaksi kopiota samasta hausta -- kirjoitettuina
> sen kanssa perusteltuna, ettei sitä saa kirjoittaa kahdesti -- ja ne olivat
> jo erkaantuneet: toinen vaati positiivista arvoa, toinen hyväksyi nollan ja
> negatiiviset. Jaettu haku kantaa nyt yhteiset ehdot, ja kutsujille jää
> tyypin sanelema pari: sallittu tyyppi ja alaraja sen yksikössä (`>= 1`
> lukumäärälle, `> 0` osuudelle).

Aggregoinnin parametrihash lasketaan koko `[aggregate]`-osiosta sekä niistä
`[thresholds]`- ja `[league]`-avaimista, jotka vaihe todella lukee:
`small_sample_rounds`, `team_identity_min_common`, `map_pool`, Story
2.5:n kuusi poikkeamakynnystä (`advance_t_share`,
`advance_area_min_observations`, `advance_max_sample_s`,
`advance_min_players`, `crunch_min_players`, `crunch_min_sources`) sekä Story
2.14:n kolme (`stack_min_players`, `stack_group_margin`,
`stack_site_separation_min`).
Luettelo on `stages.aggregate.HASHED_THRESHOLD_KEYS`issa, ja kaksi testiä
vartioi sitä: toinen lukee luetut kentät lähdekoodista, toinen ajaa jokaisen
kynnyksen läpi ja tarkistaa että hash muuttuu.

Muun kynnysarvon muuttaminen ei mitätöi raporttia -- se ajaa `classify`n
uudelleen, ja se näkyy jo syötteiden tunnisteissa. `--pakota` ohittaa
manifestin.

### `report` -- `report.json`:sta luettava Markdown

**Kolme nimeä, yksi asia.** Komento on `report`, vaihe ja paketti `render`, ja
tulos menee hakemistoon `reports/`. Nimet eivät ole synonyymejä vaan kolme eri
tasoa: `report` on se, mitä käyttäjä pyytää; `render` on se, mitä koodi tekee
(latoo valmiit luvut tekstiksi, ei laske niitä); `reports/` on se, minne
tulokset kertyvät. Sama jako kuin muualla putkessa: `aggregate` kirjoittaa
`aggregates/`-hakemistoon.


`report` lukee `aggregates/<team_key>/report.json` ja kirjoittaa
`reports/<team_key>/<YYYY-MM-DDTHHMM>-<team_slug>.md`. **Se ei laske mitään**:
jokainen raportissa esiintyvä luku on aggregoinnissa valmiina, ja uusi luku
raporttiin tarkoittaa muutosta `domain.report.Report`iin. Muodon päättää
Jinja2-malli `src/pappascout/render/report.md.j2`, valinnan
`src/pappascout/render/view.py` -- koodi valitsee **mitä** sanotaan, malli
**miten**.

```powershell
uv run pappascout report --team 9ac    # alkuosa riittää
```

Raportin rakenne on yhteenveto (rosteri, otanta kolmessa lokerossa, puuttuvat
demot, luokittelemattomat kierrokset, käytetyt kynnykset) -> **poikkeamat** ->
kartta -> puoli -> kierrostyyppi -> kierrosliite -> lukuohje -> tekninen
jäljitettävyys. Jokainen havaintorivi kantaa otantansa muodossa
`(4/7 kierroksesta)`; ilman sitä yksi kierros näyttäisi kuviolta.

**Poikkeamaluku on heti yhteenvedon jälkeen, ei lopussa.** Se on epicin
arvokkain tuotos (Story 2.5), ja dokumentin lopussa se olisi juuri se, mitä
ottelua edeltävässä kiireessä ei ehditä lukea. Luku latotaan **ehdoitta**:
tyhjä luku sanoo "ei poikkeamia" ja kertoo samalla mitä tutkittiin -- montako
kierrosta **kukin** sääntö näki, montako arkkitehtuurin sääntöä jäi ajamatta,
jäikö jonkin demon alueorientaatio tyhjäksi ja jäikö joltakin demolta
siteryhmät saamatta. Ilman sitä nolla poikkeamaa lukisi mitattuna
negatiivisena myös sokeasta pisteestä. Sokeita pisteitä on kaksi eri lajia ja
ne koskevat eri sääntöjä: tyhjä orientaatio vaientaa CT-etenemisen ja
crunchin, erottumattomat siteet vaientavat stackin.

Rivi on kaksitasoinen: koontirivi kantaa alueen, otannan ja alueen T-osuuden,
ja sen alla on rivi per kierros. Jako ei ole muotoilua vaan merkitys --
crunchin lähtösuunnat ovat yhtäaikaisia **vain saman kierroksen sisällä**,
joten kahden kierroksen suuntien yhdiste väittäisi useampaa samanaikaista
suuntaa kuin havaittiin. Kierrosnumero on samalla rivillä, koska scoutin
seuraava teko on avata se kierros demolta.

**Runko puhuu nimillä; tunnisteet ovat luvussa "Tekninen jäljitettävyys".**
Ottelua edeltävässä kiireessä luettava dokumentti ei voi puhua tiivisteistä:
ihmiselle tiiviste ja SteamID64 eivät kerro mitään, mutta putken
jäljittämiselle ne ovat ainoa arvo, joka ei vaihdu ottelusta toiseen. Molemmat
ovat totta, joten tunnisteita ei pudoteta vaan kerätään raportin viimeiseen
lukuun: joukkueen tiiviste, kokoonpanotiivisteet, pari `nimi -> SteamID64`
jokaisesta rosterin pelaajasta ja pari `kartta -> demotunnisteet`. Yhteenveto
kertoo nimen ilman tunnistetta, kokoonpanoista lukumäärän ja kynnyksen, ja
rosterista nimet. Kynnykset, työkaluversiot ja aikaleima **eivät** ole
tunnisteita eivätkä siirry: ne kertovat miten luku laskettiin, eikä väitettä voi
arvioida ilman niitä.

Luku ei ole raportin "liite": raportissa on jo `Kierrosliite`, joka osoittaa
`classify`-vaiheen kierroslistoihin arkistossa. Kaksi eri asiaa samalla
sanalla tekisi kumman tahansa mainitsemisen epäselväksi, joten luvusta
puhutaan sen omalla nimellä myös koodissa ja testeissä.

Säännöllä on **kolme poikkeusta**, ja lukuohje nimeää ne. Kussakin tunniste on
rungossa siksi, että se on siinä paikassa ainoa käyttökelpoinen muoto:

| Missä | Miksi tunniste jää |
| --- | --- |
| `Kierrosliite`n polut | Polku on käyttökelpoinen vain sellaisenaan. |
| Puuttuvan demon rivi | Tunniste on osa komentoa, jonka lukija kopioi (`uv run pappascout parse <demo>`). |
| Kartta, jonka nimeä ei tunnistettu | `map_name` **on** silloin `map_demo_id`, eli tunniste on kartan ainoa nimi. |

Luku ei laske eikä lisää mitään: jokainen arvo on `Report`issa valmiina
(`team.key`, `team.lineup_keys`, `roster[].player_id`, `maps[].map_demo_ids`),
joten se ei kosketa `report.json`ia eikä `REPORT_SCHEMA_VERSION`ia. Sen rivit
ovat siis raportin muoto eikä uusi havainto -- toisin kuin muut luvut, joiden
lisääminen tarkoittaa muutosta `Report`iin.

Rivit ovat pareja `nimiö -> arvo`, ja **arvo on koodijakso**
(`` `ANCIENT_vs_RCAVE_VETERANS` ``) eikä paettu teksti: luvun koko arvo on se,
että tunnisteen voi kopioida raportista komennolle, ja escapetus tekisi
alaviivallisesta demotunnisteesta merkkijonon, joka ei enää täsmää yhteenkään
arkiston hakemistoon. Ainoa poikkeus on tunniste, joka itse sisältää graviksen
(Windowsissa laillinen tiedostonimessä): koodijakso ei voi sisältää sitä, joten
arvo paetaan tekstinä -- rikkinäinen koodijakso latoisi loppuraportin väärin.

Karttarivin **nimiö** on samoin koodijakso, koska kartan nimi on demon
otsikosta luettu havainto eikä sitä validoida karttapoolia vasten (ks. yllä):
ilman koodijaksoa workshop-nimi kuten `*|Aim|* Botz [beta]` katkaisisi rivin
kesken, ja juuri se rivi kantaa demotunnisteet. Koodijakso säilyttää täsmälleen
samat merkit, joten kartta ei saa toista kirjoitusasua kuin karttaluvun
otsikossa.

> **Story 2.15: koodi rikkoi tätä omaa sääntöään kahdessa paikassa kolmesta.**
> Lause "kartta ei saa toista kirjoitusasua kuin karttaluvun otsikossa" oli
> kirjoitettu tähän jo aiemmin, mutta karttaluvun otsikko ja poikkeamarivi
> latoivat nimen **paljaana** -- eli sääntö toteutui vain siinä yhdessä
> paikassa, jossa se oli kirjoitettu. Molemmat käyttävät nyt samaa
> koodijaksoa. Oikeilla karttanimillä (`de_ancient`) ero näkyy raportissa
> gravisparina; workshop-nimellä se on ero ehjän ja katkenneen rivin välillä.
>
> Sama tarina suojasi myös **aluenimen** (`m_szLastPlaceName`), joka on demon
> antamaa tekstiä siinä missä joukkueen ja kartan nimi. Alue paetaan
> (`markdown_text`) eikä kääritä koodijaksoon, ja jako on projektin oma:
> **nimi paetaan, tunniste kääritään.** Aluenimeä ei kopioida raportista
> mihinkään -- se luetaan lauseen osana keskellä väiteriviä, ja koodijakso
> katkoisi jokaisen havaintorivin kolmeen palaan. Kaikilla oikeilla
> CS2-alueilla escapetus on näkymätön, joten raportti ei muuttunut.

Rosterin nimiöt on **numeroitu** (`3. pelaaja`), koska nimi ei ole
yksikäsitteinen avain: kaksi nimetöntä tai kaksi samannimistä pelaajaa
tuottaisivat ilman lukua kaksi identtistä riviä. Numero on paikka yhteenvedon
nimilistassa, joten pari löytyy laskemalla. Samasta syystä tunnistamattoman
kartan nimiö on `kartta N, nimeä ei tunnistettu` eikä sen demotunniste: nimiö
ja arvo olisivat muuten sama merkkijono.

Kierroksen tarkkuudella kuvattavat tyypit (`pistol`, `eco`, `force`, `half`)
saavat jokaisen havaintonsa kirjoitettuna. Täydet ostot (`full`) ja jatkoaika
(`ot`) kuvataan **vain toistuvina kuvioina**, ja toistumisen raja luetaan
raportista (`thresholds_used.thresholds.small_sample_rounds`) -- sitä ei
keksitä renderöinnissä. Pois jätettyjen havaintojen määrä kirjoitetaan
näkyviin, joten suodatus ei ole hiljainen.

> **Sana "säästökierros" tarkoittaa koodissa eri asiaa kuin tässä luvussa.**
> Yllä oleva nelikko on **renderöinnin** sääntö: mitkä tyypit kerrotaan
> kierroksen tarkkuudella. `constants.SAVING_ROUND_TYPES` on **taloudellinen**
> luettelo -- tyypit, joilla joukkueella ei ole varaa normaaliin ostoon -- ja
> siitä puuttuu `pistol`, koska silloin kummallakaan puolella ei ole
> ostokykyä eikä etenemisestä voi päätellä suunnitelmaa. Vain jälkimmäinen
> rajaa Story 2.5:n CT-etenemissääntöä.

**Vanha raportti ei koskaan ylikirjoitu.** Nimessä on aikaleima minuutin
tarkkuudella, ja saman minuutin sisällä ajetut saavat nollatäytetyn päätteen
`-02`, `-03` (täyttö on lajittelua varten: ilman sitä listaus järjestäisi
`-10, -100, -11, -2`). Nimi varataan atomisesti (`O_CREAT | O_EXCL`) ennen
kirjoitusta, joten kaksi rinnakkaista ajoa ei voi valita samaa nimeä; jos
kirjoitus epäonnistuu, varaus perutaan eikä hakemistoon jää tyhjää `.md`:tä.
Siksi komennossa ei ole `--pakota`-valintaa eikä vaihetta koskaan ohiteta
manifestin perusteella: käyttäjä ajaa komennon silloin kun hän haluaa
raportin.

**Joukkueen ja pelaajien nimet ovat havaintoja.** `display_name` on demosta
luettu klaaninimi (`lineups.parquet`:n `clan_name`), ja jokaisella rosterin
rivillä on sekä nimi että SteamID64 -- nimi luettavuutta varten, tunniste
jäljitettävyyttä varten. Kumpikaan ei korvaa toista eikä kumpikaan katoa; ne
vain asuvat eri luvuissa. Pelaaja, jonka nimeä ei saatu luettua, pysyy
rosterin lukumäärässä: yhteenvedossa hänen paikallaan on "nimi ei luettavissa"
ja jäljitettävyysluvussa sama teksti järjestyslukunsa kanssa hänen
SteamID64:nsä edessä. `display_name_source` kertoo joukkueen nimestä kummasta
on kyse: `clan_name` = havaittu, `team_key` = havaintoa ei ole. Ilman havaintoa
raportti **sanoo sen ääneen** eikä toista tiivistettä nimen paikalla: otsikko
on "Scouting-raportti -- joukkueen nimi ei tiedossa" ja yhteenvedon rivi kertoo
puuttumisen syyn sekä sen, missä luvussa tunniste on. Jos liitetyt demot
antavat joukkueelle eri nimen, näytetään useimmin havaittu ja loput luetellaan
(`display_name_alternatives`) -- ristiriita ei katoa. Äänestys on
kaksivaiheinen enemmistö: ensin ratkeaa demon sisällä yleisin klaani, sitten
se saa demonsa yhden äänen. Viiden pelaajan demo ei siis paina viittä kertaa
yhden pelaajan demoa, eikä yksi eri mieltä oleva pelaaja voi nostaa nimeään
otsikkoon. Tasatilanne ratkeaa molemmilla tasoilla aakkosjärjestyksessä,
jotta ajo on toistettava.

Joukkueen **avain** ei muutu: `team_key` on hakemistorakenne
(`classified/<team_key>/`), ja sen vaihtaminen nimeksi on Epic 3:n
`select`-vaiheen työtä. Nimi vaihtaa vain sen, mitä näytetään -- ja raportin
tiedostonimen slugin, joka seuraa näytettävää nimeä
(`reports/<team_key>/<aikaleima>-maturemayhem.md`). Jos nimestä ei jää yhtään
ASCII-merkkiä (kyrillinen tai CJK-klaani), slug johdetaan tunnisteesta -- ei
jaetusta vakiosta, joka antaisi kaikille tällaisille joukkueille saman
tiedostonimen.

> **Nimet pakottavat koko arkiston uudelleenajon.** Muutos toi uuden taulun
> (`lineups.parquet`) ja nosti `REPORT_SCHEMA_VERSION`in `1.0.0` -> `2.0.0`,
> koska `TeamReport.roster` muuttui `list[str]`:stä oliolistaksi. Seuraukset:
> jokainen arkiston demo on parsittava uudelleen (`parse` ei hyväksy tulosta
> ajan tasalla olevana ilman `lineups.parquet`ia, joten pelkkä `uv run
> pappascout parse <id>` riittää -- `--pakota` ei ole tarpeen), ja jokainen
> `report.json` on aggregoitava uudelleen (vanha versio luetaan tuntemattomana
> ja kirjoitetaan yli). Jo kirjoitetut Markdown-raportit jäävät paikoilleen
> vanhoina; niitä ei koskaan ylikirjoiteta.

**Kuolemat selittävät muut rivit, eivätkä ole oma lukunsa.** Jokainen
kierrostyyppi saa enintään **kaksi** kuolemariviä: mistä ja milloin joukkue
menetti ensimmäisen pelaajansa, ja miltä alueilta se teki tappoja. Raja on
`render.view.MAX_DEATH_LINES`, ja sen ylitys on virhe eikä hiljainen kasvu.

Tapporivin otanta on ainoa koko raportissa, joka **ei laske kierroksia**:
kierrostyypillä on yleensä enemmän tappoja kuin kierroksia, joten rivi sanoo
yksikkönsä itse (`Middle (4/6 taposta)`) eikä vain lukuohjeessa. Alue on
**ampujan** oma alue tappohetkellä, ei uhrin.

> **Kuolemataulu pakottaa koko arkiston uudelleenajon.** Muutos toi uuden
> taulun (`deaths.parquet`) ja nosti `REPORT_SCHEMA_VERSION`in `2.0.0` ->
> `3.0.0`, koska `RoundTypeReport` sai pakollisen `deaths`-kentän. Seuraukset
> ovat samat kuin nimillä: `parse` ei hyväksy tulosta ajan tasalla olevana
> ilman `deaths.parquet`ia, joten pelkkä `uv run pappascout parse <id>`
> riittää, ja jokainen `report.json` aggregoidaan uudelleen. Jo kirjoitetut
> Markdown-raportit jäävät paikoilleen vanhoina.

> **Panssarilaskuri pakottaa koko arkiston uudelleenajon -- eri syystä kuin
> kaksi edellistä.** Muutos ei tuo uutta taulua: olemassa oleva
> `rounds.parquet` saa **pakollisen sarakkeen** (`players_armored_buy_end`),
> ja `REPORT_SCHEMA_VERSION` nousee `3.0.0` -> `4.0.0`, koska
> `RoundTypeReport` sai pakollisen `players_armored`-kentän. Lisäksi
> `aggregate` alkaa vaatia `rounds.parquet`ia, jota se ei aiemmin lukenut.
>
> Täyden arkiston omistajalle tämä tarkoittaa kolmea komentoa demoa kohden,
> tässä järjestyksessä:
>
> 1. `uv run pappascout parse <map_demo_id>` -- **`--pakota` ei ole tarpeen**:
>    `parse` huomaa vanhan taulun puuttuvan sarakkeen skeematarkistuksessa ja
>    ajaa demon uudelleen itse.
> 2. `uv run pappascout classify <map_demo_id> --kaikki-joukkueet` -- pakollinen,
>    koska luokittelun manifesti osoittaa juuri uusittuun parsintaan. Yksikään
>    kierrostyyppi ei muutu tämän takia; sarake ei ole luokittelun syöte.
> 3. `uv run pappascout aggregate --team <key>` ja `report --team <key>`.
>
> Väliin jäänyt vaihe kertoo itse mitä tehdä: `classify` ja `aggregate`
> hylkäävät vanhan kierrostaulun suomenkielisellä virheellä, joka nimeää sekä
> puuttuvan sarakkeen että komennon. Jo kirjoitetut Markdown-raportit jäävät
> paikoilleen vanhoina.

> **Räjähdysalueen pistepilvi pakottaa koko arkiston uudelleenajon --
> kolmatta kautta.** Muutos tuo uuden taulun (`callouts.parquet`) *ja*
> muuttaa `events.parquet`in arvojoukkoa: `area_source`-luettelosta poistui
> `snapped` ja tilalle tuli `point_cloud`. Vanha taulu ei siis lataudu enää
> tämän version enumiin, ja `REPORT_SCHEMA_VERSION` nousee `4.0.0` ->
> `5.0.0`, koska `UtilityUse.area_source` hylkää vanhan arvon.
>
> Komennot ovat samat kolme kuin edellä, samassa järjestyksessä, eikä
> `--pakota` ole tarpeen: `parse` huomaa sekä puuttuvan taulun että
> vanhentuneen enumin itse.
>
> Muutos ei ole tarkennus vaan **menetelmän vaihto**. Räjähdysalue johdettiin
> aiemmin lähimmästä elossa olevasta pelaajasta, ja se oli rakenteellisesti
> väärä eikä vain epätarkka: savu heitetään sinne, missä ketään ei ole --
> juuri siksi, että se estää näkyvyyden ja pakottaa rotaatioita. Neljästä
> liigademosta **42 %** räjähdyksistä jäi kokonaan ilman aluetta (722/1 716).
> Pistepilvellä ja kynnyksellä 256 osuus on **4,3 %** (74/1 716). Kynnys on
> kalibroitu kaikilla kuudella demolla: 2 428/2 544 eli 95,4 % räjähdyksistä
> saa alueen. Raportin tasolla utility-kuvioita ilman räjähdysaluetta oli
> 219/591 (37 %) ja on nyt 14/508 (2,8 %).
>
> Ajon yhteenveto sai samalla kolme uutta riviä (`Räjähdysalue`,
> `Etäisyys ruutuun`, `Pistepilvi`) ja menetti yhden: `Nimetön alue` kertoi
> tapauksesta "lähin pelaaja löytyi, mutta pelillä ei ole nimeä hänen
> alueelleen", eikä sitä voi enää syntyä -- pistepilveen ei pääse nimetöntä
> ruutua. Sen tilalla on `Kynnyksen takana`.

> **Kartan nimi demon otsikosta pakottaa koko arkiston uudelleenajon --
> neljättä kautta.** Muutos tuo uuden taulun (`match.parquet`) ja nostaa
> `REPORT_SCHEMA_VERSION`in `5.0.0` -> `6.0.0`. Komennot ovat samat kolme kuin
> edellä, samassa järjestyksessä, eikä `--pakota` ole tarpeen: `parse` huomaa
> puuttuvan taulun itse (`expected_outputs`).
>
> Jos ajat `aggregate`n ennen parsintaa, sen virheilmoitus neuvoo `--pakota`a.
> Ohje on tarkoituksella varovainen: sama viesti palvelee myös tapausta, jossa
> taulu on olemassa mutta väärillä sarakkeilla, eikä `aggregate` näe kumpi
> tilanne on käsillä. Tässä muutoksessa pelkkä `uv run pappascout parse <id>`
> riittää; lipun antaminen ei ole väärin, se vain parsii turhaan ne demot,
> jotka olisivat menneet uudelleen joka tapauksessa.
>
> Syy on rakenteellinen eikä kosmeettinen. Kartan nimi ei ollut yhdessäkään
> taulussa, joten `aggregate` päätteli sen `map_demo_id`:stä karttapoolia
> vasten -- ja FACEIT-tunnisteessa (`1-79f71e00-...-1-1`) ei ole kartan nimeä.
> Jokainen FACEIT-demo jäi siis **omaksi karttahaarakseen** tunnisteensa
> nimellä. Mitattu RCAVE-raportista 31.8.: ennen **neljä** karttahaaraa
> kolmesta kartasta (kaksi Ancient-demoa eivät yhdistyneet), jälkeen **kolme**
> ja `de_ancient` 42 kierrosta kahdesta demosta 21:n sijaan. Merkintä
> "kartan nimeä ei tunnistettu tunnisteesta" 2 -> 0 ja "(1/1 kierroksesta)"
> 152 -> 102; jäljelle jäävät ovat aitoja yhden kierroksen näytteitä eivätkä
> haaratason pirstoutumista.
>
> Päättely poolista jää **varalähteeksi**, ja `map_name_source` kertoo
> mistä nimi tuli: `demo_header` -> `map_demo_id` -> `unknown`. Käsin
> tuoduilla demoilla nimi ei muutu, vain lähde vaihtuu: neljän
> MatureMayhem-demon raportti on muutoksen jälkeen tavu tavulta sama kuin
> ennen, ainoa ero on aikaleimarivi.

> **Poikkeavat asetelmat pakottavat luokittelun ja aggregoinnin uudelleen,
> mutta eivät parsintaa.** Story 2.5 lisää kuusi kynnystä
> `[thresholds]`-osioon ja nostaa `REPORT_SCHEMA_VERSION`in `6.0.0` ->
> `7.0.0`. Uudelleenparsintaa **ei tarvita**: mikään parsittu taulu ei muutu.
>
> Komennot ovat kaksi eikä kolme, eikä `--pakota` ole tarpeen:
>
> ```powershell
> uv run pappascout classify <map_demo_id> --kaikki-joukkueet   # jokaiselle demolle
> uv run pappascout aggregate --team <tunniste>
> uv run pappascout report --team <tunniste>
> ```
>
> `classify` ajautuu uudelleen itsestään, koska sen parametrihash kattaa
> **koko** `[thresholds]`-osion (`stages/classify.py:_params_hash`). Se on
> oikea käyttäytyminen eikä vika: osittainen hash vaatisi luettelon siitä,
> mitä kenttiä säännöt sattuvat lukemaan, ja se luettelo vanhenisi hiljaa.
> Hinta on tarpeeton uudelleenajo, joka maksaa sekunteja -- demoa ei lueta.
>
> Version nosto on **oletusarvon takia eikä sen puutteesta**. `anomalies` on
> `default_factory=list`, joten vanha `report.json` validoituisi tyhjällä
> listalla -- ja juuri se on syy nostaa: tyhjä poikkeamaluku on tässä mallissa
> **havainto** ("ei poikkeamia"), joten vanhasta tiedostosta renderöity
> raportti väittäisi mitatuksi tulokseksi sen, ettei sääntöjä ollut
> olemassa. Ehto "validoituuko vanha tiedosto" ei siis riitä yksin.
>
> Mitattu koko arkistosta 2026-09-02 (kahdeksan demoa, 93 CT-kierrosta):
> CT-eteneminen osuu **6 kierroksella** ja crunch **4 kierroksella**, eli 6,5 %
> ja 4,3 % -- poikkeaman kokoluokka. Nukella ei ole yhtään poikkeamaa
> kummassakaan raportissa, ja se on **tosi negatiivinen**: `Lobby`
> tunnistetaan oikein T:n alueeksi (T-osuus 0,89 ja 0,98), mutta lobby
> crunchia ei yhdelläkään kierroksella tehty.
>
> Arkkitehtuurin (AD-10) kolmesta poikkeamasäännöstä ajettiin tässä
> tarinassa **kaksi**. Kolmas, 4-5 pelaajan stack yhdellä sitellä, oli
> mitattu mahdottomaksi pelin nykyisellä aluejaolla (0 osumaa 93
> kierroksesta, koska peli jakaa siten useaan `env_cs_place`-alueeseen) ja
> siirrettiin omaksi tarinakseen. Story 2.14 toteutti sen; ks. alla.
>
> **Stack-sääntö: alueryhmä johdetaan demon omasta pistepilvestä.** Story
> 2.14 ottaa käyttöön AD-10:n kolmannen säännön ja lisää kolme kynnystä
> `[thresholds]`-osioon (`stack_min_players`, `stack_group_margin`,
> `stack_site_separation_min`). `REPORT_SCHEMA_VERSION` nousee `7.0.0` ->
> `8.0.0`. Uudelleenparsintaa **ei tarvita**: sääntö lukee `parse`-vaiheen jo
> kirjoittamaa `callouts.parquet`-pistepilveä (Story 2.9), eikä yksikään
> parsittu taulu muutu. Komennot ovat samat kaksi kuin Story 2.5:ssä.
>
> Puuttuva pala oli kuvaus `alue -> alueryhmä`, ja se **johdetaan** eikä
> anneta: jokaisen alueen keskipiste on sen pistepilviruutujen **solumediaani**
> (jokainen ruutu painaa yhden, havaintomäärä ei paina), ja alue kuuluu
> lähemmän siten ryhmään, jos toinen site on vähintään `stack_group_margin`
> kertaa kauempana. Sama lukittu ehto kuin alueorientaatiolla: ei
> karttatietokantaa, ei ihmisen antamaa aluejakoa, ei arkiston yli kertyvää
> taulua.
>
> Solumediaani on **mitattu ehto eikä yksinkertaistus**. Pelin
> `m_szLastPlaceName` on *viimeksi nimetty* alue, joten nimeämättömässä
> kohdassa seisova pelaaja kantaa edellisen kierroksen aluetta;
> `Ancient_vs_kaljukostaja`n CT-spawnin ruuduissa on `BombsiteB` 75 524
> havaintoa ja `CTSpawn` vain 135. Havaintopainotettu keskiarvo vetää siten
> keskipisteen spawniin, solumediaani ei. Mitattu: Ancientin ristiriitaiset
> alueet 5/18 -> **0/18**, eli aluejako on sanatarkasti sama kaikista
> kolmesta Ancient-demosta.
>
> **Kartta, jolla siteet eivät erotu, vaikenee.** Nukella `BombsiteA` ja
> `BombsiteB` ovat päällekkäin eri kerroksissa, joten mikä tahansa
> A/B-etäisyysmittari on siellä mieletön. Vartija on suhdeluku eikä
> karttalista: siteiden keskipisteiden etäisyys jaettuna siteiden omalla
> koolla on 0,47-0,54 Nukella ja 3,70-5,04 kolmella muulla kartalla, joten
> kynnys 2,0 erottaa ne **ilman että karttaa nimetään koodissa**.
> Vaikeneminen kirjataan kattavuuteen
> (`anomaly_scan.demos_without_site_groups`), koska nolla vaiennetulla
> demolla ei ole mitattu negatiivinen.
>
> Mitattu koko arkistosta (kahdeksan demoa, 93 CT-kierrosta): stack osuu
> **9 kierroksella ja 10 näytepisteellä**, ja se näki **66 kierrosta** --
> Nuken 27 kierrosta vaiennettiin. Osumat jakautuvat kolmelle näytepisteelle
> (6 s: 1, 15 s: 5, 30 s: 4), ja `stack_min_players = 5` antaa 2 kierrosta,
> eli viisi on säännön aito ääripää eikä tyhjä joukko.
>
> Siten omalla alueella **riittää yksi pelaaja**, ja sekin on mitattu valinta:
> vaatimus `>= 2` antaa 5 osumaa 5 kierroksella ja `>= 3` kaksi kumpaakin.
> Tiukennus ei poistaisi kohinaa vaan puolet havainnoista, ja pelaajamäärä on
> rivillä joka tapauksessa muodossa 4/5.
>
> Kaksi lisäehtoa ovat määritelmää eivätkä hienosäätöä. **Siten oma alue**
> vaaditaan (`BombsiteA`/`BombsiteB`): "stack sitellä" tarkoittaa että ollaan
> sitellä, ei että ollaan kartan siinä puoliskossa -- ilman ehtoa Ancientin
> `Alley` yksin tuottaa osumia, ja mitattuna ehto pudottaa 17 kierrosta -> 9.
> **Spawnit eivät laske**: `CTSpawn` on Ancientilla A-ryhmässä ja Infernolla
> B-ryhmässä, joten ilman rajausta pelkkä aloitusasetelma laukaisisi säännön.
>
> **`callouts.parquet` on tästä lähtien pakollinen jokaiselta demolta.**
> `aggregate` lukee sen samalla listalla kuin kuusi muuta parsittua taulua,
> joten ennen Story 2.9:ää parsittu demo putoaa **koko raportista** -- ei vain
> stackista -- ja päätyy osioon "Puuttuvat demot" komennon kanssa. Se on
> tarkoituksellinen, ja ero on kattavuuden arvo: vaiennettu demo on havainto
> kartasta (siteet eivät erotu tasoina) ja se kirjataan
> `anomaly_scan.demos_without_site_groups`iin, kun taas puuttuva taulu on
> vanhalla versiolla parsittu demo. Hiljaisena jälkimmäinen lukisi
> kattavuudessa edellisenä. Korjaus on `uv run pappascout parse <id>`;
> `--pakota` ei ole tarpeen, koska `parse` huomaa puuttuvan taulun itse.

#### Karsinta: toisto pois, havainnot jäävät

Raportti oli **96 sisältöriviä karttaa kohden** (RCAVE, 3 karttaa), Veetin oma
analyysi noin 30, ja epicin mittari on "noin sivu per kartta". Osa pituudesta
oli puhdasta toistoa, ja Story 2.13 jättää sen kirjoittamatta viidellä
säännöllä. Kukin sääntö on **oma asetuksensa** `[report]`-osiossa, joten mikä
tahansa niistä kääntyy pois ilman koodimuutosta -- ja kaikki viisi pois
päältä tarkoittaa, että raportti on **merkki merkiltä sama** kuin ennen
karsintaa.

Taulukon "osuu" on mitattu arkiston molemmista raporteista (RCAVE 3 karttaa /
MatureMayhem 4 karttaa, kahdeksan demoa, 52 kierrostyyppilohkoa). **Yksikkö
on eri riveillä eri**, ja se on merkitty näkyviin: sääntö 1 ja 3 poistavat
rivejä, sääntö 2 yhdistää kaksi riviä yhdeksi, ja säännöt 4 ja 5 lyhentävät
riviä pudottamatta sitä.

| # | Sääntö | Asetus | Oletus | Osuu (RCAVE / MM) |
| --- | --- | --- | --- | --- |
| 1 | Kylläinen kalustorivi pois | `drop_saturated_equipment_lines` | päällä | 24 / 20 **riviä** |
| 2 | Kalustorivit yhdeksi kun luvut ovat samat | `merge_equal_equipment_lines` | päällä | 18/25 / 19/27 **lohkoa** (yksi rivi vähemmän kussakin) |
| 3 | Nimetty näytepiste pois | `skip_sample_seconds` | **pois** | 14 / 16 **riviä** arvolla `[45.0]` |
| 4 | Utilityn kohteista N yleisintä | `max_utility_targets` | 2 | 6 / 2 **riviä lyhenee** |
| 5 | Tapoista N yleisintä aluetta | `max_kill_areas` | 3 | 4 / 2 **riviä lyhenee** |

Kokonaisvaikutus, sisältörivit karttalukujen sisällä:

| Raportti | Kaikki pois | Oletukset | Oletukset + sääntö 3 |
| --- | --- | --- | --- |
| RCAVE (3 karttaa) | 288 (96/kartta) | 258 (86/kartta) | 244 (81/kartta) |
| MatureMayhem (4 karttaa) | 347 (87/kartta) | 318 (80/kartta) | 302 (76/kartta) |

Oletuksilla siis **noin 10 riviä vähemmän karttaa kohden**. Se ei ole vielä
sivu: sivu vaatisi kokonaisten kierrostyyppilohkojen vaimentamisen, ja se on
rajattu ulos -- Veetin oma päätös 31.8. oli "lisäys ensin, vaimennus
myöhemmin", ja vaimennus vaatii poikkeamalogiikkaan luottamista. Mittaukset
kokonaisuudessaan ovat BMAD-tuotoksissa
(`_bmad-output/implementation-artifacts/karsinta-mittaus.md`) eivätkä tässä
repossa.

Neljä sääntöä, jotka pätevät kaikkiin viiteen:

**Karsinta koskee esitystä eikä sisältöä.** `report.json` ei muutu eikä
`REPORT_SCHEMA_VERSION` nouse: jokainen karsittu arvo on siellä yhä, se vain
jää kertomatta *tässä* raportissa. Säännön kääntäminen pois ja `report`in
ajaminen uudelleen tuo rivin takaisin **ilman uudelleenaggregointia**. Juuri
siksi asetukset ovat `[report]`-osiossa eivätkä `[aggregate]`ssa: aggregoinnin
hash kattaa koko oman osionsa, joten sinne kirjoitettu esitysvalinta
mitätöisi jokaisen `report.json`in.

**Karsinta ei muuta yhtäkään lukua.** Rivit rakennetaan ensin ja karsitaan
vasta sitten, joten lohkon kuviosuodatuksen huomautus ("N harvinaisempaa
havaintoa jäi pois") on **sama karsinnan kanssa ja ilman**. Se on väite
datasta eikä esitysvalinta. Sama järjestys ratkaisee myös sen, milloin sääntö
*ei* poistanut mitään: rivi, jota kuviosuodatus ei päästänyt syntymään, ei ole
karsittu -- eikä lukuohje väitä niin.

**Mikään ei katoa hiljaa.** Jos rivi jätetään kirjoittamatta, lukuohje kertoo
kertaalleen mitä sen puuttuminen tarkoittaa ja nimeää asetuksen, jolla sääntö
kääntyy pois; jos riviltä jää pois väitteitä, rivi kertoo pudotettujen määrän
samalla lauseella kuin kuviosuodatus ("7 harvinaisempaa kohdetta jäi pois").
Lukuohje selittää **vain ne säännöt, jotka oikeasti karsivat jotakin** tässä
raportissa -- selitys säännöstä, joka ei osunut kertaakaan, olisi väite
raportista, joka ei pidä. Yhteenvedon rivi `Karsinnan säännöt` (ja `info`-
komennon rivi `Karsinta`) luettelee **kaikki** arvot, koska muuten puhtaan
raportin lukija ei näkisi, mitkä säännöt olivat päällä; kun jokainen sääntö on
pois, rivi jää pois eikä raportti eroa esikarsinta-ajan raportista.

**Suojattuja kierrostyyppejä ei karsita yhdelläkään säännöllä**
(`render.view.PROTECTED_ROUND_TYPES`), ja jokainen lukuohjeen
karsintakappale sanoo sen ääneen -- sama raportti sisältää karsimattomia
lohkoja, joten ehdoton lause olisi väärä. Suojatut ovat:

| Kierrostyyppi | Miksi kalustorivi on siellä havainto |
| --- | --- |
| `pistol` | Panssariluku on **ostohavainto** vain pistoolikierroksella (Story 2.8); muualla se on hallussapitoa, joka periytyy edelliseltä kierrokselta. |
| `anomaly` | `classify` varaa tyypin ristiriitaiselle havainnolle (varustearvo laski ostoaikana) ja voiton jälkeiselle ostolle, jota ei käytännössä tehty -- kummassakin **kalusto on se havainto**, jonka takia lohko on olemassa. |

Jatkoaikaa (`ot`) **ei** suojata, ja se on mittaustulos: `[league]
.ot_start_money` on 12 500 $, joten jatkoajalla ostetaan täysi kalusto ja 5/5
on odotus kuten täydellä ostolla. Jos aloitusraha joskus laskee
pistoolitasolle, `ot` on suojattava -- riippuvuus on kirjoitettu koodin
docstringiin.

Jos sääntö poistaisi lohkosta **jokaisen** rivin, rivit säilyvät ja lohko
sanoo sen ääneen. Tyhjä lohko lakkaisi kertomasta mitään, ja se on
vaimennuspäätös eikä karsinta. Paluu koskee vain kokonaan pudotettuja rivejä:
rivin lyhentäminen (säännöt 4 ja 5) ei voi tyhjentää lohkoa, joten sitä ei
peruta.

**Sääntö 4 rajaa kohteita eikä väitteitä.** Kohde on räjähdysalue, ja sama
kohde voi olla rivillä useammin kuin kerran -- eri heittoalueelta tai eri
aikaikkunassa (`[aggregate].utility_seconds_buckets`). Väitteitä rajaamalla
kaksi säilytettyä paikkaa voisi olla sama kohde kahdessa ikkunassa, jolloin
rivi menettäisi jokaisen eri kohteen samalla kun huomautus kutsuu niitä
kohteiksi. Säännöissä 4 ja 5 **yhtä yleiset havainnot säilyvät molemmat**:
rajan katkaiseminen tasatilanteen keskeltä pudottaisi kahdesta identtisen
otannan havainnosta toisen ja kutsuisi sitä harvinaisemmaksi.

**Miksi sääntö 3 on oletuksena pois.** Mitattu kaikista kahdeksasta demosta:

| Näytepiste | Pelaajia elossa | Näytepiste olemassa |
| --- | --- | --- |
| 6 s | 100 % | 354/354 kierrospuolta |
| 15 s | 94 % | 354/354 |
| 30 s | 72 % | 329/354 (93 %) |
| **45 s** | **53 %** | **285/354 (81 %)** |

45 s -rivi kuvaa noin puolta joukkuetta neljällä kierroksella viidestä. Se on
siis ohut ja vinoutunut -- se kertoo eloonjääneistä eikä asetelmasta -- mutta
se **ei ole toistoa** kuten säännöt 1, 2, 4 ja 5, ja Veetin analyyseissä on
myöhäisen kierroksen havaintoja ("mid lähti pian rotateen"). Asetus on
olemassa, oletus on säilyttää; rivin saa pois arvolla
`skip_sample_seconds = [45.0]`. Asetus **ei ole** sama asia kuin
`[parse].snapshot_seconds`: näytepiste pysyy taulussa ja `report.json`issa,
kyse on vain siitä tulostetaanko se. Sekunnit täsmätään siinä muodossa, jossa
ne ovat rivin nimiössä, joten `45` ja `45.0` tarkoittavat samaa riviä; lista
järjestetään latauksessa, koska järjestys ei muuta raporttia eikä siis saa
muuttaa parametrihashia.

> **Karsinta ei pakota mitään uudelleen paitsi renderöinnin.** Ei uutta
> taulua, ei skeemaversion nostoa, ei uudelleenparsintaa eikä
> -aggregointia -- yksi komento riittää:
>
> ```powershell
> uv run pappascout report --team <tunniste>
> ```
>
> `report` ei ohita itseään manifestin perusteella, joten säädetty asetus
> näkyy heti seuraavassa raportissa. Vaiheen parametrihash kattaa silti
> `[report]`-osion (ks. alla): manifesti kertoo, millä säännöillä kukin
> raportti kirjoitettiin.

Manifesti on **raporttikohtainen** (`<raportin nimi>.manifest.json`) ja
jäljitettävyyttä varten. Yhteinen manifesti kestäisi huonosti juuri sitä
rinnakkaisuutta, jonka varalta nimi varataan: kaksi yhtaikaista ajoa saisi
kumpikin oman raporttinsa, mutta viimeisenä kirjoittava manifesti jäisi voimaan
ja kuvaisi eri tiedostoa kuin se, jonka käyttäjä juuri sai.

Manifestin parametrihash lasketaan **raporttimallin sisällöstä** (`sha256`) ja
**`[report]`-osiosta kokonaisena**: mallin muokkaaminen muuttaa raporttia, ja
niin muuttaa karsintasäännön säätäminenkin -- sama kuvio kuin `parse`in
aseluokittelun tiivisteellä. Osio on hashissa kokonaisena eikä kenttä
kerrallaan samasta syystä kuin `[aggregate]` on `aggregate`ssa: luettelo
luetuista kentistä vanhenisi hiljaa. Ennen Story 2.13:a hash oli pelkkä mallin
tiiviste, koska vaihe ei lukenut yhtäkään asetusta; **asetusta ei voi lisätä
vaiheeseen, joka ei huomaa sen muuttumista**, ja se on Story 1.8:n vika, joka
on tässä projektissa löytynyt kolmesti.

Hash **ei kata** `render/view.py`:tä, joka valitsee jokaisen rivin ja
sanamuodon: sen muokkaaminen muuttaa raporttia näkymättä manifestissa
mitenkään, eli kahden raportin identtiset manifestit eivät todista niiden
syntyneen samasta koodista. Vanhentunut raportti ei silti pääse ulos, koska
vaihetta ei koskaan ohiteta manifestin perusteella -- jokainen ajo latoo
raportin uudelleen. Puute on kirjattu suunnittelun `deferred-work.md`:hyn,
joka asuu BMAD-tuotoksissa (`_bmad-output/implementation-artifacts/`) eikä
tässä repossa.

Väärä `schema_version` keskeyttää ajon eikä tuota puolikasta raporttia: versio
luetaan raa'asta JSONista ennen mallin validointia, jotta virheilmoitus kertoo
ajamaan `aggregate`n eikä nimeä yksittäistä kenttää.

### Miten kierrostyyppi ratkeaa

Kynnykset on kalibroitu 2026-08-29 ihmisen antamaa totuustaulua vasten
(15 kierrosta, katsottu 2D-replaynä). Kolme sääntöä ovat **sääntöjä, eivät
kynnyksiä** -- niitä ei viilata luvuilla:

1. **Säästö on aina reaktio häviöön.** Voitetun kierroksen jälkeen joukkue
   tekee normaalin oston. Voiton jälkeen ei siis koskaan `eco`, `force` eikä
   `half`.
2. **Force ja puoliosto eroavat taskuun jätetystä rahasta**, eivät
   varustearvosta. Force = ostettiin tyhjäksi. Puoliosto = ostettiin, mutta
   jätettiin varaa seuraavalle kierrokselle -- ja "varaa" mitataan
   **pelaajakohtaisesti**, ei joukkueen keskiarvosta.
3. **Säästetty ase ei ole ostos.** Ratkaisee tällä kierroksella ostettu summa,
   ei varustearvo -- eloon jääneiden kalusto nostaa varustearvoa ilman että
   mitään ostettiin.

Ensimmäinen osuva sääntö voittaa. Järjestys on: numeroidaanko kierros ->
puuttuuko havainto -> pistoolikierros -> jatkoaika -> laskiko varustearvo
(`anomaly`) -> täysi osto varustearvosta -> onko edellistä kierrosta
(`anomaly`) -> voiton jälkeen `full` (tai `anomaly`, jos varustearvo on liian
matala ollakseen osto) -> hävityn jälkeen näin:

```
varusteet >= full_equip_min                       -> full
ostettu < force_buy_min                           -> eco
ostettu >= force_buy_min:
    havainto puuttuu tai on ristiriitainen        -> ei luokitella
    ehto A ei täyty (liian harva aseistettu)      -> eco
    raha ei siirry seuraavalle kierrokselle       -> force
    ehto A täyttyy, ehto B ei                     -> force
    molemmat täyttyvät                            -> half
```

**Puolioston kaksi ehtoa (Story 1.10).** Käyttäjän määritelmä on
kaksisuuntainen -- *"puoliosto ei ole force silloin kun seuraavalla
kierroksella mahdollistetaan normaali osto, ja ei ole eco kun käytössä on
tarpeeksi arvoa"* -- ja molempien ehtojen on täytyttävä:

- **Ehto A, kalusto.** Vähintään `armed_players_min` pelaajalla oli panssari
  ja ase ostoajan lopussa. Erottaa puolioston **ecosta**.
- **Ehto B, ensi kierroksen varallisuus.** Vähintään
  `normal_buy_players_min` pelaajaa yltää arvoon `normal_buy_money_min`, kun
  omaan saldoon lisätään häviöbonus (`[economy].loss_bonus_steps`, indeksinä
  loss count) ja summa katkaistaan rahakattoon. Erottaa puolioston
  **forcesta**.

Ehto B lasketaan **pelaajakohtaisesta rahajakaumasta**
(`money_players_buy_end`), ei joukkuesummasta: joukkue jolla yhdellä on 5 000
ja neljällä nolla saa saman keskiarvon kuin joukkue jolla kaikilla on 1 000,
mutta edellisessä neljä viidestä ei voi ostaa mitään. Puoliajan viimeisellä
kierroksella ehtoa B ei lasketa lainkaan -- raha ei siirry
pistoolikierrokselle eikä jatkoajalle, joten sitä ei ole jätetty varaa varten.

Häviön haara on tyhjentävä, joten talouspäättelyyn ei jää poikkeamaksi
putoavaa väliä. `anomaly` on varattu tilanteille, joissa **havainto** on
ristiriitainen (varustearvo laski ostoaikana, edellistä kierrosta ei ole) tai
joissa voiton jälkeen ei ostettu käytännössä mitään. Puuttuva tai
ristiriitainen pelaajakohtainen havainto ei tuota `anomaly`a vaan jättää
kierroksen **luokittelematta**: syy kerrotaan, eikä luokkaa arvata. Jokainen
päätös kantaa suomenkielisen perustelun ja kaikki vertailuun käytetyt arvot,
joten sen voi tarkistaa demoa vasten.

Rahamäärät vertaillaan pyöristetyillä per pelaaja -arvoilla -- tasan niillä
luvuilla, jotka perustelu ja kierroslista näyttävät. Perustelu ei siis voi
sanoa "ostettu 1 500 $ eli alle 1 500 $". Ehdot A ja B eivät jaa mitään:
ne lasketaan suoraan pelaajakohtaisista havainnoista.

**Mikä on mitattu ja mikä ei.** `normal_buy_money_min` on kalibroitu kahta
havaittua kierrosta vasten (marginaali 350 $ alas, 200 $ ylös).
`armed_players_min` on **käyttäjän lausuma sääntö**, jota yksikään mitattu
kierros ei koettele, ja `normal_buy_players_min` on päättelyä: aineisto antaisi
saman tuloksen millä tahansa arvolla väliltä 1-5. `settings.toml` merkitsee
eron rivikohtaisesti (`[kalibroitu]`, `[lausuttu]`, `[päätelty]`).

Kuudessa demossa on 23 hävityn kierroksen jälkeistä ostokierrosta, ja
**poistunut kiinteä raja `force_money_left_max` antaisi niistä jokaiselle
saman luokan** kuin ehdot A ja B. Yksikään mitattu kierros ei siis vielä erota
uutta sääntöä vanhasta; ero näkyy vasta epätasaisella jakaumalla, ja se on
pinnattu käsin rakennetuilla testiriveillä (sama joukkuesumma, eri jakauma,
eri tuomio). Säännön peruste on käyttäjän oma määritelmä, joka on
pelaajakohtainen -- ei mittaus, joka olisi kumonnut edellisen säännön.

Oikeaa demoa vaativat testit on merkitty `@pytest.mark.demo`, ja ne ohittavat
itsensä siististi, jos 100-230 MB:n demoja ei ole koneella. Demot etsitään
oletuksena arkiston `import`-hakemistosta; `PAPPASCOUT_TEST_DEMOS` osoittaa ne
muualle.

Asetustiedosto etsitään työhakemistosta ylöspäin ja viimeisenä repon juuresta.
Kaksi ympäristömuuttujaa ohjaa ajoa muokkaamatta versioitua `settings.toml`:ia:

| Muuttuja | Vaikutus |
| --- | --- |
| `PAPPASCOUT_SETTINGS` | Käytettävä asetustiedosto. Puuttuva tiedosto on virhe. |
| `PAPPASCOUT_ARCHIVE_ROOT` | Arkiston juuri; ohittaa `[project] archive_root`in. |
| `PAPPASCOUT_TEST_DEMOS` | Hakemisto, josta `@pytest.mark.demo`-testit etsivät oikeat demot. |

`archive_root` saa sisältää ympäristömuuttujia (`%USERPROFILE%`) ja tilden, jotka
laajennetaan latausvaiheessa -- siksi sama rivi toimii molemmilla koneilla.

## Rakenne

| Polku | Sisältö |
| --- | --- |
| `settings.toml` | Kaikki numerot: `[project] [league] [parse] [thresholds] [aggregate] [report] [economy] [faceit]` |
| `src/pappascout/constants.py` | Jaetut enum-luettelot (kierrostyyppi, puoli, tila) |
| `src/pappascout/errors.py` | `PappascoutError` ja alaluokat |
| `src/pappascout/domain/schemas.py` | Polars-skeemat `ROUNDS`, `TICKS`, `EVENTS`, `LINEUPS`, `DEATHS`, `CALLOUT_CLOUD`, `CLASSIFIED` ja `validate()` |
| `src/pappascout/domain/models.py` | Typatut asetusosiot ja `load_settings()` |
| `src/pappascout/domain/rounds.py` | `mark_played_rounds()` -- ainoa paikka, joka päättää `round_no`:n -- ja `check_win_reasons()` |
| `src/pappascout/domain/economy.py` | `loss_counts()` ja `classify_round()` -- kierrostyypin talouspäättely |
| `src/pappascout/domain/sampling.py` | `sample_ticks()` ja `first_contact_tick()` -- näytepisteiden valinta ja ensikontaktin sääntö |
| `src/pappascout/domain/selection.py` | Rosterikynnys puhtaana logiikkana: `evaluate()` (kartan kokoonpano vs. vakirosteri joukko-operaationa, luokka `5/5`/`4/5`, aina luettava syy), `guaranteed_maps()` (montako karttaa BO_n:ssa pelataan varmasti) ja `class_labels()` (luokan nimi johdetaan kynnyksista) |
| `src/pappascout/domain/teams.py` | Joukkueen identiteetti puhtaana logiikkana: `build_teams()` (vakirosteri yhdisteenä, lähdetunnisteiden liittäminen rosterin perusteella, siirtyneet pelaajat), `find_teams()` (kirjainkoosta riippumaton nimihaku, monitulkintaisuus tuloksena) ja `is_steam_id64()` |
| `src/pappascout/domain/utility.py` | `grenade_endpoints()`, `build_point_cloud()` ja `nearest_cells()` -- lentoradan pelkistys kahteen pisteeseen ja räjähdysalueen johtaminen pistepilvestä |
| `src/pappascout/archive/paths.py` | Arkiston hakemistorakenne suhteellisina polkuina |
| `src/pappascout/archive/atomic_write.py` | Atominen kirjoitus (`*.tmp-<host>` -> `rename`) |
| `src/pappascout/archive/manifest.py` | `Manifest`-malli, `is_current()` ja vaiheiden ohitussopimus |
| `src/pappascout/adapters/protocols.py` | Portit, jotka vaiheet ottavat parametrina |
| `src/pappascout/adapters/decompress.py` | `.dem.zst`-purku ja `PBDEMS2`-otsikkotarkistus |
| `src/pappascout/adapters/demo_parser.py` | demoparser2-toteutus -- ainoa paikka, joka tuntee pelin propinimet |
| `src/pappascout/adapters/faceit.py` | FACEIT Data API -asiakas -- ainoa paikka, joka tekee HTTP-kutsuja: avain otsakkeesta, uudelleenyritys vain 429/5xx:lle (`Retry-After` huomioiden), aikabudjetti per kutsu, sivutus; vastausvälimuisti `raw/faceit/` **vain valmiille otteluille**, ei ottelulistalle |
| `src/pappascout/stages/discover.py` | `discover`-vaihe: divisioonan otteluista `index/matches.json` + `index/teams.json`; ei manifestia, koska vaihetta ei koskaan ohiteta. Sisältää myös indeksien lukijat: `read_indexes()`, `matches_from_index()`, `teams_from_index()` ja `resolve_team()` -- **rikkinäinen rivi kaataa ajon eikä katoa laskuriin** |
| `src/pappascout/stages/select.py` | `select`-vaihe: indekseista `index/selections/<team_key>.json`; ei porttia (ei verkkoa) eika manifestia. Sisaltaa valintatiedoston lukijan (`read_selection()`) |
| `src/pappascout/stages/parse.py` | `parse`-vaihe: demosta `rounds.parquet` + `ticks.parquet` + `events.parquet` + `lineups.parquet` + `deaths.parquet` + `callouts.parquet` + `match.parquet` + manifesti |
| `src/pappascout/stages/classify.py` | `classify`-vaihe: kierrostaulusta kierrostyypit, kierroslista + manifesti |
| `src/pappascout/domain/report.py` | `Report`-malli: `aggregate`- ja `render`-vaiheen jaettu sopimus, `Σ n = m` -tarkistus |
| `src/pappascout/domain/aggregate.py` | Jakaumat ja otannat puhtaina funktioina; `build_report()` |
| `src/pappascout/stages/aggregate.py` | `aggregate`-vaihe: luokitelluista kierroksista `aggregates/<team_key>/report.json` + manifesti |
| `src/pappascout/render/__init__.py` | `render_report()`, raporttimallin polku ja sen sisällön tiiviste |
| `src/pappascout/render/view.py` | Raportin näkymämalli: **mitä** raportissa sanotaan (valinta, ei laskenta) |
| `src/pappascout/render/report.md.j2` | Jinja2-malli: **miten** se sanotaan; muoto muuttuu koskematta koodiin |
| `src/pappascout/stages/render.py` | `render`-vaihe: `report.json`:sta aikaleimattu Markdown + manifesti |
| `src/pappascout/cli/` | Typer-komennot |

Riippuvuusnuoli on `cli -> stages -> {domain, adapters, archive, render}`,
`render -> domain` ja `adapters -> domain`; sääntöä valvoo
`tests/test_layering.py`. `render` ei näe arkistoa eikä adaptereita, joten
"render ei laske mitään" on rakenteellinen lupaus eikä tapa.

### Indeksitiedostojen kentät

`discover` kirjoittaa kaksi tiedostoa, ja **molemmilla on sama kirjoittaja**.
Muut vaiheet lukevat ne `stages.discover.read_indexes()`illa, joka tarkistaa
`schema_version`in ja sen, että tiedostot ovat samasta ajosta.

`index/matches.json`

| Kenttä | Sisältö |
| --- | --- |
| `schema_version` | Muodon versio; lukija kaatuu tuntemattomaan |
| `generated_at` | Ajon hetki UTC:nä; **sama molemmissa tiedostoissa** |
| `competition_ids` | Mistä kilpailuista ottelut haettiin |
| `matches[]` | `match_id`, `competition_id`, `status`, `played`, `scheduled_at`, `started_at`, `finished_at`, `map_picks`, `best_of` |
| `matches[].best_of` | Ottelun pituus karttoina, tai `null` jos lähde ei kertonut. **Ei sama luku kuin `map_picks`in pituus**: 2-0 päättyneessä BO3:ssa vedossa on kolme karttaa mutta demoja kaksi. Puuttuva arvo on kelvollinen havainto, joten kentän lisääminen ei nostanut `schema_version`ia |
| `matches[].teams[]` | `faction_id` (lähteen tunniste, **ei** kanoninen `team_key`), `name`, `roster` ja `substitutes` SteamID64-listoina |

`index/teams.json`

| Kenttä | Sisältö |
| --- | --- |
| `contested_lineup_keys` | Kokoonpanotiivisteet, jotka useampi joukkue omistaa -- jottei jatkovaihe laskisi niitä kahdesti |
| `teams[].team_key` | **Kanoninen tunniste**: varhaisimman havainnon `faction_id`. Ei muutu, kun uusi kausi tuo uuden tunnisteen |
| `teams[].faction_ids` | Kaikki lähteen tunnisteet, jotka tunnistettiin tähän joukkueeseen |
| `teams[].name`, `alternative_names` | Useimmin havaittu nimi ja muut havainnot |
| `teams[].lineup_keys` | Silta arkistoon: `aggregates/<lineup_key>`-hakemistot. **Ei identiteetti** |
| `teams[].match_ids`, `played_match_ids` | Tunnistelistat aikajärjestyksessä, samassa järjestyksessä kuin `matches.json` |
| `teams[].roster[]` | `game_player_id` (SteamID64, ainoa avain), `nickname`, `player_id` (FACEITin UUID), `alternative_nicknames` |
| `teams[].released[]` | Pelaajat, jotka havaittiin tässä joukkueessa mutta myöhemmin toisessa -- eivät rosterissa, eivät myöskään kadonneet |
| `teams[].shared_players` | Pelaajat, jotka toinen joukkue havaitsi yhtä myöhään; yhä rosterissa, koska kiistaa ei ratkaista arpomalla |

`index/selections/<team_key>.json` -- kirjoittaa vain `select`, lukee
`stages.select.read_selection()`, joka tarkistaa `schema_version`in.

| Kenttä | Sisältö |
| --- | --- |
| `roster_size`, `roster_min_regulars` | Käytetyt kynnykset **arvoina**, jotta päätös on tarkistettavissa vaikka asetus muuttuisi |
| `roster` | Se vakirosteri, jota vasten jokainen rivi ratkaistiin |
| `index_generated_at` | Minkä ottelulistan perusteella rivit syntyivät |
| `counts` | `map_demos`, `accepted`, `rejected`, `league`, `observed`, `predicted`, `drifted`, `uncertain` ja luokat. `accepted + rejected == map_demos`, ja `class_5/5 + class_4/5 == accepted` |
| `selections[].map_demo_id` | `{match_id}-{map_index}`, 0-pohjainen -- sama tunniste kuin `parse`illa |
| `selections[].roster_ok` | Kelpaako kartta otantaan |
| `selections[].roster_reason` | **Aina luettava syy**, myös hyväksytyllä rivillä: luvut, kynnys, ulkopuoliset nimimerkillä ja lähde |
| `selections[].roster_class` | `5/5` tai `4/5`; `null` vain hylätyllä rivillä. Hyväksytty rivi ilman luokkaa on mahdoton rakentaa |
| `selections[].roster_source` | `observed` (demosta) tai `predicted` (ottelurosterista) |
| `selections[].certainly_played` | Tiedetäänkö kartta pelatuksi. Ottelun pituus takaa sen **tai** parsittu demo todistaa |
| `selections[].is_league` | `competition_id` vs. `[league].championship_ids` -- **ei nimestä** |
| `selections[].regulars`, `outsiders` | Kartan pelaajat jaettuna vakirosteriin ja sen ulkopuolisiin |
| `selections[].joined`, `left` | Havainnon ja ottelurosterin ero: vaihto karttojen välissä. Tyhjä, kun vertailtavaa ei ole |

**Identiteetti on rosteri, tunniste on vain avain.** Kaksi `faction_id`:tä
yhdistetään samaksi joukkueeksi, kun niiden rosterit jakavat vähintään
`[thresholds].team_identity_min_common` pelaajaa -- sama kynnys ja sama sääntö
kuin kokoonpanotiivisteiden liittämisessä. Ilman sitä uusi kausi antaisi samalle
porukalle uuden tunnisteen eikä mikään yhdistäisi niitä.

### FACEIT-välimuisti on eriytetty kutsun lajin mukaan

Vastaukset tallentuvat arkiston hakemistoon `raw/faceit/`, mutta **eivät
kaikki**. Sääntö on kaksiosainen, ja se perustuu mittaukseen:

| Kutsu | Välimuisti | Miksi |
| --- | --- | --- |
| Ottelulista (`/championships/{id}/matches`) | **Ei lainkaan** | Yksi kutsu per ajo, mutta muuttuu jatkuvasti: mitattuna 60 ottelua 66:sta oli `SCHEDULED`. Välimuisti säästäisi yhden kutsun ja maksaisi oikeellisuuden. |
| Ottelun tiedot (`/matches/{id}`) | **Pysyvästi, vain kun `FINISHED`** | Jopa 66 kutsua per ajo, ja pelatun ottelun tiedot eivät enää muutu. |

Sama ehto koskee **lukemista**, eli välimuisti on **itsekorjaava**: jos levyltä
luettu ottelu ei ole `FINISHED`, tiedosto poistetaan ja vastaus haetaan
verkosta. Vanhan version tai bugin kirjoittama tiedosto ei siis voi jäädä
tarjoilemaan vanhentunutta vastausta hiljaa, eikä hakemistoa tarvitse siivota
käsin missään tilanteessa.

Käytännön seuraus on se, joka kannattaa tietää:

> **Uudet ja siirretyt ottelut näkyvät heti seuraavassa ajossa.** Hakemistoa ei
> tarvitse tyhjentää käsin koskaan, eikä vanhenemisaikaa ole olemassa.

Keskeneräistä (`SCHEDULED`, `ONGOING`) tai peruttua (`CANCELLED`) ottelua ei
kirjoiteta levylle. `CANCELLED` on tarkoituksella ulkona vaikka se näyttää
lopulliselta: peruminen on järjestäjän päätös, jonka järjestäjä voi perua, ja
siirretty ottelu pelataan samalla `match_id`:llä. Yhden säästetyn kutsun
hinnalla menetettäisiin pelatun ottelun demo pysyvästi -- FACEIT säilyttää
demot noin 30 päivää.

Hakemiston saa poistaa milloin tahansa, mutta sitä ei tarvitse: manifestia ei
ole eikä poisto vaikuta muihin vaiheisiin, joten ainoa seuraus on uusi kutsu.

Koodirepo on tarkoituksella OneDriven ulkopuolella (`C:\Users\vpu\dev\pappascout`)
ja synkronoituu koneiden välillä GitHubin kautta -- git ja OneDrive eivät toimi
yhdessä. Arkisto sen sijaan pysyy OneDrivessa ja on molempien koneiden yhteinen.

Suunnitteludokumentit (PRD, arkkitehtuuri, storyt) ovat erillisessä
`oma cs projekti` -hakemistossa OneDrivessa.
