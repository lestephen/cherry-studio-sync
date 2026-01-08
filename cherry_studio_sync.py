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

# Config file location
CONFIG_DIR = Path.home() / ".cherry-studio-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"


DELETED_ITEM_EXPIRY_DAYS = 90


def _expire_deleted_items(deleted_dict: dict, expiry_days: int) -> list:
    """Remove entries older than expiry_days. Returns list of expired IDs."""
    if not deleted_dict:
        return []
    now = datetime.now()
    expired = []
    for item_id, timestamp_str in list(deleted_dict.items()):
        try:
            deleted_at = datetime.fromisoformat(timestamp_str)
            if (now - deleted_at).days > expiry_days:
                expired.append(item_id)
        except (ValueError, TypeError):
            expired.append(item_id)  # Invalid timestamp, remove it
    for item_id in expired:
        del deleted_dict[item_id]
    return expired


def load_config() -> dict:
    """Load configuration from file, returning defaults if not found.

    Also expires deleted_assistants and deleted_topics entries older than DELETED_ITEM_EXPIRY_DAYS.
    """
    config = {}
    try:
        if CONFIG_FILE.exists():
            config = json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass

    # Expire old deleted entries
    need_save = False
    for key in ("deleted_assistants", "deleted_topics"):
        deleted = config.get(key, {})
        if _expire_deleted_items(deleted, DELETED_ITEM_EXPIRY_DAYS):
            config[key] = deleted
            need_save = True

    if need_save:
        save_config(config)

    return config


def save_config(config: dict) -> None:
    """Save configuration to file."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
    except OSError:
        pass


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
                time_key: str = None, stats: dict = None,
                deleted_ids: set = None) -> tuple[list, list]:
    """
    Merge two lists of objects by ID.
    Items from newer take precedence.
    Items only in older are returned as orphans (unless in deleted_ids).

    Returns:
        tuple: (merged_result, orphans)
    """
    if deleted_ids is None:
        deleted_ids = set()

    newer_ids = {item.get(id_key) for item in newer if item.get(id_key)}
    older_ids = {item.get(id_key) for item in older if item.get(id_key)}

    # Build result from newer items
    result = {item.get(id_key): item for item in newer if item.get(id_key)}
    orphans = []

    # Process items from older
    for item in older:
        item_id = item.get(id_key)
        if not item_id:
            continue

        if item_id in newer_ids:
            if stats:
                stats["conflicts"] += 1
        elif item_id in deleted_ids:
            # Previously marked as deleted by user
            if stats:
                stats["skipped_orphans"] += 1
        else:
            # Orphan - needs user decision
            orphans.append(copy.deepcopy(item))

    new_only = newer_ids - older_ids
    if stats:
        stats["new_items"] += len(new_only)

    return list(result.values()), orphans


def merge_assistants(older_data: dict, newer_data: dict, stats: dict,
                     deleted_assistant_ids: set = None,
                     deleted_topic_ids: set = None) -> tuple[dict, list, list]:
    """Merge assistant structures, including nested topics.

    Preserves all top-level keys (presets, tagsOrder, collapsedTags, etc.)
    while applying special merge logic only to the assistants array.

    Returns:
        tuple: (merged_result, orphan_assistants, orphan_topics)
            - merged_result: The merged assistants data
            - orphan_assistants: List of assistants that exist only in older backup
            - orphan_topics: List of dicts with topic + assistant context:
              {"topic": {...}, "assistant_id": "...", "assistant_name": "..."}
    """
    if deleted_assistant_ids is None:
        deleted_assistant_ids = set()
    if deleted_topic_ids is None:
        deleted_topic_ids = set()

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
    orphan_assistants = []
    orphan_topics = []

    for asst in newer_assistants:
        asst_id = asst.get("id")
        if asst_id:
            merged_assistants[asst_id] = copy.deepcopy(asst)

    for old_asst in older_assistants:
        asst_id = old_asst.get("id")
        asst_name = old_asst.get("name", "Unknown Assistant")
        if not asst_id:
            continue
        if asst_id not in newer_ids:
            # Assistant only exists in older backup
            if asst_id in deleted_assistant_ids:
                # Previously marked as deleted by user, skip it
                stats["skipped_orphans"] += 1
            else:
                # Orphan - needs user decision
                orphan_assistants.append(copy.deepcopy(old_asst))
            continue

        # Merge topics for assistants that exist in both
        new_asst = merged_assistants[asst_id]
        old_topics = old_asst.get("topics", [])
        new_topics = new_asst.get("topics", [])

        merged_topics, topic_orphans = merge_by_id(
            old_topics, new_topics, "id", "updatedAt", stats, deleted_topic_ids
        )
        merged_assistants[asst_id]["topics"] = merged_topics

        # Add context to orphan topics
        for topic in topic_orphans:
            orphan_topics.append({
                "topic": topic,
                "assistant_id": asst_id,
                "assistant_name": new_asst.get("name", asst_name),
            })

    # Update the assistants array in result
    result["assistants"] = list(merged_assistants.values())

    # Ensure defaultAssistant is set (prefer newer, fallback to older)
    if "defaultAssistant" not in result or not result["defaultAssistant"]:
        result["defaultAssistant"] = older_data.get("defaultAssistant")

    return result, orphan_assistants, orphan_topics


def merge_persist_data_only(older: dict, newer: dict, stats: dict,
                            deleted_assistant_ids: set = None,
                            deleted_topic_ids: set = None) -> tuple[dict, list, list]:
    """Merge only the data portions of persist:cherry-studio (not settings).

    Returns:
        tuple: (merged_result, orphan_assistants, orphan_topics)
    """
    result = {}
    all_keys = set(older.keys()) | set(newer.keys())
    all_orphan_assistants = []
    all_orphan_topics = []

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
            merged_val, asst_orphans, topic_orphans = merge_assistants(
                older_val if isinstance(older_val, dict) else {},
                newer_val if isinstance(newer_val, dict) else {},
                stats,
                deleted_assistant_ids,
                deleted_topic_ids
            )
            all_orphan_assistants.extend(asst_orphans)
            all_orphan_topics.extend(topic_orphans)
            if newer_was_string or older_was_string:
                result[key] = json.dumps(merged_val)
            else:
                result[key] = merged_val
        elif newer_val is not None:
            result[key] = newer.get(key)
        else:
            result[key] = older.get(key)

    return result, all_orphan_assistants, all_orphan_topics


def merge_indexeddb(older: dict, newer: dict, stats: dict) -> dict:
    """Merge indexedDB databases."""
    result = {}
    all_dbs = set(older.keys()) | set(newer.keys())

    for db_name in all_dbs:
        older_db = older.get(db_name, [])
        newer_db = newer.get(db_name, [])

        if isinstance(older_db, list) and isinstance(newer_db, list):
            if db_name == "message_blocks":
                merged, _ = merge_by_id(older_db, newer_db, "id", "createdAt", stats)
            else:
                merged, _ = merge_by_id(older_db, newer_db, "id", None, stats)
            result[db_name] = merged
        elif newer_db:
            result[db_name] = newer_db
        else:
            result[db_name] = older_db

    return result


def merge_all_backups(backup_infos: list, stats: dict,
                      deleted_assistant_ids: set = None,
                      deleted_topic_ids: set = None) -> tuple[dict, dict, list, list]:
    """
    Merge all backups into unified data, keeping track of per-machine settings.

    Returns:
        merged_data: The merged conversation data
        machine_settings: Dict mapping computer_id -> their latest settings
        orphan_assistants: List of assistants needing user decision
        orphan_topics: List of topics needing user decision (with assistant context)
    """
    sorted_backups = sorted(backup_infos, key=lambda x: x["timestamp"])
    machine_settings = {}
    all_orphan_assistants = []
    all_orphan_topics = []

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

        merged_persist, asst_orphans, topic_orphans = merge_persist_data_only(
            older_persist, newer_persist, stats,
            deleted_assistant_ids, deleted_topic_ids
        )
        all_orphan_assistants.extend(asst_orphans)
        all_orphan_topics.extend(topic_orphans)

        result["localStorage"] = newer_local.copy()
        result["localStorage"]["persist:cherry-studio"] = json.dumps(merged_persist)

        # Merge indexedDB
        older_indexed = result.get("indexedDB", {})
        newer_indexed = newer_data.get("indexedDB", {})
        result["indexedDB"] = merge_indexeddb(older_indexed, newer_indexed, stats)

        result["time"] = newer_data.get("time", result.get("time"))
        result["version"] = newer_data.get("version", result.get("version"))

    # Deduplicate orphan assistants by ID
    seen_asst_ids = set()
    unique_asst_orphans = []
    for orphan in all_orphan_assistants:
        orphan_id = orphan.get("id")
        if orphan_id and orphan_id not in seen_asst_ids:
            seen_asst_ids.add(orphan_id)
            unique_asst_orphans.append(orphan)

    # Deduplicate orphan topics by ID
    seen_topic_ids = set()
    unique_topic_orphans = []
    for orphan in all_orphan_topics:
        topic_id = orphan["topic"].get("id")
        if topic_id and topic_id not in seen_topic_ids:
            seen_topic_ids.add(topic_id)
            unique_topic_orphans.append(orphan)

    return result, machine_settings, unique_asst_orphans, unique_topic_orphans


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


def _add_orphan_assistant_to_merged_cli(merged_data: dict, orphan: dict):
    """Add an orphan assistant to the merged data (CLI helper)."""
    local = merged_data.get("localStorage", {})
    persist_raw = local.get("persist:cherry-studio", "{}")
    persist = json.loads(persist_raw) if isinstance(persist_raw, str) else persist_raw

    assistants_raw = persist.get("assistants")
    if assistants_raw:
        assistants = json.loads(assistants_raw) if isinstance(assistants_raw, str) else assistants_raw
    else:
        assistants = {"assistants": []}

    # Add the orphan to the assistants list
    assistants.setdefault("assistants", []).append(orphan)

    # Save back
    if isinstance(assistants_raw, str):
        persist["assistants"] = json.dumps(assistants)
    else:
        persist["assistants"] = assistants

    if isinstance(persist_raw, str):
        local["persist:cherry-studio"] = json.dumps(persist)
    else:
        local["persist:cherry-studio"] = persist

    merged_data["localStorage"] = local


def _add_orphan_topic_to_merged_cli(merged_data: dict, topic: dict, assistant_id: str):
    """Add an orphan topic to the specified assistant in merged data (CLI helper)."""
    local = merged_data.get("localStorage", {})
    persist_raw = local.get("persist:cherry-studio", "{}")
    persist = json.loads(persist_raw) if isinstance(persist_raw, str) else persist_raw

    assistants_raw = persist.get("assistants")
    if not assistants_raw:
        return  # No assistants to add topic to

    assistants = json.loads(assistants_raw) if isinstance(assistants_raw, str) else assistants_raw

    # Find the assistant and add the topic
    for asst in assistants.get("assistants", []):
        if asst.get("id") == assistant_id:
            asst.setdefault("topics", []).append(topic)
            break

    # Save back
    if isinstance(assistants_raw, str):
        persist["assistants"] = json.dumps(assistants)
    else:
        persist["assistants"] = assistants

    if isinstance(persist_raw, str):
        local["persist:cherry-studio"] = json.dumps(persist)
    else:
        local["persist:cherry-studio"] = persist

    merged_data["localStorage"] = local


class MergeWorker:
    """Worker class to run merge operation in background thread."""

    def __init__(self, directory: Path, merge_all: bool, skip_knowledge_base: bool,
                 prune_count: int | None, log_callback, finished_callback,
                 deleted_assistant_ids: set = None, deleted_topic_ids: set = None,
                 orphan_callback=None, orphan_event=None):
        self.directory = directory
        self.merge_all = merge_all
        self.skip_knowledge_base = skip_knowledge_base
        self.prune_count = prune_count
        self.log = log_callback
        self.finished = finished_callback
        self.deleted_assistant_ids = deleted_assistant_ids or set()
        self.deleted_topic_ids = deleted_topic_ids or set()
        self.orphan_callback = orphan_callback  # Called with (asst_orphans, topic_orphans)
        self.orphan_event = orphan_event  # Event to wait on after orphan_callback
        self.orphan_response = {}  # Will be set by GUI: {"keep_assistants": [...], "keep_topics": [...]}

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
                merged_data, machine_settings, asst_orphans, topic_orphans = merge_all_backups(
                    backup_infos, stats,
                    self.deleted_assistant_ids, self.deleted_topic_ids
                )

                # Handle orphans (exist in one backup but not another)
                has_orphans = asst_orphans or topic_orphans
                if has_orphans and self.orphan_callback and self.orphan_event:
                    self.log(f"\nFound {len(asst_orphans)} assistant(s) and {len(topic_orphans)} conversation(s) needing review...")
                    self.orphan_callback(asst_orphans, topic_orphans)
                    self.orphan_event.wait()  # Wait for GUI to make decisions

                    # Apply user decisions for assistants
                    keep_asst_ids = set(self.orphan_response.get("keep_assistants", []))
                    for orphan in asst_orphans:
                        if orphan.get("id") in keep_asst_ids:
                            self._add_orphan_assistant_to_merged(merged_data, orphan)
                            self.log(f"  Keeping assistant: {orphan.get('name', orphan.get('id', 'Unknown'))}")

                    # Apply user decisions for topics
                    keep_topic_ids = set(self.orphan_response.get("keep_topics", []))
                    for orphan_info in topic_orphans:
                        topic = orphan_info["topic"]
                        if topic.get("id") in keep_topic_ids:
                            self._add_orphan_topic_to_merged(
                                merged_data, topic, orphan_info["assistant_id"]
                            )
                            topic_name = topic.get("name", topic.get("id", "Unknown"))
                            self.log(f"  Keeping conversation: {topic_name}")

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

    def _add_orphan_assistant_to_merged(self, merged_data: dict, orphan: dict):
        """Add an orphan assistant to the merged data."""
        local = merged_data.get("localStorage", {})
        persist_raw = local.get("persist:cherry-studio", "{}")
        persist = json.loads(persist_raw) if isinstance(persist_raw, str) else persist_raw

        assistants_raw = persist.get("assistants")
        if assistants_raw:
            assistants = json.loads(assistants_raw) if isinstance(assistants_raw, str) else assistants_raw
        else:
            assistants = {"assistants": []}

        # Add the orphan to the assistants list
        assistants.setdefault("assistants", []).append(orphan)

        # Save back
        if isinstance(assistants_raw, str):
            persist["assistants"] = json.dumps(assistants)
        else:
            persist["assistants"] = assistants

        if isinstance(persist_raw, str):
            local["persist:cherry-studio"] = json.dumps(persist)
        else:
            local["persist:cherry-studio"] = persist

        merged_data["localStorage"] = local

    def _add_orphan_topic_to_merged(self, merged_data: dict, topic: dict, assistant_id: str):
        """Add an orphan topic to the specified assistant in merged data."""
        local = merged_data.get("localStorage", {})
        persist_raw = local.get("persist:cherry-studio", "{}")
        persist = json.loads(persist_raw) if isinstance(persist_raw, str) else persist_raw

        assistants_raw = persist.get("assistants")
        if not assistants_raw:
            return  # No assistants to add topic to

        assistants = json.loads(assistants_raw) if isinstance(assistants_raw, str) else assistants_raw

        # Find the assistant and add the topic
        for asst in assistants.get("assistants", []):
            if asst.get("id") == assistant_id:
                asst.setdefault("topics", []).append(topic)
                break

        # Save back
        if isinstance(assistants_raw, str):
            persist["assistants"] = json.dumps(assistants)
        else:
            persist["assistants"] = assistants

        if isinstance(persist_raw, str):
            local["persist:cherry-studio"] = json.dumps(persist)
        else:
            local["persist:cherry-studio"] = persist

        merged_data["localStorage"] = local


class MergeGUI:
    """Graphical user interface for Cherry Studio Sync using PySide6."""

    def __init__(self):
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QLabel, QLineEdit, QPushButton, QCheckBox, QGroupBox, QTextEdit,
            QFileDialog, QMessageBox, QSpinBox, QDialog, QDialogButtonBox,
            QListWidget, QListWidgetItem, QAbstractItemView
        )
        from PySide6.QtCore import Qt, QThread, Signal, QObject
        from PySide6.QtGui import QIcon

        # Create signal class for thread-safe GUI updates
        class WorkerSignals(QObject):
            log_message = Signal(str)
            finished = Signal(bool, object)
            orphans_found = Signal(list, list)  # Signal: (assistant_orphans, topic_orphans)

        self.signals = WorkerSignals()
        self.orphan_event = threading.Event()  # For worker to wait on
        self.current_worker = None  # Reference to current worker for orphan response

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
        self.QDialog = QDialog
        self.QDialogButtonBox = QDialogButtonBox
        self.QListWidget = QListWidget
        self.QListWidgetItem = QListWidgetItem
        self.QAbstractItemView = QAbstractItemView
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
        self.config = load_config()
        self._setup_ui()

        # Connect signals to slots for thread-safe GUI updates
        self.signals.log_message.connect(self._log_slot)
        self.signals.finished.connect(self._finish_merge)
        self.signals.orphans_found.connect(self._show_orphan_dialog)

    def _setup_ui(self):
        """Set up the user interface components."""
        central = self.QWidget()
        self.window.setCentralWidget(central)
        layout = self.QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        # Directory selection
        layout.addWidget(self.QLabel("Backup Directory:"))

        dir_layout = self.QHBoxLayout()
        initial_dir = self.config.get("last_directory", str(Path.cwd()))
        self.dir_entry = self.QLineEdit(initial_dir)
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

    def _log_slot(self, message: str):
        """Slot: Append a message to the log area (called on main thread)."""
        self.log_text.append(message)

    def _emit_log(self, message: str):
        """Emit a log message signal (thread-safe, can be called from any thread)."""
        self.signals.log_message.emit(message)

    def _emit_finished(self, success: bool, output_files: list):
        """Emit finished signal (thread-safe, can be called from any thread)."""
        self.signals.finished.emit(success, output_files)

    def _emit_orphans(self, asst_orphans: list, topic_orphans: list):
        """Emit orphans signal (thread-safe, can be called from any thread)."""
        self.signals.orphans_found.emit(asst_orphans, topic_orphans)

    def _show_orphan_dialog(self, asst_orphans: list, topic_orphans: list):
        """Show dialog for user to decide which orphan items to keep/delete."""
        dialog = self.QDialog(self.window)
        dialog.setWindowTitle("Review Items")
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)

        layout = self.QVBoxLayout(dialog)

        # Explanation
        total = len(asst_orphans) + len(topic_orphans)
        label = self.QLabel(
            f"Found {total} item(s) that exist in one backup but not another.\n"
            "Check the ones you want to KEEP. Unchecked will be ignored.\n"
            "(Ignored items won't appear in future syncs.)"
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        # Create list widget for all items
        list_widget = self.QListWidget()
        list_widget.setSelectionMode(self.QAbstractItemView.SelectionMode.NoSelection)

        # Add assistant orphans
        if asst_orphans:
            header = self.QListWidgetItem(f"--- Assistants ({len(asst_orphans)}) ---")
            header.setFlags(self.Qt.ItemFlag.NoItemFlags)  # Not selectable/checkable
            list_widget.addItem(header)

            for orphan in asst_orphans:
                name = orphan.get("name", orphan.get("id", "Unknown Assistant"))
                topic_count = len(orphan.get("topics", []))
                item = self.QListWidgetItem(f"  {name} ({topic_count} conversation(s))")
                item.setFlags(item.flags() | self.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(self.Qt.CheckState.Checked)  # Default to keep
                item.setData(self.Qt.ItemDataRole.UserRole, ("assistant", orphan.get("id")))
                list_widget.addItem(item)

        # Add topic orphans
        if topic_orphans:
            header = self.QListWidgetItem(f"--- Conversations ({len(topic_orphans)}) ---")
            header.setFlags(self.Qt.ItemFlag.NoItemFlags)  # Not selectable/checkable
            list_widget.addItem(header)

            for orphan_info in topic_orphans:
                topic = orphan_info["topic"]
                asst_name = orphan_info["assistant_name"]
                topic_name = topic.get("name", topic.get("id", "Unknown"))
                item = self.QListWidgetItem(f"  {topic_name} (in {asst_name})")
                item.setFlags(item.flags() | self.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(self.Qt.CheckState.Checked)  # Default to keep
                item.setData(self.Qt.ItemDataRole.UserRole, ("topic", topic.get("id")))
                list_widget.addItem(item)

        layout.addWidget(list_widget)

        # Buttons
        button_box = self.QDialogButtonBox(
            self.QDialogButtonBox.StandardButton.Ok | self.QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        if dialog.exec() == self.QDialog.DialogCode.Accepted:
            # Collect user decisions
            keep_asst_ids = []
            delete_asst_ids = []
            keep_topic_ids = []
            delete_topic_ids = []

            for i in range(list_widget.count()):
                item = list_widget.item(i)
                data = item.data(self.Qt.ItemDataRole.UserRole)
                if data is None:
                    continue  # Header item

                item_type, item_id = data
                if item.checkState() == self.Qt.CheckState.Checked:
                    if item_type == "assistant":
                        keep_asst_ids.append(item_id)
                    else:
                        keep_topic_ids.append(item_id)
                else:
                    if item_type == "assistant":
                        delete_asst_ids.append(item_id)
                    else:
                        delete_topic_ids.append(item_id)

            # Save deleted IDs to config with timestamp
            now = datetime.now().isoformat()
            if "deleted_assistants" not in self.config:
                self.config["deleted_assistants"] = {}
            if "deleted_topics" not in self.config:
                self.config["deleted_topics"] = {}

            for asst_id in delete_asst_ids:
                self.config["deleted_assistants"][asst_id] = now
            for topic_id in delete_topic_ids:
                self.config["deleted_topics"][topic_id] = now
            save_config(self.config)

            # Tell worker the decision
            if self.current_worker:
                self.current_worker.orphan_response = {
                    "keep_assistants": keep_asst_ids,
                    "keep_topics": keep_topic_ids,
                }
        else:
            # User cancelled - keep all to be safe
            if self.current_worker:
                self.current_worker.orphan_response = {
                    "keep_assistants": [o.get("id") for o in asst_orphans],
                    "keep_topics": [o["topic"].get("id") for o in topic_orphans],
                }

        # Signal worker to continue
        self.orphan_event.set()

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

        # Get deleted IDs from config
        deleted_assistant_ids = set(self.config.get("deleted_assistants", {}).keys())
        deleted_topic_ids = set(self.config.get("deleted_topics", {}).keys())

        # Reset orphan handling state
        self.orphan_event.clear()

        worker = MergeWorker(
            directory, merge_all, skip_kb, prune_count,
            self._emit_log, self._emit_finished,
            deleted_assistant_ids=deleted_assistant_ids,
            deleted_topic_ids=deleted_topic_ids,
            orphan_callback=self._emit_orphans,
            orphan_event=self.orphan_event
        )
        self.current_worker = worker
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()

    def _prune_only(self):
        """Prune merged backups without syncing."""
        if self.is_merging:
            return

        directory = Path(self.dir_entry.text())
        keep_count = self.prune_spin.value()

        self._clear_log()
        self._log_slot("Pruning merged backups...")
        self._log_slot(f"Directory: {directory}")
        self._log_slot(f"Keeping: {keep_count} per computer")
        self._log_slot("")

        merged = discover_merged_backups(directory)
        if not merged:
            self._log_slot("No merged backups found.")
            return

        deleted = prune_merged_backups(directory, keep_count)
        if deleted:
            self._log_slot(f"Deleted {len(deleted)} old merged backup(s):")
            for f in deleted:
                self._log_slot(f"  - {f.name}")
        else:
            self._log_slot("No merged backups needed pruning.")

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
            self.config["last_directory"] = self.dir_entry.text()
            save_config(self.config)
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

        # Merge all data (CLI mode: load deleted_ids from config, keep all orphans)
        config = load_config()
        deleted_assistant_ids = set(config.get("deleted_assistants", {}).keys())
        deleted_topic_ids = set(config.get("deleted_topics", {}).keys())
        merged_data, machine_settings, asst_orphans, topic_orphans = merge_all_backups(
            backup_infos, stats, deleted_assistant_ids, deleted_topic_ids
        )

        # In CLI mode, keep all orphans (union behavior)
        if asst_orphans:
            print(f"\nFound {len(asst_orphans)} orphan assistant(s), keeping all:")
            for orphan in asst_orphans:
                name = orphan.get("name", orphan.get("id", "Unknown"))
                print(f"  - {name}")
                _add_orphan_assistant_to_merged_cli(merged_data, orphan)

        if topic_orphans:
            print(f"\nFound {len(topic_orphans)} orphan conversation(s), keeping all:")
            for orphan_info in topic_orphans:
                topic = orphan_info["topic"]
                asst_name = orphan_info["assistant_name"]
                topic_name = topic.get("name", topic.get("id", "Unknown"))
                print(f"  - {topic_name} (in {asst_name})")
                _add_orphan_topic_to_merged_cli(
                    merged_data, topic, orphan_info["assistant_id"]
                )

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
