# Pizza Karma market research skill

Type a city, get an A-Z market report: competition, population and neighborhoods, events and seasons, private demand, schools, weather, sports and lunch crowds, licensing and fees, and the local truck community. Each figure is labeled A (official/opened), B, or C (snippet), and gaps say where to get the data.

## Quick install (Mac and Linux, one line)

    mkdir -p ~/.claude/skills/pizza-karma-market-research && curl -fsSL https://github.com/himaltrivedi/pizza-karma-market-research/archive/refs/heads/main.tar.gz | tar -xz --strip-components=1 -C ~/.claude/skills/pizza-karma-market-research

Run the same line again any time to update to the latest version. Windows users: use Git Bash or WSL for the line above, or download the ZIP from the green Code button and unzip it into `%USERPROFILE%\.claude\skills\pizza-karma-market-research\`.

Then finish the setup below (step 4, the free Census key, is required), restart Claude Code, and run `/pizza-karma-market-research City, ST`.

## Setup details (once)
1. Install Claude Code and sign in.
2. Copy this whole folder to `~/.claude/skills/pizza-karma-market-research/` (or keep it in a project's `.claude/skills/`). Windows: `%USERPROFILE%\.claude\skills\`.
3. Python 3.9+ is needed (no packages to install). Check: `python3 --version` (Windows: `python --version` or `py -3 --version`; the skill tries all three).
4. **Get a free Census API key** (instant, by email): https://api.census.gov/data/key_signup.html. The Census API no longer answers keyless requests, so without it population, income, age, density and Asian Indian counts are missing. Then either `export CENSUS_API_KEY=yourkey` (Windows: `setx CENSUS_API_KEY yourkey`) or save the key alone in a file named `.census_key` in your home folder.
5. Optional but recommended: `pip install pypdf` (lets the agent read PDF fee schedules that the web fetch tool hands back as raw files; without it a basic built-in reader handles only simple PDFs).
6. Test the data sources: `python3 scripts/pull_data.py --selftest` (run from this folder). Census, Gazetteer and NOAA should say OK. If one says FAIL, the report will be thinner for that part and the agent will say so.

## Run
Open a terminal in a folder where you want reports saved, start `claude`, then type:

    /pizza-karma-market-research Chicago, IL

It pulls data, researches, and writes `reports/chicago-il/report.html` (plus `data/` and `sections/`). The run takes a while (30-90 minutes); it saves each module as it goes. If it stops, run the same command again and it resumes.

## What the data pull gives it (reports/<slug>/data/)
- Census ACS: metro, core places, all places and counties per state (with margins of error), plus land area for density.
- NOAA 1991-2020 monthly normals for one airport station (the most complete of the nearest four).
- OpenStreetMap counts (`poi_counts_osm.csv`) of breweries, bars, restaurants, Indian and vegetarian restaurants, colleges and Hindu temples around each core place: low-confidence proxies for the ranking. Slow and the public service is sometimes busy, so it is a separate step: `pull_data.py "Chicago, IL" --poi-only` (the skill runs it in the background).
- Events and demand (`pull_events.py`): MLB home games, airport passengers by month, Census business counts (caterers, bars, hotels, promoters), college enrollment, high-school seniors, the city's own open-data event datasets, and a year of ticketed events from Ticketmaster if you add a free key (`TICKETMASTER_API_KEY` or `~/.ticketmaster_key`, https://developer.ticketmaster.com/). `event_inventory.py` keeps the agent's event table, shows which months are still thin, and builds the calendar and demand tables.
- Schools and colleges for the metro (`*_metro.csv`, filtered by Census metro code) and `metro_counties.csv`.
- City license data (Chicago built in; other cities with `--socrata-domain/--socrata-dataset/--socrata-field/--socrata-search`).
- `MANIFEST.md` lists what was pulled and what failed.

## Tools for the agent
- `scripts/pdf_text.py FILE.pdf [--grep WORD]` turns a PDF already saved on disk into text so the agent can quote it (local file only).
- `scripts/data_tool.py` prints compact views of the big CSVs (`columns`, `head`, `filter`, `count-by`, `sum-by`, `summary`) so the agent does not read whole files.
- `scripts/build_report.py reports/<slug>` joins `sections/*.md` into `report.html` and lints it (placeholders, brief length, brief numbers missing from the body with rounding allowed, section file names, missing confidence table, past dates, revenue words, fee rows without a quote).

## Tests
From this folder: `python3 -I -m unittest discover -s tests -v` (offline; uses fixtures from a real Chicago pull).

## Files
- `SKILL.md` rules and workflow
- `modules.md` what each module must cover
- `report-format.md` output layout and checklist
- `scripts/pull_data.py`, `scripts/data_tool.py`, `scripts/build_report.py`
- `tests/` offline tests and fixtures

## License
MIT, see `LICENSE`. Reports are research aids: figures labeled C come from search summaries and fees must be confirmed with the issuing agency.
