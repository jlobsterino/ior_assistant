import pandas as pd
import tempfile
import os

# Create a sample excel file where the bigint ID is saved as string (text cell) but has a None (NaN)
df = pd.DataFrame({
    'incdnt_id': ['1234567890123456789', None, '9876543210987654321'],
    'some_val': [1.5, 2.5, 3.5]
})

with tempfile.TemporaryDirectory() as tmpdir:
    filepath = os.path.join(tmpdir, 'test.xlsx')
    df.to_excel(filepath, index=False)
    
    # Read it back with inferred types (no dtype specified)
    df_inferred = pd.read_excel(filepath)
    print("Inferred type of incdnt_id:", df_inferred['incdnt_id'].dtype)
    print("Inferred values:", df_inferred['incdnt_id'].tolist())
    
    # Read it back with specific dtype mapping for ID column
    dtype_dict = {
        'incdnt_id': str,
        'incdnt_sid': str
    }
    
    df_str = pd.read_excel(filepath, dtype=dtype_dict)
    print("\nWith dtype dict mapping:")
    print("Type of incdnt_id:", df_str['incdnt_id'].dtype)
    print("Values of incdnt_id:", df_str['incdnt_id'].tolist())
