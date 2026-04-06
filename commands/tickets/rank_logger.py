import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager

RANK_LOG_DB = Path(__file__).resolve().parent.parent.parent / 'databases' / 'rank_changes.db'

@contextmanager
def get_db_connection():
    """Context manager for database connections"""
    conn = sqlite3.connect(RANK_LOG_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def init_database():
    """Initialize the database schema if it doesn't exist"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rank_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                unix_timestamp INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                target_user_id INTEGER NOT NULL,
                target_username TEXT NOT NULL,
                executor_user_id INTEGER NOT NULL,
                executor_username TEXT NOT NULL,
                previous_rank TEXT NOT NULL,
                new_rank TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                guild_name TEXT NOT NULL,
                additional_info TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rank_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                unix_timestamp INTEGER NOT NULL,
                assignment_type TEXT NOT NULL,
                target_user_id INTEGER,
                target_username TEXT,
                executor_user_id INTEGER NOT NULL,
                executor_username TEXT NOT NULL,
                rank_name TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                guild_name TEXT NOT NULL,
                reason TEXT,
                additional_info TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_target_user 
            ON rank_changes(target_user_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_executor_user 
            ON rank_changes(executor_user_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_timestamp 
            ON rank_changes(unix_timestamp DESC)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_action_type 
            ON rank_changes(action_type)
        ''')
        
        # ADD THESE MISSING INDEXES
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ra_target_user 
            ON rank_assignments(target_user_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ra_timestamp 
            ON rank_assignments(unix_timestamp DESC)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ra_assignment_type 
            ON rank_assignments(assignment_type)
        ''')

def log_rank_assignment(
    target_user_id: int,
    target_username: str,
    rank_name: str,
    assignment_type: str,
    guild_id: int,
    guild_name: str,
    executor_user_id: int,
    executor_username: str,
    reason: str = None,
    additional_info: dict = None
):
    """Log a rank being given or removed to/from a user"""
    
    # Initialize database if it doesn't exits
    init_database()
    
    # Prepare timestamp
    now = datetime.now(timezone.utc)
    timestamp_iso = now.isoformat()
    unix_timestamp = int(now.timestamp())
    
    # Serialize additional info to JSON
    additional_info_json = json.dumps(additional_info) if additional_info else None
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO rank_assignments (
                    timestamp, unix_timestamp, assignment_type,
                    target_user_id, target_username,
                    executor_user_id, executor_username,
                    rank_name, guild_id, guild_name,
                    reason, additional_info
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                timestamp_iso, unix_timestamp, assignment_type,
                target_user_id, target_username,
                executor_user_id, executor_username,
                rank_name, guild_id, guild_name,
                reason, additional_info_json
            ))
            
            action = "given to" if assignment_type == "assign" else "removed from"
            executor_text = f"by {executor_username}" if executor_username else "automatically"
            print(f"[LOG] Rank {action}: {rank_name} → {target_username} {executor_text}")
    except Exception as e:
        print(f"[ERROR] Failed to save rank assignment log: {e}")

def log_rank_change(
    target_user_id: int,
    target_username: str,
    executor_user_id: int,
    executor_username: str,
    previous_rank: str,
    new_rank: str,
    action_type: str,  # "accept" or "demote"
    guild_id: int,
    guild_name: str,
    additional_info: dict = None
):
    """Log a rank change (accept or demotion) to the database"""
    
    # Initialize database if it doesn't exist
    init_database()
    
    # Prepare timestamp
    now = datetime.now(timezone.utc)
    timestamp_iso = now.isoformat()
    unix_timestamp = int(now.timestamp())
    
    # Serialize additional info to JSON
    additional_info_json = json.dumps(additional_info) if additional_info else None
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO rank_changes (
                    timestamp, unix_timestamp, action_type,
                    target_user_id, target_username,
                    executor_user_id, executor_username,
                    previous_rank, new_rank,
                    guild_id, guild_name,
                    additional_info
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                timestamp_iso, unix_timestamp, action_type,
                target_user_id, target_username,
                executor_user_id, executor_username,
                previous_rank, new_rank,
                guild_id, guild_name,
                additional_info_json
            ))
            
        print(f"[LOG] Rank change logged: {target_username} ({previous_rank} → {new_rank}) by {executor_username}")
    except Exception as e:
        print(f"[ERROR] Failed to save rank log: {e}")

def get_user_rank_assignments(user_id: int, limit: int = 50):
    """Get rank assignment/removal history for a specific user"""
    init_database()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM rank_assignments 
            WHERE target_user_id = ? 
            ORDER BY unix_timestamp DESC 
            LIMIT ?
        ''', (user_id, limit))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_recent_rank_assignments(limit: int = 50):
    """Get the most recent rank assignments/removals"""
    init_database()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM rank_assignments 
            ORDER BY unix_timestamp DESC 
            LIMIT ?
        ''', (limit,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_user_rank_history(user_id: int, limit: int = 50):
    """Get rank change history for a specific user"""
    init_database()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM rank_changes 
            WHERE target_user_id = ? 
            ORDER BY unix_timestamp DESC 
            LIMIT ?
        ''', (user_id, limit))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_recent_rank_changes(limit: int = 50):
    """Get the most recent rank changes"""
    init_database()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM rank_changes 
            ORDER BY unix_timestamp DESC 
            LIMIT ?
        ''', (limit,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_rank_changes_by_executor(executor_user_id: int, limit: int = 50):
    """Get rank changes performed by a specific executor"""
    init_database()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM rank_changes 
            WHERE executor_user_id = ? 
            ORDER BY unix_timestamp DESC 
            LIMIT ?
        ''', (executor_user_id, limit))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_rank_changes_by_type(action_type: str, limit: int = 50):
    """Get rank changes by action type (accept/demote)"""
    init_database()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM rank_changes 
            WHERE action_type = ? 
            ORDER BY unix_timestamp DESC 
            LIMIT ?
        ''', (action_type, limit))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def search_rank_changes(
    target_user_id: int = None,
    executor_user_id: int = None,
    action_type: str = None,
    start_timestamp: int = None,
    end_timestamp: int = None,
    limit: int = 50
):
    """Search rank changes with multiple filters"""
    init_database()
    
    query = "SELECT * FROM rank_changes WHERE 1=1"
    params = []
    
    if target_user_id:
        query += " AND target_user_id = ?"
        params.append(target_user_id)
    
    if executor_user_id:
        query += " AND executor_user_id = ?"
        params.append(executor_user_id)
    
    if action_type:
        query += " AND action_type = ?"
        params.append(action_type)
    
    if start_timestamp:
        query += " AND unix_timestamp >= ?"
        params.append(start_timestamp)
    
    if end_timestamp:
        query += " AND unix_timestamp <= ?"
        params.append(end_timestamp)
    
    query += " ORDER BY unix_timestamp DESC LIMIT ?"
    params.append(limit)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]