# -*- coding: utf-8 -*-
"""
MapSeriesExportTools.pyt

Generalized version of a one-off sidewalk beat map-export script. Does NOT
require an ArcGIS Pro "Map Series" to be configured on the layout at all -
no index layer, no page-name field, no enabled map series. Instead, it
works straight off whatever beat values exist in the currently visible
data: it collects the distinct beat values from whichever visible layers
carry the resolved beat field, and exports one PDF per distinct beat.

Picking an Activity:
  - Suggests the beat field to use (checked against whichever layers are
    currently visible in the Landscape Layout's map).
  - Overwrites the layout's title text element(s) with the activity name.
  - Converts any text element that was showing a live "current beat" value
    (a dynamic-text binding on a "...beat..." field) into one the script
    sets directly each page, so it still works without a map series driving
    per-page recalculation.
  - Shows/hides the sidewalk-specific km-by-side summary table ("Table
    Frame") - that's structurally tied to sidewalk fields (SDWK_SIDE) and
    only makes sense for the Sidewalk activity. "Beat Stats Table" is
    handled generically for any activity that has a km/length field.
  - Filters an "Equipment Table" element (if present) to the current beat,
    using whatever table is already linked to it in the layout - by default
    that's expected to be a BEAT_EQUIPMENT_ASSIGNMENT export/extract.

No layer picking required either: the layers to fit the extent to (and
pull beat values from) are whichever layers are currently turned ON in the
map and have the resolved beat field; the label layers to filter per page
are whichever visible layers are true Annotation feature classes. Just
turn on the layers you want used before running the tool.

Run it from the Geoprocessing pane, inside the ArcGIS Pro project that has
the layouts/layers you want to export.
"""

import arcpy
import os
import itertools
import importlib
import copy


ACTIVITIES = ["Salt/Plow", "Sidewalk", "DLA", "ROW", "BPX", "Cycle Tracks"]

# beat field candidates to try (in order) against whatever layers are
# visible in the map, per activity - covers both the enterprise schema
# (salt_plow_beat) and the older cartographic-layer schema (separate
# salt_beat/plow_beat).
BEAT_FIELD_CANDIDATES = {
    "Salt/Plow":            ["salt_plow_beat", "saltplow_beat", "salt_beat", "plow_beat"],
    "Sidewalk":             ["sidewalk_beat", "sdwk_beat", "SDWK_BEAT"],
    "DLA":                  ["dla_beat"],
    "ROW":                  ["row_beat"],
    "BPX":                  ["bpx_beat"],
    "Cycle Tracks":         ["cycle_beat", "cycling_beat"],
}

# canonical field name on the live enterprise BEAT_EQUIPMENT_ASSIGNMENT
# table (winterops_qa) per activity - None means that activity has no
# matching column there.
EQUIPMENT_FIELD_BY_ACTIVITY = {
    "Salt/Plow":            "salt_plow_beat",
    "Sidewalk":             "sidewalk_beat",
    "DLA":                  "dla_beat",
    "ROW":                  None,   # no matching column on BEAT_EQUIPMENT_ASSIGNMENT
    "BPX":                  "bpx_beat",
    "Cycle Tracks":         "cycle_beat",
}

# field used by the optional "Contract Area" filter (e.g. A11-A25, ADF on
# TCL_WINTER_OPERATIONS) to print only the beats that fall in one area.
CONTRACT_AREA_FIELD_CANDIDATES = ["contract_area"]
# never offered in the dropdown - ADF = DVP/FGGE, not ours (per Ramon)
CONTRACT_AREAS_EXCLUDED = {"ADF"}
# "we only do contracted beats" (Ramon 2026-10-05): in-house segments are
# left out of everything (dropdowns, extents, route, stats) for activities
# that track it - only sidewalks do (CONT / IH). Excludes IH rather than
# requiring CONT, so segments with a blank operation still print.
OPERATION_FIELD_CANDIDATES_BY_ACTIVITY = {"Sidewalk": ["sidewalk_operation", "sdwk_operation"]}
IN_HOUSE_OPERATION = "IH"
# placeholder values that never show up in the Contract Area / Beat dropdowns
BLANK_FILTER_VALUES = {"", "NA", "N/A", "NONE"}
# beat values that aren't real beats - never listed or printed
# (e.g. sidewalk_beat "No Service - Transit/Allen", per Ramon 2026-10-05)
JUNK_BEAT_PREFIXES = ("NO SERVICE",)

# {(layer data sources, field, where): sorted values} - updateParameters runs
# on every parameter edit, so don't re-scan the source data each time.
_DISTINCT_VALUE_CACHE = {}

EQUIPMENT_TABLE_SDE_PATH = (
    r"D:\Users\rboyce\Documents\ArcGIS\Projects\Sidewalks\PostgreSQL-afdcc-pg-winterops_qa(gisadmin).sde"
    r"\winterops_qa.gisadmin.BEAT_EQUIPMENT_ASSIGNMENT")
BEAT_STATS_TABLE_NAME = "MapSeriesExport_BeatStats_v2"

# reference layer whose label classes (road-class-keyed, e.g. ARTERIALS/
# COLLECTOR/LOCALS) get auto-applied (with field names substituted) to
# whatever layer is actually being used, so printed maps always show street
# labels without needing per-project label setup.
LABEL_CLASS_REFERENCE_MAP = "Sidewalks"
LABEL_CLASS_REFERENCE_LAYER = "TCL_ALL_SEG_SEPT_26_v2"
# Separate reference for the plain-gray "background" layer (kept visible
# the whole run in Salt/Plow road-class mode) - same map, its own label
# styling tuned for a dense background view rather than the highlighted
# route itself. Same whereClause/expression shape as LABEL_CLASS_REFERENCE_LAYER
# (winter_rdcls = 'CODE' / $feature.linear_n_1), so _apply_label_classes
# works on it unchanged.
LABEL_CLASS_BASEMAP_REFERENCE_LAYER = "TCL_ALL_SEG_SEPT_26_v2_basemap"
LABEL_ROADCLASS_FIELD_CANDIDATES = ["winter_roadclass", "winter_rdcls", "WINTER_RDC", "ROAD_CLASS"]
LABEL_NAME_FIELD_CANDIDATES = ["road_name", "linear_n_1", "LINEAR_NAM", "STREET_NAME"]

# kms-length field candidates per activity, checked against the fit layers'
# real fields - used to (re)build the generic per-beat "Beat Stats Table".
KMS_FIELD_CANDIDATES = {
    "Salt/Plow":            ["salt_plow_kms", "saltplow_kms", "salt_kms", "plow_kms"],
    "Sidewalk":             ["sdwk_kms", "sidewalk_kms"],
    "DLA":                  ["dla_kms"],
    "ROW":                  ["row_kms"],
    "BPX":                  [],   # point assets, no km concept
    "Cycle Tracks":         ["cycle_kms"],
}

# Salt/Plow-specific "route highlight" mode: the extracted per-beat route
# colored by road class, on top of the full underlying street network kept
# visible (in a plain thin gray style) for context. Field candidates reuse
# the same road-class field already resolved for street labeling (also
# used for the Beat Stats Table's per-road-class KMS breakdown - see
# _rebuild_beat_stats_roadclass_table).
ROADCLASS_FIELD_CANDIDATES = LABEL_ROADCLASS_FIELD_CANDIDATES
SALT_KMS_FIELD_CANDIDATES = ["salt_kms"]
PLOW_KMS_FIELD_CANDIDATES = ["plow_kms"]

BACKGROUND_STREET_SYMBOLOGY = {"color": [90, 90, 90, 100], "width": 0.5}
# Per Ramon 2026-09-29: red/arterial, blue/collector, green/local. Applied
# via the higher-level arcpy.mp symbology API (see _apply_roadclass_symbology)
# to a FRESH, one-time-use layer every beat (never a persistent, reused one -
# that's what caused an earlier intermittent renderer-type crash).
ROADCLASS_SYMBOLOGY = {
    "ART": {"color": [237, 28, 36, 100], "width": 2.5},
    "COL": {"color": [0, 112, 255, 100], "width": 1.5},
    "LOC": {"color": [0, 153, 51, 100], "width": 1.0},
}
# legend wording for the road-class codes (renderer class labels)
ROADCLASS_LABELS = {"ART": "Arterial", "COL": "Collector", "LOC": "Local", "EXP": "Expressway"}
ROADCLASS_DEFAULT_SYMBOLOGY ={"color": [160, 160, 160, 100], "width": 0.75}

BEAT_STATS_ROADCLASS_TABLE_NAME = "MapSeriesExport_BeatStats_RoadClass_v1"
# Name prefix used by an earlier, since-abandoned persistent-highlight-layer
# design - kept only so _visible_fit_layers/_cleanup_stale_route_highlight_layers
# can still find and clean up any leftover layer/data a pre-2026-09-29 run
# left behind in someone's live project.
ROUTE_HIGHLIGHT_LAYER_NAME_PREFIX = "Route (by Road Class)"

# Sidewalk mode: symbology copied (renderer CIM, field names substituted)
# from Ramon's reference layers in the "Sidewalks" map, so tweaking those
# layers there changes the printed maps too.
#   - beat route:  colored by sdwk_side (red both / blue one / purple / orange partial)
#   - background:  thin plain gray line, same as every activity (_style_background)
#   - legend:      same sdwk_side colors, relabeled with the "_legend" layer's
#                  descriptions by matching color (its own sdwk_desc codes
#                  SDWKB/SDWKO/PS don't match the enterprise SDWKN/SDWKS/... codes)
SIDEWALK_SYMBOLOGY_REFERENCE_LAYER = "TCL_ALL_SEG_SEPT_26_v2"
SIDEWALK_LEGEND_REFERENCE_LAYER = "TCL_ALL_SEG_SEPT_26_v2_legend"
SIDEWALK_SIDE_FIELD_CANDIDATES = ["sdwk_side"]
SIDEWALK_LEGEND_LAYER_NAME_PREFIX = "Sidewalk Legend"

# flat single-color beat route per activity (per Ramon 2026-10-05: CT =
# cycle track beats red 2pt, ROW blue 2pt). "label" is also the name of the
# legend helper layer, so it's what shows in the legend.
ROUTE_STYLE_BY_ACTIVITY = {
    "Cycle Tracks":         {"color": [255, 0, 0, 100], "width": 2, "label": "CT Beat"},
    "ROW":                  {"color": [0, 112, 255, 100], "width": 2, "label": "ROW Beat"},
}

# BPX (per Ramon 2026-10-08): always the enterprise TS_BS_PXO points (bus
# stops, PXOs, traffic signals), ACTIVE ones only, one symbol per type,
# drawn over the street network (TCL_WINTER_OPERATIONS) as background, and
# a count-by-type Beat Stats table instead of KMS.
_SDE = r"D:\Users\rboyce\Documents\ArcGIS\Projects\Sidewalks\PostgreSQL-afdcc-pg-winterops_qa(gisadmin).sde"
BPX_POINTS_SDE_PATH = _SDE + r"\winterops_qa.gisadmin.TS_BS_PXO"
STREETS_SDE_PATH = _SDE + r"\winterops_qa.gisadmin.TCL_WINTER_OPERATIONS"
BPX_TYPE_FIELD = "tsbspxo"
# activity -> (field, value) - only rows with that value are used anywhere
ACTIVE_FILTER_BY_ACTIVITY = {"BPX": ("active_status", "ACTIVE")}
BPX_TYPE_SYMBOLOGY = {
    "1": {"label": "Bus Stop",       "shape": "Circle 1",   "color": [0, 112, 255, 100], "size": 6, "column": "BUS_STOPS", "heading": "BUS STOPS"},
    "2": {"label": "PXO",            "shape": "Triangle 1", "color": [255, 128, 0, 100], "size": 7, "column": "PXO",       "heading": "PXO"},
    "3": {"label": "Traffic Signal", "shape": "Square 1",   "color": [220, 0, 0, 100],   "size": 6, "column": "SIGNALS",   "heading": "SIGNALS"},
}
BEAT_STATS_BPX_TABLE_NAME = "MapSeriesExport_BeatStats_BPX_v1"
BPX_LEGEND_LAYER_NAME_PREFIX = "BPX Legend"
# smallest map area (metres) a page is framed on - a beat with one point
# would otherwise have a zero-size box
MIN_FRAME_SPAN_M = 300.0
# BPX: a point "sits on" a street within this distance (bus stops are on
# the sidewalk, ~10-25 m off the centreline on wide arterials)
BPX_STREET_SEARCH_M = 40

# DLA (per Ramon 2026-10-13): route coloured by number of passes - 1 pass
# red, 2 passes green - and a Beat Stats table of KMS by passes. dla_kms is
# already pass-weighted (2-pass segment = 2 x its length).
DLA_PASS_FIELD = "dla_pass"
DLA_PASS_SYMBOLOGY = {
    "1": {"label": "1 Pass",   "color": [255, 0, 0, 100], "width": 2},
    "2": {"label": "2 Passes", "color": [0, 168, 0, 100], "width": 2},
}
BEAT_STATS_DLA_TABLE_NAME = "MapSeriesExport_BeatStats_DLA_v1"
DLA_LEGEND_LAYER_NAME_PREFIX = "DLA Legend"

# layers this tool adds itself - never auto-detected as fit layers, and
# cleaned up at the start of a run if an earlier run left one behind
TOOL_LAYER_NAME_PREFIXES = ((ROUTE_HIGHLIGHT_LAYER_NAME_PREFIX, SIDEWALK_LEGEND_LAYER_NAME_PREFIX)
                            + tuple(s["label"] for s in ROUTE_STYLE_BY_ACTIVITY.values())
                            + (BPX_LEGEND_LAYER_NAME_PREFIX, DLA_LEGEND_LAYER_NAME_PREFIX))

TITLE_TEXT_BY_ACTIVITY = {
    "Salt/Plow":            "SALT & PLOW",
    "Sidewalk":             "SIDEWALK SALTING & PLOWING",
    "DLA":                  "ANTI-ICING (DLA)",
    "ROW":                  "ROW",
    "BPX":                  "BUS STOPS, TRAFFIC SIGNALS & PXOs",
    "Cycle Tracks":         "CYCLE TRACKS",
}


class Toolbox(object):
    def __init__(self):
        self.label = "Map Series Export Tools"
        self.alias = "mapseriesexport"
        self.tools = [ExportBeatMapSeries]


class ExportBeatMapSeries(object):
    def __init__(self):
        self.label = "Export Map Series By Beat"
        self.description = (
            "Exports one PDF per distinct beat found in the currently visible layers, "
            "fitting the extent to that beat's features, auto-picking landscape vs "
            "portrait orientation, temporarily restricting visible annotation layers to "
            "each beat's area, and filtering an equipment/vehicle table to the current "
            "beat. No ArcGIS map series needs to be configured - pick an Activity and "
            "turn on the layers you want used before running."
        )
        self.canRunInBackground = False
        # set at the start of execute(); drives the contracted-only filter
        self._activity = None
        # background layers + their label classes, see _style_background /
        # _add_background_label_layer
        self._bg_layers = []
        self._bg_label_classes = None
        # real layers hidden for the whole run (e.g. the BPX points - only
        # each page's own copy is drawn); turned back on at end of run
        self._keep_hidden = []
        # bold street label classes (the beat route's), for BPX bold streets
        self._bold_label_classes = None
        # Ramon's own TS_BS_PXO renderer, see _find_bpx_reference_renderer
        self._bpx_ref_renderer = None

    # ---------------- parameters ----------------
    def getParameterInfo(self):
        p_activity = arcpy.Parameter(
            displayName="Activity",
            name="activity",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        p_activity.filter.type = "ValueList"
        p_activity.filter.list = ACTIVITIES
        p_activity.value = ACTIVITIES[0]

        p_layout_l = arcpy.Parameter(
            displayName="Landscape Layout",
            name="layout_landscape",
            datatype="GPLayout",
            parameterType="Required",
            direction="Input")

        p_layout_p = arcpy.Parameter(
            displayName="Portrait Layout (optional - landscape used for every page if omitted)",
            name="layout_portrait",
            datatype="GPLayout",
            parameterType="Optional",
            direction="Input")

        p_map = arcpy.Parameter(
            displayName="Map To Look In (optional - defaults to whatever map the Landscape Layout is wired to)",
            name="map_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        p_map.filter.type = "ValueList"
        try:
            p_map.filter.list = [m.name for m in arcpy.mp.ArcGISProject("CURRENT").listMaps()]
        except Exception:
            p_map.filter.list = []

        p_map_layers = arcpy.Parameter(
            displayName="Map Layer(s) To Use (optional - auto-detected from visible layers in the chosen Map if left blank)",
            name="map_layers",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
            multiValue=True)

        p_beat_field = arcpy.Parameter(
            displayName="Beat Field (auto-suggested from Activity; pick from the Map Layer(s) selected above)",
            name="beat_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        p_beat_field.parameterDependencies = [p_map_layers.name]

        p_contract_area = arcpy.Parameter(
            displayName="Contract Area (optional - only print beats in this contract area; blank = whole city)",
            name="contract_area",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        p_contract_area.filter.type = "ValueList"
        p_contract_area.filter.list = []

        p_beats = arcpy.Parameter(
            displayName="Beat(s) To Print (optional - blank = every beat, or every beat in the Contract Area above)",
            name="beats",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            multiValue=True)
        p_beats.filter.type = "ValueList"
        p_beats.filter.list = []

        p_anno_dist = arcpy.Parameter(
            displayName="Annotation Distance",
            name="anno_distance",
            datatype="GPLinearUnit",
            parameterType="Optional",
            direction="Input")
        p_anno_dist.value = "50 Meters"

        p_out_dir = arcpy.Parameter(
            displayName="Output Folder",
            name="output_dir",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")

        p_margin = arcpy.Parameter(
            displayName="Extent Margin Factor",
            name="margin_factor",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_margin.value = 1.10

        p_use_rotation = arcpy.Parameter(
            displayName="Use Fixed Map Rotation",
            name="use_fixed_rotation",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        p_use_rotation.value = True

        p_heading = arcpy.Parameter(
            displayName="Fixed Heading (Degrees)",
            name="fixed_heading",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        p_heading.value = -17.50

        p_dpi = arcpy.Parameter(
            displayName="Export Resolution (DPI)",
            name="dpi",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        p_dpi.value = 300

        p_equip_field_override = arcpy.Parameter(
            displayName="Equipment Table Beat Field Override (optional - auto-detected otherwise)",
            name="equipment_field_override",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")

        return [p_activity, p_layout_l, p_layout_p, p_map, p_map_layers, p_beat_field,
                p_contract_area, p_beats,
                p_anno_dist, p_out_dir, p_margin, p_use_rotation,
                p_heading, p_dpi, p_equip_field_override]

    def isLicensed(self):
        return True

    @staticmethod
    def _candidate_layers(p_layout_l, p_map, p_map_layers):
        """Layers the tool will read from, as known at validation time: the
        explicitly picked Map Layer(s) if any, else the visible feature
        layers in the chosen Map (or the Landscape Layout's map)."""
        if p_map_layers.values:
            return list(p_map_layers.values)
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        target_map = None
        if p_map.value:
            target_map = next((m for m in aprx.listMaps() if m.name == p_map.valueAsText), None)
        if target_map is None and p_layout_l.value:
            lay = next((l for l in aprx.listLayouts() if l.name == p_layout_l.valueAsText), None)
            frames = lay.listElements("MAPFRAME_ELEMENT") if lay else []
            target_map = frames[0].map if frames else None
        if target_map is None:
            return []
        return [lyr for lyr in target_map.listLayers()
                if getattr(lyr, "visible", False) and getattr(lyr, "isFeatureLayer", False)
                and not lyr.name.startswith(TOOL_LAYER_NAME_PREFIXES)]

    @staticmethod
    def _layer_fields(lyr):
        try:
            d = arcpy.Describe(lyr)
            if getattr(d, "featureType", None) == "Annotation":
                return {}
            return {f.name.lower(): f.name for f in d.fields}
        except Exception:
            return {}

    @staticmethod
    def _distinct_values(layers, field, where=None):
        """Sorted distinct, non-placeholder values of `field` across `layers`
        (only those layers that have it), cached per layers/field/where."""
        key = (tuple(getattr(l, "dataSource", str(l)) for l in layers), field.lower(), where)
        if key in _DISTINCT_VALUE_CACHE:
            return _DISTINCT_VALUE_CACHE[key]
        vals = set()
        for lyr in layers:
            fields = ExportBeatMapSeries._layer_fields(lyr)
            if field.lower() not in fields:
                continue
            with arcpy.da.SearchCursor(lyr, [fields[field.lower()]], where) as cur:
                for (v,) in cur:
                    if v is None:
                        continue
                    v = str(v).strip()
                    if v.upper() not in BLANK_FILTER_VALUES and not v.upper().startswith(JUNK_BEAT_PREFIXES):
                        vals.add(v)
        out = sorted(vals)
        _DISTINCT_VALUE_CACHE[key] = out
        return out

    @staticmethod
    def _contracted_clause(lyr, activity):
        """The "rows we actually do" filter, applied everywhere (dropdowns,
        extents, route, stats): leaves out in-house segments (sidewalks) and
        anything not ACTIVE (BPX points). None if neither applies to this
        activity/layer."""
        fields = ExportBeatMapSeries._layer_fields(lyr)
        clauses = []
        real = next((fields[c.lower()] for c in OPERATION_FIELD_CANDIDATES_BY_ACTIVITY.get(activity, [])
                     if c.lower() in fields), None)
        if real:
            f = arcpy.AddFieldDelimiters(lyr, real)
            clauses.append(f"({f} IS NULL OR {f} <> '{IN_HOUSE_OPERATION}')")
        active = ACTIVE_FILTER_BY_ACTIVITY.get(activity)
        if active and active[0].lower() in fields:
            f = arcpy.AddFieldDelimiters(lyr, fields[active[0].lower()])
            clauses.append(f"{f} = '{active[1]}'")
        return " AND ".join(clauses) if clauses else None

    @staticmethod
    def _in_house_beats(layers, beat_field, activity):
        """Beats that are in-house as a whole: some IH segments and no
        contracted ones. Dropped entirely - otherwise the odd blank-operation
        segment in an in-house yard beat (e.g. TY09NW: 161 IH + 3 blank)
        would print as a stray one-segment page. Mixed CONT/IH beats are
        kept (minus their IH segments, via _contracted_clause), and all-blank
        beats (e.g. SW22xx, operation never filled in) are kept too."""
        ops_by_beat = {}
        for lyr in layers:
            fields = ExportBeatMapSeries._layer_fields(lyr)
            op = next((fields[c.lower()] for c in OPERATION_FIELD_CANDIDATES_BY_ACTIVITY.get(activity, [])
                       if c.lower() in fields), None)
            if not op or beat_field.lower() not in fields:
                continue
            key = ("in_house", getattr(lyr, "dataSource", str(lyr)), beat_field.lower(), op)
            if key not in _DISTINCT_VALUE_CACHE:
                ops = {}
                with arcpy.da.SearchCursor(lyr, [fields[beat_field.lower()], op]) as cur:
                    for b, o in cur:
                        if b is not None:
                            ops.setdefault(str(b).strip(), set()).add(o)
                _DISTINCT_VALUE_CACHE[key] = ops
            for b, s in _DISTINCT_VALUE_CACHE[key].items():
                ops_by_beat.setdefault(b, set()).update(s)
        return {b for b, s in ops_by_beat.items() if IN_HOUSE_OPERATION in s and "CONT" not in s}

    @staticmethod
    def _and_where(*clauses):
        clauses = [c for c in clauses if c]
        if not clauses:
            return None
        return " AND ".join(f"({c})" for c in clauses)

    @staticmethod
    def _contract_area_where(layers, contract_area):
        """(field, where clause) for the Contract Area filter, or (None, None)
        if no layer carries a contract area field."""
        for lyr in layers:
            fields = ExportBeatMapSeries._layer_fields(lyr)
            for cand in CONTRACT_AREA_FIELD_CANDIDATES:
                if cand.lower() in fields:
                    real = fields[cand.lower()]
                    if contract_area is None:
                        return real, None
                    safe = str(contract_area).replace("'", "''")
                    return real, f"{arcpy.AddFieldDelimiters(lyr, real)} = '{safe}'"
        return None, None

    def updateParameters(self, parameters):
        (p_activity, p_layout_l, p_layout_p, p_map, p_map_layers, p_beat_field,
         p_contract_area, p_beats,
         p_anno_dist, p_out_dir, p_margin, p_use_rotation,
         p_heading, p_dpi, p_equip_field_override) = parameters

        if not p_layout_l.value:
            return
        try:
            layers = self._candidate_layers(p_layout_l, p_map, p_map_layers)
        except Exception:
            return
        if str(p_activity.value) == "BPX":
            # BPX always reads the enterprise TS_BS_PXO points, whether or
            # not they're in the map yet
            layers = [BPX_POINTS_SDE_PATH]

        if p_activity.value and not p_beat_field.altered:
            candidates = BEAT_FIELD_CANDIDATES.get(str(p_activity.value), [])
            real_fields = set()
            for lyr in layers:
                real_fields.update(self._layer_fields(lyr).keys())
            for cand in candidates:
                if cand.lower() in real_fields:
                    p_beat_field.value = cand
                    break

        if not p_beat_field.valueAsText:
            return
        beat_field = p_beat_field.valueAsText
        beat_layers = [l for l in layers if beat_field.lower() in self._layer_fields(l)]
        try:
            ca_field, _ = self._contract_area_where(beat_layers, None)
            # only offer areas that actually have beats for this field (e.g.
            # ADF = DVP/FGGE has no salt_plow_beat values at all)
            has_beat = None
            contracted = None
            if beat_layers:
                bf = arcpy.AddFieldDelimiters(beat_layers[0], beat_field)
                has_beat = f"{bf} IS NOT NULL AND {bf} NOT IN ('', 'NA', 'N/A', 'None')"
                contracted = self._contracted_clause(beat_layers[0], str(p_activity.value))
            p_contract_area.filter.list = (
                [a for a in self._distinct_values(beat_layers, ca_field, self._and_where(has_beat, contracted))
                 if a.upper() not in CONTRACT_AREAS_EXCLUDED]
                if ca_field else [])

            where = None
            if p_contract_area.valueAsText and ca_field:
                _, where = self._contract_area_where(beat_layers, p_contract_area.valueAsText)
            in_house = self._in_house_beats(beat_layers, beat_field, str(p_activity.value))
            beat_choices = [b for b in self._distinct_values(beat_layers, beat_field, self._and_where(where, contracted))
                            if b not in in_house]
            # keep already-ticked beats that still fit, drop ones the new
            # contract area excludes (otherwise Pro flags them as invalid)
            if p_beats.values:
                kept = [b for b in p_beats.values if b in beat_choices]
                if len(kept) != len(p_beats.values):
                    p_beats.values = kept or None
            p_beats.filter.list = beat_choices
        except Exception:
            pass
        return

    def updateMessages(self, parameters):
        p_contract_area = parameters[6]
        if p_contract_area.valueAsText and not p_contract_area.filter.list:
            p_contract_area.setWarningMessage(
                "None of the layers being used have a contract area field - this filter will fail at run time.")
        return

    # ---------------- helpers ----------------
    @staticmethod
    def _get_layout(aprx, name):
        lay = next((l for l in aprx.listLayouts() if l.name == name), None)
        if not lay:
            raise RuntimeError(f"Layout not found: {name}")
        return lay

    @staticmethod
    def _pick_map_frame(layout):
        frames = layout.listElements("MAPFRAME_ELEMENT")
        if not frames:
            raise RuntimeError(f"No map frame in {layout.name}")
        if len(frames) == 1:
            return frames[0]
        for g in ("Main Map", "Map Frame", "MapFrame", "Main"):
            f = next((x for x in frames if x.name == g), None)
            if f:
                return f
        return frames[0]

    @staticmethod
    def _iter_layers_recursive(m):
        for lyr in m.listLayers():
            yield lyr
            if getattr(lyr, "isGroupLayer", False):
                for sub in lyr.listLayers():
                    yield sub

    def _visible_fit_layers(self, mf, beat_field):
        """Visible feature layers (not annotation) that actually carry the
        resolved beat field. Never picks up the tool's own persistent
        road-class highlight layer(s) (see ROUTE_HIGHLIGHT_LAYER_NAME_PREFIX)
        - those are a schema clone of a real fit layer, so without this
        exclusion a leftover one from an earlier run (e.g. one whose
        teardown didn't complete) gets auto-detected as a second, bogus fit
        layer on every subsequent run, corrupting the combined extent."""
        out = []
        target_field = beat_field.lower()
        for lyr in self._iter_layers_recursive(mf.map):
            if not getattr(lyr, "visible", False):
                continue
            if not lyr.isFeatureLayer:
                continue
            if lyr.name.startswith(TOOL_LAYER_NAME_PREFIXES):
                continue
            try:
                d = arcpy.Describe(lyr)
                if d.featureType == "Annotation":
                    continue
                fields = {f.name.lower() for f in d.fields}
            except Exception:
                continue
            if target_field in fields:
                out.append(lyr)
        return out

    def _visible_anno_layers(self, mf):
        """Visible layers that are true Annotation feature classes."""
        out = []
        for lyr in self._iter_layers_recursive(mf.map):
            if not getattr(lyr, "visible", False):
                continue
            if not lyr.isFeatureLayer:
                continue
            try:
                if arcpy.Describe(lyr).featureType == "Annotation":
                    out.append(lyr)
            except Exception:
                continue
        return out

    @staticmethod
    def _extent_with_margin(ext, factor):
        cx, cy = (ext.XMin + ext.XMax) / 2.0, (ext.YMin + ext.YMax) / 2.0
        hw = (ext.XMax - ext.XMin) / 2.0 * factor
        hh = (ext.YMax - ext.YMin) / 2.0 * factor
        sr = getattr(ext, "spatialReference", None)
        return arcpy.Extent(cx - hw, cy - hh, cx + hw, cy + hh, spatial_reference=sr)

    @staticmethod
    def _precompute_beat_extents(fit_layers, beat_field, activity=None):
        """One geoprocessing pass per fit layer (MinimumBoundingGeometry
        grouped by beat_field, producing one envelope per distinct beat
        value) instead of a MakeFeatureLayer+Select+cursor-scan per beat -
        the latter got slow with 200+ beats since it re-scanned geometry
        from scratch for every single page. Returns {beat_value: Extent},
        combined across every fit layer passed in. Skips blank/NA beat
        values."""
        combined = {}
        for lyr in fit_layers:
            out_fc = arcpy.CreateUniqueName("beat_bounds", arcpy.env.scratchGDB)
            src = lyr
            contracted = ExportBeatMapSeries._contracted_clause(lyr, activity)
            if contracted:
                src = arcpy.management.MakeFeatureLayer(
                    lyr, arcpy.CreateUniqueName("contracted_only", "in_memory"), contracted)[0]
            try:
                arcpy.management.MinimumBoundingGeometry(
                    src, out_fc, geometry_type="ENVELOPE",
                    group_option="LIST", group_field=beat_field)
                with arcpy.da.SearchCursor(out_fc, [beat_field, "SHAPE@"]) as cur:
                    for beat_val, geom in cur:
                        if beat_val is None or not geom:
                            continue
                        beat_val = str(beat_val).strip()
                        if (not beat_val or beat_val.upper() == "NA"
                                or beat_val.upper().startswith(JUNK_BEAT_PREFIXES)):
                            continue
                        ext = geom.extent
                        prev = combined.get(beat_val)
                        if prev is None:
                            combined[beat_val] = ext
                        else:
                            combined[beat_val] = arcpy.Extent(
                                min(prev.XMin, ext.XMin), min(prev.YMin, ext.YMin),
                                max(prev.XMax, ext.XMax), max(prev.YMax, ext.YMax),
                                spatial_reference=ext.spatialReference)
            finally:
                if arcpy.Exists(out_fc):
                    arcpy.management.Delete(out_fc)
                if src is not lyr:
                    arcpy.management.Delete(src)
        return combined

    # ---- rotation-aware framing ----
    # Toronto's street grid isn't true north, so maps are rotated (Fixed
    # Heading, default -17.5). The old framing sized the view from the
    # beat's north-up envelope and only THEN rotated, which swung the
    # envelope's corners out of the frame - 80/221 Salt/Plow beats were cut
    # off (worst ~30% past the edge, e.g. LSP1116), while other edges sat
    # half empty. Now each beat's box is measured in the rotated (page)
    # direction, orientation is picked from that, and centre + scale +
    # heading are set together so the whole beat fits. Page = R(heading) * map
    # (CCW rotation by heading): heading -17.5 turns the map clockwise, which
    # levels the grid (verified: Harbord/Bloor print level).
    PAGE_UNIT_METERS = {"INCH": 0.0254, "CENTIMETER": 0.01, "MILLIMETER": 0.001, "POINT": 0.0254 / 72}

    @staticmethod
    def _rotated_boxes(fit_layers, beat_field, activity, heading_deg):
        """{beat: (umin, umax, vmin, vmax)} - each beat's bounding box in
        the rotated page direction (contracted-only segments, same as
        everything else). One cursor pass over the beat layers."""
        import math
        th = math.radians(heading_deg)
        c, s = math.cos(th), math.sin(th)
        boxes = {}
        for lyr in fit_layers:
            fields = ExportBeatMapSeries._layer_fields(lyr)
            if beat_field.lower() not in fields:
                continue
            where = ExportBeatMapSeries._contracted_clause(lyr, activity)
            with arcpy.da.SearchCursor(lyr, [fields[beat_field.lower()], "SHAPE@"], where) as cur:
                for b, g in cur:
                    if b is None or g is None:
                        continue
                    b = str(b).strip()
                    box = boxes.get(b)
                    if g.type == "point":
                        vertices = [g.firstPoint]                      # BPX points
                    elif g.type == "multipoint":
                        vertices = list(g)
                    else:
                        vertices = [p for part in g for p in part]     # lines / polygons
                    for p in vertices:
                        if p is None:
                            continue
                        u = p.X * c - p.Y * s
                        v = p.X * s + p.Y * c
                        if box is None:
                            box = [u, u, v, v]
                        else:
                            if u < box[0]: box[0] = u
                            if u > box[1]: box[1] = u
                            if v < box[2]: box[2] = v
                            if v > box[3]: box[3] = v
                    if box is not None:
                        boxes[b] = box
        return boxes

    @staticmethod
    def _frame_meters(layout, mf):
        k = ExportBeatMapSeries.PAGE_UNIT_METERS.get(str(layout.pageUnits).upper(), 0.0254)
        return mf.elementWidth * k, mf.elementHeight * k

    @staticmethod
    def _scale_for(box, frame_w_m, frame_h_m, margin):
        # never frame less than MIN_FRAME_SPAN_M across (a one-point BPX
        # beat has a zero-size box)
        w = max(box[1] - box[0], MIN_FRAME_SPAN_M)
        h = max(box[3] - box[2], MIN_FRAME_SPAN_M)
        return max(w * margin / frame_w_m, h * margin / frame_h_m)

    @staticmethod
    def _place_camera(mf, box, heading_deg, scale):
        """Centre the rotated box in the frame at this scale and heading."""
        import math
        th = math.radians(heading_deg)
        c, s = math.cos(th), math.sin(th)
        uc, vc = (box[0] + box[1]) / 2.0, (box[2] + box[3]) / 2.0
        cam = mf.camera
        cam.heading = heading_deg
        cam.X = uc * c + vc * s          # back to map coordinates (inverse rotation)
        cam.Y = -uc * s + vc * c
        cam.scale = scale

    @staticmethod
    def _link_north_arrow(layout, mf, messages):
        """North arrow must follow THIS map frame so it turns with the map.
        Both Universal layouts' arrows pointed at a frame that doesn't exist
        there ("TOA 1-5 ARTERIAL COLLECTOR MAP SERIES", left over from the
        layout they were copied from), so they always pointed straight up -
        17.5 deg off true north on every rotated page."""
        for el in layout.listElements():
            try:
                cim = el.getDefinition("V3")
            except Exception:
                continue
            if type(cim).__name__ not in ("CIMMarkerNorthArrow", "CIMNorthArrow"):
                continue
            if getattr(cim, "mapFrame", None) != mf.name:
                old = cim.mapFrame
                cim.mapFrame = mf.name
                el.setDefinition(cim)
                messages.addMessage(f"North arrow on '{layout.name}' re-linked from '{old}' to '{mf.name}' "
                                    f"so it rotates with the map.")

    @staticmethod
    def _chunked(iterable, size):
        it = iter(iterable)
        while True:
            chunk = list(itertools.islice(it, size))
            if not chunk:
                return
            yield chunk

    def _filter_anno_for_current_beat(self, fit_layers, anno_layers, beat_field, beat_text, anno_distance):
        originals = []
        if not anno_layers or not fit_layers:
            return originals

        beat_safe = beat_text.replace("'", "''")
        one_layers = []
        try:
            for roads_lyr in fit_layers:
                fld = arcpy.AddFieldDelimiters(roads_lyr, beat_field)
                where = self._and_where(f"{fld} = '{beat_safe}'",
                                        self._contracted_clause(roads_lyr, self._activity))
                roads_all = arcpy.management.MakeFeatureLayer(
                    roads_lyr, arcpy.CreateUniqueName("roads_all", "in_memory"))[0]
                roads_one = arcpy.management.MakeFeatureLayer(
                    roads_all, arcpy.CreateUniqueName("roads_one", "in_memory"), where_clause=where)[0]
                if int(arcpy.management.GetCount(roads_one)[0]) > 0:
                    one_layers.append((roads_all, roads_one))
                else:
                    arcpy.management.Delete(roads_one)
                    arcpy.management.Delete(roads_all)

            if not one_layers:
                for lyr in anno_layers:
                    originals.append((lyr, lyr.definitionQuery))
                    lyr.definitionQuery = "1=0"
                return originals

            for lyr in anno_layers:
                originals.append((lyr, lyr.definitionQuery))
                view = arcpy.management.MakeFeatureLayer(
                    lyr, arcpy.CreateUniqueName("anno_view", "in_memory"))[0]
                try:
                    matched_oids = set()
                    for _, roads_one in one_layers:
                        arcpy.management.SelectLayerByLocation(
                            view, "WITHIN_A_DISTANCE", roads_one, anno_distance,
                            selection_type="NEW_SELECTION")
                        if int(arcpy.management.GetCount(view)[0]) > 0:
                            oid_field = arcpy.Describe(lyr).OIDFieldName
                            for (oid,) in arcpy.da.SearchCursor(view, [oid_field]):
                                matched_oids.add(oid)

                    if not matched_oids:
                        lyr.definitionQuery = "1=0"
                        continue

                    oid_field = arcpy.Describe(lyr).OIDFieldName
                    clauses = []
                    for chunk in self._chunked(sorted(matched_oids), 900):
                        ids_txt = ",".join(str(x) for x in chunk)
                        clauses.append(f"{arcpy.AddFieldDelimiters(lyr, oid_field)} IN ({ids_txt})")
                    lyr.definitionQuery = " OR ".join(clauses)
                finally:
                    arcpy.management.Delete(view)
        finally:
            for roads_all, roads_one in one_layers:
                arcpy.management.Delete(roads_one)
                arcpy.management.Delete(roads_all)

        return originals

    @staticmethod
    def _restore_anno_def_queries(originals_list):
        for lyr, q in originals_list:
            try:
                lyr.definitionQuery = q or ""
            except Exception:
                pass

    @staticmethod
    def _get_reference_label_classes(aprx, messages, layer_name=LABEL_CLASS_REFERENCE_LAYER):
        try:
            ref_m = next((m for m in aprx.listMaps() if m.name == LABEL_CLASS_REFERENCE_MAP), None)
            ref_lyr = next((l for l in ref_m.listLayers() if l.name == layer_name), None) \
                if ref_m else None
            if not ref_lyr:
                messages.addMessage(
                    f"Label-class reference layer '{layer_name}' not found in "
                    f"map '{LABEL_CLASS_REFERENCE_MAP}' - auto street labeling skipped.")
                return None
            return ref_lyr.getDefinition("V3").labelClasses
        except Exception:
            return None

    @staticmethod
    def _apply_label_classes(new_lyr, ref_label_classes, messages):
        """Copy the reference layer's label classes onto new_lyr, with the
        roadclass/name field names substituted for whatever new_lyr
        actually has - skips gracefully if neither field resolves."""
        if not ref_label_classes:
            return
        try:
            real_fields = {f.name.lower(): f.name for f in arcpy.Describe(new_lyr).fields}
        except Exception:
            return
        roadclass_field = next((real_fields[c.lower()] for c in LABEL_ROADCLASS_FIELD_CANDIDATES
                                 if c.lower() in real_fields), None)
        name_field = next((real_fields[c.lower()] for c in LABEL_NAME_FIELD_CANDIDATES
                            if c.lower() in real_fields), None)
        if not roadclass_field or not name_field:
            return

        new_cim = new_lyr.getDefinition("V3")
        new_cim.labelClasses = ref_label_classes
        for lc in new_cim.labelClasses:
            where = lc.whereClause or ""
            if "=" in where:
                code = where.split("=")[-1].strip().strip("'")
                lc.whereClause = f"{roadclass_field} = '{code}'"
            lc.expression = f"$feature.{name_field}"
        new_lyr.setDefinition(new_cim)
        new_lyr.showLabels = True
        messages.addMessage(f"Applied road label classes to '{new_lyr.name}' "
                             f"(roadclass field '{roadclass_field}', name field '{name_field}').")

    def _swap_in_beat_only_layers(self, mf, fit_layers, beat_field, beat_text, scratch_gdb, ref_label_classes,
                                   messages, hide_original=True, highlight_style=None, roadclass_field=None,
                                   ref_renderer=None, bpx_points=False, dla_passes=False):
        """Show this beat's features on the map: extract them to a fresh,
        real (not in_memory - addDataFromPath doesn't handle that reliably)
        feature class, add it, apply labels, and swap it in - torn down and
        recreated fresh every single page. Both definitionQuery and
        selection-based isolation caused real rendering problems in this
        environment (a stale defQuery once hard-locked a layer to one beat;
        selecting features made the whole layer vanish, likely a "show only
        selected" map setting) - a physically separate layer sidesteps both.

        Default behaviour (hide_original=True, highlight_style=None,
        roadclass_field=None): copies the original layer's own symbology
        onto the extract and hides the original - the original
        per-activity design.

        Salt/Plow road-class mode passes hide_original=False plus either
        roadclass_field (colors the extract per ROADCLASS_SYMBOLOGY, via
        _apply_roadclass_symbology) or highlight_style (a flat color/width
        dict, via _apply_simple_renderer) - the original stays visible as
        background context either way, with the extract on top. This
        replaced an earlier persistent-layer design (one highlight layer
        created once per run, refreshed via DeleteRows+Append every beat)
        that worked in every isolated test but never showed up at all in a
        real, live Pro session's Contents pane - reverted to this
        proven-simple create-fresh-every-page pattern instead of chasing
        that further (2026-09-29). Building a per-road-class renderer this
        way is now safe (unlike the persistent design) because it's applied
        to a brand new, one-time-use layer every single beat, never
        rebuilt repeatedly on the same reused layer - that repeated-rebuild
        pattern was the likely cause of an earlier intermittent
        `AttributeError: 'SimpleRenderer' object has no attribute 'groups'`
        crash on real, live 221-beat runs.

        Self-heals leftover "beat_only"-prefixed layers from an earlier
        page before adding this page's own, since a real live Pro session
        sometimes doesn't actually remove a layer when asked (silently, no
        exception) - confirmed 2026-09-29: layers like "beat_only2"/
        "beat_only3" piled up across pages in a real run, each still
        carrying its own bold labels, which is exactly what "duplicate
        labels" looks like page after page. Never reproduced in an isolated
        script (same category as other live-session-only issues this tool
        has hit), so fixed by making every page self-correct rather than
        by chasing the exact cause further."""
        for stale in list(mf.map.listLayers()):
            if stale.name.startswith("beat_only"):
                try:
                    mf.map.removeLayer(stale)
                except Exception:
                    pass
        beat_safe = beat_text.replace("'", "''")
        records = []
        for lyr in fit_layers:
            fld = arcpy.AddFieldDelimiters(lyr, beat_field)
            where = self._and_where(f"{fld} = '{beat_safe}'", self._contracted_clause(lyr, self._activity))
            out_fc = arcpy.CreateUniqueName("beat_only", scratch_gdb)
            try:
                arcpy.analysis.Select(lyr, out_fc, where)
                new_lyr = mf.map.addDataFromPath(out_fc)
                if roadclass_field:
                    self._apply_roadclass_symbology(new_lyr, roadclass_field)
                    try:
                        mf.map.moveLayer(lyr, new_lyr, "BEFORE")
                    except Exception:
                        pass
                elif highlight_style:
                    self._apply_simple_renderer(new_lyr, highlight_style["color"], highlight_style["width"])
                    try:
                        mf.map.moveLayer(lyr, new_lyr, "BEFORE")
                    except Exception:
                        pass
                elif bpx_points:
                    # BPX: this beat's ACTIVE points, one symbol per type
                    self._apply_bpx_symbology(new_lyr)
                elif dla_passes:
                    # DLA: 1 pass red / 2 passes green
                    self._apply_dla_symbology(new_lyr)
                    try:
                        mf.map.moveLayer(lyr, new_lyr, "BEFORE")
                    except Exception:
                        pass
                elif ref_renderer:
                    # Sidewalk mode: (renderer CIM, field candidates) copied
                    # from the Sidewalks map's reference layer
                    self._apply_reference_renderer(new_lyr, ref_renderer[0], ref_renderer[1])
                    try:
                        mf.map.moveLayer(lyr, new_lyr, "BEFORE")
                    except Exception:
                        pass
                else:
                    try:
                        new_lyr.symbology = lyr.symbology
                    except Exception:
                        pass
                # Always label the highlight extract with the BOLD ("_v2")
                # reference - even in road-class mode, where the background
                # layer ALSO labels the same streets, but with the Regular-
                # weight ("_basemap") reference. Tried skipping this here to
                # avoid literal duplicate labels, but that lost the bold
                # emphasis Ramon wants specifically on the beat's own
                # streets (2026-09-29) - bold-on-top-of-regular is a
                # deliberate, visually distinct pairing (this beat's route
                # vs. background context), not the same kind of duplicate
                # as two identically-styled labels stacked on each other.
                self._apply_label_classes(new_lyr, ref_label_classes, messages)
                if hide_original:
                    lyr.visible = False
                new_lyr.visible = True
                records.append((lyr, new_lyr, out_fc))
            except Exception:
                # extraction/add failed for this layer - leave it as-is rather
                # than risk a half-done swap
                if arcpy.Exists(out_fc):
                    arcpy.management.Delete(out_fc)
        return records

    def _setup_roadclass_legend_reference(self, mf, fit_layers, roadclass_field, default_gdb, layout_label, messages):
        """A single, hidden, run-scoped layer whose only job is to give the
        Legend something to point at showing just ART/COL/LOC (not the
        background TCL_WINTER_OPERATIONS entry) - built once, never
        refreshed/rebuilt afterward, unlike the earlier per-beat-rebuilt
        persistent design that caused problems. layout_label must be
        unique per call within a run (see _swap_in_beat_only_layers docstring
        note on the earlier duplicate-name bug when Landscape/Portrait share
        a map)."""
        lyr = fit_layers[0]
        fld = arcpy.AddFieldDelimiters(lyr, roadclass_field)
        codes = "', '".join(ROADCLASS_SYMBOLOGY.keys())
        where = f"{fld} IN ('{codes}')"
        out_fc = arcpy.CreateUniqueName("roadclass_legend_ref", default_gdb)
        arcpy.analysis.Select(lyr, out_fc, where)
        new_lyr = mf.map.addDataFromPath(out_fc)
        new_lyr.name = f"{ROUTE_HIGHLIGHT_LAYER_NAME_PREFIX} ({layout_label})"
        self._apply_roadclass_symbology(new_lyr, roadclass_field)
        new_lyr.visible = False
        messages.addMessage(f"Built road-class legend reference layer '{new_lyr.name}'.")
        return new_lyr, out_fc

    @staticmethod
    def _teardown_roadclass_legend_reference(mf, layer_and_fc):
        if not layer_and_fc:
            return
        new_lyr, out_fc = layer_and_fc
        try:
            mf.map.removeLayer(new_lyr)
        except Exception:
            pass

    @staticmethod
    def _apply_roadclass_symbology(lyr, roadclass_field):
        """Unique-Value renderer keyed on roadclass_field, colored/weighted
        per ROADCLASS_SYMBOLOGY, via the higher-level arcpy.mp symbology API
        (matches what the Pro UI itself uses) rather than hand-built raw
        CIM - a hand-built CIMUniqueValueRenderer was tried first and
        silently failed to draw any real features despite passing every
        other check. Must be applied to a FRESH layer, once - never rebuilt
        repeatedly on the same reused layer (see _swap_in_beat_only_layers
        docstring)."""
        sym = lyr.symbology
        sym.updateRenderer("UniqueValueRenderer")
        sym.renderer.fields = [roadclass_field]
        for g in sym.renderer.groups:
            for it in g.items:
                code = it.label
                style = ROADCLASS_SYMBOLOGY.get(code, ROADCLASS_DEFAULT_SYMBOLOGY)
                it.symbol.color = {"RGB": style["color"]}
                it.symbol.size = style["width"]
                it.label = ROADCLASS_LABELS.get(code, code)
        lyr.symbology = sym

    def _style_background(self, fit_layers, original_renderers, basemap_label_classes, messages):
        """Full street network kept visible behind the beat route as a thin
        plain gray line (BACKGROUND_STREET_SYMBOLOGY - per Ramon, all
        activities) with the regular-weight street labels. Each layer's own
        renderer and labeling are saved into original_renderers (once) so
        execute() can put them back after the run."""
        for lyr in fit_layers:
            if not any(saved[0] is lyr or saved[0].name == lyr.name for saved in original_renderers):
                cim = lyr.getDefinition("V3")
                original_renderers.append((lyr, cim.renderer, cim.labelClasses, lyr.showLabels, lyr.visible))
            # The real layer is HIDDEN for the run - its background lines
            # never showed up in a live Pro export no matter how it was
            # styled (hand-built CIM or the symbology API, 2026-10-05), while
            # fresh per-page copies always draw. So the background comes
            # from a fresh copy every page (_add_background_label_layer),
            # same as the beat route itself. Turned back on at end of run.
            lyr.visible = False
            if not any(b is lyr or b.name == lyr.name for b in self._bg_layers):
                self._bg_layers.append(lyr)
        self._bg_label_classes = basemap_label_classes

    def _add_background_label_layer(self, mf, beat_layer_records, messages):
        """Per page: a fresh copy of the streets around the page (arcpy.env
        .extent, so it's quick) drawn as the thin gray background line
        (BACKGROUND_STREET_SYMBOLOGY), carrying the regular-weight street
        labels minus every street name the beat's own bold labels already
        show (per Ramon, those don't need repeating). Placed under the beat
        route. Returned as records in the same shape as
        _swap_in_beat_only_layers' so _restore_after_beat_only_layers tears
        it down with the rest of the page."""
        if not self._bg_layers:
            return []
        names = set()
        for _, _, out_fc in beat_layer_records:
            fields = self._layer_fields(out_fc)
            name_f = next((fields[c.lower()] for c in LABEL_NAME_FIELD_CANDIDATES if c.lower() in fields), None)
            if not name_f:
                continue
            with arcpy.da.SearchCursor(out_fc, [name_f]) as cur:
                names.update(str(n).strip() for (n,) in cur if n and str(n).strip())

        # page extent, padded generously since the frame may be rotated
        page_ext = self._extent_with_margin(mf.camera.getExtent(), 1.6)
        records = []
        for lyr in self._bg_layers:
            fields = self._layer_fields(lyr)
            name_f = next((fields[c.lower()] for c in LABEL_NAME_FIELD_CANDIDATES if c.lower() in fields), None)
            out_fc = arcpy.CreateUniqueName("beat_only_background", arcpy.env.scratchGDB)
            try:
                with arcpy.EnvManager(extent=page_ext):
                    arcpy.analysis.Select(lyr, out_fc)
                # BPX: bold the streets this beat's points sit on (their own
                # fresh label layer); those names then come off the regular
                # background labels like any other bold street
                if self._activity == "BPX" and beat_layer_records and name_f:
                    bold_names = self._bpx_street_names(beat_layer_records[0][2], out_fc, name_f, messages)
                    if bold_names:
                        names |= bold_names
                        bold_rec = self._add_bold_street_labels(mf, out_fc, name_f, bold_names, lyr, messages)
                        if bold_rec:
                            records.append(bold_rec)
                new_lyr = mf.map.addDataFromPath(out_fc)
                self._apply_simple_renderer(
                    new_lyr, BACKGROUND_STREET_SYMBOLOGY["color"], BACKGROUND_STREET_SYMBOLOGY["width"])
                if self._bg_label_classes:
                    self._apply_label_classes(new_lyr, self._bg_label_classes, messages)
                    if name_f and names:
                        # skip names the beat's bold labels already show -
                        # set once on this fresh layer, never re-edited
                        quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in sorted(names))
                        exclude = f"{name_f} IS NULL OR {name_f} NOT IN ({quoted})"
                        cim = new_lyr.getDefinition("V3")
                        for lc in cim.labelClasses or []:
                            lc.whereClause = self._and_where(lc.whereClause, exclude) or ""
                        new_lyr.setDefinition(cim)
                    if self._activity == "DLA":
                        # DLA: local roads with no DLA beat aren't serviced -
                        # leave them unlabeled (per Ramon 2026-10-13)
                        self._skip_unserviced_local_labels(new_lyr, fields)
                new_lyr.visible = True
                # under the beat route
                if beat_layer_records:
                    try:
                        mf.map.moveLayer(beat_layer_records[-1][1], new_lyr, "AFTER")
                    except Exception:
                        pass
                records.append((lyr, new_lyr, out_fc))
            except Exception as e:
                messages.addWarningMessage(f"Background streets skipped on this page: {e}")
                if arcpy.Exists(out_fc):
                    arcpy.management.Delete(out_fc)
        return records

    @staticmethod
    def _skip_unserviced_local_labels(new_lyr, fields):
        """Background labels: drop local (LOC) roads with no DLA beat. Set
        once on the fresh per-page layer, never re-edited."""
        rc_f = next((fields[c.lower()] for c in LABEL_ROADCLASS_FIELD_CANDIDATES if c.lower() in fields), None)
        beat_f = next((fields[c.lower()] for c in BEAT_FIELD_CANDIDATES["DLA"] if c.lower() in fields), None)
        if not rc_f or not beat_f:
            return
        blanks = ", ".join(f"'{v}'" for v in sorted(BLANK_FILTER_VALUES))
        keep = f"({rc_f} IS NULL OR {rc_f} <> 'LOC' OR ({beat_f} IS NOT NULL AND {beat_f} NOT IN ({blanks})))"
        cim = new_lyr.getDefinition("V3")
        for lc in cim.labelClasses or []:
            lc.whereClause = ExportBeatMapSeries._and_where(lc.whereClause, keep) or ""
        new_lyr.setDefinition(cim)

    @staticmethod
    def _norm_street(name):
        """Street names written two ways - TS_BS_PXO "STEELES AVE EAST" vs
        TCL "Steeles Ave E" - compared in one form."""
        if not name:
            return ""
        n = " ".join(str(name).upper().replace(".", " ").replace(",", " ").split())
        for full, short in ((" EAST", " E"), (" WEST", " W"), (" NORTH", " N"), (" SOUTH", " S")):
            if n.endswith(full):
                n = n[: -len(full)] + short
        return n

    def _bpx_street_names(self, points_fc, streets_fc, name_f, messages):
        """Road names (as written in the streets layer) of the street each
        BPX point sits on: streets within BPX_STREET_SEARCH_M of the point;
        the one whose name matches the point's main_street /
        main_street_segment wins (a stop at an intersection is near both
        streets), else the nearest."""
        near_tbl = arcpy.CreateUniqueName("bpx_near", "in_memory")
        try:
            arcpy.analysis.GenerateNearTable(points_fc, streets_fc, near_tbl,
                                             search_radius=f"{BPX_STREET_SEARCH_M} Meters",
                                             closest="ALL", closest_count=4)
            pt_fields = self._layer_fields(points_fc)
            main_fields = [pt_fields[f] for f in ("main_street", "main_street_segment") if f in pt_fields]
            pt_names = {}
            with arcpy.da.SearchCursor(points_fc, ["OID@"] + main_fields) as cur:
                for row in cur:
                    pt_names[row[0]] = {self._norm_street(v) for v in row[1:] if v}
            st_names = {oid: n for oid, n in arcpy.da.SearchCursor(streets_fc, ["OID@", name_f])}
            cands = {}
            with arcpy.da.SearchCursor(near_tbl, ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as cur:
                for pt, st, d in cur:
                    cands.setdefault(pt, []).append((d, st))
            chosen = set()
            by_name = 0
            for pt, lst in cands.items():
                lst.sort()
                wanted = pt_names.get(pt, set())
                pick = next((st for d, st in lst if self._norm_street(st_names.get(st)) in wanted), None)
                if pick is not None:
                    by_name += 1
                else:
                    pick = lst[0][1]
                n = st_names.get(pick)
                if n:
                    chosen.add(str(n).strip())
            messages.addMessage(f"BPX bold streets: {len(chosen)} street(s) from {len(cands)} point(s) "
                                f"({by_name} matched by name, {len(cands) - by_name} nearest).")
            return chosen
        except Exception as e:
            messages.addWarningMessage(f"Couldn't work out which streets to bold for BPX: {e}")
            return set()
        finally:
            if arcpy.Exists(near_tbl):
                arcpy.management.Delete(near_tbl)

    def _add_bold_street_labels(self, mf, streets_fc, name_f, bold_names, src_lyr, messages):
        """Fresh per-page label-only layer: just those streets, bold labels
        (same classes the beat route uses), invisible line."""
        if not self._bold_label_classes:
            return None
        out_fc = arcpy.CreateUniqueName("beat_only_bold", arcpy.env.scratchGDB)
        try:
            quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in sorted(bold_names))
            arcpy.analysis.Select(streets_fc, out_fc, f"{name_f} IN ({quoted})")
            new_lyr = mf.map.addDataFromPath(out_fc)
            self._apply_simple_renderer(new_lyr, [0, 0, 0, 0], 0.1)
            self._apply_label_classes(new_lyr, self._bold_label_classes, messages)
            new_lyr.visible = True
            return (src_lyr, new_lyr, out_fc)
        except Exception as e:
            messages.addWarningMessage(f"Bold BPX street labels skipped on this page: {e}")
            if arcpy.Exists(out_fc):
                arcpy.management.Delete(out_fc)
            return None

    @staticmethod
    def _get_reference_renderer(aprx, layer_name, messages):
        """The renderer CIM of a reference layer in the Sidewalks map, or None."""
        ref_m = next((m for m in aprx.listMaps() if m.name == LABEL_CLASS_REFERENCE_MAP), None)
        ref_lyr = next((l for l in ref_m.listLayers() if l.name == layer_name), None) if ref_m else None
        if not ref_lyr:
            messages.addMessage(f"Symbology reference layer '{layer_name}' not found in map "
                                f"'{LABEL_CLASS_REFERENCE_MAP}'.")
            return None
        return ref_lyr.getDefinition("V3").renderer

    @staticmethod
    def _apply_reference_renderer(lyr, renderer, field_candidates):
        """Put a copied renderer on lyr, keyed on whichever of
        field_candidates lyr actually has (e.g. winter_rdcls on the hosted
        reference -> winter_roadclass on TCL_WINTER_OPERATIONS). Returns
        False (lyr untouched) if none of the fields resolve."""
        real_fields = ExportBeatMapSeries._layer_fields(lyr)
        field = next((real_fields[c.lower()] for c in field_candidates if c.lower() in real_fields), None)
        if not field:
            return False
        cim = lyr.getDefinition("V3")
        try:
            renderer = copy.deepcopy(renderer)
        except Exception:
            # marker symbols (e.g. Ramon's BPX points) hold geometry objects
            # deepcopy can't clone - setDefinition serializes the renderer
            # anyway, so reusing the same object per layer is safe
            pass
        if hasattr(renderer, "fields"):
            renderer.fields = [field]
        cim.renderer = renderer
        lyr.setDefinition(cim)
        return True

    @staticmethod
    def _rgb_key(class_symbol_ref):
        """Color of a class's first stroke, rounded - used to pair up
        classes between two renderers that share a color scheme."""
        try:
            return tuple(round(v) for v in class_symbol_ref.symbol.symbolLayers[0].color.values[:3])
        except Exception:
            return None

    def _sidewalk_legend_renderer(self, side_renderer, legend_renderer):
        """The sdwk_side renderer, keeping only the classes the _legend
        layer also shows (same color) and using its labels/order."""
        labels = {}
        order = []
        for g in legend_renderer.groups or []:
            for c in g.classes:
                key = self._rgb_key(c.symbol)
                if key and key not in labels:
                    labels[key] = c.label
                    order.append(key)
        out = copy.deepcopy(side_renderer)
        for g in out.groups or []:
            kept = [c for c in g.classes if self._rgb_key(c.symbol) in labels]
            for c in kept:
                c.label = labels[self._rgb_key(c.symbol)]
            kept.sort(key=lambda c: order.index(self._rgb_key(c.symbol)))
            g.classes = kept
        out.useDefaultSymbol = False
        return out

    def _setup_sidewalk_legend_reference(self, mf, fit_layers, legend_renderer, side_field, layout_label, messages):
        """Hidden, run-scoped layer that only exists so the Legend shows the
        four sidewalk types - same idea as _setup_roadclass_legend_reference.
        Lives in scratchGDB (not the project gdb, see the 2026-09-29
        stale-layer bug)."""
        lyr = fit_layers[0]
        fld = arcpy.AddFieldDelimiters(lyr, side_field)
        out_fc = arcpy.CreateUniqueName("sidewalk_legend_ref", arcpy.env.scratchGDB)
        arcpy.analysis.Select(lyr, out_fc, f"{fld} > 0")
        new_lyr = mf.map.addDataFromPath(out_fc)
        new_lyr.name = f"{SIDEWALK_LEGEND_LAYER_NAME_PREFIX} ({layout_label})"
        self._apply_reference_renderer(new_lyr, legend_renderer, [side_field])
        new_lyr.visible = False
        messages.addMessage(f"Built sidewalk legend reference layer '{new_lyr.name}'.")
        return new_lyr, out_fc

    def _setup_flat_route_legend_reference(self, mf, fit_layers, beat_field, style, messages):
        """Hidden, run-scoped layer named style["label"] in the flat route
        style, so the legend shows the route color instead of the fit
        layer's own symbology. One per map - Landscape/Portrait sharing a
        map share it."""
        existing = next((l for l in mf.map.listLayers() if l.name == style["label"]), None)
        if existing:
            return existing, None
        lyr = fit_layers[0]
        fld = arcpy.AddFieldDelimiters(lyr, beat_field)
        out_fc = arcpy.CreateUniqueName("route_legend_ref", arcpy.env.scratchGDB)
        arcpy.analysis.Select(lyr, out_fc, f"{fld} IS NOT NULL")
        new_lyr = mf.map.addDataFromPath(out_fc)
        new_lyr.name = style["label"]
        self._apply_simple_renderer(new_lyr, style["color"], style["width"])
        cim = new_lyr.getDefinition("V3")
        cim.renderer.label = style["label"]
        new_lyr.setDefinition(cim)
        new_lyr.visible = False
        messages.addMessage(f"Built route legend reference layer '{new_lyr.name}'.")
        return new_lyr, out_fc

    # ---------------- DLA (passes) ----------------
    @staticmethod
    def _apply_dla_symbology(lyr):
        """Line coloured by dla_pass (DLA_PASS_SYMBOLOGY) via the symbology
        API on a fresh layer - same pattern as _apply_roadclass_symbology,
        proven live. Other pass values (a few 0s) get no class."""
        sym = lyr.symbology
        sym.updateRenderer("UniqueValueRenderer")
        sym.renderer.fields = [ExportBeatMapSeries._layer_fields(lyr).get(DLA_PASS_FIELD, DLA_PASS_FIELD)]
        for g in sym.renderer.groups:
            for it in g.items:
                style = DLA_PASS_SYMBOLOGY.get(str(it.values[0][0]))
                if not style:
                    continue
                it.symbol.color = {"RGB": style["color"]}
                it.symbol.size = style["width"]
                it.label = style["label"]
        lyr.symbology = sym

    def _setup_dla_legend_reference(self, mf, fit_layers, layout_label, messages):
        """Hidden helper so the legend shows just "1 Pass" / "2 Passes"."""
        lyr = fit_layers[0]
        fld = arcpy.AddFieldDelimiters(lyr, self._layer_fields(lyr).get(DLA_PASS_FIELD, DLA_PASS_FIELD))
        codes = ", ".join(DLA_PASS_SYMBOLOGY)
        out_fc = arcpy.CreateUniqueName("dla_legend_ref", arcpy.env.scratchGDB)
        arcpy.analysis.Select(lyr, out_fc, f"{fld} IN ({codes})")
        new_lyr = mf.map.addDataFromPath(out_fc)
        new_lyr.name = f"{DLA_LEGEND_LAYER_NAME_PREFIX} ({layout_label})"
        self._apply_dla_symbology(new_lyr)
        new_lyr.visible = False
        messages.addMessage(f"Built DLA legend reference layer '{new_lyr.name}'.")
        return new_lyr, out_fc

    def _rebuild_dla_stats_table(self, layout, mf, fit_layers, beat_field, kms_field, default_gdb, messages):
        """Beat Stats for DLA: one row per beat per number of passes,
        BEAT | PASSES | KMS (dla_kms summed - already pass-weighted). Fixed
        schema, rows replaced each run (schema-lock lesson)."""
        el = self._find_table_frame(layout, "Beat Stats Table")
        if not el:
            return
        out_path = os.path.join(default_gdb, BEAT_STATS_DLA_TABLE_NAME)
        if not arcpy.Exists(out_path):
            gdb, name = os.path.split(out_path)
            arcpy.management.CreateTable(gdb, name)
            arcpy.management.AddField(out_path, "BEAT", "TEXT", field_length=255, field_alias="BEAT")
            arcpy.management.AddField(out_path, "PASSES", "SHORT", field_alias="PASSES")
            arcpy.management.AddField(out_path, "KMS", "DOUBLE", field_alias="KMS")
        totals = {}
        for lyr in fit_layers:
            f = self._layer_fields(lyr)
            if beat_field.lower() not in f or DLA_PASS_FIELD not in f:
                continue
            cols = [f[beat_field.lower()], f[DLA_PASS_FIELD]] + ([f[kms_field.lower()]] if kms_field and kms_field.lower() in f else [])
            with arcpy.da.SearchCursor(lyr, cols) as cur:
                for row in cur:
                    b = str(row[0]).strip() if row[0] is not None else ""
                    p = row[1]
                    if b.upper() in BLANK_FILTER_VALUES or p is None or str(p) not in DLA_PASS_SYMBOLOGY:
                        continue
                    k = (row[2] or 0) if len(row) > 2 else 0
                    totals[(b, int(p))] = totals.get((b, int(p)), 0) + k
        arcpy.management.DeleteRows(out_path)
        with arcpy.da.InsertCursor(out_path, ["BEAT", "PASSES", "KMS"]) as icur:
            for (b, p) in sorted(totals):
                icur.insertRow((b, p, round(totals[(b, p)], 2)))
        tbl = self._ensure_table_in_map(mf.map, out_path, BEAT_STATS_DLA_TABLE_NAME)
        el.table = tbl
        self._set_table_fields(el, ["BEAT", "PASSES", "KMS"])
        el.visible = True
        messages.addMessage(f"Beat Stats Table: DLA KMS by passes for {len({b for b, _ in totals})} beat(s).")

    # ---------------- BPX (TS_BS_PXO points) ----------------
    @staticmethod
    def _find_or_add_layer(map_obj, sde_path):
        """The map's layer for this dataset (matched on the dataset name at
        the end of its data source), adding it if the map doesn't have one.
        Returns (layer, added_by_us)."""
        ds = sde_path.split("\\")[-1].lower()
        for lyr in map_obj.listLayers():
            try:
                if lyr.isFeatureLayer and lyr.dataSource.lower().endswith(ds):
                    return lyr, False
            except Exception:
                continue
        return map_obj.addDataFromPath(sde_path), True

    @staticmethod
    def _find_bpx_reference_renderer(aprx, preferred_map, messages):
        """Ramon's own TS_BS_PXO symbology (per Ramon 2026-10-08: "make it
        symbology i applied"), read from the live project at run time so
        whatever he sets is what prints: a TS_BS_PXO layer in the map being
        printed first, then any other map. Must be a unique-value renderer on
        the type field. None -> built-in BPX_TYPE_SYMBOLOGY. Call BEFORE the
        tool adds its own TS_BS_PXO layer (that one has Pro's default look)."""
        # table name only - Ramon's styled layer can point at production
        # (winterops.gisadmin.TS_BS_PXO) while the tool reads QA
        table = BPX_POINTS_SDE_PATH.split(".")[-1].lower()       # "ts_bs_pxo"
        maps = [preferred_map] + [m for m in aprx.listMaps() if m.name != preferred_map.name]
        for m in maps:
            for lyr in m.listLayers():
                try:
                    if not lyr.isFeatureLayer or lyr.dataSource.lower().split(".")[-1] != table:
                        continue
                    if lyr.name.startswith(TOOL_LAYER_NAME_PREFIXES) or lyr.name.startswith("beat_only"):
                        continue
                    r = lyr.getDefinition("V3").renderer
                except Exception:
                    continue
                if type(r).__name__ == "CIMUniqueValueRenderer" and \
                        [f.lower() for f in (r.fields or [])] == [BPX_TYPE_FIELD]:
                    messages.addMessage(f"BPX symbology copied from '{lyr.name}' in map '{m.name}'.")
                    return r
        messages.addMessage("No styled TS_BS_PXO layer found in the project - using the built-in BPX symbols.")
        return None

    def _apply_bpx_symbology(self, lyr):
        """One symbol per type: copied from Ramon's TS_BS_PXO layer when
        there is one (self._bpx_ref_renderer - copying a real renderer is
        the pattern proven to draw live, as in Sidewalk mode), else built-in
        BPX_TYPE_SYMBOLOGY via the symbology API."""
        if self._bpx_ref_renderer is not None and \
                self._apply_reference_renderer(lyr, self._bpx_ref_renderer, [BPX_TYPE_FIELD]):
            return
        sym = lyr.symbology
        sym.updateRenderer("UniqueValueRenderer")
        sym.renderer.fields = [BPX_TYPE_FIELD]
        for g in sym.renderer.groups:
            for it in g.items:
                style = BPX_TYPE_SYMBOLOGY.get(str(it.values[0][0]))
                if not style:
                    continue
                it.symbol.applySymbolFromGallery(style["shape"])
                it.symbol.color = {"RGB": style["color"]}
                it.symbol.size = style["size"]
                it.label = style["label"]
        lyr.symbology = sym

    def _setup_bpx_legend_reference(self, mf, bpx_layer, layout_label, messages):
        """Hidden helper layer so the legend shows the three point types."""
        out_fc = arcpy.CreateUniqueName("bpx_legend_ref", arcpy.env.scratchGDB)
        arcpy.analysis.Select(bpx_layer, out_fc, self._contracted_clause(bpx_layer, "BPX"))
        new_lyr = mf.map.addDataFromPath(out_fc)
        new_lyr.name = f"{BPX_LEGEND_LAYER_NAME_PREFIX} ({layout_label})"
        self._apply_bpx_symbology(new_lyr)
        # only the real types in the legend - no "<all other values>" row
        # (Ramon's renderer has it on; every printed point is one of the 3)
        cim = new_lyr.getDefinition("V3")
        if hasattr(cim.renderer, "useDefaultSymbol"):
            cim.renderer.useDefaultSymbol = False
            new_lyr.setDefinition(cim)
        new_lyr.visible = False
        messages.addMessage(f"Built BPX legend reference layer '{new_lyr.name}'.")
        return new_lyr, out_fc

    @staticmethod
    def _ensure_bpx_stats_schema(out_path):
        """Fixed schema, created once, rows replaced each run (same
        schema-lock reason as _ensure_beat_stats_schema). Column headings
        come from the field aliases."""
        if arcpy.Exists(out_path):
            return
        gdb, name = os.path.split(out_path)
        arcpy.management.CreateTable(gdb, name)
        arcpy.management.AddField(out_path, "BEAT", "TEXT", field_length=255, field_alias="BEAT")
        for style in BPX_TYPE_SYMBOLOGY.values():
            arcpy.management.AddField(out_path, style["column"], "LONG", field_alias=style["heading"])

    def _rebuild_bpx_stats_table(self, layout, mf, bpx_layer, beat_field, default_gdb, messages):
        """Beat Stats for BPX: one row per beat, ACTIVE point counts by type
        (BEAT | BUS STOPS | PXO | SIGNALS)."""
        el = self._find_table_frame(layout, "Beat Stats Table")
        if not el:
            return
        out_path = os.path.join(default_gdb, BEAT_STATS_BPX_TABLE_NAME)
        self._ensure_bpx_stats_schema(out_path)
        fields = self._layer_fields(bpx_layer)
        counts = {}
        with arcpy.da.SearchCursor(bpx_layer, [fields[beat_field.lower()], fields[BPX_TYPE_FIELD]],
                                   self._contracted_clause(bpx_layer, "BPX")) as cur:
            for b, t in cur:
                b = str(b).strip() if b is not None else ""
                if b.upper() in BLANK_FILTER_VALUES:
                    continue
                row = counts.setdefault(b, {k: 0 for k in BPX_TYPE_SYMBOLOGY})
                if str(t) in row:
                    row[str(t)] += 1
        cols = [s["column"] for s in BPX_TYPE_SYMBOLOGY.values()]
        arcpy.management.DeleteRows(out_path)
        with arcpy.da.InsertCursor(out_path, ["BEAT"] + cols) as icur:
            for b in sorted(counts):
                icur.insertRow([b] + [counts[b][k] for k in BPX_TYPE_SYMBOLOGY])
        tbl = self._ensure_table_in_map(mf.map, out_path, BEAT_STATS_BPX_TABLE_NAME)
        el.table = tbl
        self._set_table_fields(el, ["BEAT"] + cols)
        el.visible = True
        messages.addMessage(f"Beat Stats Table: ACTIVE BPX point counts by type for {len(counts)} beat(s).")

    @staticmethod
    def _hide_legend_item_heading(layout, layer_name):
        """Legend entry shows just the classes, not the helper layer's name."""
        leg = next((e for e in layout.listElements() if type(e).__name__ == "LegendElement"), None)
        if not leg:
            return
        cim = leg.getDefinition("V3")
        for it in cim.items or []:
            if getattr(it, "name", None) == layer_name:
                it.showLayerName = False
                it.showHeading = False
        leg.setDefinition(cim)

    def _restore_after_beat_only_layers(self, mf, records):
        for lyr, new_lyr, out_fc in records:
            try:
                mf.map.removeLayer(new_lyr)
            except Exception:
                pass
            try:
                if arcpy.Exists(out_fc):
                    arcpy.management.Delete(out_fc)
            except Exception:
                pass
            try:
                # real background layers stay hidden for the whole run (the
                # per-page copy stands in for them, see _style_background) -
                # they're turned back on by the end-of-run restore
                if any(b is lyr or b.name == lyr.name
                       for b in list(self._bg_layers) + list(self._keep_hidden)):
                    continue
                if not lyr.visible:
                    lyr.visible = True
            except Exception:
                pass

    @staticmethod
    def _cleanup_stale_route_highlight_layers(map_obj, messages):
        """Only removes the LAYER reference, never the backing dataset -
        deliberately. Deleting the feature class here, synchronously, while
        running inside a live/CURRENT Pro session that may still have that
        exact dataset open in its rendering pipeline (it was sitting visible
        in the Contents pane from whatever earlier run left it behind) risks
        a same-thread deadlock: arcpy.management.Delete waiting on a lock
        that only releases via a UI repaint, on the one thread that's
        currently blocked running this tool. Orphaned feature classes left
        on disk from this are harmless clutter, not a correctness problem -
        clean them up separately (Catalog pane, or a script, with Pro
        closed) rather than fixing it here."""
        stale = [l for l in map_obj.listLayers()
                 if l.name.startswith(TOOL_LAYER_NAME_PREFIXES)]
        if not stale:
            return
        removed = 0
        for l in stale:
            try:
                map_obj.removeLayer(l)
                removed += 1
            except Exception:
                pass
        messages.addWarningMessage(
            f"Removed {removed} stale '{ROUTE_HIGHLIGHT_LAYER_NAME_PREFIX}' layer reference(s) left over "
            f"from an earlier run (teardown must not have completed that time) - their backing feature "
            f"class(es) were left on disk on purpose (not deleted here) and can be cleaned up separately.")

    # ---------------- activity-driven layout setup (once per run, not per page) ----------------
    def _find_beat_label_elements(self, layout):
        """The layouts have exactly one text element named 'BEAT NAME'."""
        return [e for e in layout.listElements("TEXT_ELEMENT") if e.name == "BEAT NAME"]

    def _set_beat_label_text(self, beat_label_elements, beat_text):
        for el in beat_label_elements:
            el.text = beat_text
            # Portrait's BEAT NAME was saved hidden (found 2026-10-13)
            el.visible = True

    @staticmethod
    def _right_align_beat_label(layout, mf, beat_label_elements):
        """Portrait: beat name on the title's line, its right edge just
        inside the map frame's right edge. The old spot above the North
        Arrow was 1.2" wide and long names ran past the border
        (BPX1110, 2026-10-13)."""
        title = next((e for e in layout.listElements("TEXT_ELEMENT")
                      if e.name.upper() in ("MAP TITLE", "MAP NAME")), None)
        right = mf.elementPositionX + mf.elementWidth - 0.15
        for el in beat_label_elements:
            el.elementPositionX = right - el.elementWidth
            if title:
                el.elementPositionY = title.elementPositionY

    def _position_beat_labels_above_north_arrow(self, layout, beat_label_elements, messages):
        north = next((e for e in layout.listElements() if e.name == "North Arrow"), None)
        if not north or not beat_label_elements:
            return
        arrow_center_x = north.elementPositionX + north.elementWidth / 2.0
        arrow_top_y = north.elementPositionY + north.elementHeight
        label_w, label_h = 1.2, 0.3
        gap = 0.05
        for el in beat_label_elements:
            el.elementWidth = label_w
            el.elementHeight = label_h
            el.elementPositionX = arrow_center_x - label_w / 2.0
            el.elementPositionY = arrow_top_y + gap
        messages.addMessage(f"Positioned beat label(s) above North Arrow on '{layout.name}'.")

    def _set_title_text(self, layout, title_text):
        for el in layout.listElements("TEXT_ELEMENT"):
            if el.name.upper() in ("MAP TITLE", "MAP NAME"):
                el.text = title_text

    def _rebuild_legend(self, layout, fit_layers, messages):
        """Rebuild the layout's legend to show exactly the layer(s) this
        run is actually using, dropping anything else and adding anything
        missing, then keep it in sync with layer visibility going forward."""
        leg = next((e for e in layout.listElements() if type(e).__name__ == "LegendElement"), None)
        if not leg:
            return

        fit_names = {l.name for l in fit_layers}

        cim = leg.getDefinition("V3")
        existing_names = [getattr(ci, "name", None) for ci in (cim.items or [])]
        # autoAdd=True (the default) makes the Legend automatically re-add
        # ANY visible map layer on its own, fighting any manual removal -
        # confirmed live 2026-09-29: a stale "TCL_WINTER_OPERATIONS" entry
        # kept reappearing after being explicitly filtered out, because it's
        # a visible background layer and autoAdd kept putting it straight
        # back. Since this method already fully manages legend contents
        # itself, turn autoAdd off so its own logic is authoritative.
        cim.autoAdd = False
        if cim.items:
            # Filter the CIM item list directly and commit once, rather
            # than calling leg.removeItem() per stale item in a loop - doing
            # that one at a time can invalidate the remaining wrapper
            # references mid-loop.
            cim.items = [ci for ci in cim.items if getattr(ci, "name", None) in fit_names]
        leg.setDefinition(cim)

        already_present = {n for n in existing_names if n in fit_names}
        for lyr in fit_layers:
            if lyr.name not in already_present:
                try:
                    leg.addItem(lyr)
                except Exception:
                    pass

        # NOT syncLayerVisibility=True: the real fit layers get hidden during
        # export in favor of a per-beat extracted proxy layer with matching
        # symbology (see _swap_in_beat_only_layers), so their legend entries
        # must stay regardless of that layer's visibility.
        leg.syncLayerVisibility = False
        # an item added for a hidden layer (the road-class / sidewalk legend
        # helper layers are always hidden) comes in with isVisible=False and
        # never draws on the page - force every kept item on
        cim = leg.getDefinition("V3")
        for ci in cim.items or []:
            ci.isVisible = True
        leg.setDefinition(cim)
        messages.addMessage(f"Legend rebuilt for: {sorted(fit_names)}")

    def _resolve_kms_field(self, fit_layers, activity):
        candidates = KMS_FIELD_CANDIDATES.get(activity, [])
        if not candidates or not fit_layers:
            return None
        # any beat layer, not just the first - the first can be a point
        # layer with no km field while the line layer has one
        for lyr in fit_layers:
            real_fields = self._layer_fields(lyr)
            for cand in candidates:
                if cand.lower() in real_fields:
                    return cand
        return None

    @staticmethod
    def _resolve_field(fit_layers, candidates):
        """Like _resolve_kms_field but returns the field's real casing (as
        Describe reports it), for fields used directly in cursors/where
        clauses against the source data rather than just display."""
        if not candidates or not fit_layers:
            return None
        try:
            real_fields = {f.name.lower(): f.name for f in arcpy.Describe(fit_layers[0]).fields}
        except Exception:
            return None
        for cand in candidates:
            if cand.lower() in real_fields:
                return real_fields[cand.lower()]
        return None

    @staticmethod
    def _make_line_symbol_ref(color, width):
        color_mod = importlib.import_module("arcpy.cim.CIMColor")
        stroke = arcpy.cim.CIMSymbols.CIMSolidStroke()
        stroke.enable = True
        stroke.color = color_mod.CIMRGBColor()
        stroke.color.values = color
        stroke.width = width
        line_symbol = arcpy.cim.CIMSymbols.CIMLineSymbol()
        line_symbol.symbolLayers = [stroke]
        ref = arcpy.cim.CIMSymbolizers.CIMSymbolReference()
        ref.symbol = line_symbol
        return ref

    @staticmethod
    def _is_line_layer(lyr):
        try:
            return arcpy.Describe(lyr).shapeType == "Polyline"
        except Exception:
            return False

    def _apply_simple_renderer(self, lyr, color, width):
        """Single-symbol line style. Goes through the arcpy.mp symbology API
        (the same path as Pro's Symbology pane) - a hand-built CIM renderer
        rendered fine in standalone scripts but silently drew NOTHING in a
        live Pro export (2026-10-05: background lines missing while their
        labels showed; same symptom as the hand-built UV renderer, see
        _apply_roadclass_symbology). Hand-built CIM is only the fallback."""
        try:
            sym = lyr.symbology
            if not hasattr(sym, "renderer"):
                raise RuntimeError("layer has no renderer")
            if sym.renderer.type != "SimpleRenderer":
                sym.updateRenderer("SimpleRenderer")
            sym.renderer.symbol.color = {"RGB": list(color)}
            sym.renderer.symbol.size = width
            lyr.symbology = sym
            # Guard: a just-added layer in a live Pro session can come back
            # with a POINT symbol (round black-outlined markers in the
            # route/background colors showed up in CT142.pdf, 2026-10-05,
            # though every layer here is a line). For a line layer, insist
            # on a line symbol.
            if self._is_line_layer(lyr) and type(
                    lyr.getDefinition("V3").renderer.symbol.symbol).__name__ == "CIMLineSymbol":
                return
            if not self._is_line_layer(lyr):
                return
        except Exception:
            pass
        cim_def = lyr.getDefinition("V3")
        renderer = arcpy.cim.CIMSymbolizers.CIMSimpleRenderer()
        renderer.symbol = self._make_line_symbol_ref(color, width)
        cim_def.renderer = renderer
        lyr.setDefinition(cim_def)

    @staticmethod
    def _find_table_frame(layout, name):
        """Exact name first, else Pro's duplicate-style name ("Equipment
        Table 1") - Portrait's only equipment table is named that way."""
        frames = [e for e in layout.listElements() if type(e).__name__ == "TableFrameElement"]
        return (next((e for e in frames if e.name == name), None)
                or next((e for e in frames if e.name.startswith(name + " ")), None))

    @staticmethod
    def _ensure_table_in_map(map_obj, table_path, table_name):
        """Add a table to the map if it isn't already there, and return the
        Table object either way."""
        existing = next((t for t in map_obj.listTables() if t.name == table_name), None)
        if existing:
            return existing
        map_obj.addDataFromPath(table_path)
        return next(t for t in map_obj.listTables() if t.name == table_name)

    @staticmethod
    def _set_table_frame_query(el, where_clause):
        """Custom attribute filter on a table frame - setQuery() only
        offers ALL_ROWS/MAPSERIES_ROWS/VISIBLE_ROWS, so this goes through
        the CIM's customWhereClause instead."""
        cim = el.getDefinition("V3")
        cim.customWhereClause = where_clause
        el.setDefinition(cim)

    @staticmethod
    def _ensure_beat_stats_schema(out_path):
        """Create the persistent Beat Stats table ONCE with a fixed schema
        (BEAT/KMS) if it doesn't exist yet. Never recreated after that -
        only its rows change - because arcpy.analysis.Statistics can't
        overwrite a table's schema in place once it's bound to a
        TableFrameElement in the map (schema lock), even with
        overwriteOutput=True."""
        if arcpy.Exists(out_path):
            return
        gdb, name = os.path.split(out_path)
        arcpy.management.CreateTable(gdb, name)
        arcpy.management.AddField(out_path, "BEAT", "TEXT", field_length=255)
        arcpy.management.AddField(out_path, "KMS", "DOUBLE")

    def _rebuild_beat_stats_table(self, layout, mf, fit_layers, beat_field, kms_field, default_gdb, messages):
        el = self._find_table_frame(layout, "Beat Stats Table")
        if not el:
            return
        if not fit_layers:
            el.visible = False
            return

        out_path = os.path.join(default_gdb, BEAT_STATS_TABLE_NAME)
        self._ensure_beat_stats_schema(out_path)

        # Always shown (per Ramon). KMS totals come only from the beat
        # layers that actually carry the km field - e.g. a visible point
        # layer that also has cycle_beat used to hide the whole table when
        # it happened to be checked first. No km field anywhere (BPX point
        # assets): still list the beat, KMS left blank.
        km_layers = [l for l in fit_layers
                     if kms_field and kms_field.lower() in self._layer_fields(l)]
        if not km_layers:
            beats = set()
            for lyr in fit_layers:
                beats.update(self._distinct_values(
                    [lyr], beat_field, self._contracted_clause(lyr, self._activity)))
            arcpy.management.DeleteRows(out_path)
            with arcpy.da.InsertCursor(out_path, ["BEAT", "KMS"]) as icur:
                for b in sorted(beats):
                    icur.insertRow((b, None))
            tbl = self._ensure_table_in_map(mf.map, out_path, BEAT_STATS_TABLE_NAME)
            el.table = tbl
            self._set_table_fields(el, ["BEAT", "KMS"])
            el.visible = True
            messages.addMessage("Beat Stats Table: no km field on the beat layer(s) - beats listed without KMS.")
            return

        # contracted-only views (no-op for activities that don't track it)
        views = []
        sources = []
        for lyr in km_layers:
            contracted = self._contracted_clause(lyr, self._activity)
            if contracted:
                v = arcpy.management.MakeFeatureLayer(
                    lyr, arcpy.CreateUniqueName("contracted_only", "in_memory"), contracted)[0]
                views.append(v)
                sources.append(v)
            else:
                sources.append(lyr)
        source_fc = sources[0]
        if len(sources) > 1:
            source_fc = arcpy.management.Merge(
                sources, arcpy.CreateUniqueName("beat_stats_src", "in_memory"))[0]
        scratch_stats = arcpy.CreateUniqueName("beat_stats_scratch", "in_memory")
        try:
            sum_field_name = f"SUM_{kms_field}"
            arcpy.analysis.Statistics(
                source_fc, scratch_stats,
                statistics_fields=[[kms_field, "SUM"]],
                case_field=beat_field)

            arcpy.management.DeleteRows(out_path)
            with arcpy.da.SearchCursor(scratch_stats, [beat_field, sum_field_name]) as scur, \
                    arcpy.da.InsertCursor(out_path, ["BEAT", "KMS"]) as icur:
                for beat_val, kms_val in scur:
                    icur.insertRow((beat_val, round(kms_val, 2) if kms_val is not None else None))
        finally:
            if len(sources) > 1:
                arcpy.management.Delete(source_fc)
            for v in views:
                arcpy.management.Delete(v)
            arcpy.management.Delete(scratch_stats)

        tbl = self._ensure_table_in_map(mf.map, out_path, BEAT_STATS_TABLE_NAME)
        el.table = tbl
        self._set_table_fields(el, ["BEAT", "KMS"])
        el.visible = True
        messages.addMessage(f"Rebuilt 'Beat Stats Table' from {[l.name for l in fit_layers]} "
                             f"grouped by '{beat_field}', summing '{kms_field}' (2 decimals, no frequency).")

    @staticmethod
    def _ensure_beat_stats_roadclass_schema(out_path, messages):
        """Same rationale as _ensure_beat_stats_schema: created once, rows
        only ever get deleted/reinserted, never the table itself. Field
        NAMES stay valid identifiers (no spaces); the Table Frame displays
        each field's ALIAS, which does have spaces, per Ramon's request."""
        aliases = {"BEAT": "BEAT", "ROAD_CLASS": "ROAD CLASS", "SALT_KMS": "SALT KMS", "PLOW_KMS": "PLOW KMS"}
        if arcpy.Exists(out_path):
            # table already existed from an earlier run (before aliases with
            # spaces were added) - try to update in place. AlterField is a
            # schema change, so it can hit the same "exclusive schema lock"
            # issue as any other if the table's bound to a live map/layout -
            # warn and move on rather than crash if so.
            try:
                for fname, alias in aliases.items():
                    arcpy.management.AlterField(out_path, fname, new_field_alias=alias)
            except Exception as e:
                messages.addWarningMessage(
                    f"Could not update field aliases on existing '{os.path.basename(out_path)}' "
                    f"(probably a schema lock from it being open in a map) - close Pro and rerun once "
                    f"to pick up 'BEAT'/'ROAD CLASS'/'SALT KMS'/'PLOW KMS' as display names: {e}")
            return
        gdb, name = os.path.split(out_path)
        arcpy.management.CreateTable(gdb, name)
        arcpy.management.AddField(out_path, "BEAT", "TEXT", field_length=255, field_alias=aliases["BEAT"])
        arcpy.management.AddField(out_path, "ROAD_CLASS", "TEXT", field_length=255, field_alias=aliases["ROAD_CLASS"])
        arcpy.management.AddField(out_path, "SALT_KMS", "DOUBLE", field_alias=aliases["SALT_KMS"])
        arcpy.management.AddField(out_path, "PLOW_KMS", "DOUBLE", field_alias=aliases["PLOW_KMS"])

    def _rebuild_beat_stats_roadclass_table(self, layout, mf, fit_layers, beat_field, roadclass_field,
                                             salt_kms_field, plow_kms_field, default_gdb, messages):
        """Salt/Plow road-class mode: one row per (BEAT, ROAD_CLASS) with
        separate summed SALT_KMS/PLOW_KMS columns, instead of the generic
        single-KMS Beat Stats Table."""
        el = self._find_table_frame(layout, "Beat Stats Table")
        if not el:
            return
        if not fit_layers:
            el.visible = False
            return

        out_path = os.path.join(default_gdb, BEAT_STATS_ROADCLASS_TABLE_NAME)
        self._ensure_beat_stats_roadclass_schema(out_path, messages)

        source_fc = fit_layers[0]
        if len(fit_layers) > 1:
            source_fc = arcpy.management.Merge(
                fit_layers, arcpy.CreateUniqueName("beat_stats_rc_src", "in_memory"))[0]
        scratch_stats = arcpy.CreateUniqueName("beat_stats_rc_scratch", "in_memory")
        try:
            arcpy.analysis.Statistics(
                source_fc, scratch_stats,
                statistics_fields=[[salt_kms_field, "SUM"], [plow_kms_field, "SUM"]],
                case_field=[beat_field, roadclass_field])

            arcpy.management.DeleteRows(out_path)
            sum_salt = f"SUM_{salt_kms_field}"
            sum_plow = f"SUM_{plow_kms_field}"
            with arcpy.da.SearchCursor(scratch_stats, [beat_field, roadclass_field, sum_salt, sum_plow]) as scur, \
                    arcpy.da.InsertCursor(out_path, ["BEAT", "ROAD_CLASS", "SALT_KMS", "PLOW_KMS"]) as icur:
                for beat_val, rc_val, salt_val, plow_val in scur:
                    icur.insertRow((
                        beat_val, rc_val,
                        round(salt_val, 2) if salt_val is not None else None,
                        round(plow_val, 2) if plow_val is not None else None))
        finally:
            if source_fc != fit_layers[0]:
                arcpy.management.Delete(source_fc)
            arcpy.management.Delete(scratch_stats)

        tbl = self._ensure_table_in_map(mf.map, out_path, BEAT_STATS_ROADCLASS_TABLE_NAME)
        el.table = tbl
        self._set_table_fields(el, ["BEAT", "ROAD_CLASS", "SALT_KMS", "PLOW_KMS"])
        el.visible = True
        messages.addMessage(f"Rebuilt 'Beat Stats Table' (road-class mode) from "
                             f"{[l.name for l in fit_layers]} grouped by '{beat_field}'+'{roadclass_field}', "
                             f"summing '{salt_kms_field}'/'{plow_kms_field}' (2 decimals).")

    def _filter_beat_stats_table_for_beat(self, layout, beat_text):
        el = self._find_table_frame(layout, "Beat Stats Table")
        if not el or not el.visible:
            return
        beat_safe = beat_text.replace("'", "''")
        self._set_table_frame_query(el, f"BEAT = '{beat_safe}'")

    def _bind_equipment_table(self, layout, mf, activity, override, messages):
        el = self._find_table_frame(layout, "Equipment Table")
        if not el:
            return None
        equipment_field = override or EQUIPMENT_FIELD_BY_ACTIVITY.get(activity)
        if not equipment_field:
            el.visible = False
            messages.addMessage(
                f"No equipment-table field mapping for activity '{activity}' - "
                f"equipment table hidden (set 'Equipment Table Beat Field Override' "
                f"if this activity's field has a different name).")
            return None
        tbl = self._ensure_table_in_map(mf.map, EQUIPMENT_TABLE_SDE_PATH, "BEAT_EQUIPMENT_ASSIGNMENT")
        el.table = tbl
        self._set_table_fields(el, ["equipment_assignment_id"])
        # column heading "EQUIPMENT" instead of the raw field name (per Ramon).
        # The table frame heading comes from the field alias on the map's
        # table (the frame field's own displayName is ignored), so set it
        # there - map-level only, the enterprise table is untouched.
        try:
            cim = tbl.getDefinition("V3")
            descs = list(cim.fieldDescriptions or [])
            hit = next((d for d in descs if d.fieldName.lower() == "equipment_assignment_id"), None)
            if hit is None:
                hit = arcpy.cim.CreateCIMObjectFromClassName("CIMFieldDescription", "V3")
                hit.fieldName = "equipment_assignment_id"
                hit.visible = True
                descs.append(hit)
            hit.alias = "EQUIPMENT"
            cim.fieldDescriptions = descs
            tbl.setDefinition(cim)
        except Exception as e:
            messages.addWarningMessage(f"Couldn't rename the equipment column heading: {e}")
        el.visible = True
        messages.addMessage(f"Equipment table bound to live BEAT_EQUIPMENT_ASSIGNMENT, "
                             f"filtered on '{equipment_field}'.")
        return equipment_field

    # table-frame look (not data/binding/position) - used to make a table's
    # columns match its first column (_copy_table_style(bs, bs))
    TABLE_STYLE_PROPS = (
        "alternate1RowBackgroundCount", "alternate1RowBackgroundSymbol",
        "alternate2RowBackgroundCount", "alternate2RowBackgroundSymbol",
        "columnBorderSymbol", "columnGap", "fieldGap", "headingBackgroundSymbol",
        "headingBorderSymbol", "headingGap", "headingLineSymbol", "horizontalTextGap",
        # "graphicFrame" = border/background look; NOT "frame", which is the
        # element's position (copying it stacked Equipment on Beat Stats)
        "rowBorderSymbol", "rowGap", "showHeadings", "verticalTextGap", "graphicFrame")
    TABLE_FIELD_STYLE_PROPS = (
        "backgroundSymbol", "borderSymbol", "headingTextSymbol", "textSymbol", "verticalLineSymbol")

    def _set_table_fields(self, el, field_names):
        """Bind a table frame's columns WITHOUT losing the layout's column
        styling: re-assigning el.fields resets every column's own symbols
        (the dividers between columns vanished, 2026-10-08). So: same
        columns already -> leave them alone; different columns -> remember
        the first column's style and give it to every new column."""
        try:
            current = [f.name for f in (el.getDefinition("V3").fields or [])]
        except Exception:
            current = []
        if [c.lower() for c in current] == [f.lower() for f in field_names]:
            return
        ref = None
        try:
            fields = el.getDefinition("V3").fields
            if fields:
                ref = {p: getattr(fields[0], p) for p in self.TABLE_FIELD_STYLE_PROPS if hasattr(fields[0], p)}
        except Exception:
            ref = None
        el.fields = field_names
        if not ref:
            return
        cim = el.getDefinition("V3")
        for f in cim.fields or []:
            for p, v in ref.items():
                setattr(f, p, copy.deepcopy(v))
        el.setDefinition(cim)

    def _copy_table_style(self, src, dst, messages):
        """Table-frame look (TABLE_STYLE_PROPS + per-field symbols) from
        src onto dst. Never position/size, table, fields or filter."""
        if not src or not dst:
            return
        try:
            s = src.getDefinition("V3")
            d = dst.getDefinition("V3")
            for p in self.TABLE_STYLE_PROPS:
                if hasattr(s, p):
                    setattr(d, p, copy.deepcopy(getattr(s, p)))
            if s.fields:
                ref_field = s.fields[0]
                for f in d.fields or []:
                    for p in self.TABLE_FIELD_STYLE_PROPS:
                        if hasattr(ref_field, p):
                            setattr(f, p, copy.deepcopy(getattr(ref_field, p)))
            dst.setDefinition(d)
        except Exception as e:
            messages.addWarningMessage(f"Couldn't copy table style from '{src.name}' to '{dst.name}': {e}")

    def _filter_equipment_table_for_beat(self, layout, equipment_field, beat_text):
        if not equipment_field:
            return
        el = self._find_table_frame(layout, "Equipment Table")
        if not el or not el.visible:
            return
        beat_safe = beat_text.replace("'", "''")
        self._set_table_frame_query(el, f"{equipment_field} = '{beat_safe}'")

    # ---------------- execute ----------------
    def execute(self, parameters, messages):
        # TEMPORARY diagnostic checkpoints (2026-09-29) to localize a hang
        # reported with 0% CPU and zero GP-pane messages - remove once found.
        messages.addMessage("CHECKPOINT 1: execute() entered")
        (p_activity, p_layout_l, p_layout_p, p_map, p_map_layers, p_beat_field,
         p_contract_area, p_beats,
         p_anno_dist, p_out_dir, p_margin, p_use_rotation,
         p_heading, p_dpi, p_equip_field_override) = parameters

        activity = str(p_activity.value)
        self._activity = activity
        self._bg_layers = []
        self._bg_label_classes = None
        self._keep_hidden = []
        self._bpx_ref_renderer = None
        contract_area = p_contract_area.valueAsText
        selected_beats = [str(b).strip() for b in (p_beats.values or []) if str(b).strip()]
        layout_l_name = p_layout_l.valueAsText
        layout_p_name = p_layout_p.valueAsText
        map_name = p_map.valueAsText
        beat_field = p_beat_field.valueAsText
        anno_distance = p_anno_dist.valueAsText or "50 Meters"
        output_dir = p_out_dir.valueAsText
        margin_factor = p_margin.value if p_margin.value is not None else 1.10
        use_fixed_rotation = bool(p_use_rotation.value)
        fixed_heading = p_heading.value if p_heading.value is not None else 0.0
        dpi = int(p_dpi.value) if p_dpi.value else 300
        equip_field_override = p_equip_field_override.valueAsText

        arcpy.env.overwriteOutput = True
        arcpy.env.addOutputsToMap = False
        messages.addMessage("CHECKPOINT 2: about to get CURRENT project")
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        messages.addMessage("CHECKPOINT 3: got CURRENT project, about to read reference label classes")
        ref_label_classes = self._get_reference_label_classes(aprx, messages)
        self._bold_label_classes = ref_label_classes
        ref_label_classes_basemap = self._get_reference_label_classes(
            aprx, messages, layer_name=LABEL_CLASS_BASEMAP_REFERENCE_LAYER)
        messages.addMessage("CHECKPOINT 4: got reference label classes, about to resolve layouts/map frames")

        layoutL = self._get_layout(aprx, layout_l_name)
        layoutP = self._get_layout(aprx, layout_p_name) if layout_p_name else None
        mfL = self._pick_map_frame(layoutL)
        mfP = self._pick_map_frame(layoutP) if layoutP else mfL
        messages.addMessage("CHECKPOINT 5: got map frames, about to resolve target map")

        if map_name:
            target_map = next((m for m in aprx.listMaps() if m.name == map_name), None)
            if not target_map:
                raise RuntimeError(f"Map '{map_name}' not found in this project.")
            # Reassigning a live layout's MapFrame.map is a confirmed deadlock
            # risk when that layout is the active view in a live/CURRENT Pro
            # session (Ramon hit it on both the Landscape and Portrait map
            # frames, 2026-09-29) - it needs a UI repaint that can't happen
            # while the same thread is stuck running this tool. Never
            # attempt it live: verify instead, and fail fast with clear
            # instructions if the wiring doesn't already match.
            mismatches = []
            if mfL.map.name != target_map.name:
                mismatches.append(f"Landscape Layout's map frame is wired to '{mfL.map.name}'")
            if mfP is not mfL and mfP.map.name != target_map.name:
                mismatches.append(f"Portrait Layout's map frame is wired to '{mfP.map.name}'")
            if mismatches:
                raise RuntimeError(
                    f"'Map To Look In' was set to '{target_map.name}', but: {'; '.join(mismatches)}. "
                    f"Reassigning a layout's map frame while running inside Pro is a known deadlock risk, "
                    f"so this tool no longer does it automatically. In Pro, open each layout, drag '{target_map.name}' "
                    f"onto its map frame (or set the map frame's map in its properties) so it's wired to "
                    f"'{target_map.name}' ahead of time, then either leave 'Map To Look In' blank or re-run "
                    f"with it set to '{target_map.name}' once the wiring matches.")
            messages.addMessage(f"Looking in map: '{target_map.name}' (already correctly wired)")
        else:
            messages.addMessage(f"Looking in map: '{mfL.map.name}' (whatever the Landscape Layout is wired to)")

        os.makedirs(output_dir, exist_ok=True)

        # self-heal: a route-highlight layer/fc left behind by an earlier run
        # whose teardown didn't complete (e.g. a crash, or a cancelled run)
        # would otherwise get auto-detected as a bogus second "fit layer" on
        # every run from here on, corrupting the combined extent - so always
        # clear any of these out before doing anything else.
        messages.addMessage("CHECKPOINT 9: about to run stale-layer self-heal on mfL.map")
        self._cleanup_stale_route_highlight_layers(mfL.map, messages)
        messages.addMessage("CHECKPOINT 10: self-heal on mfL.map done")
        if mfP is not mfL:
            messages.addMessage("CHECKPOINT 11: about to run stale-layer self-heal on mfP.map")
            self._cleanup_stale_route_highlight_layers(mfP.map, messages)
            messages.addMessage("CHECKPOINT 12: self-heal on mfP.map done")

        # --- fit layers: explicit selection if given, else auto-detect from visible layers ---
        selected_names = [getattr(v, "name", None) for v in (p_map_layers.values or [])]
        selected_names = [n for n in selected_names if n]

        if selected_names:
            fit_layers_L = [l for l in mfL.map.listLayers() if l.name in selected_names]
            missing = set(selected_names) - {l.name for l in fit_layers_L}
            if missing:
                raise RuntimeError(
                    f"Selected map layer(s) not found in the Landscape Layout's map: {sorted(missing)}")
            fit_layers_P = ([l for l in mfP.map.listLayers() if l.name in selected_names]
                             if mfP is not mfL else fit_layers_L)
            messages.addMessage(f"Using selected layer(s): {[l.name for l in fit_layers_L]}")
        else:
            fit_layers_L = self._visible_fit_layers(mfL, beat_field)
            fit_layers_P = self._visible_fit_layers(mfP, beat_field) if mfP is not mfL else fit_layers_L
            messages.addMessage(f"Auto-detected fit layers: {[l.name for l in fit_layers_L]}")

        # BPX: always the enterprise TS_BS_PXO points (added to the map if
        # missing, removed again at the end) with TCL_WINTER_OPERATIONS as
        # the street background - whatever else is turned on is ignored.
        bpx_mode = activity == "BPX"
        bpx_added = []          # (map, layer) we added and should remove after
        street_layers_L = street_layers_P = None
        if bpx_mode:
            # read Ramon's styling before the tool adds any layer of its own
            self._bpx_ref_renderer = self._find_bpx_reference_renderer(aprx, mfL.map, messages)

            def _bpx_layers_for(map_obj):
                pts, added_p = self._find_or_add_layer(map_obj, BPX_POINTS_SDE_PATH)
                st, added_s = self._find_or_add_layer(map_obj, STREETS_SDE_PATH)
                for lyr, added in ((pts, added_p), (st, added_s)):
                    if added:
                        bpx_added.append((map_obj, lyr))
                return pts, st
            pts_L, st_L = _bpx_layers_for(mfL.map)
            fit_layers_L, street_layers_L = [pts_L], [st_L]
            if mfP is not mfL:
                # same map: finds the layers just added above, no duplicate
                pts_P, st_P = _bpx_layers_for(mfP.map)
                fit_layers_P, street_layers_P = [pts_P], [st_P]
            else:
                fit_layers_P, street_layers_P = fit_layers_L, street_layers_L
            messages.addMessage(f"BPX: using '{pts_L.name}' (ACTIVE points only) over street layer "
                                f"'{st_L.name}'.")

        # a layer picked explicitly might be toggled off in the map's Contents -
        # force it visible so it actually draws, regardless of how it was found
        # (Layer objects aren't reliably hashable, so no set() dedup here)
        for lyr in list(fit_layers_L) + list(fit_layers_P):
            lyr.visible = True

        anno_layers_L = self._visible_anno_layers(mfL)
        anno_layers_P = self._visible_anno_layers(mfP) if mfP is not mfL else anno_layers_L
        messages.addMessage(f"Auto-detected annotation layers: {[l.name for l in anno_layers_L]}")

        if not fit_layers_L:
            raise RuntimeError(
                "No layer to pull beats/extent from was found. Either select one in "
                "'Map Layer(s) To Use', or turn on the layer(s) you want auto-detected "
                "before running this tool.")

        # One geoprocessing pass per fit layer up front (bounding polygon per
        # beat via MinimumBoundingGeometry) instead of a MakeFeatureLayer +
        # Select + cursor-scan per beat inside the export loop - the latter
        # got slow with 200+ beats since it re-scanned geometry from scratch
        # every single page.
        messages.addMessage("CHECKPOINT 13: about to precompute beat extents (MinimumBoundingGeometry)")
        beat_extents_L = self._precompute_beat_extents(fit_layers_L, beat_field, activity)
        messages.addMessage(f"CHECKPOINT 14: precomputed {len(beat_extents_L)} beat extents for Landscape")
        if not beat_extents_L:
            raise RuntimeError(f"No distinct, non-blank values found in '{beat_field}'.")
        beats = sorted(beat_extents_L.keys())
        messages.addMessage(f"Found {len(beats)} distinct beats: {beats}")

        # contracted beats only (sidewalks) - see _in_house_beats
        in_house = self._in_house_beats(fit_layers_L, beat_field, activity)
        if in_house:
            beats = [b for b in beats if b not in in_house]
            messages.addMessage(f"Skipping {len(in_house)} in-house beat(s); {len(beats)} contracted beat(s) left.")

        # optional Contract Area / Beat(s) filters - narrow the print list
        # only; each printed beat still gets its full extent and stats
        if contract_area:
            ca_field, where = self._contract_area_where(fit_layers_L, contract_area)
            if not ca_field:
                raise RuntimeError(
                    f"Contract Area '{contract_area}' was picked, but none of {[l.name for l in fit_layers_L]} "
                    f"have a contract area field ({CONTRACT_AREA_FIELD_CANDIDATES}).")
            in_area = set(self._distinct_values(fit_layers_L, beat_field, self._and_where(where, self._contracted_clause(fit_layers_L[0], activity))))
            beats = [b for b in beats if b in in_area]
            messages.addMessage(f"Contract Area {contract_area}: {len(beats)} beat(s)")
        if selected_beats:
            unknown = [b for b in selected_beats if b not in beats]
            if unknown:
                messages.addWarningMessage(
                    f"Skipping beat(s) not found in the data"
                    f"{' for contract area ' + contract_area if contract_area else ''}: {unknown}")
            beats = [b for b in beats if b in selected_beats]
        if not beats:
            raise RuntimeError("No beats left to print after applying the Contract Area / Beat(s) filters.")
        if contract_area or selected_beats:
            messages.addMessage(f"Printing {len(beats)} beat(s): {beats}")
        beat_extents_P = (self._precompute_beat_extents(fit_layers_P, beat_field, activity)
                           if layoutP and fit_layers_P is not fit_layers_L else beat_extents_L)
        messages.addMessage("CHECKPOINT 15: beat extents ready (both orientations), starting per-run setup")

        # --- activity-driven, once-per-run layout setup ---
        title_text = TITLE_TEXT_BY_ACTIVITY.get(activity, activity.upper())
        kms_field_L = self._resolve_kms_field(fit_layers_L, activity)
        kms_field_P = self._resolve_kms_field(fit_layers_P, activity) if layoutP else kms_field_L

        beat_label_els_L = self._find_beat_label_elements(layoutL)
        beat_label_els_P = self._find_beat_label_elements(layoutP) if layoutP else beat_label_els_L
        # Landscape's "BEAT NAME" element is manually positioned in the
        # layout itself - leave it alone. Portrait's is right-aligned on the
        # title line per page (_right_align_beat_label), since its width
        # depends on the beat name.

        # Salt/Plow road-class mode: full network stays visible as a plain
        # gray background, with a persistent per-beat "highlight" layer
        # colored by road class on top - only kicks in when the activity and
        # the underlying fields actually support it; every other activity
        # keeps the original single-color extract-and-swap behavior.
        roadclass_field_L = self._resolve_field(fit_layers_L, ROADCLASS_FIELD_CANDIDATES)
        salt_kms_field_L = self._resolve_field(fit_layers_L, SALT_KMS_FIELD_CANDIDATES)
        plow_kms_field_L = self._resolve_field(fit_layers_L, PLOW_KMS_FIELD_CANDIDATES)
        road_class_mode_L = bool(activity == "Salt/Plow" and roadclass_field_L
                                  and salt_kms_field_L and plow_kms_field_L)
        if layoutP:
            roadclass_field_P = self._resolve_field(fit_layers_P, ROADCLASS_FIELD_CANDIDATES)
            salt_kms_field_P = self._resolve_field(fit_layers_P, SALT_KMS_FIELD_CANDIDATES)
            plow_kms_field_P = self._resolve_field(fit_layers_P, PLOW_KMS_FIELD_CANDIDATES)
            road_class_mode_P = bool(activity == "Salt/Plow" and roadclass_field_P
                                      and salt_kms_field_P and plow_kms_field_P)
        else:
            roadclass_field_P, salt_kms_field_P, plow_kms_field_P = (
                roadclass_field_L, salt_kms_field_L, plow_kms_field_L)
            road_class_mode_P = road_class_mode_L

        # Sidewalk mode: same idea as road-class mode, but every piece of
        # symbology is copied from Ramon's reference layers in the
        # "Sidewalks" map (see SIDEWALK_SYMBOLOGY_REFERENCE_LAYER).
        side_renderer = sidewalk_legend_renderer = None
        if activity == "Sidewalk":
            side_renderer = self._get_reference_renderer(aprx, SIDEWALK_SYMBOLOGY_REFERENCE_LAYER, messages)
            legend_src = self._get_reference_renderer(aprx, SIDEWALK_LEGEND_REFERENCE_LAYER, messages)
            if side_renderer is not None:
                sidewalk_legend_renderer = (self._sidewalk_legend_renderer(side_renderer, legend_src)
                                            if legend_src is not None else side_renderer)
        side_field_L = self._resolve_field(fit_layers_L, SIDEWALK_SIDE_FIELD_CANDIDATES)
        side_field_P = self._resolve_field(fit_layers_P, SIDEWALK_SIDE_FIELD_CANDIDATES) if layoutP else side_field_L
        sidewalk_mode_L = bool(side_renderer is not None and side_field_L)
        sidewalk_mode_P = bool(side_renderer is not None and side_field_P)
        if activity == "Sidewalk" and not sidewalk_mode_L:
            messages.addWarningMessage(
                "Sidewalk symbology from the 'Sidewalks' map couldn't be used (reference layer or "
                "sdwk_side field missing) - falling back to each layer's own symbology.")
        # background layers' own renderers, put back after the run
        original_renderers = []
        # BPX points belong on BPX maps only: any visible TS_BS_PXO layer in
        # the printed map(s) is hidden for other activities (it printed every
        # bus stop across a DLA page, 2026-10-13) and turned back on at the end
        if not bpx_mode:
            bpx_table = BPX_POINTS_SDE_PATH.split(".")[-1].lower()
            for map_obj in {id(mfL.map): mfL.map, id(mfP.map): mfP.map}.values():
                for lyr in map_obj.listLayers():
                    try:
                        if (lyr.isFeatureLayer and lyr.visible
                                and lyr.dataSource.lower().split(".")[-1] == bpx_table):
                            cim = lyr.getDefinition("V3")
                            original_renderers.append((lyr, cim.renderer, cim.labelClasses, lyr.showLabels, True))
                            lyr.visible = False
                            messages.addMessage(f"Hid '{lyr.name}' (BPX points) for this {activity} run.")
                    except Exception:
                        continue
        # flat single-color route (CT / ROW), see ROUTE_STYLE_BY_ACTIVITY
        route_style = ROUTE_STYLE_BY_ACTIVITY.get(activity)
        dla_mode = activity == "DLA"

        equipment_field_L = None
        equipment_field_P = None
        legend_ref_L = None
        legend_ref_P = None
        for lay, mf, fit_layers, kms_field, roadclass_field, salt_f, plow_f, road_class_mode in (
                [(layoutL, mfL, fit_layers_L, kms_field_L, roadclass_field_L,
                  salt_kms_field_L, plow_kms_field_L, road_class_mode_L)] +
                ([(layoutP, mfP, fit_layers_P, kms_field_P, roadclass_field_P,
                   salt_kms_field_P, plow_kms_field_P, road_class_mode_P)] if layoutP else [])):
            self._set_title_text(lay, title_text)

            if road_class_mode:
                # background gray styling only, once per run - the beat
                # highlight itself is a fresh per-page swap-in/out, same as
                # every other activity (see _swap_in_beat_only_layers)
                self._style_background(fit_layers, original_renderers, ref_label_classes_basemap, messages)
                self._rebuild_beat_stats_roadclass_table(
                    lay, mf, fit_layers, beat_field, roadclass_field, salt_f, plow_f,
                    aprx.defaultGeodatabase, messages)
                legend_ref = self._setup_roadclass_legend_reference(
                    mf, fit_layers, roadclass_field, arcpy.env.scratchGDB, lay.name, messages)
                legend_layers = [legend_ref[0]]
            elif (sidewalk_mode_L if lay is layoutL else sidewalk_mode_P):
                side_field = side_field_L if lay is layoutL else side_field_P
                self._style_background(fit_layers, original_renderers, ref_label_classes_basemap, messages)
                self._rebuild_beat_stats_table(
                    lay, mf, fit_layers, beat_field, kms_field, aprx.defaultGeodatabase, messages)
                legend_ref = self._setup_sidewalk_legend_reference(
                    mf, fit_layers, sidewalk_legend_renderer, side_field, lay.name, messages)
                legend_layers = [legend_ref[0]]
            elif route_style:
                self._style_background(fit_layers, original_renderers, ref_label_classes_basemap, messages)
                self._rebuild_beat_stats_table(
                    lay, mf, fit_layers, beat_field, kms_field, aprx.defaultGeodatabase, messages)
                legend_ref = self._setup_flat_route_legend_reference(
                    mf, fit_layers, beat_field, route_style, messages)
                legend_layers = [legend_ref[0]]
            elif dla_mode:
                self._style_background(fit_layers, original_renderers, ref_label_classes_basemap, messages)
                self._rebuild_dla_stats_table(
                    lay, mf, fit_layers, beat_field, kms_field, aprx.defaultGeodatabase, messages)
                legend_ref = self._setup_dla_legend_reference(mf, fit_layers, lay.name, messages)
                legend_layers = [legend_ref[0]]
            elif bpx_mode:
                streets = street_layers_L if lay is layoutL else street_layers_P
                # street network = background; the real points layer stays
                # hidden all run (each page draws its own ACTIVE-only copy)
                self._style_background(streets, original_renderers, ref_label_classes_basemap, messages)
                for lyr in fit_layers:
                    if not any(saved[0] is lyr or saved[0].name == lyr.name for saved in original_renderers):
                        cim = lyr.getDefinition("V3")
                        original_renderers.append((lyr, cim.renderer, cim.labelClasses, lyr.showLabels, lyr.visible))
                    lyr.visible = False
                    if not any(k is lyr or k.name == lyr.name for k in self._keep_hidden):
                        self._keep_hidden.append(lyr)
                self._rebuild_bpx_stats_table(lay, mf, fit_layers[0], beat_field, aprx.defaultGeodatabase, messages)
                legend_ref = self._setup_bpx_legend_reference(mf, fit_layers[0], lay.name, messages)
                legend_layers = [legend_ref[0]]
            else:
                self._rebuild_beat_stats_table(
                    lay, mf, fit_layers, beat_field, kms_field, aprx.defaultGeodatabase, messages)
                legend_ref = None
                legend_layers = fit_layers
            if lay is layoutL:
                legend_ref_L = legend_ref
            else:
                legend_ref_P = legend_ref

            self._rebuild_legend(lay, legend_layers, messages)
            # every helper legend layer (incl. Salt/Plow's "Route (by Road
            # Class) (...)") shows just its classes, never its own name
            if legend_ref and legend_ref[0].name.startswith(TOOL_LAYER_NAME_PREFIXES):
                self._hide_legend_item_heading(lay, legend_ref[0].name)
            # table look lives on the layouts themselves (set 2026-10-08) and
            # is kept through column changes by _set_table_fields
            eq_field = self._bind_equipment_table(lay, mf, activity, equip_field_override, messages)
            if lay is layoutL:
                equipment_field_L = eq_field
            else:
                equipment_field_P = eq_field
        if not layoutP:
            equipment_field_P = equipment_field_L

        # Real map frame dimensions (not just page size - a title block etc.
        # eats into it), used below to pick whichever orientation actually
        # fits the beat tightest (best/highest scale), not just whichever
        # one has a "wider than tall" extent - a wide layout frame can still
        # be the worse fit for a mildly-wide beat if its own aspect ratio is
        # further from the beat's than the other orientation's is.
        heading = fixed_heading if use_fixed_rotation else mfL.camera.heading
        frameL = self._frame_meters(layoutL, mfL)
        frameP = self._frame_meters(layoutP, mfP) if layoutP else None
        self._link_north_arrow(layoutL, mfL, messages)
        if layoutP:
            self._link_north_arrow(layoutP, mfP, messages)
        boxes_L = self._rotated_boxes(fit_layers_L, beat_field, activity, heading)
        boxes_P = (self._rotated_boxes(fit_layers_P, beat_field, activity, heading)
                   if layoutP and fit_layers_P is not fit_layers_L else boxes_L)

        for i, beat in enumerate(beats, start=1):
            messages.addMessage(f"Processing {beat} ({i}/{len(beats)})")

            extent_L = beat_extents_L.get(beat)
            if extent_L is None:
                raise RuntimeError(
                    f"No features found for beat '{beat}' in {[l.name for l in fit_layers_L]} "
                    f"(field '{beat_field}'). Fix the data or the beat list rather than silently "
                    f"skipping a beat from the export.")

            box_L = boxes_L.get(beat)
            if box_L is None:
                raise RuntimeError(f"No geometry found for beat '{beat}' to frame the map on.")
            scale_L = self._scale_for(box_L, frameL[0], frameL[1], margin_factor)
            use_landscape = True
            if layoutP:
                # Whichever orientation fits the beat at the bigger (more
                # zoomed-in) scale, judged on the ROTATED box - what's
                # actually on the page - not the north-up one.
                box_P = boxes_P.get(beat, box_L)
                scale_P = self._scale_for(box_P, frameP[0], frameP[1], margin_factor)
                use_landscape = scale_L <= scale_P

            layout = layoutL if use_landscape else layoutP
            mf = mfL if use_landscape else mfP
            fit_layers = fit_layers_L if use_landscape else fit_layers_P
            anno_layers = anno_layers_L if use_landscape else anno_layers_P
            beat_label_els = beat_label_els_L if use_landscape else beat_label_els_P
            equipment_field = equipment_field_L if use_landscape else equipment_field_P
            road_class_mode = road_class_mode_L if use_landscape else road_class_mode_P
            roadclass_field = roadclass_field_L if use_landscape else roadclass_field_P
            if use_landscape:
                self._place_camera(mf, box_L, heading, scale_L)
            else:
                self._place_camera(mf, box_P, heading, scale_P)

            self._set_beat_label_text(beat_label_els, beat)
            if not use_landscape:
                self._right_align_beat_label(layout, mf, beat_label_els)

            saved_defs = self._filter_anno_for_current_beat(
                fit_layers, anno_layers, beat_field, beat, anno_distance)

            self._filter_equipment_table_for_beat(layout, equipment_field, beat)
            self._filter_beat_stats_table_for_beat(layout, beat)

            if road_class_mode:
                beat_layer_records = self._swap_in_beat_only_layers(
                    mf, fit_layers, beat_field, beat, arcpy.env.scratchGDB, ref_label_classes, messages,
                    hide_original=False, roadclass_field=roadclass_field)
            elif sidewalk_mode_L if use_landscape else sidewalk_mode_P:
                beat_layer_records = self._swap_in_beat_only_layers(
                    mf, fit_layers, beat_field, beat, arcpy.env.scratchGDB, ref_label_classes, messages,
                    hide_original=False, ref_renderer=(side_renderer, SIDEWALK_SIDE_FIELD_CANDIDATES))
            elif dla_mode:
                beat_layer_records = self._swap_in_beat_only_layers(
                    mf, fit_layers, beat_field, beat, arcpy.env.scratchGDB, ref_label_classes, messages,
                    hide_original=False, dla_passes=True)
            elif bpx_mode:
                beat_layer_records = self._swap_in_beat_only_layers(
                    mf, fit_layers, beat_field, beat, arcpy.env.scratchGDB, ref_label_classes, messages,
                    hide_original=False, bpx_points=True)
            elif route_style:
                beat_layer_records = self._swap_in_beat_only_layers(
                    mf, fit_layers, beat_field, beat, arcpy.env.scratchGDB, ref_label_classes, messages,
                    hide_original=False, highlight_style=route_style)
            else:
                beat_layer_records = self._swap_in_beat_only_layers(
                    mf, fit_layers, beat_field, beat, arcpy.env.scratchGDB, ref_label_classes, messages)
            beat_layer_records = beat_layer_records + self._add_background_label_layer(
                mf, beat_layer_records, messages)
            try:
                safe_beat = "".join(c if c not in '<>:"/\\|?*' else "_" for c in beat)
                out_pdf = os.path.join(output_dir, f"{safe_beat}.pdf")
                layout.exportToPDF(out_pdf, resolution=dpi, image_quality="BEST", embed_fonts=True)
            finally:
                self._restore_after_beat_only_layers(mf, beat_layer_records)

            self._restore_anno_def_queries(saved_defs)

            messages.addMessage(
                f"{beat}: {'landscape' if use_landscape else 'portrait'} | "
                f"scale 1:{int(mf.camera.scale)} | heading {mf.camera.heading} deg -> {out_pdf}")

        self._teardown_roadclass_legend_reference(mfL, legend_ref_L)
        if mfP is not mfL:
            self._teardown_roadclass_legend_reference(mfP, legend_ref_P)
        # put the background layers' own symbology back (every activity that grays it)
        for lyr, renderer, label_classes, show_labels, was_visible in original_renderers:
            try:
                cim = lyr.getDefinition("V3")
                cim.renderer = renderer
                cim.labelClasses = label_classes
                lyr.setDefinition(cim)
                lyr.showLabels = show_labels
                lyr.visible = was_visible
            except Exception:
                messages.addWarningMessage(f"Couldn't restore the original symbology/labels on '{lyr.name}'.")
        # BPX: take back out any layer this run added to the map (layer
        # reference only - the enterprise data is never touched)
        for map_obj, lyr in bpx_added:
            try:
                map_obj.removeLayer(lyr)
            except Exception:
                pass

        messages.addMessage("Finished exporting all beats successfully.")
