"""Build MapSeriesExportTools_templates/ from the Sidewalks project:
the two Universal layouts (.pagx, data layers stripped) and the reference
layers the tool copies symbology/labels from (.lyrx). Works on a temp copy
of the .aprx so the real project is never touched. Scrubs any saved
password out of the JSON afterwards."""
import arcpy, json, os, re, shutil, sys

SRC_APRX = r"D:\Users\rboyce\Documents\ArcGIS\Projects\Sidewalks\Sidewalks.aprx"
OUT = sys.argv[1]
here = os.path.dirname(os.path.abspath(__file__))
tmp_dir = os.path.join(here, "tmpl_build")
shutil.rmtree(tmp_dir, ignore_errors=True)
os.makedirs(tmp_dir)
os.makedirs(OUT, exist_ok=True)
for f in os.listdir(OUT):
    os.remove(os.path.join(OUT, f))

arcpy.mp.ArcGISProject(SRC_APRX).saveACopy(os.path.join(tmp_dir, "copy.aprx"))
aprx = arcpy.mp.ArcGISProject(os.path.join(tmp_dir, "copy.aprx"))

# --- reference layers -> .lyrx ---
LAYERS = {
    ("Sidewalks", "TCL_ALL_SEG_SEPT_26_v2"): "street_labels_bold.lyrx",
    ("Sidewalks", "TCL_ALL_SEG_SEPT_26_v2_basemap"): "street_labels_regular.lyrx",
    ("Sidewalks", "TCL_ALL_SEG_SEPT_26_v2_legend"): "sidewalk_legend.lyrx",
    ("2026/27 Changes", "TS_BS_PXO"): "bpx_points.lyrx",
}
for (map_name, lyr_name), fname in LAYERS.items():
    m = next(m for m in aprx.listMaps() if m.name == map_name)
    lyr = next(l for l in m.listLayers() if l.name == lyr_name)
    out = os.path.join(OUT, fname)
    lyr.saveACopy(out) if hasattr(lyr, "saveACopy") else arcpy.management.SaveToLayerFile(lyr, out)
    print("lyrx:", fname, "<-", map_name, "/", lyr_name)

# --- layouts -> .pagx, map stripped of data layers/tables ---
lays = {l.name: l for l in aprx.listLayouts()}
mapL = lays["Universal Beat Map Series - Landscape"].listElements("MAPFRAME_ELEMENT")[0].map
for lyr in mapL.listLayers():
    if lyr.isBasemapLayer or lyr.name in ("World Topographic Map", "World Hillshade"):
        continue
    mapL.removeLayer(lyr)
for t in mapL.listTables():
    mapL.removeTable(t)
print("template map keeps:", [l.name for l in mapL.listLayers()], [t.name for t in mapL.listTables()])
for name, fname in (("Universal Beat Map Series - Landscape", "Beat Map Landscape.pagx"),
                    ("Universal Beat Map Series - Portrait", "Beat Map Portrait.pagx")):
    mapL.name = name.replace("Universal Beat Map Series", "Beat Map Series")
    lays[name].exportToPAGX(os.path.join(OUT, fname))
    print("pagx:", fname, "map:", mapL.name)
del aprx

# --- scrub credentials ---
pat = re.compile(r"(ENCRYPTED_PASSWORD(_UTF8)?|PASSWORD)=[^;\"]*;?", re.I)
# server/instance/user too - the tool only reads symbology from these files
host = re.compile(r"\b(SERVER|INSTANCE|DB_CONNECTION_PROPERTIES|USER)=[^;\"]*", re.I)
for f in os.listdir(OUT):
    p = os.path.join(OUT, f)
    txt = open(p, encoding="utf-8").read()
    new = host.sub(lambda m: m.group(1) + "=", pat.sub("", txt))
    if new != txt:
        open(p, "w", encoding="utf-8").write(new)
        print("scrubbed password from", f)
    json.loads(new)   # still valid JSON
    print(f, os.path.getsize(p), "bytes | 'password' left:", "password" in new.lower(),
          "| server left:", "amazonaws" in new.lower())
    if f.endswith(".lyrx"):
        d = arcpy.mp.LayerFile(p).listLayers()[0].getDefinition("V3")
        print("   loads OK:", type(d.renderer).__name__, "| label classes:", len(d.labelClasses or []))
shutil.rmtree(tmp_dir, ignore_errors=True)
