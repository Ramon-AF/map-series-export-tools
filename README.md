# Map Series Export Tools

> ## ⚠️ WORK IN PROGRESS — NOT COMPLETE
> Do not use this for production map runs yet. Most recent changes have only been tested by
> rendering pages from a standalone script, not in a full live ArcGIS Pro run. See
> **Status** and **Still to do** below.

An ArcGIS Pro Python toolbox (`MapSeriesExportTools.pyt`) with one tool, **Export Map Series By Beat**.
It exports one PDF per beat for a winter-operations activity. It doesn't need an ArcGIS Map Series:
it reads the beats from the data, frames each one on a rotated map, and fills in the layout's title,
legend, Beat Stats table and Equipment table for each page.

## Activities

| Activity | Beat field | Route / points style | Beat Stats table | Equipment table |
|---|---|---|---|---|
| Salt/Plow | `salt_plow_beat` | Coloured by road class (Arterial red, Collector blue, Local green) | BEAT / ROAD CLASS / SALT KMS / PLOW KMS | Yes |
| Sidewalk | `sidewalk_beat` | Copied from the sidewalk reference layers in the "Sidewalks" map; contracted beats only | BEAT / KMS | Yes |
| Cycle Tracks | `cycle_beat` | Red 2 pt line | BEAT / KMS | Yes |
| ROW | `row_beat` | Blue 2 pt line | BEAT / KMS | **No** (no ROW column in `BEAT_EQUIPMENT_ASSIGNMENT`) |
| BPX (Bus Stops, Traffic Signals & PXOs) | `bpx_beat` | `TS_BS_PXO` points, ACTIVE only, styled from the project's own `TS_BS_PXO` layer; streets the points sit on are labeled bold | BEAT / BUS STOPS / PXO / SIGNALS | Yes |
| DLA | `dla_beat` | **Not set up yet** (uses the layer's own symbology, no street background) | BEAT / KMS | Yes |

Shared features:
- **Contract Area** dropdown and **Beat(s)** checklist, to print one area or a few beats instead of the whole city.
- Thin gray street background with labels. Streets on the route are labeled bold and aren't repeated in the background labels.
- Rotation-aware framing: maps are rotated to Toronto's grid (default −17.5°), and each beat is fitted on the rotated page so nothing is cut off.
- The north arrow is linked to the map frame, so it points to true north on rotated pages.
- Table styling lives on the layouts and is kept when the tool changes a table's columns.

## Status

| Activity | Last full live run in Pro |
|---|---|
| Salt/Plow | Before the latest changes |
| Sidewalk | Partly. Background fix not yet confirmed live |
| Cycle Tracks | Partly. Point-symbol fix not yet confirmed live |
| ROW | Not run live |
| BPX | Not run live |
| DLA | Not run live / not styled |

## Still to do
1. One live test run per activity, especially with the rotation fix (try beats LSP1116 and SW1114) and BPX.
2. Pick symbology for **DLA**.
3. **ROW equipment:** needs a `row_beat` column on `BEAT_EQUIPMENT_ASSIGNMENT`.
4. Remove the temporary `CHECKPOINT` debug messages in `execute()`.
5. Confirm QA (`winterops_qa`) vs production (`winterops`) as the data source.
6. **Portability:** the data connection is hard-coded to a local `.sde` file path
   (`...\Projects\Sidewalks\PostgreSQL-afdcc-pg-winterops_qa(gisadmin).sde`). Other users need their own
   connection file, and the path should become a tool parameter.

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
