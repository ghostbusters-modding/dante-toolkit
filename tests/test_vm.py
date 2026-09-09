"""The headless VM against GameWorld, ending with a smoke test of every level."""
import os
import math

import pytest

from dante.vm import VM, Ptr, VMFault, load
from dante.vm.world import make_world

TICK = 1.0 / 30.0


def run_until(vm, thread, max_ticks):
    """Tick until the thread is done or max_ticks elapse; returns ticks used."""
    ticks = 0
    while thread.state != "done" and ticks < max_ticks:
        vm.run(ticks=1)
        ticks += 1
    return ticks


@pytest.fixture
def global_vm(corpus):
    """A VM holding just global.dante, over a fresh GameWorld."""
    world = make_world()
    vm = VM([load(os.path.join(corpus, "global.dante"))], world=world)
    vm.init_modules()
    return vm, world


# --------------------------------------------------------------- script maths
@pytest.mark.parametrize("proto,args,want", [
    ("int absInt(int)", (-5,), 5),
    ("int absInt(int)", (7,), 7),
    ("float round(float)", (2.6,), 3.0),
    ("float round(float)", (2.4,), 2.0),
    ("float absFloat(float)", (-2.5,), 2.5),
])
def test_math(global_vm, proto, args, want):
    vm, _ = global_vm
    assert vm.call(proto, *args) == want


def test_vector_constructor(global_vm):
    vm, _ = global_vm
    v = vm.call("Vector getVector(float,float,float)", 1.0, 2.0, 3.0)
    assert tuple(v) == (1.0, 2.0, 3.0)


def test_angle_conversion(global_vm):
    vm, _ = global_vm
    assert abs(vm.call("float radToDeg(float)", math.pi) - 180.0) < 0.1
    assert abs(vm.call("float degToRad(float)", 180.0) - math.pi) < 0.01


def test_string_ops(global_vm):
    vm, _ = global_vm
    assert vm.call("String StringVector(Vector)", (1.0, 2.0, 3.0)) == "***(1, 2, 3)***"


# ------------------------------------------------------------------- objects
def test_actor_group_liveness(global_vm):
    vm, world = global_vm
    g = Ptr(world.make_object(vm, "CActorGroup", "g"), 0)
    members = [Ptr(world.make_object(vm, "CCharacter", n), 0) for n in ("c1", "c2")]
    world.natives["void CActorGroup::constructor{@CActorGroup}()"](vm, g)
    for c in members:
        world.natives["int CActorGroup::add{@CActorGroup}(@CActor)"](vm, g, c)

    assert vm.call("bool isActorGroupDead(@CActorGroup)", g) is False
    for c in members:
        world.kill(c.obj)
    assert vm.call("bool isActorGroupDead(@CActorGroup)", g) is True


# ------------------------------------------------------------------ threading
def test_wait_blocks_for_the_right_number_of_ticks(global_vm):
    vm, _ = global_vm
    t = vm.start("void wait(float)", 1.0)
    ticks = run_until(vm, t, 40)
    assert 29 < ticks <= 31, "wait(1.0) finished at tick %d" % ticks


def test_timer_expires(global_vm):
    vm, _ = global_vm
    tid = vm.call("int startTimer(float)", 0.5)
    assert isinstance(tid, int) and tid > 0
    assert vm.call("bool isTimerActive(int)", tid) is True
    vm.run(ticks=20)                    # 0.5 s is ~16 ticks, plus margin
    assert vm.call("bool isTimerActive(int)", tid) is False


def test_dbsay_speaks_then_returns(global_vm):
    vm, world = global_vm
    char = Ptr(world.make_object(vm, "CCharacter", "Ray"), 0)
    entry = Ptr(world.make_global(vm, "CDialogDatabaseEntry", "greeting"), 0)

    t = vm.start("void dbSay(@CCharacter,@CDialogDatabaseEntry,bool)", char, entry, False)
    run_until(vm, t, 40)
    assert t.state == "done"
    assert world.dialogue and world.dialogue[-1] == ("Ray", "greeting")


def test_dbsay_narrates_without_a_speaker(global_vm):
    vm, world = global_vm
    entry = Ptr(world.make_global(vm, "CDialogDatabaseEntry", "greeting"), 0)
    t = vm.start("void dbSay(@CCharacter,@CDialogDatabaseEntry,bool)", None, entry, False)
    run_until(vm, t, 70)
    assert t.state == "done"


# ------------------------------------------------------------- shipped levels
@pytest.fixture(scope="module")
def cemetery2(corpus):
    """cemetery2 with setupLevel() run and main() ticked 100 times."""
    world = make_world()
    vm = VM([load(os.path.join(corpus, "global.dante")),
             load(os.path.join(corpus, "cemetery2.dante"))], world=world)
    vm.init_modules()
    vm.call("void setupLevel()")
    vm.start("void main()")
    live = vm.run(ticks=100)
    return vm, world, live


def test_cemetery2_defines_its_checkpoints(cemetery2, report):
    vm, world, _ = cemetery2
    defines = [c for c in world.checkpoints if c[0] == "define"]
    assert len(defines) == 10
    assert all(p.startswith("void checkpoint_") and p.endswith("()") for _, p, _ in defines)
    report("vm            cemetery2: %d distinct natives across setupLevel()+100 ticks"
           % len(set(p for _, p, _, _ in vm.calls)))


def test_cemetery2_main_keeps_running(cemetery2):
    vm, _, live = cemetery2
    assert any(t.name == "main" for t in live)
    assert sum(1 for _, p, _, _ in vm.calls if p == "void idle()") > 0


def test_every_module_sets_up(corpus, corpus_module, report):
    """init_modules() + setupLevel() must not fault in any shipped module."""
    stem = os.path.basename(corpus_module)[:-len(".dante")]
    world = make_world()
    mods = [load(corpus_module)]
    if stem != "global":
        mods.insert(0, load(os.path.join(corpus, "global.dante")))
    vm = VM(mods, world=world)
    try:
        vm.init_modules()
        if "void setupLevel()" in vm.funcs:
            vm.call("void setupLevel()")
    except VMFault as ex:
        pytest.fail("%s: VM fault: %s" % (stem, ex))
    report("vm smoke      %-22s %6d instructions, %3d unknown native(s)"
           % (stem, vm.instr_count, len(world.unknown)))
