#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2025 Stephen Le

"""
Cherry Studio Sync

Sync your Cherry Studio data across multiple computers and operating systems.

This is a manual sync tool that merges backup files from multiple computers
into unified backups. Each computer gets a merged backup with all conversations
while preserving its machine-specific settings (paths, preferences).

Uses timestamp-based conflict resolution (most recent wins).
Orphaned items from older backups are skipped (assumed deleted).

Usage:
    python cherry_studio_sync.py                    # Auto-discover and merge
    python cherry_studio_sync.py --all              # Include all backups
    python cherry_studio_sync.py --gui              # Launch graphical interface
"""

import argparse
import copy
import json
import os
import re
import shutil
import tempfile
import threading
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


# Pattern: cherry-studio.<timestamp>.<hostname>.<os>.zip
BACKUP_PATTERN = re.compile(r'^cherry-studio\.(\d{14})\.(.+)\.(mac|windows|linux)\.zip$')

# Keys in persist:cherry-studio that contain machine-specific settings (paths, etc.)
MACHINE_SPECIFIC_KEYS = {'settings', 'backup', 'shortcuts'}

# Keys that contain shareable data (conversations, etc.)
DATA_KEYS = {'assistants', 'knowledge', 'memory', 'paintings', 'note'}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync Cherry Studio data across multiple computers"
    )
    parser.add_argument(
        "backups",
        nargs="*",
        help="Backup zip files to merge (default: auto-discover latest from each computer)"
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Merge all backups, not just the latest from each computer"
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch graphical user interface"
    )
    return parser.parse_args()


def parse_backup_filename(filename: str) -> dict | None:
    """Parse a Cherry Studio backup filename to extract metadata."""
    match = BACKUP_PATTERN.match(filename)
    if not match:
        return None

    timestamp_str, hostname, os_type = match.groups()
    return {
        "timestamp_str": timestamp_str,
        "hostname": hostname,
        "os": os_type,
        "computer_id": f"{hostname}.{os_type}",
    }


def discover_backups(directory: Path = None) -> list[dict]:
    """Discover Cherry Studio backups in a directory."""
    if directory is None:
        directory = Path.cwd()

    backups = []
    for file_path in directory.glob("cherry-studio.*.zip"):
        # Skip merged backups
        if ".merged." in file_path.name:
            continue
        meta = parse_backup_filename(file_path.name)
        if meta:
            meta["path"] = str(file_path)
            backups.append(meta)

    return backups


def group_backups_by_computer(backups: list[dict]) -> dict[str, list[dict]]:
    """Group backups by computer ID and sort each group by timestamp."""
    by_computer = defaultdict(list)

    for backup in backups:
        by_computer[backup["computer_id"]].append(backup)

    # Sort each computer's backups by timestamp (newest first)
    for computer_id in by_computer:
        by_computer[computer_id].sort(key=lambda x: x["timestamp_str"], reverse=True)

    return dict(by_computer)


def extract_backup(zip_path: str, temp_dir: str) -> dict:
    """Extract a backup zip and parse its data.json."""
    extract_path = Path(temp_dir) / Path(zip_path).stem
    extract_path.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_path)

    data_json_path = extract_path / "data.json"
    with open(data_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Parse filename for metadata
    meta = parse_backup_filename(Path(zip_path).name) or {}

    return {
        "path": zip_path,
        "extract_path": extract_path,
        "data": data,
        "timestamp": data.get("time", 0),
        "computer_id": meta.get("computer_id", "unknown"),
    }


def merge_by_id(older: list, newer: list, id_key: str = "id",
                time_key: str = None, stats: dict = None) -> list:
    """
    Merge two lists of objects by ID.
    Items from newer take precedence.
    Items only in older are skipped (orphan deletion strategy).
    """
    newer_ids = {item.get(id_key) for item in newer if item.get(id_key)}
    older_ids = {item.get(id_key) for item in older if item.get(id_key)}

    # Build result from newer items
    result = {item.get(id_key): item for item in newer if item.get(id_key)}

    # Add items from older that also exist in newer (merge conflicts)
    for item in older:
        item_id = item.get(id_key)
        if not item_id:
            continue

        if item_id in newer_ids:
            if stats:
                stats["conflicts"] += 1
        else:
            if stats:
                stats["skipped_orphans"] += 1

    new_only = newer_ids - older_ids
    if stats:
        stats["new_items"] += len(new_only)

    return list(result.values())


def merge_assistants(older_data: dict, newer_data: dict, stats: dict) -> dict:
    """Merge assistant structures, including nested topics.

    Preserves all top-level keys (presets, tagsOrder, collapsedTags, etc.)
    while applying special merge logic only to the assistants array.
    """
    # Start with older_data, then overlay newer_data on top.
    # This preserves keys like presets/tagsOrder from older backups
    # when newer backups don't have them.
    result = copy.deepcopy(older_data)
    for key, value in newer_data.items():
        if key != "assistants":  # assistants handled specially below
            result[key] = copy.deepcopy(value)

    # Only apply special merge logic to the assistants array
    older_assistants = older_data.get("assistants", [])
    newer_assistants = newer_data.get("assistants", [])

    newer_ids = {a.get("id") for a in newer_assistants}
    merged_assistants = {}

    for asst in newer_assistants:
        asst_id = asst.get("id")
        if asst_id:
            merged_assistants[asst_id] = copy.deepcopy(asst)

    for old_asst in older_assistants:
        asst_id = old_asst.get("id")
        if not asst_id or asst_id not in newer_ids:
            stats["skipped_orphans"] += 1
            continue

        # Merge topics for assistants that exist in both
        new_asst = merged_assistants[asst_id]
        old_topics = old_asst.get("topics", [])
        new_topics = new_asst.get("topics", [])

        merged_topics = merge_by_id(old_topics, new_topics, "id", "updatedAt", stats)
        merged_assistants[asst_id]["topics"] = merged_topics

    # Update the assistants array in result
    result["assistants"] = list(merged_assistants.values())

    # Ensure defaultAssistant is set (prefer newer, fallback to older)
    if "defaultAssistant" not in result or not result["defaultAssistant"]:
        result["defaultAssistant"] = older_data.get("defaultAssistant")

    return result


def merge_persist_data_only(older: dict, newer: dict, stats: dict) -> dict:
    """Merge only the data portions of persist:cherry-studio (not settings)."""
    result = {}
    all_keys = set(older.keys()) | set(newer.keys())

    for key in all_keys:
        older_val = older.get(key)
        newer_val = newer.get(key)

        older_was_string = isinstance(older_val, str) if older_val else False
        newer_was_string = isinstance(newer_val, str) if newer_val else False

        if older_was_string:
            try:
                older_val = json.loads(older_val)
            except (json.JSONDecodeError, TypeError):
                pass
        if newer_was_string:
            try:
                newer_val = json.loads(newer_val)
            except (json.JSONDecodeError, TypeError):
                pass

        if key == "assistants":
            merged_val = merge_assistants(
                older_val if isinstance(older_val, dict) else {},
                newer_val if isinstance(newer_val, dict) else {},
                stats
            )
            if newer_was_string or older_was_string:
                result[key] = json.dumps(merged_val)
            else:
                result[key] = merged_val
        elif newer_val is not None:
            result[key] = newer.get(key)
        else:
            result[key] = older.get(key)

    return result


def merge_indexeddb(older: dict, newer: dict, stats: dict) -> dict:
    """Merge indexedDB databases."""
    result = {}
    all_dbs = set(older.keys()) | set(newer.keys())

    for db_name in all_dbs:
        older_db = older.get(db_name, [])
        newer_db = newer.get(db_name, [])

        if isinstance(older_db, list) and isinstance(newer_db, list):
            if db_name == "message_blocks":
                result[db_name] = merge_by_id(older_db, newer_db, "id", "createdAt", stats)
            else:
                result[db_name] = merge_by_id(older_db, newer_db, "id", None, stats)
        elif newer_db:
            result[db_name] = newer_db
        else:
            result[db_name] = older_db

    return result


def merge_all_backups(backup_infos: list, stats: dict) -> tuple[dict, dict]:
    """
    Merge all backups into unified data, keeping track of per-machine settings.

    Returns:
        merged_data: The merged conversation data
        machine_settings: Dict mapping computer_id -> their latest settings
    """
    sorted_backups = sorted(backup_infos, key=lambda x: x["timestamp"])
    machine_settings = {}

    print(f"\nMerge order (oldest to newest):")
    for i, b in enumerate(sorted_backups):
        ts = datetime.fromtimestamp(b["timestamp"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {i+1}. {Path(b['path']).name} ({ts})")

    # Start with oldest as base
    result = copy.deepcopy(sorted_backups[0]["data"])

    # Store first machine's settings
    first_local = result.get("localStorage", {})
    first_persist_raw = first_local.get("persist:cherry-studio", "{}")
    first_persist = json.loads(first_persist_raw) if isinstance(first_persist_raw, str) else first_persist_raw
    machine_settings[sorted_backups[0]["computer_id"]] = {
        "localStorage": copy.deepcopy(first_local),
        "persist": copy.deepcopy(first_persist),
    }

    # Merge each subsequent backup
    for backup in sorted_backups[1:]:
        print(f"\nMerging: {Path(backup['path']).name}")
        newer_data = backup["data"]

        # Store this machine's settings
        newer_local = newer_data.get("localStorage", {})
        newer_persist_raw = newer_local.get("persist:cherry-studio", "{}")
        newer_persist = json.loads(newer_persist_raw) if isinstance(newer_persist_raw, str) else newer_persist_raw
        machine_settings[backup["computer_id"]] = {
            "localStorage": copy.deepcopy(newer_local),
            "persist": copy.deepcopy(newer_persist),
        }

        # Merge localStorage data
        older_local = result.get("localStorage", {})
        older_persist_raw = older_local.get("persist:cherry-studio", "{}")
        older_persist = json.loads(older_persist_raw) if isinstance(older_persist_raw, str) else older_persist_raw

        merged_persist = merge_persist_data_only(older_persist, newer_persist, stats)

        result["localStorage"] = newer_local.copy()
        result["localStorage"]["persist:cherry-studio"] = json.dumps(merged_persist)

        # Merge indexedDB
        older_indexed = result.get("indexedDB", {})
        newer_indexed = newer_data.get("indexedDB", {})
        result["indexedDB"] = merge_indexeddb(older_indexed, newer_indexed, stats)

        result["time"] = newer_data.get("time", result.get("time"))
        result["version"] = newer_data.get("version", result.get("version"))

    return result, machine_settings


def apply_machine_settings(merged_data: dict, machine_settings: dict) -> dict:
    """Apply a specific machine's settings to merged data."""
    result = copy.deepcopy(merged_data)

    # Get the merged persist data
    merged_local = result.get("localStorage", {})
    merged_persist_raw = merged_local.get("persist:cherry-studio", "{}")
    merged_persist = json.loads(merged_persist_raw) if isinstance(merged_persist_raw, str) else merged_persist_raw

    # Get machine's original settings
    machine_persist = machine_settings.get("persist", {})

    # Replace machine-specific keys with the machine's original values
    for key in MACHINE_SPECIFIC_KEYS:
        if key in machine_persist:
            merged_persist[key] = machine_persist[key]

    # Update the result
    result["localStorage"]["persist:cherry-studio"] = json.dumps(merged_persist)

    return result


def create_output_zip(merged_data: dict, backup_infos: list, output_path: str, temp_dir: str):
    """Create the merged backup zip file."""
    output_temp = Path(temp_dir) / f"output_{Path(output_path).stem}"
    output_temp.mkdir(parents=True, exist_ok=True)

    data_json_path = output_temp / "data.json"
    with open(data_json_path, 'w', encoding='utf-8') as f:
        json.dump(merged_data, f, ensure_ascii=False)

    # Merge Data folders from all backups
    for backup in backup_infos:
        src_data = backup["extract_path"] / "Data"
        if src_data.exists():
            dest_data = output_temp / "Data"
            shutil.copytree(src_data, dest_data, dirs_exist_ok=True)

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in output_temp.rglob('*'):
            if file_path.is_file():
                arcname = file_path.relative_to(output_temp)
                zf.write(file_path, arcname)

    return output_path


class MergeGUI:
    """Graphical user interface for Cherry Studio Sync."""

    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox

        self.root = tk.Tk()
        self.root.title("Cherry Studio Sync")
        self.root.geometry("600x500")
        self.root.minsize(500, 400)

        self.merge_all_var = tk.BooleanVar(value=False)
        self.is_merging = False

        self._setup_ui(tk, ttk, scrolledtext)

    def _setup_ui(self, tk, ttk, scrolledtext):
        """Set up the user interface components."""
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)

        # Directory selection
        ttk.Label(main_frame, text="Backup Directory:").grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )

        dir_frame = ttk.Frame(main_frame)
        dir_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        dir_frame.columnconfigure(0, weight=1)

        self.dir_var = tk.StringVar(value=str(Path.cwd()))
        self.dir_entry = ttk.Entry(dir_frame, textvariable=self.dir_var)
        self.dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))

        browse_btn = ttk.Button(dir_frame, text="Browse...", command=self._browse_directory)
        browse_btn.grid(row=0, column=1)

        # Options
        options_frame = ttk.LabelFrame(main_frame, text="Options", padding="5")
        options_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        ttk.Checkbutton(
            options_frame,
            text="Merge all backups (not just latest per computer)",
            variable=self.merge_all_var
        ).grid(row=0, column=0, sticky="w")

        # Merge button
        self.merge_btn = ttk.Button(
            main_frame, text="Sync Backups", command=self._start_merge
        )
        self.merge_btn.grid(row=3, column=0, columnspan=3, pady=(0, 10))

        # Status log
        ttk.Label(main_frame, text="Status:").grid(
            row=4, column=0, sticky="w", pady=(0, 5)
        )

        self.log_text = scrolledtext.ScrolledText(
            main_frame, height=15, state="disabled", wrap=tk.WORD
        )
        self.log_text.grid(row=5, column=0, columnspan=3, sticky="nsew")
        main_frame.rowconfigure(5, weight=1)

    def _browse_directory(self):
        """Open directory browser dialog."""
        directory = self.filedialog.askdirectory(
            initialdir=self.dir_var.get(),
            title="Select Cherry Studio Backup Directory"
        )
        if directory:
            self.dir_var.set(directory)

    def _log(self, message: str):
        """Append a message to the log area."""
        self.log_text.configure(state="normal")
        self.log_text.insert(self.tk.END, message + "\n")
        self.log_text.see(self.tk.END)
        self.log_text.configure(state="disabled")
        self.root.update_idletasks()

    def _clear_log(self):
        """Clear the log area."""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", self.tk.END)
        self.log_text.configure(state="disabled")

    def _start_merge(self):
        """Start the merge operation in a background thread."""
        if self.is_merging:
            return

        self.is_merging = True
        self.merge_btn.configure(state="disabled")
        self._clear_log()

        thread = threading.Thread(target=self._run_merge, daemon=True)
        thread.start()

    def _run_merge(self):
        """Execute the merge operation."""
        try:
            directory = Path(self.dir_var.get())
            merge_all = self.merge_all_var.get()

            self._log("Cherry Studio Sync")
            self._log("==================")
            self._log(f"Directory: {directory}")
            self._log("")

            # Discover backups
            backup_metas = discover_backups(directory)

            if not backup_metas:
                self._log("No Cherry Studio backups found.")
                self._log("Expected filename format: cherry-studio.<timestamp>.<hostname>.<os>.zip")
                self._finish_merge(success=False)
                return

            # Group by computer
            by_computer = group_backups_by_computer(backup_metas)

            if len(by_computer) < 2:
                self._log(f"Error: Need backups from at least 2 computers to sync")
                self._log(f"(Found backups from only {len(by_computer)} computer)")
                self._finish_merge(success=False)
                return

            # Select which backups to merge
            if merge_all:
                selected_paths = [b["path"] for b in backup_metas]
                self._log(f"Syncing all {len(selected_paths)} backups from {len(by_computer)} computers")
            else:
                selected_paths = []
                self._log(f"Discovered {len(backup_metas)} backups from {len(by_computer)} computers:")
                for computer_id, backups in sorted(by_computer.items()):
                    latest = backups[0]
                    selected_paths.append(latest["path"])
                    self._log(f"  {computer_id}:")
                    self._log(f"    Latest: {Path(latest['path']).name}")
                    if len(backups) > 1:
                        self._log(f"    (skipping {len(backups) - 1} older backups)")

            stats = {
                "conflicts": 0,
                "skipped_orphans": 0,
                "new_items": 0,
            }

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            output_files = []

            with tempfile.TemporaryDirectory() as temp_dir:
                # Extract all selected backups
                self._log(f"\nExtracting backups...")
                backup_infos = []
                for path in selected_paths:
                    self._log(f"  Extracting: {Path(path).name}")
                    info = extract_backup(path, temp_dir)
                    backup_infos.append(info)

                # Merge all data
                self._log("\nMerging data...")
                merged_data, machine_settings = merge_all_backups(backup_infos, stats)

                # Create a merged backup for each computer
                self._log(f"\nCreating synced backups...")
                for computer_id in sorted(by_computer.keys()):
                    machine_merged = apply_machine_settings(merged_data, machine_settings[computer_id])
                    output_path = directory / f"cherry-studio.{timestamp}.{computer_id}.merged.zip"
                    create_output_zip(machine_merged, backup_infos, str(output_path), temp_dir)
                    output_files.append(output_path)
                    self._log(f"  Created: {output_path.name}")

            # Print statistics
            self._log(f"\n==================")
            self._log(f"Sync Statistics:")
            self._log(f"  Conflicts resolved (newer won): {stats['conflicts']}")
            self._log(f"  Orphans skipped (likely deleted): {stats['skipped_orphans']}")
            self._log(f"  New items added: {stats['new_items']}")
            self._log(f"\nCreated {len(output_files)} synced backups:")
            for f in output_files:
                self._log(f"  - {f.name}")
            self._log(f"\nTo import: Open Cherry Studio > Settings > Data > Restore")
            self._log(f"           Use the backup matching your computer.")

            self._finish_merge(success=True, output_files=output_files)

        except Exception as e:
            self._log(f"\nError: {e}")
            self._finish_merge(success=False)

    def _finish_merge(self, success: bool, output_files: list = None):
        """Complete the merge operation and re-enable UI."""
        self.is_merging = False
        self.root.after(0, lambda: self.merge_btn.configure(state="normal"))

        if success and output_files:
            self.root.after(0, lambda: self.messagebox.showinfo(
                "Sync Complete",
                f"Successfully created {len(output_files)} synced backup(s).\n\n"
                "To import: Open Cherry Studio > Settings > Data > Restore\n"
                "Use the backup matching your computer."
            ))
        elif not success:
            self.root.after(0, lambda: self.messagebox.showerror(
                "Sync Failed",
                "The sync operation failed. Check the log for details."
            ))

    def run(self):
        """Start the GUI main loop."""
        self.root.mainloop()


def run_gui():
    """Launch the graphical user interface."""
    try:
        gui = MergeGUI()
        gui.run()
        return 0
    except ImportError:
        print("Error: tkinter is not available.")
        print("On Linux, install it with: sudo apt-get install python3-tk")
        return 1


def main():
    args = parse_args()

    # Launch GUI if requested
    if args.gui:
        return run_gui()

    print("Cherry Studio Sync")
    print("==================")

    # Discover backups
    if args.backups:
        # User specified files - parse them for metadata
        backup_metas = []
        for path in args.backups:
            if not os.path.exists(path):
                print(f"Error: File not found: {path}")
                return 1
            meta = parse_backup_filename(Path(path).name)
            if meta:
                meta["path"] = path
                backup_metas.append(meta)
            else:
                print(f"Warning: {path} doesn't match expected naming pattern")
    else:
        backup_metas = discover_backups()

    if not backup_metas:
        print("\nNo Cherry Studio backups found.")
        print("Expected filename format: cherry-studio.<timestamp>.<hostname>.<os>.zip")
        return 1

    # Group by computer
    by_computer = group_backups_by_computer(backup_metas)

    if len(by_computer) < 2:
        print("\nError: Need backups from at least 2 computers to sync")
        print(f"(Found backups from only {len(by_computer)} computer)")
        return 1

    # Select which backups to merge
    if args.all:
        selected_paths = [b["path"] for b in backup_metas]
        print(f"\nSyncing all {len(selected_paths)} backups from {len(by_computer)} computers")
    else:
        selected_paths = []
        print(f"\nDiscovered {len(backup_metas)} backups from {len(by_computer)} computers:")
        for computer_id, backups in sorted(by_computer.items()):
            latest = backups[0]  # Already sorted newest first
            selected_paths.append(latest["path"])
            print(f"  {computer_id}:")
            print(f"    Latest: {Path(latest['path']).name}")
            if len(backups) > 1:
                print(f"    (skipping {len(backups) - 1} older backups)")

    stats = {
        "conflicts": 0,
        "skipped_orphans": 0,
        "new_items": 0,
    }

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    output_files = []

    with tempfile.TemporaryDirectory() as temp_dir:
        # Extract all selected backups
        print(f"\nExtracting backups...")
        backup_infos = []
        for path in selected_paths:
            print(f"  Extracting: {Path(path).name}")
            info = extract_backup(path, temp_dir)
            backup_infos.append(info)

        # Merge all data
        merged_data, machine_settings = merge_all_backups(backup_infos, stats)

        # Create a merged backup for each computer
        print(f"\nCreating synced backups...")
        for computer_id in sorted(by_computer.keys()):
            # Apply this machine's settings to the merged data
            machine_merged = apply_machine_settings(merged_data, machine_settings[computer_id])

            # Generate output filename
            output_path = f"cherry-studio.{timestamp}.{computer_id}.merged.zip"
            create_output_zip(machine_merged, backup_infos, output_path, temp_dir)
            output_files.append(output_path)
            print(f"  Created: {output_path}")

    # Print statistics
    print(f"\n==================")
    print(f"Sync Statistics:")
    print(f"  Conflicts resolved (newer won): {stats['conflicts']}")
    print(f"  Orphans skipped (likely deleted): {stats['skipped_orphans']}")
    print(f"  New items added: {stats['new_items']}")
    print(f"\nCreated {len(output_files)} synced backups:")
    for f in output_files:
        print(f"  - {f}")
    print(f"\nTo import: Open Cherry Studio > Settings > Data > Restore")
    print(f"           Use the backup matching your computer.")

    return 0


if __name__ == "__main__":
    exit(main())
