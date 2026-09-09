"""A headless interpreter for the Dante VM.

Natives are answered by a "world" object; an unstubbed one is logged, never fatal."""
import os
import re
import sys
import math
import struct
import importlib
import importlib.util

from .. import module as DV

MAX_SLICE = 10000          # engine: "Infinite loop. (Missing idle() call?)"
STACK_LIMIT = 0x1FEF       # bytes; ENTER fails when fp*2 + n*2 > this
VEC_TYPES = ("Vector",)


class VMFault(Exception):
    pass


# ---------------------------------------------------------------------------
# values
class Mem:
    """A slot-addressed region.  Keys are script-unit offsets (multiples of 4)."""
    __slots__ = ("name", "slots")

    def __init__(self, name):
        self.name = name
        self.slots = {}

    def get(self, off):
        return self.slots.get(off)

    def set(self, off, v):
        self.slots[off] = v

    def __repr__(self):
        return "<%s>" % self.name


class EngObj(Mem):
    """A fake engine object.  `props` is the world's to use, `slots` are script-visible."""
    __slots__ = ("cls", "props")

    def __init__(self, cls, name):
        Mem.__init__(self, name)
        self.cls = cls
        self.props = {}

    def __repr__(self):
        return "<%s %s>" % (self.cls, self.name)


class Ptr:
    __slots__ = ("mem", "off")

    def __init__(self, mem, off=0):
        self.mem = mem
        self.off = off

    def __eq__(self, o):
        return isinstance(o, Ptr) and o.mem is self.mem and o.off == self.off

    def __ne__(self, o):
        return not self.__eq__(o)

    def __hash__(self):
        return hash((id(self.mem), self.off))

    def __add__(self, n):
        return Ptr(self.mem, self.off + n)

    def get(self, k=0):
        return self.mem.get(self.off + k)

    def set(self, v, k=0):
        self.mem.set(self.off + k, v)

    @property
    def obj(self):
        """The EngObj this points at (offset 0), else None."""
        return self.mem if isinstance(self.mem, EngObj) else None

    def __repr__(self):
        if isinstance(self.mem, EngObj) and self.off == 0:
            return repr(self.mem)
        return "&%s+%X" % (self.mem.name, self.off)


class FuncRef:
    __slots__ = ("mod", "off", "proto")

    def __init__(self, mod, off, proto):
        self.mod, self.off, self.proto = mod, off, proto

    def __eq__(self, o):
        return isinstance(o, FuncRef) and o.mod is self.mod and o.off == self.off

    def __ne__(self, o):
        return not self.__eq__(o)

    def __hash__(self):
        return hash((id(self.mod), self.off))

    def __repr__(self):
        return "fn:%s" % self.proto


class NativeRef:
    __slots__ = ("proto",)

    def __init__(self, proto):
        self.proto = proto

    def __eq__(self, o):
        return isinstance(o, NativeRef) and o.proto == self.proto

    def __ne__(self, o):
        return not self.__eq__(o)

    def __hash__(self):
        return hash(self.proto)

    def __repr__(self):
        return "native:%s" % self.proto


def as_int(v):
    if v is None:
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        v &= 0xFFFFFFFF
        return v - 0x100000000 if v & 0x80000000 else v
    if isinstance(v, float):
        return DV.s32(*DV.w32(DV.fbits(v)))
    return v                     # Ptr / FuncRef / NativeRef / str: compared, never added


def as_float(v):
    if v is None:
        return 0.0
    if isinstance(v, float):
        return v
    if isinstance(v, bool):
        return DV.bitsf(int(v))
    if isinstance(v, int):
        return DV.bitsf(v & 0xFFFFFFFF)
    raise VMFault("float operand holds %r" % (v,))


def as_str(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    raise VMFault("String operand holds %r" % (v,))


def as_bool(v):
    return as_int(v) != 0 if not isinstance(v, (Ptr, FuncRef, NativeRef, str)) else True


def norm(v):
    return 0 if v is None else v


def coerce(v, typ):
    """Python value -> slot value for a parameter of script type `typ`."""
    if typ == "float":
        return float(v or 0)
    if typ == "bool":
        return 1 if v else 0
    if typ == "int":
        return int(v or 0)
    if typ == "String":
        return "" if v is None else str(v)
    if typ in VEC_TYPES:
        return tuple(float(x) for x in (v or (0, 0, 0)))
    return v


def fmt_g(f):
    return "%g" % f


# ---------------------------------------------------------------------------
# prototypes
_PROTO_RE = re.compile(r"^(?P<ret>\S+)\s+(?:(?P<cls>[A-Za-z_0-9]+)::)?(?P<name>[A-Za-z_0-9]+)"
                       r"(?:\{(?P<this>[^}]*)\})?\((?P<params>.*)\)$")


def split_params(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [p.strip() for p in out]


class Proto:
    """`ret [Class::]name[{@Class}](params)` split into its parts + slot sizes."""
    _cache = {}

    def __new__(cls, text):
        p = cls._cache.get(text)
        if p is None:
            p = object.__new__(cls)
            p._init(text)
            cls._cache[text] = p
        return p

    def _init(self, text):
        m = _PROTO_RE.match(text.strip())
        if not m:
            raise VMFault("bad prototype %r" % text)
        self.text = text
        self.ret = m.group("ret")
        self.cls = m.group("cls")
        self.name = m.group("name")
        self.this = m.group("this")
        self.params = split_params(m.group("params"))
        self.sizes = [12 if p in VEC_TYPES else 4 for p in self.params]
        self.ret_size = 0 if self.ret == "void" else (12 if self.ret in VEC_TYPES else 4)

    def __repr__(self):
        return self.text


# ---------------------------------------------------------------------------
# world shim
class World:
    """Base shim: `natives` maps a prototype to callable(vm, this, *params)."""

    def __init__(self, natives=None):
        self.natives = dict(natives or {})
        self.natives.setdefault("void idle()", lambda vm, this: vm.idle())
        self.unknown = {}          # proto -> count
        self.time_slice = 1.0 / 30.0
        self.log = []

    # engine globals the scripts reference but no module exports
    def make_global(self, vm, typ, name):
        """Return the region a `D "<typ> <name>"` symbol resolves to."""
        if typ.startswith("@") or typ.startswith("("):
            cell = Mem("%s %s" % (typ, name))
            inner = typ[1:]
            if inner in ("float", "int", "bool", "String"):
                target = Mem("*%s" % name)
                target.set(0, vm.world_scalar(name, inner))
                cell.set(0, Ptr(target, 0))
            elif inner.startswith("@") or inner.startswith("("):
                cell.set(0, None)
            else:
                cell.set(0, Ptr(self.make_object(vm, inner, name), 0))
            return cell
        if typ in ("float", "int", "bool", "String", "Vector"):
            cell = Mem("%s %s" % (typ, name))
            cell.set(0, {"float": 0.0, "int": 0, "bool": 0, "String": ""}.get(typ))
            return cell
        return self.make_object(vm, typ, name)              # value object: CFoo name

    def make_object(self, vm, cls, name):
        o = EngObj(cls, name)
        vm.objects[name] = o
        return o

    def isa(self, obj, cls):
        """DYNCAST test.  Default: same class name, or unknown -> permissive."""
        if obj.cls == cls:
            return True
        parents = getattr(self, "parents", None)
        if parents and obj.cls in parents:
            p = obj.cls
            while p:
                if p == cls:
                    return True
                p = parents.get(p)
            return False
        return True

    def enum_value(self, vm, sym):
        """`I "EFoo eValue"` -> int.  Default: stable index per enum type."""
        typ, _, name = sym.partition(" ")
        table = vm.enums.setdefault(typ, {})
        return table.setdefault(name, len(table))

    def member_offset(self, vm, sym):
        return None                                          # None = synthesize

    def call_native(self, vm, thread, proto, this, args):
        fn = self.natives.get(proto.text)
        if fn is None:
            self.unknown[proto.text] = self.unknown.get(proto.text, 0) + 1
            vm.note("unknown native %s" % proto.text)
            return None
        return fn(vm, this, *args)

    def on_tick(self, vm):
        pass


# ---------------------------------------------------------------------------
class Thread:
    __slots__ = ("id", "name", "stack", "mod", "pc", "fp", "dtors", "state",
                 "slice_count", "result_ptr", "proto")

    def __init__(self, tid, name, mod, pc, fp):
        self.id = tid
        self.name = name
        self.stack = Mem("stack%d" % tid)
        self.mod = mod
        self.pc = pc
        self.fp = fp
        self.dtors = []           # ('try', pc, fp, mod) | ('obj', Ptr, NativeRef) | ('str', Ptr)
        self.state = "run"        # run | idle | done
        self.slice_count = 0
        self.result_ptr = None
        self.proto = None

    def __repr__(self):
        return "t%d(%s)" % (self.id, self.name)


class VM:
    def __init__(self, modules, world=None, trace=False, out=None):
        self.modules = list(modules)
        self.world = world or World()
        self.trace = trace
        self.out = out or sys.stdout
        self.threads = []
        self.next_tid = 1
        self.current = None
        self.calls = []           # (tid, proto text, args, ret)
        self.notes = []
        self.objects = {}         # name -> EngObj
        self.enums = {}
        self.globals = {}         # "type name" -> Mem region
        self.members = {}         # "type S::member" -> offset
        self._synth = {}
        self.static_dtors = []
        self.funcs = {}           # proto -> FuncRef
        self.data = {}            # module -> Mem
        self.instr_count = 0
        self.max_slice = MAX_SLICE
        self.tick_no = 0
        for m in self.modules:
            self._load_module(m)

    # -- loading ----------------------------------------------------------------
    def _load_module(self, m):
        d = Mem("%s.data" % m.name)
        for off, s in m.strings:
            d.set(off, s)
        self.data[m] = d
        for k, off, proto in m.exports:
            if k == "C":
                self.funcs.setdefault(proto, FuncRef(m, off, proto))
        for k, text, val in m.assumptions:
            if k == "M":
                self.members.setdefault(text, val)

    def init_modules(self):
        """Run every module's `__<stem>_init()` (global.dante first)."""
        mods = sorted(self.modules, key=lambda m: 0 if m.name == "global" else 1)
        for m in mods:
            inits = [p for k, o, p in m.exports if k == "C" and p.startswith("void __") and p.endswith("_init()")]
            for p in inits:
                self.call(p)

    def find_func(self, proto, prefer=None):
        if prefer is not None:
            for k, off, p in prefer.exports:
                if k == "C" and p == proto:
                    return FuncRef(prefer, off, p)
        f = self.funcs.get(proto)
        if f is None:
            raise VMFault("unresolved script function %r" % proto)
        return f

    def resolve_global(self, sym, prefer):
        """`D "<type> <name>"` -> Ptr to the symbol (module data, global.dante, or world)."""
        if sym.startswith("__") and sym.endswith("_data"):
            for m in self.modules:
                if m.stem_data == sym:
                    return Ptr(self.data[m], 0)
            raise VMFault("unknown data base %s" % sym)
        order = ([prefer] if prefer is not None else []) + [m for m in self.modules if m is not prefer]
        for m in order:
            for k, off, text in m.exports:
                if k == "D" and text == sym:
                    return Ptr(self.data[m], off)
        r = self.globals.get(sym)
        if r is None:
            typ, _, name = sym.partition(" ")
            r = self.world.make_global(self, typ, name)
            self.globals[sym] = r
        return Ptr(r, 0)

    def world_scalar(self, name, typ):
        if name == "gTimeSlice":
            return self.world.time_slice
        return {"float": 0.0, "int": 0, "bool": 0, "String": ""}[typ]

    def reset_time_slice(self):
        """Refill gTimeSlice before a thread runs, as the engine does each frame."""
        cell = self.globals.get("@float gTimeSlice")
        if cell is not None:
            cell.get(0).set(float(self.world.time_slice))

    def member_off(self, sym):
        v = self.members.get(sym)
        if v is None:
            v = self.world.member_offset(self, sym)
        if v is None:
            # synthesize a stable, unique offset per struct member (past any known ones)
            struct_name = sym.split("::")[0].split(" ")[-1]
            used = [o for t, o in self.members.items() if t.split("::")[0].split(" ")[-1] == struct_name]
            v = (max(used) + 12) if used else 0x100
            while v in used:
                v += 4
            self.note("synthesized member offset %s = %d" % (sym, v))
        self.members[sym] = v
        return v

    def struct_get(self, ptr, struct_name, member):
        """World helper: read `ptr-><struct_name>::<member>` by name."""
        for text, off in self.members.items():
            if text.endswith(" %s::%s" % (struct_name, member)):
                return ptr.get(off)
        return None

    # -- diagnostics ------------------------------------------------------------
    def note(self, msg):
        self.notes.append(msg)
        if self.trace:
            self.out.write("  ; %s\n" % msg)

    def where(self, t=None):
        t = t or self.current
        if t is None:
            return "(no thread)"
        fn = t.mod.func_of(t.pc)
        return "%s %s@%X in %s" % (t, t.mod.name, t.pc, t.mod.func_at.get(fn, "?"))

    def fault(self, msg):
        raise VMFault("%s at %s" % (msg, self.where()))

    # -- threads ----------------------------------------------------------------
    def new_thread(self, name, mod, pc, fp):
        t = Thread(self.next_tid, name, mod, pc, fp)
        self.next_tid += 1
        t.stack.set(fp, -1)            # return pc: negative = thread finishes on RET
        t.stack.set(fp + 4, 0)
        self.threads.append(t)
        return t

    def thread_active(self, tid):
        return any(t.id == tid and t.state != "done" for t in self.threads)

    def start(self, proto_or_ref, *args, name=None):
        """Create a thread to run `proto(args)`.  Read its result with thread_result()."""
        f = proto_or_ref if isinstance(proto_or_ref, FuncRef) else self.find_func(proto_or_ref)
        p = Proto(f.proto)
        if len(args) != len(p.params):
            raise VMFault("%s called with %d args" % (p.text, len(args)))
        total = sum(p.sizes)
        fp = total + p.ret_size
        t = self.new_thread(name or p.name, f.mod, f.off, fp)
        t.proto = p
        off = fp - total
        for a, typ, sz in zip(args, p.params, p.sizes):
            self._write_val(Ptr(t.stack, off), coerce(a, typ), sz)
            off += sz
        if p.ret_size:
            t.result_ptr = Ptr(t.stack, 0)
        return t

    def thread_result(self, t):
        if t.proto is None or not t.proto.ret_size:
            return None
        return self._read_val(t.result_ptr, t.proto.ret)

    def call(self, proto_or_ref, *args):
        """Run `proto(args)` to completion.  If it idles, returns None and keeps the thread."""
        t = self.start(proto_or_ref, *args)
        self.run_thread(t)
        if t.state != "done":
            self.note("%s idled inside an event call; left running" % t)
            return None
        return self.thread_result(t)

    def run(self, ticks=1):
        """Scheduler: each tick runs every live thread until it idles or finishes."""
        for _ in range(ticks):
            self.tick_no += 1
            self.world.on_tick(self)
            i = 0
            while i < len(self.threads):
                t = self.threads[i]
                i += 1
                if t.state == "done":
                    continue
                t.state = "run"
                self.reset_time_slice()
                self.run_thread(t)
            self.threads = [t for t in self.threads if t.state != "done"]
        return [t for t in self.threads if t.state != "done"]

    def run_thread(self, t):
        prev = self.current
        self.current = t
        t.slice_count = 0
        try:
            while t.state == "run":
                self.step(t)
        finally:
            self.current = prev

    def finish_thread(self, t):
        while t.dtors:
            self._run_dtor(t, t.dtors.pop())
        t.state = "done"

    def abort_thread(self, t):
        """Engine 'skip': unwind to the innermost TRY handler (or finish the thread)."""
        while t.dtors:
            e = t.dtors.pop()
            if e[0] == "try":
                t.pc, t.fp, t.mod = e[1], e[2], e[3]
                t.state = "run"
                return True
            self._run_dtor(t, e)
        t.state = "done"
        return False

    def _run_dtor(self, t, e):
        if e[0] == "obj":
            self.invoke_native(t, e[2].proto, this_ptr=e[1], args_ptr=None)
        # 'str' and 'try' entries need no action

    def idle(self):
        t = self.current
        if t is not None:
            t.state = "idle"

    # -- operands ---------------------------------------------------------------
    def operand(self, t, ins, k):
        """-> (addr Ptr or None, value).  Reads go through `value`; LEA/dest use addr."""
        o = ins.ops[k]
        m = o.mode
        if m == 0:
            return None, 0
        if m == 1:
            return None, 1
        if m == 2:
            return None, DV.s16(o.words[0])
        if m == 3 or m == 6:
            off = t.fp + o.value()
            if off < 0 and -off > t.fp + 0x1000 or off * 2 > STACK_LIMIT:
                self.fault("Stack %s in operand %d" % ("underflow" if off < 0 else "overflow", k))
            p = Ptr(t.stack, off)
            return p, p.get()
        if m == 4:
            v = o.value()
            for kind, sym in o.fix:
                if kind == "C":
                    return None, self.find_func(sym, prefer=t.mod)
                if kind == "N":
                    return None, NativeRef(sym)
                if kind == "I":
                    return None, self.world.enum_value(self, sym)
                if kind == "D":                     # rare: data address as an immediate
                    return None, self.resolve_global(sym, t.mod) + v
            return None, v
        if m == 5:
            base, off = None, o.value()
            for kind, sym in o.fix:
                if kind == "N":
                    return None, NativeRef(sym)
                if kind == "D":
                    base = self.resolve_global(sym, t.mod)
                elif kind == "M":
                    off += self.member_off(sym)
                elif kind == "I":
                    return None, self.world.enum_value(self, sym)
                elif kind == "C":
                    return None, self.find_func(sym, prefer=t.mod)
            if base is None:
                self.fault("mode-5 operand without a D/N fixup")
            p = base + off
            return p, p.get()
        if m == 7:
            slot = t.fp + DV.s16(o.words[0])
            ptr = t.stack.get(slot)
            off = DV.s16(o.words[1])
            for kind, sym in o.fix16:
                if kind == "M":
                    off += self.member_off(sym)
            if not isinstance(ptr, Ptr):
                self.fault("Invalid pointer in operand %d (%r)" % (k, ptr))
            p = ptr + off
            return p, p.get()
        self.fault("bad operand mode %d" % m)

    def addr(self, t, ins, k):
        p, _ = self.operand(t, ins, k)
        if p is None:
            self.fault("operand %d of %s is not addressable" % (k, ins.name))
        return p

    def val(self, t, ins, k):
        return self.operand(t, ins, k)[1]

    def vec(self, t, ins, k):
        p, v = self.operand(t, ins, k)
        if p is None:
            return (0.0, 0.0, 0.0)
        return (as_float(p.get(0)), as_float(p.get(4)), as_float(p.get(8)))

    def _write_val(self, p, v, size):
        if size == 12:
            v = tuple(v) if v is not None else (0.0, 0.0, 0.0)
            p.set(float(v[0]), 0)
            p.set(float(v[1]), 4)
            p.set(float(v[2]), 8)
        else:
            p.set(v)

    def _read_val(self, p, typ):
        if typ in VEC_TYPES:
            return (as_float(p.get(0)), as_float(p.get(4)), as_float(p.get(8)))
        v = p.get()
        if typ == "float":
            return as_float(v)
        if typ == "int":
            return as_int(v)
        if typ == "bool":
            return as_int(v) != 0
        if typ == "String":
            return as_str(v)
        return v

    # -- calls ------------------------------------------------------------------
    def invoke_native(self, t, proto_text, this_ptr, args_ptr):
        p = Proto(proto_text)
        args = []
        if args_ptr is not None:
            off = args_ptr.off + p.ret_size
            this = None
            if p.this:
                this = args_ptr.mem.get(off)
                off += 4
            for typ, sz in zip(p.params, p.sizes):
                args.append(self._read_val(Ptr(args_ptr.mem, off), typ))
                off += sz
        else:
            this = this_ptr
        ret = self.world.call_native(self, t, p, this, args)
        if p.ret_size and args_ptr is not None:
            if ret is None:
                # Only the scalar types have a real zero.  A reference-shaped slot has to
                # default to None, or an unstubbed native hands a method call `this=0`.
                if p.ret in VEC_TYPES:
                    ret = (0.0, 0.0, 0.0)
                elif p.ret == "String":
                    ret = ""
                elif p.ret == "int":
                    ret = 0
                elif p.ret == "float":
                    ret = 0.0
                elif p.ret == "bool":
                    ret = False
                else:
                    ret = None
            if p.ret == "bool":
                ret = 1 if ret else 0
            self._write_val(args_ptr, ret, p.ret_size)
        self.calls.append((t.id, p.text, args, ret))
        if self.trace:
            self.out.write("[%s] %s%s(%s)%s\n" % (
                t, ("%r." % this) if p.this else "", p.name,
                ", ".join(repr(a) for a in args),
                "" if not p.ret_size else " -> %r" % (ret,)))
        return ret

    def call_script(self, t, f, new_fp, ret_mod, ret_pc):
        t.stack.set(new_fp, (ret_mod, ret_pc))
        t.stack.set(new_fp + 4, t.fp)
        t.fp = new_fp
        t.mod = f.mod
        t.pc = f.off

    # -- the interpreter --------------------------------------------------------
    def step(self, t):
        ins = t.mod.by_off.get(t.pc)
        if ins is None:
            self.fault("pc %X is not on an instruction" % t.pc)
        t.slice_count += 1
        self.instr_count += 1
        if t.slice_count > self.max_slice:
            self.fault("Infinite loop. (Missing idle() call?)")
        op = ins.op
        name = DV.OPNAME.get(op)
        nxt = t.pc + ins.size
        S = t.stack

        if op == 0:                                     # RET
            ret = S.get(t.fp)
            saved = S.get(t.fp + 4)
            if not isinstance(ret, tuple):
                self.finish_thread(t)
                return
            t.mod, t.pc = ret
            t.fp = saved
            return
        if op == 1:                                     # ENTER n
            n = self.val(t, ins, 0)
            if (t.fp + n) * 2 > STACK_LIMIT:
                self.fault("Stack overflow")
            t.pc = nxt
            return
        if op == 61:                                    # CALL
            fn = self.val(t, ins, 0)
            if isinstance(fn, NativeRef):
                a = self.val(t, ins, 2)
                args_ptr = None if a == -1 else Ptr(S, t.fp + a)
                self.invoke_native(t, fn.proto, None, args_ptr)
                t.pc = nxt
                return
            if isinstance(fn, FuncRef):
                frame = self.val(t, ins, 1)
                self.call_script(t, fn, t.fp + frame, t.mod, nxt)
                return
            self.fault("CALL of a non-function %r" % (fn,))
        if op == 65:                                    # THREAD d, fn, frame
            fn = self.val(t, ins, 1)
            if not isinstance(fn, FuncRef):
                self.fault("THREAD of a non-function %r" % (fn,))
            frame = self.val(t, ins, 2)
            nt = self.new_thread(Proto(fn.proto).name, fn.mod, fn.off, frame)
            nt.proto = Proto(fn.proto)
            d = self.addr(t, ins, 0)
            d.set(nt.id, 0)
            d.set(Ptr(nt.stack, 0), 4)
            t.pc = nxt
            return

        # --- everything else: no control transfer except jumps ---
        if op == 3:                                     # DTORPOP d
            d = self.addr(t, ins, 0)
            stack = self.static_dtors if ins.ops[0].mode == 5 else t.dtors
            if not stack:
                self.fault("DTORPOP on an empty destructor stack")
            e = stack.pop()
            if e[0] == "try" or e[1] != d:
                self.note("DTORPOP %r does not match top entry %r" % (d, e))
            self._run_dtor(t, e)
        elif op == 4:                                   # STRFREE d
            pass
        elif op == 5:                                   # TRY -> L
            t.dtors.append(("try", t.pc + self.val(t, ins, 0), t.fp, t.mod))
        elif op == 16:                                  # ENDTRY
            while t.dtors and t.dtors[-1][0] != "try":
                self.note("ENDTRY over a live destructor entry")
                t.dtors.pop()
            if t.dtors:
                t.dtors.pop()
        elif op == 6:                                   # MOV
            self.addr(t, ins, 0).set(norm(self.val(t, ins, 1)))
        elif op == 7:                                   # MOVV
            d = self.addr(t, ins, 0)
            self._write_val(d, self.vec(t, ins, 1), 12)
        elif op == 8:                                   # STRCPY
            self.addr(t, ins, 0).set(as_str(self.val(t, ins, 1)))
        elif op == 9:                                   # FTOI
            self.addr(t, ins, 0).set(int(math.floor(as_float(self.val(t, ins, 1)))))
        elif op == 10:                                  # ITOF
            self.addr(t, ins, 0).set(float(as_int(self.val(t, ins, 1))))
        elif op == 11:                                  # ITOS
            self.addr(t, ins, 0).set("%d" % as_int(self.val(t, ins, 1)))
        elif op == 12:                                  # FTOS
            self.addr(t, ins, 0).set(fmt_g(as_float(self.val(t, ins, 1))))
        elif op == 13:                                  # BTOS
            self.addr(t, ins, 0).set("true" if as_int(self.val(t, ins, 1)) else "false")
        elif op == 14:                                  # VTOS
            v = self.vec(t, ins, 1)
            self.addr(t, ins, 0).set("{%s, %s, %s}" % tuple(fmt_g(x) for x in v))
        elif op == 15:                                  # LEA
            p, v = self.operand(t, ins, 1)
            self.addr(t, ins, 0).set(p if p is not None else v)
        elif 17 <= op <= 32:
            self._arith(t, ins, op)
        elif 33 <= op <= 46:
            if self._cmp(t, ins, op):
                t.pc = t.pc + self.val(t, ins, 0)
                return
        elif 47 <= op <= 60:
            self.addr(t, ins, 0).set(1 if self._cmp(t, ins, op) else 0)
        elif op == 62:                                  # CTOR d, thread, dtor
            d = self.addr(t, ins, 0)
            fn = self.val(t, ins, 2)
            th = self.val(t, ins, 1)
            stack = self.static_dtors if ins.ops[0].mode == 5 else self._dtor_stack(t, th)
            stack.append(("obj", d, fn))
        elif op == 63:                                  # STRNEW d, thread
            d = self.addr(t, ins, 0)
            d.set("")
            self._dtor_stack(t, self.val(t, ins, 1)).append(("str", d))
        elif op == 64:                                  # DYNCAST d, a, classId
            a = self.val(t, ins, 1)
            cid = as_int(self.val(t, ins, 2)) & 0xFFFFFFFF
            cls = DV.class_table().get(cid, "class#%08X" % cid)
            res = None
            if isinstance(a, Ptr) and a.obj is not None:
                res = a if self.world.isa(a.obj, cls) else None
            elif isinstance(a, Ptr):
                res = a
            self.addr(t, ins, 0).set(res)
        else:
            self.fault("invalid opcode %d" % op)
        t.pc = nxt

    def _dtor_stack(self, t, th):
        if not th:
            return t.dtors
        for x in self.threads:
            if x.id == th:
                return x.dtors
        self.fault("CTOR/STRNEW on unknown thread %r" % (th,))

    def _arith(self, t, ins, op):
        d = self.addr(t, ins, 0)
        if op in (17, 21, 24, 28, 31):                  # int
            a, b = as_int(self.val(t, ins, 1)), as_int(self.val(t, ins, 2))
            if op == 17:
                r = a + b
            elif op == 21:
                r = a - b
            elif op == 24:
                r = a * b
            elif op == 28:
                if b == 0:
                    self.fault("Divide by zero")
                r = int(a / b)                           # C truncation
            else:
                if b == 0:
                    self.fault("Divide by zero")
                r = a % abs(b)                           # engine: non-negative result
            d.set(as_int(r))
        elif op in (18, 22, 25, 29, 32):                # float
            a, b = as_float(self.val(t, ins, 1)), as_float(self.val(t, ins, 2))
            if op == 18:
                r = a + b
            elif op == 22:
                r = a - b
            elif op == 25:
                r = a * b
            elif op == 29:
                if b == 0.0:
                    self.fault("Divide by zero")
                r = a / b
            else:
                if b == 0.0:
                    self.fault("Divide by zero")
                r = a - math.floor(a / b) * b
            d.set(float(struct.unpack("<f", struct.pack("<f", r))[0]))
        elif op == 19:                                  # ADDV
            a, b = self.vec(t, ins, 1), self.vec(t, ins, 2)
            self._write_val(d, tuple(x + y for x, y in zip(a, b)), 12)
        elif op == 23:                                  # SUBV
            a, b = self.vec(t, ins, 1), self.vec(t, ins, 2)
            self._write_val(d, tuple(x - y for x, y in zip(a, b)), 12)
        elif op == 26:                                  # MULVS
            a, s = self.vec(t, ins, 1), as_float(self.val(t, ins, 2))
            self._write_val(d, tuple(x * s for x in a), 12)
        elif op == 27:                                  # DOT
            a, b = self.vec(t, ins, 1), self.vec(t, ins, 2)
            d.set(float(sum(x * y for x, y in zip(a, b))))
        elif op == 30:                                  # DIVVS
            a, s = self.vec(t, ins, 1), as_float(self.val(t, ins, 2))
            if s == 0.0:
                self.fault("Divide by zero")
            self._write_val(d, tuple(x / s for x in a), 12)
        elif op == 20:                                  # STRCAT
            d.set(as_str(self.val(t, ins, 1)) + as_str(self.val(t, ins, 2)))

    # comparison kinds: (test, type) for opcodes 33..60
    _CMP = {33: ("lt", "I"), 34: ("lt", "F"), 35: ("le", "I"), 36: ("le", "F"),
            37: ("eq", "I"), 38: ("eq", "F"), 39: ("eq", "V"), 40: ("eq", "S"), 41: ("eq", "B"),
            42: ("ne", "I"), 43: ("ne", "F"), 44: ("ne", "V"), 45: ("ne", "S"), 46: ("ne", "B"),
            47: ("lt", "I"), 48: ("lt", "F"), 49: ("le", "I"), 50: ("le", "F"),
            51: ("eq", "I"), 52: ("eq", "F"), 53: ("eq", "V"), 54: ("eq", "S"), 55: ("eq", "B"),
            56: ("ne", "I"), 57: ("ne", "F"), 58: ("ne", "V"), 59: ("ne", "S"), 60: ("ne", "B")}

    def _cmp(self, t, ins, op):
        test, typ = self._CMP[op]
        if typ == "V":
            a, b = self.vec(t, ins, 1), self.vec(t, ins, 2)
        elif typ == "F":
            a, b = as_float(self.val(t, ins, 1)), as_float(self.val(t, ins, 2))
        elif typ == "S":
            a, b = as_str(self.val(t, ins, 1)), as_str(self.val(t, ins, 2))
        elif typ == "B":
            a, b = as_bool(self.val(t, ins, 1)), as_bool(self.val(t, ins, 2))
        else:
            a, b = norm(self.val(t, ins, 1)), norm(self.val(t, ins, 2))
            if test in ("lt", "le"):
                a, b = as_int(a), as_int(b)
        if test == "eq":
            return a == b
        if test == "ne":
            return a != b
        if test == "lt":
            return a < b
        return a <= b


# ---------------------------------------------------------------------------
# CLI helpers
def load(path):
    return DV.load(path)


def load_world(path):
    """Load a world from a Python file or module name; it exposes WORLD or make_world()."""
    if path is None:
        return World()
    # Older world files say `import dantevm`.  Alias it here so they get this module
    # and not a second copy whose Ptr/EngObj classes would fail every isinstance check.
    sys.modules.setdefault("dantevm", sys.modules[__name__])
    if os.path.exists(path):
        spec = importlib.util.spec_from_file_location("dante_vm_world_file", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(path)
    if hasattr(mod, "make_world"):
        return mod.make_world()
    return getattr(mod, "WORLD")


def parse_arg(vm, s):
    if s == "true":
        return 1
    if s == "false":
        return 0
    if s == "null":
        return None
    m = re.match(r"^vec\(([^,]+),([^,]+),([^)]+)\)$", s)
    if m:
        return tuple(float(x) for x in m.groups())
    m = re.match(r"^obj:([A-Za-z_0-9]+):([A-Za-z_0-9]+)$", s)
    if m:
        return Ptr(vm.world.make_object(vm, m.group(1), m.group(2)), 0)
    if re.match(r"^-?\d+$", s):
        return int(s)
    if re.match(r"^-?\d*\.\d+(e-?\d+)?$|^-?\d+\.$", s):
        return float(s)
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def build_vm(args):
    mods = [load(p) for p in (args.lib or [])] + [load(args.module)]
    world = load_world(getattr(args, "world", None))
    vm = VM(mods, world=world, trace=args.trace)
    if not getattr(args, "no_init", False):
        vm.init_modules()
    return vm


def cmd_call(args):
    vm = build_vm(args)
    vals = [parse_arg(vm, a) for a in args.args]
    r = vm.call(args.proto, *vals)
    print("%s(%s) -> %r" % (args.proto, ", ".join(repr(v) for v in vals), r))
    report(vm, args)
    return 0


def cmd_run(args):
    vm = build_vm(args)
    for e in args.entry or ["void setupLevel()", "void main()"]:
        if e not in vm.funcs:
            print("(no export %r -- skipped)" % e)
            continue
        vm.start(e)
        vm.run_thread(vm.threads[-1])
    live = vm.run(ticks=args.ticks)
    print("after %d tick(s): %d live thread(s): %s" % (args.ticks, len(live), ", ".join(map(repr, live))))
    report(vm, args)
    return 0


def report(vm, args):
    print("%d instructions, %d native calls, %d threads created" % (vm.instr_count, len(vm.calls), vm.next_tid - 1))
    if vm.world.unknown:
        print("unknown natives (%d): %s" % (len(vm.world.unknown), ", ".join(
            "%s x%d" % kv for kv in sorted(vm.world.unknown.items(), key=lambda kv: -kv[1])[:20])))


def cmd_natives(args):
    d = load(args.module)
    seen = {}
    for k, sym, sites in d.fixups:
        if k == "N":
            seen[sym] = len(sites)
    for sym, n in sorted(seen.items(), key=lambda kv: kv[0].lower()):
        print("%4d  %s" % (n, sym))
    print("%d natives" % len(seen))
    return 0


def register(sub):
    """Mount the `dante vm` subcommand group on the CLI."""
    vm = sub.add_parser("vm", help="run compiled modules headlessly")
    vsub = vm.add_subparsers(dest="vmcmd", required=True)

    def common(p):
        p.add_argument("module")
        p.add_argument("--lib", action="append", help="extra module(s), e.g. global.dante")
        p.add_argument("--world", help="python file, or module name, exposing WORLD or "
                                       "make_world() (e.g. dante.vm.world)")
        p.add_argument("--trace", action="store_true", help="print every native call")
        p.add_argument("--no-init", action="store_true", help="skip __<stem>_init()")

    p = vsub.add_parser("call", help="run one export to completion")
    common(p)
    p.add_argument("proto")
    p.add_argument("args", nargs="*")
    p.set_defaults(fn=cmd_call)
    p = vsub.add_parser("run", help="start entries as threads and tick the scheduler")
    common(p)
    p.add_argument("--entry", action="append")
    p.add_argument("--ticks", type=int, default=1)
    p.set_defaults(fn=cmd_run)
    p = vsub.add_parser("natives", help="list the natives a module calls")
    p.add_argument("module")
    p.set_defaults(fn=cmd_natives)
