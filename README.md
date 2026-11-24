# FloodNowcast Europe

Real-time European hydrological impact & flash-flood nowcasting portal combining:
- **FlashNet** convective nowcasting (0–3 h, 10-min cadence, ~1.3 km) & **OPERA** observed radar accumulations (−12 h)
- **HydroSHEDS / HydroBASINS** Europe (Levels 7, 8, 9)
- **Poschlod et al. (2021)** European 10-year sub-daily return levels + **De Michele et al. (2001)** IDAF areal reduction + **Gradex** scaling
- **Vigicrues** official French river vigilance overlay

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```

Then open `http://localhost:8080`.

## API endpoints

- `GET /` — MapLibre GL JS interactive map
- `GET /api/health` — Health check
- `GET /api/basins/geometry/{level}` — Simplified HydroBASINS GeoJSON (`7`, `8`, or `9`)
- `GET /api/flood/summary` — Alert counts & active basins
- `GET /api/flood/basins?level=8` — All basin metrics for a level
- `GET /api/vigicrues` — Official Vigicrues GeoJSON
