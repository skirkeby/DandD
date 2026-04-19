import random
import json
import asyncio
import aiosqlite
import ollama
import os
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
        await db.execute('''
            CREATE TABLE IF NOT EXISTS characters (
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
        
        for c_col, c_type in [("inventory", "TEXT DEFAULT '[]'"), 
                              ("abilities", "TEXT DEFAULT '[]'"),
                              ("level", "INTEGER DEFAULT 1"),
                              ("class", "TEXT DEFAULT ''"),
                              ("race", "TEXT DEFAULT ''")]:
            try:
                await db.execute(f'ALTER TABLE characters ADD COLUMN "{c_col}" {c_type}')
            except Exception:
                pass
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id TEXT PRIMARY KEY,
                llm_engine TEXT
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_bindings (
                user_id TEXT,
                channel_id TEXT,
                character_id TEXT,
                PRIMARY KEY (user_id, channel_id)
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS channel_memory (
                channel_id TEXT,
                role TEXT,
                content TEXT,
                is_pinned BOOLEAN,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
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
            
        if isinstance(state.get("inventory"), str):
            try:
                state["inventory"] = json.loads(state["inventory"])
            except:
                state["inventory"] = []
                
        if isinstance(state.get("abilities"), str):
            try:
                state["abilities"] = json.loads(state["abilities"])
            except:
                state["abilities"] = []
            
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
            if key in ["stats", "effects", "inventory", "abilities"] and isinstance(value, (dict, list)):
                values.append(json.dumps(value))
            else:
                values.append(value)
                
        query = f"UPDATE characters SET {', '.join(set_clauses)} WHERE id = ? AND channel_id = ?"
        values.extend([char_id, channel_id])
        
        await self.db.execute(query, tuple(values))
        await self.db.commit()
        return {"status": "success"}

    async def get_inventory(self, channel_id: str, player_id: str) -> list:
        state = await self.get_character_state(channel_id, player_id)
        if "error" in state:
            return []
        return state.get("inventory", [])

    async def update_inventory(self, channel_id: str, player_id: str, new_inv: list) -> dict:
        return await self.update_character_state(channel_id, player_id, {"inventory": new_inv})

    async def bind_user_to_character(self, user_id: str, channel_id: str, character_id: str) -> Dict[str, Any]:
        """Binds a player to a character, ensuring 1-to-1 uniqueness per channel."""
        # Check if character exists (Case Insensitive Search)
        async with self.db.execute("SELECT id FROM characters WHERE LOWER(id) = LOWER(?) AND channel_id = ?", (character_id, channel_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return {"success": False, "error": f"Character '{character_id}' does not exist in this channel."}
            actual_character_id = row['id']
                
        # Check if another user already bound this character
        async with self.db.execute("SELECT user_id FROM user_bindings WHERE character_id = ? AND channel_id = ?", (actual_character_id, channel_id)) as cursor:
            row = await cursor.fetchone()
            if row and row['user_id'] != user_id:
                return {"success": False, "error": f"Character '{actual_character_id}' is already controlled by another player!"}
                
        # Upsert the binding for this user
        await self.db.execute(
            "INSERT INTO user_bindings (user_id, channel_id, character_id) VALUES (?, ?, ?) ON CONFLICT(user_id, channel_id) DO UPDATE SET character_id = excluded.character_id",
            (user_id, channel_id, actual_character_id)
        )
        await self.db.commit()
        return {"success": True, "character_id": actual_character_id}

    async def release_user_character(self, user_id: str, channel_id: str) -> Dict[str, Any]:
        """Releases any bound character for the user in this channel."""
        await self.db.execute("DELETE FROM user_bindings WHERE user_id = ? AND channel_id = ?", (user_id, channel_id))
        await self.db.commit()
        return {"success": True}

    async def get_bound_character(self, user_id: str, channel_id: str) -> str:
        """Retrieves the bound character_id directly."""
        async with self.db.execute("SELECT character_id FROM user_bindings WHERE user_id = ? AND channel_id = ?", (user_id, channel_id)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row['character_id']
        return None

    async def get_all_characters_and_bindings(self, channel_id: str) -> Dict[str, str]:
        """Returns a dict mapping character_id to user_id (or None if unbound) for the channel."""
        char_map = {}
        # Get all characters
        async with self.db.execute("SELECT id FROM characters WHERE channel_id = ?", (channel_id,)) as cursor:
            chars = await cursor.fetchall()
            for c in chars:
                char_map[c['id']] = None
                
        # Get bindings
        async with self.db.execute("SELECT character_id, user_id FROM user_bindings WHERE channel_id = ?", (channel_id,)) as cursor:
            bindings = await cursor.fetchall()
            for b in bindings:
                # We need to map case correctly if needed, but dict keys are exact DB values.
                if b['character_id'] in char_map:
                    char_map[b['character_id']] = b['user_id']
                    
        return char_map

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

    async def add_game_event(self, channel_id: str, event_text: str):
        """Adds a manually pinned system log that the AI will always receive."""
        await self.db.execute(
            "INSERT INTO channel_memory (channel_id, role, content, is_pinned) VALUES (?, ?, ?, ?)",
            (channel_id, "system", f"Important Game Event: {event_text}", True)
        )
        await self.db.commit()

    async def generate_ai_response(self, channel_id: str, prompt: str) -> str:
        """Generates an AI response using the configured LLM engine and history."""
        # 1. Log the user prompt
        await self.db.execute(
            "INSERT INTO channel_memory (channel_id, role, content, is_pinned) VALUES (?, ?, ?, ?)",
            (channel_id, "user", prompt, False)
        )
        await self.db.commit()

        # 2. Fetch memory context
        messages = []
        
        # Pinned messages (System + Events)
        async with self.db.execute("SELECT role, content FROM channel_memory WHERE channel_id = ? AND is_pinned = 1 ORDER BY timestamp ASC", (channel_id,)) as cursor:
            pinned = await cursor.fetchall()
            for r in pinned:
                messages.append({"role": r['role'], "content": r['content']})
                
        # Last 10 Non-pinned messages
        async with self.db.execute("""
            SELECT role, content FROM (
                SELECT role, content, timestamp FROM channel_memory 
                WHERE channel_id = ? AND is_pinned = 0 
                ORDER BY timestamp DESC LIMIT 10
            ) ORDER BY timestamp ASC
        """, (channel_id,)) as cursor:
            recent = await cursor.fetchall()
            for r in recent:
                messages.append({"role": r['role'], "content": r['content']})

        engine_type = await self.get_channel_llm_engine(channel_id)
        
        response_text = ""
        if engine_type == "ollama":
            try:
                # Use environment variable for host mapping or default to localhost
                ollama_host = os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434')
                print(f"[Ollama] Generating chat response for channel {channel_id} with gemma4:e4b on {ollama_host}")
                client = ollama.AsyncClient(host=ollama_host)
                response = await client.chat(model='gemma4:e4b', messages=messages)
                response_text = response['message']['content']
            except Exception as e:
                return f"⚠️ Error connecting to local Ollama (gemma4:e4b): {e}"
        else:
            return f"⚠️ Engine '{engine_type}' is not yet implemented for generating responses."
            
        # 3. Save AI response
        if response_text and not response_text.startswith("⚠️"):
            await self.db.execute(
                "INSERT INTO channel_memory (channel_id, role, content, is_pinned) VALUES (?, ?, ?, ?)",
                (channel_id, "assistant", response_text, False)
            )
            await self.db.commit()
            
        return response_text

    async def start_new_game(self, channel_id: str, characters_info: list, adventure_type: str, adventure_level: str) -> str:
        """Wipes characters for the channel and seeds new ones based on detailed inputs."""
        # Wipe existing characters and memory for this channel
        await self.db.execute("DELETE FROM characters WHERE channel_id = ?", (channel_id,))
        await self.db.execute("DELETE FROM channel_memory WHERE channel_id = ?", (channel_id,))
        
        char_descriptions = []
        for char in characters_info:
            c_name = char["name"]
            c_race = char["race"]
            c_class = char["class"]
            c_phys = char["physical"]
            c_pers = char["personality"]
            
            char_descriptions.append(f"- {c_name} ({c_race} {c_class}): {c_phys}, {c_pers}")
            
        heroes_text = "\n".join(char_descriptions)

        # Isolated AI Generation for Gear & Abilities
        generation_prompt = (
            f"You are a Dungeons & Dragons 5e Rule Master. Fully outfit exactly each of these Level {adventure_level} heroes strictly following official 5e rules based on their race, class, and level:\n{heroes_text}\n"
            f"Requirements per hero:\n"
            f"1. 'inventory': A list containing a themed primary weapon, specific armor/robes, a pack/tool, and a thematic magic item/trinket appropriate for 5e.\n"
            f"2. 'abilities': A list containing 2 to 4 canonical D&D 5e spells, class features, or racial abilities strictly appropriate for their specific class and level.\n"
            f"3. 'ac': An integer representing their mathematically accurate Armor Class (AC) derived strictly from their generated 5e armor and class constraints.\n"
            "Format the output EXACTLY as a JSON dictionary mapping the character name to another dictionary with keys 'inventory', 'abilities', and 'ac'. "
            "Output ONLY the valid JSON and nothing else."
        )
        hero_data = {}
        engine_type = await self.get_channel_llm_engine(channel_id)
        if engine_type == "ollama":
            try:
                ollama_host = os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434')
                print(f"[Ollama] Generating starting gear and abilities for level {adventure_level} on {ollama_host}...")
                client = ollama.AsyncClient(host=ollama_host)
                response = await client.generate(model='gemma4:e4b', prompt=generation_prompt, format='json')
                raw_text = response['response'].strip()
                if raw_text.startswith("```json"): raw_text = raw_text[7:]
                if raw_text.startswith("```"): raw_text = raw_text[3:]
                if raw_text.endswith("```"): raw_text = raw_text[:-3]
                raw_dict = json.loads(raw_text.strip())
                # Enforce case-insensitive matching
                hero_data = {k.lower(): v for k, v in raw_dict.items()}
            except Exception as e:
                print(f"⚠️ Failed to parse AI hero data: {e}")

        # Insert characters with customized items
        for char in characters_info:
            c_name = char["name"]
            c_level = int(adventure_level)
            c_race_display = char["race"]
            c_class_display = char["class"]
            
            c_class = char["class"].lower()
            
            stats_dict = self.generate_class_stats(c_class)
            stats_json = json.dumps(stats_dict)
            
            # Interesting fallback gear based on class heuristic
            fallback_weapon = "Forged Longsword"
            fallback_armor = "Chainmail Armor"
            fallback_ac = 15
            
            if "rogue" in c_class or "monk" in c_class: 
                fallback_weapon = "Serrated Daggers"
                fallback_armor = "Shadow-Weave Leather Armor"
                fallback_ac = 14
            elif "wizard" in c_class or "sorcerer" in c_class or "warlock" in c_class: 
                fallback_weapon = "Carved Arcane Staff"
                fallback_armor = "Embroidered Spellcaster Robes"
                fallback_ac = 11
            elif "cleric" in c_class or "paladin" in c_class: 
                fallback_weapon = "Engraved Holy Mace"
                fallback_armor = "Blessed Half-Plate Armor"
                fallback_ac = 16
            elif "ranger" in c_class: 
                fallback_weapon = "Strung Yew Longbow"
                fallback_armor = "Camouflage Studded Leather"
                fallback_ac = 15
            elif "barbarian" in c_class: 
                fallback_weapon = "Heavy Battleaxe"
                fallback_armor = "Bear-Hide Armor"
                fallback_ac = 14
            elif "bard" in c_class: 
                fallback_weapon = "Silver Rapier"
                fallback_armor = "Tailored Duelist Leather"
                fallback_ac = 13
                
            # Fallback abilities heuristic
            fallback_abilities = ["Second Wind", "Action Surge"]
            if "rogue" in c_class: fallback_abilities = ["Sneak Attack", "Cunning Action"]
            elif "wizard" in c_class: fallback_abilities = ["Mage Armor", "Magic Missile", "Shield"]
            elif "sorcerer" in c_class: fallback_abilities = ["Chaos Bolt", "Shield", "Metamagic"]
            elif "cleric" in c_class: fallback_abilities = ["Cure Wounds", "Bless", "Channel Divinity"]
            elif "paladin" in c_class: fallback_abilities = ["Lay on Hands", "Divine Smite", "Aura of Protection"]
            elif "ranger" in c_class: fallback_abilities = ["Hunter's Mark", "Favored Foe"]
            elif "barbarian" in c_class: fallback_abilities = ["Rage", "Danger Sense"]
            elif "bard" in c_class: fallback_abilities = ["Bardic Inspiration", "Vicious Mockery", "Healing Word"]
            elif "monk" in c_class: fallback_abilities = ["Flurry of Blows", "Unarmored Defense"]
            elif "warlock" in c_class: fallback_abilities = ["Eldritch Blast", "Hex", "Armor of Shadows"]
            
            c_data = hero_data.get(c_name.lower(), {})
            if "inventory" in c_data and isinstance(c_data["inventory"], list):
                c_inv_list = c_data["inventory"]
            else:
                c_inv_list = c_data if isinstance(c_data, list) else ["Adventurer's Pack", fallback_weapon, fallback_armor, "Minor Healing Potion"]
            
            c_ab_list = c_data.get("abilities", fallback_abilities) if isinstance(c_data, dict) else fallback_abilities
            c_ac = c_data.get("ac", fallback_ac) if isinstance(c_data, dict) else fallback_ac
            
            c_inv_json = json.dumps(c_inv_list)
            c_ab_json = json.dumps(c_ab_list)
            
            await self.db.execute(
                "INSERT INTO characters (id, channel_id, hp, max_hp, temp_hp, ac, stats, effects, inventory, abilities, level, class, race) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c_name, channel_id, 25, 25, 0, c_ac, stats_json, "[]", c_inv_json, c_ab_json, c_level, c_class_display, c_race_display)
            )
        await self.db.commit()
        
        system_rules = (
            f"You are the Game Master for a Level {adventure_level} D&D adventure of type '{adventure_type}'. "
            f"Here is your party of heroes:\n{heroes_text}\n"
            "Keep responses reasonably concise. Actively lead the narrative."
        )
        
        await self.db.execute(
            "INSERT INTO channel_memory (channel_id, role, content, is_pinned) VALUES (?, ?, ?, ?)",
            (channel_id, "system", system_rules, True)
        )
        await self.db.commit()
        
        intro_prompt = "Start the adventure natively! Write a short, engaging opening narration setting the specific scene " \
                       "so the players know where they are. Do NOT provide options or choices yet."
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
    intro = await engine.start_new_game(mock_discord_channel_id, chars, "dungeon delve", "3")
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