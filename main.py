import random
import json
import asyncio
import aiosqlite
import ollama
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

DB_PATH = "dnd_database.db"

async def init_db(db_path: str = DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        # Drop and recreate for the new per-channel schema
        await db.execute('DROP TABLE IF EXISTS characters')
        await db.execute('''
            CREATE TABLE characters (
                id TEXT,
                channel_id TEXT,
                hp INTEGER,
                max_hp INTEGER,
                temp_hp INTEGER,
                ac INTEGER,
                stats TEXT,
                effects TEXT,
                PRIMARY KEY (id, channel_id)
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id TEXT PRIMARY KEY,
                llm_engine TEXT
            )
        ''')
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

    @staticmethod
    def generate_class_stats(char_class: str) -> dict:
        """Generates stats fitting the class natively. Simple heuristic."""
        base_stats = {"Str": 10, "Dex": 10, "Con": 12, "Int": 10, "Wis": 10, "Cha": 10}
        char_class = char_class.lower()
        if "fighter" in char_class or "barbarian" in char_class or "paladin" in char_class:
            base_stats["Str"] = 16
            base_stats["Con"] = 14
        elif "rogue" in char_class or "ranger" in char_class or "monk" in char_class:
            base_stats["Dex"] = 16
            base_stats["Wis"] = 14
        elif "wizard" in char_class or "artificer" in char_class:
            base_stats["Int"] = 16
            base_stats["Dex"] = 14
        elif "cleric" in char_class or "druid" in char_class:
            base_stats["Wis"] = 16
            base_stats["Con"] = 14
        elif "bard" in char_class or "sorcerer" in char_class or "warlock" in char_class:
            base_stats["Cha"] = 16
            base_stats["Dex"] = 14
        else:
            base_stats["Str"] = 14
            base_stats["Dex"] = 14
            base_stats["Con"] = 14
        return base_stats

    # -------------------------------------------------------------------------
    # B. Character State Management
    # -------------------------------------------------------------------------

    async def get_character_state(self, channel_id: str, char_id: str) -> Dict[str, Any]:
        """Pulls and parses canonical data from the async database."""
        print(f"\n[DB FETCH] Retrieving state for {char_id} in {channel_id}...")
        async with self.db.execute("SELECT * FROM characters WHERE id = ? AND channel_id = ?", (char_id, channel_id)) as cursor:
            row = await cursor.fetchone()
        
        if not row:
            return {"error": "Character not found."}
        
        state = dict(row)
        
        if isinstance(state.get("stats"), str):
            state["stats"] = json.loads(state["stats"])
            
        if isinstance(state.get("effects"), str):
            state["effects"] = json.loads(state["effects"])
            
        return state

    async def update_character_state(self, channel_id: str, char_id: str, updates: dict):
        """Persists changes to the async database."""
        print(f"[DB UPDATE] Updating state for {char_id} in {channel_id}...")
        
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
                
        query = f"UPDATE characters SET {', '.join(set_clauses)} WHERE id = ? AND channel_id = ?"
        values.extend([char_id, channel_id])
        
        await self.db.execute(query, tuple(values))
        await self.db.commit()
        return {"status": "success"}

    async def set_channel_llm_engine(self, channel_id: str, engine_type: str) -> Dict[str, Any]:
        """Sets the LLM engine preference for a specific Discord channel."""
        print(f"[DB UPDATE] Setting LLM engine for channel {channel_id} to {engine_type}")
        
        await self.db.execute('''
            INSERT INTO channel_settings (channel_id, llm_engine) 
            VALUES (?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET llm_engine = excluded.llm_engine
        ''', (channel_id, engine_type))
        await self.db.commit()
        return {"status": "success", "channel_id": channel_id, "llm_engine": engine_type}

    async def get_channel_llm_engine(self, channel_id: str) -> str:
        """Gets the LLM engine preference for a specific Discord channel."""
        async with self.db.execute("SELECT llm_engine FROM channel_settings WHERE channel_id = ?", (channel_id,)) as cursor:
            row = await cursor.fetchone()
        
        if row:
            # For aiosqlite, row object is either tuple or sqlite3.Row.
            # Using index 0 allows retrieving the exact column queried.
            return row[0]
        return "gemini" # Default value

    async def generate_ai_response(self, channel_id: str, prompt: str) -> str:
        """Generates an AI response using the configured LLM engine."""
        engine_type = await self.get_channel_llm_engine(channel_id)
        
        if engine_type == "ollama":
            try:
                print(f"[Ollama] Generating response for channel {channel_id} with gemma4:e4b")
                client = ollama.AsyncClient()
                response = await client.generate(model='gemma4:e4b', prompt=prompt)
                return response['response']
            except Exception as e:
                return f"⚠️ Error connecting to local Ollama (gemma4:e4b): {e}"
        else:
            return f"⚠️ Engine '{engine_type}' is not yet implemented for generating responses."

    async def start_new_game(self, channel_id: str, characters_info: list, adventure_type: str) -> str:
        """Wipes characters for the channel and seeds new ones based on detailed inputs."""
        # Wipe existing characters for this channel
        await self.db.execute("DELETE FROM characters WHERE channel_id = ?", (channel_id,))
        
        char_descriptions = []
        for char in characters_info:
            c_name = char["name"]
            c_race = char["race"]
            c_class = char["class"]
            c_phys = char["physical"]
            c_pers = char["personality"]
            
            char_descriptions.append(f"- {c_name} ({c_race} {c_class}): {c_phys}, {c_pers}")
            
            stats_dict = self.generate_class_stats(c_class)
            stats_json = json.dumps(stats_dict)
            
            await self.db.execute(
                "INSERT INTO characters (id, channel_id, hp, max_hp, temp_hp, ac, stats, effects) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (c_name, channel_id, 25, 25, 0, 15, stats_json, "[]")
            )
            
        enemy_stats = json.dumps({"Str": 12, "Dex": 14, "Con": 12, "Int": 12, "Wis": 12, "Cha": 12})
        await self.db.execute(
            "INSERT INTO characters (id, channel_id, hp, max_hp, temp_hp, ac, stats, effects) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Enemy1", channel_id, 30, 30, 0, 12, enemy_stats, "[]")
        )
        await self.db.commit()
        
        heroes_text = "\n".join(char_descriptions)
        intro_prompt = (
            f"Write a short, engaging opening narration for a new D&D adventure of type '{adventure_type}'. "
            f"Embed these heroes into the scene:\n{heroes_text}\n"
            "Keep it under 150 words. Do NOT provide any options or choices for the adventure; simply set the stage and describe the scene that is currently happening."
        )
        intro_text = await self.generate_ai_response(channel_id, intro_prompt)
        return intro_text

    # -------------------------------------------------------------------------
    # C. Core Ability Check Function
    # -------------------------------------------------------------------------

    async def perform_ability_check(self, channel_id: str, player_id: str, stat_type: Stat, target_dc: int) -> Dict[str, Any]:
        state = await self.get_character_state(channel_id, player_id)
        
        # Retrieve the RAW score from the database
        raw_score = state.get("stats", {}).get(stat_type.value)
        if raw_score is None:
            return {"success": False, "error": f"Stat {stat_type.value} missing."}

        # Calculate modifier dynamically
        modifier = self.get_modifier(raw_score)
        roll = self.roll_dice(20, 1)
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

    async def combat_initiative(self, channel_id: str, player_id: str) -> Dict[str, Any]:
        """Rolls initiative and maps it to a specific Discord channel."""
        state = await self.get_character_state(channel_id, player_id)
        if "error" in state:
            return {"action": "Combat Initiative", "player_id": player_id, "success": False, "error": state["error"]}
            
        raw_dex = state.get("stats", {}).get(Stat.DEX.value, 10)
        dex_mod = self.get_modifier(raw_dex)
        
        roll = self.roll_dice(20, 1)
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

    async def apply_damage(self, channel_id: str, target_id: str, damage_amount: int) -> Dict[str, Any]:
        """Applies damage, prioritizing Temporary HP before actual HP."""
        state = await self.get_character_state(channel_id, target_id)
        if "error" in state:
            return {"action": "Damage Application", "target": target_id, "success": False, "error": state["error"]}
            
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

        await self.update_character_state(channel_id, target_id, {
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

    async def apply_status_effect(self, channel_id: str, target_id: str, effect: Condition, duration: int) -> Dict[str, Any]:
        state = await self.get_character_state(channel_id, target_id)
        if "error" in state:
            return {"action": "Status Effect", "target": target_id, "success": False, "error": state["error"]}
            
        effects_list = state.get("effects", [])
        
        effects_list.append({
            "name": effect.value,
            "duration": duration
        })
        
        await self.update_character_state(channel_id, target_id, {"effects": effects_list})

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
    db_conn = await aiosqlite.connect(DB_PATH)
    db_conn.row_factory = aiosqlite.Row
    redis_client = MockAsyncRedisClient()
    engine = DndEngine(db_conn, redis_client)

    print("\n" + "="*60)
    print("TEST: START NEW GAME & ASYNC DEMONSTRATIONS")
    print("="*60)
    mock_discord_channel_id = "1049382059384"
    
    # Initialize game for channel
    chars = [{
        "name": "Brogbar", "race": "Orc", "class": "Barbarian", "physical": "tall", "personality": "angry"
    }]
    intro = await engine.start_new_game(mock_discord_channel_id, chars, "dungeon delve")
    print(f"Intro: {intro[:100]}...\n")
    
    check_result = await engine.perform_ability_check(mock_discord_channel_id, "Brogbar", Stat.DEX, 16)
    print(json.dumps(check_result, indent=4))

    damage_result = await engine.apply_damage(mock_discord_channel_id, "Brogbar", 8)
    print(json.dumps(damage_result, indent=4))
    
    await engine.combat_initiative(mock_discord_channel_id, "Brogbar")
    
    status_result = await engine.apply_status_effect(mock_discord_channel_id, "Enemy1", Condition.POISONED, 3)
    print(json.dumps(status_result, indent=4))

    await db_conn.close()

if __name__ == "__main__":
    asyncio.run(main())