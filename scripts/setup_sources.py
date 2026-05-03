#!/usr/bin/env python3
"""
Multi-Source Configuration Helper

This script helps you set up media_sources in config.yaml for indexing
photos/videos from multiple locations (OneDrive, iCloud, network shares, etc.)

Usage:
    python scripts/setup_sources.py
"""

import os
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"

def detect_common_sources():
    """Auto-detect common photo/video storage locations"""
    sources = []
    
    # Check for OneDrive (Windows via WSL)
    onedrive_paths = [
        "/mnt/c/Users/*/OneDrive/Pictures",
        "/mnt/c/Users/*/OneDrive/Camera Roll",
    ]
    for pattern in onedrive_paths:
        from glob import glob
        for path in glob(pattern):
            if Path(path).exists():
                sources.append({
                    "name": "onedrive_photos",
                    "path": path,
                    "url_base": path.replace("/mnt/c/", "file:///C:/"),
                    "read_only": True,
                    "description": "OneDrive camera uploads"
                })
                break
    
    # Check for iCloud (macOS)
    icloud_paths = [
        str(Path.home() / "Pictures" / "iCloud Photos"),
        str(Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Photos"),
    ]
    for path in icloud_paths:
        if Path(path).exists():
            sources.append({
                "name": "icloud_photos",
                "path": path,
                "url_base": f"file://{path}",
                "read_only": True,
                "description": "iCloud Photos library"
            })
            break
    
    # Check for common Windows Pictures folder
    windows_pictures = "/mnt/c/Users/*/Pictures"
    from glob import glob
    for path in glob(windows_pictures):
        if Path(path).exists() and "OneDrive" not in path:
            sources.append({
                "name": "windows_pictures",
                "path": path,
                "url_base": path.replace("/mnt/c/", "file:///C:/"),
                "read_only": True,
                "description": "Windows Pictures folder"
            })
            break
    
    return sources

def interactive_setup():
    """Interactive CLI to set up media sources"""
    print("\n" + "="*70)
    print("Media Search Agent - Multi-Source Configuration")
    print("="*70 + "\n")
    
    print("This tool will help you configure multiple media sources for indexing.")
    print("Sources can be local folders, cloud storage, network shares, etc.\n")
    
    # Load existing config
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, 'r') as f:
            config = yaml.safe_load(f)
        print(f"✅ Loaded existing config from: {CONFIG_PATH}\n")
    else:
        print(f"❌ Config not found: {CONFIG_PATH}")
        return
    
    # Check if already using multi-source
    existing_sources = config.get('media_sources', [])
    if existing_sources:
        print(f"📁 Found {len(existing_sources)} existing source(s):\n")
        for src in existing_sources:
            status = "enabled" if src.get('enabled', True) else "disabled"
            print(f"  • {src['name']} ({status})")
            print(f"    {src['path']}")
        print()
    
    # Auto-detect sources
    print("🔍 Detecting common photo/video locations...\n")
    detected = detect_common_sources()
    
    if detected:
        print(f"Found {len(detected)} potential source(s):\n")
        for src in detected:
            print(f"  • {src['name']}")
            print(f"    Path: {src['path']}")
            print(f"    Desc: {src['description']}")
        print()
    else:
        print("No common sources detected automatically.\n")
    
    # Ask if user wants to add sources
    print("Options:")
    print("  1. Use auto-detected sources")
    print("  2. Manually add a new source")
    print("  3. Keep current configuration")
    print("  4. View example configuration")
    print()
    
    choice = input("Select option (1-4): ").strip()
    
    if choice == "1" and detected:
        # Use detected sources
        if 'media_sources' not in config:
            config['media_sources'] = []
        
        # Merge with existing sources (avoid duplicates by name)
        existing_names = {s['name'] for s in config['media_sources']}
        for src in detected:
            if src['name'] not in existing_names:
                config['media_sources'].append(src)
                print(f"✅ Added source: {src['name']}")
        
        save_config(config)
        
    elif choice == "2":
        # Manual source addition
        add_manual_source(config)
        
    elif choice == "3":
        print("\n✅ Keeping current configuration")
        return
        
    elif choice == "4":
        show_example_config()
        return
    else:
        print("\n❌ Invalid choice")
        return

def add_manual_source(config):
    """Manually add a new source"""
    print("\n" + "-"*70)
    print("Add New Media Source")
    print("-"*70 + "\n")
    
    name = input("Source name (e.g., 'gopro_videos'): ").strip()
    if not name:
        print("❌ Name is required")
        return
    
    path = input("Absolute path (e.g., '/mnt/d/GoPro'): ").strip()
    if not path:
        print("❌ Path is required")
        return
    
    if not Path(path).exists():
        confirm = input(f"⚠️  Path does not exist: {path}\nAdd anyway? (y/n): ")
        if confirm.lower() != 'y':
            return
    
    url_base = input("URL base (optional, for browsing - leave empty for API serving): ").strip()
    url_base = url_base if url_base else None
    
    read_only = input("Read-only? (y/n, default=y): ").strip().lower() != 'n'
    
    description = input("Description (optional): ").strip()
    description = description if description else None
    
    # Create source config
    source = {
        "name": name,
        "path": path,
        "read_only": read_only,
    }
    if url_base:
        source["url_base"] = url_base
    if description:
        source["description"] = description
    
    # Add to config
    if 'media_sources' not in config:
        config['media_sources'] = []
    config['media_sources'].append(source)
    
    print(f"\n✅ Added source: {name}")
    save_config(config)

def save_config(config):
    """Save updated config to YAML"""
    # Backup original
    if CONFIG_PATH.exists():
        backup_path = CONFIG_PATH.with_suffix('.yaml.backup')
        import shutil
        shutil.copy(CONFIG_PATH, backup_path)
        print(f"📦 Backed up original config to: {backup_path}")
    
    # Save updated config
    with open(CONFIG_PATH, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    
    print(f"💾 Saved configuration to: {CONFIG_PATH}")
    print("\n📋 Next steps:")
    print("  1. Review config.yaml to verify settings")
    print("  2. Run: msa index run  (to start indexing)\n")

def show_example_config():
    """Display example configuration"""
    print("\n" + "="*70)
    print("Example Multi-Source Configuration")
    print("="*70 + "\n")
    
    example = """
# config.yaml

# Option 1: Legacy single root (still supported)
# root: "/path/to/photos"

# Option 2: Multi-source configuration (recommended)
media_sources:
  # Example 1: OneDrive (Windows via WSL)
  - name: "onedrive_photos"
    path: "/mnt/c/Users/Kumar/OneDrive/Pictures"
    url_base: "file:///C:/Users/Kumar/OneDrive/Pictures"
    read_only: true
    description: "iPhone auto-upload to OneDrive"
  
  # Example 2: Local storage
  - name: "local_photos"
    path: "/home/kumar/Pictures"
    url_base: null  # Serve via API
    read_only: false
    description: "Edited photos on local disk"
  
  # Example 3: Network share (NAS)
  - name: "nas_videos"
    path: "/mnt/nas/videos"
    url_base: "smb://192.168.1.100/videos"
    read_only: true
    enabled: false  # Temporarily disabled
    description: "GoPro videos on Synology NAS"
  
  # Example 4: External drive
  - name: "backup_drive"
    path: "/media/kumar/Backup/Photos"
    url_base: "file:///media/kumar/Backup/Photos"
    read_only: true
    description: "Backup of 2023 photos"

# Each source can have:
#   name: Unique identifier (required)
#   path: Absolute path for indexing (required)
#   url_base: Base URL for browsing (optional)
#   read_only: Prevent modifications (default: true)
#   enabled: Allow disabling without removing (default: true)
#   description: Human-readable description (optional)
"""
    
    print(example)
    print("="*70 + "\n")
    
    print("URL Base Examples:")
    print("  • Local Windows files:  file:///C:/Users/Kumar/Pictures")
    print("  • Network share (SMB):  smb://nas.local/photos")
    print("  • Network share (NFS):  nfs://server/export/photos")
    print("  • Web server:           https://photos.example.com/media")
    print("  • null or omit:         Serve via API (default)\n")

if __name__ == "__main__":
    try:
        interactive_setup()
    except KeyboardInterrupt:
        print("\n\n⚠️  Cancelled by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
