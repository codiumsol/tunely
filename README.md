# Tunely Radio – public data

Public files for the **Tunely Radio** Android app ("Your home radio, anywhere").
The app's code is kept private; this repo only holds what has to be public:

- `data/stations.json` – the station list the app downloads. Built every night by
  `scripts/update_stations.py` (GitHub Actions): stations from the free
  [Radio Browser](https://www.radio-browser.info) directory plus the official streams in
  `data/extra_stations.json` (e.g. Radio Pakistan), and **every stream is tested** – dead
  ones are dropped. `data/report.json` shows what was tested.
- `docs/privacy.html` – privacy policy (https://codiumsol.github.io/tunely/privacy.html).
- `docs/s/` – the page behind shared station links: opens the station in the app, or
  Google Play if the app isn't installed.

**Station owners:** to have a station removed or corrected, email raabtify@gmail.com.
Removals are listed in `data/blocked.txt`.
