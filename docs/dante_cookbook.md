# Dante scripting cookbook

A guide to writing `.dn` source code, written based on the 21 shipped game modules. This is how the game actually uses `.dn`, whereas [docs/dante_lang.md](dante_lang.md) is the language spec.

Every recipe cites real functions as `file.dn:LINE name()`. Get the cited files with:
```bash
dante decompile "$DANTE_CORPUS" -o scripts/
```

Compile the snippets found here with:
```bash
dante compile <snippet>.dn --no-init -S -o /dev/null
```

`--no-init` because these are excerpts rather than whole modules; a real module gets a `__<stem>_init()`.

The symbol database (`dante/data/dante_symbols.json`) loads automatically. It contains 505 native prototypes, 74 struct-layout assumptions, and 85 enum constants.  
`native "..."` is only for something outside that set  
`extern "..."` is only for another module's C export (almost always `global.dante`)  

You can copy every prototype below into `extern`/`native` lines verbatim.

---

## 1. Module skeleton

| export | called | rule |
|---|---|---|
| `void setupLevel()` | once, at level load | **event call**. Return within one frame: register checkpoints, attach emitters, set the level description. No `idle()`/`wait()`. |
| `void main()` | once, as the level thread | the one export allowed to block; drives scripted flow start to finish |
| `void checkpoint_<Name>()` | when that checkpoint loads | **event call**, same one-frame rule. Restore state synchronously, `beginThread`/`thread` anything slow |
| `void __<stem>_init()` | before `setupLevel()` | compiler-generated (unless `--no-init`): constructs value-object globals, `STRNEW`s String globals |

`defineCheckpoint(String,String)` registers a checkpoint's re-entry function by prototype string plus its save-slot text, and is always called from `setupLevel()`.

Cites: `cemetery2.dn:620 setupLevel()`, `cemetery2.dn:696 main()`, `cemetery2.dn:750 checkpoint_BeginLevel()`, `cemetery2.dn:756 checkpoint_PlotsFight()`.

```c
module cookbook;

extern "void wait(float)";
struct CDialogDatabaseEntry { String text; }
extern CDialogDatabaseEntry MissionDescription;

bool loadingFromCheckpoint = false;

void checkpoint_Start()
{
    // Event call: must return within one frame. No idle()/wait() here.
    loadingFromCheckpoint = true;
}

void setupLevel()
{
    // Event call: registers checkpoints and returns immediately.
    defineCheckpoint("void checkpoint_Start()", "Level Start");
    setLevelDescription(MissionDescription.text);
}

void main()
{
    // The level thread: the one place idle()/wait() are allowed at top level.
    if (!loadingFromCheckpoint) {
        wait(1.0);
    }
    thread ambientLoop();
}

void ambientLoop()
{
    while (true) {
        idle();
    }
}
```

---

## 2. Spawning

```
@CCharacter CSpawn::spawnCharacter{@CSpawn}(@SSpawnInfo)          // native
void CCharacter::setVictimUponSpawn{@CCharacter}(@CCharacter)     // native
void CSpawnTrigger::activate{@CSpawnTrigger}()                    // native
void CSpawnTrigger::setNumberOfEnemies{@CSpawnTrigger}(int)       // native
void CSpawnTrigger::setAllEnemiesDeadEvent{@CSpawnTrigger}((void(@CSpawnTrigger)))  // native
void CSpawnTrigger::unspawnAllEnemies{@CSpawnTrigger}()           // native
```

`SSpawnInfo` is in the symbol DB already, so it needs no `struct` line. Three String fields: `classTypeName`, `citFilename`, `variantName`. `citFilename` keeps the `.cit` extension even though the on-disk asset is `.cib`. The engine remaps it.

`classTypeName` seen: `CBiped`, `CBipedLarge`, `CFloater`, `CFlyerMedium`, `CFlyerSmall`, `CScuttler`  
`variantName` seen: `standard`, `random`, `chase`, `cem1`, `cultist_skulls`, `Mayan`, `MuseumFlock`, `lost_island`, `sw_fight`, `josh`

There is no shared spawn API. Each module writes its own per-class wrapper: build an `SSpawnInfo`, set `classTypeName` to a literal, take the rest from parameters, then `return (@CType) spawn.spawnCharacter(spawnInfo);`. Cites: `cemetery2.dn:4514 spawnFloater()`, `:4524 spawnBiped()`, `:4534 spawnBipedLarge()`, `library1a.dn:1790 spawnZombie()`.

`CSpawn` and `CSpawnTrigger` actors call back into script by export name resolved from the `.lvl` file. You never call these yourself (`cemetery1.dn:2088 spEmit_Area2Cultist3_onActivate()`, `cemetery2.dn:2908 spTrig_UGEndScuttlers1_onAllEnemiesDead()`). `onAllEnemiesDead` is the exception, bound explicitly as a function value (`cemetery2.dn:2864`).

Pool matching uses the engine-resolved triple, not the `.lvl` text (verified in-game). `spawnCharacter` recycles a dormant actor whose `(class, cit, variant)` matches the request. The cit it registers is the canonical name from the character data, so a pool actor placed as `Biped3\golem_coal_clone.cit` still registers as `Biped3\Golem_Coal.cit` and a request for the clone path returns null.

Matching is case-insensitive. Emitter `enemyClassName` does not gate `spawnCharacter`. CScuttler-tagged emitters spawn CBiped and CFloater fine.

Track a wave's live members with `isActorGroupDead` (recipe 3) plus `CActorGroup::add`, `N` and `get`.

```c
module cookbook;

extern "bool isActorGroupDead(@CActorGroup)";
extern "void enableActorGroup(@CActorGroup,bool)";
extern @CCharacter hero;
extern CSpawn spEmit_Wave1;
extern CSpawnTrigger spTrig_Wave1;

CActorGroup g_Wave1;

@CBiped spawnBiped(@CSpawn spawn, String citFilename, String variant)
{
    SSpawnInfo spawnInfo;
    spawnInfo.classTypeName = "CBiped";
    spawnInfo.citFilename = citFilename;
    spawnInfo.variantName = variant;
    return (@CBiped) spawn.spawnCharacter(spawnInfo);
}

void spawnWave1()
{
    @CBiped biped = spawnBiped(spEmit_Wave1, "Biped1\\Fiend_web.cit", "standard");
    if (biped != null) {
        biped.setVictimUponSpawn(hero);
        g_Wave1.add(biped);
    }
    spTrig_Wave1.setNumberOfEnemies(1);
    spTrig_Wave1.activate();
    while (!isActorGroupDead(g_Wave1)) {
        idle();
    }
}
```

---

## 3. Waves & fights

The universal idiom, `global.dn:189 isActorGroupDead(@CActorGroup)`:
```c
bool isActorGroupDead(@CActorGroup actorGroup)
{
    int n = 0;
    while (n < actorGroup.N()) {
        @CCharacter character = (@CCharacter) actorGroup.get(n);
        if (character != null && character.getHitPointsPct() > 0.0) return false;
        n += 1;
    }
    return true;
}
```

Use it as `while (!isActorGroupDead(g)) idle();` wherever a script blocks on a fight ending.

Cites: `cemetery2.dn:1802 PlotsFight()`, `cemetery2.dn:2801 launchCoffins2()`, `library1a.dn:6188 spawnLibrarianGuards()`.

`killGroupMembers(@CActorGroup)` force-kills every live member, cleaning up a wave the player didn't finish (`abyss.dn:2366`). It is a `global.dante` export, so declare it `extern`.

`setMusic(String)` both starts and stops a fight loop: `setMusic(fightLoop)` starts, `setMusic("")` stops (`cemetery2.dn:1820`).

```c
module cookbook;

extern "bool isActorGroupDead(@CActorGroup)";
extern "void killGroupMembers(@CActorGroup)";
extern "void wait(float)";
extern CSpawnTrigger spTrig_Wave1;
extern CSpawnTrigger spTrig_Wave2;

String fightLoop = "music/Music_Fight_03";
CActorGroup g_Fighters;

void PlotsFight()
{
    setMusic(fightLoop);
    spTrig_Wave1.setNumberOfEnemies(4);
    spTrig_Wave1.activate();
    while (!isActorGroupDead(g_Fighters)) {
        idle();
    }
    wait(1.0);
    spTrig_Wave2.setNumberOfEnemies(6);
    spTrig_Wave2.activate();
    while (!isActorGroupDead(g_Fighters)) {
        idle();
    }
    killGroupMembers(g_Fighters);
    setMusic("");
}
```

---

## 4. Objectives & HUD text

```
void setLevelDescription(String)              // native -- once, in setupLevel()
void setCurrentObjective(String)               // native -- the long/journal objective text
void displayMessage(EHudMessage,String,float)  // native -- flashes short text on the HUD
void display(String,float,int)                 // native -- slot 0..5 debug/status text
```

`setLevelDescription` is called once, in `setupLevel()`, with the mission blurb (`cemetery2.dn:689`).

Updating the current objective mid-level is always the same pair: set the long text, then flash the short one with `eHudMessage_ObjectivesUpdated`. A duration of `-1.0` means it stays until replaced. Cite: `hotel2.dn:1744 checkpoint_KitchenFinalRoom()`.

`display(text, dur, slot)` is what the shipped scripts use for debug overlays, not objectives. It takes concatenation: `display("Destroying Root: " + actor.getName(), -1.0, 0)` (`cemetery2.dn:1399`). `global.dn:479 DisplayAndWait()` exists but nothing calls it.

All of these read `.text` off a `CDialogDatabaseEntry` global. Recipe 5 covers where that text lives.

```c
module cookbook;

struct CDialogDatabaseEntry { String text; }
extern CDialogDatabaseEntry objLong_FindGhostbusters;
extern CDialogDatabaseEntry objShort_FindGhostbusters;

enum EHudMessage { eHudMessage_ObjectivesUpdated }

void checkpoint_PlotsFight()
{
    setCurrentObjective(objLong_FindGhostbusters.text);
    displayMessage(eHudMessage_ObjectivesUpdated, objShort_FindGhostbusters.text, -1.0);
}

void debugStatus(int n)
{
    display("Enemies remaining: " + toString(n), -1.0, 0);
}
```

---

## 5. Dialogue

```
void dbSay(@CCharacter,@CDialogDatabaseEntry,bool)                  // extern (global.dante)
float dbStartSay(@CCharacter,@CDialogDatabaseEntry,bool)            // extern -- async, returns line duration
int dbPrepareSay(@CCharacter,@CDialogDatabaseEntry)                 // extern -- pre-caches, returns a handle
int dbStartPreparedSay(@CCharacter,@CDialogDatabaseEntry,int)       // extern -- fires a prepared handle
void dbNarrativeWait(@CDialogDatabaseEntry)                         // extern -- plays + blocks for its duration
bool CCharacter::isTalking{@CCharacter}()                           // native
```

`dbSay` and `dbStartSay` take the speaking character, or `null` for an off-screen narrator, plus a `CDialogDatabaseEntry` global named `Diag_<Speaker>_<LEVEL>_<id>` (`cemetery2.dn:38`).

`dbPrepareSay` + `dbStartPreparedSay` is the pattern for a line that must start in sync with a cinemat frame. `cemetery1.dn:1879 playGraveFiendIntro()` prepares the handle, plays the cinemat, waits for a frame with `streamingCinematGetFrame`, then fires it.

`dbNarrativeWait` is the blocking form: `dbNarrative()` then `wait(entry.getDuration())`. It is used back-to-back for narration dumps (`library1a.dn:4643`).

`CDialogDatabaseEntry`'s member offset is not in `ASSUMPTIONS`, so every shipped module redeclares it locally as `struct CDialogDatabaseEntry { String text; }`. You only need that line if you touch `.text` yourself. Passing the whole entry to `dbSay` does not.

Wait for a line to finish with `while (Egon.isTalking()) idle();` (`13th_floor_boss.dn:503`, `cemetery2.dn:3672`, and `boss_sp_side.dn:1349` with `||` across four speakers).

### How the game avoids dialogue collisions

There is no arbiter, and no native answers "is any dialogue playing". Serialization is structural (verified in-game):

* each conversation is one thread chaining blocking calls, with literal `wait(2.0)` beats
  between lines;
* flow triggers gate which conversation can exist. A trigger fires, disables its siblings,
  and starts the one dialogue thread for that stretch;
* interruption is an act by the owner, not a race: `killThreadByFunction(RayIntro)` when the
  player outruns the intro (`cemetery2.dn:1608`), or `stopTalking`/`dbNarrativeStop`;
* incidental VO uses separate engine channels. `queueChitChat` is the hero-only tutorial
  channel (`firehouse.dn:2807`). Combat barks come from character data.

This matters if you insert your own lines. Polling `isTalking()` is not enough. `dbSay(null, ...)` narrative lines set no character flag, so a guard is blind to them. Join the sequence instead: read the level's own chain (cemetery2's intro runs `trig_setupPlots` to `pp_RayIntro_onArrival` to `RayIntro()`, `cemetery2.dn:1526-1593`), then gate on its completion state or sum its line durations with `CDialogDatabaseEntry::getDuration()`.

Never reuse a `Diag_*` entry the level already speaks. Double-cast lines feel broken.

The subtitle and audio text is not in the `.dante` at all. `Diag_*` tags key into a per-language `<lang>\<stem>.txt` shipped alongside the level.

```c
module cookbook;

extern "void dbSay(@CCharacter,@CDialogDatabaseEntry,bool)";
extern "float dbStartSay(@CCharacter,@CDialogDatabaseEntry,bool)";
extern "int dbPrepareSay(@CCharacter,@CDialogDatabaseEntry)";
extern "int dbStartPreparedSay(@CCharacter,@CDialogDatabaseEntry,int)";
extern "void dbNarrativeWait(@CDialogDatabaseEntry)";
extern CGhostbuster Ray;
extern CGhostbuster Egon;
extern CDialogDatabaseEntry Diag_Ray_CEM_S_051;
extern CDialogDatabaseEntry Diag_Egon_CEM_PU_002;

void introBanter()
{
    dbSay(null, Diag_Egon_CEM_PU_002, false);
    dbStartSay(Ray, Diag_Ray_CEM_S_051, true);
    while (Ray.isTalking()) {
        idle();
    }
    dbNarrativeWait(Diag_Egon_CEM_PU_002);
}
```

---

## 6. Checkpoints

```
void defineCheckpoint(String,String)   // native -- (re-entry fn prototype string, save-slot text)
void saveCheckpoint(String)            // native -- (save-slot text, must match a defineCheckpoint call)
```

`defineCheckpoint` is called once per checkpoint, always in `setupLevel()`. It pairs the function's prototype string, exactly as it appears as a `C` export, with the `CDialogDatabaseEntry.text` shown on the save slot (`cemetery2.dn:624-625`).

`saveCheckpoint(text)` is what actually writes the save. It takes the same text but is called later, from wherever the level thread decides the condition is truly met (`cemetery2.dn:1528`, and again at `:1806` inside `PlotsFight()`).

`loadingFromCheckpoint` is not an engine intrinsic. It is a plain module-owned `bool`, set `true` at the top of every `checkpoint_X()` and read in `main()` to skip first-run-only setup (`cemetery2.dn:698`). The name and case vary per module. `hotel2.dn` spells it `loadingFromCheckPoint`.

A `checkpoint_X()` body re-does everything normal level flow would have done: flips lights and emitters on, disables triggers already resolved, restarts long-running threads with `beginThread(fn, false)`, warps actors to their checkpoint positions, and sets the current objective. `cemetery2.dn:756 checkpoint_PlotsFight()` does all five.

```c
module cookbook;

extern "void enableActorGroup(@CActorGroup,bool)";
struct CDialogDatabaseEntry { String text; }
extern CDialogDatabaseEntry checkpointText_PlotsFight;
extern CTrigger trig_BellRing;

bool loadingFromCheckpoint = false;
CActorGroup g_PlotsMonsters;

void setupLevel()
{
    defineCheckpoint("void checkpoint_PlotsFight()", checkpointText_PlotsFight.text);
}

void checkpoint_PlotsFight()
{
    loadingFromCheckpoint = true;
    trig_BellRing.enable(false);
    enableActorGroup(g_PlotsMonsters, true);
    beginThread(PlotsFightThread, false);
}

void PlotsFightThread()
{
    idle();
    saveCheckpoint(checkpointText_PlotsFight.text);
}
```

---

## 7. Triggers & events

Three binding mechanisms coexist.

**(a) By export name, resolved from the `.lvl` file.** Script never references these itself: `cemetery2.dn:1336 trig_ectoBumper_onActorEnter()`, `abyss.dn:1951 physO_ShandorPylon_onBreak()`, `cemetery2.dn:1931 aiNode_AfterGateRayWait_onArrival()`. `spEmit_*_onActivate` for `CSpawn` is the same idea (recipe 2). Shipped casing is inconsistent. Both `onActorLeave` and `onactorLeave` exist.

**(b) A function value passed to a `set*Event` native.** Explicit, from script.

```
void CActorBase::setDamageFilterEvent{@CActorBase}((void(@CActor,@SDamageInfo)))   // native
void CCharacter::setDeathEvent{@CCharacter}((void(@CCharacter)))                   // native
void CGhost::setGotTrappedEvent{@CGhost}((void(@CGhost)))                          // native
void CSpawnTrigger::setAllEnemiesDeadEvent{@CSpawnTrigger}((void(@CSpawnTrigger))) // native
```

`preGetHurt(@CActor,@SDamageInfo)` is a damage filter. Zero both strength fields to cancel a hit outright:
```c
void cancelAllDamage_preGetHurt(@CActor actor, @SDamageInfo pSDamageInfo)
{
    pSDamageInfo.strength = 0;
    pSDamageInfo.continuousStrength = 0;
}
```

Bind it with `setDamageFilterEvent` (`abyss.dn:1101`). `setDeathEvent` and `setGotTrappedEvent` follow the same shape. Both are bound right after a successful spawn (`cemetery1.dn:1971`), with the handler taking the actor that fired it (`cemetery2.dn:2340 PlotsFloaters_gotTrapped(@CGhost ghost)`).

**(c) A function value passed as a plain argument, stored in a struct field.** This is `walkToActor`'s `onArrival` callback (`global.dn:661`).

```c
void walkToActor(@CCharacter character, @CActor actor, bool run, (void(@CCharacter,String)) onArrival)
{
    SScriptWalkInfo scriptWalkInfo;
    // ... defaults omitted ...
    if (onArrival != null) scriptWalkInfo.eventFunction = onArrival;
    character.setCommandGoToPoint(scriptWalkInfo, eCommandPostureStand);
}
```

Called as `walkToActor(Egon, WP_Egon_Ballroom2, false, onArrival_freezeMe)` (`13th_floor_boss.dn:502`), with the handler at `13th_floor_boss.dn:559`.

```c
module cookbook;

extern "void killMe(@CActor,@CActor)";
extern @CCharacter hero;

// Bound to a CTrigger actor by function name in the .lvl file -- never
// called directly from script.
void trig_DoorTrap_onActorEnter(@CTrigger trigger, @CActor actor)
{
    trigger.enable(false);
}

void trig_DoorTrap_onActorLeave(@CTrigger trigger, @CActor actor)
{
    trigger.enable(true);
}

// Bound with a set*Event native call -- a function value, not a name.
void physO_Barrel_preGetHurt(@CActor actor, @SDamageInfo damageInfo)
{
    damageInfo.strength = 0;
}

void gasBarrel_onDeathEvent(@CCharacter character)
{
    killMe(character, hero);
}

void armBarrel(@CActor barrel, @CCharacter character)
{
    barrel.setDamageFilterEvent(physO_Barrel_preGetHurt);
    character.setDeathEvent(gasBarrel_onDeathEvent);
}
```

---

## 8. Timers, threads, wait

```
void idle()                     // native -- yield one VM tick; NOT optional in a loop
void wait(float)                // global.dn @6D8 -- idle() until *gTimeSlice seconds have elapsed
void waitForFlag(@bool,bool)    // global.dn @6FE -- idle() while *pbool == b
void waitForThread(int)         // global.dn @741 -- idle() while isThreadActive(handle)
int  startTimer(float)          // global.dn @121C -- thread timerThread(seconds); returns its handle
bool isTimerActive(int)         // global.dn @1241 -- isThreadActive(handle)
int  beginThread((void()),bool) // native -- what `thread f();` compiles to for a no-arg f
bool killThreadByHandle(int)    // native
int  killThreadByFunction((void())) // native
```

The interpreter runs a thread for at most 10,000 instructions per dispatch, then aborts it with "Infinite loop. (Missing idle() call?)". Every `while` or `for` that doesn't otherwise block must `idle()` on each iteration.

`gTimeSlice` is the engine's per-frame delta. `wait()` is exactly `while (*gTimeSlice < f) { f -= *gTimeSlice; idle(); } (*gTimeSlice) -= f;`. Read it yourself with `*gTimeSlice` when hand-rolling a ramp (`global.dn:206 rampPortalLightThread`).

`thread fn(args);` is the source form and returns a handle you can capture: `int n = thread cine_partyChat(s);` (`hotel1a.dn:2525`). Pass it to `killThreadByHandle(n)` or `waitForThread(n)` later.

For a thread with no arguments the shipped scripts often call `beginThread(fn, false)` directly. Same effect, no handle captured (`cemetery2.dn:764`).

```c
module cookbook;

extern "void wait(float)";
extern "void waitForFlag(@bool,bool)";
extern "void waitForThread(int)";
extern "int startTimer(float)";
extern "bool isTimerActive(int)";
extern @float gTimeSlice;

int hazardTimer = 0;
bool doorOpen = false;

void startHazard()
{
    hazardTimer = startTimer(5.0);
    int handle = thread pulseHazard();
    waitForThread(handle);
}

void pulseHazard()
{
    while (isTimerActive(hazardTimer)) {
        idle();
    }
    waitForFlag(&doorOpen, false);
    wait(*gTimeSlice);
}
```

---

## 9. Cinematics

```
void playStreamingCinemat(String)       // native
void stopStreamingCinemat(String)       // native
bool isStreamingCinematPlaying(String)  // native
float streamingCinematGetFrame(String)  // native
void cine_cleanup()                     // global.dn @6A3
void queueCinemat((void()))             // global.dn @776 -- 3-slot FIFO of function values
void playQueuedCinemats()               // global.dn @7B5 -- extern in the consuming module
```

The shipped idiom wraps the wait loop in `try`/`catch`. The engine unwinds into `catch` when the thread is aborted, which is what a skipped cinematic does. Cleanup goes there rather than after. Cites: `museum1.dn:3442 endShandorCombatThread()`, and `cemetery1.dn:1875 playGraveFiendIntro()`, which puts `dbPrepareSay` before the `try` and fires `dbStartPreparedSay` mid-cinemat.

`cine_cleanup()` turns the letterbox off, restores controls, time factor and camera, and cancels walk commands. Call it once after the `try`/`catch`, not inside it (`cemetery1.dn:1170`).

`queueCinemat(fn)` and `playQueuedCinemats()` chain cinematics without nesting `try` blocks. Push a function value, and whoever calls `playQueuedCinemats()` runs it next (`museum1.dn:3488`).

A function-value global cannot be called directly in `.dn`, only passed as an argument. That is why `playQueuedCinemats()`'s own source has to comment past `cinematQueue0()`. The decompiler can show it, but `dante compile` cannot rebuild it.

```c
module cookbook;

extern "void cine_cleanup()";
extern "void queueCinemat((void()))";
extern "void wait(float)";

String cine_GraveFiendIntro = "Cin_CEM_GraveFiend_Intro";

void playIntroCinemat()
{
    letterbox(true);
    try {
        wait(1.0);
        playStreamingCinemat(cine_GraveFiendIntro);
        while (isStreamingCinematPlaying(cine_GraveFiendIntro)) {
            idle();
        }
    } catch {
        stopStreamingCinemat(cine_GraveFiendIntro);
    }
    cine_cleanup();
    queueCinemat(afterIntro);
}

void afterIntro()
{
    letterbox(false);
}
```

---

## 10. Effects, sound, camera, misc

| native | cited use |
|---|---|
| `void cacheEffect(String)` | `cemetery2.dn:634 cacheEffect(kSkullHitFx);` |
| `int startEffect(String,Vector,Vector)` | `cemetery2.dn:2406 startEffect(kSkullHitFx, actor.getBoundsCenter(), actor.getOrient());` |
| `void killEffect(int)` | `cemetery1.dn:1975`, killed right after `startEffect` (a one-shot puff, not a looped emitter) |
| `bool cacheSfx(String)` | `global.dn:1027 cacheSfx(kDoorLockedSfx);` in `trig_DoorLocked_onActorEnter` (`global.dn:1025`) |
| `int startSfx(String)` | `cemetery2.dn:2805 startSfx(coffinLaunchScare);` |
| `int startSfxSpatialized(String,Vector)` | `cemetery2.dn:951 startSfxSpatialized(kEctoAccel, Ecto1.getPos());` |
| `void killSfx(int)` | `cemetery2.dn:793 killSfx(soundhandle1);` |
| `bool setMusic(String)` | `cemetery2.dn:1820 setMusic(fightLoop);` ... `setMusic("")` stops it |
| `CGameView::shakeCamera{@CGameView}(float,float,float,float,float)` | `abyss.dn:1515 gMainView.shakeCamera(0.1, 4.0, 0.0, 0.0, 1.0);` |
| `void setFog(int,int,int,float,float,float,float)` | `cemetery1.dn:563 setFog(68, 81, 96, 20.0, 300.0, 0.25, 1.0);` (r,g,b,begin,end,max,ramp) |
| `void enableActorGroup(@CActorGroup,bool)` | `global.dn:149`, calls `.enable(b)` on every member |

```c
module cookbook;

extern "void enableActorGroup(@CActorGroup,bool)";
extern @CGameView gMainView;

String kSkullHitFx = "fx/skull_hit.tfa";
String fightLoop = "music/Music_Fight_03";

void hitReaction(@CActor actor)
{
    cacheEffect(kSkullHitFx);
    int handle = startEffect(kSkullHitFx, actor.getBoundsCenter(), actor.getOrient());
    killEffect(handle);
    cacheSfx(kSkullHitFx);
    startSfxSpatialized(kSkullHitFx, actor.getBoundsCenter());
    setMusic(fightLoop);
    gMainView.shakeCamera(0.1, 4.0, 0.0, 0.0, 1.0);
    setFog(68, 81, 96, 20.0, 300.0, 0.25, 1.0);
}
```

---

## 11. Pitfalls

**`setAnimation(name, hold)` with `hold=true` freezes the character** (verified in-game). Holding the animation command without releasing it kills AI and attacks, T-poses the character on damage, and leaves the corpse stuck at death until it is hit again.

Every shipped `true` site releases immediately: `setAnimation("teleport_out", true); clearCommand(); setDefaultAnimation();` (`cemetery2.dn:2279`). Fire-and-forget spawns use `false` (`cemetery2.dn:2134`, `:2653`).

Use the `false` form unless you replicate the full triple.

* **`idle()` in every loop.** The interpreter kills a thread after 10,000 instructions
  without yielding. Any `while`/`for` that doesn't otherwise block needs an explicit `idle();`.
* **Strings are free-standing.** The compiler inserts `STRNEW`/`STRCPY`/`STRFREE` for every
  temporary and local. You never manage string lifetime yourself.
* **Value vs reference is load-bearing.** `CFoo g;` is a value global and a method call on it
  implicitly takes `&g`. `@CFoo r;` is a reference and passes `r` directly. Backwards is a compile error.
* **Casts are `DYNCAST`, not `static_cast`.** `(@CBiped) someActor` is `null` if `someActor`
  isn't a `CBiped`. Always null-check, like every shipped `spawnX()` does.
* **No arrays, no indexing.** Use a `CActorGroup` for anything bigger than a few named globals.
* **Unknown natives** need `native "ret Class::method{@Class}(argtypes)";` for a method, or
  `native "ret name(argtypes)";` for a free function.
* **`extern`, not `native`, for another module's script function.** `native` is an engine C++
  method; `extern` is a C export of another `.dante`, almost always `global.dante`.
* **Enums must be declared** unless every constant is already in the symbol DB. Check its
  `enums` list first.
* **A function-value global can't be called.** Only passed as an argument. See recipe 9.

```c
module cookbook;

native "void debugPrint(String)";
extern "bool isActorGroupDead(@CActorGroup)";

enum EMyState { eStateIdle, eStateActive }

CActorGroup g_Watched;   // value object: methods take &g_Watched implicitly

void pollGroup(@CActorGroup refGroup)   // reference: methods take refGroup directly
{
    while (!isActorGroupDead(refGroup)) {
        idle();               // required every loop iteration -- no idle() = VM
    }                          // aborts after 10,000 instructions ("Missing idle()?")
    debugPrint("group dead, local: " + toString(g_Watched.N()));
}

void castExample(@CActor actor)
{
    @CBiped biped = (@CBiped) actor;   // DYNCAST; null if actor is not a CBiped
    if (biped != null) {
        biped.setScannable(true);
    }
}
```
