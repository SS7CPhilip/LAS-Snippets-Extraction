import arcpy
    import laspy
    import os
    import numpy as np
    import shapely
    from shapely import from_wkb
    import argparse
    
    def process_las_extraction(gdb_path, feature_dataset, las_folder, output_folder):
        """
        Main extraction logic with memory optimization.
        """
        arcpy.env.workspace = os.path.join(gdb_path, feature_dataset)
        feature_classes = arcpy.ListFeatureClasses()
        
        if not feature_classes:
            print("No feature classes found in the dataset.")
            return
    
        # Order by FileName to process one LAS file at a time (Memory Opt)
        fields = ["SHAPE@", "FileName", "Z_Min", "Height", "Type", "ID"]
        sql_orderby = "ORDER BY FileName"
    
        for fc in feature_classes:
            print(f"Processing Feature Class: {fc}")
            
            with arcpy.da.SearchCursor(fc, fields, sql_clause=(None, sql_orderby)) as cursor:
                
                current_las_name = None
                las_data = None
                
                for row in cursor:
                    geom_arcpy, las_name, z_base, height_delta, feat_type, fid = row
                    
                    # --- 1. Load LAS File (Only when filename changes) ---
                    if las_name != current_las_name:
                        las_data = None # Force garbage collection
                        current_las_name = las_name
                        
                        las_path = os.path.join(las_folder, las_name)
                        if os.path.exists(las_path):
                            print(f"  Loading LAS: {las_name}...")
                            try:
                                las_data = laspy.read(las_path)
                            except Exception as e:
                                print(f"    [Error] Failed to read {las_name}: {e}")
                                las_data = None
                        else:
                            print(f"    [Warning] Missing LAS: {las_path}")
                            las_data = None
    
                    if las_data is None:
                        continue
    
                    # --- 2. Geometry Prep ---
                    # Use WKB for faster geometry conversion
                    polygon = from_wkb(geom_arcpy.WKB)
                    
                    # Z-Window Calculation
                    min_z = z_base if z_base is not None else -9999
                    h_val = float(height_delta) if height_delta is not None else 2.0 
                    max_z = min_z + h_val
    
                    # --- 3. Filtering ---
                    x, y, z = las_data.x, las_data.y, las_data.z
                    
                    # Bounding Box Filter (Fast)
                    minx, miny, maxx, maxy = polygon.bounds
                    bbox_mask = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
                    
                    if not np.any(bbox_mask):
                        continue
    
                    # Vectorized Point-in-Poly (Precise)
                    subset_x = x[bbox_mask]
                    subset_y = y[bbox_mask]
                    
                    cand_pts = shapely.points(subset_x, subset_y)
                    inside_subset = shapely.contains(polygon, cand_pts)
                    
                    # Reconstruct full-length mask
                    final_mask = np.zeros(len(x), dtype=bool)
                    
                    # Combine spatial match + Z match
                    z_match = (z[bbox_mask] >= min_z) & (z[bbox_mask] <= max_z)
                    temp_mask = inside_subset & z_match
                    
                    final_mask[bbox_mask] = temp_mask
    
                    if not np.any(final_mask):
                        continue
    
                    # --- 4. Export ---
                    prefix = sanitize_type(feat_type)
                    type_folder = os.path.join(output_folder, prefix)
                    os.makedirs(type_folder, exist_ok=True)
                    
                    out_name = f"{prefix}_{fid}_{las_name[:-4]}_clip.las"
                    out_path = os.path.join(type_folder, out_name)
    
                    new_las = laspy.create(
                        point_format=las_data.header.point_format, 
                        file_version=las_data.header.version
                    )
                    new_las.header.offsets = las_data.header.offsets
                    new_las.header.scales = las_data.header.scales
                    new_las.points = las_data.points[final_mask]
                    new_las.write(out_path)
    
    def sanitize_type(t):
        return (t or "Unknown").replace("/", "_").replace("\", "_")
    
    if __name__ == "__main__":
        # Allow running with arguments or falling back to defaults
        parser = argparse.ArgumentParser(description="Extract LAS features based on GDB masks.")
        parser.add_argument("--gdb", help="Path to Geodatabase")
        parser.add_argument("--dataset", help="Feature Dataset Name")
        parser.add_argument("--las", help="Folder containing source LAS files")
        parser.add_argument("--out", help="Output folder")
        
        args = parser.parse_args()
        
        # Default values (for testing/IDE use)
        GDB_PATH = args.gdb
        DATASET = args.dataset
        LAS_FOLDER = args.las
        OUT_FOLDER = args.out
    
        process_las_extraction(GDB_PATH, DATASET, LAS_FOLDER, OUT_FOLDER)
        print("Extraction Complete.")
    
