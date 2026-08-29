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
  ja räjähdys ovat kaksi riviä, jotka yhdistää `(round_no,
  grenade_entity_id)`. Utility mitataan **heitoista, ei ostoista**, ja se
  luetaan lentoradoista -- `grenade_thrown`-tapahtumaa ei ole olemassa.
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

Raha on kierrostaulussa kahtena lukuna. `money_freeze_end` on **jäljelle
jäänyt** saldo ostoajan jälkeen -- säästökierroksella se on suuri ja täydellä
ostolla pieni -- ja `money_spent` on kierroksella käytetty raha. Näiden summa
on se raha, joka joukkueella oli ostoaikana käytettävissä. Ostettu varustemäärä
on vastaavasti erotus `equip_freeze_end - equip_round_start`.

Kaluston **jakauma** on omana havaintonaan: `players_armed_freeze_end` kertoo,
monellako pelaajalla oli freezetimen lopussa **panssari ja vähintään yksi ase
hallussa**. Se on käyttäjän oma määritelmä ("kevlar ja jokin parannettu ase --
parempi pistooli, SMG tai halpa kivääri") suoraan mitattuna: ase luetaan
pelaajan tavaraluettelosta ja panssari `m_ArmorValue`-propista. Joukkuesumma ei
kerro tätä -- kaksi AK:ta ja kolme tyhjää antaa saman summan kuin viisi
puolinaista. Luku on aina väliltä `0`-`players_freeze_end`; `0` on havainto ja
tarkoittaa **"kukaan ei ollut aseistettu"**, ja `null` tarkoittaa, ettei
havaintoa saatu.

Kevlar ilman asetta ei riitä eikä ase ilman kevlaria. Kypärää ei vaadita: CT
ostaa usein pelkän kevlarin, koska AK tappaa päähän kypärästä huolimatta.
Kranaatit, C4 ja Zeus eivät ole aseita. Luokittelu ei vielä muuta kierrostyypin
päättelyä: puolioston sääntö odottaa aineistoon ensimmäistä kiistatonta
puoliostoa, jota vasten sen voi kalibroida.

> **Hallussapito, ei ostos.** Tavaraluettelo luetaan freezetimen lopusta, joten
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
saataisiin lopuista. Pelaaja pysyy `players_freeze_end`in jakajassa, joten
`4/5` väittäisi, että yksi oli aseeton, vaikka totuus on ettei häntä saatu
luettua: vaiettu lukuvirhe näyttäisi säästökierrokselta. Tyhjä tavaraluettelo
ja `0` panssaria ovat sen sijaan **havaintoja** eivätkä puutteita.

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

### Miten kierrostyyppi ratkeaa

Kynnykset on kalibroitu 2026-08-29 ihmisen antamaa totuustaulua vasten
(15 kierrosta, katsottu 2D-replaynä). Kolme sääntöä ovat **sääntöjä, eivät
kynnyksiä** -- niitä ei viilata luvuilla:

1. **Säästö on aina reaktio häviöön.** Voitetun kierroksen jälkeen joukkue
   tekee normaalin oston. Voiton jälkeen ei siis koskaan `eco`, `force` eikä
   `half`.
2. **Force ja puoliosto eroavat taskuun jätetystä rahasta**, eivät
   varustearvosta. Force = ostettiin tyhjäksi. Puoliosto = ostettiin, mutta
   jätettiin varaa seuraavalle kierrokselle.
3. **Säästetty ase ei ole ostos.** Ratkaisee tällä kierroksella ostettu summa,
   ei varustearvo -- eloon jääneiden kalusto nostaa varustearvoa ilman että
   mitään ostettiin.

Ensimmäinen osuva sääntö voittaa. Järjestys on: numeroidaanko kierros ->
puuttuuko havainto -> pistoolikierros -> jatkoaika -> laskiko varustearvo
(`anomaly`) -> täysi osto varustearvosta -> onko edellistä kierrosta
(`anomaly`) -> voiton jälkeen `full` (tai `anomaly`, jos varustearvo on liian
matala ollakseen osto) -> hävityn jälkeen nämä neljä riviä, kaikki per pelaaja:

```
varusteet >= full_equip_min                                    -> full
ostettu >= force_buy_min ja jäljellä <= force_money_left_max    -> force
ostettu >= force_buy_min                                       -> half
muuten                                                         -> eco
```

Häviön haara on tyhjentävä, joten talouspäättelyyn ei jää poikkeamaksi
putoavaa väliä. `anomaly` on varattu tilanteille, joissa **havainto** on
ristiriitainen (varustearvo laski ostoaikana, edellistä kierrosta ei ole) tai
joissa voiton jälkeen ei ostettu käytännössä mitään. Jokainen päätös kantaa
suomenkielisen perustelun ja kaikki vertailuun käytetyt arvot, joten sen voi
tarkistaa demoa vasten.

Kaikki vertailut tehdään pyöristetyillä per pelaaja -arvoilla -- tasan niillä
luvuilla, jotka perustelu ja kierroslista näyttävät. Perustelu ei siis voi
sanoa "jäi 1 000 $ eli yli 1 000 $".

`force_money_left_max` on näistä kynnyksistä ainoa, joka nojaa päättelyyn eikä
havaintoon: kalibrointiaineistossa ei ole yhtäkään kiistatonta puoliostoa,
joten rajan yläpuolta ei ole nähty. Se säädetään uudelleen, kun ensimmäinen
sellainen kierros tulee vastaan.

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
| `settings.toml` | Kaikki numerot: `[project] [league] [parse] [thresholds] [economy]` |
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
| `src/pappascout/cli/` | Typer-komennot |

Riippuvuusnuoli on `cli -> stages -> {domain, adapters, archive}` ja
`adapters -> domain`; sääntöä valvoo `tests/test_layering.py`.

Koodirepo on tarkoituksella OneDriven ulkopuolella (`C:\Users\vpu\dev\pappascout`)
ja synkronoituu koneiden välillä GitHubin kautta -- git ja OneDrive eivät toimi
yhdessä. Arkisto sen sijaan pysyy OneDrivessa ja on molempien koneiden yhteinen.

Suunnitteludokumentit (PRD, arkkitehtuuri, storyt) ovat erillisessä
`oma cs projekti` -hakemistossa OneDrivessa.
