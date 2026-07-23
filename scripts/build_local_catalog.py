import json
import sqlite3
from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "local_kb.duckdb"
SCHEMA_DIR = ROOT / "backend" / "agent" / "schema"

# Categorical column candidates to index
CATEGORICAL_COLS = {
    "d6_base_of_knowledge_ior": [
        "org_struct_lvl_2_name", "org_struct_lvl_3_name", "org_struct_lvl_4_name",
        "org_struct_lvl_5_name", "org_struct_lvl_6_name", "org_struct_lvl_7_name",
        "process_lvl_1_name", "process_lvl_2_name", "process_lvl_3_name", "process_lvl_4_name",
        "funct_block_lvl_2_name", "funct_block_lvl_3_name", "funct_block_lvl_4_name",
        "risk_profile_id", "risk_profile_name", "incdnt_type_lvl_1_name", "incdnt_type_lvl_2_name",
        "incdnt_status_name", "src_type_lvl_1_name", "src_type_lvl_2_name",
        "incdnt_client_type_name", "incdnt_source_name", "incdnt_autoreg_flag"
    ],
    "d6_base_of_knowledge_incident_fin_impact": [
        "fin_impact_type_name", "fin_impact_kind_name", "fin_impact_monitoring_flag"
    ],
    "d6_base_of_knowledge_incident_nonfin_impact": [
        "nonfin_impact_kind_name", "nonfin_impact_influence_class_name"
    ],
    "d6_base_of_knowledge_incident_recovery": [
        "recovery_type_name"
    ],
    "d6_base_of_knowledge_incident_stts_chng": [
        "incdnt_status_name", "stts_chng_action_name", "stts_chng_action_code"
    ]
}

def main():
    print(f"Connecting to DuckDB at: {DB_PATH}")
    if not DB_PATH.exists():
        print("Error: local_kb.duckdb does not exist. Please run gen_local_data.py first.")
        return

    conn = duckdb.connect(str(DB_PATH), read_only=True)
    catalog = {"columns": {}}
    inverted_index = {}

    for table, cols in CATEGORICAL_COLS.items():
        print(f"Processing table: {table}...")
        for col in cols:
            full_col_name = f"{table}.{col}"
            # 1. Calculate filled_pct
            total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            non_null_rows = conn.execute(f"SELECT COUNT({col}) FROM {table}").fetchone()[0]
            filled_pct = round(100.0 * non_null_rows / max(total_rows, 1), 2)

            # 2. Get distinct values and counts
            res = conn.execute(f"SELECT {col}, COUNT(*) FROM {table} WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 500").fetchall()
            
            values = []
            counts = {}
            for val, cnt in res:
                sval = str(val)
                values.append(sval)
                counts[sval] = cnt

                # 3. Add to inverted index
                norm_val = sval.strip().lower()
                if norm_val not in inverted_index:
                    inverted_index[norm_val] = []
                inverted_index[norm_val].append({
                    "column": full_col_name,
                    "value": sval,
                    "count": cnt
                })

            catalog["columns"][full_col_name] = {
                "filled_pct": filled_pct,
                "values": values,
                "counts": counts
            }
            print(f"  Col {col}: filled={filled_pct}%, values={len(values)}")

    # Ensure output dir exists
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    
    # Write catalog
    catalog_path = SCHEMA_DIR / "kb_value_catalog.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote value catalog to {catalog_path}")

    # Write inverted index
    index_path = SCHEMA_DIR / "kb_value_index.json"
    index_path.write_text(json.dumps(inverted_index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote inverted value index to {index_path}")

    conn.close()

if __name__ == "__main__":
    main()
