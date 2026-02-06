# -*- coding: utf-8 -*-
    """
    Preprocess LAS Extract Mask
    v12 - Optimized for GitHub
    """
    
    import arcpy
    import os
    import sys
    import re
    from contextlib import contextmanager
    
    # =========================== CONFIGURATION ===================================
    
    # Define buffer sizes (in meters/feet depending on your projection) per feature Type
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
    
    def _strip_quotes(s):
        if s is None: return s
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            return s[1:-1]
        return s
    
    def _split_multi_text(text):
        if not text: return []
        return [_strip_quotes(x.strip()) for x in text.split(";") if x.strip()]
    
    def _to_bool(text, default=False):
        if text is None: return default
        return str(text).strip().lower() in ("true","1","yes","y","t")
    
    def _get_text(i, default=None):
        try:
            v = arcpy.GetParameterAsText(i)
            return v if v not in ("", None) else default
        except:
            return default
    
    def _ensure_field(fc, name, ftype="DOUBLE", precision=None, scale=None, length=None):
        fields = [f.name for f in arcpy.ListFields(fc)]
        if name not in fields:
            arcpy.management.AddField(fc, name, ftype, precision, scale, length)
    
    def _delete(path):
        if arcpy.Exists(path):
            arcpy.management.Delete(path)
    
    def _gdb_of(ds):
        try:
            d = arcpy.Describe(ds)
            return getattr(d, "path", arcpy.env.workspace)
        except:
            return arcpy.env.workspace
    
    @contextmanager
    def env_manager(**envs):
        old = {k: getattr(arcpy.env, k) for k in envs}
        for k,v in envs.items(): setattr(arcpy.env, k, v)
        try:
            yield
        finally:
            for k,v in old.items(): setattr(arcpy.env, k, v)
    
    # =========================== NUMERIC PARSING =================================
    
    _NULLS = {""," ","n/a","na","null","none","-","--"}
    _num_re = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
    
    def _parse_float_safe(val):
        if val is None: return None
        if isinstance(val, (int, float)): return float(val)
        s = str(val).strip()
        if s.lower() in _NULLS: return None
        s_clean = s.replace(",", "")
        try:
            return float(s_clean)
        except:
            m = _num_re.search(s_clean)
            return float(m.group(0)) if m else None
    
    # =========================== LOGIC ===========================================
    
    def _span_from_point(ftype, span_txt):
        ft = (ftype or "").strip()
        
        if ft in TYPE_CONFIG:
            return TYPE_CONFIG[ft]
        
        if ft in REQUIRES_SPAN_TXT:
            val = _parse_float_safe(span_txt)
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
        _ensure_field(fc, "Height", "DOUBLE")
        oid = arcpy.Describe(fc).OIDFieldName
        with arcpy.da.UpdateCursor(fc, [oid, "Height_txt", "Height"]) as cur:
            for oidv, txt, h in cur:
                v = _parse_float_safe(txt)
                new_h = v * 1.05 if v is not None else None
                cur.updateRow([oidv, txt, new_h])
    
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
        arcpy.AddMessage("Merging and processing points...")
        arcpy.management.Merge(points, pts_merge)
    
        arcpy.management.CalculateGeometryAttributes(pts_merge, [["Z_Min", "POINT_Z"]])
        _calc_height(pts_merge)
    
        _ensure_field(pts_merge, "Span", "DOUBLE")
        if "Span_txt" not in [f.name for f in arcpy.ListFields(pts_merge)]:
            _ensure_field(pts_merge, "Span_txt", "TEXT", length=50)
    
        with arcpy.da.UpdateCursor(pts_merge, ["OID@", "Type", "Span_txt", "Span"]) as cur:
            for oidv, typ, stxt, sv in cur:
                try:
                    s = _span_from_point(typ, stxt)
                except Exception as ex:
                    arcpy.AddWarning(f"Span skipped OID={oidv}: {ex}")
                    s = None
                cur.updateRow([oidv, typ, stxt, s])
    
        pts_buf = arcpy.CreateUniqueName("pts_buf", mem)
        arcpy.analysis.PairwiseBuffer(pts_merge, pts_buf, "Span")
        buffers = [pts_buf]
    
        # 2. Process Lines (Optional)
        if lines and line_ht:
            arcpy.AddMessage("Processing lines...")
            lns_merge = arcpy.CreateUniqueName("lns_merge", mem)
            lns_ht = arcpy.CreateUniqueName("lns_ht", mem)
            lns_buf = arcpy.CreateUniqueName("lns_buf", mem)
    
            arcpy.management.Merge(lines, lns_merge)
            arcpy.management.Merge(line_ht, lns_ht)
            arcpy.management.JoinField(lns_merge, "ID", lns_ht, "ID", ["Height_txt"])
            
            _calc_height(lns_merge)
            arcpy.management.CalculateGeometryAttributes(lns_merge, [["Z_Min", "EXTENT_MIN_Z"]])
            _ensure_field(lns_merge, "Span", "DOUBLE")
    
            with arcpy.da.UpdateCursor(lns_merge, ["OID@", "Type", "Span"]) as cur:
                for oidv, typ, sv in cur:
                    try:
                        s = _span_from_line(typ)
                    except Exception as ex:
                        arcpy.AddWarning(f"Span skipped OID={oidv}: {ex}")
                        s = None
                    cur.updateRow([oidv, typ, s])
    
            arcpy.analysis.PairwiseBuffer(lns_merge, lns_buf, "Span")
            buffers.append(lns_buf)
    
        # 3. Final Merge & Spatial Join
        _delete(out_mask)
        if len(buffers) == 1:
            arcpy.management.CopyFeatures(buffers[0], out_mask)
        else:
            mbuf = arcpy.CreateUniqueName("mask_merge", mem)
            arcpy.management.Merge(buffers, mbuf)
            arcpy.management.CopyFeatures(mbuf, out_mask)
    
        sj_fc = arcpy.CreateUniqueName("mask_sj", mem)
        
        kwargs = {
            "target_features": out_mask,
            "join_features": bounds,
            "out_feature_class": sj_fc,
            "join_operation": "JOIN_ONE_TO_MANY",
            "join_type": "KEEP_COMMON",
            "match_option": "INTERSECT"
        }
        if search_radius and str(search_radius).strip():
            kwargs["search_radius"] = str(search_radius).strip()
    
        arcpy.AddMessage("Spatial Joining with Bounds...")
        arcpy.analysis.SpatialJoin(**kwargs)
    
        # 4. Split By Attributes (Must go to Workspace, not FeatureDataset)
        out_gdb = workspace or _gdb_of(out_mask)
        arcpy.AddMessage(f"Splitting by 'FileName' into GDB Root: {out_gdb}")
        
        arcpy.analysis.SplitByAttributes(
            Input_Table=sj_fc,
            Target_Workspace=out_gdb,
            Split_Fields=["FileName"]
        )
        
        return out_mask
    
    def main():
        try:
            # Params: 0=Pts, 1=Lns, 2=LnHt, 3=Bnds, 4=Out, 5=Rad, 6=Wksp, 7=Over
            points   = _split_multi_text(_get_text(0))
            lines    = _split_multi_text(_get_text(1))
            line_ht  = _split_multi_text(_get_text(2))
            bounds   = _strip_quotes(_get_text(3))
            out_mask = _strip_quotes(_get_text(4))
            search_r = _get_text(5)
            workspace= _strip_quotes(_get_text(6))
            overwrite= _to_bool(_get_text(7), True)
    
            if not points or not bounds or not out_mask:
                raise arcpy.ExecuteError("Points, Bounds, and Output are required.")
    
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
            return 1
    
    if __name__ == "__main__":
        sys.exit(main())
    
