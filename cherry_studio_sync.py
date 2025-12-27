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

# Pattern for merged backups: cherry-studio.<timestamp>.<hostname>.<os>.merged.zip
MERGED_BACKUP_PATTERN = re.compile(r'^cherry-studio\.(\d{14})\.(.+)\.(mac|windows|linux)\.merged\.zip$')

# Keys in persist:cherry-studio that contain machine-specific settings (paths, etc.)
MACHINE_SPECIFIC_KEYS = {'settings', 'backup', 'shortcuts'}

# Keys that contain shareable data (conversations, etc.)
DATA_KEYS = {'assistants', 'knowledge', 'memory', 'paintings', 'note'}


def migrate_assistants_format(assistants_data: dict) -> dict:
    """Migrate old backup format to new format by adding missing fields.

    Cherry Studio updates may add new required fields. This function ensures
    old backups have all fields needed by the current version.
    """
    if not isinstance(assistants_data, dict):
        return assistants_data

    # Add missing top-level keys with defaults
    if 'tagsOrder' not in assistants_data:
        assistants_data['tagsOrder'] = []
    if 'collapsedTags' not in assistants_data:
        assistants_data['collapsedTags'] = []
    if 'presets' not in assistants_data:
        assistants_data['presets'] = []
    if 'unifiedListOrder' not in assistants_data:
        assistants_data['unifiedListOrder'] = []

    # Add missing fields to each assistant
    for assistant in assistants_data.get('assistants', []):
        if 'enableGenerateImage' not in assistant:
            assistant['enableGenerateImage'] = False
        if 'knowledge_bases' not in assistant:
            assistant['knowledge_bases'] = []

    return assistants_data


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
    parser.add_argument(
        "--prune",
        type=int,
        metavar="N",
        help="Prune old merged backups, keeping only N per computer"
    )
    parser.add_argument(
        "--include-knowledge-base",
        action="store_true",
        help="Include Knowledge Base files in merged backups (excluded by default)"
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


def discover_merged_backups(directory: Path = None) -> list[dict]:
    """Discover merged Cherry Studio backups in a directory."""
    if directory is None:
        directory = Path.cwd()

    backups = []
    for file_path in directory.glob("cherry-studio.*.merged.zip"):
        match = MERGED_BACKUP_PATTERN.match(file_path.name)
        if match:
            timestamp_str, hostname, os_type = match.groups()
            backups.append({
                "path": file_path,
                "timestamp_str": timestamp_str,
                "hostname": hostname,
                "os": os_type,
                "computer_id": f"{hostname}.{os_type}",
            })

    return backups


def prune_merged_backups(directory: Path, keep_count: int) -> list[Path]:
    """
    Prune old merged backups, keeping only the newest `keep_count` per computer.

    Returns list of deleted file paths.
    """
    if keep_count < 1:
        return []

    merged_backups = discover_merged_backups(directory)
    if not merged_backups:
        return []

    # Group by computer
    by_computer = defaultdict(list)
    for backup in merged_backups:
        by_computer[backup["computer_id"]].append(backup)

    deleted = []
    for computer_id, backups in by_computer.items():
        # Sort by timestamp (newest first)
        backups.sort(key=lambda x: x["timestamp_str"], reverse=True)

        # Delete all but the newest keep_count
        for backup in backups[keep_count:]:
            path = backup["path"]
            try:
                path.unlink()
                deleted.append(path)
            except OSError as e:
                print(f"Warning: Could not delete {path}: {e}")

    return deleted


def extract_backup(zip_path: str, temp_dir: str) -> dict:
    """Extract a backup zip and parse its data.json."""
    extract_path = Path(temp_dir) / Path(zip_path).stem
    extract_path.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_path)

    data_json_path = extract_path / "data.json"
    with open(data_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Migrate assistants format to ensure compatibility with newer Cherry Studio
    local_storage = data.get("localStorage", {})
    persist_raw = local_storage.get("persist:cherry-studio")
    if persist_raw:
        try:
            persist = json.loads(persist_raw) if isinstance(persist_raw, str) else persist_raw
            assistants_raw = persist.get("assistants")
            if assistants_raw:
                assistants = json.loads(assistants_raw) if isinstance(assistants_raw, str) else assistants_raw
                assistants = migrate_assistants_format(assistants)
                persist["assistants"] = json.dumps(assistants) if isinstance(assistants_raw, str) else assistants
                data["localStorage"]["persist:cherry-studio"] = json.dumps(persist)
        except (json.JSONDecodeError, TypeError):
            pass  # Skip migration if JSON is malformed

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
    # Migrate both inputs to ensure they have all required fields
    older_data = migrate_assistants_format(older_data)
    newer_data = migrate_assistants_format(newer_data)

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


def create_output_zip(merged_data: dict, backup_infos: list, output_path: str, temp_dir: str,
                      skip_knowledge_base: bool = True):
    """Create the merged backup zip file.

    Args:
        merged_data: The merged JSON data
        backup_infos: List of backup info dicts with extract_path
        output_path: Path for the output zip file
        temp_dir: Temporary directory for staging
        skip_knowledge_base: If True (default), exclude Data/KnowledgeBase/ from output
    """
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
            if skip_knowledge_base:
                # Copy everything except KnowledgeBase
                def ignore_knowledge_base(dir_path, names):
                    if Path(dir_path) == src_data:
                        return ['KnowledgeBase'] if 'KnowledgeBase' in names else []
                    return []
                shutil.copytree(src_data, dest_data, dirs_exist_ok=True, ignore=ignore_knowledge_base)
            else:
                shutil.copytree(src_data, dest_data, dirs_exist_ok=True)

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in output_temp.rglob('*'):
            if file_path.is_file():
                arcname = file_path.relative_to(output_temp)
                zf.write(file_path, arcname)

    return output_path


class MergeWorker:
    """Worker class to run merge operation in background thread."""

    def __init__(self, directory: Path, merge_all: bool, skip_knowledge_base: bool,
                 prune_count: int | None, log_callback, finished_callback):
        self.directory = directory
        self.merge_all = merge_all
        self.skip_knowledge_base = skip_knowledge_base
        self.prune_count = prune_count
        self.log = log_callback
        self.finished = finished_callback

    def run(self):
        """Execute the merge operation."""
        try:
            self.log("Cherry Studio Sync")
            self.log("==================")
            self.log(f"Directory: {self.directory}")
            self.log("")

            # Discover backups
            backup_metas = discover_backups(self.directory)

            if not backup_metas:
                self.log("No Cherry Studio backups found.")
                self.log("Expected filename format: cherry-studio.<timestamp>.<hostname>.<os>.zip")
                self.finished(False, [])
                return

            # Group by computer
            by_computer = group_backups_by_computer(backup_metas)

            if len(by_computer) < 2:
                self.log(f"Error: Need backups from at least 2 computers to sync")
                self.log(f"(Found backups from only {len(by_computer)} computer)")
                self.finished(False, [])
                return

            # Select which backups to merge
            if self.merge_all:
                selected_paths = [b["path"] for b in backup_metas]
                self.log(f"Syncing all {len(selected_paths)} backups from {len(by_computer)} computers")
            else:
                selected_paths = []
                self.log(f"Discovered {len(backup_metas)} backups from {len(by_computer)} computers:")
                for computer_id, backups in sorted(by_computer.items()):
                    latest = backups[0]
                    selected_paths.append(latest["path"])
                    self.log(f"  {computer_id}:")
                    self.log(f"    Latest: {Path(latest['path']).name}")
                    if len(backups) > 1:
                        self.log(f"    (skipping {len(backups) - 1} older backups)")

            stats = {
                "conflicts": 0,
                "skipped_orphans": 0,
                "new_items": 0,
            }

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            output_files = []

            with tempfile.TemporaryDirectory() as temp_dir:
                # Extract all selected backups
                self.log(f"\nExtracting backups...")
                backup_infos = []
                for path in selected_paths:
                    self.log(f"  Extracting: {Path(path).name}")
                    info = extract_backup(path, temp_dir)
                    backup_infos.append(info)

                # Merge all data
                self.log("\nMerging data...")
                merged_data, machine_settings = merge_all_backups(backup_infos, stats)

                # Create a merged backup for each computer
                self.log(f"\nCreating synced backups...")
                for computer_id in sorted(by_computer.keys()):
                    machine_merged = apply_machine_settings(merged_data, machine_settings[computer_id])
                    output_path = self.directory / f"cherry-studio.{timestamp}.{computer_id}.merged.zip"
                    create_output_zip(machine_merged, backup_infos, str(output_path), temp_dir,
                                    skip_knowledge_base=self.skip_knowledge_base)
                    output_files.append(output_path)
                    self.log(f"  Created: {output_path.name}")

            # Prune old merged backups if requested
            if self.prune_count is not None:
                self.log(f"\nPruning merged backups (keeping {self.prune_count} per computer)...")
                deleted = prune_merged_backups(self.directory, self.prune_count)
                if deleted:
                    self.log(f"Deleted {len(deleted)} old merged backup(s):")
                    for f in deleted:
                        self.log(f"  - {f.name}")

            # Print statistics
            self.log(f"\n==================")
            self.log(f"Sync Statistics:")
            self.log(f"  Conflicts resolved (newer won): {stats['conflicts']}")
            self.log(f"  Orphans skipped (likely deleted): {stats['skipped_orphans']}")
            self.log(f"  New items added: {stats['new_items']}")
            self.log(f"\nCreated {len(output_files)} synced backups:")
            for f in output_files:
                self.log(f"  - {f.name}")
            self.log(f"\nTo import: Open Cherry Studio > Settings > Data > Restore")
            self.log(f"           Use the backup matching your computer.")

            self.finished(True, output_files)

        except Exception as e:
            self.log(f"\nError: {e}")
            self.finished(False, [])


class MergeGUI:
    """Graphical user interface for Cherry Studio Sync using PySide6."""

    def __init__(self):
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QLabel, QLineEdit, QPushButton, QCheckBox, QGroupBox, QTextEdit,
            QFileDialog, QMessageBox, QSpinBox
        )
        from PySide6.QtCore import Qt, QThread, Signal, QObject
        from PySide6.QtGui import QIcon

        self.QApplication = QApplication
        self.QMainWindow = QMainWindow
        self.QWidget = QWidget
        self.QVBoxLayout = QVBoxLayout
        self.QHBoxLayout = QHBoxLayout
        self.QLabel = QLabel
        self.QLineEdit = QLineEdit
        self.QPushButton = QPushButton
        self.QCheckBox = QCheckBox
        self.QGroupBox = QGroupBox
        self.QTextEdit = QTextEdit
        self.QFileDialog = QFileDialog
        self.QMessageBox = QMessageBox
        self.QSpinBox = QSpinBox
        self.Qt = Qt
        self.QThread = QThread
        self.Signal = Signal
        self.QObject = QObject
        self.QIcon = QIcon

        import sys
        self.app = QApplication(sys.argv)
        self.window = QMainWindow()
        self.window.setWindowTitle("Cherry Studio Sync")
        self.window.setMinimumSize(600, 550)

        # Set window icon
        icon_path = Path(__file__).parent / "assets" / "cherry-icon.png"
        if icon_path.exists():
            self.window.setWindowIcon(QIcon(str(icon_path)))

        self.is_merging = False
        self._setup_ui()

    def _setup_ui(self):
        """Set up the user interface components."""
        central = self.QWidget()
        self.window.setCentralWidget(central)
        layout = self.QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        # Directory selection
        layout.addWidget(self.QLabel("Backup Directory:"))

        dir_layout = self.QHBoxLayout()
        self.dir_entry = self.QLineEdit(str(Path.cwd()))
        dir_layout.addWidget(self.dir_entry)

        browse_btn = self.QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_directory)
        dir_layout.addWidget(browse_btn)
        layout.addLayout(dir_layout)

        # Options group
        options_group = self.QGroupBox("Options")
        options_layout = self.QVBoxLayout(options_group)

        self.merge_all_cb = self.QCheckBox("Merge all backups (not just latest per computer)")
        options_layout.addWidget(self.merge_all_cb)

        self.skip_kb_cb = self.QCheckBox("Skip Knowledge Bases (recommended)")
        self.skip_kb_cb.setChecked(True)
        options_layout.addWidget(self.skip_kb_cb)

        # Prune options
        prune_layout = self.QHBoxLayout()
        self.prune_cb = self.QCheckBox("Prune old merged backups, keep:")
        prune_layout.addWidget(self.prune_cb)

        self.prune_spin = self.QSpinBox()
        self.prune_spin.setRange(1, 10)
        self.prune_spin.setValue(1)
        self.prune_spin.setEnabled(False)
        prune_layout.addWidget(self.prune_spin)

        prune_layout.addWidget(self.QLabel("per computer"))
        prune_layout.addStretch()
        options_layout.addLayout(prune_layout)

        self.prune_cb.toggled.connect(self.prune_spin.setEnabled)

        layout.addWidget(options_group)

        # Buttons
        btn_layout = self.QHBoxLayout()

        self.sync_btn = self.QPushButton("Sync Backups")
        self.sync_btn.clicked.connect(self._start_merge)
        btn_layout.addWidget(self.sync_btn)

        self.prune_only_btn = self.QPushButton("Prune Only")
        self.prune_only_btn.clicked.connect(self._prune_only)
        btn_layout.addWidget(self.prune_only_btn)

        layout.addLayout(btn_layout)

        # Status log
        layout.addWidget(self.QLabel("Status:"))
        self.log_text = self.QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

    def _browse_directory(self):
        """Open directory browser dialog."""
        directory = self.QFileDialog.getExistingDirectory(
            self.window,
            "Select Cherry Studio Backup Directory",
            self.dir_entry.text()
        )
        if directory:
            self.dir_entry.setText(directory)

    def _log(self, message: str):
        """Append a message to the log area (thread-safe via signal)."""
        self.log_text.append(message)

    def _clear_log(self):
        """Clear the log area."""
        self.log_text.clear()

    def _start_merge(self):
        """Start the merge operation in a background thread."""
        if self.is_merging:
            return

        self.is_merging = True
        self.sync_btn.setEnabled(False)
        self.prune_only_btn.setEnabled(False)
        self._clear_log()

        directory = Path(self.dir_entry.text())
        merge_all = self.merge_all_cb.isChecked()
        skip_kb = self.skip_kb_cb.isChecked()
        prune_count = self.prune_spin.value() if self.prune_cb.isChecked() else None

        worker = MergeWorker(directory, merge_all, skip_kb, prune_count,
                           self._log, self._finish_merge)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()

    def _prune_only(self):
        """Prune merged backups without syncing."""
        if self.is_merging:
            return

        directory = Path(self.dir_entry.text())
        keep_count = self.prune_spin.value()

        self._clear_log()
        self._log("Pruning merged backups...")
        self._log(f"Directory: {directory}")
        self._log(f"Keeping: {keep_count} per computer")
        self._log("")

        merged = discover_merged_backups(directory)
        if not merged:
            self._log("No merged backups found.")
            return

        deleted = prune_merged_backups(directory, keep_count)
        if deleted:
            self._log(f"Deleted {len(deleted)} old merged backup(s):")
            for f in deleted:
                self._log(f"  - {f.name}")
        else:
            self._log("No merged backups needed pruning.")

        self.QMessageBox.information(
            self.window,
            "Prune Complete",
            f"Deleted {len(deleted)} old merged backup(s)."
        )

    def _finish_merge(self, success: bool, output_files: list):
        """Complete the merge operation and re-enable UI."""
        self.is_merging = False
        self.sync_btn.setEnabled(True)
        self.prune_only_btn.setEnabled(True)

        if success and output_files:
            self.QMessageBox.information(
                self.window,
                "Sync Complete",
                f"Successfully created {len(output_files)} synced backup(s).\n\n"
                "To import: Open Cherry Studio > Settings > Data > Restore\n"
                "Use the backup matching your computer."
            )
        elif not success:
            self.QMessageBox.critical(
                self.window,
                "Sync Failed",
                "The sync operation failed. Check the log for details."
            )

    def run(self):
        """Start the GUI main loop."""
        self.window.show()
        return self.app.exec()


def run_gui():
    """Launch the graphical user interface."""
    try:
        gui = MergeGUI()
        return gui.run()
    except ImportError as e:
        print(f"Error: PySide6 is not available: {e}")
        print("Install it with: pip install PySide6")
        return 1


def main():
    args = parse_args()

    # Launch GUI if requested
    if args.gui:
        return run_gui()

    print("Cherry Studio Sync")
    print("==================")

    # Handle prune-only mode
    if args.prune is not None and not args.backups:
        # Check if we should just prune without merging
        merged = discover_merged_backups()
        if not merged:
            print("\nNo merged backups found to prune.")
            return 0

        print(f"\nPruning merged backups (keeping {args.prune} per computer)...")
        deleted = prune_merged_backups(Path.cwd(), args.prune)
        if deleted:
            print(f"Deleted {len(deleted)} old merged backup(s):")
            for f in deleted:
                print(f"  - {f.name}")
        else:
            print("No merged backups needed pruning.")
        return 0

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
            skip_kb = not args.include_knowledge_base
            create_output_zip(machine_merged, backup_infos, output_path, temp_dir,
                            skip_knowledge_base=skip_kb)
            output_files.append(output_path)
            print(f"  Created: {output_path}")

    # Prune old merged backups if requested
    if args.prune is not None:
        print(f"\nPruning merged backups (keeping {args.prune} per computer)...")
        deleted = prune_merged_backups(Path.cwd(), args.prune)
        if deleted:
            print(f"Deleted {len(deleted)} old merged backup(s):")
            for f in deleted:
                print(f"  - {f.name}")

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
