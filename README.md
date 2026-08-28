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
uv run pappascout --version
uv run pytest                   # testit
```

Asetustiedosto etsitään työhakemistosta ylöspäin ja viimeisenä repon juuresta.
Kaksi ympäristömuuttujaa ohjaa ajoa muokkaamatta versioitua `settings.toml`:ia:

| Muuttuja | Vaikutus |
| --- | --- |
| `PAPPASCOUT_SETTINGS` | Käytettävä asetustiedosto. Puuttuva tiedosto on virhe. |
| `PAPPASCOUT_ARCHIVE_ROOT` | Arkiston juuri; ohittaa `[project] archive_root`in. |

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
| `src/pappascout/archive/paths.py` | Arkiston hakemistorakenne suhteellisina polkuina |
| `src/pappascout/archive/atomic_write.py` | Atominen kirjoitus (`*.tmp-<host>` -> `rename`) |
| `src/pappascout/archive/manifest.py` | `Manifest`-malli, `is_current()` ja vaiheiden ohitussopimus |
| `src/pappascout/cli/` | Typer-komennot |

Koodirepo on tarkoituksella OneDriven ulkopuolella (`C:\Users\vpu\dev\pappascout`)
ja synkronoituu koneiden välillä GitHubin kautta -- git ja OneDrive eivät toimi
yhdessä. Arkisto sen sijaan pysyy OneDrivessa ja on molempien koneiden yhteinen.

Suunnitteludokumentit (PRD, arkkitehtuuri, storyt) ovat erillisessä
`oma cs projekti` -hakemistossa OneDrivessa.
