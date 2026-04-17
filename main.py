import random
import json
import asyncio
import aiosqlite
from typing import Dict, Any, Union
from enum import Enum

# =============================================================================
# ENUMS (Prevents typos and standardizes inputs)
# =============================================================================

class Stat(str, Enum):
    STR = "Str"
    DEX = "Dex"
    CON = "Con"
    INT = "Int"
    WIS = "Wis"
    CHA = "Cha"

class Condition(str, Enum):
    POISONED = "Poisoned"
    PARALYZED = "Paralyzed"
    EXHAUSTED = "Exhausted"

# =============================================================================
# MOCK ASYNC DEPENDENCIES
# Represents libraries like `aiosqlite` and `redis.asyncio`
# =============================================================================

async def init_db(db_path: str = "dnd_database.db"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS characters (
                id TEXT PRIMARY KEY,
                hp INTEGER,
                max_hp INTEGER,
                temp_hp INTEGER,
                ac INTEGER,
                stats TEXT,
                effects TEXT
            )
        ''')
        
        async with db.execute("SELECT COUNT(*) FROM characters") as cursor:
            row = await cursor.fetchone()
            if row and row[0] == 0:
                print("--- [DEBUG] Seeding database with initial characters ---")
                player_stats = json.dumps({"Str": 16, "Dex": 14, "Con": 12, "Int": 14, "Wis": 14, "Cha": 12})
                enemy_stats = json.dumps({"Str": 12, "Dex": 14, "Con": 12, "Int": 12, "Wis": 12, "Cha": 12})
                
                await db.execute(
                    "INSERT INTO characters (id, hp, max_hp, temp_hp, ac, stats, effects) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("Player1", 25, 25, 5, 15, player_stats, "[]")
                )
                await db.execute(
                    "INSERT INTO characters (id, hp, max_hp, temp_hp, ac, stats, effects) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("Enemy1", 30, 30, 0, 12, enemy_stats, "[]")
                )
        await db.commit()

class MockAsyncRedisClient:
    """Mocks asynchronous redis client behavior."""
    def __init__(self):
        self.cache = {}
        print("--- [DEBUG] Async Redis client initialized (MOCK) ---")

    async def set(self, key: str, value: str, ex: int):
        self.cache[key] = value
        print(f"   [CACHE SET] Set {key} successfully (Expires in {ex}s).")
        await asyncio.sleep(0.01)

    async def get(self, key: str) -> Union[str, None]:
        await asyncio.sleep(0.01)
        return self.cache.get(key)

# =============================================================================
# CORE ASYNC ENGINE CLASS
# =============================================================================

class DndEngine:
    def __init__(self, db_connection: aiosqlite.Connection, redis_client: MockAsyncRedisClient):
        self.db = db_connection
        self.cache = redis_client
        print("\n[OK] Async DndEngine Initialized: Ready to calculate.")
    
    # -------------------------------------------------------------------------
    # A. Utility Functions
    # -------------------------------------------------------------------------

    @staticmethod
    def roll_dice(sides: int, count: int) -> int:
        if sides <= 0 or count <= 0:
            return 0
        total = sum(random.randint(1, sides) for _ in range(count))
        print(f"   [ROLL] Rolled {count}d{sides}. Total: {total}")
        return total

    @staticmethod
    def get_modifier(stat_score: int) -> int:
        """Calculates D&D 5e modifier from raw score."""
        # FIX: Corrected math formula -> (Score - 10) / 2
        return (stat_score - 10) // 2

    # -------------------------------------------------------------------------
    # B. Character State Management
    # -------------------------------------------------------------------------

    async def get_character_state(self, char_id: str) -> Dict[str, Any]:
        """Pulls and parses canonical data from the async database."""
        print(f"\n[DB FETCH] Retrieving state for {char_id}...")
        async with self.db.execute("SELECT * FROM characters WHERE id = ?", (char_id,)) as cursor:
            row = await cursor.fetchone()
        
        if not row:
            return {"error": "Character not found."}
        
        state = dict(row)
        
        if isinstance(state.get("stats"), str):
            state["stats"] = json.loads(state["stats"])
            
        if isinstance(state.get("effects"), str):
            state["effects"] = json.loads(state["effects"])
            
        return state

    async def update_character_state(self, char_id: str, updates: dict):
        """Persists changes to the async database."""
        print(f"[DB UPDATE] Updating state for {char_id}...")
        
        if not updates:
            return {"status": "no updates provided"}
            
        set_clauses = []
        values = []
        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            if key in ["stats", "effects"] and isinstance(value, (dict, list)):
                values.append(json.dumps(value))
            else:
                values.append(value)
                
        query = f"UPDATE characters SET {', '.join(set_clauses)} WHERE id = ?"
        values.append(char_id)
        
        await self.db.execute(query, tuple(values))
        await self.db.commit()
        return {"status": "success"}

    # -------------------------------------------------------------------------
    # C. Core Ability Check Function
    # -------------------------------------------------------------------------

    async def perform_ability_check(self, player_id: str, stat_type: Stat, target_dc: int) -> Dict[str, Any]:
        state = await self.get_character_state(player_id)
        
        # Retrieve the RAW score from the database
        raw_score = state.get("stats", {}).get(stat_type.value)
        if raw_score is None:
            return {"success": False, "error": f"Stat {stat_type.value} missing."}

        # Calculate modifier dynamically
        modifier = self.get_modifier(raw_score)
        roll = random.randint(1, 20)
        total = roll + modifier
        success = total >= target_dc

        result = {
            "action": f"{stat_type.value} Check",
            "success": success,
            "roll": roll,
            "modifier": modifier,
            "total": total,
            "difficulty": target_dc,
            "result_text_hint": f"The roll ({total}) {'exceeded' if success else 'failed'} the DC ({target_dc}) by {abs(total - target_dc)}."
        }
        return result

    # -------------------------------------------------------------------------
    # D. Specialized Mechanics
    # -------------------------------------------------------------------------

    async def combat_initiative(self, player_id: str, channel_id: str) -> Dict[str, Any]:
        """Rolls initiative and maps it to a specific Discord channel."""
        state = await self.get_character_state(player_id)
        raw_dex = state.get("stats", {}).get(Stat.DEX.value, 10)
        dex_mod = self.get_modifier(raw_dex)
        
        roll = random.randint(1, 20)
        initiative_score = roll + dex_mod
        
        # BEST PRACTICE: Bind the combat cache to the Discord Channel/Thread ID
        key = f"combat:{channel_id}:initiative:{player_id}"
        await self.cache.set(key, str(initiative_score), ex=3600) # Cached for 1 hour
        
        return {
            "action": "Combat Initiative",
            "player_id": player_id,
            "total_initiative": initiative_score,
            "cache_key": key
        }

    async def apply_damage(self, target_id: str, damage_amount: int) -> Dict[str, Any]:
        """Applies damage, prioritizing Temporary HP before actual HP."""
        state = await self.get_character_state(target_id)
        hp = state.get("hp", 0)
        temp_hp = state.get("temp_hp", 0)
        
        damage_remaining = max(0, damage_amount)

        # FIX: Temp HP depletion logic
        if temp_hp > 0:
            if temp_hp >= damage_remaining:
                temp_hp -= damage_remaining
                damage_remaining = 0
            else:
                damage_remaining -= temp_hp
                temp_hp = 0
        
        new_hp = max(0, hp - damage_remaining)

        await self.update_character_state(target_id, {
            "hp": new_hp,
            "temp_hp": temp_hp
        })

        return {
            "action": "Damage Application",
            "target": target_id,
            "damage_taken": damage_amount,
            "hp_after": new_hp,
            "temp_hp_after": temp_hp
        }

    async def apply_status_effect(self, target_id: str, effect: Condition, duration: int) -> Dict[str, Any]:
        state = await self.get_character_state(target_id)
        effects_list = state.get("effects", [])
        
        effects_list.append({
            "name": effect.value,
            "duration": duration
        })
        
        await self.update_character_state(target_id, {"effects": effects_list})

        return {
            "action": "Status Effect",
            "target": target_id,
            "effect_name": effect.value,
            "success": True
        }

# =============================================================================
# ASYNC EXECUTION DEMONSTRATION
# =============================================================================

async def main():
    await init_db()
    db_conn = await aiosqlite.connect("dnd_database.db")
    db_conn.row_factory = aiosqlite.Row
    redis_client = MockAsyncRedisClient()
    engine = DndEngine(db_conn, redis_client)

    print("\n" + "="*60)
    print("TEST: ASYNC ACROBATICS CHECK")
    print("="*60)
    
    check_result = await engine.perform_ability_check("Player1", Stat.DEX, 16)
    print(json.dumps(check_result, indent=4))

    print("\n" + "="*60)
    print("TEST: ASYNC DAMAGE (With Temp HP logic)")
    print("="*60)
    
    damage_result = await engine.apply_damage("Player1", 8)
    print(json.dumps(damage_result, indent=4))
    
    print("\n" + "="*60)
    print("TEST: CHANNEL-BOUND INITIATIVE & STATUS EFFECT")
    print("="*60)
    
    # Simulating a command run in a specific discord channel
    mock_discord_channel_id = "1049382059384"
    await engine.combat_initiative("Player1", mock_discord_channel_id)
    
    status_result = await engine.apply_status_effect("Enemy1", Condition.POISONED, 3)
    print(json.dumps(status_result, indent=4))

    await db_conn.close()

if __name__ == "__main__":
    asyncio.run(main())