# -*- coding: utf-8 -*-
"""
Extract LAS snippets by polygon, grouped by 'Type' field, with optional Z filtering.

Designed for ArcGIS Pro Script Tool (.tbx):
  Parameters (in order):
    0: Feature Dataset (DEFeatureDataset)
    1: LAS folder (DEFolder)
    2: Output folder (DEFolder)
    3: Enable Z filter (GPBoolean)
"""

import arcpy
import os
import laspy
import numpy as np
import shapely
from shapely import from_wkt

def sanitize_type(t):
    """Minimal fast sanitization for folder/file-friendly type strings."""
    if not t:
        return "UnknownType"
    return t.replace("/", "_").replace("\\", "_")

def main():
    # --- Script Tool parameters ---
    feature_dataset = arcpy.GetParameterAsText(0)   # e.g., F:\Project\My.gdb\LAS2
    las_folder      = arcpy.GetParameterAsText(1)   # folder with .las
    output_folder   = arcpy.GetParameterAsText(2)   # output folder
    use_z_filter    = arcpy.GetParameter(3)         # Boolean -> True/False

    # --- Basic validation ---
    if not arcpy.Exists(feature_dataset):
        arcpy.AddError(f"Feature dataset not found: {feature_dataset}")
        raise arcpy.ExecuteError

    if not os.path.isdir(las_folder):
        arcpy.AddError(f"LAS folder does not exist: {las_folder}")
        raise arcpy.ExecuteError

    # Create output folder if missing
    os.makedirs(output_folder, exist_ok=True)

    arcpy.AddMessage(
        f"[Run] Dataset='{feature_dataset}', LAS='{las_folder}', "
        f"Out='{output_folder}', Z filter={'ON' if use_z_filter else 'OFF'}"
    )

    # Workspace is the dataset; ListFeatureClasses() enumerates inside it
    arcpy.env.workspace = feature_dataset

    # Cache for LAS files
    las_cache = {}

    # Your expected fields (from your original script)
    fields = ["SHAPE@", "FileName", "Z_Min", "Height", "Type", "ID", "Path"]

    fcs = arcpy.ListFeatureClasses()
    if not fcs:
        arcpy.AddWarning("No feature classes found in the selected feature dataset.")
        return

    for fc in fcs:
        arcpy.AddMessage(f"Processing feature class: {fc}")
        with arcpy.da.SearchCursor(fc, fields) as cursor:
            for row in cursor:
                geom, las_name, z_min, height, typ, fid = (
                    row[0], row[1], row[2], row[3], row[4], row[5]
                )

                # Optional Z window (preserves your original behavior)
                if use_z_filter:
                    if height is None:
                        min_z = z_min - 1
                        max_z = min_z + 2
                    else:
                        min_z = z_min
                        max_z = z_min + float(height)

                prefix = sanitize_type(typ)
                polygon = from_wkt(geom.WKT)

                # Validate LAS path
                las_path = os.path.join(las_folder, las_name)
                if not os.path.exists(las_path):
                    arcpy.AddWarning(f"Missing LAS: {las_path}")
                    continue

                # Cached read
                if las_name not in las_cache:
                    las_cache[las_name] = laspy.read(las_path)
                las = las_cache[las_name]

                x, y, z = las.x, las.y, las.z

                # Fast bbox prefilter
                minx, miny, maxx, maxy = polygon.bounds
                bbox_mask = ((x >= minx) & (x <= maxx) &
                             (y >= miny) & (y <= maxy))
                if not np.any(bbox_mask):
                    continue

                # Vectorized point-in-polygon on subset
                cand_pts = shapely.points(x[bbox_mask], y[bbox_mask])
                inside_subset = shapely.contains(polygon, cand_pts)

                # Map back to full mask
                spatial_mask = np.zeros_like(bbox_mask, dtype=bool)
                spatial_mask[bbox_mask] = inside_subset

                # Optional Z filter
                if use_z_filter:
                    z_mask = (z >= min_z) & (z <= max_z)
                    final_mask = spatial_mask & z_mask
                else:
                    final_mask = spatial_mask

                if not np.any(final_mask):
                    continue

                # Per-Type output folder + filename
                type_folder = os.path.join(output_folder, prefix)
                os.makedirs(type_folder, exist_ok=True)
                base_name = os.path.splitext(las_name)[0]
                out_name = f"{prefix}_{fid}_{base_name}_clip.las"
                out_path = os.path.join(type_folder, out_name)

                # Write output LAS (preserve header scale/offset and format/version)
                new_las = laspy.create(
                    point_format=las.header.point_format,
                    file_version=las.header.version
                )
                new_las.header.offsets = las.header.offsets
                new_las.header.scales  = las.header.scales
                new_las.points = las.points[final_mask]
                new_las.update_header()
                new_las.write(out_path)
                arcpy.AddMessage(f"Exported: {out_path}")

    arcpy.AddMessage("Everything is done")

if __name__ == "__main__":
    main()