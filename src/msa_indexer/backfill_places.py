"""Backfill missing place names for GPS-tagged media.

This utility finds media items with GPS coordinates but no place name,
and performs reverse geocoding to populate the place field.
"""

from pathlib import Path
from loguru import logger

try:
    import reverse_geocoder as rg
    RG_AVAILABLE = True
except ImportError:
    RG_AVAILABLE = False
    logger.error("reverse_geocoder not available - cannot backfill places")

from msa_settings import load_config
from .db.sqlite_store import SQLiteStore


def get_place_name(lat: float, lon: float) -> str | None:
    """Convert GPS coordinates to place name using reverse geocoding."""
    if not RG_AVAILABLE or rg is None:
        return None
    
    try:
        result = rg.search((lat, lon), mode=1)
        if result and len(result) > 0:
            r = result[0]
            parts = []
            if r.get('name'):
                parts.append(r['name'])
            if r.get('admin1'):
                parts.append(r['admin1'])
            if r.get('cc'):
                parts.append(r['cc'])
            return ", ".join(parts) if parts else None
    except Exception as e:
        logger.debug(f"Reverse geocoding failed for ({lat}, {lon}): {e}")
    
    return None


def backfill_missing_places():
    """Find media with GPS but no place, and backfill place names."""
    if not RG_AVAILABLE:
        logger.error("reverse_geocoder not installed. Install with: pip install reverse_geocoder")
        return
    
    config = load_config()
    db = SQLiteStore(Path(config.sqlite_path))
    
    # Find media with GPS but no place
    cursor = db.conn.execute('''
        SELECT media_id, gps_lat, gps_lon 
        FROM media 
        WHERE gps_lat IS NOT NULL 
        AND gps_lon IS NOT NULL 
        AND (place IS NULL OR place = '')
        AND deleted = 0
    ''')
    
    missing = cursor.fetchall()
    logger.info(f"Found {len(missing)} media items with GPS but no place name")
    
    if not missing:
        logger.info("All GPS-tagged media already have place names")
        db.close()
        return
    
    # Backfill each one
    updated = 0
    failed = 0
    
    for media_id, lat, lon in missing:
        place_name = get_place_name(lat, lon)
        
        if place_name:
            db.conn.execute(
                'UPDATE media SET place = ? WHERE media_id = ?',
                (place_name, media_id)
            )
            updated += 1
            logger.debug(f"Updated media_id={media_id}: ({lat:.5f}, {lon:.5f}) -> {place_name}")
        else:
            failed += 1
            logger.warning(f"Failed to geocode media_id={media_id}: ({lat:.5f}, {lon:.5f})")
    
    db.conn.commit()
    db.close()
    
    logger.info(f"Backfill complete: {updated} updated, {failed} failed")


if __name__ == "__main__":
    backfill_missing_places()
