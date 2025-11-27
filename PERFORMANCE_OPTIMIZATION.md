# /top Endpoint Performance Optimization

## Problem
The `/top` endpoint was taking over 1 second to execute due to inefficient database queries. The query joins three tables (node, packet, packet_seen) and performs COUNT aggregations on large result sets without proper indexes.

## Root Cause Analysis

The `get_top_traffic_nodes()` query in `meshview/store.py` executes:

```sql
SELECT 
    n.node_id,
    n.long_name,
    n.short_name,
    n.channel,
    COUNT(DISTINCT p.id) AS total_packets_sent,
    COUNT(ps.packet_id) AS total_times_seen
FROM node n
LEFT JOIN packet p ON n.node_id = p.from_node_id
    AND p.import_time >= DATETIME('now', 'localtime', '-24 hours')
LEFT JOIN packet_seen ps ON p.id = ps.packet_id
GROUP BY n.node_id, n.long_name, n.short_name
HAVING total_packets_sent > 0
ORDER BY total_times_seen DESC;
```

### Performance Bottlenecks Identified:

1. **Missing composite index on packet(from_node_id, import_time)**
   - The query filters packets by BOTH `from_node_id` AND `import_time >= -24 hours`
   - Without a composite index, SQLite must:
     - Scan using `idx_packet_from_node_id` index
     - Then filter each result by `import_time` (expensive!)
   
2. **Missing index on packet_seen(packet_id)**
   - The LEFT JOIN to packet_seen uses `packet_id` as the join key
   - Without an index, SQLite performs a table scan for each packet
   - With potentially millions of packet_seen records, this is very slow

## Solution

### 1. Added Database Indexes

Modified `meshview/models.py` to include two new indexes:

```python
# In Packet class
Index("idx_packet_from_node_time", "from_node_id", desc("import_time"))

# In PacketSeen class  
Index("idx_packet_seen_packet_id", "packet_id")
```

### 2. Added Performance Profiling

Modified `meshview/web.py` `/top` endpoint to include:
- Timing instrumentation for database queries
- Timing for data processing
- Detailed logging with `[PROFILE /top]` prefix
- On-page performance metrics display

### 3. Created Migration Script

Created `add_db_indexes.py` to add indexes to existing databases.

## Implementation Steps

### Step 1: Stop the Database Writer
```bash
# Stop startdb.py if it's running
pkill -f startdb.py
```

### Step 2: Run Migration Script
```bash
python add_db_indexes.py
```

Expected output:
```
======================================================================
Database Index Migration for /top Endpoint Performance
======================================================================
Connecting to database: sqlite+aiosqlite:///path/to/packets.db

======================================================================
Checking for index: idx_packet_from_node_time
======================================================================
Creating index idx_packet_from_node_time...
  Table: packet
  Columns: from_node_id, import_time DESC
  Purpose: Speeds up filtering packets by sender and time range
✓ Index created successfully in 2.34 seconds

======================================================================
Checking for index: idx_packet_seen_packet_id
======================================================================
Creating index idx_packet_seen_packet_id...
  Table: packet_seen
  Columns: packet_id
  Purpose: Speeds up joining packet_seen with packet table
✓ Index created successfully in 3.12 seconds

... (index listings)

======================================================================
Migration completed successfully!
======================================================================
```

### Step 3: Restart Services
```bash

# Restart server  
python mvrun.py &
```

### Step 4: Verify Performance Improvement

1. Visit `/top` endpoint eg http://127.0.0.1:8081/top?perf=true
2. Scroll to bottom of page
3. Check the Performance Metrics panel
4. Compare DB query time before and after

**Expected Results:**
- **Before:** 1000-2000ms query time
- **After:** 50-200ms query time  
- **Improvement:** 80-95% reduction

## Performance Metrics

The `/top` page now displays at the bottom:

```
⚡ Performance Metrics
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Database Query:    45.23ms
Data Processing:   2.15ms
Total Time:        47.89ms
Nodes Processed:   156
Total Packets:     45,678
Times Seen:        123,456
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```


## Technical Details

### Why Composite Index Works

SQLite can use a composite index `(from_node_id, import_time DESC)` to:
1. Quickly find all packets for a specific `from_node_id`
2. Filter by `import_time` without additional I/O (data is already sorted)
3. Both operations use a single index lookup

### Why packet_id Index Works

The `packet_seen` table can have millions of rows. Without an index:
- Each packet requires a full table scan of packet_seen
- O(n * m) complexity where n=packets, m=packet_seen rows

With the index:
- Each packet uses an index lookup
- O(n * log m) complexity - dramatically faster

### Index Size Impact

- `idx_packet_from_node_time`: ~10-20% of packet table size
- `idx_packet_seen_packet_id`: ~5-10% of packet_seen table size
- Total additional disk space: typically 50-200MB depending on data volume
- Performance gain: 80-95% query time reduction

## Future Optimizations

If query is still slow after indexes:

1. **Add ANALYZE**: Run `ANALYZE;` to update SQLite query planner statistics
2. **Consider materialized view**: Pre-compute traffic stats in a background job
3. **Add caching**: Cache results for 5-10 minutes using Redis/memcached
4. **Partition data**: Archive old packet_seen records

## Rollback

If needed, indexes can be removed:

```sql
DROP INDEX IF EXISTS idx_packet_from_node_time;
DROP INDEX IF EXISTS idx_packet_seen_packet_id;
```

## Files Modified

- `meshview/models.py` - Added index definitions
- `meshview/web.py` - Added performance profiling
- `meshview/templates/top.html` - Added metrics display
- `add_db_indexes.py` - Migration script (NEW)
- `PERFORMANCE_OPTIMIZATION.md` - This documentation (NEW)

## Support

For questions or issues:
1. Verify indexes exist: `python add_db_indexes.py` (safe to re-run)
2. Review SQLite EXPLAIN QUERY PLAN for the query
