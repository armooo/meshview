#!/usr/bin/env python3
"""
Migration script to add performance indexes

This script adds two critical indexes:
1. idx_packet_from_node_time: Composite index on packet(from_node_id, import_time DESC)
2. idx_packet_seen_packet_id: Index on packet_seen(packet_id)

These indexes significantly improve the performance of the get_top_traffic_nodes() query.

Usage:
    python add_db_indexes.py

The script will:
- Connect to your database in WRITE mode
- Check if indexes already exist
- Create missing indexes
- Report timing for each operation
"""

import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from meshview.config import CONFIG


async def add_indexes():
    # Get database connection string and remove read-only flag
    db_string = CONFIG["database"]["connection_string"]
    if "?mode=ro" in db_string:
        db_string = db_string.replace("?mode=ro", "")

    print(f"Connecting to database: {db_string}")

    # Create engine with write access
    engine = create_async_engine(db_string, echo=False, connect_args={"uri": True})

    try:
        async with engine.begin() as conn:
            # Check and create idx_packet_from_node_time
            print("\n" + "=" * 70)
            print("Checking for index: idx_packet_from_node_time")
            print("=" * 70)

            result = await conn.execute(
                text("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND name='idx_packet_from_node_time'
            """)
            )

            if result.fetchone():
                print("✓ Index idx_packet_from_node_time already exists")
            else:
                print("Creating index idx_packet_from_node_time...")
                print("  Table: packet")
                print("  Columns: from_node_id, import_time DESC")
                print("  Purpose: Speeds up filtering packets by sender and time range")

                start_time = time.perf_counter()
                await conn.execute(
                    text("""
                    CREATE INDEX idx_packet_from_node_time 
                    ON packet(from_node_id, import_time DESC)
                """)
                )
                elapsed = time.perf_counter() - start_time

                print(f"✓ Index created successfully in {elapsed:.2f} seconds")

            # Check and create idx_packet_seen_packet_id
            print("\n" + "=" * 70)
            print("Checking for index: idx_packet_seen_packet_id")
            print("=" * 70)

            result = await conn.execute(
                text("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND name='idx_packet_seen_packet_id'
            """)
            )

            if result.fetchone():
                print("✓ Index idx_packet_seen_packet_id already exists")
            else:
                print("Creating index idx_packet_seen_packet_id...")
                print("  Table: packet_seen")
                print("  Columns: packet_id")
                print("  Purpose: Speeds up joining packet_seen with packet table")

                start_time = time.perf_counter()
                await conn.execute(
                    text("""
                    CREATE INDEX idx_packet_seen_packet_id 
                    ON packet_seen(packet_id)
                """)
                )
                elapsed = time.perf_counter() - start_time

                print(f"✓ Index created successfully in {elapsed:.2f} seconds")

            # Show index info
            print("\n" + "=" * 70)
            print("Current indexes on packet table:")
            print("=" * 70)
            result = await conn.execute(
                text("""
                SELECT name, sql FROM sqlite_master 
                WHERE type='index' AND tbl_name='packet'
                ORDER BY name
            """)
            )
            for row in result:
                if row[1]:  # Skip auto-indexes (they have NULL sql)
                    print(f"  • {row[0]}")

            print("\n" + "=" * 70)
            print("Current indexes on packet_seen table:")
            print("=" * 70)
            result = await conn.execute(
                text("""
                SELECT name, sql FROM sqlite_master 
                WHERE type='index' AND tbl_name='packet_seen'
                ORDER BY name
            """)
            )
            for row in result:
                if row[1]:  # Skip auto-indexes
                    print(f"  • {row[0]}")

            print("\n" + "=" * 70)
            print("Migration completed successfully!")
            print("=" * 70)
            print("\nNext steps:")
            print("1. Restart your web server (mvrun.py)")
            print("2. Visit /top endpoint and check the performance metrics")
            print("3. Compare DB query time with previous measurements")
            print("\nExpected improvement: 50-90% reduction in query time")

    except Exception as e:
        print(f"\n❌ Error during migration: {e}")
        raise
    finally:
        await engine.dispose()


if __name__ == "__main__":
    print("=" * 70)
    print("Database Index Migration for Endpoint Performance")
    print("=" * 70)
    asyncio.run(add_indexes())
