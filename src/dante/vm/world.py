"""A fake game world: enough of the engine's natives to run the shipped scripts."""
import math
import random

from . import machine as dvm


class GameWorld(dvm.World):
    # DYNCAST parent chain; classes missing from here cast permissively.
    parents = {
        "CGhostbuster": "CCharacter", "CBiped": "CCharacter", "CBipedLarge": "CCharacter",
        "CScuttler": "CCharacter", "CFloater": "CCharacter", "CFlyerSmall": "CCharacter",
        "CFlyerMedium": "CCharacter", "CGhost": "CCharacter",
        "CCharacter": "CActor", "CActor": "CActorBase",
        "CSpawn": "CActor", "CTrigger": "CActor", "CSpawnTrigger": "CActor",
        "CEmitter": "CActor", "CAniModel": "CActor", "CPhysicsObject": "CActor",
        "CCarEffects": "CActor", "CCameraPathActor": "CActor", "CTrinket": "CActor",
    }

    def __init__(self):
        dvm.World.__init__(self)
        self.rng = random.Random(1)
        self.hud = []                 # [text, ...]           (display/displayMessage)
        self.dialogue = []             # [(speaker name or None, tag), ...]
        self.checkpoints = []          # [("define"/"save", name, text), ...]
        self.spawned = []              # [EngObj, ...]         (CSpawn::spawnCharacter)
        self.music = None
        self.level_description = None
        self._ext = {}                 # Ptr -> dict, for a `this` that isn't an EngObj
        self._handle = 0
        self._events = {}              # tick -> [fn(vm), ...]
        self.natives.update(self._native_table())

    # -- per-object state, whether `this` is a real EngObj or a raw struct Ptr -----------
    def state(self, this):
        if not isinstance(this, dvm.Ptr):
            return {}                      # None, or a stray int from an unstubbed native
        obj = this.obj
        if obj is not None:
            return obj.props
        return self._ext.setdefault(this, {})

    def _next_handle(self):
        self._handle += 1
        return self._handle

    @staticmethod
    def _tag(ptr):
        return ptr.obj.name if isinstance(ptr, dvm.Ptr) and ptr.obj is not None else None

    # -- engine globals ---------------------------------------------------------------
    def make_global(self, vm, typ, name):
        """A CDialogDatabaseEntry global gets its own name as both tag and text."""
        if typ == "CDialogDatabaseEntry":
            obj = self.make_object(vm, typ, name)
            obj.set(vm.member_off("String CDialogDatabaseEntry::tag"), name)
            obj.set(vm.member_off("String CDialogDatabaseEntry::text"), name)
            return obj
        return dvm.World.make_global(self, vm, typ, name)

    # -- test scheduler -----------------------------------------------------------------
    def at(self, tick, fn):
        """Run fn(vm) on the given tick."""
        self._events.setdefault(tick, []).append(fn)

    def on_tick(self, vm):
        for fn in self._events.pop(vm.tick_no, []):
            fn(vm)

    def kill(self, obj):
        obj.props["hitPoints"] = 0.0
        obj.props["dead"] = True

    def kill_all(self, spawner_or_class):
        for o in self.spawned:
            if o.cls == spawner_or_class or o.props.get("spawner") == spawner_or_class:
                self.kill(o)

    # -- natives --------------------------------------------------------------------
    def _native_table(self):
        return {
            # math ------------------------------------------------------------------
            "float floor(float)": lambda vm, this, f: float(math.floor(f)),
            "float ceil(float)": lambda vm, this, f: float(math.ceil(f)),
            "float fAbs(float)": lambda vm, this, f: abs(f),
            "int iRand(int,int)": lambda vm, this, a, b: self.rng.randint(*sorted((a, b))),
            "float fRand(float,float)": lambda vm, this, a, b: self.rng.uniform(*sorted((a, b))),
            "bool randBool(float)": lambda vm, this, p: self.rng.random() < p,
            "float distance(Vector,Vector)": lambda vm, this, a, b: math.sqrt(
                sum((x - y) ** 2 for x, y in zip(a, b))),

            # CActorGroup -------------------------------------------------------------
            "void CActorGroup::constructor{@CActorGroup}()": lambda vm, this: self.state(this).__setitem__("members", []),
            "void CActorGroup::destructor{@CActorGroup}()": lambda vm, this: self.state(this).__setitem__("members", []),
            "int CActorGroup::N{@CActorGroup}()": lambda vm, this: len(self.state(this).get("members", [])),
            "@CActor CActorGroup::get{@CActorGroup}(int)": self._group_get,
            "int CActorGroup::add{@CActorGroup}(@CActor)": self._group_add,
            "void CActorGroup::removeAll{@CActorGroup}()": lambda vm, this: self.state(this).__setitem__("members", []),
            "void CActorGroup::removeByIndex{@CActorGroup}(int)": self._group_remove_index,
            "bool CActorGroup::removeByPointer{@CActorGroup}(@CActor)": self._group_remove_ptr,
            "int CActorGroup::find{@CActorGroup}(@CActor)": lambda vm, this, actor: (
                self.state(this).get("members", []).index(actor)
                if actor in self.state(this).get("members", []) else -1),

            # CCharacter ---------------------------------------------------------------
            "float CCharacter::getHitPointsPct{@CCharacter}()": self._hp_pct,
            "float CCharacter::getHitPoints{@CCharacter}()": lambda vm, this: self.state(this).get("hitPoints", 100.0),
            "float CCharacter::getMaxHitPoints{@CCharacter}()": lambda vm, this: self.state(this).get("maxHitPoints", 100.0),
            "void CCharacter::setMaxHitPoints{@CCharacter}(float)": lambda vm, this, hp: self.state(this).__setitem__("maxHitPoints", hp),
            "void CCharacter::restoreHitPointsToMax{@CCharacter}()": self._restore_hp,
            "bool CCharacter::isDead{@CCharacter}()": lambda vm, this: bool(self.state(this).get("dead", False)),
            "void CCharacter::setHitPoints{@CCharacter}(float)": self._set_hp,
            "void CCharacter::setInvulnerableFlag{@CCharacter}(bool)": lambda vm, this, b: self.state(this).__setitem__("invulnerable", bool(b)),
            "void CCharacter::setVictimUponSpawn{@CCharacter}(@CCharacter)": lambda vm, this, v: self.state(this).__setitem__("victim_upon_spawn", v),
            "void CCharacter::setVictim{@CCharacter}(@CActor)": lambda vm, this, v: self.state(this).__setitem__("victim", v),
            "void CCharacter::setVictimDefault{@CCharacter}()": lambda vm, this: self.state(this).__setitem__("victim", "default"),
            "void CCharacter::setVictimDisable{@CCharacter}()": lambda vm, this: self.state(this).__setitem__("victim", None),
            "float CCharacter::startTalking{@CCharacter}(String)": self._start_talking,
            "bool CCharacter::isTalking{@CCharacter}()": lambda vm, this: vm.tick_no < self.state(this).get("talking_until", -1),
            "void CCharacter::setAnimation{@CCharacter}(String,bool)": lambda vm, this, name, loop: self.state(this).__setitem__("animation", (name, loop)),
            "void CActor::setScannable{@CActor}(bool)": lambda vm, this, b: self.state(this).__setitem__("scannable", bool(b)),

            # spawning -------------------------------------------------------------
            "@CCharacter CSpawn::spawnCharacter{@CSpawn}(@SSpawnInfo)": self._spawn_character,
            "void SSpawnInfo::constructor{@SSpawnInfo}()": lambda vm, this: None,
            "void SSpawnInfo::destructor{@SSpawnInfo}()": lambda vm, this: None,
            "void CScuttler::setSplinePathActor{@CScuttler}(@CSplinePath,float)": lambda vm, this, path, t: None,

            # CActorBase -----------------------------------------------------------
            "Vector CActorBase::getPos{@CActorBase}()": lambda vm, this: self.state(this).get("pos", (0.0, 0.0, 0.0)),
            "Vector CActorBase::getOrient{@CActorBase}()": lambda vm, this: self.state(this).get("orient", (0.0, 0.0, 0.0)),
            "Vector CActorBase::getBoundsCenter{@CActorBase}()": lambda vm, this: self.state(this).get(
                "bounds_center", self.state(this).get("pos", (0.0, 0.0, 0.0))),
            "void CActorBase::enable{@CActorBase}(bool)": lambda vm, this, b: self.state(this).__setitem__("enabled", bool(b)),
            "bool CActorBase::isEnabled{@CActorBase}()": lambda vm, this: self.state(this).get("enabled", True),
            "String CActorBase::getName{@CActorBase}()": lambda vm, this: (
                self._tag(this) or self.state(this).get("name", "")),
            "void CActorBase::attachToActor{@CActorBase}(@CActor)": lambda vm, this, actor: self.state(this).__setitem__("attached_to", actor),
            "void CActorBase::attachToActorTag{@CActorBase}(@CActor,String,bool)": self._attach_tag,
            "void CActorBase::warpTo{@CActorBase}(Vector,Vector)": self._warp_to,
            "void CActorBase::warpToActor{@CActorBase}(@CActor)": self._warp_to_actor,

            # HUD / flow -------------------------------------------------------------
            "void display(String,float,int)": lambda vm, this, text, dur, slot: self.hud.append(text),
            "void displayMessage(EHudMessage,String,float)": lambda vm, this, mid, text, dur: self.hud.append(text),
            "void debugPrint(String)": lambda vm, this, text: self.log.append(text),
            "void defineCheckpoint(String,String)": lambda vm, this, name, text: self.checkpoints.append(("define", name, text)),
            "void saveCheckpoint(String)": lambda vm, this, name: self.checkpoints.append(("save", name, None)),
            "void setLevelDescription(String)": lambda vm, this, text: setattr(self, "level_description", text),
            "bool setMusic(String)": lambda vm, this, name: (setattr(self, "music", name), True)[1],
            "int startSfx(String)": lambda vm, this, name: self._next_handle(),
            "int startSfxSpatialized(String,Vector)": lambda vm, this, name, pos: self._next_handle(),
            "bool cacheSfx(String)": lambda vm, this, name: True,
            "void cacheEffect(String)": lambda vm, this, name: None,
            "int startEffect(String,Vector,Vector)": lambda vm, this, name, pos, orient: self._next_handle(),
            "void killEffect(int)": lambda vm, this, handle: None,
            "bool isSfxActive(int)": lambda vm, this, handle: False,
            "bool isThreadActive(int)": lambda vm, this, tid: vm.thread_active(tid),
            "int beginThread((void()),bool)": self._begin_thread,
            "float dbNarrative(@CDialogDatabaseEntry)": self._db_narrative,
        }

    # -- CActorGroup helpers --------------------------------------------------------
    def _group_get(self, vm, this, i):
        members = self.state(this).get("members", [])
        return members[i] if 0 <= i < len(members) else None

    def _group_add(self, vm, this, actor):
        members = self.state(this).setdefault("members", [])
        members.append(actor)
        return len(members) - 1

    def _group_remove_index(self, vm, this, i):
        members = self.state(this).get("members", [])
        if 0 <= i < len(members):
            members.pop(i)

    def _group_remove_ptr(self, vm, this, actor):
        members = self.state(this).get("members", [])
        if actor in members:
            members.remove(actor)
            return True
        return False

    # -- CCharacter helpers ----------------------------------------------------------
    def _hp_pct(self, vm, this):
        s = self.state(this)
        maxhp = s.get("maxHitPoints", 100.0)
        return (s.get("hitPoints", 100.0) / maxhp * 100.0) if maxhp else 0.0

    def _restore_hp(self, vm, this):
        s = self.state(this)
        s["hitPoints"] = s.get("maxHitPoints", 100.0)
        s["dead"] = False

    def _set_hp(self, vm, this, hp):
        s = self.state(this)
        s["hitPoints"] = hp
        s["dead"] = hp <= 0.0

    def _start_talking(self, vm, this, text):
        s = self.state(this)
        duration = max(0.6, 0.12 * len(text))
        n_ticks = max(1, int(math.ceil(duration / vm.world.time_slice)))
        s["talking_until"] = vm.tick_no + n_ticks
        s["talking_line"] = text
        self.dialogue.append((self._tag(this), text))
        return duration

    # -- CActorBase helpers ------------------------------------------------------------
    def _attach_tag(self, vm, this, actor, tag, b):
        s = self.state(this)
        s["attached_to"] = actor
        s["attach_tag"] = tag

    def _warp_to(self, vm, this, pos, orient):
        s = self.state(this)
        s["pos"], s["orient"] = pos, orient

    def _warp_to_actor(self, vm, this, actor):
        self.state(this)["pos"] = self.state(actor).get("pos", (0.0, 0.0, 0.0))

    # -- spawning ------------------------------------------------------------------
    def _spawn_character(self, vm, this, info):
        cls = vm.struct_get(info, "SSpawnInfo", "classTypeName") or "CCharacter"
        cit = vm.struct_get(info, "SSpawnInfo", "citFilename") or ""
        variant = vm.struct_get(info, "SSpawnInfo", "variantName") or ""
        obj = self.make_object(vm, cls, "%s#%d" % (cls, self._next_handle()))
        obj.props.update(hitPoints=100.0, maxHitPoints=100.0, dead=False, cit=cit, variant=variant)
        spawner = this.obj if isinstance(this, dvm.Ptr) else None
        if spawner is not None:
            obj.props["spawner"] = spawner.name
            spawner.props.setdefault("spawned", []).append(obj)
        self.spawned.append(obj)
        return dvm.Ptr(obj, 0)

    # -- HUD / flow helpers --------------------------------------------------------
    def _begin_thread(self, vm, this, fn, persistent):
        if not isinstance(fn, dvm.FuncRef):
            return 0
        return vm.start(fn).id

    def _db_narrative(self, vm, this, entry):
        self.dialogue.append((None, self._tag(entry)))
        return 2.0


def make_world():
    return GameWorld()
