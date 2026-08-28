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
uv run pappascout parse <tiedosto|map_demo_id>   # demosta kierrostaulu
uv run pappascout classify <map_demo_id> --team <tunniste> --show  # kierrostyypit
uv run pappascout classify <map_demo_id> --kaikki-joukkueet        # molemmat joukkueet
uv run pappascout --version
uv run pytest                   # testit
uv run pytest -m "not demo"     # vain demoista riippumattomat testit
```

`parse` lukee `.dem`- tai `.dem.zst`-tiedoston ja kirjoittaa arkistoon
`parsed/<map_demo_id>/rounds.parquet`-taulun: kaksi riviä jokaista pelattua
kierrosta kohden, yksi kummallekin joukkueelle. Ilman polkua annettu tunniste
etsitään arkiston `demos/`- ja `import/`-hakemistoista. Toisella ajolla vaihe
ohitetaan, jos manifesti täsmää -- `[thresholds]`-arvon muuttaminen **ei**
aiheuta uudelleenparsintaa.

Warmup, puukkokierros ja uudelleenkäynnistykset eivät ole pelattuja kierroksia
eivätkä päädy tauluun. `round_raw` on demoparser2:n `round_end`-tapahtuman oma
kierrosnumero, joten ohitetut kierrokset näkyvät siinä aukkona: Ancientilla
`round_no` 1..21 vastaa `round_raw`-arvoja 2..22.

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
on vastaavasti erotus `equip_freeze_end - equip_round_start`, ja se on se
havainto, joka erottaa forcen ecosta.

Kierros, jota mikään sääntö ei kata, on `anomaly` perusteluineen -- ei
"luultavasti eco". Ne ovat tarkalleen ne kierrokset, joille Story 1.4 hakee
rajan.

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
| `src/pappascout/archive/paths.py` | Arkiston hakemistorakenne suhteellisina polkuina |
| `src/pappascout/archive/atomic_write.py` | Atominen kirjoitus (`*.tmp-<host>` -> `rename`) |
| `src/pappascout/archive/manifest.py` | `Manifest`-malli, `is_current()` ja vaiheiden ohitussopimus |
| `src/pappascout/adapters/protocols.py` | Portit, jotka vaiheet ottavat parametrina |
| `src/pappascout/adapters/decompress.py` | `.dem.zst`-purku ja `PBDEMS2`-otsikkotarkistus |
| `src/pappascout/adapters/demo_parser.py` | demoparser2-toteutus -- ainoa paikka, joka tuntee pelin propinimet |
| `src/pappascout/stages/parse.py` | `parse`-vaihe: demosta `rounds.parquet` + manifesti |
| `src/pappascout/stages/classify.py` | `classify`-vaihe: kierrostaulusta kierrostyypit, kierroslista + manifesti |
| `src/pappascout/cli/` | Typer-komennot |

Riippuvuusnuoli on `cli -> stages -> {domain, adapters, archive}` ja
`adapters -> domain`; sääntöä valvoo `tests/test_layering.py`.

Koodirepo on tarkoituksella OneDriven ulkopuolella (`C:\Users\vpu\dev\pappascout`)
ja synkronoituu koneiden välillä GitHubin kautta -- git ja OneDrive eivät toimi
yhdessä. Arkisto sen sijaan pysyy OneDrivessa ja on molempien koneiden yhteinen.

Suunnitteludokumentit (PRD, arkkitehtuuri, storyt) ovat erillisessä
`oma cs projekti` -hakemistossa OneDrivessa.
