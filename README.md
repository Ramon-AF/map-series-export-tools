# Map Series Export Tools

> ## ⚠️ WORK IN PROGRESS — NOT COMPLETE
> Four of the six activities have passed a live run in ArcGIS Pro. Cycle Tracks and ROW
> have not been run live yet. See **Status** and **Still to do** below.

An ArcGIS Pro Python toolbox (`MapSeriesExportTools.pyt`) with one tool, **Export Map Series By Beat**.
It exports one PDF per beat for a winter-operations activity. It doesn't need an ArcGIS Map Series:
it reads the beats from the data, frames each one on a rotated map, and fills in the layout's title,
legend, Beat Stats table and Equipment table for each page.

## Activities

| Activity | Map title | Beat field | Route / points style | Beat Stats table | Equipment table |
|---|---|---|---|---|---|
| Salt/Plow | SALT & PLOW | `salt_plow_beat` | Coloured by road class (Arterial red, Collector blue, Local green) | BEAT / ROAD CLASS / SALT KMS / PLOW KMS | Yes |
| Sidewalk | SIDEWALK SALTING & PLOWING | `sidewalk_beat` | Copied from the sidewalk reference layers in the "Sidewalks" map; contracted beats only | BEAT / KMS | Yes |
| BPX | BUS STOPS, TRAFFIC SIGNALS & PXOs | `bpx_beat` | `TS_BS_PXO` points, ACTIVE only, styled from the project's own `TS_BS_PXO` layer; streets the points sit on are labeled bold | BEAT / BUS STOPS / PXO / SIGNALS | Yes |
| DLA | ANTI-ICING (DLA) | `dla_beat` | 1 pass red, 2 passes green (2 pt); local roads with no DLA beat aren't labeled | BEAT / PASSES / KMS (KMS count passes) | Yes |
| Cycle Tracks | CYCLE TRACKS | `cycle_beat` | Red 2 pt line | BEAT / KMS | Yes |
| ROW | ROW | `row_beat` | Blue 2 pt line | BEAT / KMS | **No** (no ROW column in `BEAT_EQUIPMENT_ASSIGNMENT`) |

Shared features:
- **Contract Area** dropdown and **Beat(s)** checklist, to print one area or a few beats instead of the whole city.
  "No Service" beats and ADF are left out.
- Thin gray street background with labels. Streets on the route are labeled bold and aren't repeated in the background labels.
- Rotation-aware framing: maps are rotated to Toronto's grid (default −17.5°), and each beat is fitted on the rotated page so nothing is cut off.
  Each beat goes on whichever layout (Landscape or Portrait) shows it bigger.
- The north arrow is linked to the map frame, so it points to true north on rotated pages.
- Table styling lives on the layouts and is kept when the tool changes a table's columns.

## Status

| Activity | Live run in Pro |
|---|---|
| Salt/Plow | Passed (2026-10-09) |
| Sidewalk | Passed (2026-10-09) |
| BPX | Passed (2026-10-09) |
| DLA | Passed (2026-10-13) |
| Cycle Tracks | Not run live yet |
| ROW | Not run live yet |

Recent fixes not yet confirmed in a live run: Portrait beat label (it was saved hidden in the layout and ran past
the border; now turned on and right-aligned on the title line), and DLA hiding labels on unserviced local roads.

## Data and output
- All data is read from **QA** (`winterops_qa`): streets and beats from the `TCL_WINTER_OPERATIONS` layer in the
  layout's map; `BEAT_EQUIPMENT_ASSIGNMENT` and `TS_BS_PXO` through a fixed connection-file path in the tool.
- **Beat Stats tables** are written to the **default geodatabase of the open project** (created on the first run,
  emptied and refilled on every run): `MapSeriesExport_BeatStats_v2`, `_RoadClass_v1`, `_BPX_v1`, `_DLA_v1`.
  Each user should have their own project or default geodatabase. Two people sharing one geodatabase at the
  same time can hit lock errors.
- PDFs are named after the beat (for example `BPX1110.pdf`), 300 DPI by default.

## Still to do
1. Live test Cycle Tracks (try CT142) and ROW.
2. **ROW equipment:** needs a `row_beat` column on `BEAT_EQUIPMENT_ASSIGNMENT`.
3. Remove the temporary `CHECKPOINT` debug messages in `execute()`.
4. **Portability:** the connection to `winterops_qa` is a hard-coded local `.sde` file path. Other users need
   their own connection file, and the path should become a tool parameter.
5. Optional: a QA / production switch (staying on QA for now).
6. The Portrait layout's scale bar sits below the bottom border (layout setting, not the tool).

## Requirements
- ArcGIS Pro 3.x
- An enterprise geodatabase connection to `winterops_qa` (not included in this repository, because connection
  files contain credentials)
- A project with the "Universal Beat Map Series - Landscape" / "- Portrait" layouts and the reference layers
  in the "Sidewalks" map

## Usage
Add `MapSeriesExportTools.pyt` to the project (Catalog → Toolboxes → Add Toolbox), open **Export Map Series
By Beat**, pick the activity, layouts and output folder, and optionally a contract area or beats, then run.
Run it from inside the project; it uses the project that's currently open.
