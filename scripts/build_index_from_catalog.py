import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "backend" / "agent" / "schema"

def main():
    catalog_path = SCHEMA_DIR / "kb_value_catalog.json"
    index_path = SCHEMA_DIR / "kb_value_index.json"

    print(f"Reading value catalog from: {catalog_path}")
    if not catalog_path.exists():
        print(f"Error: {catalog_path} does not exist.")
        return

    with open(catalog_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    inverted_index = {}
    columns_data = catalog.get("columns", {})

    print(f"Processing {len(columns_data)} columns...")
    for full_col_name, info in columns_data.items():
        values = info.get("values", [])
        counts = info.get("counts", {})
        
        for val in values:
            sval = str(val)
            norm_val = sval.strip().lower()
            
            # Get the exact count for this value
            cnt = counts.get(sval, counts.get(val, 0))
            
            if norm_val not in inverted_index:
                inverted_index[norm_val] = []
                
            inverted_index[norm_val].append({
                "column": full_col_name,
                "value": sval,
                "count": int(cnt)
            })

    print(f"Inverted index size: {len(inverted_index)} unique normalized values.")
    
    # Save index
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(inverted_index, f, ensure_ascii=False, indent=2)
        
    print(f"Successfully wrote index to {index_path}")

if __name__ == "__main__":
    main()
