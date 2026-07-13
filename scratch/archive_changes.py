import os
import zipfile
import shutil

# Date for the archive name
date_str = "2026-07-06"
archive_dir = r"d:\ior_assistant\archives"
archive_name = f"{date_str}.zip"
archive_path = os.path.join(archive_dir, archive_name)

# List of files modified and their target flat text names
modified_files = {
    r"d:\ior_assistant\backend\agent\agent_flow.py": "backend_agent_agent_flow.txt",
    r"d:\ior_assistant\backend\agent\tools\run_preset.py": "backend_agent_tools_run_preset.txt",
    r"d:\ior_assistant\backend\skills\runners\excel_inspector.py": "backend_skills_runners_excel_inspector.txt",
    r"d:\ior_assistant\backend\skills\runners\notebook_runner.py": "backend_skills_runners_notebook_runner.txt"
}

# Ensure archives directory exists
os.makedirs(archive_dir, exist_ok=True)

print(f"Creating archive {archive_path}...")
with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
    for src_path, dest_name in modified_files.items():
        if os.path.exists(src_path):
            print(f"Adding {src_path} as {dest_name}...")
            # We can write the content of python file directly into zip with the destination name
            zipf.write(src_path, dest_name)
        else:
            print(f"WARNING: File not found: {src_path}")

print("Archive created successfully!")
