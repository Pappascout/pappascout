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
uv run pappascout parse <tiedosto|map_demo_id>   # demosta kierrokset ja asetelmat
uv run pappascout classify <map_demo_id> --team <tunniste> --show  # kierrostyypit
uv run pappascout classify <map_demo_id> --kaikki-joukkueet        # molemmat joukkueet
uv run pappascout aggregate --team <tunniste>                      # report.json
uv run pappascout report --team <tunniste>                         # Markdown-raportti
uv run pappascout --version
uv run pytest                   # testit
uv run pytest -m "not demo"     # vain demoista riippumattomat testit
```

`parse` lukee `.dem`- tai `.dem.zst`-tiedoston ja kirjoittaa arkistoon kolme
taulua yhdellä lukukerralla:

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
  tickiltä. Räjähdyksellä ei ole omaa aluenimeä, joten sen alue johdetaan
  lähimmän elossa olevan pelaajan alueesta, jos hän on enintään
  `[parse].area_snap_units`in päässä; muuten `area` jää tyhjäksi mutta
  koordinaatit tallentuvat. `area_source` (`observed` / `snapped`) erottaa nämä
  kaksi ja `snap_distance` kertoo arvion etäisyyden, jotta raportti voi
  myöhemmin sanoa "3 savua Rampille (2 varmaa)". Tyhjä taulu on kelvollinen
  tulos: utility voi aidosti puuttua.

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

`aggregate` lukee joukkueen luokitellut kierrokset ja niiden näytepiste- ja
tapahtumataulut ja kirjoittaa **yhden tiedoston**:
`aggregates/<team_key>/report.json` sekä sen manifestin. Demoa ei lueta, joten
ajo valmistuu sekunneissa. Tiedosto on pydantic-malli
`domain.report.Report`, ja se on `aggregate`-vaiheen ja tulevan
`render`-vaiheen **jaettu sopimus**: `render` ei laske mitään, vaan kaikki
luvut ovat valmiina.

Rakenne on `maps[] -> sides[] -> round_types[]`, ja jokaisella tasolla on
otanta. Kierrostyypin alla on viisi havaintoa:

| Kenttä | Mihin se vastaa |
| --- | --- |
| `positions[].areas[].players_dist[]` | *"3A ja 2B"*, *"2-ramp"* -- pelaajamäärä alueittain näytepisteessä |
| `utility[]` | *"T-spawnista CT-savu B sitelle"*, *"insta mid talo savu"* |
| `utility_counts[]` | *"2 savua 2 valoo"* -- montako heitettiin kierroksella |
| `players_armed` | monellako oli panssari ja ase ostoajan lopussa |
| `first_contact[]` | *"otti kontaktin partsi käytävällä"* |

**Jokainen väite kantaa otantansa.** `n` = kierrokset, joissa havainto tehtiin;
`m` = kyseisen puolen ja kierrostyypin kierrokset, joilta havainto oli
luettavissa. `players_dist` sisältää myös arvon `0` (alue oli tyhjä), joten
`n`-arvojen summa yhden alueen yli on aina `m`. Se on tarkistus eikä koriste:
malli itse nostaa `AggregateError`in, jos summa ei täsmää -- silloin kierros on
kadonnut liitoksessa, ja raportti näyttäisi oikealta mutta väittäisi väärää
otantaa. Sama tarkistus tehdään myös **tasojen välillä**: kierrostyyppien summa
on puolen otanta, puolien summa kartan ja karttojen summa koko raportin.

**`first_contact[]` on tarkoituksella poikkeus tähän sääntöön.** Se laskee
läsnäoloa eikä pelaajamäärää: sama kierros tuottaa havainnon jokaiselle
alueelle, jolla joukkueella oli elossa oleva pelaaja sillä hetkellä, joten
`n`-arvojen summa on suurempi kuin `m`. Täysi jakauma samalta hetkeltä on
`positions`-listan `first_contact`-näytepisteessä, ja sitä `Σ n = m` koskee
normaalisti.

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

**Kartan nimi on johdettu.** Sitä ei ole yhdessäkään taulussa, joten se
päätellään `map_demo_id`:stä karttapoolia vasten (`Ancient_vs_kaljukostaja` ->
`de_ancient`). `map_name_source` kertoo onnistuiko päättely; tuntematon kartta
jää omaksi haarakseen tunnisteensa nimellä eikä sulaudu toiseen.

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

Aggregoinnin parametrihash lasketaan koko `[aggregate]`-osiosta sekä niistä
`[thresholds]`- ja `[league]`-avaimista, jotka vaihe todella lukee
(`small_sample_rounds`, `team_identity_min_common`, `map_pool`). Muun
kynnysarvon muuttaminen ei siis mitätöi raporttia -- se ajaa `classify`n
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
demot, luokittelemattomat kierrokset, käytetyt kynnykset) -> kartta -> puoli ->
kierrostyyppi -> kierrosliite -> lukuohje. Jokainen havaintorivi kantaa
otantansa muodossa `(4/7 kierroksesta)`; ilman sitä yksi kierros näyttäisi
kuviolta.

Säästökierrokset (`pistol`, `eco`, `force`, `half`) kuvataan kierroksen
tarkkuudella: jokainen havainto kirjoitetaan. Täydet ostot (`full`) ja
jatkoaika (`ot`) kuvataan **vain toistuvina kuvioina**, ja toistumisen raja
luetaan raportista (`thresholds_used.thresholds.small_sample_rounds`) -- sitä
ei keksitä renderöinnissä. Pois jätettyjen havaintojen määrä kirjoitetaan
näkyviin, joten suodatus ei ole hiljainen.

**Vanha raportti ei koskaan ylikirjoitu.** Nimessä on aikaleima minuutin
tarkkuudella, ja saman minuutin sisällä ajetut saavat nollatäytetyn päätteen
`-02`, `-03` (täyttö on lajittelua varten: ilman sitä listaus järjestäisi
`-10, -100, -11, -2`). Nimi varataan atomisesti (`O_CREAT | O_EXCL`) ennen
kirjoitusta, joten kaksi rinnakkaista ajoa ei voi valita samaa nimeä; jos
kirjoitus epäonnistuu, varaus perutaan eikä hakemistoon jää tyhjää `.md`:tä.
Siksi komennossa ei ole `--pakota`-valintaa eikä vaihetta koskaan ohiteta
manifestin perusteella: käyttäjä ajaa komennon silloin kun hän haluaa
raportin.

**Joukkueen ja pelaajien nimiä ei ole.** `display_name` on toistaiseksi sama
kuin joukkuetunniste ja rosteri on SteamID64-numeroita, koska nimet ovat
demossa (`team_clan_name`, pelaajan `name`) muttei missään parsitussa
taulussa. Raportti **sanoo sen ääneen** eikä toista tiivistettä nimen paikalla:
otsikko on "Scouting-raportti -- joukkueen nimi ei tiedossa". Korjaus on
parsinnan muutos ja oma tarinansa; se on kirjattu `deferred-work.md`:hen.

Manifesti on **raporttikohtainen** (`<raportin nimi>.manifest.json`) ja
jäljitettävyyttä varten. Yhteinen manifesti kestäisi huonosti juuri sitä
rinnakkaisuutta, jonka varalta nimi varataan: kaksi yhtaikaista ajoa saisi
kumpikin oman raporttinsa, mutta viimeisenä kirjoittava manifesti jäisi voimaan
ja kuvaisi eri tiedostoa kuin se, jonka käyttäjä juuri sai.

Manifestin parametrihash lasketaan **raporttimallin sisällöstä** (`sha256`),
koska `render` ei lue yhtään asetusosiota mutta mallin muokkaaminen muuttaa
raporttia -- sama kuvio kuin `parse`in aseluokittelun tiivisteellä.

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
| `settings.toml` | Kaikki numerot: `[project] [league] [parse] [thresholds] [aggregate] [economy]` |
| `src/pappascout/constants.py` | Jaetut enum-luettelot (kierrostyyppi, puoli, tila) |
| `src/pappascout/errors.py` | `PappascoutError` ja alaluokat |
| `src/pappascout/domain/schemas.py` | Polars-skeemat `ROUNDS`, `TICKS`, `EVENTS`, `CLASSIFIED` ja `validate()` |
| `src/pappascout/domain/models.py` | Typatut asetusosiot ja `load_settings()` |
| `src/pappascout/domain/rounds.py` | `mark_played_rounds()` -- ainoa paikka, joka päättää `round_no`:n -- ja `check_win_reasons()` |
| `src/pappascout/domain/economy.py` | `loss_counts()` ja `classify_round()` -- kierrostyypin talouspäättely |
| `src/pappascout/domain/sampling.py` | `sample_ticks()` ja `first_contact_tick()` -- näytepisteiden valinta ja ensikontaktin sääntö |
| `src/pappascout/domain/utility.py` | `grenade_endpoints()` ja `snap_area()` -- lentoradan pelkistys kahteen pisteeseen ja alueen johtaminen koordinaateista |
| `src/pappascout/archive/paths.py` | Arkiston hakemistorakenne suhteellisina polkuina |
| `src/pappascout/archive/atomic_write.py` | Atominen kirjoitus (`*.tmp-<host>` -> `rename`) |
| `src/pappascout/archive/manifest.py` | `Manifest`-malli, `is_current()` ja vaiheiden ohitussopimus |
| `src/pappascout/adapters/protocols.py` | Portit, jotka vaiheet ottavat parametrina |
| `src/pappascout/adapters/decompress.py` | `.dem.zst`-purku ja `PBDEMS2`-otsikkotarkistus |
| `src/pappascout/adapters/demo_parser.py` | demoparser2-toteutus -- ainoa paikka, joka tuntee pelin propinimet |
| `src/pappascout/stages/parse.py` | `parse`-vaihe: demosta `rounds.parquet` + `ticks.parquet` + `events.parquet` + manifesti |
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

Koodirepo on tarkoituksella OneDriven ulkopuolella (`C:\Users\vpu\dev\pappascout`)
ja synkronoituu koneiden välillä GitHubin kautta -- git ja OneDrive eivät toimi
yhdessä. Arkisto sen sijaan pysyy OneDrivessa ja on molempien koneiden yhteinen.

Suunnitteludokumentit (PRD, arkkitehtuuri, storyt) ovat erillisessä
`oma cs projekti` -hakemistossa OneDrivessa.
