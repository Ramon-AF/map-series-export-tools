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

Recent changes not yet confirmed in a live run: works in anyone's project (bundled layouts + symbology, finds
the user's own connection); Portrait beat label (was saved hidden and ran past the border); Portrait Equipment
table moved clear of the Beat Stats table; DLA hides labels on unserviced local roads. The "own project" setup was
tested end to end in a stripped project (no maps or layouts) for BPX, Sidewalk, Salt/Plow and DLA.

## Data and output
- All data is read from **QA** (`winterops_qa`) through the user's own connection: streets and beats from the
  `TCL_WINTER_OPERATIONS` layer in the layout's map, plus `BEAT_EQUIPMENT_ASSIGNMENT` and `TS_BS_PXO`.
- **Beat Stats tables** are written to the **default geodatabase of the open project** (created on the first run,
  emptied and refilled on every run): `MapSeriesExport_BeatStats_v2`, `_RoadClass_v1`, `_BPX_v1`, `_DLA_v1`.
  Each user should have their own project or default geodatabase. Two people sharing one geodatabase at the
  same time can hit lock errors.
- PDFs are named after the beat (for example `BPX1110.pdf`), 300 DPI by default.

## Still to do
1. Live test Cycle Tracks (try CT142) and ROW.
2. **ROW equipment:** needs a `row_beat` column on `BEAT_EQUIPMENT_ASSIGNMENT`.
3. Remove the temporary `CHECKPOINT` debug messages in `execute()`.
4. Optional: a QA / production switch (staying on QA for now).
5. The Portrait layout's scale bar sits below the bottom border (layout setting, not the tool).
6. Live test of the "own project" setup by a second user.

## Requirements
- ArcGIS Pro 3.x
- Your own database connection (`.sde`) to `winterops_qa`. Not included here, because connection files contain
  credentials.

## Using it in your own project
1. Download the repo and keep `MapSeriesExportTools.pyt` and the `MapSeriesExportTools_templates` folder
   **side by side**.
2. In your project, make a database connection to `winterops_qa` (Catalog → Databases → New Database Connection)
   and save the `.sde` in your project folder. The tool finds it automatically; or pick it in the tool's
   **Database Connection** setting.
3. Add the toolbox (Catalog → Toolboxes → Add Toolbox), open **Export Map Series By Beat**, pick the activity and
   an output folder, and run. Leave **Landscape Layout** blank.

What happens on the first run in a new project:
- If the project doesn't have the "Universal Beat Map Series - Landscape" / "- Portrait" layouts, they're added
  from `MapSeriesExportTools_templates` (each brings its own map). If you already imported the layouts someone
  shared with you, those are used instead.
- `TCL_WINTER_OPERATIONS` is added to the layout's map from your connection.
- Symbology comes with the tool: street labels (bold on the route, regular in the background), the Sidewalk colours
  and legend, and the BPX symbols are read from the `.lyrx` files in the templates folder. If your project has a
  "Sidewalks" map with the reference layers, those win, so restyling them there changes the printed maps.
- The Beat Stats tables are created in your project's default geodatabase.

## Templates folder
| File | What it is |
|---|---|
| `Beat Map Landscape.pagx`, `Beat Map Portrait.pagx` | The Universal layouts, with no data layers in their maps |
| `street_labels_bold.lyrx` | Bold route labels; also the Sidewalk symbology |
| `street_labels_regular.lyrx` | Regular background labels |
| `sidewalk_legend.lyrx` | Sidewalk legend wording |
| `bpx_points.lyrx` | Bus stop / PXO / traffic signal symbols |

The files carry no passwords or server names. After changing the layouts or reference layers in the master
project, rebuild them with `build_templates.py` (run with ArcGIS Pro's Python, Pro closed):
`python build_templates.py <output folder>`.
