# -*- coding: utf-8 -*-
"""
Preprocess LAS Extract Mask (Script Tool version)
v13 - Final Optimized
- Uses dictionary-based configuration for easy editing.
- Smart parsing for Decimal units vs. Feet-Inches (e.g., 5' 11").
- Robust ArcPy environment handling.
"""

import arcpy
import os
import sys
import re
from contextlib import contextmanager

# =========================== CONFIGURATION ===================================

# Define buffer sizes (in the Linear Unit of your project) per feature Type
TYPE_CONFIG = {
    # Point Defaults
    "Storm Drain Manhole": 1.5, "Sewer(SS)": 1.5, "Manhole": 1.5,
    "Water Valve": 0.5, "Gas Valve": 0.5,
    "Bollard": 1.0, "Fiber Marker": 1.0, "Gas(G)": 1.0,
    "Power Pedestal": 1.5, "Telecom Pedestal": 1.5, "CATV (Cable Television) Pedestal": 1.5,
    "Lamp Post": 1.5, "Fire Hydrant": 1.15,
    # Line Defaults
    "Traffic Signal": 4.0, "Street Light": 2.0
}

# Types that REQUIRE a specific 'Span_txt' value in the attribute table
REQUIRES_SPAN_TXT = {
    "Catch Basin", "Handhole", "Water(W)", "Street Sign",
    "Existing Cabinet", "Mailbox", "Power/Electric(PWR)", "Storm Drain(SD)"
}

# =========================== UTILITIES =======================================

def _split_multi_text(text):
    """Split semicolon-separated multi-value parameter text into a list."""
    if not text:
        return []
    return [t.strip() for t in text.split(';') if t.strip()]

def _to_bool(text, default=False):
    if text is None or text == "":
        return default
    return str(text).strip().lower() in ("true", "1", "yes", "y", "t")

def _ensure_field(fc, name, ftype="DOUBLE", precision=None, scale=None, length=None):
    names = [f.name for f in arcpy.ListFields(fc)]
    if name not in names:
        arcpy.management.AddField(fc, name, ftype, precision, scale, length)

def _delete(path):
    if arcpy.Exists(path):
        arcpy.management.Delete(path)

def _gdb_of(ds):
    """Return the geodatabase path that contains the dataset."""
    try:
        desc = arcpy.Describe(ds)
        return getattr(desc, "path", arcpy.env.workspace)
    except Exception:
        return arcpy.env.workspace

@contextmanager
def env_manager(**envs):
    old = {k: getattr(arcpy.env, k) for k in envs}
    for k, v in envs.items():
        setattr(arcpy.env, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(arcpy.env, k, v)

# =========================== NUMERIC PARSING =================================

def _parse_height_value(val):
    """
    Parses 'Height_txt' into a float.
    1. Tries direct decimal conversion (e.g., "12.5", "100").
    2. Tries Feet-Inches format (e.g., "5' 11"", "5'11").
       Returns decimal feet (e.g., 5'6" -> 5.5).
    """
    if val is None:
        return None
    
    s = str(val).strip()
    if not s or s.lower() in ("nan", "null", "none", ""):
        return None
    
    # Scenario 1-3: Decimal Values (Inches, Feet, or Metric)
    # If it's just a number, trust the input.
    try:
        return float(s)
    except ValueError:
        pass  # Not a simple number, check for 5'11" logic
    
    # Scenario 4: Feet and Inches (e.g., 5' 11" or 5'11)
    # Regex: Starts with digits + ', optional space, digits + optional decimals, optional "
    match = re.match(r"^(\d+)'\s*(\d+(?:\.\d*)?)\"?$", s)
    if match:
        feet = float(match.group(1))
        inches = float(match.group(2))
        return feet + (inches / 12.0)

    return None

# =========================== LOGIC ===========================================

def _span_from_point(ftype, span_txt):
    ft = (ftype or "").strip()
    
    # Check simple config first
    if ft in TYPE_CONFIG:
        return TYPE_CONFIG[ft]
    
    # Check if it requires manual span
    if ft in REQUIRES_SPAN_TXT:
        val = _parse_height_value(span_txt)
        if val is None:
            raise ValueError(f"{ft} requires numeric Span_txt")
        return val * 1.1
        
    raise ValueError(f"Unknown point type: {ft}")

def _span_from_line(ftype):
    ft = (ftype or "").strip()
    if ft in TYPE_CONFIG:
        return TYPE_CONFIG[ft]
    raise ValueError(f"Unknown line type: {ft}")

def _calc_height(fc):
    """Height = Height_txt * 1.05 (DOUBLE)."""
    _ensure_field(fc, "Height", "DOUBLE")
    oid = arcpy.Describe(fc).OIDFieldName
    with arcpy.da.UpdateCursor(fc, [oid, "Height_txt", "Height"]) as cur:
        for oidv, htxt, _ in cur:
            v = _parse_height_value(htxt)
            new_h = v * 1.05 if v is not None else None
            cur.updateRow([oidv, htxt, new_h])

# =========================== MAIN WORKFLOW ===================================

def preprocess(points, lines, line_ht, bounds, out_mask,
               search_radius=None, workspace=None, overwrite=True):
    
    arcpy.env.overwriteOutput = overwrite
    if workspace:
        arcpy.env.workspace = workspace
    
    mem = "in_memory"

    # 1. Process Points
    pts_merge = arcpy.CreateUniqueName("pts_merge", mem)
    _delete(pts_merge)
    arcpy.AddMessage("Merging point inputs...")
    arcpy.management.Merge(points, pts_merge)

    # Basic Validation
    req = ["ID", "Type", "Height_txt", "Comments"]
    flds = [f.name for f in arcpy.ListFields(pts_merge)]
    miss = [x for x in req if x not in flds]
    if miss:
        raise arcpy.ExecuteError(f"Points missing fields: {miss}")

    arcpy.AddMessage("Calculating point Z_Min...")
    arcpy.management.CalculateGeometryAttributes(pts_merge, [["Z_Min", "POINT_Z"]])

    arcpy.AddMessage("Calculating point Height...")
    _calc_height(pts_merge)

    if "Span_txt" not in flds:
        _ensure_field(pts_merge, "Span_txt", "TEXT", length=50)
        arcpy.AddWarning("Span_txt missing: types requiring Span_txt will be NULL.")

    _ensure_field(pts_merge, "Span", "DOUBLE")
    arcpy.AddMessage("Calculating point Span...")
    
    oidp = arcpy.Describe(pts_merge).OIDFieldName
    with arcpy.da.UpdateCursor(pts_merge, [oidp, "Type", "Span_txt", "Span"]) as cur:
        for oidv, typ, stxt, _ in cur:
            try:
                s = _span_from_point(typ, stxt)
            except Exception as ex:
                arcpy.AddWarning(f"Span skipped OID={oidv}: {ex}")
                s = None
            cur.updateRow([oidv, typ, stxt, s])

    # Count NULL Spans (Diagnostic)
    try:
        pts_null_view = arcpy.management.MakeTableView(pts_merge, "pts_null_span", "Span IS NULL")[0]
        null_pts = int(arcpy.management.GetCount(pts_null_view)[0])
        if null_pts:
            arcpy.AddWarning(f"{null_pts} point features have NULL Span and were not buffered.")
    except Exception:
        pass

    pts_buf = arcpy.CreateUniqueName("pts_buf", mem)
    _delete(pts_buf)
    arcpy.AddMessage("Buffering points...")
    arcpy.analysis.PairwiseBuffer(pts_merge, pts_buf, "Span")
    buffers = [pts_buf]

    # 2. Process Lines (Optional)
    if lines and line_ht:
        lns_merge = arcpy.CreateUniqueName("lns_merge", mem)
        lns_ht    = arcpy.CreateUniqueName("lns_ht", mem)
        lns_buf   = arcpy.CreateUniqueName("lns_buf", mem)

        _delete(lns_merge)
        arcpy.AddMessage("Merging line inputs...")
        arcpy.management.Merge(lines, lns_merge)

        _delete(lns_ht)
        arcpy.management.Merge(line_ht, lns_ht)

        lh_fields = [f.name for f in arcpy.ListFields(lns_ht)]
        if "ID" not in lh_fields or "Height_txt" not in lh_fields:
            raise arcpy.ExecuteError("Line-height sources require ID + Height_txt")

        arcpy.AddMessage("Joining Height_txt onto lines...")
        arcpy.management.JoinField(lns_merge, "ID", lns_ht, "ID", ["Height_txt"])

        _calc_height(lns_merge)
        arcpy.management.CalculateGeometryAttributes(lns_merge, [["Z_Min", "EXTENT_MIN_Z"]])

        _ensure_field(lns_merge, "Span", "DOUBLE")
        oidl = arcpy.Describe(lns_merge).OIDFieldName
        with arcpy.da.UpdateCursor(lns_merge, [oidl, "Type", "Span"]) as cur:
            for oidv, typ, _ in cur:
                try:
                    s = _span_from_line(typ)
                except Exception as ex:
                    arcpy.AddWarning(f"Span skipped OID={oidv}: {ex}")
                    s = None
                cur.updateRow([oidv, typ, s])

        arcpy.AddMessage("Buffering lines...")
        _delete(lns_buf)
        arcpy.analysis.PairwiseBuffer(lns_merge, lns_buf, "Span")
        buffers.append(lns_buf)

    # 3. Final Merge & Spatial Join
    _delete(out_mask)
    if len(buffers) == 1:
        arcpy.management.CopyFeatures(buffers[0], out_mask)
    else:
        mbuf = arcpy.CreateUniqueName("mask_merge", mem)
        _delete(mbuf)
        arcpy.management.Merge(buffers, mbuf)
        arcpy.management.CopyFeatures(mbuf, out_mask)

    sj_fc = arcpy.CreateUniqueName("mask_sj", mem)
    _delete(sj_fc)

    kwargs = dict(
        target_features=out_mask,
        join_features=bounds,
        out_feature_class=sj_fc,
        join_operation="JOIN_ONE_TO_MANY",
        join_type="KEEP_COMMON",
        match_option="INTERSECT"
    )
    if search_radius:
        kwargs["search_radius"] = search_radius

    arcpy.AddMessage("Spatial Joining with Bounds...")
    arcpy.analysis.SpatialJoin(**kwargs)

    # 4. Split By Attributes (Must go to GDB Root)
    out_gdb = workspace or _gdb_of(out_mask)
    arcpy.AddMessage(f"Splitting by 'FileName' into GDB: {out_gdb}")

    fields = [f.name for f in arcpy.ListFields(sj_fc)]
    if "FileName" not in fields:
        raise arcpy.ExecuteError("Spatial Join output missing 'FileName' field.")

    arcpy.analysis.SplitByAttributes(
        Input_Table=sj_fc,
        Target_Workspace=out_gdb,
        Split_Fields=["FileName"]
    )

    arcpy.AddMessage("Done. Split outputs are in the GDB root.")
    return out_mask

# =========================== ENTRYPOINT ======================================

def main():
    try:
        # Params: 0=Pts, 1=Lns, 2=LnHt, 3=Bnds, 4=Out, 5=Rad, 6=Wksp, 7=Over
        points   = _split_multi_text(arcpy.GetParameterAsText(0))
        lines    = _split_multi_text(arcpy.GetParameterAsText(1))
        line_ht  = _split_multi_text(arcpy.GetParameterAsText(2))
        bounds   = arcpy.GetParameterAsText(3)
        out_mask = arcpy.GetParameterAsText(4)
        search_r = arcpy.GetParameterAsText(5)
        workspace= arcpy.GetParameterAsText(6)
        overwrite= _to_bool(arcpy.GetParameterAsText(7), True)

        if not points:
            raise arcpy.ExecuteError("Input Points are required.")
        if not bounds:
            raise arcpy.ExecuteError("Bounds is required.")
        if not out_mask:
            raise arcpy.ExecuteError("Output LAS Extract Mask is required.")

        # both-or-none rule
        if (lines and not line_ht) or (line_ht and not lines):
            raise arcpy.ExecuteError("Provide BOTH Lines and Line Heights, or leave BOTH blank.")

        with env_manager(workspace=workspace, overwriteOutput=overwrite):
            preprocess(points, lines, line_ht, bounds, out_mask,
                       search_radius=search_r, workspace=workspace,
                       overwrite=overwrite)
            arcpy.SetParameterAsText(4, out_mask)
        return 0

    except arcpy.ExecuteError:
        arcpy.AddError(arcpy.GetMessages(2))
        return 1
    except Exception as ex:
        arcpy.AddError(f"Unexpected error: {ex}")
        arcpy.AddError(arcpy.GetMessages(2))
        return 1

if __name__ == "__main__":
    sys.exit(main())
